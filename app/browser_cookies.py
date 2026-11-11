from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from urllib.error import HTTPError, URLError
from itertools import count
from pathlib import Path
from shutil import which
from time import sleep
from urllib.request import urlopen

from app.adapters.downloader import cookie_storage_path
from app.config import Settings, settings
from app.cookies import cookie_objects_to_netscape


BASE_DEBUG_PORT = int(os.getenv("COOKIE_CAPTURE_DEBUG_PORT", "9222"))
MAX_DEBUG_PORT = int(os.getenv("COOKIE_CAPTURE_MAX_DEBUG_PORT", str(BASE_DEBUG_PORT + 9)))
DOUYIN_URL = "https://www.douyin.com/"
LOGGER = logging.getLogger(__name__)
IMPORTANT_DOUYIN_COOKIES = {
    "sessionid",
    "sid_guard",
    "sid_tt",
    "passport_csrf_token",
    "odin_tt",
    "ttwid",
    "msToken",
    "s_v_web_id",
}


class BrowserCookieError(Exception):
    pass


def cookie_browser_capabilities(runtime_settings: Settings = settings) -> dict[str, object]:
    mode = runtime_settings.cookie_login_mode.strip().lower()
    if mode not in {"auto", "desktop", "import"} or mode == "import":
        reason = "configured_import"
    elif mode == "auto" and not _has_desktop_display():
        reason = "headless"
    elif _active_debug_port(runtime_settings) is None and _find_chromium() is None:
        reason = "browser_unavailable"
    else:
        return {"mode": "desktop", "desktop_available": True, "reason": None}
    return {
        "mode": "import",
        "desktop_available": False,
        "reason": reason,
        "message": (
            "当前服务没有可用的桌面登录窗口，无法在你的电脑上弹出浏览器。"
            "请上传对应平台的 Cookies 文件或粘贴导出内容。"
        ),
    }


def _has_desktop_display() -> bool:
    return sys.platform in {"win32", "darwin"} or bool(
        os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")
    )


def _require_desktop_login(runtime_settings: Settings) -> None:
    capabilities = cookie_browser_capabilities(runtime_settings)
    if not capabilities["desktop_available"]:
        raise BrowserCookieError(str(capabilities["message"]))


def start_cookie_browser(
    runtime_settings: Settings = settings,
    *,
    reset_session: bool = False,
) -> dict[str, object]:
    _require_desktop_login(runtime_settings)
    existing_port = _active_debug_port(runtime_settings)
    if existing_port is not None:
        tab_state = _ensure_douyin_tab(existing_port)
        if reset_session:
            _clear_douyin_browser_cookies(existing_port)
        if tab_state.get("opened"):
            message = "已在专用浏览器中打开抖音登录页，请用抖音 App 扫码登录。"
        else:
            message = "已找到专用浏览器中的抖音页面并切换到前台，请扫码登录。"
        return {
            "started": False,
            "running": True,
            "port": existing_port,
            "url": DOUYIN_URL,
            "opened_tab": tab_state.get("opened"),
            "focused_tab": tab_state.get("focused"),
            "message": message,
        }

    browser = _find_chromium()
    if browser is None:
        raise BrowserCookieError("未找到 Chrome 或 Edge 浏览器。")

    profile_dir = _new_profile_dir(runtime_settings).resolve()
    debug_port = _pick_free_debug_port()
    args = [
        browser,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--new-window",
        DOUYIN_URL,
    ]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    metadata_dir = _profile_dir(runtime_settings)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "browser.pid").write_text(str(process.pid), encoding="utf-8")
    (metadata_dir / "debug_port.txt").write_text(str(debug_port), encoding="utf-8")
    (metadata_dir / "current_profile.txt").write_text(str(profile_dir), encoding="utf-8")
    _wait_for_devtools(debug_port)
    return {
        "started": True,
        "running": True,
        "pid": process.pid,
        "port": debug_port,
        "profile_dir": str(profile_dir),
        "url": DOUYIN_URL,
        "message": "专用浏览器已启动并打开抖音登录页，请用抖音 App 扫码登录。",
    }


def cookie_browser_status(runtime_settings: Settings = settings) -> dict[str, object]:
    capabilities = cookie_browser_capabilities(runtime_settings)
    if not capabilities["desktop_available"]:
        return {
            **capabilities, "running": False, "port": None,
            "tabs": [], "has_douyin_tab": False,
        }
    debug_port = _active_debug_port(runtime_settings)
    if debug_port is None:
        return {
            "running": False,
            "port": _saved_debug_port(runtime_settings) or BASE_DEBUG_PORT,
            "tabs": [],
            "has_douyin_tab": False,
        }

    tabs = _get_tabs(debug_port)
    visible_tabs = [
        {
            "title": tab.get("title"),
            "url": tab.get("url"),
            "type": tab.get("type"),
        }
        for tab in tabs
        if isinstance(tab, dict)
    ]
    return {
        "running": True,
        "port": debug_port,
        "tabs": visible_tabs,
        "has_douyin_tab": any("douyin.com" in (tab.get("url") or "") for tab in tabs if isinstance(tab, dict)),
    }


