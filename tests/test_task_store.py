from __future__ import annotations

from pathlib import Path

from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo, TaskStatus
from app.task_store import SQLiteTaskStore


def _request(url: str = "https://example.com/video", client_id: str | None = None) -> AnalyzeRequest:
    return AnalyzeRequest(url=url, mode=AnalysisMode.summary, use_mock=True, client_id=client_id)


def test_create_and_get(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    task = store.create(_request())

    got = store.get(task.task_id)

    assert got is not None
    assert got.task_id == task.task_id
    assert got.status == TaskStatus.queued
    assert got.request.url == "https://example.com/video"


def test_list_order_and_status_filter(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    first = store.create(_request("https://example.com/a", client_id="c1"))
    second = store.create(_request("https://example.com/b", client_id="c1"))

    assert [t.task_id for t in store.list_tasks(client_id="c1")] == [second.task_id, first.task_id]
    assert [t.task_id for t in store.list_tasks(limit=1, client_id="c1")] == [second.task_id]
    assert store.list_tasks(status=TaskStatus.failed, client_id="c1") == []


def test_list_tasks_requires_client_id(tmp_path: Path) -> None:
    """任务按 client_id 隔离：未带身份返回空，他人身份看不到。"""
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    task = store.create(_request("https://example.com/a", client_id="c1"))

    assert store.list_tasks() == []
    assert store.list_tasks(client_id="c2") == []
    assert [t.task_id for t in store.list_tasks(client_id="c1")] == [task.task_id]


def test_succeeded_roundtrip(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    task = store.create(_request())
    result = AnalysisResult(
        mode=AnalysisMode.summary,
        transcript="hello",
        segments=[],
        output="summary text",
        media=MediaInfo(source_url=task.request.url),
    )

    store.set_succeeded(task.task_id, result)
    got = store.get(task.task_id)

    assert got is not None
    assert got.status == TaskStatus.succeeded
    assert got.error is None
    assert got.result is not None
    assert got.result.output == "summary text"
    assert got.result.media.source_url == "https://example.com/video"


def test_failed_error_message(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    task = store.create(_request())

    store.set_failed(task.task_id, "boom")
    got = store.get(task.task_id)

    assert got is not None
    assert got.status == TaskStatus.failed
    assert got.error == "boom"
    assert got.result is None


def test_persistence_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "tasks.db"
    store1 = SQLiteTaskStore(db)
    task = store1.create(_request())
    store1.set_running(task.task_id)

    store2 = SQLiteTaskStore(db)
    got = store2.get(task.task_id)

    assert got is not None
    assert got.status == TaskStatus.running


def test_delete(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    task = store.create(_request())

    assert store.delete(task.task_id) is True
    assert store.delete(task.task_id) is False
    assert store.get(task.task_id) is None
