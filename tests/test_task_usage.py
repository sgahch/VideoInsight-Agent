from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import app.main as main
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo, TaskStatus
from app.task_store import SQLiteTaskStore


def test_used_marker_survives_updates_and_restart(tmp_path):
    database = tmp_path / "tasks.db"
    store = SQLiteTaskStore(database)
    task = store.create(AnalyzeRequest(url="https://example.com/video", client_id="owner"))
    assert task.used_at is None
    assert store.get(task.task_id).used_at is None
    used_at = store.mark_used(task.task_id, "owner")
    assert used_at is not None
    assert store.mark_used(task.task_id, "owner") == used_at
    store.set_running(task.task_id)
    store.set_partial_result(task.task_id, AnalysisResult(
        mode=AnalysisMode.summary, transcript="First transcript", segments=[], output="",
        media=MediaInfo(source_url=task.request.url),
    ))
    store.set_failed(task.task_id, "Retry later")
    reopened = SQLiteTaskStore(database)
    assert reopened.get(task.task_id).used_at == used_at
    assert reopened.get(task.task_id).status == TaskStatus.failed
    assert reopened.list_summaries(client_id="owner")[0].used_at == used_at
    assert reopened.list_tasks(client_id="owner")[0].used_at == used_at


def test_used_marker_is_atomic_and_keeps_first_timestamp(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    task = store.create(AnalyzeRequest(url="https://example.com/video"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        stamps = list(pool.map(lambda _: store.mark_used(task.task_id, None), range(16)))
    assert stamps[0] is not None
    assert len(set(stamps)) == 1
    assert store.get(task.task_id).used_at == stamps[0]


def test_used_marker_migrates_old_database_without_losing_tasks(tmp_path):
    database = tmp_path / "tasks.db"
    request = AnalyzeRequest(url="https://example.com/old", client_id="owner")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, request_json TEXT NOT NULL, "
            "result_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, client_id TEXT)"
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)",
            ("old-task", "succeeded", request.model_dump_json(), "2026-11-06T10:00:00", "2026-11-06T10:01:00", "owner"),
        )
    store = SQLiteTaskStore(database)
    assert store.get("old-task").used_at is None
    assert store.get("old-task").request.url == request.url
    assert store.mark_used("old-task", "owner") is not None
    assert SQLiteTaskStore(database).get("old-task").used_at is not None


def test_usage_api_is_scoped_persistent_and_in_all_task_views(monkeypatch, tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main, "task_store", store)
    task = store.create(AnalyzeRequest(url="https://example.com/owned", client_id="owner"))
    anonymous = store.create(AnalyzeRequest(url="https://example.com/anonymous"))
    client = TestClient(main.app)
    headers = {"X-Client-Id": "owner"}

    for other in ({}, {"X-Client-Id": "other"}):
        assert client.post(f"/tasks/{task.task_id}/used", headers=other).status_code == 404
    assert client.post(f"/tasks/{anonymous.task_id}/used", headers=headers).status_code == 404
    assert client.post("/tasks/missing/used", headers=headers).status_code == 404
    assert store.get(task.task_id).used_at is None

    response = client.post(f"/tasks/{task.task_id}/used", headers=headers)
    assert response.status_code == 200
    stamp = response.json()["used_at"]
    assert response.json()["task_id"] == task.task_id
    assert client.post(f"/tasks/{task.task_id}/used", headers=headers).json()["used_at"] == stamp
    assert client.get(f"/tasks/{task.task_id}", headers=headers).json()["used_at"] == stamp
    assert client.get("/tasks", headers=headers).json()[0]["used_at"] == stamp
    assert client.get("/tasks?view=summary", headers=headers).json()[0]["used_at"] == stamp
    assert client.get("/queue", headers=headers).json()["tasks"][0]["used_at"] == stamp
    assert client.post(f"/tasks/{anonymous.task_id}/used").status_code == 200
    assert store.get(anonymous.task_id).used_at is not None

    store.delete(task.task_id)
    assert client.post(f"/tasks/{task.task_id}/used", headers=headers).status_code == 404
