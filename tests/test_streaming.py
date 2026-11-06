from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.workflow as workflow
from app.adapters.llm import OpenAICompatibleAnalyzer
from app.config import Settings
from app.errors import WorkflowError
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo, TaskStatus
from app.streaming import iter_events
from app.task_store import SQLiteTaskStore


@pytest.fixture
def store(monkeypatch, tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main, "task_store", store)
    return store


def make_task(store, *, owner="owner", succeeded=False):
    task = store.create(AnalyzeRequest(
        url="https://example.com/video", use_mock=True, with_comments=False,
        client_id=owner, llm_api_key="never-serialize-this",
    ))
    if succeeded:
        store.set_succeeded(task.task_id, make_result())
    return task


def make_result(output="summary", transcript="transcript"):
    return AnalysisResult(
        mode=AnalysisMode.summary, transcript=transcript, output=output,
        title="Result title", media=MediaInfo(source_url="https://example.com/video", title="Media title"),
    )


def test_summary_listing_is_small_scoped_and_keeps_full_api(store, monkeypatch):
    task = make_task(store)
    store.set_succeeded(task.task_id, make_result(transcript="large transcript " * 10000))
    make_task(store, owner="other", succeeded=True)
    make_task(store, owner=None, succeeded=True)
    client = TestClient(main.app)
    headers = {"X-Client-Id": "owner"}

    full = client.get("/tasks", headers=headers)
    summary = client.get("/tasks?view=summary", headers=headers)
    assert full.status_code == summary.status_code == 200
    assert len(summary.json()) == 1
    item = summary.json()[0]
    assert item["title"] == "Result title"
    assert item["media_title"] == "Media title"
    assert "result" not in item
    assert "never-serialize-this" not in summary.text
    assert len(summary.content) < len(full.content) / 100
    assert full.json()[0]["result"]["transcript"].startswith("large transcript")
    assert client.get("/tasks?view=summary&status=failed", headers=headers).json() == []
    assert len(client.get("/tasks?view=summary").json()) == 1
    assert client.get("/tasks?view=invalid").status_code == 422

    def no_full_record(*args):
        raise AssertionError("History summaries must not deserialize full results")

    monkeypatch.setattr(store, "_row_to_task", no_full_record)
    assert store.list_summaries(client_id="owner")[0].task_id == task.task_id


def test_summary_listing_keeps_stable_order_and_limit(store):
    first = make_task(store)
    second = make_task(store)
    assert [t.task_id for t in store.list_summaries(client_id="owner")] == [second.task_id, first.task_id]
    assert len(store.list_summaries(client_id="owner", limit=1)) == 1


def test_partial_results_survive_failure_and_reopening(store):
    task = make_task(store)
    store.set_running(task.task_id)
    store.set_partial_result(task.task_id, make_result(output="partial"))
    store.set_failed(task.task_id, "provider disconnected")
    restored = SQLiteTaskStore(store.db_path).get(task.task_id)
    assert restored.status == TaskStatus.failed
    assert restored.result.output == "partial"
    assert restored.result.transcript == "transcript"


def test_event_endpoint_checks_owner_and_sends_terminal_task(store):
    task = make_task(store, succeeded=True)
    client = TestClient(main.app)
    assert client.get(f"/tasks/{task.task_id}/events").status_code == 404
    assert client.get(f"/tasks/{task.task_id}/events", headers={"X-Client-Id": "other"}).status_code == 404
    response = client.get(f"/tasks/{task.task_id}/events", headers={"X-Client-Id": "owner"})
    events = list(iter_events(response.text.splitlines()))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert events[0][0] == "complete"
    assert json.loads(events[0][1])["result"]["output"] == "summary"
    assert "never-serialize-this" not in response.text


def test_task_events_resume_snapshot_then_only_send_deltas(store, monkeypatch):
    task = make_task(store)
    store.set_running(task.task_id)
    monkeypatch.setattr(main, "get_snapshot", lambda *args: None)

    async def check():
        stream = main.task_events(task.task_id, "owner").body_iterator
        assert "event: snapshot" in await anext(stream)
        store.set_partial_result(task.task_id, make_result(output=""))
        transcript_event = await anext(stream)
        assert "event: snapshot" in transcript_event
        assert '"transcript": "transcript"' in transcript_event
        store.set_partial_result(task.task_id, make_result(output="first"))
        delta = await anext(stream)
        assert "event: delta" in delta and '"text": "first"' in delta
        assert "transcript" not in delta
        await stream.aclose()

        resumed = main.task_events(task.task_id, "owner").body_iterator
        assert '"output": "first"' in await anext(resumed)
        store.set_succeeded(task.task_id, make_result(output="first second"))
        assert "event: complete" in await anext(resumed)
        await resumed.aclose()

    asyncio.run(check())
    assert store.get(task.task_id).status == TaskStatus.succeeded


def test_task_events_handle_deleted_task(store):
    task = make_task(store)

    async def check():
        stream = main.task_events(task.task_id, "owner").body_iterator
        await anext(stream)
        store.delete(task.task_id)
        assert '"status": 404' in await anext(stream)
        await stream.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("protocol,frames", [
    ("chat_completions", [
        {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
    ]),
    ("responses", [
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.output_text.delta", "delta": " world"},
        {"type": "response.completed"},
    ]),
    ("messages", [
        {"type": "content_block_start", "content_block": {"type": "text", "text": "Hello"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " world"}},
        {"type": "message_stop"},
    ]),
])
def test_llm_stream_protocols(monkeypatch, protocol, frames):
    def handler(request):
        assert json.loads(request.content)["stream"] is True
        body = "".join("data: " + json.dumps(frame) + "\r\n\r\n" for frame in frames)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "stream", client.stream)
    analyzer = OpenAICompatibleAnalyzer(Settings(), api_key="test-key", protocol=protocol)
    assert list(analyzer.chat_stream("question", "transcript")) == ["Hello", " world"]
    client.close()


