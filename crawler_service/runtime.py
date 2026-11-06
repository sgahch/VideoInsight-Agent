"""Paths and an offline smoke check for the isolated MediaCrawler runtime."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_REVISION = "8f583999006d5b692272c608440e193ca282e73b"
UPSTREAM_REPOSITORY = "https://github.com/NanmiCoder/MediaCrawler.git"
CLIENTS = {
    "dy": ("douyin", "DouYinClient", ("get_aweme_comments", "get_sub_comments")),
    "ks": ("kuaishou", "KuaiShouClient", ("get_video_comments", "get_video_sub_comments")),
    "bili": ("bilibili", "BilibiliClient", ("get_video_comments", "get_video_level_two_comments")),
    "xhs": ("xhs", "XiaoHongShuClient", ("get_note_comments", "get_note_sub_comments")),
}


def upstream_dir() -> Path:
    default = Path("/opt/MediaCrawler")
    if not default.is_dir():
        default = PROJECT_ROOT / ".vendor" / "MediaCrawler"
    return Path(os.environ.get("MEDIACRAWLER_DIR") or default).expanduser().resolve()


def state_dir() -> Path:
    default = Path("/state") if Path("/state").is_dir() else PROJECT_ROOT / "data" / "comments"
    return Path(os.environ.get("COMMENT_CRAWLER_STATE") or default).expanduser().resolve()


def browser_options() -> dict:
    channel = os.environ.get("COMMENT_CRAWLER_BROWSER_CHANNEL", "").strip()
    if channel not in {"", "chrome", "msedge"}:
        raise ValueError("COMMENT_CRAWLER_BROWSER_CHANNEL must be chrome, msedge or empty.")
    return {"channel": channel} if channel else {}


def runtime_status() -> dict:
    source = upstream_dir()
    return {
        "engine": "MediaCrawler",
        "expected_revision": UPSTREAM_REVISION,
        "source_installed": (source / "config" / "__init__.py").is_file() and (source / "LICENSE").is_file(),
        "playwright_installed": importlib.util.find_spec("playwright") is not None,
        "node_installed": shutil.which("node") is not None,
    }


def check_runtime() -> dict:
    result = runtime_status()
    result["ready"] = False
    if not all(result[key] for key in ("source_installed", "playwright_installed", "node_installed")):
        result["error"] = "Run python -m crawler_service.manage install first."
        return result
    original_dir = Path.cwd()
    try:
        source = upstream_dir()
        os.chdir(source)
        sys.path.insert(0, str(source))
        for platform, (module, client_name, methods) in CLIENTS.items():
            importlib.import_module(f"media_platform.{module}.core")
            client = getattr(importlib.import_module(f"media_platform.{module}.client"), client_name)
            for method in methods:
                if not callable(getattr(client, method, None)):
                    raise RuntimeError(f"Incompatible {platform} client: missing {method}")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, **browser_options())
            browser.close()
        result["ready"] = True
        result["clients_checked"] = list(CLIENTS)
        result["chromium_started"] = True
        result["browser"] = os.environ.get("COMMENT_CRAWLER_BROWSER_CHANNEL") or "chromium"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        os.chdir(original_dir)
    return result


if __name__ == "__main__":
    report = check_runtime()
    print(json.dumps(report, ensure_ascii=True))
    raise SystemExit(0 if report["ready"] else 1)
