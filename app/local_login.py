"""Single-use, account-bound handoff to the optional Windows login helper."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.account_auth import (
    Account, _admin_password_hash, _check_origin, account_store, session_account,
    task_account_scope,
)
from app.adapters.downloader import cookie_storage_path
from app.config import Settings, settings
from app.cookies import is_douyin_domain, save_douyin_login_cookies, summarize_cookie_file


router = APIRouter()
TTL = 600
MAX_BODY = 256 * 1024
HELPER_NAME = "VideoInsightLoginHelper.exe"
SCHEMA = """
CREATE TABLE IF NOT EXISTS cookie_helper_sessions (
    session_id TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    owner TEXT,
    username TEXT NOT NULL,
    password_stamp TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    upload_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cookie_helper_identity
ON cookie_helper_sessions(identity, created_at);
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _identity(account: Account) -> str:
    return account.owner or "admin:" + account.username


def _password_stamp(identity: str) -> str | None:
    if identity.startswith("admin:"):
        return _digest(_admin_password_hash()) if identity == "admin:" + settings.auth_username else None
    user = account_store().by_id(identity.removeprefix("account:"))
    return _digest(user["password_hash"]) if user else None


def _account(request: Request) -> Account:
    account = session_account(request)
    if not account:
        raise HTTPException(401, "请先登录网站账号，再使用本地助手。")
    return account


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def _origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _secure_origin(origin: str) -> bool:
    url = urlsplit(origin)
    return url.scheme == "https" or (
        url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    )


def helper_binary(config: Settings = settings) -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "downloads" / HELPER_NAME


def capabilities(request: Request, config: Settings = settings) -> dict:
    origin = _origin(request)
    secure = _secure_origin(origin)
    available = helper_binary(config).is_file()
    account = session_account(request)
    return {
        "available": secure and available and account is not None,
        "secure": secure,
        "platform": "windows",
        "download_url": "/cookies/helper/download" if available else None,
        "secure_url": origin if secure else "https://" + request.url.netloc + "/#cookies",
        "username": account.username if account else None,
    }


class HelperStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def owned(db, session_id: str, identity: str):
        row = db.execute(
            "SELECT * FROM cookie_helper_sessions WHERE session_id=? AND identity=?",
            (session_id, identity),
        ).fetchone()
        if not row:
            raise HTTPException(404, "本地授权会话不存在。")
        return row

    @staticmethod
    def active(row) -> None:
        if row["expires_at"] <= int(time.time()):
            raise HTTPException(410, "授权已过期，请回到网站重新开始。")
        if row["status"] not in {"pending", "claimed"}:
            raise HTTPException(409, "本次授权已结束，请回到网站重新开始。")
        stamp = _password_stamp(row["identity"])
        if not stamp or not hmac.compare_digest(row["password_stamp"], stamp):
            raise HTTPException(410, "账号已变更，请重新登录网站后授权。")

    @staticmethod
    def authorized(db, token: str, *, upload: bool):
        column = "upload_hash" if upload else "token_hash"
        row = db.execute(
            f"SELECT * FROM cookie_helper_sessions WHERE {column}=?", (_digest(token),),
        ).fetchone()
        if not row:
            raise HTTPException(401, "无效的本地授权凭证。")
        HelperStore.active(row)
        expected = "claimed" if upload else "pending"
        if row["status"] != expected:
            raise HTTPException(409, "授权凭证已使用，请重新开始。")
        return row

    @contextmanager
    def invalidate(self, identity: str):
        # Keep this transaction open through clearing Cookies. Completion takes
        # the same SQLite write lock, so an old helper cannot restore a logout.
        with self.connection() as db:
            db.execute(
                "UPDATE cookie_helper_sessions SET status='cancelled', token_hash='', upload_hash='' "
                "WHERE identity=? AND status IN ('pending', 'claimed', 'confirmed')", (identity,),
            )
            yield db


@lru_cache(maxsize=8)
def _store(path: Path) -> HelperStore:
    return HelperStore(path)


def store() -> HelperStore:
    account_store()
    return _store(settings.db_path)


@contextmanager
def cancel_for_account(request: Request, config: Settings):
    account = session_account(request)
    if account:
        with _store(config.db_path).invalidate(_identity(account)) as db:
            yield db
    else:
        yield None


@router.get("/cookies/helper/download", include_in_schema=False)
def download(request: Request):
    _account(request)
    binary = helper_binary(settings)
    if not binary.is_file():
        raise HTTPException(503, "本地助手安装包暂未就绪，请稍后重试。")
    return FileResponse(
        binary, filename=HELPER_NAME, media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/cookies/helper/start")
def start(request: Request, response: Response):
    _check_origin(request)
    account = _account(request)
    _no_store(response)
    origin = _origin(request)
    if not _secure_origin(origin):
        raise HTTPException(409, "请先通过 HTTPS 访问本站，再打开本地助手。")
    identity = _identity(account)
    stamp = _password_stamp(identity)
    if not stamp:
        raise HTTPException(401, "账号已变更，请重新登录。")
    now = int(time.time())
    session_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
    with store().connection() as db:
        # Logout revokes the website session under this same write lock.
        # A start that waited for logout must not create a new handoff afterward.
        _account(request)
        db.execute("DELETE FROM cookie_helper_sessions WHERE created_at < ?", (now - 86400,))
        recent = db.execute(
            "SELECT count(*) FROM cookie_helper_sessions WHERE identity=? AND created_at>?",
            (identity, now - 300),
        ).fetchone()[0]
        total = db.execute(
            "SELECT count(*) FROM cookie_helper_sessions WHERE created_at>?", (now - TTL,),
        ).fetchone()[0]
        if recent >= 8 or total >= 500:
            raise HTTPException(429, "打开过于频繁，请稍后重试。", headers={"Retry-After": "60"})
        db.execute(
            "UPDATE cookie_helper_sessions SET status='cancelled', token_hash='', upload_hash='' "
            "WHERE identity=? AND status IN ('pending', 'claimed')", (identity,),
        )
        db.execute(
            "INSERT INTO cookie_helper_sessions "
            "(session_id,identity,owner,username,password_stamp,token_hash,status,origin,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?,'pending',?,?,?)",
            (session_id, identity, account.owner, account.username, stamp, _digest(token), origin, now, now + TTL),
        )
    return {
        "session_id": session_id, "status": "pending", "expires_in": TTL,
        "launch_url": "videoinsight-login://authorize?" + urlencode({"server": origin, "token": token}),
        "username": account.username,
    }


@router.get("/cookies/helper/{session_id}/status")
def status(session_id: str, request: Request, response: Response):
    account = _account(request)
    _no_store(response)
    with store().connection() as db:
        row = store().owned(db, session_id, _identity(account))
        value = row["status"]
        if value in {"pending", "claimed"} and row["expires_at"] <= int(time.time()):
            value = "expired"
    return {
        "session_id": session_id, "status": value, "completed": value == "confirmed",
        "expires_in": max(0, row["expires_at"] - int(time.time())),
    }


@router.delete("/cookies/helper/{session_id}")
def cancel(session_id: str, request: Request, response: Response):
    _check_origin(request)
    account = _account(request)
    _no_store(response)
    with store().connection() as db:
        row = store().owned(db, session_id, _identity(account))
        if row["status"] in {"pending", "claimed"}:
            db.execute(
                "UPDATE cookie_helper_sessions SET status='cancelled',token_hash='',upload_hash='' WHERE session_id=?",
                (session_id,),
            )
    return {"cancelled": True}


def _bearer(request: Request) -> str:
    if not _secure_origin(_origin(request)):
        raise HTTPException(400, "本地助手回传必须使用 HTTPS。")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not 32 <= len(token) <= 128:
        raise HTTPException(401, "缺少本地授权凭证。")
    # Helper callbacks never authenticate using the website session cookie.
    return token


@router.post("/cookies/helper/claim")
def claim(request: Request, response: Response):
    _no_store(response)
    token, upload_token = _bearer(request), secrets.token_urlsafe(32)
    with store().connection() as db:
        row = store().authorized(db, token, upload=False)
        db.execute(
            "UPDATE cookie_helper_sessions SET status='claimed',token_hash='',upload_hash=? WHERE session_id=?",
            (_digest(upload_token), row["session_id"]),
        )
    return {
        "session_id": row["session_id"], "username": row["username"],
        "server": row["origin"], "upload_token": upload_token,
        "expires_in": max(0, row["expires_at"] - int(time.time())),
    }


@router.post("/cookies/helper/progress")
def progress(request: Request, response: Response):
    _no_store(response)
    with store().connection() as db:
        row = store().authorized(db, _bearer(request), upload=True)
    return {"status": row["status"], "expires_in": max(0, row["expires_at"] - int(time.time()))}


class CookieItem(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=16384)
    domain: str = Field(min_length=1, max_length=254)
    path: str = Field(default="/", min_length=1, max_length=2048)
    secure: bool = False
    httpOnly: bool = False
    expires: float = Field(default=-1, ge=-1, le=253402300799)


class CookiePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookies: list[CookieItem] = Field(min_length=1, max_length=500)


async def _cookies(request: Request) -> list[dict]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "授权内容过大。")
    try:
        payload = CookiePayload.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise HTTPException(400, "授权内容格式无效。") from None
    now = time.time()
    items = []
    for item in payload.cookies:
        if not is_douyin_domain(item.domain):
            continue
        if any(c in value for value in (item.name, item.value, item.domain, item.path) for c in "\r\n\t\0"):
            raise HTTPException(400, "授权内容包含非法字符。")
        if item.expires > 0 and item.expires <= now:
            continue
        items.append(item.model_dump())
    if not any(item["name"] in {"sessionid", "sid_tt", "sid_guard"} for item in items):
        raise HTTPException(400, "尚未检测到有效的抖音登录态，请完成登录后重试。")
    return items


@router.post("/cookies/helper/complete")
async def complete(request: Request, response: Response):
    _no_store(response)
    token = _bearer(request)
    cookies = await _cookies(request)
    return await run_in_threadpool(_complete, token, cookies)


def _complete(token: str, cookies: list[dict]) -> dict:
    with store().connection() as db:
        previous = db.execute(
            "SELECT * FROM cookie_helper_sessions WHERE upload_hash=?", (_digest(token),),
        ).fetchone()
        if previous and previous["status"] == "confirmed" and previous["expires_at"] > time.time():
            stamp = _password_stamp(previous["identity"])
            if not stamp or not hmac.compare_digest(previous["password_stamp"], stamp):
                raise HTTPException(410, "账号已变更，请重新授权。")
            return {"saved": True, "username": previous["username"], "already_saved": True}
        row = store().authorized(db, token, upload=True)
        try:
            with task_account_scope(row["owner"]):
                target = cookie_storage_path(settings)
                save_douyin_login_cookies(target, cookies)
                count = summarize_cookie_file(target)["count"]
        except (OSError, ValueError):
            raise HTTPException(500, "保存授权失败，请稍后重试。") from None
        db.execute(
            "UPDATE cookie_helper_sessions SET status='confirmed' WHERE session_id=?",
            (row["session_id"],),
        )
    return {"saved": True, "count": count, "username": row["username"]}


@router.post("/cookies/helper/fail")
def fail(request: Request, response: Response):
    _no_store(response)
    with store().connection() as db:
        row = store().authorized(db, _bearer(request), upload=True)
        result = "failed" if request.headers.get("x-login-result") == "failed" else "cancelled"
        db.execute(
            "UPDATE cookie_helper_sessions SET status=?,upload_hash='' WHERE session_id=?",
            (result, row["session_id"]),
        )
    return {"cancelled": True}
