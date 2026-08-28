from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from app.adapters.audio import FfmpegAudioPreprocessor
from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError
from app.schemas import TranscriptSegment


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

        recognition = Recognition(
            model=self.settings.dashscope_asr_model,
            format="wav",
            sample_rate=16000,
            language_hints=["zh", "en"],
            callback=None,
        )
        result = recognition.call(str(wav_path))
        if result.status_code != HTTPStatus.OK:
            raise WorkflowError(f"ASR 识别失败：{result.message}")

        segments: list[TranscriptSegment] = []
        for sentence in result.get_sentence():
            segments.append(
                TranscriptSegment(
                    start=(sentence.get("begin_time") or 0) / 1000,
                    end=(sentence.get("end_time") or 0) / 1000,
                    text=sentence.get("text", ""),
                )
            )
        return segments


def format_transcript(segments: list[TranscriptSegment], include_timestamps: bool = True) -> str:
    if include_timestamps:
        return "\n".join(f"[{item.start:.2f}-{item.end:.2f}] {item.text}" for item in segments)
    return "".join(item.text for item in segments)

