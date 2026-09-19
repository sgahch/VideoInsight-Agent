"""长任务的执行载体：有界线程池 + 重启对账。

**为什么要换掉 FastAPI 的 `BackgroundTasks`**

1. 它把同步任务丢进 anyio 的默认线程池，而**所有同步端点共用同一个池**
   （默认 40 个工人）。一个视频任务动辄十几分钟，几个并发任务就能把池子占满，
   连 `GET /tasks` 都不再响应。
2. 没有并发上限：N 次提交 = N 路同时下载 + 转写，打满带宽、磁盘和 ASR 配额。
3. 进程重启后，库里停在 `queued` / `running` 的任务永远没人管 —— 既不会跑完，
   也不会失败，只会作为僵尸记录一直躺在历史列表里。

这里用一个**专用**的有界 `ThreadPoolExecutor` 解决前两条（业务是 IO 与子进程
密集型，GIL 不是瓶颈），再用 `reconcile_interrupted_tasks` 解决第三条。
"""
from __future__ import annotations

import logging
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.schemas import AnalyzeRequest, TaskStatus
from app.task_store import SQLiteTaskStore

LOGGER = logging.getLogger(__name__)

UPLOAD_URL_PREFIX = "upload://"
INTERRUPTED_MESSAGE = "服务重启导致任务中断，请重新提交。"
MISSING_UPLOAD_MESSAGE = "服务重启后未找到已上传的文件，请重新上传。"


class TaskQueue:
    """把长任务提交到专用的有界线程池执行。"""

    def __init__(self, max_workers: int, name: str = "videoinsight-task") -> None:
        self.max_workers = max(1, max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix=name
        )
        self._closed = False

    def submit(
        self,
        task_id: str,
        store: SQLiteTaskStore,
        target: Callable[..., None],
        *args: object,
    ) -> None:
        """提交任务；入队失败时直接把任务标记为失败，不让它悬着。

        store 按次传入而不是构造时持有：队列是进程级单例，而 store 在测试里
        会被替换。持有旧引用会让"入队失败"的兜底写到错误的库上，任务就此停在
        queued 再也不动 —— 这正是这个参数存在的原因。
        """
        if self._closed:
            store.set_failed(task_id, "服务正在关闭，任务未执行。")
            return
        try:
            future = self._executor.submit(target, *args)
        except RuntimeError as exc:
            store.set_failed(task_id, f"任务入队失败：{exc}")
            return
        future.add_done_callback(lambda done: self._on_done(store, task_id, done))

    def _on_done(self, store: SQLiteTaskStore, task_id: str, future: Future) -> None:
        """兜底：执行体自己会捕获业务异常，这里只接住它没接住的那些。

        没有这层的话，`set_running` 之类的早期失败会让任务永远停在 queued，
        而 Future 里的异常没有人读，会被静默吞掉。
        """
        exc = future.exception()
        if exc is None:
            return
        LOGGER.error("任务 %s 的执行体抛出未捕获异常：%s", task_id, exc, exc_info=exc)
        try:
            store.set_failed(
                task_id,
                f"任务执行异常（{type(exc).__name__}）：{exc} · "
                + " | ".join(traceback.format_exception_only(type(exc), exc)).strip()[:300],
            )
        except Exception:  # noqa: BLE001 - 已经是最后一道兜底
            LOGGER.exception("标记任务 %s 失败时再次失败", task_id)

    def shutdown(self, wait: bool = False) -> None:
        """关闭池子。未开始执行的排队任务会被取消并留在 queued 状态，
        下次启动时由 reconcile_interrupted_tasks 重新入队。"""
        self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)


@dataclass
class PendingTask:
    """重启后需要重新入队的任务。media_path 非空表示这是上传型任务。"""

    task_id: str
    request: AnalyzeRequest
    media_path: Path | None = None


@dataclass
class ReconciliationReport:
    pending: list[PendingTask] = field(default_factory=list)
    marked_failed: int = 0


def _recover_uploaded_file(settings: Settings, task_id: str) -> Path | None:
    """找回上传型任务的原始文件（正常情况下该目录下就一个文件）。"""
    directory = settings.media_dir / task_id
    if not directory.is_dir():
        return None
    candidates = sorted(path for path in directory.iterdir() if path.is_file())
    return candidates[0] if candidates else None


def reconcile_interrupted_tasks(
    store: SQLiteTaskStore,
    settings: Settings,
) -> ReconciliationReport:
    """进程重启后对账，把僵尸任务收敛掉。

    两类处理刻意不同：

    - `running`：进程死了，任务必然被中断，而链路**没有断点续传** —— 磁盘上
      的半成品不可信。标记失败、让用户明确重试，比伪装成"还在跑"诚实。
    - `queued`：从未开始，没有部分状态，提交意图明确 → 重新入队。上传型任务
      需要原始文件还在，否则只能失败。
    """
    report = ReconciliationReport()

    for task_id in store.task_ids_with_status(TaskStatus.running):
        store.set_failed(task_id, INTERRUPTED_MESSAGE)
        report.marked_failed += 1

    for task_id in store.task_ids_with_status(TaskStatus.queued):
        task = store.get(task_id)
        if task is None:
            continue
        if str(task.request.url).startswith(UPLOAD_URL_PREFIX):
            media_path = _recover_uploaded_file(settings, task_id)
            if media_path is None:
                store.set_failed(task_id, MISSING_UPLOAD_MESSAGE)
                report.marked_failed += 1
                continue
            report.pending.append(PendingTask(task_id, task.request, media_path))
        else:
            report.pending.append(PendingTask(task_id, task.request))

    if report.pending or report.marked_failed:
        LOGGER.info(
            "重启对账：重新入队 %s 条，标记失败 %s 条",
            len(report.pending),
            report.marked_failed,
        )
    return report
