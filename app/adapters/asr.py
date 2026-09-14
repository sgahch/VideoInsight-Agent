from __future__ import annotations

import logging
import wave
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path

from app.adapters.audio import FfmpegAudioPreprocessor
from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError
from app.schemas import TranscriptSegment


LOGGER = logging.getLogger(__name__)


class MockTranscriber:
    def transcribe(self, audio_path: Path | None, task_id: str) -> list[TranscriptSegment]:
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

    def transcribe(self, audio_path: Path | None, task_id: str) -> list[TranscriptSegment]:
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

        wav_path = audio_path.parent / f"{task_id}-16k.wav"
        self.preprocessor.convert_to_wav16k(audio_path, wav_path)

        duration = _wav_duration(wav_path)
        if (
            duration is not None
            and duration >= max(1, self.settings.dashscope_asr_chunk_threshold_seconds)
            and self.settings.dashscope_asr_chunk_seconds > 0
            and self.settings.dashscope_asr_max_workers > 1
        ):
            return self._transcribe_long_wav(
                wav_path,
                task_id,
                Recognition,
                duration,
            )

        return self._recognize_file(wav_path, Recognition)

    def _recognize_file(self, wav_path: Path, recognition_class: object) -> list[TranscriptSegment]:
        recognition = recognition_class(
            model=self.settings.dashscope_asr_model,
            format="wav",
            sample_rate=16000,
            language_hints=["zh", "en"],
            callback=None,
        )
        result = recognition.call(str(wav_path))
        if result.status_code != HTTPStatus.OK:
            raise WorkflowError(f"ASR 识别失败：{result.message}")
        return _result_to_segments(result)

    def _transcribe_long_wav(
        self,
        wav_path: Path,
        task_id: str,
        recognition_class: object,
        duration: float,
    ) -> list[TranscriptSegment]:
        chunk_seconds = max(1, self.settings.dashscope_asr_chunk_seconds)
        chunk_paths = _split_wav(wav_path, task_id, chunk_seconds, duration)
        workers = min(max(1, self.settings.dashscope_asr_max_workers), len(chunk_paths))

        try:
            def recognize_chunk(item: tuple[int, Path]) -> tuple[int, list[TranscriptSegment]]:
                index, path = item
                local_segments = self._recognize_file(path, recognition_class)
                offset = index * chunk_seconds
                return index, [
                    segment.model_copy(
                        update={
                            "start": segment.start + offset,
                            "end": segment.end + offset,
                        }
                    )
                    for segment in local_segments
                ]

            results: list[tuple[int, list[TranscriptSegment]]] = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(recognize_chunk, item) for item in enumerate(chunk_paths)]
                for future in futures:
                    results.append(future.result())

            segments = [segment for _, chunk in sorted(results) for segment in chunk]
            segments.sort(key=lambda segment: (segment.start, segment.end))
            return segments
        finally:
            # 分片已在并发识别中全部消费完；清理是收尾动作，绝不能反过来
            # 毁掉已经成功的转写（Windows 上文件被占用/只读目录都会抛异常）。
            for chunk_path in chunk_paths:
                _best_effort_unlink(chunk_path)


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


def format_transcript(segments: list[TranscriptSegment], include_timestamps: bool = True) -> str:
    if include_timestamps:
        return "\n".join(f"[{item.start:.2f}-{item.end:.2f}] {item.text}" for item in segments)
    return "".join(item.text for item in segments)
