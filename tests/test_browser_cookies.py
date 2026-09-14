from __future__ import annotations

from pathlib import Path

import app.browser_cookies as browser_cookies
from app.config import Settings
from app.browser_cookies import _cookies_to_netscape
from app.browser_cookies import cookie_browser_status


def test_cookies_to_netscape() -> None:
    text = _cookies_to_netscape(
        [
            {
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
                "expires": 2000000000,
                "name": "sessionid",
                "value": "abc",
            }
        ]
    )

    assert ".douyin.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tabc" in text


def test_session_cookie_expiry_is_normalized_for_ytdlp() -> None:
    text = _cookies_to_netscape(
        [
            {
                "domain": "www.douyin.com",
                "path": "/",
                "secure": False,
                "expires": -1,
                "name": "x-web-secsdk-uid",
                "value": "uid",
            }
        ]
    )

    row = next(line for line in text.splitlines() if "x-web-secsdk-uid" in line)
    assert int(row.split("\t")[4]) > 0


def test_cookie_browser_status_shape() -> None:
    status = cookie_browser_status()

    assert "running" in status
    assert "port" in status


def test_active_debug_port_does_not_attach_to_unowned_browser(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_settings = Settings(media_dir=tmp_path / "media")
    monkeypatch.setattr(browser_cookies, "_is_devtools_port", lambda port: True)

    assert browser_cookies._active_debug_port(runtime_settings) is None


def test_active_debug_port_reuses_saved_browser(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_settings = Settings(media_dir=tmp_path / "media")
    metadata_dir = runtime_settings.media_dir / "_browser_profile"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "debug_port.txt").write_text("9224", encoding="utf-8")
    monkeypatch.setattr(browser_cookies, "_is_devtools_port", lambda port: port == 9224)

    assert browser_cookies._active_debug_port(runtime_settings) == 9224


def test_start_cookie_browser_resets_reused_login_session(monkeypatch, tmp_path: Path) -> None:
    runtime_settings = Settings(media_dir=tmp_path / "media")
    reset_ports: list[int] = []
    monkeypatch.setattr(browser_cookies, "_active_debug_port", lambda settings: 9224)
    monkeypatch.setattr(
        browser_cookies,
        "_ensure_douyin_tab",
        lambda port: {"opened": False, "focused": True, "url": browser_cookies.DOUYIN_URL},
    )
    monkeypatch.setattr(browser_cookies, "_clear_douyin_browser_cookies", reset_ports.append)

    result = browser_cookies.start_cookie_browser(runtime_settings, reset_session=True)

    assert result["started"] is False
    assert reset_ports == [9224]


def test_profile_dir_reuses_saved_profile(monkeypatch, tmp_path: Path) -> None:
    runtime_settings = Settings(media_dir=tmp_path / "media")
    saved_profile = runtime_settings.media_dir / "_browser_profiles" / "session-old"
    saved_profile.mkdir(parents=True)
    metadata_dir = runtime_settings.media_dir / "_browser_profile"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "current_profile.txt").write_text(str(saved_profile), encoding="utf-8")

    assert browser_cookies._new_profile_dir(runtime_settings) == saved_profile


def test_qr_login_does_not_reset_reused_profile(monkeypatch, tmp_path: Path) -> None:
    import app.qr_login as qr_login

    runtime_settings = Settings(media_dir=tmp_path / "media")
    calls: list[bool] = []

    monkeypatch.setattr(
        qr_login,
        "start_cookie_browser",
        lambda _settings, **kwargs: calls.append(kwargs.get("reset_session", False)) or {"started": False},
    )

    result = qr_login.start_qr_login(runtime_settings)

    assert result["status"] == "waiting"
    assert calls == [False]
