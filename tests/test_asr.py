from __future__ import annotations

import sys
import threading
import types
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from app.adapters.asr import ParaformerTranscriber
from app.config import Settings
from app.errors import WorkflowError


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


@pytest.mark.parametrize(
    ("status", "code", "message", "expected"),
    [
        (403, "AccessDenied.Unpurchased", "denied", "开通阿里云百炼"),
        (400, "", "Access to model denied. Please make sure you are eligible for using the model.", "开通阿里云百炼"),
        (400, "Arrearage", "denied", "计费状态异常"),
        (400, "", "Access denied, please make sure your account is in good standing.", "计费状态异常"),
        (401, "InvalidApiKey", "invalid key", "鉴权失败"),
        (403, "Model.AccessDenied", "denied", "权限不足"),
        (400, "InvalidParameter", "bad format", "bad format"),
        (404, "ModelNotFound", "model not found", "model not found"),
    ],
)
def test_permanent_asr_errors_fail_without_retry(
    monkeypatch, tmp_path: Path, status: int, code: str, message: str, expected: str,
) -> None:
    calls = []

    class Recognition:
        def __init__(self, **kwargs):
            pass

        def call(self, path):
            calls.append(path)
            return types.SimpleNamespace(
                status_code=status, code=code, message=message, request_id="request-123",
            )

    def no_sleep(_seconds):
        pytest.fail("permanent ASR errors must not enter retry backoff")

    monkeypatch.setattr("app.adapters.asr.sleep", no_sleep)
    settings = _chunked_settings(tmp_path)
    with pytest.raises(WorkflowError) as error:
        ParaformerTranscriber(settings)._recognize_file(tmp_path / "audio.wav", Recognition)

    assert len(calls) == 1
    assert expected in str(error.value)
    assert settings.dashscope_asr_model in str(error.value)
    assert f"HTTP：{status}" in str(error.value)
    assert "request-123" in str(error.value)
    if "Access to model denied" in message:
        assert "欠费" not in str(error.value)


@pytest.mark.parametrize("status", [429, 503])
def test_transient_asr_errors_retry_and_recover(monkeypatch, tmp_path: Path, status: int) -> None:
    calls = []
    sleeps = []

    class Recognition:
        def __init__(self, **kwargs):
            pass

        def call(self, path):
            calls.append(path)
            if len(calls) < 3:
                return types.SimpleNamespace(status_code=status, code="Temporary", message="try later")
            return FakeResult("recovered", 1000)

    monkeypatch.setattr("app.adapters.asr.sleep", sleeps.append)
    segments = ParaformerTranscriber(_chunked_settings(tmp_path))._recognize_file(
        tmp_path / "audio.wav", Recognition,
    )
    assert [segment.text for segment in segments] == ["recovered"]
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("legacy", [False, True])
def test_permission_failure_stops_other_chunks_without_fallback(
    monkeypatch, tmp_path: Path, legacy: bool,
) -> None:
    _install_fake_dashscope(monkeypatch)
    calls = []

    def denied(self, path):
        calls.append(path)
        return types.SimpleNamespace(
            status_code=403, code="AccessDenied.Unpurchased", message="denied",
        )

    monkeypatch.setattr(FakeRecognition, "call", denied)
    monkeypatch.setattr("app.adapters.asr.audio_duration", lambda _path: None if legacy else 3)
    source = tmp_path / "source.wav"
    _make_wav(source, 3)
    settings = replace(_chunked_settings(tmp_path), dashscope_asr_global_workers=1)
    transcriber = ParaformerTranscriber(settings, FakePreprocessor())
    if not legacy:
        def unexpected_fallback(*args):
            pytest.fail("cloud recognition failure must not trigger another transcription")
        monkeypatch.setattr(transcriber, "_transcribe_legacy", unexpected_fallback)

    with pytest.raises(WorkflowError, match="开通阿里云百炼"):
        transcriber.transcribe(source, "task")
    assert len(calls) == 1
    assert list(tmp_path.glob("task-*.wav")) == []


