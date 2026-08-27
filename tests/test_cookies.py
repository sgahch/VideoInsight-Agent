from __future__ import annotations

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
