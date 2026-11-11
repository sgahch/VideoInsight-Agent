from __future__ import annotations

import json
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path

import httpx
import pytest

import app.adapters.groq_asr as groq
from app.adapters.asr import MockTranscriber, ParaformerTranscriber, select_transcriber
from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError


def _settings(**kwargs) -> Settings:
    return Settings(**{
        "asr_provider": "groq",
        "groq_api_key": "private-test-groq-key",
        "groq_asr_chunk_seconds": 1,
        "groq_asr_global_workers": 2,
        "groq_asr_max_workers": 2,
        "groq_asr_requests_per_minute": 10000,
        **kwargs,
    })


def _wav(path: Path, seconds: int = 1) -> Path:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000 * seconds)
    return path


def _payload(text: str = "识别结果") -> dict:
    return {"text": text, "segments": [{"start": 0, "end": 0.8, "text": text}]}


class _Preprocessor:
    def convert_to_wav16k(self, source, target):
        target.write_bytes(source.read_bytes())
        return target


def _mock_client(monkeypatch, handler):
    original_client = httpx.Client
    monkeypatch.setattr(
        groq.httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def _multipart(request: httpx.Request) -> dict:
    raw = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.read()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}


def test_provider_selection_is_explicit_and_mock_still_works():
    assert isinstance(select_transcriber(_settings(), False), groq.GroqTranscriber)
    assert isinstance(select_transcriber(_settings(asr_provider="paraformer"), False), ParaformerTranscriber)
    assert isinstance(select_transcriber(_settings(asr_provider="invalid"), True), MockTranscriber)
    with pytest.raises(ExternalDependencyError, match="ASR_PROVIDER"):
        select_transcriber(_settings(asr_provider="invalid"), False)


def test_long_audio_preserves_offsets_progress_and_request_parameters(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        assert str(request.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer private-test-groq-key"
        parts = _multipart(request)
        assert parts["model"].get_content() == "whisper-large-v3-turbo"
        assert parts["response_format"].get_content() == "verbose_json"
        assert parts["timestamp_granularities[]"].get_content() == "segment"
        assert parts["temperature"].get_content() == "0"
        index = int(Path(parts["file"].get_filename()).stem.rsplit("-", 1)[1])
        with wave.open(BytesIO(parts["file"].get_payload(decode=True))) as audio:
            assert audio.getframerate() == 16000
            assert audio.getnframes() == 16000
        calls.append(index)
        return httpx.Response(200, json=_payload(f"chunk-{index}"))

    class Reporter:
        started = None
        chunks = []

        def mark_transcribe_started(self, duration, total):
            self.started = duration, total

        def on_transcribe_chunk(self, done, total):
            self.chunks.append((done, total))

    _mock_client(monkeypatch, handler)
    reporter = Reporter()
    source = _wav(tmp_path / "source.wav", 3)
    transcriber = groq.GroqTranscriber(_settings(), _Preprocessor())
    segments = transcriber.transcribe(source, "task", reporter)

    assert sorted(calls) == [0, 1, 2]
    assert [item.text for item in segments] == ["chunk-0", "chunk-1", "chunk-2"]
    assert [item.start for item in segments] == [0, 1, 2]
    assert [item.end for item in segments] == [0.8, 1.8, 2.8]
    assert transcriber.last_duration == 3
    assert reporter.started == (3, 3)
    assert reporter.chunks == [(1, 3), (2, 3), (3, 3)]
    assert source.exists()
    assert list(tmp_path.glob("task-*.wav")) == []


def test_short_audio_is_one_request_and_does_not_require_dashscope(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload())

    _mock_client(monkeypatch, handler)
    source = _wav(tmp_path / "source.wav")
    transcriber = groq.GroqTranscriber(_settings(dashscope_api_key=None), _Preprocessor())
    assert transcriber.transcribe(source, "task")[0].text == "识别结果"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 413])
