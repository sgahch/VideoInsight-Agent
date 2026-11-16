from __future__ import annotations

import os
import threading

import pytest
from fastapi import HTTPException

import app.adapters.downloader as downloader
import app.comments_api as comments
from app.account_auth import account_owner, task_account_scope
from app.browser_cookies import cookie_browser_capabilities
from app.config import Settings
from app.cookies import write_normalized_cookie_file
from app.qr_login import QRLoginError, check_qr_login, start_qr_login
from app.remote_qr import RemoteQRLoginError
from app.schemas import AnalyzeRequest
from app.task_queue import TaskQueue, reconcile_interrupted_tasks
from app.task_store import SQLiteTaskStore


A = "account:" + "a" * 32
B = "account:" + "b" * 32


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = Settings(db_path=tmp_path / "data/tasks.db", media_dir=tmp_path / "media",
                      ytdlp_cookies_file=tmp_path / "admin.txt", ytdlp_cookies_from_browser="chrome",
                      cookie_login_mode="desktop", cookie_qr_browser_url=None)
    monkeypatch.setattr(downloader, "settings", config)
    write_normalized_cookie_file(config.ytdlp_cookies_file, b"sessionid=admin-secret")
    return config


def test_no_shared_file_browser_or_desktop_fallback(config, tmp_path):
    with task_account_scope(A):
        assert downloader.resolve_cookies_file(config) is None
        assert downloader._cookie_args(None, tmp_path) == []
        assert not downloader._refresh_project_cookies_from_browser()
        assert cookie_browser_capabilities(config)["mode"] == "import"
        with pytest.raises(QRLoginError):
            start_qr_login(config, A)
        with pytest.raises(RemoteQRLoginError) as error:
            check_qr_login("browser", config, A)
        assert error.value.status_code == 404
        path = downloader.cookie_storage_path(config)
        assert not path.is_relative_to(config.media_dir)
        write_normalized_cookie_file(path, b"sessionid=alice-secret")
        assert downloader.resolve_cookies_file(config) == path
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
    with task_account_scope(B):
        assert downloader.resolve_cookies_file(config) is None
    assert downloader.resolve_cookies_file(config) == config.ytdlp_cookies_file
    assert account_owner() is None


def test_recovered_queue_restores_owner_and_reused_thread_does_not_leak(config):
    store = SQLiteTaskStore(config.db_path)
    for owner, value in [(A, "alice"), (B, "bob")]:
        with task_account_scope(owner):
            write_normalized_cookie_file(downloader.cookie_storage_path(config), f"sessionid={value}".encode())
    expected = [(A, "alice"), (B, "bob"), (None, "admin-secret"), (A, "alice"), (None, "admin-secret")]
    for owner, _ in expected:
        store.create(AnalyzeRequest(url="https://example.com/video", client_id=owner))
    recovered = reconcile_interrupted_tasks(store, config)
    observed = []
    queue = TaskQueue(1)

    def work():
        owner = account_owner()
        header = downloader.DouyinShareDownloader(config.media_dir)._douyin_cookie_header()
        observed.append((owner, header.removeprefix("sessionid="), threading.get_ident()))
        if len(observed) == 4:
            raise RuntimeError("synthetic worker failure")

    try:
        for task in recovered.pending:
            queue.submit(task.task_id, store, work)
    finally:
        queue.shutdown(wait=True)
    assert [(owner, value) for owner, value, _ in observed] == expected
    assert len({thread for _, _, thread in observed}) == 1
    assert account_owner() is None


def test_concurrent_workers_use_only_own_cookie_file(config):
    store = SQLiteTaskStore(config.db_path)
    for owner, value in [(A, "alice"), (B, "bob")]:
        with task_account_scope(owner):
            write_normalized_cookie_file(downloader.cookie_storage_path(config), f"sessionid={value}".encode())
    barrier = threading.Barrier(2)
    seen = {}

    def work():
        barrier.wait(timeout=5)
        seen[account_owner()] = downloader.DouyinShareDownloader(config.media_dir)._douyin_cookie_header()

    queue = TaskQueue(2)
    try:
        for owner in [A, B]:
            task = store.create(AnalyzeRequest(url="https://example.com/video", client_id=owner))
            queue.submit(task.task_id, store, work)
    finally:
        queue.shutdown(wait=True)
    assert seen == {A: "sessionid=alice", B: "sessionid=bob"}


def test_comment_jobs_sync_private_cookie_and_fail_closed_on_old_service(config, monkeypatch):
    monkeypatch.setattr(comments, "canonical_media_url", lambda url, **kwargs: url)
    calls = []
    isolated = True

    def service(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"account_cookies": isolated} if path == "/health" else {"id": "job"}

    monkeypatch.setattr(comments, "service_request", service)
    with task_account_scope(A):
        write_normalized_cookie_file(downloader.cookie_storage_path(config), b"sessionid=alice-secret")
    comments.start_for_analysis("https://www.douyin.com/video/123", A, config)
    upload = calls[1]
    assert upload[:2] == ("PUT", "/platforms/dy/cookies")
    assert upload[2]["params"] == {"owner": A}
    assert "alice-secret" in upload[2]["json"]["content"]
    assert "admin-secret" not in str(calls)
    isolated = False
    calls.clear()
    with pytest.raises(HTTPException) as error:
        comments.start_for_analysis("https://www.douyin.com/video/123", A, config)
    assert error.value.status_code == 503
    assert [path for _, path, _ in calls] == ["/health"]
    assert account_owner() is None
