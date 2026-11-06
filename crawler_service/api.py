from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from crawler_service.runtime import PROJECT_ROOT, runtime_status, state_dir
from crawler_service.store import CommentScope, CommentSort, PLATFORMS, Store

STATE = state_dir()
store = Store(STATE / "comments.db")
pool = ThreadPoolExecutor(max_workers=1)
lock = threading.RLock()
scheduled: set[str] = set()
processes: dict[str, subprocess.Popen] = {}


@asynccontextmanager
async def lifespan(_):
    store.recover()
    yield
    with lock:
        for process in processes.values():
            stop_process(process)
    pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def authorize(request: Request, call_next):
    if request.url.path != "/health":
        token = os.environ.get("COMMENT_CRAWLER_TOKEN", "")
        given = request.headers.get("authorization", "")
        if not token or not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
            return Response(status_code=401)
    return await call_next(request)


def owned(job_id: str, owner: str):
    job = store.job(job_id, owner)
    if not job:
        raise HTTPException(status_code=404, detail="评论任务不存在。")
    return job


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        process.terminate()


def execute(job_id: str):
    try:
        with lock:
            job = store.job(job_id)
            if job["status"] != "queued":
                return
            store.status(job_id, "running", "正在逐页抓取一级评论和子评论。")
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
            env = {**os.environ, "COMMENT_CRAWLER_STATE": str(STATE)}
            process = subprocess.Popen([sys.executable, "-m", "crawler_service.worker", job_id],
                                       cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, **options)
            processes[job_id] = process
        code = process.wait()
        job = store.job(job_id)
        if job["status"] == "running":
            store.status(job_id, "partial" if job["total"] else "failed", f"抓取进程已中断（退出码 {code}），可点击继续。")
    except Exception as exc:
        job = store.job(job_id)
        if job and job["status"] == "running":
            store.status(job_id, "partial" if job["total"] else "failed",
                         f"无法启动抓取进程（{type(exc).__name__}），请检查 MediaCrawler 运行环境后继续。")
    finally:
        with lock:
            processes.pop(job_id, None)
            scheduled.discard(job_id)


def schedule(job_id: str):
    with lock:
        if job_id not in scheduled:
            scheduled.add(job_id)
            pool.submit(execute, job_id)


class Start(BaseModel):
    url: str = Field(min_length=5, max_length=5000)
    owner: str = Field(max_length=200)


class Cookies(BaseModel):
    content: str = Field(max_length=200000)


class BrowserImport(BaseModel):
    owner: str = Field(max_length=200)
    snapshot: dict


@app.get("/health")
def health():
    return {"status": "ok", "platforms": PLATFORMS, "runtime": runtime_status()}


@app.get("/platforms")
def platforms():
    return [{"id": platform, "name": name, "cookies_configured": (STATE / "cookies" / f"{platform}.txt").exists()} for platform, name in PLATFORMS.items()]


@app.put("/platforms/{platform}/cookies")
def save_cookies(platform: str, payload: Cookies):
    if platform not in PLATFORMS:
        raise HTTPException(404)
    text = payload.content.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "cookies" in data:
                data = data["cookies"]
            if isinstance(data, dict):
                text = "; ".join(f"{k}={v}" for k, v in data.items())
            else:
                domain = {"dy": "douyin.com", "ks": "kuaishou.com", "bili": "bilibili.com", "xhs": "xiaohongshu.com"}[platform]
                text = "; ".join(f"{r['name']}={r['value']}" for r in data if isinstance(r, dict) and r.get("name") and (not r.get("domain") or str(r["domain"]).lstrip(".") == domain or str(r["domain"]).endswith("." + domain)))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Cookies JSON 格式无效。")
    elif "\t" in text:
        domain = {"dy": "douyin.com", "ks": "kuaishou.com", "bili": "bilibili.com", "xhs": "xiaohongshu.com"}[platform]
        rows = [line.removeprefix("#HttpOnly_").split("\t") for line in text.splitlines() if not line.startswith("#") or line.startswith("#HttpOnly_")]
        text = "; ".join(f"{r[5]}={r[6]}" for r in rows if len(r) >= 7 and (r[0].lstrip(".") == domain or r[0].endswith("." + domain)))
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    if not text or "=" not in text or any(c in text for c in ("\r", "\n", "\x00")):
        raise HTTPException(400, "请导入 Cookie 请求头、Netscape 文件或浏览器导出的 JSON。")
    path = STATE / "cookies" / f"{platform}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp-" + secrets.token_hex(8))
    temporary.touch(mode=0o600)
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return {"saved": True}


@app.delete("/platforms/{platform}/cookies")
def delete_cookies(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(404)
    (STATE / "cookies" / f"{platform}.txt").unlink(missing_ok=True)
    return {"deleted": True}


@app.post("/jobs", status_code=202)
def start(payload: Start):
    try:
        job = store.create(payload.owner, payload.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    schedule(job["id"])
    return job


@app.get("/jobs")
def jobs(owner: str):
    return store.list_jobs(owner)


@app.post("/jobs/import-browser", status_code=201)
def import_browser(payload: BrowserImport):
    from crawler_service.browser_import import import_capture

    try:
        return import_capture(store, payload.owner, payload.snapshot)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(400, "采集文件格式或校验数据无效，请选择 snapshot.json。") from exc


@app.get("/jobs/{job_id}")
def get_job(job_id: str, owner: str):
    return owned(job_id, owner)


@app.post("/jobs/{job_id}/retry", status_code=202)
def retry(job_id: str, owner: str):
    with lock:
        job = owned(job_id, owner)
        if job.get("source") == "local-browser":
            raise HTTPException(400, "本机采集任务请在本机续抓后重新导入 snapshot.json。")
        if job_id in scheduled or job["status"] in {"queued", "running", "completed"}:
            return job
        store.status(job_id, "queued", "从已保存的翻页位置继续。")
        schedule(job_id)
    return owned(job_id, owner)


@app.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, owner: str):
    with lock:
        owned(job_id, owner)
        store.status(job_id, "cancelled", "已停止，已保存的评论仍可浏览和导出。")
        process = processes.get(job_id)
        if process and process.poll() is None:
            stop_process(process)
    return owned(job_id, owner)


@app.get("/jobs/{job_id}/comments")
def comments(job_id: str, owner: str, offset: int = 0, limit: int = 100,
             sort: CommentSort = "collected", scope: CommentScope = "all",
             root_id: str | None = Query(default=None, min_length=1, max_length=200),
             top: int | None = Query(default=None, ge=1, le=1000)):
    job = owned(job_id, owner)
    matched = store.comment_count(job_id, scope=scope, root_id=root_id)
    return {"items": store.comments(job_id, max(0, offset), max(1, min(limit, 200)), sort=sort,
                                    scope=scope, root_id=root_id, top=top),
            "total": min(matched, top) if top is not None else matched,
            "matched_total": matched, "job_total": job["total"]}


@app.get("/jobs/{job_id}/export")
def export(job_id: str, owner: str, sort: CommentSort = "collected", scope: CommentScope = "all",
           root_id: str | None = Query(default=None, min_length=1, max_length=200),
           top: int | None = Query(default=None, ge=1, le=1000)):
    owned(job_id, owner)

    def lines():
        offset = 0
        while rows := store.comments(job_id, offset, 500, sort=sort, scope=scope, root_id=root_id, top=top):
            for row in rows:
                yield json.dumps(row, ensure_ascii=False) + "\n"
            offset += len(rows)

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="comments-{job_id}.jsonl"'})
