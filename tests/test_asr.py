from __future__ import annotations

import sys
import types
import wave
from pathlib import Path

from app.adapters.asr import ParaformerTranscriber
from app.config import Settings


class FakeResult:
    status_code = 200
    message = "ok"

    def __init__(self, text: str, duration_ms: int) -> None:
        self.text = text
        self.duration_ms = duration_ms

    def get_sentence(self) -> list[dict[str, object]]:
        return [{"begin_time": 0, "end_time": self.duration_ms, "text": self.text}]


class FakeRecognition:
    calls: list[str] = []

    def __init__(self, **_kwargs) -> None:
        pass

    def call(self, file: str) -> FakeResult:
        self.calls.append(file)
        index = int(Path(file).stem.rsplit("-", 1)[-1]) if "-asr-" in file else 0
        return FakeResult(f"chunk-{index}", 1000)


class FakePreprocessor:
    def convert_to_wav16k(self, input_audio: Path, output_wav: Path) -> Path:
        output_wav.write_bytes(input_audio.read_bytes())
        return output_wav


def _make_wav(path: Path, seconds: int) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\0\0" * 16000 * seconds)


def _install_fake_dashscope(monkeypatch) -> None:
    dashscope = types.ModuleType("dashscope")
    dashscope.audio = types.ModuleType("dashscope.audio")
    asr_module = types.ModuleType("dashscope.audio.asr")
    asr_module.Recognition = FakeRecognition
    dashscope.audio.asr = asr_module
    monkeypatch.setitem(sys.modules, "dashscope", dashscope)
    monkeypatch.setitem(sys.modules, "dashscope.audio", dashscope.audio)
    monkeypatch.setitem(sys.modules, "dashscope.audio.asr", asr_module)


def _chunked_settings(tmp_path: Path) -> Settings:
    return Settings(
        mock_mode=False,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        dashscope_api_key="test",
        dashscope_asr_chunk_threshold_seconds=2,
        dashscope_asr_chunk_seconds=1,
        dashscope_asr_max_workers=2,
    )


def test_long_audio_is_split_and_timestamps_are_offset(monkeypatch, tmp_path: Path) -> None:
    _install_fake_dashscope(monkeypatch)

    source = tmp_path / "source.wav"
    _make_wav(source, 3)
    settings = _chunked_settings(tmp_path)
    FakeRecognition.calls = []

    segments = ParaformerTranscriber(settings, FakePreprocessor()).transcribe(source, "task")

    assert len(FakeRecognition.calls) == 3
    assert [segment.text for segment in segments] == ["chunk-0", "chunk-1", "chunk-2"]
    assert [segment.start for segment in segments] == [0, 1, 2]
    assert list(tmp_path.glob("task-asr-*.wav")) == []


def test_chunk_cleanup_failure_does_not_kill_transcription(monkeypatch, tmp_path: Path) -> None:
    """分片清理失败（文件被占用 / 运行环境的删除保护）不应毁掉已完成的转写。

    真实事故：unlink 被环境拦截成 SystemExit，finally 里的清理击穿整个任务，
    25 分钟音频的 ASR 结果与进程一起丢失。
    """
    _install_fake_dashscope(monkeypatch)

    def blocked_unlink(self, missing_ok: bool = False) -> None:
        raise SystemExit("safe-delete blocked")

    monkeypatch.setattr(Path, "unlink", blocked_unlink)

    source = tmp_path / "source.wav"
    _make_wav(source, 3)
    FakeRecognition.calls = []

    segments = ParaformerTranscriber(_chunked_settings(tmp_path), FakePreprocessor()).transcribe(
        source, "task"
    )

    assert len(FakeRecognition.calls) == 3
    assert [segment.text for segment in segments] == ["chunk-0", "chunk-1", "chunk-2"]
