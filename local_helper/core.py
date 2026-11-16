from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from itertools import count
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import websocket


VERSION = "1.0.1"
SCHEME = "videoinsight-login"
SITE = "https://206.187.222.21"
TRUSTED_SERVERS = frozenset({
    SITE, "http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:8003",
})
DOUYIN_URL = "https://www.douyin.com/"
MAX_RESPONSE = 256 * 1024


class HelperError(Exception):
    pass


class Cancelled(HelperError):
    pass


class NetworkError(HelperError):
    pass


def parse_launch_url(value: str) -> tuple[str, str]:
    if len(value) > 2048 or any(c in value for c in "\r\n\0"):
        raise HelperError("授权链接格式无效，请从网站重新打开。")
    url = urlsplit(value)
    if url.scheme != SCHEME or url.netloc != "authorize" or url.path not in {"", "/"} or url.fragment:
        raise HelperError("不支持的授权链接。")
    query = parse_qs(url.query, strict_parsing=True)
    if set(query) != {"server", "token"} or any(len(values) != 1 for values in query.values()):
        raise HelperError("授权链接格式无效。")
    server, token = query["server"][0], query["token"][0]
    if server not in TRUSTED_SERVERS:
        raise HelperError("该网站不在助手的可信列表中，已拒绝发送登录态。")
    if not 32 <= len(token) <= 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in token):
        raise HelperError("授权凭证格式无效。")
    return server, token


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HelperError("服务器发生重定向，已停止回传。请从网站重新打开助手。")


class HelperClient:
    def __init__(self, server: str):
        if server not in TRUSTED_SERVERS:
            raise HelperError("不受信任的网站。")
        self.server = server
        # Never forward the handoff credential through an HTTP redirect.
        handlers = [NoRedirects()]
        if urlsplit(server).hostname in {"localhost", "127.0.0.1"}:
            handlers.append(ProxyHandler({}))
        self.opener = build_opener(*handlers)

    def post(self, action: str, token: str, data: dict | None = None, *, failed: bool = False) -> dict:
        if action not in {"claim", "complete", "fail", "progress"}:
            raise HelperError("不支持的授权操作。")
        headers = {
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "User-Agent": "VideoInsight-Login-Helper/" + VERSION,
        }
        if failed:
            headers["X-Login-Result"] = "failed"
        request = Request(
            self.server + "/cookies/helper/" + action,
            data=json.dumps(data or {}, allow_nan=False).encode(), headers=headers, method="POST",
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise HelperError("网站响应异常。")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise HelperError("网站响应格式异常。")
            return result
        except HTTPError as exc:
            messages = {
                400: "授权内容无效，请重新登录抖音。",
                401: "授权凭证无效，请回到网站重新开始。",
                409: "授权已结束或已被替换，请回到网站重新开始。",
                410: "授权已过期或账号已变更，请重新开始。",
                413: "授权内容超过大小限制。",
                429: "操作过于频繁，请稍后重试。",
            }
            raise HelperError(messages.get(exc.code, "网站暂时无法保存授权，请稍后重试。")) from None
        except (URLError, TimeoutError, OSError):
            raise NetworkError("无法安全连接网站，请检查网络和系统时间后重试。") from None
        except (ValueError, TypeError):
            raise HelperError("网站响应格式异常。") from None


def find_browser() -> str:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for name in ("msedge", "chrome"):
        executable = shutil.which(name)
        if executable:
            candidates.append(Path(executable))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise HelperError("未找到 Microsoft Edge 或 Google Chrome，请先安装其中一个浏览器。")


def private_home() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise HelperError("无法定位当前 Windows 用户的应用目录。")
    return Path(base) / "VideoInsight" / "LoginHelper"


def douyin_login_cookies(items: list[dict]) -> list[dict]:
    now = time.time()
    result = []
    for item in items:
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if domain != "douyin.com" and not domain.endswith(".douyin.com"):
            continue
        if not item.get("name") or not item.get("value"):
            continue
        expires = float(item.get("expires", -1))
        if expires > 0 and expires <= now:
            continue
        result.append({key: item[key] for key in (
            "name", "value", "domain", "path", "secure", "httpOnly", "expires",
        ) if key in item})
    if not any(item["name"] in {"sessionid", "sid_tt", "sid_guard"} for item in result):
        return []
    return result


class DedicatedBrowser:
    """Own a fresh profile and ephemeral loopback CDP port for one handoff."""
    def __init__(self, home: Path | None = None):
        self.home = home or private_home()
        self.profile: Path | None = None
        self.process = None
        self.ws = None
        self.ids = count(1)

    def arguments(self, executable: str) -> list[str]:
        return [
            executable, "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            "--user-data-dir=" + str(self.profile), "--no-first-run", "--no-default-browser-check",
            "--disable-default-apps", "--new-window", DOUYIN_URL,
        ]

    def open(self, cancelled: threading.Event):
        if cancelled.is_set():
            raise Cancelled("已取消登录。")
        root = self.home / "sessions"
        root.mkdir(parents=True, exist_ok=True)
        self.profile = Path(tempfile.mkdtemp(prefix="login-", dir=root))
        arguments = self.arguments(find_browser())
        if sys.platform == "win32":
            from local_helper.process import OwnedProcess
            self.process = OwnedProcess(arguments)
        else:
            self.process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        active = self.profile / "DevToolsActivePort"
        while time.monotonic() < deadline:
            if cancelled.wait(0.25):
                raise Cancelled("已取消登录。")
            if not active.is_file():
                continue
            try:
                lines = active.read_text(encoding="utf-8").splitlines()
                port = int(lines[0])
                path = lines[1]
                if not 1 <= port <= 65535 or not path.startswith("/devtools/browser/") or "?" in path:
                    continue
                self.ws = websocket.create_connection(
                    f"ws://127.0.0.1:{port}{path}", timeout=8,
                    suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"],
                )
                return
            except (OSError, ValueError, IndexError, websocket.WebSocketException):
                continue
        raise HelperError("登录窗口连接超时，请关闭助手后重试。")

    def call(self, method: str, params: dict | None = None) -> dict:
        if self.ws is None:
            raise HelperError("登录窗口尚未连接。")
        message_id = next(self.ids)
        try:
            self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                message = json.loads(self.ws.recv())
                if message.get("id") != message_id:
                    continue
                if "error" in message:
                    raise HelperError("登录窗口暂时无法读取授权，请重试。")
                return message.get("result", {})
        except (OSError, ValueError, websocket.WebSocketException):
            raise HelperError("登录窗口已关闭或连接中断。") from None
        raise HelperError("读取登录窗口超时。")

    def cookies(self) -> list[dict]:
        return douyin_login_cookies(self.call("Storage.getCookies").get("cookies", []))

    def _profile_to_remove(self) -> Path | None:
        if self.profile is None:
            return None
        home = Path(os.path.abspath(self.home))
        root = home / "sessions"
        target = Path(os.path.abspath(self.profile))
        if target.parent != root or not target.name.startswith("login-"):
            raise HelperError("临时浏览器目录异常，已停止清理。")
        for directory in (home, root, target):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                return None
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or directory.resolve() != directory
            ):
                raise HelperError("临时浏览器目录异常，已停止清理。")
        return target

    def _cleanup_profile(self):
        last_error = None
        # A closed browser job can release child file handles asynchronously.
        for attempt in range(25):
            try:
                target = self._profile_to_remove()
                if target is None:
                    return
                shutil.rmtree(target)
            except OSError as exc:
                last_error = exc
            try:
                if self._profile_to_remove() is None:
                    return
            except OSError as exc:
                last_error = exc
            if attempt < 24:
                time.sleep(0.4)
        raise HelperError("临时浏览器目录未能清理，请关闭助手后检查本地助手目录。") from last_error

    def close(self):
        try:
            if self.ws is not None:
                try:
                    self.call("Browser.close")
                except HelperError:
                    pass
                finally:
                    try:
                        self.ws.close()
                    except (OSError, websocket.WebSocketException):
                        pass
                    self.ws = None
        finally:
            try:
                if self.process is not None:
                    try:
                        try:
                            self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.process.terminate()
                            try:
                                self.process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                self.process.kill()
                                self.process.wait(timeout=5)
                    finally:
                        if hasattr(self.process, "close"):
                            self.process.close()
            finally:
                self._cleanup_profile()


