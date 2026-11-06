from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from app.adapters.asr import MockTranscriber, ParaformerTranscriber, format_transcript
from app.adapters.comment_fetcher import fetch_comments_for_url
from app.comments_api import start_for_analysis
from app.adapters.downloader import select_downloader
from app.adapters.llm import MockLlmAnalyzer, OpenAICompatibleAnalyzer
from app.config import Settings, settings
from app.errors import VideoInsightError
from app.prompts import render_comments_block
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, CommentItem, MediaInfo
from app.progress import ProgressReporter, drop_tracker, invalidate_calibration
from app.task_store import SQLiteTaskStore

LOGGER = logging.getLogger(__name__)

# 提示词是否允许按时间戳组织大纲；workflow 当前传纯文本，保持 False（改 True 需同时传带时间戳的转写）
_INCLUDE_TIMESTAMPS_IN_PROMPT = False


def detect_platform(url: str) -> str:
    value = url.strip().lower()
    if value.startswith("upload://"):
        return "上传文件"
    if any(host in value for host in ("kuaishou.com", "chenzhongtech.com", "gifshow.com")):
        return "快手"
    if "douyin.com" in value:
        return "抖音"
    if "bilibili.com" in value or "b23.tv" in value:
        return "B站"
    if "xiaohongshu.com" in value or "xhslink" in value:
        return "小红书"
    return "未知平台"


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "未知"
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _build_metadata(
    *,
    title: str | None,
    author: str | None,
    duration: float | None,
    url: str,
) -> dict[str, str]:
    return {
        "video_title": title or "未知标题",
        "author": author or "未知作者",
        "platform": detect_platform(url),
        "duration": format_duration(duration),
    }


def _discard_transcode_intermediate(audio_path: Path | None, task_id: str) -> None:
    """转写完成后删掉 16kHz 中间 WAV。

    它是 ASR 的内部输入（`ParaformerTranscriber` 生成），转写完就没有任何代码
    再读它 —— `media.audio_path` 指向的是下载/上传的原始文件。一小时音频的
    16kHz 单声道 WAV 约 115 MB，按任务长期留着纯属浪费。删除失败只记日志。
    """
    if audio_path is None:
        return
    for leftover in audio_path.parent.glob(f"{task_id}-*.wav"):
        # 与 downloader/asr 的 _best_effort_unlink 同一约定：清理由 BaseException
        # 兜底，某些解释器会把 unlink 拦成 SystemExit（不属于 Exception）。
        try:
            leftover.unlink(missing_ok=True)
        except BaseException as exc:  # noqa: BLE001 - 清理路径，吞掉一切错误
            LOGGER.warning("转码中间文件删除失败，已忽略：%s（%s）", leftover, exc)


def _generate_output(
    llm: MockLlmAnalyzer | OpenAICompatibleAnalyzer | None,
    mode: AnalysisMode,
    transcript_plain: str,
    metadata: dict[str, str],
    comments_text: str | None = None,
    on_output: Callable[[str], None] | None = None,
) -> str:
    if mode == AnalysisMode.transcript:
        # 文字稿模式直接返回原文，跳过 LLM 调用（省时省钱）
        return transcript_plain
    if llm is None:
        raise RuntimeError("AI 分析模式缺少 LLM 分析器。")
    stream = getattr(llm, "analyze_stream", None)
    if on_output is not None and callable(stream):
        parts: list[str] = []
        last_reported = perf_counter()
        try:
            for text in stream(
                transcript_plain, mode, metadata=metadata,
                include_timestamps=_INCLUDE_TIMESTAMPS_IN_PROMPT, comments_text=comments_text,
            ):
                parts.append(text)
                if len(parts) == 1 or perf_counter() - last_reported >= 0.4:
                    on_output("".join(parts))
                    last_reported = perf_counter()
        finally:
            on_output("".join(parts))
        return "".join(parts)
    return llm.analyze(
        transcript_plain,
        mode,
        metadata=metadata,
        include_timestamps=_INCLUDE_TIMESTAMPS_IN_PROMPT,
        comments_text=comments_text,
    )


def _select_llm(
    request: AnalyzeRequest,
    use_mock: bool,
    runtime_settings: Settings,
) -> MockLlmAnalyzer | OpenAICompatibleAnalyzer | None:
    if request.mode == AnalysisMode.transcript:
        return None
    if use_mock:
        return MockLlmAnalyzer()
    if request.llm_api_key:
        return OpenAICompatibleAnalyzer(
            runtime_settings,
            provider=request.llm_provider,
            base_url=request.llm_base_url,
            model=request.llm_model,
            api_key=request.llm_api_key,
            protocol=request.llm_protocol,
        )
    return OpenAICompatibleAnalyzer(runtime_settings)


