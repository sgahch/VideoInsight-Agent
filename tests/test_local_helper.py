from __future__ import annotations

import threading
from urllib.parse import urlencode

import pytest

from local_helper.core import (
    SITE, Cancelled, DedicatedBrowser, HelperError, NetworkError,
    capture_and_save, douyin_login_cookies, parse_launch_url,
)


def uri(server=SITE, token="x" * 43):
    return "videoinsight-login://authorize?" + urlencode({"server": server, "token": token})


def test_launch_only_accepts_explicit_trusted_origins():
    assert parse_launch_url(uri()) == (SITE, "x" * 43)
    for server in ("http://206.187.222.21", "https://evil.example", "https://206.187.222.21@evil.example",
                   "https://206.187.222.21.evil.example", "file:///C:/", "http://127.0.0.1:22"):
        with pytest.raises(HelperError):
            parse_launch_url(uri(server))


@pytest.mark.parametrize("value", [
    "https://206.187.222.21/",
    "videoinsight-login://authorize?server=x",
    uri() + "&token=" + "y" * 43,
    uri() + "#injected",
    uri(token="bad token"),
    uri() + "\r\ninjected",
    "videoinsight-login://authorize/execute?" + urlencode({"server": SITE, "token": "x" * 43}),
    uri() + "&exec=calc.exe",
])
def test_untrusted_protocol_input_is_rejected(value):
    with pytest.raises((HelperError, ValueError)):
        parse_launch_url(value)


def test_export_filters_exact_domain_expiry_and_empty_cookies():
    login = {"domain": ".douyin.com", "name": "sessionid", "value": "test-only", "httpOnly": True}
    assert douyin_login_cookies([login]) == [login]
    for item in (
        {**login, "domain": ".notdouyin.com"}, {**login, "domain": ".douyin.com.evil.example"},
        {**login, "expires": 1}, {**login, "value": ""}, {**login, "name": "ttwid"},
    ):
        assert douyin_login_cookies([item]) == []
    assert douyin_login_cookies([{**login, "domain": "www.douyin.com"}])


def test_browser_uses_fresh_profile_and_loopback_only(tmp_path):
    browser = DedicatedBrowser(tmp_path)
    browser.profile = tmp_path / "sessions" / "login-test"
    arguments = browser.arguments("edge.exe")
    assert "--remote-debugging-address=127.0.0.1" in arguments
    assert "--remote-debugging-port=0" in arguments
    assert not any("remote-allow-origins" in argument for argument in arguments)
    assert arguments[-1] == "https://www.douyin.com/"
    assert str(tmp_path) in " ".join(arguments)


def test_already_cancelled_browser_never_creates_a_profile(tmp_path):
    browser = DedicatedBrowser(tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        browser.open(cancelled)
    assert browser.profile is None and browser.process is None
    assert not (tmp_path / "sessions").exists()


def test_cleanup_refuses_paths_outside_helper_sessions(tmp_path):
    browser = DedicatedBrowser(tmp_path)
    browser.profile = tmp_path.parent
    with pytest.raises(HelperError):
        browser.close()
    assert tmp_path.is_dir()


def test_socket_cleanup_error_does_not_skip_process_or_profile_cleanup(tmp_path):
    browser = DedicatedBrowser(tmp_path)
    browser.profile = tmp_path / "sessions" / "login-test"
    browser.profile.mkdir(parents=True)

    class Connection:
        def close(self):
            raise OSError("synthetic socket close error")

    class Process:
        closed = False

        def wait(self, **kwargs):
            return 0

        def close(self):
            self.closed = True

    browser.ws = Connection()
    browser.call = lambda *args: {}
    browser.process = Process()
    browser.close()
    assert browser.process.closed and browser.ws is None
    assert not browser.profile.exists()


def test_process_wait_error_still_releases_owned_job_and_profile(tmp_path):
    browser = DedicatedBrowser(tmp_path)
    browser.profile = tmp_path / "sessions" / "login-test"
    browser.profile.mkdir(parents=True)

    class Process:
        closed = False

        def wait(self, **kwargs):
            raise OSError("synthetic process handle error")

        def close(self):
            self.closed = True

    browser.process = Process()
    with pytest.raises(OSError):
        browser.close()
    assert browser.process.closed
    assert not browser.profile.exists()


def test_persistent_profile_cleanup_failure_is_reported(tmp_path, monkeypatch):
    browser = DedicatedBrowser(tmp_path)
    browser.profile = tmp_path / "sessions" / "login-test"
    browser.profile.mkdir(parents=True)

    def locked(*args):
        raise PermissionError("synthetic locked profile")

    monkeypatch.setattr("local_helper.core.shutil.rmtree", locked)
    monkeypatch.setattr("local_helper.core.time.sleep", lambda _: None)
    with pytest.raises(HelperError, match="临时"):
        browser.close()
    assert browser.profile.exists()


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.opened = False

    def open(self, cancelled):
        self.opened = True

    def cookies(self):
        return [{"domain": ".douyin.com", "name": "sessionid", "value": "test-only"}]

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, action, token, data=None, **kwargs):
        self.calls.append((action, token, data))
        return {"saved": True, "username": "test@example.invalid"}


