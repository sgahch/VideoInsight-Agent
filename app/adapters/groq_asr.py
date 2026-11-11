from __future__ import annotations

import math
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic

import httpx

from app.adapters.asr import _best_effort_unlink, _recognition_gate, _split_wav, _wav_duration
from app.adapters.audio import FfmpegAudioPreprocessor
from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError
from app.schemas import TranscriptSegment


_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_MAX_UPLOAD_BYTES = 25_000_000
_REQUEST_TIMES: deque[float] = deque()
_RATE_LOCK = threading.Lock()


class GroqTranscriber:
    def __init__(self, settings: Settings, preprocessor: FfmpegAudioPreprocessor | None = None) -> None:
        self.settings = settings
        self.preprocessor = preprocessor or FfmpegAudioPreprocessor()
        self.last_duration: float | None = None
        self._stopped = threading.Event()
        self._error_lock = threading.Lock()
        self._error: WorkflowError | None = None

    def transcribe(
        self, audio_path: Path | None, task_id: str, reporter: object | None = None,
    ) -> list[TranscriptSegment]:
        if audio_path is None:
            raise WorkflowError("ASR 需要音频文件，但下载模块未返回音频路径。")
        if not self.settings.groq_api_key:
            raise ExternalDependencyError("未配置 GROQ_API_KEY，无法调用 Groq 语音转写。")
        if self.settings.groq_asr_model not in ("whisper-large-v3-turbo", "whisper-large-v3"):
            raise ExternalDependencyError("GROQ_ASR_MODEL 必须为 whisper-large-v3-turbo 或 whisper-large-v3。")
        self._stopped.clear()
        self._error = None
        self.last_duration = None
        # 16kHz mono PCM is 32 KB/s; ten-minute chunks remain below the free-tier upload limit.
        chunk_seconds = min(600, max(1, self.settings.groq_asr_chunk_seconds))
        wav_path = audio_path.parent / f"{task_id}-groq-16k.wav"
        chunk_paths: list[Path] = []
        try:
            self.preprocessor.convert_to_wav16k(audio_path, wav_path)
            duration = _wav_duration(wav_path)
            if not duration:
                raise WorkflowError("Groq ASR 无法读取音频时长，或音频文件为空。")
            self.last_duration = duration
            chunk_paths = (
                _split_wav(wav_path, task_id, chunk_seconds, duration)
                if duration > chunk_seconds else [wav_path]
            )
            if reporter is not None:
                reporter.mark_transcribe_started(duration, len(chunk_paths))
            with httpx.Client(
                headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
                timeout=httpx.Timeout(120, connect=15),
                follow_redirects=False,
            ) as client:
                workers = min(max(1, self.settings.groq_asr_max_workers), len(chunk_paths))
                executor = ThreadPoolExecutor(max_workers=workers)
                futures = {}
                results: dict[int, list[TranscriptSegment]] = {}
                try:
                    for index, path in enumerate(chunk_paths):
                        futures[executor.submit(self._recognize_file, path, client)] = index
                    for future in as_completed(futures):
                        index = futures[future]
                        offset = index * chunk_seconds
                        results[index] = [
                            segment.model_copy(update={
                                "start": segment.start + offset,
                                "end": segment.end + offset,
                            })
                            for segment in future.result()
                        ]
                        if reporter is not None:
                            reporter.on_transcribe_chunk(len(results), len(chunk_paths))
                except WorkflowError as exc:
                    raise self._stop(exc)
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
            segments = [segment for index in sorted(results) for segment in results[index]]
            if not segments:
                raise WorkflowError("Groq ASR 未识别到有效语音，请检查音频内容。")
            return segments
        finally:
            for path in chunk_paths:
                if path != wav_path:
                    _best_effort_unlink(path)
            _best_effort_unlink(wav_path)

    def _stop(self, error: WorkflowError) -> WorkflowError:
        with self._error_lock:
            if self._error is None:
                self._error = error
            self._stopped.set()
            return self._error

    def _check_stopped(self) -> None:
        if self._stopped.is_set():
            raise self._error or WorkflowError("Groq ASR 分片处理已停止。")

    def _wait(self, seconds: float) -> None:
        self._stopped.wait(seconds)
        self._check_stopped()

    def _wait_for_request_slot(self) -> None:
        limit = max(1, self.settings.groq_asr_requests_per_minute)
        while True:
            self._check_stopped()
            with _RATE_LOCK:
                now = monotonic()
                while _REQUEST_TIMES and _REQUEST_TIMES[0] <= now - 60:
                    _REQUEST_TIMES.popleft()
                if len(_REQUEST_TIMES) < limit:
                    _REQUEST_TIMES.append(now)
                    return
                delay = max(0.01, _REQUEST_TIMES[0] + 60 - now)
            self._wait(delay)

    def _recognize_file(self, path: Path, client: httpx.Client) -> list[TranscriptSegment]:
        if path.stat().st_size > _MAX_UPLOAD_BYTES:
            raise self._stop(WorkflowError("Groq ASR 音频分片超过 25MB，请减小 GROQ_ASR_CHUNK_SECONDS。"))
        for attempt in range(3):
            self._check_stopped()
            with _recognition_gate(max(1, self.settings.groq_asr_global_workers)):
                self._check_stopped()
                self._wait_for_request_slot()
                try:
                    with path.open("rb") as audio:
                        response = client.post(
                            _TRANSCRIPTION_URL,
                            data={
                                "model": self.settings.groq_asr_model,
                                "response_format": "verbose_json",
                                "timestamp_granularities[]": "segment",
                                "temperature": "0",
                            },
                            files={"file": (path.name, audio, "audio/wav")},
                        )
                except httpx.RequestError:
                    raise self._stop(WorkflowError(
                        "Groq ASR 网络连接失败或超时，请检查网络后重试；未自动重复上传或切换到收费服务。"
                    )) from None
            if response.is_success:
                try:
                    data = response.json()
                    return _parse_segments(data, _wav_duration(path) or 0)
                except (ValueError, TypeError, KeyError, OverflowError):
                    raise self._stop(WorkflowError(
                        "Groq ASR 返回的逐句时间戳格式异常，请稍后重试。"
                    )) from None
            error = self._response_error(response)
            delay = float(attempt + 1)
            if response.status_code == 429:
                try:
                    delay = max(delay, float(response.headers.get("retry-after", delay)))
                except ValueError:
                    pass
                if not math.isfinite(delay) or delay > 5:
                    raise self._stop(error)
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise self._stop(error)
            self._wait(delay)
        raise AssertionError("unreachable")

    def _response_error(self, response: httpx.Response) -> WorkflowError:
        status = response.status_code
        if status == 401:
            detail = "Groq API Key 无效或已撤销，请更新本机 GROQ_API_KEY。"
        elif status == 403:
            detail = "Groq 拒绝访问，请检查账号、模型权限及服务地域限制。"
        elif status == 429:
            detail = "Groq 请求频率或音频额度已达限制，请等待额度恢复后重试；未自动切换到收费服务。"
        elif status == 413:
            detail = "Groq 音频分片过大，请减小 GROQ_ASR_CHUNK_SECONDS。"
        else:
            detail = "Groq ASR 调用失败，请检查模型配置或稍后重试。"
        context = [f"模型：{self.settings.groq_asr_model}", f"HTTP：{status}"]
        try:
            data = response.json()
            error = data.get("error", {}) if isinstance(data, dict) else {}
            if isinstance(error, dict):
                for label, key in (("错误码", "code"), ("服务提示", "message")):
                    if error.get(key):
                        value = str(error[key])
                        if self.settings.groq_api_key:
                            value = value.replace(self.settings.groq_api_key, "[REDACTED]")
                        context.append(f"{label}：{value[:500]}")
        except ValueError:
            pass
        request_id = response.headers.get("x-request-id")
        if request_id:
            context.append(f"Request ID：{request_id}")
        message = f"{detail}（{'；'.join(context)}）"
        if self.settings.groq_api_key:
            message = message.replace(self.settings.groq_api_key, "[REDACTED]")
        return WorkflowError(message)


def _parse_segments(data: object, duration: float) -> list[TranscriptSegment]:
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise ValueError("missing segments")
    result = []
    for item in data["segments"]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("invalid segment")
        text = item["text"].strip()
        if not text:
            continue
        start, end = float(item["start"]), float(item["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError("invalid timestamp")
        if start >= duration:
            raise ValueError("timestamp exceeds audio duration")
        result.append(TranscriptSegment(start=start, end=min(end, duration), text=text))
    if data.get("text") and not result:
        raise ValueError("text without timestamps")
    return sorted(result, key=lambda segment: (segment.start, segment.end))
