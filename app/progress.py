from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 进度校准：用历史上「真实运行」记录推导转写速度比与非转写开销。
#
# 数据源 = data/results/*.json 里的 diagnostics：
#   media_duration_seconds / transcribe_seconds / total_seconds / asr_speed_x
# 必须先过滤掉 Mock 模式（diagnostics 为 None，或 transcribe_seconds==0 的假数据），
# 否则中位数会被 180/0 这种脏值污染（已踩过坑）。
# ---------------------------------------------------------------------------

# 安全默认值：取自 8 条真实样本（asr_speed_x 中位数 ≈ 7.7×，非转写开销中位数 ≈ 77s）。
# 当真实样本不足时使用，保证进度条在任何环境都有合理形状。
_DEFAULT_ASR_SPEED_X = 7.7
_DEFAULT_NON_TRANSCRIBE_S = 77.0
# 下载阶段固定估算（历史数据未单独记录下载耗时，给一个保守值用于 ETA 与首段进度）。
_DOWNLOAD_EST_S = 25.0
# 至少多少条真实样本才采用学习值，否则回退默认。
_MIN_SAMPLES = 3
# 校准缓存 TTL（秒）：让进度条随新记录缓慢自学习，又不必每次轮询都扫盘。
_CALIBRATION_TTL_S = 300


@dataclass(frozen=True)
class Calibration:
    asr_speed_x: float          # 转写速度比：视频 1 秒 ≈ 实际 transcribe_seconds / asr_speed_x
    non_transcribe_s: float     # 下载 + LLM + 开销（total - transcribe）的中位估算
    download_est_s: float       # 下载阶段固定估算
    samples: int                # 参与计算的真实样本数
    non_transcribe_ratio: float = 0.0  # 非转写开销 / 原视频时长的中位比值


_CALIBRATION_CACHE: dict[str, tuple[float, Calibration]] = {}
_CALIBRATION_LOCK = threading.Lock()


def invalidate_calibration(results_dir: str | Path | None = None) -> None:
    """让刚完成的真实样本立即参与下一次任务的 ETA 估算。"""
    with _CALIBRATION_LOCK:
        if results_dir is None:
            _CALIBRATION_CACHE.clear()
        else:
            _CALIBRATION_CACHE.pop(str(results_dir), None)


def _scan_real_run_details(results_dir: str | Path) -> list[tuple[float, float, float]]:
    """扫描结果目录，返回 [(asr_speed_x, non_transcribe_s, media_duration), ...]。

    只保留：media_duration、transcribe、total 都非空，且 transcribe_seconds > 0
    的记录——这正是「真实 Paraformer 转写」的判别条件，Mock 会被剔除。
    """
    samples: list[tuple[float, float, float]] = []
    try:
        paths = sorted(Path(results_dir).glob("*.json"))
    except OSError:
        return samples
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # data/results/*.json 直接是 AnalysisResult，diagnostics 在顶层；
        # 也兼容极少数被 TaskRecord 包装的情况（diagnostics 在 result 内）。
        diag = data.get("diagnostics")
        if not diag:
            diag = (data.get("result") or {}).get("diagnostics")
        if not diag:
            continue
        md = diag.get("media_duration_seconds")
        ts = diag.get("transcribe_seconds")
        tot = diag.get("total_seconds")
        if not (isinstance(md, (int, float)) and isinstance(ts, (int, float)) and isinstance(tot, (int, float))):
            continue
        if md <= 0 or ts <= 0 or tot <= 0:
            continue
        # 快手/抖音等 Mock 会以 180 秒时长 + 0 秒转写混入，必须靠 ts>0 剔除。
        speed = md / ts
        non_transcribe = max(tot - ts, 0.0)
        samples.append((speed, non_transcribe, float(md)))
    return samples


def _scan_real_runs(results_dir: str | Path) -> list[tuple[float, float]]:
    """兼容旧调用方：只返回速度与非转写耗时。"""
    return [(speed, non_transcribe) for speed, non_transcribe, _ in _scan_real_run_details(results_dir)]