def _strip_markdown(line: str) -> str:
    """去掉行首的 Markdown 标记（标题井号、列表符号、引用）后返回。"""
    return line.strip().lstrip("#>-* ").strip()


def generate_result_title(output: str, mode: AnalysisMode, fallback: str | None = None) -> str:
    """从 AI 输出中提取一句话核心作为任务标题（对话摘要式命名）。

    优先级：
    1. 从输出中定位「一句话核心 / 核心观点 / 面试岗位」等摘要板块，取其后第一句；
    2. 兜底取输出中第一个非空标题行；
    3. 仍为空则用 fallback（媒体标题/文件名），再兜底用模式描述。
    """
    MAX_TITLE = 30
    mode_name = {
        AnalysisMode.summary: "AI 总结",
        AnalysisMode.transcript: "逐字稿",
        AnalysisMode.interview: "面经提取",
    }.get(mode, "任务")

    def clean(text: str) -> str:
        text = _strip_markdown(text).strip()
        if not text or text.startswith(("（", "(", "⚠️", "[")):
            return ""
        # 去掉常见占位提示行
        for head in ("不超过", "从文本", "提取", "按出现", "对应每个", "给出", "无"):
            if text.startswith(head):
                return ""
        text = " ".join(text.split())
        return text[:MAX_TITLE] + ("…" if len(text) > MAX_TITLE else "")

    # 1) 定位摘要板块
    markers = ("一句话核心", "核心观点", "面试岗位", "### 核心", "## 核心")
    for marker in markers:
        idx = output.find(marker)
        if idx == -1:
            continue
        tail = output[idx + len(marker):]
        for raw in tail.splitlines():
            candidate = clean(raw)
            if candidate:
                return candidate
        break

    # 2) 第一个非空标题行
    for raw in output.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") and not stripped.startswith("# 转写"):
            candidate = clean(stripped)
            if candidate:
                return candidate

    # 3) 第一句非空文本
    for raw in output.splitlines():
        candidate = clean(raw)
        if candidate:
            return candidate

    # 4) 兜底
    fallback_name = clean(fallback or "") if fallback else ""
    return fallback_name or mode_name


def run_analysis_task(task_id: str, request: AnalyzeRequest, store: SQLiteTaskStore) -> None:
    store.set_running(task_id)
    try:
        result = analyze(
            request, task_id, settings,
            on_update=lambda result: store.set_partial_result(task_id, result),
        )
    except VideoInsightError as exc:
        store.set_failed(task_id, str(exc))
    except Exception as exc:  # Keep background task failures visible to API clients.
        store.set_failed(task_id, f"未预期错误：{exc}")
    except BaseException as exc:  # noqa: BLE001
        # SystemExit 不属于 Exception（如某些解释器把文件操作拦截成 SystemExit），
        # 若不加这层会从 worker 逃逸，用户只会看到晦涩的 "任务执行异常：1"。
        # worker 线程吞下它是对的：记录失败后线程自然结束，不能让它打断整个池。
        LOGGER.error(
            "任务 %s 被非 Exception 异常击穿（不应到达这里）：%s", task_id, exc, exc_info=exc
        )
        store.set_failed(
            task_id,
            f"任务中断（{type(exc).__name__}）：{exc} · "
            + " | ".join(traceback.format_exception_only(type(exc), exc)).strip()[:300],
        )
    else:
        store.set_succeeded(task_id, result)
    finally:
        drop_tracker(task_id)


def run_file_analysis_task(
    task_id: str,
    request: AnalyzeRequest,
    media_path: Path,
    store: SQLiteTaskStore,
) -> None:
    store.set_running(task_id)
    try:
        result = analyze_media_file(
            request, task_id, media_path, settings,
            on_update=lambda result: store.set_partial_result(task_id, result),
        )
    except VideoInsightError as exc:
        store.set_failed(task_id, str(exc))
    except Exception as exc:
        store.set_failed(task_id, f"未预期错误：{exc}")
    except BaseException as exc:  # noqa: BLE001
        LOGGER.error(
            "任务 %s 被非 Exception 异常击穿（不应到达这里）：%s", task_id, exc, exc_info=exc
        )
        store.set_failed(
            task_id,
            f"任务中断（{type(exc).__name__}）：{exc} · "
            + " | ".join(traceback.format_exception_only(type(exc), exc)).strip()[:300],
        )
    else:
        store.set_succeeded(task_id, result)
    finally:
        drop_tracker(task_id)


