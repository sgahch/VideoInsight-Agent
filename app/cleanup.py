"""产物回收：把 data/tmp 与 media 下的任务目录收敛回数据库的真实状态。

设计前提：**数据库是唯一事实来源**。每个任务的产物放在以它 task_id 命名的
目录里，所以「目录存在、库里没有对应记录」= 纯垃圾，可以无条件删除。这条
规则同时覆盖了三种历史包袱：删除任务时清理失败留下的残渣、进程被中断的
半成品、以及开发脚本手工造出来的目录。

三条硬性安全边界：
1. 只处理名字**严格等于** 32 位小写十六进制 task_id 的目录 —— 开发脚本留下的
   `realtest/`、`probe_task/` 之类不会被误删。
2. 下划线开头的条目永不触碰 —— `media/_cookies`（抖音登录态）与
   `media/_browser_profiles`（专用浏览器配置，数 GB）是用户资产，不是产物。
3. 只回收「最后修改时间早于 min_age_seconds」的目录，避免和正在创建目录、
   尚未落库的任务抢跑。
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.schemas import TaskStatus
from app.task_store import SQLiteTaskStore

LOGGER = logging.getLogger(__name__)

# 任务目录名就是 uuid4().hex。
TASK_DIR_RE = re.compile(r"^[0-9a-f]{32}$")
# 孤儿目录的静默期：比这个更"新"的目录本轮不动，留给下一轮。
ORPHAN_MIN_AGE_SECONDS = 3600


@dataclass
class CleanupReport:
    dirs_removed: int = 0
    files_removed: int = 0
    bytes_reclaimed: int = 0
    tasks_removed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def megabytes_reclaimed(self) -> float:
        return round(self.bytes_reclaimed / 1048576, 2)


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            # 文件可能刚好被另一个进程删掉；体积统计不值得让回收失败。
            continue
    return total


def _iter_task_dirs(root: Path) -> list[Path]:
    """列出 root 下形如 task_id 的目录。下划线开头的持久化资产直接跳过。"""
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in root.iterdir():
        if child.name.startswith("_") or not child.is_dir():
            continue
        if TASK_DIR_RE.fullmatch(child.name):
            found.append(child)
    return found


def remove_task_artifacts(settings: Settings, task_id: str) -> int:
    """删除单个任务的全部磁盘产物，返回回收的字节数。

    绝不抛异常：调用方（删除接口、保留期回收）都只关心"尽力清干净"。
    """
    reclaimed = 0
    for directory in (settings.tmp_dir / task_id, settings.media_dir / task_id):
        if not directory.is_dir():
            continue
        reclaimed += _dir_size(directory)
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except OSError as exc:  # ignore_errors 已兜底，这里只是留痕
            LOGGER.warning("任务目录删除失败：%s（%s）", directory, exc)

    result_file = settings.results_dir / f"{task_id}.json"
    try:
        if result_file.is_file():
            reclaimed += result_file.stat().st_size
            result_file.unlink()
    except OSError as exc:
        LOGGER.warning("结果文件删除失败：%s（%s）", result_file, exc)
    return reclaimed


def prune_orphan_task_dirs(
    store: SQLiteTaskStore,
    settings: Settings,
    *,
    min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS,
) -> CleanupReport:
    """回收库里已无记录的任务目录。"""
    report = CleanupReport()
    live_ids = store.all_task_ids()
    cutoff = time.time() - max(0, min_age_seconds)

    for root in (settings.tmp_dir, settings.media_dir):
        for directory in _iter_task_dirs(root):
            if directory.name in live_ids:
                continue
            try:
                if directory.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            size = _dir_size(directory)
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                report.errors.append(f"{directory}: {exc}")
                continue
            report.dirs_removed += 1
            report.bytes_reclaimed += size
            LOGGER.info("回收孤儿任务目录 %s（%.1f MB）", directory, size / 1048576)
    return report


def prune_stale_intermediates(store: SQLiteTaskStore, settings: Settings) -> CleanupReport:
    """删除**已终态**任务里残留的转写中间 WAV。

    这里回收的是最大的一块死重量：一小时音频的 16kHz 单声道 WAV 约 115 MB，
    而它只是 ASR 的内部输入 —— 转写完成后没有任何代码再读它
    （`/tasks/{id}/media` 提供的是 media.audio_path，即下载/上传的原始文件）。
    实测存量里它占了存活任务产物的 55%。

    只处理 succeeded/failed 的任务：queued/running 的任务正需要这些文件。
    文件名严格按 task_id 前缀匹配，避免误删用户上传的、恰好叫 `*-16k.wav`
    的源文件。
    """
    report = CleanupReport()
    terminal = store.task_ids_with_status(TaskStatus.succeeded, TaskStatus.failed)

    for root in (settings.tmp_dir, settings.media_dir):
        for directory in _iter_task_dirs(root):
            task_id = directory.name
            if task_id not in terminal:
                continue
            for pattern in (f"{task_id}-16k.wav", f"{task_id}-asr-*.wav"):
                for leftover in directory.glob(pattern):
                    try:
                        size = leftover.stat().st_size
                        leftover.unlink()
                    except OSError as exc:
                        report.errors.append(f"{leftover}: {exc}")
                        continue
                    report.files_removed += 1
                    report.bytes_reclaimed += size
    return report


def prune_expired_tasks(
    store: SQLiteTaskStore,
    settings: Settings,
    *,
    retention_hours: int,
) -> CleanupReport:
    """淘汰超过保留期的任务记录与产物。retention_hours<=0 表示未启用。"""
    report = CleanupReport()
    if retention_hours <= 0:
        return report

    cutoff = datetime.now() - timedelta(hours=retention_hours)
    for task_id in store.delete_created_before(cutoff):
        report.tasks_removed += 1
        report.bytes_reclaimed += remove_task_artifacts(settings, task_id)
    if report.tasks_removed:
        LOGGER.info(
            "按保留期淘汰了 %s 条任务记录（%s 小时），回收 %.1f MB",
            report.tasks_removed,
            retention_hours,
            report.megabytes_reclaimed,
        )
    return report


def run_cleanup(
    store: SQLiteTaskStore,
    settings: Settings,
    *,
    retention_hours: int | None = None,
    min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS,
) -> CleanupReport:
    """一次完整回收：孤儿目录 + 终态任务的中间产物 + 过期任务。"""
    orphans = prune_orphan_task_dirs(store, settings, min_age_seconds=min_age_seconds)
    intermediates = prune_stale_intermediates(store, settings)
    expired = prune_expired_tasks(
        store,
        settings,
        retention_hours=settings.task_retention_hours if retention_hours is None else retention_hours,
    )
    return CleanupReport(
        dirs_removed=orphans.dirs_removed,
        files_removed=intermediates.files_removed,
        bytes_reclaimed=(
            orphans.bytes_reclaimed + intermediates.bytes_reclaimed + expired.bytes_reclaimed
        ),
        tasks_removed=expired.tasks_removed,
        errors=orphans.errors + intermediates.errors + expired.errors,
    )
