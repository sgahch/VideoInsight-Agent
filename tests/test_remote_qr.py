from __future__ import annotations

import base64
import threading
import time
from dataclasses import replace
from http.cookiejar import MozillaCookieJar

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.qr_login as qr_login
import app.remote_qr as remote_qr
from app.browser_cookies import BrowserCookieError, cookie_browser_capabilities, start_cookie_browser
from app.config import Settings
from app.cookies import is_douyin_domain, save_douyin_login_cookies, summarize_cookie_file


IMAGE = remote_qr.PNG_PREFIX + base64.b64encode(b"\x89PNG\r\n\x1a\nexample-qr").decode()
COOKIE = {"domain": ".douyin.com", "name": "sessionid", "value": "dummy-login", "expires": -1, "httpOnly": True}


class FakeBrowser:
    def __init__(self):
        self.confirmed = threading.Event()
        self.opened = False
        self.closed = False
        self.page = {"image": IMAGE}
        self.login_cookies = [COOKIE]

    def open(self):
        self.opened = True

    def cookies(self):
        return self.login_cookies if self.confirmed.is_set() else []

    def state(self):
        return self.page

    def close(self):
        self.closed = True


@pytest.fixture
def login(monkeypatch, tmp_path):
    browser = FakeBrowser()
    manager = remote_qr.RemoteQRLoginManager()
    config = Settings(
        cookie_login_mode="remote", cookie_qr_browser_url="http://private-browser:9222",
        ytdlp_cookies_file=tmp_path / "cookies.txt",
    )
    monkeypatch.setattr(remote_qr, "BrowserConnection", lambda _: browser)
    monkeypatch.setattr(remote_qr, "POLL_SECONDS", 0.01)
    yield manager, browser, config
    manager.shutdown()


