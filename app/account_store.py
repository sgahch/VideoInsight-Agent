from __future__ import annotations

import hmac
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_email_codes (
    email TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    generation TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_mail_limits (
    bucket TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    count INTEGER NOT NULL
);
"""
CODE_SECONDS = 600
RESEND_SECONDS = 60
MAX_CODE_ATTEMPTS = 5


class MailRateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)


class SQLiteAccountStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SCHEMA)
        if os.name != "nt":
            for private_path in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                if private_path.is_file():
                    private_path.chmod(0o600)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def by_email(self, email: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM auth_users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def by_id(self, user_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM auth_users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def reserve_code(self, email: str, digest: str, generation: str, buckets: list[tuple[str, int, int]], now: int) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM auth_email_codes WHERE sent_at < ?", (now - CODE_SECONDS,))
            connection.execute("DELETE FROM auth_mail_limits WHERE expires_at <= ?", (now,))
            previous = connection.execute("SELECT sent_at FROM auth_email_codes WHERE email = ?", (email,)).fetchone()
            if previous and now - previous["sent_at"] < RESEND_SECONDS:
                raise MailRateLimited(RESEND_SECONDS - (now - previous["sent_at"]))
            for bucket, maximum, seconds in buckets:
                row = connection.execute("SELECT count, expires_at FROM auth_mail_limits WHERE bucket = ?", (bucket,)).fetchone()
                if row and row["count"] >= maximum:
                    raise MailRateLimited(row["expires_at"] - now)
                connection.execute(
                    "INSERT INTO auth_mail_limits VALUES (?, ?, 1) ON CONFLICT(bucket) DO UPDATE SET count = count + 1",
                    (bucket, now + seconds),
                )
            connection.execute(
                """INSERT INTO auth_email_codes VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(email) DO UPDATE SET digest=excluded.digest, generation=excluded.generation,
                expires_at=excluded.expires_at, sent_at=excluded.sent_at, attempts=0""",
                (email, digest, generation, now + CODE_SECONDS, now),
            )
            return connection.execute("SELECT 1 FROM auth_users WHERE email = ?", (email,)).fetchone() is None

    def invalidate_code(self, email: str, generation: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE auth_email_codes SET digest='', expires_at=0 WHERE email=? AND generation=?",
                (email, generation),
            )

    def register(self, email: str, digest: str, password: str, password_hasher: Callable[[str], str], now: int) -> dict | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM auth_email_codes WHERE email = ?", (email,)).fetchone()
            if not row or row["expires_at"] <= now or row["attempts"] >= MAX_CODE_ATTEMPTS:
                return None
            if not hmac.compare_digest(row["digest"], digest):
                connection.execute("UPDATE auth_email_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
                return None
            if connection.execute("SELECT 1 FROM auth_users WHERE email = ?", (email,)).fetchone():
                return None
            user = {"user_id": uuid.uuid4().hex, "email": email, "password_hash": password_hasher(password), "created_at": now}
            connection.execute(
                "INSERT INTO auth_users VALUES (:user_id, :email, :password_hash, :created_at)", user,
            )
            connection.execute("DELETE FROM auth_email_codes WHERE email = ?", (email,))
            return user
