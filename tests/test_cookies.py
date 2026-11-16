from __future__ import annotations

import pytest

from app.cookies import cookie_json_to_netscape, header_cookie_to_netscape, normalize_cookie_upload, summarize_cookie_text


def test_header_cookie_to_netscape() -> None:
    text = header_cookie_to_netscape("a=1; b=two")

    assert "# Netscape HTTP Cookie File" in text
    assert ".douyin.com\tTRUE\t/\tFALSE" in text
    assert "\ta\t1" in text
    assert "\tb\ttwo" in text


def test_normalize_keeps_netscape() -> None:
    raw = b"# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tFALSE\t0\ta\t1\n"

    assert normalize_cookie_upload(raw) == raw.decode()


def test_cookie_editor_json_to_netscape() -> None:
    text = cookie_json_to_netscape(
        '[{"domain": ".douyin.com", "path": "/", "secure": true, '
        '"expirationDate": 2000000000, "name": "sessionid", "value": "abc"}]'
    )

    assert ".douyin.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tabc" in text
    summary = summarize_cookie_text(text)
    assert summary["count"] == 1
    assert summary["has_login_cookie"] is True


@pytest.mark.parametrize("domain,expires,value", [
    (".douyin.com", "123", "expired"),
    (".douyin.com", "0", ""),
    (".douyin.com.evil.example", "0", "unrelated"),
    (".notdouyin.com", "0", "unrelated"),
    (".bilibili.com", "0", "unrelated"),
    (".douyin.com", "invalid", "malformed"),
    (".douyin.com", "-1", "invalid-expiry"),
])
def test_summary_does_not_report_invalid_or_foreign_login_cookies(domain, expires, value):
    text = f"# Netscape HTTP Cookie File\n{domain}\tTRUE\t/\tFALSE\t{expires}\tsessionid\t{value}\n"
    summary = summarize_cookie_text(text)
    assert summary["count"] == 1
    assert not summary["has_login_cookie"]
    assert summary["present_important"] == []


@pytest.mark.parametrize("expires", ["0", "", "253402300799"])
def test_summary_recognizes_session_and_future_httponly_login_cookies(expires):
    text = f"# Netscape HTTP Cookie File\n#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t{expires}\tsessionid\tactive\n"
    assert summarize_cookie_text(text)["has_login_cookie"]