def test_capture_saves_then_closes_only_its_browser():
    browser, client = FakeBrowser(), FakeClient()
    result = capture_and_save(client, {"upload_token": "upload", "expires_in": 600},
                              threading.Event(), lambda _: None, browser_factory=lambda: browser)
    assert result["saved"] and browser.closed and browser.opened
    assert [call[0] for call in client.calls] == ["progress", "progress", "complete"]
    assert client.calls[-1][2]["cookies"][0]["name"] == "sessionid"


def test_cleanup_failure_does_not_turn_a_confirmed_save_into_failure():
    class CleanupFailure(FakeBrowser):
        def close(self):
            raise OSError("synthetic cleanup failure")

    browser, client = CleanupFailure(), FakeClient()
    result = capture_and_save(client, {"upload_token": "upload", "expires_in": 600},
                              threading.Event(), lambda _: None, browser_factory=lambda: browser)
    assert result["saved"] and result["cleanup_warning"]
    assert client.calls[-1][0] == "complete"


def test_cleanup_failure_does_not_mask_the_original_authorization_failure():
    class CleanupFailure(FakeBrowser):
        def close(self):
            raise OSError("synthetic cleanup failure")

    class Revoked(FakeClient):
        def post(self, action, *args, **kwargs):
            if action == "progress":
                raise HelperError("synthetic revoked authorization")
            return super().post(action, *args, **kwargs)

    with pytest.raises(HelperError, match="synthetic revoked authorization"):
        capture_and_save(Revoked(), {"upload_token": "upload", "expires_in": 600},
                         threading.Event(), lambda _: None, browser_factory=CleanupFailure)


def test_cancel_does_not_upload_and_closes_browser():
    cancelled = threading.Event()
    browser, client = FakeBrowser(), FakeClient()
    cancelled.set()
    with pytest.raises(Cancelled):
        capture_and_save(client, {"upload_token": "upload", "expires_in": 600}, cancelled,
                         lambda _: None, browser_factory=lambda: browser)
    assert browser.closed
    assert not browser.opened
    assert "complete" not in [call[0] for call in client.calls]
    assert client.calls[-1][0] == "fail"


def test_cancellation_while_checking_site_does_not_open_browser():
    cancelled = threading.Event()
    browser = FakeBrowser()

    class CancelDuringProgress(FakeClient):
        def post(self, action, *args, **kwargs):
            if action == "progress":
                cancelled.set()
            return super().post(action, *args, **kwargs)

    client = CancelDuringProgress()
    with pytest.raises(Cancelled):
        capture_and_save(client, {"upload_token": "upload", "expires_in": 600}, cancelled,
                         lambda _: None, browser_factory=lambda: browser)
    assert not browser.opened and browser.closed
    assert [call[0] for call in client.calls] == ["progress", "fail"]


def test_site_cancellation_is_checked_before_opening_browser():
    browser = FakeBrowser()

    class Revoked(FakeClient):
        def post(self, *args, **kwargs):
            raise HelperError("revoked")

    with pytest.raises(HelperError):
        capture_and_save(Revoked(), {"upload_token": "upload", "expires_in": 600},
                         threading.Event(), lambda _: None, browser_factory=lambda: browser)
    assert browser.closed and not browser.opened


def test_ambiguous_network_result_retries_idempotently():
    browser = FakeBrowser()

    class Interrupted(FakeClient):
        def post(self, action, *args, **kwargs):
            if action == "complete" and not any(call[0] == "complete" for call in self.calls):
                self.calls.append(("complete", None, None))
                raise NetworkError("response lost")
            return super().post(action, *args, **kwargs)

    client = Interrupted()
    assert capture_and_save(client, {"upload_token": "upload", "expires_in": 600},
                            threading.Event(), lambda _: None, browser_factory=lambda: browser)["saved"]
    assert len([call for call in client.calls if call[0] == "complete"]) == 2
    assert browser.closed
