from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.account_auth as accounts
import app.adapters.downloader as downloader
import app.comments_api as comments
import app.main as main
import app.qr_login as qr
import app.remote_qr as remote
import app.security as security
from app.config import Settings
from app.cookies import (
    clear_douyin_login_cookies, cookie_logout_path, save_douyin_login_cookies,
    write_normalized_cookie_file,
)
from app.schemas import AnalyzeRequest
from app.task_store import SQLiteTaskStore


A = "account:" + "a" * 32
B = "account:" + "b" * 32
COOKIE = {"domain": ".douyin.com", "name": "sessionid", "value": "synthetic", "expires": -1}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    config = Settings(
        auth_token=None, auth_username="owner", auth_password_hash=accounts.hash_password("admin-password"),
        auth_session_secret="isolated-cookie-logout-session-secret",
        db_path=tmp_path / "tasks.db", media_dir=tmp_path / "media",
        tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results",
        ytdlp_cookies_file=None, ytdlp_cookies_from_browser="chrome",
        cookie_login_mode="import", cookie_qr_browser_url=None,
        comment_crawler_url=None, comment_crawler_token=None,
    )
    config.ensure_dirs()
    for module in (accounts, downloader, comments, main, security):
        monkeypatch.setattr(module, "settings", config)
    store = SQLiteTaskStore(config.db_path)
    monkeypatch.setattr(main, "task_store", store)
    manager = remote.RemoteQRLoginManager()
    monkeypatch.setattr(main, "remote_qr_manager", manager)
    clients = []
    for owner in (A, B):
        user = {"user_id": owner.removeprefix("account:"), "email": owner[-1] + "@example.com",
                "password_hash": accounts.hash_password("user-password")}
        account_store = accounts.account_store()
        with account_store._connection() as connection:
            connection.execute("INSERT INTO auth_users VALUES (?, ?, ?, ?)",
                               (user["user_id"], user["email"], user["password_hash"], int(time.time())))
        client = TestClient(main.app)
        client.cookies.set(accounts.SESSION_COOKIE_NAME, accounts.create_user_session(user))
        clients.append(client)
    return config, store, manager, clients


def test_clear_own_cookie_preserves_other_accounts_platforms_and_tasks(setup):
    config, store, manager, (alice, bob) = setup
    paths = {}
    for owner in (A, B, None):
        with accounts.task_account_scope(owner):
            path = downloader.cookie_storage_path(config)
            save_douyin_login_cookies(path, [COOKIE])
            paths[owner] = path
    with accounts.task_account_scope(A):
        path = paths[A]
        write_normalized_cookie_file(path, path.read_bytes() + b".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tbili-test\n")
    task = store.create(AnalyzeRequest(url="https://example.com/video", client_id=A))
    for owner in (A, B):
        session = remote.LoginSession(owner)
        manager.sessions[session.session_id] = session
    response = alice.delete("/cookies", headers={"X-Client-ID": B})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["scope"] == "account" and response.json()["removed"] == 1
    assert "SESSDATA" in paths[A].read_text() and "sessionid" not in paths[A].read_text()
    assert all("sessionid" in paths[owner].read_text() for owner in (B, None))
    assert cookie_logout_path(paths[A]).is_file()
    assert [s.cancel.is_set() for s in manager.sessions.values()] == [True, False]
    assert alice.get(f"/tasks/{task.task_id}").status_code == 200
    assert bob.get("/cookies/status").json()["has_login_cookie"]
    assert alice.delete("/cookies").json()["removed"] == 0
    assert alice.get("/auth/me").json()["authenticated"]


def test_clear_rejects_unauthenticated_and_cross_origin_requests(setup):
    _, _, _, (alice, _) = setup
    assert TestClient(main.app).delete("/cookies").status_code == 401
    assert alice.delete("/cookies", headers={"Origin": "https://other.example"}).status_code == 403


def test_clearing_blocks_automatic_browser_recovery_until_explicit_import(setup, tmp_path):
    config, _, _, _ = setup
    path = downloader.cookie_storage_path(config)
    save_douyin_login_cookies(path, [COOKIE])
    assert downloader._cookie_args(None, tmp_path) == ["--cookies-from-browser", "chrome"]
    clear_douyin_login_cookies(path)
    assert downloader.resolve_cookies_file(config) is None
    assert downloader._cookie_args(None, tmp_path) == []
    assert not downloader._refresh_project_cookies_from_browser()
    assert qr.check_qr_login("browser", config)["status"] == "cancelled"
    write_normalized_cookie_file(path, b"sessionid=new-explicit-login")
    assert not cookie_logout_path(path).exists()
    assert downloader.resolve_cookies_file(config) == path