def capture_douyin_cookies(runtime_settings: Settings = settings) -> dict[str, object]:
    _require_desktop_login(runtime_settings)
    debug_port = _active_debug_port(runtime_settings)
    if debug_port is None:
        raise BrowserCookieError("专用登录窗口未连接，请先点击“打开登录窗口”。")

    LOGGER.info("Starting Douyin cookie capture on DevTools port %s", debug_port)
    tabs = _get_tabs(debug_port)
    tab = _pick_douyin_tab(tabs)
    if tab is None:
        raise BrowserCookieError("没有找到抖音页面，请先点击“打开登录窗口”并完成登录。")

    LOGGER.info("Using browser tab for cookie capture: %s", tab.get("url"))
    cookies = _read_cookies_from_tab(tab["webSocketDebuggerUrl"])
    douyin_cookies = [
        cookie for cookie in cookies if "douyin.com" in cookie.get("domain", "")
    ]
    if not douyin_cookies:
        raise BrowserCookieError("没有抓到 douyin.com cookies，请确认专用浏览器窗口里已登录抖音。")

    names = {cookie.get("name") for cookie in douyin_cookies}
    has_login_cookie = bool(names & {"sessionid", "sid_guard", "sid_tt"})
    LOGGER.info("Captured %s douyin cookies. Login cookie present=%s", len(douyin_cookies), has_login_cookie)

    target = cookie_storage_path(runtime_settings)
    if has_login_cookie:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_cookies_to_netscape(douyin_cookies), encoding="utf-8")

    return {
        "saved": has_login_cookie,
        "path": str(target) if has_login_cookie else None,
        "count": len(douyin_cookies),
        "has_login_cookie": has_login_cookie,
        "present_important": sorted(name for name in IMPORTANT_DOUYIN_COOKIES if name in names),
        "missing_important": sorted(name for name in IMPORTANT_DOUYIN_COOKIES if name not in names),
    }


