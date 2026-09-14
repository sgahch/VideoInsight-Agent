from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.config import settings
from app.schemas import AnalysisResult, AnalyzeRequest, TaskRecord, TaskStatus

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
        """列出任务。

        仅返回指定 client_id 创建的任务；client_id 为 None（未提供身份）时
        返回空列表，避免无归属的老数据或他人任务被任何客户端看到。
        """
        if client_id is None:
            return []

        sql = "SELECT * FROM tasks WHERE client_id = ?"
        params: list[object] = [client_id]

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
        return [self._row_to_task(row) for row in rows]

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
