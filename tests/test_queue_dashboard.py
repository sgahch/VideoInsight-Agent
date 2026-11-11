from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.adapters.asr import ParaformerTranscriber
from app.config import Settings
from app.schemas import AnalyzeRequest
from app.task_queue import TaskQueue, TaskQueueFull, TaskRunning, reconcile_interrupted_tasks
from app.task_store import SQLiteTaskStore


def test_core_queue_overflow_and_rejection_are_bounded(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    queue = TaskQueue(2, core_workers=1, queue_capacity=1)
    release = threading.Event()
    tasks = [store.create(AnalyzeRequest(url=f"https://example.com/{i}")) for i in range(4)]
    executed = []

    def work(task_id):
        executed.append(task_id)
        release.wait(5)

    try:
        queue.submit(tasks[0].task_id, store, work, tasks[0].task_id)
        assert queue.snapshot()["active_workers"] == 1
        queue.submit(tasks[1].task_id, store, work, tasks[1].task_id)
        assert queue.snapshot()["queued_task_ids"] == [tasks[1].task_id]
        queue.submit(tasks[2].task_id, store, work, tasks[2].task_id)
        snapshot = queue.snapshot()
        assert snapshot["active_workers"] == 2
        assert snapshot["queued_task_ids"] == [tasks[2].task_id]
        with pytest.raises(TaskQueueFull):
            queue.submit(tasks[3].task_id, store, work, tasks[3].task_id)
        with pytest.raises(TaskRunning):
            queue.cancel_pending(tasks[0].task_id)
        assert queue.snapshot()["rejected"] == 1
        assert queue.cancel_pending(tasks[2].task_id)
    finally:
        release.set()
        queue.shutdown(wait=True)
    assert tasks[2].task_id not in executed
    assert queue.snapshot()["active_workers"] == 0


def test_waiting_tasks_drain_in_fifo_order(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    queue = TaskQueue(1, queue_capacity=5)
    release = threading.Event()
    order = []
    try:
        for index in range(4):
            task = store.create(AnalyzeRequest(url=f"https://example.com/{index}"))

            def work(value=index):
                release.wait(5)
                order.append(value)

            queue.submit(task.task_id, store, work)
        release.set()
        queue.shutdown(wait=True)
    finally:
        release.set()
    assert order == [0, 1, 2, 3]
    assert queue.snapshot()["completed"] == 4


def test_shutdown_retains_pending_database_records_for_recovery(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    queue = TaskQueue(1)
    release = threading.Event()
    first = store.create(AnalyzeRequest(url="https://example.com/first"))
    second = store.create(AnalyzeRequest(url="https://example.com/second"))
    queue.submit(first.task_id, store, lambda: release.wait(5))
    queue.submit(second.task_id, store, lambda: pytest.fail("Cancelled task ran"))
    queue.shutdown(wait=False)
    release.set()
    queue.shutdown(wait=True)
    assert store.get(second.task_id).status.value == "queued"


def test_recovery_keeps_submission_order_for_same_second_tasks(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    tasks = [store.create(AnalyzeRequest(url=f"https://example.com/{i}")) for i in range(8)]
    report = reconcile_interrupted_tasks(store, Settings())
    assert [item.task_id for item in report.pending] == [task.task_id for task in tasks]


def test_deleted_deferred_recovery_task_cannot_be_resubmitted(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    task = store.create(AnalyzeRequest(url="https://example.com/deferred"))
    queue = TaskQueue(1)
    queue.cancel_pending(task.task_id, store)
    queue.submit(task.task_id, store, lambda: pytest.fail("Deleted task was resurrected"))
    queue.shutdown(wait=True)
    assert queue.snapshot()["accepted"] == 0
    assert store.get(task.task_id) is None


def test_queue_api_is_scoped_and_full_submissions_leave_no_orphans(monkeypatch, tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    queue = TaskQueue(2, core_workers=1, queue_capacity=1)
    release = threading.Event()
    monkeypatch.setattr(main, "task_store", store)
    monkeypatch.setattr(main, "task_queue", queue)

    def work(task_id, request, task_store):
        task_store.set_running(task_id)
        release.wait(5)
        task_store.set_failed(task_id, "test completed")

    monkeypatch.setattr(main, "run_analysis_task", work)
    client = TestClient(main.app)
    headers = {"X-Client-Id": "queue-owner"}
    payload = {
        "url": "https://example.com/video", "use_mock": True,
        "label": "Owner-only title", "llm_api_key": "private-runtime-key",
    }
    try:
        ids = []
        for _ in range(3):
            response = client.post("/analyze", json=payload, headers=headers)
            assert response.status_code == 202, response.text
            ids.append(response.json()["task_id"])
        rejected = client.post("/analyze", json=payload, headers=headers)
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "3"
        assert len(store.list_tasks(client_id="queue-owner")) == 3
        overview = client.get("/queue", headers=headers).json()
        assert overview["pool"]["active_workers"] == 2
        assert overview["pool"]["queued"] == 1
        assert "active_task_ids" not in overview["pool"]
        assert "queued_task_ids" not in overview["pool"]
        assert overview["engine"]["local_gpu_inference"] is False
        assert len(overview["tasks"]) == 3
        assert "private-runtime-key" not in str(overview)
        other = client.get("/queue", headers={"X-Client-Id": "someone-else"}).json()
        assert other["tasks"] == []
        assert sum(other["counts"].values()) == 0
        assert "Owner-only title" not in str(other)
        assert client.delete(f"/tasks/{ids[0]}", headers=headers).status_code == 409
        assert client.delete(f"/tasks/{ids[2]}", headers=headers).status_code == 204
        assert queue.snapshot()["queued"] == 0
        assert client.get(f"/tasks/{ids[0]}", headers={"X-Client-Id": "someone-else"}).status_code == 404
        assert client.get(f"/view/{ids[0]}").status_code == 200
    finally:
        release.set()
        queue.shutdown(wait=True)


def test_upload_rejection_cleans_up_files(monkeypatch, tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    settings = Settings(media_dir=tmp_path / "media", tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results")
    settings.ensure_dirs()
    queue = TaskQueue(1, queue_capacity=1)
    release = threading.Event()
    monkeypatch.setattr(main, "task_store", store)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "task_queue", queue)
    for index in range(2):
        task = store.create(AnalyzeRequest(url=f"https://example.com/{index}"))
        queue.submit(task.task_id, store, lambda: release.wait(5))
    try:
        response = TestClient(main.app).post(
            "/analyze-file", files={"file": ("sample.wav", b"data", "audio/wav")},
            data={"use_mock": "true"},
        )
        assert response.status_code == 429
        assert len(store.list_tasks()) == 2
        assert list(settings.media_dir.iterdir()) == []
    finally:
        release.set()
        queue.shutdown(wait=True)


def test_asr_limit_is_shared_across_video_instances(tmp_path):
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class Recognition:
        status_code = 200

        def __init__(self, **kwargs):
            pass

        def call(self, path):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.03)
            with lock:
                state["active"] -= 1
            return self

        def get_sentence(self):
            return [{"begin_time": 0, "end_time": 1000, "text": "sample"}]

    settings = Settings(dashscope_asr_global_workers=2)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(ParaformerTranscriber(settings)._recognize_file, tmp_path / f"{i}.wav", Recognition)
            for i in range(6)
        ]
        assert all(future.result() for future in futures)
    assert state["peak"] == 2