def test_permanent_errors_are_not_retried_or_sent_to_paraformer(monkeypatch, tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"code": "denied", "message": "private-test-groq-key"}},
            headers={"x-request-id": "request-123"},
        )

    _mock_client(monkeypatch, handler)
    monkeypatch.setattr(
        ParaformerTranscriber, "transcribe",
        lambda *args, **kwargs: pytest.fail("must not fall back to a paid provider"),
    )
    source = _wav(tmp_path / "source.wav", 3)
    transcriber = groq.GroqTranscriber(_settings(groq_asr_global_workers=1), _Preprocessor())
    monkeypatch.setattr(transcriber, "_wait", lambda delay: pytest.fail("must not retry"))
    with pytest.raises(WorkflowError) as error:
        transcriber.transcribe(source, "task")
    assert len(calls) == 1
    assert f"HTTP：{status}" in str(error.value)
    assert "request-123" in str(error.value)
    assert "private-test-groq-key" not in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert list(tmp_path.glob("task-*.wav")) == []


@pytest.mark.parametrize("status", [429, 503])
def test_short_transient_errors_retry_with_backoff(tmp_path, status):
    calls = []
    waits = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(status, json={"error": {"message": "try later"}}, headers={"retry-after": "1"})
        return httpx.Response(200, json=_payload())

    transcriber = groq.GroqTranscriber(_settings())
    transcriber._wait = waits.append
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        segments = transcriber._recognize_file(_wav(tmp_path / "source.wav"), client)
    assert segments[0].text == "识别结果"
    assert len(calls) == 3
    assert waits == [1.0, 2.0]


def test_long_quota_backoff_stops_without_sleeping(tmp_path):
    calls = []
    transcriber = groq.GroqTranscriber(_settings())

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}}, headers={"retry-after": "3600"})

    transcriber._wait = lambda delay: pytest.fail("must not wait an hour or keep retrying")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WorkflowError, match="音频额度"):
            transcriber._recognize_file(_wav(tmp_path / "source.wav"), client)
    assert len(calls) == 1


def test_retries_are_bounded(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    transcriber = groq.GroqTranscriber(_settings())
    transcriber._wait = lambda delay: None
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WorkflowError, match="unavailable"):
            transcriber._recognize_file(_wav(tmp_path / "source.wav"), client)
    assert len(calls) == 3


def test_network_error_does_not_echo_credentials_or_repeat_upload(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-test-groq-key", request=request)

    transcriber = groq.GroqTranscriber(_settings())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WorkflowError, match="网络连接失败") as error:
            transcriber._recognize_file(_wav(tmp_path / "source.wav"), client)
    assert len(calls) == 1
    assert "private-test-groq-key" not in str(error.value)


def test_error_redaction_happens_before_message_truncation():
    transcriber = groq.GroqTranscriber(_settings())
    response = httpx.Response(400, json={
        "error": {"message": "x" * 490 + "private-test-groq-key"},
    })
    error = str(transcriber._response_error(response))
    assert "private" not in error


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"text": "text", "segments": []},
        {"segments": [{"start": 0, "end": 1}]},
        {"segments": [{"start": -1, "end": 1, "text": "text"}]},
        {"segments": [{"start": 1, "end": 0, "text": "text"}]},
        {"segments": [{"start": 0, "end": float("inf"), "text": "text"}]},
        {"segments": [{"start": float("nan"), "end": 1, "text": "text"}]},
    ],
)
def test_invalid_timestamps_are_not_fabricated(data):
    with pytest.raises(ValueError):
        groq._parse_segments(data, 10)


def test_timestamps_are_sorted_and_clamped_to_real_audio():
    data = {"segments": [
        {"start": 2, "end": 12, "text": " second "},
        {"start": 0, "end": 1, "text": "first"},
    ]}
    segments = groq._parse_segments(data, 3)
    assert [item.text for item in segments] == ["first", "second"]
    assert segments[-1].end == 3


