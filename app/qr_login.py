from __future__ import annotations

from app.browser_cookies import BrowserCookieError, capture_douyin_cookies, cookie_browser_status, start_cookie_browser
from app.config import Settings, settings
from app.adapters.downloader import cookie_storage_path
from app.cookies import summarize_cookie_file


class QRLoginError(Exception):
    pass


def start_qr_login(runtime_settings: Settings = settings) -> dict[str, object]:
    try:
        # Reuse the persistent dedicated profile. Clearing it here forced a full
        # QR/SMS login every time the user clicked the login button.
        browser = start_cookie_browser(runtime_settings)
    except BrowserCookieError as exc:
        raise QRLoginError(str(exc)) from exc
    return {
        "session_id": "browser",
        "status": "waiting",
        "completed": False,
        "message": "请在专用抖音窗口中完成登录，本页会自动检测；如果已有登录态，通常无需再次扫码。",
        "browser": browser,
    }


def check_qr_login(session_id: str, runtime_settings: Settings = settings) -> dict[str, object]:
    if session_id != "browser":
        raise QRLoginError("没有找到二维码会话，请重新打开扫码登录窗口。")

    try:
        status = cookie_browser_status()
    except BrowserCookieError as exc:
        raise QRLoginError(str(exc)) from exc

    if not status.get("running"):
        return _payload("waiting", "专用登录窗口未连接，请重新打开扫码登录窗口。", False, status, runtime_settings)
    if not status.get("has_douyin_tab"):
        return _payload("waiting", "窗口已打开，等待抖音页面加载完成。", False, status, runtime_settings)

    try:
        captured = capture_douyin_cookies(runtime_settings)
    except BrowserCookieError as exc:
        raise QRLoginError(str(exc)) from exc

    if captured.get("has_login_cookie"):
        return _payload("confirmed", "扫码登录成功，Cookies 已保存。", True, status, runtime_settings, captured)
    return _payload(
        "waiting",
        f"窗口已连接，等待扫码确认；当前抓到 {captured.get('count', 0)} 条非登录 Cookies，未覆盖本地文件。",
        False,
        status,
        runtime_settings,
        captured,
    )


def _payload(
    status: str,
    message: str,
    completed: bool,
    browser_status: dict[str, object],
    runtime_settings: Settings,
    captured: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "session_id": "browser",
        "status": status,
        "completed": completed,
        "message": message,
        "browser": browser_status,
        "capture": captured,
        "cookies": summarize_cookie_file(cookie_storage_path(runtime_settings)),
    }
