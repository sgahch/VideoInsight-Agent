from __future__ import annotations

import sqlite3
from datetime import datetime
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


def test_list_tasks_scopes_to_exact_client_id(tmp_path: Path) -> None:
    """任务按 client_id **严格**归属过滤。

    他人身份看不到自己的任务；无归属任务只属于"无身份"这个命名空间，
    不会被带身份的调用方看到（反之亦然）。
    """
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    owned = store.create(_request("https://example.com/a", client_id="c1"))
    anonymous = store.create(_request("https://example.com/b"))

    # 他人身份：看不到 c1 的任务
    assert store.list_tasks(client_id="c2") == []
    assert [t.task_id for t in store.list_tasks(client_id="c1")] == [owned.task_id]

    # 无身份：只看得到无归属任务。此前这里直接返回 []，导致不带身份提交的
    # 任务创建成功却永远列不出来（数据黑洞）。
    assert [t.task_id for t in store.list_tasks()] == [anonymous.task_id]
    # 反过来，带身份也看不到无归属任务
    assert anonymous.task_id not in {t.task_id for t in store.list_tasks(client_id="c1")}


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


def test_delete_expired_tasks_keeps_terminal_task_at_cutoff(tmp_path: Path) -> None:
    db = tmp_path / "tasks.db"
    store = SQLiteTaskStore(db)
    task = store.create(_request())
    store.set_failed(task.task_id, "failed")
    cutoff = datetime(2025, 1, 2, 12)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET created_at = ?, updated_at = ? WHERE task_id = ?",
            ("2025-01-01T12:00:00", cutoff.isoformat(), task.task_id),
        )

    assert store.delete_terminal_before(cutoff) == []
    assert store.get(task.task_id) is not None


def test_mark_used_does_not_extend_task_retention(tmp_path: Path) -> None:
    db = tmp_path / "tasks.db"
    store = SQLiteTaskStore(db)
    task = store.create(_request(client_id="owner"))
    store.set_failed(task.task_id, "failed")
    completed_at = datetime(2025, 1, 1, 12)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
            (completed_at.isoformat(), task.task_id),
        )

    assert store.mark_used(task.task_id, "owner") is not None
    assert store.get(task.task_id).updated_at == completed_at
    assert store.delete_terminal_before(datetime(2025, 1, 2, 12)) == [task.task_id]


def test_delete_expired_tasks_returns_only_deleted_ids(tmp_path: Path) -> None:
    db = tmp_path / "tasks.db"
    store = SQLiteTaskStore(db)
    kept = store.create(_request())
    deleted = store.create(_request())
    store.set_failed(kept.task_id, "protected")
    store.set_failed(deleted.task_id, "failed")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET created_at = ?, updated_at = ?",
            ("2025-01-01T12:00:00", "2025-01-01T12:00:00"),
        )
        conn.execute(
            "CREATE TRIGGER keep_protected_task BEFORE DELETE ON tasks "
            "WHEN OLD.error = 'protected' BEGIN SELECT RAISE(IGNORE); END"
        )

    assert store.delete_terminal_before(datetime(2025, 1, 2, 12)) == [deleted.task_id]
    assert store.get(kept.task_id) is not None
    assert store.get(deleted.task_id) is None
