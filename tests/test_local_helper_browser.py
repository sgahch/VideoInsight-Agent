"""Opt-in real Chromium transport; never opens a real login or daily profile."""
from __future__ import annotations

import os
import threading

import pytest

from local_helper.core import DedicatedBrowser, capture_and_save


@pytest.mark.skipif(os.getenv("HELPER_BROWSER_TEST") != "1", reason="Requires a local Chrome or Edge installation")
def test_real_dedicated_browser_captures_http_only_cookie_and_erases_profile(tmp_path):
    observed = {}

    class OfflineBrowser(DedicatedBrowser):
        def __init__(self):
            super().__init__(tmp_path)

        def arguments(self, executable):
            args = super().arguments(executable)
            args.remove("--new-window")
            return args[:-1] + ["--headless=new", "about:blank"]

        def open(self, cancelled):
            super().open(cancelled)
            observed["profile"] = self.profile
            observed["pid"] = self.process.pid
            assert not self.cookies()
            self.call("Storage.setCookies", {"cookies": [
                {"name": "sessionid", "value": "offline-test-only", "domain": ".douyin.com",
                 "path": "/", "secure": True, "httpOnly": True},
                {"name": "secret", "value": "not-exported", "domain": ".example.com", "path": "/"},
            ]})

    browser = OfflineBrowser()

    class Client:
        def post(self, action, token, data=None, **kwargs):
            if action == "complete":
                observed["uploaded"] = data
            return {"saved": True, "username": "offline@example.invalid"}

    result = capture_and_save(
        Client(), {"upload_token": "offline-upload", "expires_in": 60},
        threading.Event(), lambda _: None, browser_factory=lambda: browser,
    )
    assert result["saved"]
    assert observed["uploaded"]["cookies"] == [
        {"name": "sessionid", "value": "offline-test-only", "domain": ".douyin.com", "path": "/",
         "secure": True, "httpOnly": True, "expires": -1},
    ]
    assert browser.process.handle is None if hasattr(browser.process, "handle") else browser.process.poll() is not None
    assert not observed["profile"].exists()