def load_calibration(results_dir: str | Path) -> Calibration:
    """读取（带 TTL 缓存）校准参数；真实样本不足时回退默认值。"""
    key = str(results_dir)
    now = time.monotonic()
    with _CALIBRATION_LOCK:
        cached = _CALIBRATION_CACHE.get(key)
        if cached and (now - cached[0]) < _CALIBRATION_TTL_S:
            return cached[1]

        samples = _scan_real_run_details(results_dir)
        if len(samples) >= _MIN_SAMPLES:
            speeds = sorted(s[0] for s in samples)
            non_ts = sorted(s[1] for s in samples)
            non_ts_ratio = sorted(s[1] / s[2] for s in samples if s[2] > 0)
            asr_speed_x = _median(speeds)
            non_transcribe_s = _median(non_ts)
            non_transcribe_ratio = _median(non_ts_ratio)
        else:
            asr_speed_x = _DEFAULT_ASR_SPEED_X
            non_transcribe_s = _DEFAULT_NON_TRANSCRIBE_S
            non_transcribe_ratio = 0.0

        calib = Calibration(
            asr_speed_x=asr_speed_x,
            non_transcribe_s=non_transcribe_s,
            download_est_s=_DOWNLOAD_EST_S,
            samples=len(samples),
            non_transcribe_ratio=non_transcribe_ratio,
        )
        _CALIBRATION_CACHE[key] = (now, calib)
        return calib


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


# ---------------------------------------------------------------------------
# 进程内进度追踪器：workflow 在任务各阶段埋点，HTTP 轮询时用 snapshot() 实时推算
# 进度百分比与预计剩余时间。进程重启即清空（此时前端回退装饰性动画）。
# ---------------------------------------------------------------------------

# 进度只在任务真正完成时到 100%；运行中最多显示到 99%，避免用户误以为
# 已经结束。阶段百分比不再写死，而是按预计耗时动态分配。
_RUNNING_MAX_PROGRESS = 99.0


@dataclass
class _Tracker:
    task_id: str
    mode: str
    is_mock: bool
    download_required: bool = True
    started_at: float = 0.0
    download_done_at: float | None = None
    media_duration: float | None = None
    transcribe_started_at: float | None = None
    transcribe_done: int = 0
    transcribe_total: int = 0
    transcribe_done_at: float | None = None
    actual_transcribe_s: float | None = None
    analyzing_at: float | None = None
    done_at: float | None = None
    last_progress: float = 0.0


_trackers: dict[str, _Tracker] = {}
_lock = threading.Lock()


class ProgressReporter:
    """供 workflow / ASR 在任务生命周期中上报阶段进度。线程安全。"""

    def __init__(
        self,
        task_id: str,
        mode: str,
        is_mock: bool,
        *,
        download_required: bool = True,
    ) -> None:
        self._task_id = task_id
        with _lock:
            _trackers[task_id] = _Tracker(
                task_id=task_id,
                mode=mode,
                is_mock=is_mock,
                download_required=download_required,
                started_at=time.monotonic(),
            )

    def mark_download_done(self, media_duration: float | None) -> None:
        with _lock:
            t = _trackers.get(self._task_id)
            if t is None:
                return
            t.download_done_at = time.monotonic()
            if media_duration is not None:
                t.media_duration = media_duration

    def mark_transcribe_started(
        self,
        media_duration: float | None,
        total: int,
    ) -> None:
        """ASR 真正开始前上报总分片数，避免进度在 12% 长时间不动。"""
        with _lock:
            t = _trackers.get(self._task_id)
            if t is None:
                return
            if t.download_done_at is None:
                # 上传文件没有下载阶段；兼容调用方漏报时也不要显示“下载中”。
                t.download_done_at = time.monotonic()
            if media_duration is not None and media_duration > 0:
                t.media_duration = media_duration
            if t.transcribe_started_at is None:
                t.transcribe_started_at = time.monotonic()
            t.transcribe_total = max(0, int(total))

    def on_transcribe_chunk(self, done: int, total: int) -> None:
        with _lock:
            t = _trackers.get(self._task_id)
            if t is None:
                return
            if t.transcribe_started_at is None:
                t.transcribe_started_at = time.monotonic()
            t.transcribe_done = max(0, int(done))
            t.transcribe_total = max(t.transcribe_total, int(total))

    def mark_transcribe_done(self, actual_transcribe_s: float, media_duration: float | None) -> None:
        with _lock:
            t = _trackers.get(self._task_id)
            if t is None:
                return
            t.transcribe_done_at = time.monotonic()
            if t.transcribe_started_at is None:
                t.transcribe_started_at = t.download_done_at or time.monotonic()
            t.actual_transcribe_s = max(0.0, actual_transcribe_s)
            t.transcribe_done = t.transcribe_total
            if media_duration is not None:
                t.media_duration = media_duration

    def mark_analyzing(self) -> None:
        with _lock:
            t = _trackers.get(self._task_id)
            if t is None:
                return
            t.analyzing_at = time.monotonic()

    def mark_done(self) -> None:
        with _lock:
            t = _trackers.get(self._task_id)
            if t is None:
                return
            t.done_at = time.monotonic()