def _find_chromium() -> str | None:
    preferred = os.getenv("COOKIE_CAPTURE_BROWSER", "edge").strip().lower()
    chrome_candidates = [
        os.getenv("CHROME_PATH"),
        which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    edge_candidates = [
        os.getenv("EDGE_PATH"),
        which("msedge"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    if preferred == "chrome":
        candidates = chrome_candidates + edge_candidates
    else:
        candidates = edge_candidates + chrome_candidates
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _profile_dir(runtime_settings: Settings = settings) -> Path:
    return runtime_settings.media_dir / "_browser_profile"


def _new_profile_dir(runtime_settings: Settings = settings) -> Path:
    profiles_root = runtime_settings.media_dir / "_browser_profiles"
    profiles_root.mkdir(parents=True, exist_ok=True)
    saved_profile_file = _profile_dir(runtime_settings) / "current_profile.txt"
    if saved_profile_file.exists():
        try:
            saved_profile = Path(saved_profile_file.read_text(encoding="utf-8").strip())
        except OSError:
            saved_profile = None
        if saved_profile and saved_profile.is_dir():
            return saved_profile

    profile_dir = profiles_root / "douyin"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _saved_debug_port(runtime_settings: Settings = settings) -> int | None:
    port_file = _profile_dir(runtime_settings) / "debug_port.txt"
    if not port_file.exists():
        return None
    try:
        return int(port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _active_debug_port(runtime_settings: Settings = settings) -> int | None:
    saved_port = _saved_debug_port(runtime_settings)
    if saved_port is None or not _is_devtools_port(saved_port):
        return None
    return saved_port


def _is_devtools_port(port: int) -> bool:
    if not _is_tcp_open(port):
        return False
    try:
        data = _json_get(f"http://127.0.0.1:{port}/json/version", timeout=1)
    except BrowserCookieError:
        return False
    return isinstance(data, dict) and (
        "webSocketDebuggerUrl" in data or "Browser" in data
    )


def _pick_free_debug_port() -> int:
    for port in range(BASE_DEBUG_PORT, MAX_DEBUG_PORT + 1):
        if not _is_tcp_open(port):
            return port
    raise BrowserCookieError(f"{BASE_DEBUG_PORT}-{MAX_DEBUG_PORT} 端口都被占用，无法启动专用登录窗口。")


def _is_tcp_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_devtools(port: int) -> None:
    for _ in range(40):
        if _is_devtools_port(port):
            return
        sleep(0.25)
    raise BrowserCookieError(f"浏览器已启动，但 DevTools 端口 {port} 未就绪。")


def _json_get(url: str, timeout: int = 5) -> object:
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise BrowserCookieError(f"DevTools 接口返回 HTTP {exc.code}：{url}") from exc
    except URLError as exc:
        raise BrowserCookieError(f"无法连接专用浏览器 DevTools：{exc.reason}") from exc
    except TimeoutError as exc:
        raise BrowserCookieError("连接专用浏览器 DevTools 超时，请重新打开登录窗口。") from exc
    except json.JSONDecodeError as exc:
        raise BrowserCookieError(f"DevTools 返回内容不是 JSON：{url}") from exc


def _get_tabs(debug_port: int) -> list[dict]:
    errors: list[str] = []
    for path in ("/json", "/json/list"):
        try:
            tabs = _json_get(f"http://127.0.0.1:{debug_port}{path}")
            if isinstance(tabs, list):
                return tabs
            errors.append(f"{path} 返回的不是列表")
        except BrowserCookieError as exc:
            errors.append(str(exc))
    raise BrowserCookieError("无法读取专用浏览器标签页：" + "；".join(errors))


def _pick_douyin_tab(tabs: object) -> dict | None:
    if not isinstance(tabs, list):
        return None
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        if "douyin.com" in tab.get("url", "") and tab.get("webSocketDebuggerUrl"):
            return tab
    for tab in tabs:
        if isinstance(tab, dict) and tab.get("webSocketDebuggerUrl"):
            return tab
    return None


def _ensure_douyin_tab(debug_port: int) -> dict[str, object]:
    try:
        import websocket
    except ImportError as exc:
        raise BrowserCookieError("缺少 websocket-client 依赖，请运行 pip install -r requirements.txt。") from exc

    version = _json_get(f"http://127.0.0.1:{debug_port}/json/version")
    ws_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
    if not ws_url:
        raise BrowserCookieError("无法读取专用浏览器 DevTools 版本信息，请重新打开登录窗口。")

    tabs = _get_tabs(debug_port)
    douyin_tab = next(
        (tab for tab in tabs if isinstance(tab, dict) and "douyin.com" in tab.get("url", "")),
        None,
    )

    ws = websocket.create_connection(ws_url, timeout=10)
    ws.settimeout(10)
    ids = count(1)
    try:
        if douyin_tab is not None:
            target_id = douyin_tab.get("id")
            if target_id:
                _send_cdp(ws, ids, "Target.activateTarget", {"targetId": target_id})
            return {"opened": False, "focused": True, "url": douyin_tab.get("url")}
        created = _send_cdp(ws, ids, "Target.createTarget", {"url": DOUYIN_URL})
        target_id = created.get("targetId")
        if target_id:
            try:
                _send_cdp(ws, ids, "Target.activateTarget", {"targetId": target_id})
            except BrowserCookieError:
                pass
        return {"opened": True, "focused": True, "url": DOUYIN_URL}
    finally:
        ws.close()


def _read_cookies_from_tab(ws_url: str) -> list[dict]:
    try:
        import websocket
    except ImportError as exc:
        raise BrowserCookieError("缺少 websocket-client 依赖，请运行 pip install -r requirements.txt。") from exc

    LOGGER.info("Connecting to DevTools websocket")
    ws = websocket.create_connection(ws_url, timeout=10)
    ws.settimeout(10)
    ids = count(1)
    try:
        _send_cdp(ws, ids, "Network.enable")
        result = _send_cdp(ws, ids, "Network.getAllCookies")
    finally:
        ws.close()
    return result.get("cookies", [])


def _clear_douyin_browser_cookies(debug_port: int) -> None:
    try:
        import websocket
    except ImportError as exc:
        raise BrowserCookieError("缺少 websocket-client 依赖，请运行 pip install -r requirements.txt。") from exc

    tab = _pick_douyin_tab(_get_tabs(debug_port))
    if tab is None:
        raise BrowserCookieError("没有找到可重置的抖音登录页面，请重新打开登录窗口。")

    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    ws.settimeout(10)
    ids = count(1)
    try:
        _send_cdp(ws, ids, "Network.enable")
        _send_cdp(ws, ids, "Network.clearBrowserCookies")
        _send_cdp(ws, ids, "Page.navigate", {"url": DOUYIN_URL})
    finally:
        ws.close()


def _send_cdp(ws: object, ids: count, method: str, params: dict | None = None) -> dict:
    message_id = next(ids)
    ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
    while True:
        try:
            raw = ws.recv()
        except Exception as exc:
            raise BrowserCookieError(f"等待 Chrome DevTools 响应超时或失败：{method}") from exc
        message = json.loads(raw)
        if message.get("id") != message_id:
            continue
        if "error" in message:
            raise BrowserCookieError(f"Chrome DevTools 调用失败：{message['error']}")
        return message.get("result", {})


def _cookies_to_netscape(cookies: list[dict]) -> str:
    return cookie_objects_to_netscape(cookies, source="dedicated browser profile")
