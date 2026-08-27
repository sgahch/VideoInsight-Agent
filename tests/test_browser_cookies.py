from __future__ import annotations

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


def test_cookie_browser_status_shape() -> None:
    status = cookie_browser_status()

    assert "running" in status
    assert "port" in status