def test_llm_stream_json_gateway_and_endpoint_fallback(monkeypatch):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if request.url.path == "/chat/completions":
            return httpx.Response(404)
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "stream", client.stream)
    analyzer = OpenAICompatibleAnalyzer(Settings(), api_key="test-key", base_url="https://llm.example")
    assert "".join(analyzer.chat_stream("question", "transcript")) == "answer"
    assert len(urls) == 2 and urls[-1].endswith("/v1/chat/completions")
    client.close()


def test_llm_disconnect_does_not_silently_succeed_or_retry(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                              headers={"content-type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "stream", client.stream)
    analyzer = OpenAICompatibleAnalyzer(Settings(), api_key="test-key", base_url="https://llm.example")
    chunks = analyzer.chat_stream("question", "transcript")
    assert next(chunks) == "partial"
    with pytest.raises(WorkflowError, match="中断"):
        next(chunks)
    assert len(calls) == 1
    client.close()


def test_iter_events_supports_comments_multiline_and_final_frame():
    assert list(iter_events([
        ": heartbeat", "event: delta", 'data: {"text":', 'data: "hello"}', "",
        "data: [DONE]",
    ])) == [("delta", '{"text":\n"hello"}'), ("", "[DONE]")]


def test_chat_stream_is_scoped_and_keeps_nonstreaming_api(store):
    task = make_task(store, succeeded=True)
    client = TestClient(main.app)
    path = f"/tasks/{task.task_id}/chat"
    assert client.post(path + "/stream", json={"question": "hello"}).status_code == 404
    headers = {"X-Client-Id": "owner"}
    response = client.post(path + "/stream", headers=headers, json={"question": "hello"})
    events = [(event, json.loads(raw)) for event, raw in iter_events(response.text.splitlines())]
    assert response.status_code == 200
    assert events[-1][0] == "done"
    assert "Mock" in events[0][1]["text"]
    assert client.post(path, headers=headers, json={"question": "hello"}).json()["answer"] == events[0][1]["text"]
    assert client.post(path + "/stream", headers=headers, json={"question": " "}).status_code == 422


def test_stream_routes_still_require_access_token(store, monkeypatch):
    import app.security as security

    config = replace(main.settings, auth_token="private-token")
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(security, "settings", config)
    task = make_task(store, succeeded=True)
    client = TestClient(main.app)
    path = f"/tasks/{task.task_id}"
    assert client.get(path + "/events").status_code == 401
    assert client.post(path + "/chat/stream", json={"question": "hello"}).status_code == 401
    headers = {"X-Client-Id": "owner", "Authorization": "Bearer private-token"}
    assert client.get(path + "/events", headers=headers).status_code == 200
    assert client.post(path + "/chat/stream", headers=headers, json={"question": "hello"}).status_code == 200


def test_chat_stream_reports_error_after_preserving_partial_answer(store, monkeypatch):
    task = make_task(store, succeeded=True)

    def interrupted(*args, **kwargs):
        yield "partial answer"
        raise WorkflowError("provider stopped")

    monkeypatch.setattr(main.MockLlmAnalyzer, "chat_stream", interrupted)
    response = TestClient(main.app).post(
        f"/tasks/{task.task_id}/chat/stream",
        headers={"X-Client-Id": "owner"}, json={"question": "hello"},
    )
    events = [(event, json.loads(raw)) for event, raw in iter_events(response.text.splitlines())]
    assert [event for event, _ in events] == ["delta", "error"]
    assert events[0][1]["text"] == "partial answer"
    assert "provider stopped" in events[1][1]["detail"]


def test_workflow_publishes_transcript_before_llm_and_preserves_partial_failure(monkeypatch, tmp_path):
    settings = Settings(mock_mode=True, tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results",
                        media_dir=tmp_path / "media", db_path=tmp_path / "tasks.db")
    monkeypatch.setattr(workflow, "settings", settings)
    store = SQLiteTaskStore(settings.db_path)
    task = make_task(store)

    class Analyzer:
        def analyze_stream(self, *args, **kwargs):
            live = store.get(task.task_id)
            assert live.status == TaskStatus.running
            assert live.result.transcript
            assert live.result.output == ""
            yield "partial summary"
            raise WorkflowError("provider stopped")

    monkeypatch.setattr(workflow, "_select_llm", lambda *args: Analyzer())
    workflow.run_analysis_task(task.task_id, task.request, store)
    failed = store.get(task.task_id)
    assert failed.status == TaskStatus.failed
    assert failed.result.transcript
    assert failed.result.output == "partial summary"
    assert failed.error == "provider stopped"


def test_upload_workflow_also_publishes_transcript_before_llm(monkeypatch, tmp_path):
    settings = Settings(mock_mode=True, tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results",
                        media_dir=tmp_path / "media", db_path=tmp_path / "tasks.db")
    monkeypatch.setattr(workflow, "settings", settings)
    store = SQLiteTaskStore(settings.db_path)
    task = make_task(store)
    media = tmp_path / "sample.mp3"
    media.write_bytes(b"test")

    class Analyzer:
        def analyze_stream(self, *args, **kwargs):
            partial = store.get(task.task_id)
            assert partial.status == TaskStatus.running
            assert partial.result.transcript
            assert partial.result.output == ""
            yield "complete summary"

    monkeypatch.setattr(workflow, "_select_llm", lambda *args: Analyzer())
    workflow.run_file_analysis_task(task.task_id, task.request, media, store)
    completed = store.get(task.task_id)
    assert completed.status == TaskStatus.succeeded
    assert completed.result.output == "complete summary"
