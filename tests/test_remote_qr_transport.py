"""Opt-in QR DOM, CDP cookie capture, and atomic-save integration check."""
from __future__ import annotations

import os
import time

import pytest

import app.remote_qr as remote_qr
from app.config import Settings
from app.cookies import summarize_cookie_file
from app.account_auth import task_account_scope
from app.adapters.downloader import cookie_storage_path


@pytest.mark.skipif(not os.getenv("QR_TEST_BROWSER_URL"), reason="Requires an isolated test Chromium CDP endpoint")
@pytest.mark.parametrize("owner", ["transport-test", "account:" + "a" * 32])
def test_real_browser_transport_detects_qr_and_saves_test_login(tmp_path, monkeypatch, owner):
    endpoint = os.environ["QR_TEST_BROWSER_URL"]
    image = remote_qr.PNG_PREFIX + "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII="
    actual_browser = remote_qr.BrowserConnection

    class FixtureBrowser(actual_browser):
        def call(self, method, params=None, *, page=False):
            if method == "Page.navigate":
                params = {"url": "about:blank"}
            return super().call(method, params, page=page)

        def open(self):
            super().open()
            frame = self.call("Page.getFrameTree", page=True)["frameTree"]["frame"]["id"]
            self.call("Page.setDocumentContent", {
                "frameId": frame,
                "html": f'<div><div><div id="douyin_login_comp_scan_code_qrcode"><img src="{image}"></div></div></div>',
            }, page=True)

    browser = FixtureBrowser(endpoint)
    monkeypatch.setattr(remote_qr, "BrowserConnection", lambda _: browser)
    monkeypatch.setattr(remote_qr, "POLL_SECONDS", 0.02)
    config = Settings(cookie_login_mode="remote", cookie_qr_browser_url=endpoint,
                      ytdlp_cookies_file=tmp_path / "cookies.txt", db_path=tmp_path / "data/tasks.db")
    config.ytdlp_cookies_file.write_text(
        "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tkeep-other-platform\n",
        encoding="utf-8",
    )
    global_before = config.ytdlp_cookies_file.read_bytes()
    manager = remote_qr.RemoteQRLoginManager()
    with task_account_scope(owner):
        target = cookie_storage_path(config)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(global_before)
        session_id = manager.start(config, owner)["session_id"]
    try:
        deadline = time.monotonic() + 10
        while manager.status(session_id, owner)["status"] != "waiting":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert manager.status(session_id, owner)["qr_image_url"] == image
        assert not summarize_cookie_file(target)["has_login_cookie"]
        # The worker owns its CDP connection; inject through a separate connection.
        injector = actual_browser(endpoint)
        try:
            injector.call("Storage.setCookies", {
                "browserContextId": browser.context,
                "cookies": [
                    {"name": name, "value": "test-only-not-a-real-login", "domain": ".douyin.com",
                     "path": "/", "secure": True, "httpOnly": True}
                    for name in ("sessionid", "ttwid")
                ],
            })
        finally:
            injector.close()
        while not manager.status(session_id, owner)["completed"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        manager.current.thread.join(3)
        assert not manager.current.thread.is_alive()
        text = target.read_text(encoding="utf-8")
        assert "keep-other-platform" in text and "test-only-not-a-real-login" in text
        assert summarize_cookie_file(target)["has_login_cookie"]
        assert "test-only" not in str(manager.status(session_id, owner))
        if owner.startswith("account:"):
            assert config.ytdlp_cookies_file.read_bytes() == global_before
        check = actual_browser(endpoint)
        try:
            assert browser.context not in check.call("Target.getBrowserContexts")["browserContextIds"]
        finally:
            check.close()
    finally:
        manager.shutdown()
