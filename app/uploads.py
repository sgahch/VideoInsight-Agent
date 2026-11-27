"""Durable, bounded uploads. A SQLite transaction serializes each upload independently."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from uuid import uuid4
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.config import Settings
from app.schemas import AnalysisMode, AnalyzeRequest, TaskRecord
from app.task_queue import TaskQueueFull
from app.task_store import SQLiteTaskStore

LOGGER = logging.getLogger(__name__)
UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UploadInit(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class UploadOptions(BaseModel):
    asr_provider: Literal["groq", "siliconflow", "cloudflare", "faster_whisper"] | None = None
    asr_require_timestamps: bool = True
    mode: AnalysisMode = AnalysisMode.summary
    use_mock: bool | None = None
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_protocol: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)


def _safe_filename(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:180]
    if not name:
        name = "uploaded-media"
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0]):
        name = "_" + name
    return name


class UploadStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # The existing task cleaner deliberately skips underscore-prefixed directories.
        self.root = settings.tmp_dir / "_uploads"
        self.chunk_size = max(1, min(settings.upload_chunk_bytes, 16 * 1024 * 1024))
        self.ttl = max(1, settings.upload_retention_hours) * 3600

    def _directory(self, upload_id: str) -> Path:
        if not UPLOAD_ID.fullmatch(upload_id):
            raise HTTPException(404, "上传会话不存在。")
        directory = self.root / upload_id
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise HTTPException(404, "上传会话不存在。")
        return directory

    @contextmanager
    def _connect(self, directory: Path) -> Iterator[sqlite3.Connection]:
        db = directory / "session.sqlite3"
        if not db.is_file():
            raise HTTPException(404, "上传会话不存在。")
        try:
            connection = sqlite3.connect(db.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
        except sqlite3.OperationalError as exc:
            raise HTTPException(404, "上传会话不存在。") from exc
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _locked(self, upload_id: str, owner: str | None) -> Iterator[tuple[sqlite3.Connection, sqlite3.Row, Path]]:
        directory = self._directory(upload_id)
        with self._connect(directory) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM upload").fetchone()
                if row is None or row["owner"] != (owner or ""):
                    raise HTTPException(404, "上传会话不存在。")
                if row["state"] == "cancelled" or row["updated_at"] + self.ttl <= time.time():
                    raise HTTPException(410, "上传已取消或过期，请重新上传。")
                yield connection, row, directory
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _view(self, connection: sqlite3.Connection, row: sqlite3.Row, directory: Path) -> dict[str, object]:
        chunks = connection.execute("SELECT * FROM chunks ORDER BY idx").fetchall()
        if row["state"] != "completed":
            for chunk in chunks:
                path = directory / f"{chunk['idx']}.part"
                if not path.is_file() or path.stat().st_size != chunk["size"]:
                    connection.execute("DELETE FROM chunks WHERE idx = ?", (chunk["idx"],))
            chunks = connection.execute("SELECT * FROM chunks ORDER BY idx").fetchall()
        return {
            "upload_id": directory.name, "filename": row["filename"], "size": row["size"],
            "sha256": row["sha256"], "chunk_size": row["chunk_size"], "total_chunks": row["total_chunks"],
            "uploaded_chunks": [chunk["idx"] for chunk in chunks],
            "uploaded_bytes": sum(chunk["size"] for chunk in chunks),
            "state": row["state"], "task_id": row["task_id"], "expires_at": row["updated_at"] + self.ttl,
        }

    def create(self, metadata: UploadInit, owner: str | None) -> dict[str, object]:
        maximum = self.settings.max_upload_mb * 1024 * 1024
        if metadata.size > maximum:
            raise HTTPException(413, f"文件超过 {self.settings.max_upload_mb} MB 上限。")
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup_expired()
        # This short catalog transaction only serializes quota checks and new sessions.
        with closing(sqlite3.connect(self.root / "catalog.sqlite3", timeout=30)) as catalog, catalog:
            catalog.execute("BEGIN IMMEDIATE")
            upload_id = (
                hashlib.sha256(f"{owner or ''}\0{metadata.idempotency_key}".encode()).hexdigest()[:32]
                if metadata.idempotency_key else uuid4().hex
            )
            directory = self._directory(upload_id)
            if (directory / "session.sqlite3").is_file():
                existing = self.status(upload_id, owner)
                if existing["size"] != metadata.size or existing["sha256"] != metadata.sha256 or existing["filename"] != _safe_filename(metadata.filename):
                    raise HTTPException(409, "上传初始化标识已用于其他文件。")
                return existing
            pending, reserved = 0, 0
            for directory in self.root.iterdir():
                if not UPLOAD_ID.fullmatch(directory.name) or directory.is_symlink():
                    continue
                try:
                    with self._connect(directory) as connection:
                        row = connection.execute("SELECT * FROM upload").fetchone()
                except (HTTPException, sqlite3.Error):
                    # Cancellation or an interrupted initialization cannot block other uploads.
                    continue
                if row and row["owner"] == (owner or "") and row["state"] == "uploading" and row["updated_at"] + self.ttl > time.time():
                    pending += 1
                    reserved += row["size"]
            if pending >= max(1, self.settings.max_pending_uploads) or reserved + metadata.size > maximum * 2:
                raise HTTPException(429, "未完成上传已达到配额，请继续或取消已有上传。")
            directory = self._directory(upload_id)
            directory.mkdir()
            try:
                with closing(sqlite3.connect(directory / "session.sqlite3")) as connection, connection:
                    connection.executescript(
                        "CREATE TABLE upload (owner TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL, "
                        "sha256 TEXT NOT NULL, chunk_size INTEGER NOT NULL, total_chunks INTEGER NOT NULL, "
                        "updated_at REAL NOT NULL, state TEXT NOT NULL, task_id TEXT);"
                        "CREATE TABLE chunks (idx INTEGER PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL);"
                    )
                    connection.execute("INSERT INTO upload VALUES (?, ?, ?, ?, ?, ?, ?, 'uploading', NULL)", (
                        owner or "", _safe_filename(metadata.filename), metadata.size, metadata.sha256,
                        self.chunk_size, (metadata.size + self.chunk_size - 1) // self.chunk_size, time.time(),
                    ))
            except BaseException:
                self._remove(directory)
                raise
        return self.status(upload_id, owner)

    def status(self, upload_id: str, owner: str | None) -> dict[str, object]:
        with self._locked(upload_id, owner) as (connection, row, directory):
            return self._view(connection, row, directory)

    def write_chunk(self, upload_id: str, owner: str | None, index: int, data: bytes, digest: str) -> dict[str, object]:
        with self._locked(upload_id, owner) as (connection, row, directory):
            if row["state"] != "uploading":
                raise HTTPException(409, "上传已完成，不能再写入分片。")
            if not 0 <= index < row["total_chunks"]:
                raise HTTPException(422, "分片序号无效。")
            expected = min(row["chunk_size"], row["size"] - index * row["chunk_size"])
            if len(data) != expected:
                raise HTTPException(422, "分片长度不正确。")
            if not SHA256.fullmatch(digest) or hashlib.sha256(data).hexdigest() != digest:
                raise HTTPException(422, "分片 SHA-256 校验失败。")
            existing = connection.execute("SELECT sha256 FROM chunks WHERE idx = ?", (index,)).fetchone()
            if existing and existing["sha256"] != digest:
                raise HTTPException(409, "相同序号的分片内容不一致，请取消后重新上传。")
            temporary = directory / f"{index}.{uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, directory / f"{index}.part")
            finally:
                temporary.unlink(missing_ok=True)
            connection.execute("INSERT OR REPLACE INTO chunks VALUES (?, ?, ?)", (index, len(data), digest))
            connection.execute("UPDATE upload SET updated_at = ?", (time.time(),))
            row = connection.execute("SELECT * FROM upload").fetchone()
            return self._view(connection, row, directory)

    def complete(
        self, upload_id: str, owner: str | None, options: UploadOptions, store: SQLiteTaskStore,
        enqueue: Callable[[TaskRecord, Path], None],
    ) -> TaskRecord:
        with self._locked(upload_id, owner) as (connection, row, directory):
            # The deterministic task ID survives a crash between the task commit and session commit.
            existing = store.get(upload_id)
            if existing is not None:
                connection.execute("UPDATE upload SET state = 'completed', task_id = ?, updated_at = ?", (upload_id, time.time()))
                return existing
            if row["state"] == "completed":
                raise HTTPException(410, "该上传对应的分析任务已删除，请重新发起上传。")
            view = self._view(connection, row, directory)
            if len(view["uploaded_chunks"]) != row["total_chunks"]:
                raise HTTPException(409, "文件尚未传完，请继续上传缺失分片。")
            target_dir = self.settings.media_dir / upload_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / row["filename"]
            # data/tmp and media are separate Docker mounts in the supported deployment.
            # Assemble next to the final file so publication never crosses a filesystem.
            temporary = target_dir / ".assembled-upload.tmp"
            try:
                full_hash = hashlib.sha256()
                written = 0
                with temporary.open("wb") as output:
                    for chunk in connection.execute("SELECT * FROM chunks ORDER BY idx"):
                        chunk_hash = hashlib.sha256()
                        with (directory / f"{chunk['idx']}.part").open("rb") as source:
                            while block := source.read(1024 * 1024):
                                output.write(block)
                                full_hash.update(block)
                                chunk_hash.update(block)
                                written += len(block)
                        if chunk_hash.hexdigest() != chunk["sha256"]:
                            raise HTTPException(422, "分片内容损坏，请取消后重新上传。")
                    output.flush()
                    os.fsync(output.fileno())
                if written != row["size"] or full_hash.hexdigest() != row["sha256"]:
                    raise HTTPException(422, "完整文件 SHA-256 校验失败，请取消后重新上传。")
                os.replace(temporary, target)
                request = AnalyzeRequest(
                    url=f"upload://{row['filename']}", label=row["filename"], client_id=owner,
                    **options.model_dump(),
                )
                from app.adapters.multi_asr import pin_provider
                request = pin_provider(request, self.settings)
                task = store.create(request, task_id=upload_id)
                try:
                    enqueue(task, target)
                except TaskQueueFull:
                    store.delete(upload_id)
                    target.unlink(missing_ok=True)
                    with suppress(OSError):
                        target_dir.rmdir()
                    raise
                connection.execute("UPDATE upload SET state = 'completed', task_id = ?, updated_at = ?", (task.task_id, time.time()))
            finally:
                temporary.unlink(missing_ok=True)
                with suppress(OSError):
                    target_dir.rmdir()
        # Commit the session before removing chunks so a crash can always resume.
        for part in directory.glob("*.part"):
            with suppress(OSError):
                part.unlink()
        return task

    def cancel(self, upload_id: str, owner: str | None) -> None:
        with self._locked(upload_id, owner) as (connection, row, directory):
            if row["state"] == "completed":
                raise HTTPException(409, "分析任务已提交，请在任务列表中管理。")
            connection.execute("UPDATE upload SET state = 'cancelled'")
        self._remove(directory)

    def _remove(self, directory: Path) -> None:
        # Check the final absolute path before recursive deletion, including on Windows.
        checked = self._directory(directory.name)
        if checked.resolve().parent != self.root.resolve():
            raise ValueError("Upload directory escaped its root")
        shutil.rmtree(checked)

    def cleanup_expired(self) -> int:
        if not self.root.is_dir():
            return 0
        removed = 0
        for directory in list(self.root.iterdir()):
            if not UPLOAD_ID.fullmatch(directory.name) or not directory.is_dir() or directory.is_symlink():
                continue
            try:
                with self._connect(directory) as connection:
                    row = connection.execute("SELECT * FROM upload").fetchone()
                    if row and row["state"] != "cancelled" and row["updated_at"] + self.ttl > time.time():
                        continue
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute("SELECT * FROM upload").fetchone()
                    expired = row is None or row["state"] == "cancelled" or row["updated_at"] + self.ttl <= time.time()
                    if expired:
                        connection.execute("UPDATE upload SET state = 'cancelled'")
                    connection.commit()
                if expired:
                    self._remove(directory)
                    removed += 1
            except (OSError, sqlite3.Error, HTTPException) as exc:
                # A crash before the upload table exists leaves no timestamp in SQLite.
                # Only reclaim such initialization remnants after the same retention window.
                invalid_session = (
                    isinstance(exc, HTTPException) and exc.status_code == 404
                    or isinstance(exc, sqlite3.Error) and any(
                        message in str(exc).lower() for message in ("no such table", "not a database", "malformed")
                    )
                )
                if invalid_session and directory.is_dir() and directory.stat().st_mtime + self.ttl <= time.time():
                    try:
                        self._remove(directory)
                        removed += 1
                        continue
                    except OSError:
                        pass
                LOGGER.warning("上传分片清理已跳过 %s: %s", directory.name, exc)
        return removed
