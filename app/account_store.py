from __future__ import annotations

import hmac
import os
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
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
CREATE TABLE IF NOT EXISTS auth_admin_credentials (
    username TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    seed_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_action_codes (
    purpose TEXT NOT NULL,
    email TEXT NOT NULL,
    digest TEXT NOT NULL,
    generation TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (purpose, email)
);
CREATE TABLE IF NOT EXISTS auth_session_revocations (
    session_hash TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_session_revocations_expiry
ON auth_session_revocations(expires_at);
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

    def admin(self, username: str, seed_hash: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_admin_credentials WHERE username = ? AND seed_hash = ?",
                (username, seed_hash),
            ).fetchone()
        return dict(row) if row else None

    def session_revoked(self, session_hash: str, now: int) -> bool:
        with self._connection() as connection:
            return bool(connection.execute(
                "SELECT 1 FROM auth_session_revocations WHERE session_hash=? AND expires_at>?",
                (session_hash, now),
            ).fetchone())

    def revoke_session(
        self, session_hash: str, expires_at: int, now: int, *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        with self._connection() if connection is None else nullcontext(connection) as db:
            db.execute("DELETE FROM auth_session_revocations WHERE expires_at<=?", (now,))
            db.execute(
                "INSERT INTO auth_session_revocations VALUES (?, ?) ON CONFLICT(session_hash) DO NOTHING",
                (session_hash, expires_at),
            )

    @staticmethod
    def _email_used(connection, email: str) -> bool:
        return bool(
            connection.execute("SELECT 1 FROM auth_users WHERE email = ?", (email,)).fetchone()
            or connection.execute("SELECT 1 FROM auth_admin_credentials WHERE email = ?", (email,)).fetchone()
        )

    @staticmethod
    def _mail_limits(connection, buckets: list[tuple[str, int, int]], now: int) -> None:
        connection.execute("DELETE FROM auth_mail_limits WHERE expires_at <= ?", (now,))
        for bucket, maximum, seconds in buckets:
            row = connection.execute("SELECT count, expires_at FROM auth_mail_limits WHERE bucket = ?", (bucket,)).fetchone()
            if row and row["count"] >= maximum:
                raise MailRateLimited(row["expires_at"] - now)
            connection.execute(
                "INSERT INTO auth_mail_limits VALUES (?, ?, 1) ON CONFLICT(bucket) DO UPDATE SET count = count + 1",
                (bucket, now + seconds),
            )

    def reserve_code(self, email: str, digest: str, generation: str, buckets: list[tuple[str, int, int]], now: int) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM auth_email_codes WHERE sent_at < ?", (now - CODE_SECONDS,))
            previous = connection.execute("SELECT sent_at FROM auth_email_codes WHERE email = ?", (email,)).fetchone()
            if previous and now - previous["sent_at"] < RESEND_SECONDS:
                raise MailRateLimited(RESEND_SECONDS - (now - previous["sent_at"]))
            self._mail_limits(connection, buckets, now)
            connection.execute(
                """INSERT INTO auth_email_codes VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(email) DO UPDATE SET digest=excluded.digest, generation=excluded.generation,
                expires_at=excluded.expires_at, sent_at=excluded.sent_at, attempts=0""",
                (email, digest, generation, now + CODE_SECONDS, now),
            )
            return not self._email_used(connection, email)

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
            if self._email_used(connection, email):
                return None
            user = {"user_id": uuid.uuid4().hex, "email": email, "password_hash": password_hasher(password), "created_at": now}
            connection.execute(
                "INSERT INTO auth_users VALUES (:user_id, :email, :password_hash, :created_at)", user,
            )
            connection.execute("DELETE FROM auth_email_codes WHERE email = ?", (email,))
            return user

    def reserve_action_code(
        self, purpose: str, email: str, digest: str, generation: str,
        password_hash: str, buckets: list[tuple[str, int, int]], now: int,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM auth_action_codes WHERE sent_at < ?", (now - CODE_SECONDS,))
            previous = connection.execute(
                "SELECT sent_at FROM auth_action_codes WHERE purpose = ? AND email = ?", (purpose, email),
            ).fetchone()
            if previous and now - previous["sent_at"] < RESEND_SECONDS:
                raise MailRateLimited(RESEND_SECONDS - (now - previous["sent_at"]))
            self._mail_limits(connection, buckets, now)
            connection.execute(
                """INSERT INTO auth_action_codes VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(purpose, email) DO UPDATE SET digest=excluded.digest,
                generation=excluded.generation, password_hash=excluded.password_hash,
                expires_at=excluded.expires_at, sent_at=excluded.sent_at, attempts=0""",
                (purpose, email, digest, generation, password_hash, now + CODE_SECONDS, now),
            )

    def invalidate_action_code(self, purpose: str, email: str, generation: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE auth_action_codes SET digest='', expires_at=0 WHERE purpose=? AND email=? AND generation=?",
                (purpose, email, generation),
            )

    @staticmethod
    def _valid_action_code(connection, purpose: str, email: str, digest: str, version: str, now: int) -> bool:
        row = connection.execute(
            "SELECT * FROM auth_action_codes WHERE purpose=? AND email=?", (purpose, email),
        ).fetchone()
        if not row or row["expires_at"] <= now or row["attempts"] >= MAX_CODE_ATTEMPTS:
            return False
        if not hmac.compare_digest(row["password_hash"], version):
            return False
        if not hmac.compare_digest(row["digest"], digest):
            connection.execute(
                "UPDATE auth_action_codes SET attempts=attempts+1 WHERE purpose=? AND email=?", (purpose, email),
            )
            return False
        return True

    def update_password(
        self, purpose: str, email: str, digest: str, new_hash: str, now: int,
        admin_username: str | None = None, seed_hash: str | None = None,
    ) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if admin_username is None:
                row = connection.execute("SELECT * FROM auth_users WHERE email=?", (email,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM auth_admin_credentials WHERE username=? AND email=? AND seed_hash=?",
                    (admin_username, email, seed_hash),
                ).fetchone()
            if not row or not self._valid_action_code(connection, purpose, email, digest, row["password_hash"], now):
                return False
            if admin_username is None:
                connection.execute("UPDATE auth_users SET password_hash=? WHERE user_id=?", (new_hash, row["user_id"]))
            else:
                connection.execute(
                    "UPDATE auth_admin_credentials SET password_hash=? WHERE username=?", (new_hash, admin_username),
                )
            connection.execute("DELETE FROM auth_action_codes WHERE email=?", (email,))
            return True

    def bind_admin_email(self, username: str, seed_hash: str, email: str, digest: str, now: int) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            bound = connection.execute(
                "SELECT 1 FROM auth_admin_credentials WHERE username=? AND seed_hash=?", (username, seed_hash),
            ).fetchone()
            occupied = (
                connection.execute("SELECT 1 FROM auth_users WHERE email=?", (email,)).fetchone()
                or connection.execute(
                    "SELECT 1 FROM auth_admin_credentials WHERE email=? AND username<>?", (email, username),
                ).fetchone()
            )
            if bound or occupied:
                return False
            if not self._valid_action_code(connection, "bind", email, digest, seed_hash, now):
                return False
            connection.execute(
                """INSERT INTO auth_admin_credentials VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET email=excluded.email,
                password_hash=excluded.password_hash, seed_hash=excluded.seed_hash""",
                (username, email, seed_hash, seed_hash),
            )
            connection.execute("DELETE FROM auth_action_codes WHERE purpose='bind' AND email=?", (email,))
            connection.execute("DELETE FROM auth_email_codes WHERE email=?", (email,))
            return True