def test_configured_file_can_be_cleared_without_missing_file_error(setup, monkeypatch):
    config, _, _, _ = setup
    config = replace(config, ytdlp_cookies_file=config.media_dir / "configured.txt")
    monkeypatch.setattr(downloader, "settings", config)
    save_douyin_login_cookies(config.ytdlp_cookies_file, [COOKIE])
    clear_douyin_login_cookies(config.ytdlp_cookies_file)
    assert downloader.resolve_cookies_file(config) is None
    save_douyin_login_cookies(config.ytdlp_cookies_file, [COOKIE])
    assert not cookie_logout_path(config.ytdlp_cookies_file).exists()


def test_clear_cancels_in_flight_qr_confirmation_before_save(setup, monkeypatch):
    config, _, manager, _ = setup
    entered, release = threading.Event(), threading.Event()

    class Browser:
        def __init__(self, *_):
            pass

        def open(self):
            pass

        def cookies(self):
            entered.set()
            assert release.wait(3)
            return [COOKIE]

        def close(self):
            pass

    monkeypatch.setattr(remote, "BrowserConnection", Browser)
    with accounts.task_account_scope(A):
        session = manager.start(config, A)
        assert entered.wait(3)
        path = downloader.cookie_storage_path(config)
        manager.clear_cookies(config, A)
    release.set()
    manager.current.thread.join(3)
    assert not manager.current.thread.is_alive()
    assert manager.status(session["session_id"], A)["status"] == "cancelled"
    assert not path.exists() and cookie_logout_path(path).exists()


def test_cleared_comment_copy_is_removed_and_old_service_fails_closed(setup, monkeypatch):
    monkeypatch.setattr(comments, "canonical_media_url", lambda url, **kwargs: url)
    config, _, _, (alice, _) = setup
    config = replace(config, comment_crawler_url="http://isolated-comments",
                     comment_crawler_token="synthetic-only")
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(comments, "settings", config)
    calls = []

    def service(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"account_cookies": True} if path == "/health" else {"deleted": True}

    monkeypatch.setattr(main, "service_request", service)
    monkeypatch.setattr(comments, "service_request", service)
    response = alice.delete("/cookies")
    assert response.json()["comments_cleared"]
    assert calls[-1][0:2] == ("DELETE", "/platforms/dy/cookies")
    assert calls[-1][2]["params"] == {"owner": A}
    calls.clear()
    comments.start_for_analysis("https://www.douyin.com/video/123", A, config)
    assert [call[0] for call in calls] == ["GET", "DELETE", "POST"]
    assert calls[1][2]["params"] == {"owner": A}

    def unavailable(*args, **kwargs):
        raise HTTPException(503, "fixture unavailable")

    monkeypatch.setattr(main, "service_request", unavailable)
    assert not alice.delete("/cookies").json()["comments_cleared"]
    monkeypatch.setattr(comments, "service_request", unavailable)
    with pytest.raises(HTTPException):
        comments.start_for_analysis("https://www.douyin.com/video/123", A, config)


def test_explicit_desktop_relogin_resets_old_profile_before_reenabling_capture(setup, monkeypatch):
    config, _, _, _ = setup
    path = downloader.cookie_storage_path(config)
    clear_douyin_login_cookies(path)
    calls = []
    monkeypatch.setattr(qr, "start_cookie_browser",
                        lambda _, **kwargs: calls.append(kwargs) or {"running": True})
    assert qr.start_qr_login(config)["session_id"] == "browser"
    assert calls == [{"reset_session": True}]
    assert not cookie_logout_path(path).exists()


def test_invalid_cookie_file_is_preserved_and_clear_returns_failure(setup):
    config, _, _, (alice, _) = setup
    with accounts.task_account_scope(A):
        path = downloader.cookie_storage_path(config)
        path.parent.mkdir(parents=True)
        path.write_text("not a cookie jar", encoding="utf-8")
    response = alice.delete("/cookies")
    assert response.status_code == 500
    assert path.read_text(encoding="utf-8") == "not a cookie jar"
