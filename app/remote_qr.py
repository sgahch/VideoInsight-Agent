from __future__ import annotations

import base64
import itertools
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from contextvars import copy_context
from urllib.parse import urlsplit, urlunsplit

import httpx
import websocket

from app.adapters.downloader import cookie_storage_path
from app.account_auth import is_account_owner
from app.config import Settings
from app.cookies import LOGIN_COOKIE_NAMES, clear_douyin_login_cookies, is_douyin_domain, save_douyin_login_cookies


SESSION_SECONDS = 180
PAGE_LOAD_SECONDS = 75
POLL_SECONDS = 2
TERMINAL = {"confirmed", "expired", "cancelled", "failed"}
PNG_PREFIX = "data:" + "image/png;" + "base64,"
LOGGER = logging.getLogger("uvicorn.error")
PAGE_STATE = r"""(() => {
    const box = document.querySelector('#douyin_login_comp_scan_code_qrcode');
    const img = box?.querySelector('img');
    const root = document.querySelector('#douyin_login_comp_scan_code_login_content')
        || document.querySelector('#douyin_login_comp_scan_code')
        || box?.parentElement?.parentElement;
    const text = (root?.innerText || '').slice(0, 3000);
    return {
        image: img?.src?.startsWith('data:image/png;base64,') ? img.src : null,
        login_visible: !!root && root.getBoundingClientRect().width > 0,
        scanned: /扫码成功|扫描成功|请在手机上确认|请在抖音.*确认/.test(text),
        confirming: /登录成功|授权成功|正在登录|登录中/.test(text),
        expired: /二维码已失效|二维码已过期|点击刷新/.test(text),
        verification: /请完成.*验证|安全验证|滑块验证|短信验证|验证失败/.test(text),
        rejected: /已取消登录|登录已取消|授权已取消|已拒绝登录|已拒绝授权/.test(text),
        login_error: /登录失败|授权失败|登录异常|操作过于频繁|系统繁忙|网络异常/.test(text)
    };
})()"""
OPEN_LOGIN = r"""(() => {
    const login = [...document.querySelectorAll('p,button,span')].find(
        e => e.textContent.trim() === '登录' && e.getBoundingClientRect().width > 0
    );
    if (!login) return false;
    login.click();
    return true;
})()"""


class RemoteQRLoginError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class BrowserConnection:
    """A private browser connection; each login owns a disposable context."""

    def __init__(self, endpoint: str):
        address = urlsplit(endpoint)
        if address.scheme not in {"http", "https"} or not address.hostname or address.username or address.query or address.fragment:
            raise RemoteQRLoginError("扫码浏览器地址配置无效，请联系管理员。")
        with httpx.Client(trust_env=False, timeout=8) as client:
            response = client.get(endpoint.rstrip("/") + "/json/version", headers={"Host": "localhost"})
            response.raise_for_status()
            path = urlsplit(response.json()["webSocketDebuggerUrl"]).path
        if not path.startswith("/devtools/browser/"):
            raise RemoteQRLoginError("扫码浏览器连接异常，请重新尝试。")
        url = urlunsplit(("wss" if address.scheme == "https" else "ws", address.netloc, path, "", ""))
        self.ws = websocket.create_connection(
            url, timeout=8, suppress_origin=True, host="localhost",
            http_no_proxy=[address.hostname],
        )
        self.ids = itertools.count(1)
        self.context: str | None = None
        self.session: str | None = None
        self.login_opened = False

    def call(self, method: str, params: dict | None = None, *, page: bool = False) -> dict:
        number = next(self.ids)
        command = {"id": number, "method": method, "params": params or {}}
        if page:
            command["sessionId"] = self.session
        self.ws.send(json.dumps(command))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = json.loads(self.ws.recv())
            if response.get("id") == number:
                if "error" in response:
                    raise RemoteQRLoginError("扫码浏览器响应异常，请刷新二维码重试。")
                return response.get("result", {})
        raise RemoteQRLoginError("扫码浏览器响应超时，请刷新二维码重试。")

    def open(self) -> None:
        self.context = self.call("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        target = self.call("Target.createTarget", {
            "url": "about:blank", "browserContextId": self.context,
        })["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.enable", page=True)
        self.call("Network.enable", page=True)
        self.call("Network.setBlockedURLs", {
            "urls": ["*.mp4*", "*.m3u8*", "*douyinpic.com*", "*douyincdn.com*"],
        }, page=True)
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False,
        }, page=True)
        self.call("Page.navigate", {"url": "https://www.douyin.com/user/self"}, page=True)

    def cookies(self) -> list[dict]:
        values = self.call("Storage.getCookies", {"browserContextId": self.context}).get("cookies", [])
        return [
            item for item in values
            if is_douyin_domain(str(item.get("domain", ""))) and item.get("value")
            and (item.get("expires", -1) <= 0 or item["expires"] > time.time())
        ]

    def state(self) -> dict:
        result = self.call("Runtime.evaluate", {
            "expression": PAGE_STATE, "returnByValue": True,
        }, page=True)
        if result.get("exceptionDetails"):
            raise RemoteQRLoginError("登录页面状态读取失败，请刷新二维码重试。")
        state = result.get("result", {}).get("value") or {}
        if state.get("image") or state.get("login_visible"):
            self.login_opened = True
        # A disappearing QR is part of authorization, not a reason to reopen login.
        if not self.login_opened:
            opened = self.call("Runtime.evaluate", {
                "expression": OPEN_LOGIN, "returnByValue": True,
            }, page=True)
            self.login_opened = bool(opened.get("result", {}).get("value"))
        return state

    def close(self) -> None:
        try:
            if self.context:
                self.ws.settimeout(2)
                self.call("Target.disposeBrowserContext", {"browserContextId": self.context})
        except Exception:
            pass
        finally:
            self.ws.close()