def drop_tracker(task_id: str) -> None:
    """任务进入终态后清理（成功/失败都由 workflow 调用，避免 tracker 泄漏）。"""
    with _lock:
        _trackers.pop(task_id, None)


def get_snapshot(task_id: str, results_dir: str | Path) -> dict[str, Any] | None:
    """返回实时进度快照；无追踪信息（Mock / 重启后 / 已完成清理）时返回 None。

    返回字段：stage / progress / eta_seconds / media_duration /
    transcribe_done / transcribe_total / elapsed_seconds /
    estimated_total_seconds。前端据此渲染真实进度条与预计剩余。
    """
    with _lock:
        t = _trackers.get(task_id)
        if t is None or t.is_mock:
            return None
        # 复制快照，解锁后再计算，缩短持锁时间
        snap = {
            "started_at": t.started_at,
            "download_required": t.download_required,
            "download_done_at": t.download_done_at,
            "media_duration": t.media_duration,
            "transcribe_started_at": t.transcribe_started_at,
            "transcribe_done": t.transcribe_done,
            "transcribe_total": t.transcribe_total,
            "transcribe_done_at": t.transcribe_done_at,
            "analyzing_at": t.analyzing_at,
            "done_at": t.done_at,
            "actual_transcribe_s": t.actual_transcribe_s,
            "last_progress": t.last_progress,
        }
    calib = load_calibration(results_dir)
    now = time.monotonic()
    media = snap["media_duration"]
    if media and media > 0 and calib.non_transcribe_ratio > 0:
        # 下载与 LLM 开销会随媒体大小变化；用“每秒视频的非 ASR 开销”
        # 比直接套一个固定 77~80 秒更适合短视频，也不会让长视频低估。
        non_transcribe_est = max(20.0, media * calib.non_transcribe_ratio)
    else:
        non_transcribe_est = max(20.0, calib.non_transcribe_s)
    if snap["download_required"]:
        # 历史样本没有单独的下载耗时，因此从非转写开销中取一个保守比例。
        # 短视频不再硬加 25 秒，长视频仍保留上限 25 秒。
        download_est = min(calib.download_est_s, max(8.0, non_transcribe_est * 0.45))
    else:
        download_est = 0.0
    analysis_est = max(non_transcribe_est - download_est, 8.0)
    transcribe_est = _estimate_transcribe_seconds(snap, calib, now)
    estimated_total = max(download_est + transcribe_est + analysis_est, 1.0)

    def clamp(x: float) -> float:
        return max(0.0, min(1.0, x))

    # 已完成
    if snap["done_at"] is not None:
        return _pack(task_id, "done", 100.0, 0.0, snap, now, estimated_total)

    # 分析中：前两阶段使用真实耗时，LLM 阶段按历史样本估算推进。
    if snap["analyzing_at"] is not None:
        elapsed_total = max(0.0, now - snap["started_at"])
        elapsed_analysis = max(0.0, now - snap["analyzing_at"])
        progress = 100.0 * min(
            _RUNNING_MAX_PROGRESS / 100.0,
            (elapsed_total / estimated_total),
        )
        # 若前面阶段实际比历史样本慢，至少保持在“分析中”的合理位置。
        completed_before_analysis = max(0.0, snap["analyzing_at"] - snap["started_at"])
        progress = max(
            progress,
            100.0 * min(
                _RUNNING_MAX_PROGRESS / 100.0,
                (completed_before_analysis + min(elapsed_analysis, analysis_est)) / estimated_total,
            ),
        )
        eta = max(0.0, estimated_total - elapsed_total)
        return _pack(task_id, "analyzing", progress, eta, snap, now, estimated_total)

    # 转写中：分片完成数是直接证据，当前耗时用于校正“这台机器实际有多快”。
    if snap["transcribe_started_at"] is not None and snap["transcribe_total"] > 0:
        done = snap["transcribe_done"]
        total = snap["transcribe_total"]
        chunk_frac = clamp(done / total) if total else 0.0
        elapsed_t = now - snap["transcribe_started_at"]
        time_frac = clamp(elapsed_t / transcribe_est) if transcribe_est else 0.0
        frac = max(chunk_frac, time_frac)
        if done < total:
            frac = min(frac, 0.995)
        before = max(0.0, snap["transcribe_started_at"] - snap["started_at"])
        progress = 100.0 * (before + transcribe_est * frac) / estimated_total
        remaining_t = max(0.0, transcribe_est * (1.0 - frac))
        eta = remaining_t + analysis_est
        return _pack(task_id, "transcribing", progress, eta, snap, now, estimated_total)

    # 媒体已准备好但 ASR 尚未回调的瞬间，给用户一个明确的转写中状态。
    if snap["download_done_at"] is not None:
        elapsed = max(0.0, now - snap["started_at"])
        progress = 100.0 * min(elapsed, download_est) / estimated_total
        eta = max(0.0, estimated_total - elapsed)
        return _pack(task_id, "transcribing", progress, eta, snap, now, estimated_total)

    # 下载中：按历史下载时间平滑推进，但保留 99% 上限，不伪造完成。
    elapsed = now - snap["started_at"]
    progress = 100.0 * min(elapsed, download_est) / estimated_total
    eta = max(0.0, estimated_total - elapsed)
    return _pack(task_id, "downloading", progress, eta, snap, now, estimated_total)


