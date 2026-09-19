from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import app.adapters.audio as audio_module
from app.adapters.audio import FfmpegAudioPreprocessor
from app.errors import WorkflowError


def test_converts_with_timeout_and_success(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "in.mp4"
    source.write_bytes(b"not really a video")
    output = tmp_path / "out-16k.wav"
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        captured["cmd"] = cmd
        output.write_bytes(b"RIFF....WAVE")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(audio_module, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio_module.subprocess, "run", fake_run)

    result = FfmpegAudioPreprocessor().convert_to_wav16k(source, output)

    assert result == output
    # 超时是必填项：少了它，损坏的源文件会让 ffmpeg 永久占住一个线程池工人
    assert captured["timeout"] == audio_module.FFMPEG_TIMEOUT_SECONDS
    # 16kHz 单声道 PCM，与 ASR 的输入约定一致
    assert captured["cmd"][-2:] == ["-y", str(output)]
    assert "16000" in captured["cmd"]


def test_ffmpeg_timeout_raises_and_removes_partial_output(monkeypatch, tmp_path: Path) -> None:
    """回归：超时中止后必须删掉半成品 WAV。

    半成品 WAV 的文件头是合法的，`_wav_duration` 会读出一个偏短的时长并继续
    送进 ASR，最终静默产出一份不完整的转写稿 —— 比直接报错更糟。
    """
    source = tmp_path / "in.mp4"
    source.write_bytes(b"not really a video")
    output = tmp_path / "out-16k.wav"
    output.write_bytes(b"RIFF partial output")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(audio_module, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio_module.subprocess, "run", fake_run)

    with pytest.raises(WorkflowError, match="超过"):
        FfmpegAudioPreprocessor().convert_to_wav16k(source, output)

    assert not output.exists()


def test_ffmpeg_failure_reports_stderr(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "in.mp4"
    source.write_bytes(b"broken")
    output = tmp_path / "out-16k.wav"

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Invalid data found")

    monkeypatch.setattr(audio_module, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio_module.subprocess, "run", fake_run)

    with pytest.raises(WorkflowError, match="Invalid data found"):
        FfmpegAudioPreprocessor().convert_to_wav16k(source, output)