def capture_and_save(
    client: HelperClient, claim: dict, cancelled: threading.Event, update,
    *, browser_factory=DedicatedBrowser,
) -> dict:
    browser = browser_factory()
    token = claim["upload_token"]
    deadline = time.monotonic() + min(int(claim["expires_in"]), 600)
    result = None
    try:
        if cancelled.is_set():
            raise Cancelled("已取消登录。")
        client.post("progress", token)
        if cancelled.is_set():
            raise Cancelled("已取消登录。")
        update("正在打开专用抖音登录窗口…")
        browser.open(cancelled)
        update("请在抖音窗口完成登录，成功后将自动保存。")
        next_check = 0
        network_failures = 0
        while time.monotonic() < deadline:
            if cancelled.is_set():
                raise Cancelled("已取消登录。")
            if time.monotonic() >= next_check:
                try:
                    client.post("progress", token)
                    network_failures = 0
                except NetworkError:
                    network_failures += 1
                    if network_failures >= 3:
                        raise
                    update("与网站连接暂时中断，正在重连…")
                next_check = time.monotonic() + 5
            items = browser.cookies()
            if items:
                if cancelled.is_set():
                    raise Cancelled("已取消登录。")
                update("已检测到抖音登录，正在保存到网站账号…")
                try:
                    result = client.post("complete", token, {"cookies": items})
                except NetworkError:
                    if cancelled.wait(1):
                        raise Cancelled("已取消登录。") from None
                    result = client.post("complete", token, {"cookies": items})
                if not result.get("saved"):
                    raise HelperError("网站未确认保存成功，请重新授权。")
                return result
            cancelled.wait(2)
        raise HelperError("本次授权已过期，请回到网站重新开始。")
    except BaseException as exc:
        try:
            client.post("fail", token, failed=not isinstance(exc, Cancelled))
        except HelperError:
            pass
        raise
    finally:
        failed = sys.exc_info()[0] is not None
        try:
            browser.close()
        except (HelperError, OSError, subprocess.SubprocessError):
            if not failed:
                if result and result.get("saved"):
                    result["cleanup_warning"] = True
                else:
                    raise