def test_exhausted_service_errors_do_not_trigger_transcoding_fallback(monkeypatch, tmp_path: Path) -> None:
    _install_fake_dashscope(monkeypatch)
    calls = []

    def unavailable(self, path):
        calls.append(path)
        return types.SimpleNamespace(status_code=503, code="ServiceUnavailable", message="try later")

    monkeypatch.setattr(FakeRecognition, "call", unavailable)
    monkeypatch.setattr("app.adapters.asr.audio_duration", lambda _path: 2)
    monkeypatch.setattr("app.adapters.asr.sleep", lambda _seconds: None)
    source = tmp_path / "source.wav"
    _make_wav(source, 2)
    transcriber = ParaformerTranscriber(_chunked_settings(tmp_path), FakePreprocessor())
    monkeypatch.setattr(
        transcriber, "_transcribe_legacy",
        lambda *args: pytest.fail("exhausted service retries must not upload the whole file again"),
    )

    with pytest.raises(WorkflowError, match="try later"):
        transcriber.transcribe(source, "task")
    assert len(calls) == 6
    assert list(tmp_path.glob("task-*.wav")) == []


def test_asr_error_does_not_expose_key(monkeypatch, tmp_path: Path) -> None:
    key = "secret-asr-api-key"
    settings = replace(_chunked_settings(tmp_path), dashscope_api_key=key)

    class Recognition:
        def __init__(self, **kwargs):
            pass

        def call(self, path):
            return types.SimpleNamespace(status_code=400, message=f"unexpected response for {key}")

    with pytest.raises(WorkflowError) as error:
        ParaformerTranscriber(settings)._recognize_file(tmp_path / "audio.wav", Recognition)
    assert key not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_chunks_are_not_removed_while_recognition_is_still_using_them(monkeypatch, tmp_path: Path) -> None:
    _install_fake_dashscope(monkeypatch)
    started = threading.Event()
    shutting_down = threading.Event()
    release = threading.Event()
    source = tmp_path / "source.wav"
    _make_wav(source, 2)

    def recognize(self, path):
        if path.endswith("0000.wav"):
            assert started.wait(timeout=5)
            return types.SimpleNamespace(status_code=403, message="denied")
        started.set()
        assert release.wait(timeout=5)
        assert Path(path).exists()
        return FakeResult("done", 1000)

    monkeypatch.setattr(FakeRecognition, "call", recognize)
    monkeypatch.setattr("app.adapters.asr.audio_duration", lambda _path: 2)
    original_shutdown = ThreadPoolExecutor.shutdown

    def shutdown(self, *args, **kwargs):
        shutting_down.set()
        return original_shutdown(self, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "shutdown", shutdown)
    settings = replace(_chunked_settings(tmp_path), dashscope_asr_global_workers=2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ParaformerTranscriber(settings).transcribe, source, "task")
        try:
            assert shutting_down.wait(timeout=5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
            assert (tmp_path / "task-asr-0001.wav").exists()
        finally:
            release.set()
        with pytest.raises(WorkflowError):
            future.result(timeout=5)
    assert list(tmp_path.glob("task-*.wav")) == []


def test_new_transcription_clears_previous_permission_failure(monkeypatch, tmp_path: Path) -> None:
    _install_fake_dashscope(monkeypatch)
    source = tmp_path / "source.wav"
    _make_wav(source, 1)
    monkeypatch.setattr("app.adapters.asr.audio_duration", lambda _path: 1)
    transcriber = ParaformerTranscriber(_chunked_settings(tmp_path), FakePreprocessor())
    original_call = FakeRecognition.call
    monkeypatch.setattr(
        FakeRecognition, "call",
        lambda *args: types.SimpleNamespace(status_code=403, message="denied"),
    )
    with pytest.raises(WorkflowError):
        transcriber.transcribe(source, "first")
    monkeypatch.setattr(FakeRecognition, "call", original_call)
    assert transcriber.transcribe(source, "second")[0].text == "chunk-0"


def test_local_decoder_failure_still_uses_legacy_fallback(monkeypatch, tmp_path: Path) -> None:
    _install_fake_dashscope(monkeypatch)
    monkeypatch.setattr("app.adapters.asr.audio_duration", lambda _path: 3)
    source = tmp_path / "source.wav"
    _make_wav(source, 3)
    transcriber = ParaformerTranscriber(_chunked_settings(tmp_path), FakePreprocessor())

    def decoder_failed(*args):
        raise WorkflowError("ffmpeg 解码失败")

    monkeypatch.setattr(transcriber, "_transcribe_concurrent", decoder_failed)
    segments = transcriber.transcribe(source, "task")
    assert [segment.text for segment in segments] == ["chunk-0", "chunk-1", "chunk-2"]