def _generate_live_output(
    result: AnalysisResult,
    llm: MockLlmAnalyzer | OpenAICompatibleAnalyzer | None,
    transcript_plain: str,
    metadata: dict[str, str],
    on_update: Callable[[AnalysisResult], None] | None,
    comments_text: str | None = None,
) -> None:
    last_saved = perf_counter()
    if on_update:
        on_update(result)

    def publish(output: str) -> None:
        nonlocal last_saved
        result.output = output
        # Persist checkpoints, not individual tokens; reconnects can resume from SQLite.
        if on_update and perf_counter() - last_saved >= 0.4:
            on_update(result)
            last_saved = perf_counter()

    try:
        result.output = _generate_output(
            llm, result.mode, transcript_plain, metadata,
            comments_text=comments_text, on_output=publish if on_update else None,
        )
    finally:
        if on_update:
            on_update(result)


def analyze(
    request: AnalyzeRequest,
    task_id: str,
    runtime_settings: Settings = settings,
    *,
    on_update: Callable[[AnalysisResult], None] | None = None,
) -> AnalysisResult:
    runtime_settings.ensure_dirs()
    use_mock = runtime_settings.mock_mode if request.use_mock is None else request.use_mock
    t0 = perf_counter()
    # 真实模式才建追踪器：进度条用真实分片进度；Mock 模式不追踪（前端回退装饰性动画）。
    reporter = ProgressReporter(task_id, request.mode.value, bool(use_mock)) if not use_mock else None

    downloader = select_downloader(str(request.url), runtime_settings.tmp_dir, use_mock)
    transcriber = MockTranscriber() if use_mock else ParaformerTranscriber(runtime_settings)
    llm = _select_llm(request, use_mock, runtime_settings)

    # 观众评论抓取（可选、可降级）：不阻断主链路。
    # 刻意排在下载/转写**之前**：评论区只是一次轻量 HTTP 请求（约 1 秒），
    # 先拿到就不会被后面动辄十几分钟的下载+ASR 拖着；即使视频侧后续失败，
    # 也已尽力拿到评论数据。
    # 逐字稿模式同样抓取——虽然它不调 LLM（_generate_output 直接返回原文），
    # 但前端「观众评论」面板要展示，不能让用户对着空白发懵。
    comments: list[CommentItem] = []
    comments_source: str | None = None
    comment_job_id: str | None = None
    if request.with_comments and runtime_settings.comment_crawler_url and not use_mock:
        try:
            comment_job = start_for_analysis(str(request.url), request.client_id, runtime_settings)
            comment_job_id = comment_job["id"]
            comments_source = "完整评论正在后台逐页抓取，可在评论中心查看进度和导出（包含子评论）。"
        except Exception:
            comments_source = "评论后台任务暂未启动，请在评论中心重试；视频分析继续。"
    elif request.with_comments:
        comments, comments_source = fetch_comments_for_url(str(request.url), runtime_settings)
    comments_text = render_comments_block(comments) if comments else None

    media = downloader.download_audio(str(request.url), task_id)
    if reporter is not None:
        reporter.mark_download_done(media.duration)
    asr_start = perf_counter()
    segments = transcriber.transcribe(media.audio_path, task_id, reporter=reporter)
    transcribe_seconds = perf_counter() - asr_start
    # 视频总时长优先用转写器实测值（ffprobe）：比平台下发的 media.duration 更可靠，
    # 快手等平台 media.duration 常为 None。
    media_duration = transcriber.last_duration if transcriber.last_duration is not None else media.duration
    if reporter is not None:
        reporter.mark_transcribe_done(transcribe_seconds, media_duration)
    _discard_transcode_intermediate(media.audio_path, task_id)
    transcript = format_transcript(segments, include_timestamps=True)
    transcript_plain = format_transcript(segments, include_timestamps=False)
    metadata = _build_metadata(
        title=media.title,
        author=media.author,
        duration=media.duration,
        url=str(request.url),
    )

    if reporter is not None:
        reporter.mark_analyzing()
    media_info = MediaInfo(
        audio_path=str(media.audio_path) if media.audio_path else None,
        title=media.title,
        author=media.author,
        duration=media.duration,
        source_url=media.source_url,
    )
    result = AnalysisResult(
        mode=request.mode,
        transcript=transcript,
        segments=segments,
        output="",
        media=media_info,
        comments=comments or None,
        diagnostics={
            "mock_mode": use_mock,
            "comments_source": comments_source,
            "comment_job_id": comment_job_id,
            "comment_count": len(comments),
            "media_duration_seconds": round(media_duration, 2) if media_duration is not None else None,
            "transcribe_seconds": round(transcribe_seconds, 2),
            "total_seconds": round(perf_counter() - t0, 2),
            "asr_speed_x": (
                round(media_duration / transcribe_seconds, 2)
                if media_duration and transcribe_seconds
                else None
            ),
        },
    )
    _generate_live_output(result, llm, transcript_plain, metadata, on_update, comments_text)
    result.diagnostics["total_seconds"] = round(perf_counter() - t0, 2)
    result.title = generate_result_title(result.output, request.mode, fallback=media_info.title)
    result_path = _write_result(runtime_settings.results_dir, task_id, result)
    result.artifacts["result_json"] = str(result_path)
    _write_result(runtime_settings.results_dir, task_id, result)
    invalidate_calibration(runtime_settings.results_dir)

    if reporter is not None:
        reporter.mark_done()
    _log_timing(task_id, media_duration, transcribe_seconds, perf_counter() - t0)
    return result


