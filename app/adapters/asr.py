from __future__ import annotations

import logging
import struct
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from pathlib import Path
from time import sleep

from app.adapters.audio import (
    FfmpegAudioPreprocessor,
    audio_duration,
    resolve_ffmpeg,
)
from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError
from app.schemas import TranscriptSegment


LOGGER = logging.getLogger(__name__)


class MockTranscriber:
    def __init__(self) -> None:
        # 与 MockDownloader 返回的时长保持一致，便于量化演示。
        self.last_duration: float | None = 180.0

    def transcribe(
        self,
        audio_path: Path | None,
        task_id: str,
        reporter: object | None = None,
    ) -> list[TranscriptSegment]:
        return [
            TranscriptSegment(start=0, end=18, text="大家好，今天分享一次 AI 产品经理岗位的面试经验。"),
            TranscriptSegment(start=18, end=56, text="面试官先问了项目背景，我介绍了视频分析助手从链接解析、语音转写到结构化总结的流程。"),
            TranscriptSegment(start=56, end=104, text="随后被问到如何控制模型幻觉，我回答会把事实核查拆成声明抽取、联网搜索、证据比对和风险标注四步。"),
            TranscriptSegment(start=104, end=146, text="最后讨论了合规问题，包括用户授权、原始音视频自动删除和敏感内容审核。"),
            TranscriptSegment(start=146, end=180, text="我的复盘建议是先做最小闭环，用真实样本验证下载成功率、转写质量和总结可用性。"),
        ]


