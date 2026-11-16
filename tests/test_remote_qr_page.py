"""Opt-in DOM regressions against an isolated, offline browser context."""
from __future__ import annotations

import os

import pytest

from app.remote_qr import BrowserConnection, PNG_PREFIX


pytestmark = pytest.mark.skipif(
    not os.getenv("QR_TEST_BROWSER_URL"), reason="Requires an isolated test Chromium CDP endpoint",
)
IMAGE = PNG_PREFIX + "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII="
BUTTON = '<button onclick="window.loginClicks=(window.loginClicks||0)+1">登录</button>'


@pytest.fixture
def browser():
    class OfflineBrowser(BrowserConnection):
        def call(self, method, params=None, *, page=False):
            if method == "Page.navigate":
                params = {"url": "about:blank"}
            return super().call(method, params, page=page)

    connection = OfflineBrowser(os.environ["QR_TEST_BROWSER_URL"])
    connection.open()
    try:
        yield connection
    finally:
        connection.close()


def render(browser, html):
    frame = browser.call("Page.getFrameTree", page=True)["frameTree"]["frame"]["id"]
    browser.call("Page.setDocumentContent", {"frameId": frame, "html": html}, page=True)


def evaluate(browser, expression):
    return browser.call("Runtime.evaluate", {
        "expression": expression, "returnByValue": True,
    }, page=True)["result"].get("value")


def test_login_opener_is_clicked_only_once_while_qr_is_loading(browser):
    render(browser, BUTTON)
    for _ in range(4):
        browser.state()
    assert evaluate(browser, "window.loginClicks") == 1


def test_missing_qr_after_display_never_reopens_login_even_after_document_change(browser):
    render(browser, f'{BUTTON}<div id="douyin_login_comp_scan_code_qrcode"><img src="{IMAGE}"></div>')
    assert browser.state()["image"] == IMAGE
    render(browser, BUTTON)
    for _ in range(3):
        browser.state()
    assert evaluate(browser, "window.loginClicks || 0") == 0


def test_confirmation_text_is_read_after_qr_element_is_replaced(browser):
    render(browser, f'{BUTTON}<div id="douyin_login_comp_scan_code_login_content">'
           f'<div id="douyin_login_comp_scan_code_qrcode"><img src="{IMAGE}"></div></div>')
    assert browser.state()["image"] == IMAGE
    evaluate(browser, """document.getElementById('douyin_login_comp_scan_code_login_content')
        .innerHTML = '<p>扫码成功，请在手机上确认</p>'""")
    state = browser.state()
    assert state["scanned"] and not state["image"]
    assert evaluate(browser, "window.loginClicks || 0") == 0


def test_security_verification_is_detected_only_inside_login_component(browser):
    render(browser, '<p>其他内容提到安全验证</p><div id="douyin_login_comp_scan_code_login_content"></div>')
    assert not browser.state().get("verification")
    evaluate(browser, """document.getElementById('douyin_login_comp_scan_code_login_content')
        .textContent = '请完成安全验证'""")
    assert browser.state()["verification"]