def analyze_media_file(
    request: AnalyzeRequest,
    task_id: str,
    media_path: Path,
    runtime_settings: Settings = settings,
    *,
    on_update: Callable[[AnalysisResult], None] | None = None,
) -> AnalysisResult:
    runtime_settings.ensure_dirs()
    use_mock = runtime_settings.mock_mode if request.use_mock is None else request.use_mock
    t0 = perf_counter()
    # 真实模式才建追踪器：进度条用真实分片进度；Mock 模式不追踪（前端回退装饰性动画）。
    reporter = (
        ProgressReporter(
            task_id,
            request.mode.value,
            bool(use_mock),
            download_required=False,
        )
        if not use_mock
        else None
    )
    if reporter is not None:
        # 上传文件已经在请求阶段落盘，不应把固定的“下载耗时”算进 ETA。
        reporter.mark_download_done(None)

    transcriber = MockTranscriber() if use_mock else ParaformerTranscriber(runtime_settings)
    llm = _select_llm(request, use_mock, runtime_settings)

    asr_start = perf_counter()
    segments = transcriber.transcribe(media_path, task_id, reporter=reporter)
    transcribe_seconds = perf_counter() - asr_start
    media_duration = transcriber.last_duration
    if reporter is not None:
        reporter.mark_transcribe_done(transcribe_seconds, media_duration)
    _discard_transcode_intermediate(media_path, task_id)
    transcript = format_transcript(segments, include_timestamps=True)
    transcript_plain = format_transcript(segments, include_timestamps=False)
    metadata = _build_metadata(
        title=media_path.name,
        author=None,
        duration=None,
        url=str(request.url),
    )
    if reporter is not None:
        reporter.mark_analyzing()
    media_info = MediaInfo(
        audio_path=str(media_path),
        title=media_path.name,
        author=None,
        duration=None,
        source_url=str(request.url),
    )
    result = AnalysisResult(
        mode=request.mode,
        transcript=transcript,
        segments=segments,
        output="",
        media=media_info,
        diagnostics={
            "mock_mode": use_mock,
            "source": "upload",
            "media_duration_seconds": round(media_duration, 2) if media_duration is not None else None,
            "transcribe_seconds": round(transcribe_seconds, 2),
            "total_seconds": round(perf_counter() - t0, 2),
            "asr_speed_x": (
                round(media_duration / transcribe_seconds, 2)
                if media_duration and transcribe_seconds
                else None
            ),
        },
    )
    _generate_live_output(result, llm, transcript_plain, metadata, on_update)
    result.diagnostics["total_seconds"] = round(perf_counter() - t0, 2)
    result.title = generate_result_title(result.output, request.mode, fallback=media_info.title)
    result_path = _write_result(runtime_settings.results_dir, task_id, result)
    result.artifacts["result_json"] = str(result_path)
    _write_result(runtime_settings.results_dir, task_id, result)
    invalidate_calibration(runtime_settings.results_dir)

    if reporter is not None:
        reporter.mark_done()
    _log_timing(task_id, media_duration, transcribe_seconds, perf_counter() - t0)
    return result


def _write_result(results_dir: Path, task_id: str, result: AnalysisResult) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{task_id}.json"
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result_path


def _log_timing(
    task_id: str,
    media_duration: float | None,
    transcribe_seconds: float,
    total_seconds: float,
) -> None:
    """把「视频总时长 / 转写用时 / 总用时」打到服务日志，方便量化提速效果。"""
    speed = f"，约 {media_duration / transcribe_seconds:.1f}× 实时" if media_duration and transcribe_seconds else ""
    LOGGER.info(
        "任务 %s 完成：视频时长 %.1fs，转写用时 %.1fs，总用时 %.1fs%s",
        task_id,
        media_duration or 0.0,
        transcribe_seconds,
        total_seconds,
        speed,
    )