class ParaformerTranscriber:
    def __init__(self, settings: Settings, preprocessor: FfmpegAudioPreprocessor | None = None) -> None:
        self.settings = settings
        self.preprocessor = preprocessor or FfmpegAudioPreprocessor()
        # 最近一次转写实测到的音频时长（秒）；供 workflow 记录「视频总时长」。
        self.last_duration: float | None = None
        # 当前转写任务 id（供分片进度回调回传，避免层层透传参数）。
        self._current_task_id: str | None = None

    def transcribe(
        self,
        audio_path: Path | None,
        task_id: str,
        reporter: object | None = None,
    ) -> list[TranscriptSegment]:
        if audio_path is None:
            raise WorkflowError("ASR 需要音频文件，但下载模块未返回音频路径。")
        if not self.settings.dashscope_api_key:
            raise ExternalDependencyError("未配置 DASHSCOPE_API_KEY，无法调用 Paraformer ASR。")

        try:
            import dashscope
            from dashscope.audio.asr import Recognition
        except ImportError as exc:
            raise ExternalDependencyError("未安装 dashscope SDK，请安装：pip install -r requirements-asr.txt") from exc

        dashscope.api_key = self.settings.dashscope_api_key
        if self.settings.dashscope_workspace_id:
            dashscope.base_websocket_api_url = (
                f"wss://{self.settings.dashscope_workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
            )

        chunk_seconds = max(1, self.settings.dashscope_asr_chunk_seconds)
        threshold = max(1, self.settings.dashscope_asr_chunk_threshold_seconds)
        max_workers = max(1, self.settings.dashscope_asr_max_workers)

        # 优先用 ffprobe 直接拿时长，决定要不要走长音频分片。没有 ffprobe 时
        # 拿不到时长，统一回退到「整文件转码后测量」的旧路径，行为与原实现一致。
        duration = audio_duration(audio_path)
        self.last_duration = duration

        if duration is None:
            return self._transcribe_legacy(audio_path, task_id, Recognition, reporter)

        if duration >= threshold and chunk_seconds > 0 and max_workers > 1:
            if reporter is not None:
                reporter.mark_transcribe_started(
                    duration,
                    max(1, (int(duration) + chunk_seconds - 1) // chunk_seconds),
                )
            # 快路径：ffmpeg 边解码边把分片送去识别，使「解码」与「上传+识别」
            # 并发进行（之前是整段切完再统一识别，解码时间是纯串行浪费）。
            try:
                return self._transcribe_concurrent(
                    audio_path, task_id, Recognition, chunk_seconds, reporter
                )
            except WorkflowError as exc:
                LOGGER.warning("ffmpeg 并发分片识别失败，回退整文件转码方案：%s", exc)
                return self._transcribe_legacy(audio_path, task_id, Recognition, reporter)

        # 短音频：整文件转码后单次识别（无分片，进度直接标记 1/1）。
        if reporter is not None:
            reporter.mark_transcribe_started(duration, 1)
        wav_path = audio_path.parent / f"{task_id}-16k.wav"
        self.preprocessor.convert_to_wav16k(audio_path, wav_path)
        try:
            segments = self._recognize_file(wav_path, Recognition)
        finally:
            _best_effort_unlink(wav_path)
        if reporter is not None:
            reporter.on_transcribe_chunk(1, 1)
        return segments

    def _recognize_file(self, wav_path: Path, recognition_class: object) -> list[TranscriptSegment]:
        recognition = recognition_class(
            model=self.settings.dashscope_asr_model,
            format="wav",
            sample_rate=16000,
            language_hints=["zh", "en"],
            callback=None,
        )
        last_error: Exception | None = None
        # 并发调高后可能撞 DashScope 限流（429/503），退避后重试，避免整段转写白做。
        for attempt in range(3):
            result = recognition.call(str(wav_path))
            if result.status_code == HTTPStatus.OK:
                return _result_to_segments(result)
            last_error = WorkflowError(f"ASR 识别失败：{result.message}")
            if attempt < 2:
                sleep(1.0 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _recognize_chunk(
        self,
        index: int,
        path: Path,
        chunk_seconds: int,
        recognition_class: object,
    ) -> tuple[int, list[TranscriptSegment]]:
        local_segments = self._recognize_file(path, recognition_class)
        offset = index * chunk_seconds
        return index, [
            segment.model_copy(
                update={"start": segment.start + offset, "end": segment.end + offset}
            )
            for segment in local_segments
        ]

    def _transcribe_concurrent(
        self,
        audio_path: Path,
        task_id: str,
        recognition_class: object,
        chunk_seconds: int,
        reporter: object | None = None,
    ) -> list[TranscriptSegment]:
        """ffmpeg 把源音频解码成 raw PCM 流，主线程边读边按 chunk_seconds 写分片
        WAV 并立即提交识别线程——解码（子进程）与上传+识别（线程池）真正重叠，
        消除「先整段切完再统一识别」的串行等待。分片仍落盘是为了兼容 DashScope
        文件接口（非流式）。
        """
        ffmpeg = resolve_ffmpeg()
        cmd = [ffmpeg, "-i", str(audio_path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise WorkflowError(f"ffmpeg 启动失败：{exc}")

        stderr_chunks: list[bytes] = []
        stop = threading.Event()

        def _drain_stderr() -> None:
            try:
                while not stop.is_set():
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)
            except Exception:  # noqa: BLE001 - 避免管道读取异常阻断主流程
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        workers = max(1, self.settings.dashscope_asr_max_workers)
        executor = ThreadPoolExecutor(max_workers=workers)
        chunk_paths: list[Path] = []
        futures = []
        index = 0
        try:
            chunk_bytes = chunk_seconds * 16000 * 2
            while True:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    break
                chunk_path = audio_path.parent / f"{task_id}-asr-{index:04d}.wav"
                _write_wav16k(chunk_path, data)
                chunk_paths.append(chunk_path)
                futures.append(
                    executor.submit(self._recognize_chunk, index, chunk_path, chunk_seconds, recognition_class)
                )
                index += 1
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if proc.poll() is None:
                    proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            stop.set()
            stderr_thread.join(timeout=2)

        if not chunk_paths and proc.returncode not in (0, None):
            detail = b"".join(stderr_chunks).decode("utf-8", "replace").strip() or "unknown ffmpeg error"
            raise WorkflowError(f"ffmpeg 解码失败：{detail}")

        results: list[tuple[int, list[TranscriptSegment]]] = []
        total = (
            (int(self.last_duration) + chunk_seconds - 1) // chunk_seconds
            if self.last_duration
            else max(1, len(futures))
        )
        try:
            done = 0
            for future in as_completed(futures):
                results.append(future.result())
                done += 1
                if reporter is not None:
                    reporter.on_transcribe_chunk(done, total)
        finally:
            executor.shutdown(wait=False)
            for chunk_path in chunk_paths:
                _best_effort_unlink(chunk_path)

        segments = [segment for _, chunk in sorted(results) for segment in chunk]
        segments.sort(key=lambda segment: (segment.start, segment.end))
        return segments

    def _transcribe_chunks(
        self,
        chunk_paths: list[Path],
        chunk_seconds: int,
        recognition_class: object,
        reporter: object | None = None,
    ) -> list[TranscriptSegment]:
        workers = min(max(1, self.settings.dashscope_asr_max_workers), len(chunk_paths))

        def recognize_chunk(item: tuple[int, Path]) -> tuple[int, list[TranscriptSegment]]:
            index, path = item
            return self._recognize_chunk(index, path, chunk_seconds, recognition_class)

        results: list[tuple[int, list[TranscriptSegment]]] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(recognize_chunk, item) for item in enumerate(chunk_paths)]
            done = 0
            total = max(1, len(futures))
            for future in as_completed(futures):
                results.append(future.result())
                done += 1
                if reporter is not None:
                    reporter.on_transcribe_chunk(done, total)

        segments = [segment for _, chunk in sorted(results) for segment in chunk]
        segments.sort(key=lambda segment: (segment.start, segment.end))
        return segments

    def _transcribe_legacy(
        self,
        audio_path: Path,
        task_id: str,
        recognition_class: object,
        reporter: object | None = None,
    ) -> list[TranscriptSegment]:
        """旧路径：整文件转 16k WAV → 测时长 → 长则切分并行识别。

        作为「无 ffprobe」或「ffmpeg 分片失败」的兜底，行为与原实现完全一致。
        """
        self._current_task_id = task_id
        wav_path = audio_path.parent / f"{task_id}-16k.wav"
        self.preprocessor.convert_to_wav16k(audio_path, wav_path)
        try:
            duration = _wav_duration(wav_path) or 0.0
            self.last_duration = duration
            chunk_seconds = max(1, self.settings.dashscope_asr_chunk_seconds)
            threshold = max(1, self.settings.dashscope_asr_chunk_threshold_seconds)
            max_workers = max(1, self.settings.dashscope_asr_max_workers)
            if duration >= threshold and chunk_seconds > 0 and max_workers > 1:
                chunk_paths = _split_wav(wav_path, task_id, chunk_seconds, duration)
                if reporter is not None:
                    reporter.mark_transcribe_started(
                        duration,
                        max(1, len(chunk_paths)),
                    )
                try:
                    return self._transcribe_chunks(chunk_paths, chunk_seconds, recognition_class, reporter)
                finally:
                    for chunk_path in chunk_paths:
                        _best_effort_unlink(chunk_path)
            if reporter is not None:
                reporter.mark_transcribe_started(duration, 1)
            segments = self._recognize_file(wav_path, recognition_class)
            if reporter is not None:
                reporter.on_transcribe_chunk(1, 1)
            return segments
        finally:
            _best_effort_unlink(wav_path)


def _best_effort_unlink(path: Path) -> None:
    """删除临时文件，失败只记日志不抛出。

    用 BaseException 是刻意的：某些运行环境（沙箱/内嵌解释器）会把 unlink
    拦截成 SystemExit，那不属于 Exception，若用 except Exception 仍会击穿
    finally 并让整个任务前功尽弃。
    """
    try:
        path.unlink(missing_ok=True)
    except BaseException as exc:  # noqa: BLE001 - 清理路径，吞掉一切错误
        LOGGER.warning("临时文件删除失败，已忽略：%s（%s）", path, exc)


def _result_to_segments(result: object) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for sentence in result.get_sentence() or []:  # type: ignore[attr-defined]
        segments.append(
            TranscriptSegment(
                start=(sentence.get("begin_time") or 0) / 1000,
                end=(sentence.get("end_time") or 0) / 1000,
                text=sentence.get("text", ""),
            )
        )
    return segments


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wav_file:
            rate = wav_file.getframerate()
            return wav_file.getnframes() / rate if rate else None
    except (OSError, wave.Error):
        return None


def _split_wav(path: Path, task_id: str, chunk_seconds: int, duration: float) -> list[Path]:
    del duration  # The frame count is the authoritative boundary for WAV files.
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        frames_per_chunk = max(1, int(params.framerate * chunk_seconds))
        chunks: list[Path] = []
        index = 0
        while True:
            frames = source.readframes(frames_per_chunk)
            if not frames:
                break
            chunk_path = path.parent / f"{task_id}-asr-{index:04d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setparams(params)
                target.writeframes(frames)
            chunks.append(chunk_path)
            index += 1
    return chunks


def _write_wav16k(path: Path, pcm: bytes) -> None:
    """把一段 16kHz 单声道 16-bit PCM 字节写成标准 WAV 文件。

    流水线分片直接来自 ffmpeg 的 raw PCM 输出，需要自带 RIFF 头才能被
    DashScope 文件接口当 wav 读取。
    """
    size = len(pcm)
    with open(path, "wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 36 + size))
        handle.write(b"WAVE")
        handle.write(b"fmt ")
        # PCM=1, 单声道, 采样率 16000, 字节率 32000, 块对齐 2, 位深 16
        handle.write(struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16))
        handle.write(b"data")
        handle.write(struct.pack("<I", size))
        handle.write(pcm)


def format_transcript(segments: list[TranscriptSegment], include_timestamps: bool = True) -> str:
    if include_timestamps:
        return "\n".join(f"[{item.start:.2f}-{item.end:.2f}] {item.text}" for item in segments)
    return "".join(item.text for item in segments)