@dataclass
class LoginSession:
    owner: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.monotonic)
    status: str = "starting"
    message: str = "正在加载抖音登录二维码..."
    image: str | None = None
    authorization_started: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)


class RemoteQRLoginManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.current: LoginSession | None = None
        self.sessions: dict[str, LoginSession] = {}

    def start(self, runtime_settings: Settings, owner: str) -> dict[str, object]:
        with self.lock:
            current = self.current
            if current and current.thread and current.thread.is_alive():
                if current.owner == owner and current.status not in TERMINAL:
                    return self._payload(current)
                raise RemoteQRLoginError("已有扫码会话正在处理，请稍后重试。", 409)
            session = LoginSession(owner)
            context = copy_context()
            session.thread = threading.Thread(
                target=context.run, args=(self._run, session, runtime_settings), name="cookie-qr-login", daemon=True,
            )
            # Retain completed results so another user's login cannot hide success.
            for key, previous in list(self.sessions.items()):
                if previous.status in TERMINAL and time.monotonic() - previous.started > 600:
                    del self.sessions[key]
            while len(self.sessions) >= 64:
                del self.sessions[next(iter(self.sessions))]
            self.sessions[session.session_id] = session
            self.current = session
            session.thread.start()
            return self._payload(session)

    def status(self, session_id: str, owner: str) -> dict[str, object]:
        with self.lock:
            return self._payload(self._owned(session_id, owner))

    def cancel(self, session_id: str, owner: str) -> dict[str, object]:
        with self.lock:
            session = self._owned(session_id, owner)
            if session.status not in TERMINAL:
                session.cancel.set()
                self._update(session, "cancelled", "已取消扫码登录。")
            thread = session.thread
        if thread:
            thread.join(timeout=12)
        with self.lock:
            return self._payload(session)

    def shutdown(self) -> None:
        with self.lock:
            session = self.current
        if session:
            self.cancel(session.session_id, session.owner)

    def clear_cookies(self, runtime_settings: Settings, owner: str | None) -> int:
        # Serialize with QR saves so an in-flight confirmation cannot restore a cleared login.
        with self.lock:
            for session in self.sessions.values():
                if session.owner == owner or (owner is None and not is_account_owner(session.owner)):
                    session.cancel.set()
                    self._update(session, "cancelled", "本账号登录态已清除，请重新扫码。")
            return clear_douyin_login_cookies(cookie_storage_path(runtime_settings))

    def _owned(self, session_id: str, owner: str) -> LoginSession:
        session = self.sessions.get(session_id)
        if not session or session.owner != owner:
            raise RemoteQRLoginError("二维码会话不存在或已结束，请重新生成。", 404)
        return session

    def _payload(self, session: LoginSession) -> dict[str, object]:
        return {
            "session_id": session.session_id, "status": session.status,
            "completed": session.status == "confirmed", "message": session.message,
            "qr_image_url": session.image,
            "expires_in": max(0, int(SESSION_SECONDS - (time.monotonic() - session.started))),
        }

    def _update(self, session: LoginSession, status: str, message: str, image: str | None = None) -> None:
        with self.lock:
            if session.cancel.is_set() and status != "cancelled":
                return
            if status in {"scanned", "confirming"} and session.authorization_started is None:
                session.authorization_started = time.monotonic()
            if session.status != status:
                LOGGER.info(
                    "douyin_qr session=%s state=%s elapsed_s=%d",
                    session.session_id[:8], status, time.monotonic() - session.started,
                )
            session.status, session.message = status, message
            if status in TERMINAL:
                session.image = None
            elif image:
                session.image = image

    def _run(self, session: LoginSession, runtime_settings: Settings) -> None:
        browser = None
        try:
            browser = BrowserConnection(runtime_settings.cookie_qr_browser_url or "")
            browser.open()
            while not session.cancel.is_set():
                elapsed = time.monotonic() - session.started
                if elapsed >= SESSION_SECONDS:
                    message = "扫码后仍未收到有效登录态，本次授权未完成，请重新扫码。"
                    if session.authorization_started is None:
                        message = "二维码已过期，请点击刷新。"
                    self._update(session, "expired", message)
                    return
                cookies = browser.cookies()
                if any(cookie["name"] in LOGIN_COOKIE_NAMES for cookie in cookies):
                    with self.lock:
                        if session.cancel.is_set():
                            return
                        try:
                            save_douyin_login_cookies(cookie_storage_path(runtime_settings), cookies)
                        except OSError as exc:
                            raise RemoteQRLoginError("已收到抖音登录态，但服务器保存失败，请联系管理员处理。") from exc
                        self._update(session, "confirmed", "登录成功，Cookies 已自动保存。")
                    return
                state = browser.state()
                image = state.get("image")
                if image:
                    # Only accept a bounded PNG from the owned login component.
                    if not isinstance(image, str) or not image.startswith(PNG_PREFIX) or len(image) > 350_000:
                        raise RemoteQRLoginError("平台返回的二维码无效，请刷新重试。")
                    raw = base64.b64decode(image.removeprefix("data:image/png;base64,"), validate=True)
                    if not image.startswith("data:image/png;base64,") or not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 256_000:
                        raise RemoteQRLoginError("平台返回的二维码无效，请刷新重试。")
                if state.get("expired"):
                    self._update(session, "expired", "二维码已过期，请点击刷新。")
                    return
                if state.get("verification"):
                    raise RemoteQRLoginError("抖音要求额外安全验证，尚未完成登录。请完成平台验证后重试，或手动导入 Cookies。")
                if state.get("rejected"):
                    raise RemoteQRLoginError("本次抖音登录授权已取消，请重新扫码。")
                if state.get("login_error"):
                    raise RemoteQRLoginError("抖音未能完成本次登录，请稍后重新扫码。")
                if state.get("scanned"):
                    message = "已扫码，请在抖音 App 中确认登录。"
                    if session.authorization_started is not None and time.monotonic() - session.authorization_started > 15:
                        message = "已扫码；若已在手机上确认，请稍候，正在等待抖音返回登录结果。"
                    self._update(session, "scanned", message, image)
                elif state.get("confirming") or (session.image and not image):
                    message = "登录页面已更新，正在核验抖音返回的登录结果，请勿重复扫码或刷新。"
                    if session.authorization_started is not None and time.monotonic() - session.authorization_started > 20:
                        message = "尚未收到有效登录态，请检查抖音 App 是否需要额外验证；本页会继续检测。"
                    self._update(session, "confirming", message)
                elif image and session.status not in {"scanned", "confirming"}:
                    self._update(session, "waiting", "使用抖音 App 扫码，并在手机上确认登录。", image)
                elif elapsed > PAGE_LOAD_SECONDS and not session.image:
                    raise RemoteQRLoginError("抖音二维码加载失败，请刷新重试；平台可能暂时限制了服务器访问。")
                session.cancel.wait(POLL_SECONDS)
        except Exception as exc:
            if not session.cancel.is_set():
                LOGGER.warning(
                    "douyin_qr session=%s error_type=%s",
                    session.session_id[:8], type(exc.__cause__ or exc).__name__,
                )
                message = str(exc) if isinstance(exc, RemoteQRLoginError) else "扫码登录暂时不可用，请刷新重试；若持续失败请联系管理员。"
                self._update(session, "failed", message)
        finally:
            if browser:
                browser.close()


remote_qr_manager = RemoteQRLoginManager()
