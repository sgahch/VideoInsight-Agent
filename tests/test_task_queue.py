from __future__ import annotations

import threading
import time
from pathlib import Path

from app.config import Settings
from app.schemas import AnalysisMode, AnalyzeRequest
from app.task_queue import (
    INTERRUPTED_MESSAGE,
    MISSING_UPLOAD_MESSAGE,
    TaskQueue,
    reconcile_interrupted_tasks,
)
from app.task_store import SQLiteTaskStore


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        mock_mode=True,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        db_path=tmp_path / "tasks.db",
    )
    settings.ensure_dirs()
    return settings


def _request(url: str = "https://example.com/v", client_id: str | None = None) -> AnalyzeRequest:
    return AnalyzeRequest(url=url, mode=AnalysisMode.summary, use_mock=True, client_id=client_id)


def test_queue_bounds_concurrency(tmp_path: Path) -> None:
    """回归：并发必须有上限。

    用 BackgroundTasks 时提交 N 个任务就是 N 路同时下载 + 转写，会打满带宽、
    磁盘并撞 ASR 配额。这里断言池子最多同时跑 max_workers 个。
    """
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    queue = TaskQueue(2)
    lock = threading.Lock()
    stats = {"current": 0, "peak": 0, "done": 0}

    def work() -> None:
        with lock:
            stats["current"] += 1
            stats["peak"] = max(stats["peak"], stats["current"])
        time.sleep(0.08)
        with lock:
            stats["current"] -= 1
            stats["done"] += 1

    for index in range(6):
        task = store.create(_request(f"https://example.com/{index}"))
        queue.submit(task.task_id, store, work)

    deadline = time.monotonic() + 15
    while stats["done"] < 6 and time.monotonic() < deadline:
        time.sleep(0.02)

    assert stats["done"] == 6
    assert stats["peak"] == 2
    queue.shutdown()


def test_queue_marks_failed_with_type_name_for_systemexit(tmp_path: Path) -> None:
    """回归（真实事故）：任务曾以晦涩的 "任务执行异常：1" 失败 ——
    str(SystemExit(1)) 就是 "1"，说明有非 Exception 击穿了 worker 边界。

    现在职责分工是：
    - run_analysis_task 内的 except BaseException 接住大多数（转成可读错误）；
    - 即使仍有漏网，_on_done 兜底也会带上异常类型名再落库，而不是让用户看到裸数字。
    这个环境确有 unlink 被解释器拦成 SystemExit 的先例（downloader/asr 的
    _best_effort_unlink 注释里写明），所以必须用 BaseException 兜住。
    """
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    queue = TaskQueue(1)

    def boom() -> None:
        raise SystemExit(1)

    task = store.create(_request())
    queue.submit(task.task_id, store, boom)

    # 同一个队列、同一个工作线程：SystemExit 之后池子必须还能干活。
    # 这里刻意复用 queue（而不是另建一个），否则证明不了线程存活。
    task2 = store.create(_request())
    state = {"alive": False}
    queue.submit(task2.task_id, store, lambda: state.update(alive=True))

    queue.shutdown(wait=True)

    got = store.get(task.task_id)
    assert got is not None
    assert got.status.value == "failed"
    assert "任务执行异常（SystemExit）：1" in (got.error or "")
    assert state["alive"] is True


def test_queue_marks_task_failed_when_closed(tmp_path: Path) -> None:
    """关闭后提交的任务要显式失败，而不是静静躺在 queued 里。"""
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    queue = TaskQueue(1)
    queue.shutdown()

    task = store.create(_request())
    queue.submit(task.task_id, store, lambda: None)

    got = store.get(task.task_id)
    assert got is not None
    assert got.status.value == "failed"
    assert "关闭" in (got.error or "")


def test_reconcile_fails_running_tasks(tmp_path: Path) -> None:
    """进程被杀后 running 任务是僵尸：链路没有断点续传，只能明确失败。"""
    settings = _settings(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(_request())
    store.set_running(task.task_id)

    report = reconcile_interrupted_tasks(store, settings)

    assert report.marked_failed == 1
    assert report.pending == []
    got = store.get(task.task_id)
    assert got is not None
    assert got.status.value == "failed"
    assert got.error == INTERRUPTED_MESSAGE


def test_reconcile_requeues_queued_link_task(tmp_path: Path) -> None:
    """queued 任务从未开始，提交意图明确 → 重新入队。"""
    settings = _settings(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(_request("https://example.com/link"))

    report = reconcile_interrupted_tasks(store, settings)

    assert report.marked_failed == 0
    assert len(report.pending) == 1
    item = report.pending[0]
    assert item.task_id == task.task_id
    assert item.media_path is None
    # 保持 queued，交给队列去跑
    got = store.get(task.task_id)
    assert got is not None and got.status.value == "queued"


def test_reconcile_requeues_upload_task_when_file_survives(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(_request("upload://meeting.mp4"))
    directory = settings.media_dir / task.task_id
    directory.mkdir(parents=True)
    uploaded = directory / "meeting.mp4"
    uploaded.write_bytes(b"video")

    report = reconcile_interrupted_tasks(store, settings)

    assert len(report.pending) == 1
    assert report.pending[0].media_path == uploaded


def test_reconcile_fails_upload_task_when_file_is_gone(tmp_path: Path) -> None:
    """原始文件没了就没法重跑，明确失败并说明原因。"""
    settings = _settings(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(_request("upload://gone.mp4"))

    report = reconcile_interrupted_tasks(store, settings)

    assert report.pending == []
    assert report.marked_failed == 1
    got = store.get(task.task_id)
    assert got is not None
    assert got.status.value == "failed"
    assert got.error == MISSING_UPLOAD_MESSAGE


def test_reconcile_leaves_terminal_tasks_untouched(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    succeeded = store.create(_request("https://example.com/ok"))
    failed = store.create(_request("https://example.com/bad"))
    store.set_failed(succeeded.task_id, "boom")
    store.set_failed(failed.task_id, "boom")

    report = reconcile_interrupted_tasks(store, settings)

    assert report.pending == []
    assert report.marked_failed == 0