def wait_for(manager, session, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = manager.status(session, "owner")
        if state["status"] in expected:
            return state
        time.sleep(0.01)
    raise AssertionError("QR login did not reach expected state")


def test_inline_qr_waits_then_saves_and_closes_context(login):
    manager, browser, config = login
    started = manager.start(config, "owner")
    session = started["session_id"]
    assert wait_for(manager, session, {"waiting"})["qr_image_url"] == IMAGE
    assert not config.ytdlp_cookies_file.exists()
    browser.confirmed.set()
    result = wait_for(manager, session, {"confirmed"})
    manager.current.thread.join(2)
    assert result["completed"] and result["qr_image_url"] is None
    assert browser.closed and summarize_cookie_file(config.ytdlp_cookies_file)["has_login_cookie"]
    assert "dummy-login" not in str(result)


def test_inline_qr_owner_isolation_and_single_active_context(login):
    manager, _, config = login
    session = manager.start(config, "owner")["session_id"]
    assert manager.start(config, "owner")["session_id"] == session
    with pytest.raises(remote_qr.RemoteQRLoginError) as busy:
        manager.start(config, "other")
    assert busy.value.status_code == 409
    for action in (manager.status, manager.cancel):
        with pytest.raises(remote_qr.RemoteQRLoginError) as missing:
            action(session, "other")
        assert missing.value.status_code == 404


def test_cancel_releases_context_without_replacing_existing_cookies(login):
    manager, browser, config = login
    config.ytdlp_cookies_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    before = config.ytdlp_cookies_file.read_bytes()
    session = manager.start(config, "owner")["session_id"]
    wait_for(manager, session, {"waiting"})
    assert manager.cancel(session, "owner")["status"] == "cancelled"
    assert browser.closed and config.ytdlp_cookies_file.read_bytes() == before


@pytest.mark.parametrize("page,status", [
    ({"image": IMAGE, "expired": True}, "expired"),
    ({"image": "https://untrusted.example/qr.png"}, "failed"),
    ({"image": remote_qr.PNG_PREFIX + "not-base64"}, "failed"),
    ({"image": remote_qr.PNG_PREFIX + base64.b64encode(b"not-png").decode()}, "failed"),
    ({"image": remote_qr.PNG_PREFIX + "A" * 350_000}, "failed"),
])
def test_terminal_or_invalid_qr_releases_browser_without_saving(login, page, status):
    manager, browser, config = login
    browser.page = page
    session = manager.start(config, "owner")["session_id"]
    result = wait_for(manager, session, {status})
    manager.current.thread.join(2)
    assert browser.closed and not config.ytdlp_cookies_file.exists()
    assert result["qr_image_url"] is None


def test_scanned_waits_for_actual_login_cookie(login):
    manager, browser, config = login
    browser.page["scanned"] = True
    session = manager.start(config, "owner")["session_id"]
    result = wait_for(manager, session, {"scanned"})
    assert not result["completed"] and not config.ytdlp_cookies_file.exists()


def test_non_login_cookies_are_not_saved_as_success(login):
    manager, browser, config = login
    browser.login_cookies = [{**COOKIE, "name": "ttwid"}]
    browser.confirmed.set()
    session = manager.start(config, "owner")["session_id"]
    assert not wait_for(manager, session, {"waiting"})["completed"]
    assert not config.ytdlp_cookies_file.exists()


def test_browser_cookie_filter_rejects_other_domains_expired_and_empty():
    browser = remote_qr.BrowserConnection.__new__(remote_qr.BrowserConnection)
    browser.context = "isolated-context"
    values = [
        COOKIE,
        {**COOKIE, "domain": ".evildouyin.com"},
        {**COOKIE, "domain": "douyin.com.evil.example"},
        {**COOKIE, "expires": time.time() - 60},
        {**COOKIE, "value": ""},
    ]
    browser.call = lambda *args, **kwargs: {"cookies": values}
    assert browser.cookies() == [COOKIE]


def test_expiry_is_enforced_without_any_client_polling(login, monkeypatch):
    manager, browser, config = login
    monkeypatch.setattr(remote_qr, "SESSION_SECONDS", 0.05)
    manager.start(config, "owner")
    manager.current.thread.join(2)
    assert manager.current.status == "expired" and browser.closed
    assert not config.ytdlp_cookies_file.exists()


def test_loading_timeout_reports_failure_and_closes_context(login, monkeypatch):
    manager, browser, config = login
    browser.page = {}
    monkeypatch.setattr(remote_qr, "PAGE_LOAD_SECONDS", 0)
    manager.start(config, "owner")
    manager.current.thread.join(2)
    assert manager.current.status == "failed" and browser.closed


def test_connection_error_does_not_leak_internals(login, monkeypatch):
    manager, _, config = login

    def fail(_):
        raise RuntimeError("private-token-and-browser-address")

    monkeypatch.setattr(remote_qr, "BrowserConnection", fail)
    session = manager.start(config, "owner")["session_id"]
    state = wait_for(manager, session, {"failed"})
    assert "private-token" not in str(state)


def test_shutdown_cancels_active_session(login):
    manager, browser, config = login
    session = manager.start(config, "owner")["session_id"]
    wait_for(manager, session, {"waiting"})
    manager.shutdown()
    assert browser.closed and manager.current.status == "cancelled"


@pytest.mark.parametrize("domain,expected", [
    (".douyin.com", True), ("www.douyin.com", True), (".passport.douyin.com", True),
    ("evildouyin.com", False), ("douyin.com.evil.example", False),
])
def test_cookie_domain_boundaries(domain, expected):
    assert is_douyin_domain(domain) is expected


def test_cookie_save_is_private_atomic_and_preserves_other_platforms(tmp_path):
    target = tmp_path / "cookies.txt"
    target.write_text(
        "# Netscape HTTP Cookie File\n"
        ".bilibili.com\tTRUE\t/\tFALSE\t2000000000\tSESSDATA\tkeep-bilibili\n"
        ".douyin.com\tTRUE\t/\tFALSE\t2000000000\tsessionid\told-douyin\n",
        encoding="utf-8",
    )
    save_douyin_login_cookies(target, [
        COOKIE, {"domain": ".evildouyin.com", "name": "sessionid", "value": "reject"},
    ])
    jar = MozillaCookieJar()
    jar.load(str(target), ignore_discard=True, ignore_expires=True)
    assert {(cookie.domain, cookie.name, cookie.value) for cookie in jar} == {
        (".bilibili.com", "SESSDATA", "keep-bilibili"),
        (".douyin.com", "sessionid", "dummy-login"),
    }
    assert summarize_cookie_file(target)["has_login_cookie"]
    assert not list(tmp_path.glob(".qr-cookies-*"))


def test_inline_qr_capability_does_not_enable_native_browser(login):
    _, _, config = login
    capabilities = cookie_browser_capabilities(config)
    assert capabilities["mode"] == "remote" and capabilities["qr_available"]
    with pytest.raises(BrowserCookieError, match="本页扫码"):
        start_cookie_browser(config)
    assert cookie_browser_capabilities(replace(config, cookie_login_mode="import"))["mode"] == "import"
    assert config.cookie_qr_browser_url not in repr(config)


def test_cookie_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    import app.cookies as cookie_module

    target = tmp_path / "cookies.txt"
    before = b"# Netscape HTTP Cookie File\n"
    target.write_bytes(before)

    def fail_replace(*_):
        raise OSError("test write failure")

    monkeypatch.setattr(cookie_module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        save_douyin_login_cookies(target, [COOKIE])
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".qr-cookies-*"))


def test_inline_qr_api_binds_owner_and_disables_caching(login, monkeypatch):
    manager, browser, config = login
    monkeypatch.setattr(main_module, "settings", config)
    monkeypatch.setattr(qr_login, "remote_qr_manager", manager)
    monkeypatch.setattr(main_module, "remote_qr_manager", manager)
    client = TestClient(main_module.app)
    assert client.post("/cookies/qr/start").status_code == 400
    started = client.post("/cookies/qr/start", headers={"X-Client-ID": "owner"})
    assert started.status_code == 200 and started.headers["cache-control"] == "no-store"
    session = started.json()["session_id"]
    wait_for(manager, session, {"waiting"})
    endpoint = f"/cookies/qr/{session}/status"
    assert client.get(endpoint, headers={"X-Client-ID": "other"}).status_code == 404
    response = client.get(endpoint, headers={"X-Client-ID": "owner"})
    assert response.headers["cache-control"] == "no-store" and response.json()["qr_image_url"] == IMAGE
    assert client.delete(f"/cookies/qr/{session}", headers={"X-Client-ID": "other"}).status_code == 404
    assert client.delete(f"/cookies/qr/{session}", headers={"X-Client-ID": "owner"}).json()["status"] == "cancelled"
    assert browser.closed
