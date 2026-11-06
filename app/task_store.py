from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.config import settings
from app.schemas import AnalysisResult, AnalyzeRequest, TaskRecord, TaskStatus, TaskSummary

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json  TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    client_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""

# 老库可能没有 client_id 列：启动时执行一次轻量迁移
_MIGRATE_CLIENT_ID = "ALTER TABLE tasks ADD COLUMN client_id TEXT"
# client_id 索引在迁移完成后创建（避免老库缺列时建索引失败）
_INDEX_CLIENT_ID = "CREATE INDEX IF NOT EXISTS idx_tasks_client ON tasks(client_id)"
_INDEX_CLIENT_HISTORY = "CREATE INDEX IF NOT EXISTS idx_tasks_client_history ON tasks(client_id, created_at)"


class SQLiteTaskStore:
    """SQLite 持久化任务存储，进程重启后任务不丢失。

    与旧 InMemoryTaskStore 保持相同接口（create/get/set_*），
    并新增 list_tasks / delete 以支持历史记录与一键删除。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._migrate_if_needed(conn)
                conn.commit()
            finally:
                conn.close()

    def _migrate_if_needed(self, conn: sqlite3.Connection) -> None:
        """老库补 client_id 列（幂等，仅当列不存在时执行），随后建索引。"""
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "client_id" not in columns:
            conn.execute(_MIGRATE_CLIENT_ID)
        conn.execute(_INDEX_CLIENT_ID)
        conn.execute(_INDEX_CLIENT_HISTORY)

    def create(self, request: AnalyzeRequest) -> TaskRecord:
        now = datetime.now()
        task = TaskRecord(
            task_id=uuid4().hex,
            status=TaskStatus.queued,
            request=request,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO tasks (task_id, status, request_json, result_json, error, created_at, updated_at, client_id)"
                    " VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)",
                    (
                        task.task_id,
                        task.status.value,
                        task.request.model_dump_json(),
                        task.created_at.isoformat(timespec="seconds"),
                        task.updated_at.isoformat(timespec="seconds"),
                        task.request.client_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return task

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            finally:
                conn.close()
        return self._row_to_task(row) if row else None

    def list_tasks(
        self,
        limit: int = 50,
        status: TaskStatus | None = None,
        client_id: str | None = None,
    ) -> list[TaskRecord]:
        """列出任务，按 client_id **严格**归属过滤。

        无身份（client_id=None）只能看到同样无身份的任务；带身份时只能看到
        自己的任务。两条边都必须严格匹配，否则会同时出现两个不一致的后果：
        「提交时忘带身份 → 任务创建成功但永远列不出来」，以及「无归属任务
        被任意身份读取/删除」。SQLite 里无归属存为 NULL，不能用 `= ?` 比较。
        """
        return [self._row_to_task(row) for row in self._list_rows("*", limit, status, client_id)]

    def list_summaries(
        self,
        limit: int = 50,
        status: TaskStatus | None = None,
        client_id: str | None = None,
    ) -> list[TaskSummary]:
        columns = (
            "task_id, status, request_json, error, created_at, updated_at, "
            "json_extract(result_json, '$.title') AS title, "
            "json_extract(result_json, '$.media.title') AS media_title"
        )
        return [
            TaskSummary(
                task_id=row["task_id"],
                status=row["status"],
                request=AnalyzeRequest.model_validate_json(row["request_json"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                title=row["title"] or "",
                media_title=row["media_title"],
                error=row["error"],
            )
            for row in self._list_rows(columns, limit, status, client_id)
        ]

    def _list_rows(
        self, columns: str, limit: int, status: TaskStatus | None, client_id: str | None,
    ) -> list[sqlite3.Row]:
        if client_id is None:
            sql = f"SELECT {columns} FROM tasks WHERE client_id IS NULL"
            params: list[object] = []
        else:
            sql = f"SELECT {columns} FROM tasks WHERE client_id = ?"
            params = [client_id]

        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)

        # created_at 只精确到秒，同秒创建的任务会并列；
        # 用 rowid DESC 兜底，保证历史列表顺序稳定（后创建的排前面）。
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return rows

    def set_partial_result(self, task_id: str, result: AnalysisResult) -> None:
        self._update(task_id, result=result)

    def set_running(self, task_id: str) -> None:
        self._update(task_id, status=TaskStatus.running, error=None)

    def set_succeeded(self, task_id: str, result: AnalysisResult) -> None:
        self._update(task_id, status=TaskStatus.succeeded, result=result, error=None)

    def set_failed(self, task_id: str, error: str) -> None:
        self._update(task_id, status=TaskStatus.failed, error=error)

    def delete(self, task_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def all_task_ids(self) -> set[str]:
        """库里全部 task_id，供产物回收判断哪些目录已经成了孤儿。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT task_id FROM tasks").fetchall()
            finally:
                conn.close()
        return {row["task_id"] for row in rows}

    def task_ids_with_status(self, *statuses: TaskStatus) -> set[str]:
        """指定状态的任务 id 集合（回收中间产物时用来排除仍在跑的任务）。"""
        if not statuses:
            return set()
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT task_id FROM tasks WHERE status IN ({placeholders})",
                    [status.value for status in statuses],
                ).fetchall()
            finally:
                conn.close()
        return {row["task_id"] for row in rows}

    def delete_created_before(self, cutoff: datetime) -> list[str]:
        """删除创建时间早于 cutoff 的任务记录，返回被删的 task_id。

        刻意分成「先查再删」两步：调用方拿到 id 才能一并清掉磁盘上的产物，
        否则记录没了、目录还在，反而变成回收器要处理的孤儿。
        created_at 存的是 isoformat(timespec="seconds")，文本比较对 ISO 时间安全。
        """
        stamp = cutoff.isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT task_id FROM tasks WHERE created_at < ?", (stamp,)
                ).fetchall()
                task_ids = [row["task_id"] for row in rows]
                if task_ids:
                    conn.executemany(
                        "DELETE FROM tasks WHERE task_id = ?", [(task_id,) for task_id in task_ids]
                    )
                    conn.commit()
            finally:
                conn.close()
        return task_ids

    def _update(self, task_id: str, **updates: object) -> None:
        fields: dict[str, object] = {}
        if "status" in updates:
            fields["status"] = updates["status"].value  # type: ignore[attr-defined]
        if "error" in updates:
            fields["error"] = updates["error"]  # type: ignore[assignment]
        if "result" in updates:
            fields["result_json"] = updates["result"].model_dump_json()  # type: ignore[attr-defined]
        fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE tasks SET {assignments} WHERE task_id = ?", values)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRecord:
        request = AnalyzeRequest.model_validate_json(row["request_json"])
        result = None
        if row["result_json"]:
            result = AnalysisResult.model_validate_json(row["result_json"])
        return TaskRecord(
            task_id=row["task_id"],
            status=TaskStatus(row["status"]),
            request=request,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            result=result,
            error=row["error"],
        )


task_store = SQLiteTaskStore(settings.db_path)
