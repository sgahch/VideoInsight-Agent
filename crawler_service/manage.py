"""Install and run the comment service without changing the application's venv."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from crawler_service.runtime import PROJECT_ROOT, UPSTREAM_REPOSITORY, UPSTREAM_REVISION


def runtime_python() -> Path:
    directory = PROJECT_ROOT / ".venv-comments"
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run_command(command: list[str], *, env: dict | None = None) -> None:
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)


def load_environment() -> None:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")


def configure_local(port: int, browser: str = "chromium") -> None:
    from dotenv import dotenv_values, set_key

    path = PROJECT_ROOT / ".env"
    values = dotenv_values(path)
    current_url = values.get("COMMENT_CRAWLER_URL") or os.environ.get("COMMENT_CRAWLER_URL")
    if current_url and urlsplit(current_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("An external COMMENT_CRAWLER_URL is configured; it has not been overwritten.")
    updates = {
        "COMMENT_CRAWLER_URL": f"http://127.0.0.1:{port}",
        "COMMENT_CRAWLER_TOKEN": values.get("COMMENT_CRAWLER_TOKEN") or os.environ.get("COMMENT_CRAWLER_TOKEN") or secrets.token_urlsafe(48),
        "COMMENT_CRAWLER_STATE": str((PROJECT_ROOT / "data" / "comments").resolve()),
        "MEDIACRAWLER_DIR": str((PROJECT_ROOT / ".vendor" / "MediaCrawler").resolve()),
        "COMMENT_CRAWLER_BROWSER_CHANNEL": "" if browser == "chromium" else browser,
    }
    for key, value in updates.items():
        for attempt in range(4):
            try:
                set_key(str(path), key, value)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.1 * (attempt + 1))


def install(port: int, *, configure: bool = True, browser: str = "chromium") -> None:
    uv = shutil.which("uv")
    git = shutil.which("git")
    if not uv or not git or not shutil.which("node"):
        raise RuntimeError("Install uv, git and Node.js before setting up MediaCrawler.")
    if configure:
        from dotenv import dotenv_values

        url = dotenv_values(PROJECT_ROOT / ".env").get("COMMENT_CRAWLER_URL") or os.environ.get("COMMENT_CRAWLER_URL")
        if url and urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("An external comment service is configured. Use install --no-config to keep it.")
    source = PROJECT_ROOT / ".vendor" / "MediaCrawler"
    source.parent.mkdir(parents=True, exist_ok=True)
    print("MediaCrawler is licensed for non-commercial learning. Its LICENSE is retained in .vendor/MediaCrawler.", flush=True)
    if not source.exists():
        run_command([git, "clone", "--filter=blob:none", UPSTREAM_REPOSITORY, str(source)])
    revision = subprocess.check_output([git, "-C", str(source), "rev-parse", "--verify", "HEAD"], text=True).strip()
    if revision != UPSTREAM_REVISION:
        changes = subprocess.check_output([git, "-C", str(source), "status", "--porcelain"], text=True).strip()
        if changes:
            raise RuntimeError("The upstream checkout has local changes; refusing to overwrite them.")
        run_command([git, "-C", str(source), "fetch", "--depth", "1", "origin", UPSTREAM_REVISION])
        run_command([git, "-C", str(source), "checkout", "--detach", UPSTREAM_REVISION])
    python = runtime_python()
    if not python.exists():
        run_command([uv, "venv", "--python", "3.12", str(python.parent.parent)])
    run_command([uv, "pip", "install", "--python", str(python), "-r", str(PROJECT_ROOT / "crawler_service" / "requirements.txt")])
    if browser == "chromium":
        env = {**os.environ, "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "120000"}
        run_command([str(python), "-m", "playwright", "install", "chromium"], env=env)
    env = os.environ.copy()
    env["MEDIACRAWLER_DIR"] = str(source)
    env["COMMENT_CRAWLER_BROWSER_CHANNEL"] = "" if browser == "chromium" else browser
    run_command([str(python), "-m", "crawler_service.runtime"], env=env)
    if configure:
        configure_local(port, browser)
    print("MediaCrawler runtime is installed. Run python -m crawler_service.manage serve.", flush=True)


def service_available(url: str, token: str) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(url.rstrip("/") + "/health"), timeout=2) as response:
            report = json.load(response)
        if report.get("runtime", {}).get("engine") != "MediaCrawler":
            return False
        with opener.open(Request(url.rstrip("/") + "/platforms",
                                 headers={"Authorization": f"Bearer {token}"}), timeout=2) as response:
            return response.status == 200
    except (URLError, ValueError, TimeoutError, OSError):
        return False


def start_local(python: Path, port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    token = os.environ["COMMENT_CRAWLER_TOKEN"]
    if service_available(url, token):
        print(f"Comment service is already running: {url}")
        return
    logs = PROJECT_ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logfile = logs / "comments-service.log"
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with logfile.open("ab") as output:
        process = subprocess.Popen([str(python), "-m", "uvicorn", "crawler_service.api:app",
                                    "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
                                   cwd=PROJECT_ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, **options)
    for _ in range(30):
        if process.poll() is not None:
            break
        if service_available(url, token):
            print(f"Comment service started: {url}")
            return
        time.sleep(0.3)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    raise RuntimeError(f"Comment service did not start. Check {logfile}. No existing service was stopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "serve", "start", "check"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-config", action="store_true", help="Do not modify .env during installation.")
    parser.add_argument("--browser", choices=["chromium", "chrome", "msedge"], default="chromium",
                        help="Use an already installed Chrome/Edge instead of downloading Chromium.")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        if args.command == "install":
            install(args.port or 8090, configure=not args.no_config, browser=args.browser)
        else:
            load_environment()
            python = runtime_python()
            if not python.exists():
                raise RuntimeError("Run python -m crawler_service.manage install first.")
            if args.command == "check":
                run_command([str(python), "-m", "crawler_service.runtime"])
            else:
                if not os.environ.get("COMMENT_CRAWLER_TOKEN"):
                    raise RuntimeError("COMMENT_CRAWLER_TOKEN is missing; run install or configure .env.")
                url = urlsplit(os.environ.get("COMMENT_CRAWLER_URL", "http://127.0.0.1:8090"))
                if url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                    raise RuntimeError("This command starts a local service, but COMMENT_CRAWLER_URL is external.")
                port = args.port or url.port or 8090
                if args.command == "start":
                    start_local(python, port)
                else:
                    run_command([str(python), "-m", "uvicorn", "crawler_service.api:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"])
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Comment service: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