def test_process_wide_request_limit_is_shared_and_waits(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(groq, "_REQUEST_TIMES", groq.deque())
    monkeypatch.setattr(groq, "monotonic", lambda: clock[0])
    first = groq.GroqTranscriber(_settings(groq_asr_requests_per_minute=2))
    second = groq.GroqTranscriber(_settings(groq_asr_requests_per_minute=2))
    waits = []

    def wait(delay):
        waits.append(delay)
        clock[0] += delay

    second._wait = wait
    first._wait_for_request_slot()
    second._wait_for_request_slot()
    second._wait_for_request_slot()
    assert waits == [60]
    assert list(groq._REQUEST_TIMES) == [60]


def test_concurrency_limit_is_shared_between_videos(tmp_path):
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def handler(request):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        started.set()
        assert release.wait(timeout=5)
        with lock:
            state["active"] -= 1
        return httpx.Response(200, json=_payload())

    paths = [_wav(tmp_path / f"{i}.wav") for i in range(4)]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(groq.GroqTranscriber(_settings(groq_asr_global_workers=1))._recognize_file, path, client)
            for path in paths
        ]
        try:
            assert started.wait(timeout=5)
        finally:
            release.set()
        assert all(future.result(timeout=5) for future in futures)
    assert state["peak"] == 1


def test_credentials_and_invalid_models_are_checked_before_conversion(tmp_path):
    source = tmp_path / "missing.wav"
    with pytest.raises(ExternalDependencyError, match="GROQ_API_KEY"):
        groq.GroqTranscriber(_settings(groq_api_key=None)).transcribe(source, "task")
    with pytest.raises(ExternalDependencyError, match="GROQ_ASR_MODEL"):
        groq.GroqTranscriber(_settings(groq_asr_model="unrelated-model")).transcribe(source, "task")
    assert "private-test-groq-key" not in repr(_settings())


def test_health_reports_provider_without_credentials(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "settings", _settings())
    health = main.health()
    assert health["asr_provider"] == "groq"
    assert health["asr_model"] == "whisper-large-v3-turbo"
    assert "private-test-groq-key" not in json.dumps(health)
    assert _settings().asr_global_workers == 2


@pytest.mark.parametrize("uploaded", [False, True])
def test_both_workflow_entry_points_use_groq_without_calling_llm(monkeypatch, tmp_path, uploaded):
    import app.workflow as workflow
    from app.adapters.downloader import DownloadedMedia
    from app.schemas import AnalysisMode, AnalyzeRequest

    _mock_client(monkeypatch, lambda request: httpx.Response(200, json=_payload()))
    monkeypatch.setattr(groq, "FfmpegAudioPreprocessor", _Preprocessor)
    monkeypatch.setattr(
        workflow, "OpenAICompatibleAnalyzer",
        lambda *args, **kwargs: pytest.fail("transcript mode must not call an LLM"),
    )
    source = _wav(tmp_path / "source.wav")
    runtime = _settings(
        mock_mode=False, tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results",
        media_dir=tmp_path / "media", db_path=tmp_path / "tasks.db",
    )
    request = AnalyzeRequest(
        url="https://example.com/video", mode=AnalysisMode.transcript, use_mock=False,
        with_comments=False,
    )

    class Downloader:
        def download_audio(self, url, task_id):
            return DownloadedMedia(source, "video", "author", 1, url)

    monkeypatch.setattr(workflow, "select_downloader", lambda *args: Downloader())
    if uploaded:
        result = workflow.analyze_media_file(request, "task", source, runtime)
    else:
        result = workflow.analyze(request, "task", runtime)
    assert result.output == "识别结果"
    assert result.segments[0].end == 0.8
    assert result.diagnostics["asr_provider"] == "groq"
    assert result.diagnostics["asr_model"] == "whisper-large-v3-turbo"
    assert "private-test-groq-key" not in result.model_dump_json()
    assert source.exists()