def _estimate_transcribe_seconds(
    snap: dict[str, Any],
    calib: Calibration,
    now: float,
) -> float:
    """用历史速度做先验，再用当前任务已完成分片的耗时做在线校正。"""
    if snap["actual_transcribe_s"] is not None and snap["actual_transcribe_s"] > 0:
        return max(1.0, float(snap["actual_transcribe_s"]))

    media = snap["media_duration"]
    baseline = media / max(calib.asr_speed_x, 0.1) if media and media > 0 else calib.download_est_s * 4
    started = snap["transcribe_started_at"]
    done = snap["transcribe_done"]
    total = snap["transcribe_total"]
    if started is None or done <= 0 or total <= 0:
        return max(1.0, baseline)

    elapsed = max(0.0, now - started)
    frac = min(1.0, done / total)
    observed_total = elapsed / frac if frac > 0 else baseline
    # 早期样本噪声大，随着分片完成逐步提高观测值权重。
    weight = min(0.75, max(0.08, frac))
    return max(1.0, baseline * (1.0 - weight) + observed_total * weight)


def _pack(
    task_id: str,
    stage: str,
    progress: float,
    eta_seconds: float,
    snap: dict[str, Any],
    now: float,
    estimated_total: float,
) -> dict[str, Any]:
    # 校准值变动或阶段切换不能让进度条倒退；只有 mark_done 才能到 100%。
    progress = max(snap["last_progress"], min(_RUNNING_MAX_PROGRESS, progress))
    if stage == "done":
        progress = 100.0
    with _lock:
        tracker = _trackers.get(task_id)
        if tracker is not None:
            tracker.last_progress = max(tracker.last_progress, progress)
            progress = tracker.last_progress
    return {
        "stage": stage,
        "progress": round(progress, 1),
        "eta_seconds": round(eta_seconds, 1),
        "media_duration": snap["media_duration"],
        "transcribe_done": snap["transcribe_done"],
        "transcribe_total": snap["transcribe_total"],
        "transcribe_seconds": snap["actual_transcribe_s"],
        "elapsed_seconds": round(max(0.0, now - snap["started_at"]), 1),
        "estimated_total_seconds": round(estimated_total, 1),
    }
