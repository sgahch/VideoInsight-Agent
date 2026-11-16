from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import Settings, settings
from app.account_auth import account_owner, task_account_scope
from app.media_urls import canonical_media_url
from app.url_safety import UnsafeURL

CommentSort = Literal["collected", "likes", "newest"]
CommentScope = Literal["all", "roots", "replies"]

router = APIRouter()


@lru_cache(maxsize=1)
def service_tls_context() -> ssl.SSLContext:
    # Loading a trust store per poll is costly on Windows; verification stays enabled.
    return ssl.create_default_context()


def service_request(method: str, path: str, *, config: Settings | None = None, **kwargs):
    config = config or settings
    if not config.comment_crawler_url or not config.comment_crawler_token:
        raise HTTPException(503, "评论抓取服务尚未配置。")
    try:
        response = httpx.request(method, config.comment_crawler_url.rstrip("/") + path, headers={"Authorization": f"Bearer {config.comment_crawler_token}"}, timeout=20, trust_env=False, verify=service_tls_context(), **kwargs)
        if response.status_code >= 400:
            data = response.json()
            raise HTTPException(response.status_code, data.get("detail", "评论服务请求失败。"))
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "评论服务暂时不可用，请稍后重试。")


def private_service_params(config: Settings | None = None) -> dict:
    if not account_owner():
        return {}
    health = service_request("GET", "/health", config=config)
    if health.get("account_cookies") is not True:
        raise HTTPException(503, "评论服务尚未启用账号独立授权，已阻止使用共享 Cookies，请联系管理员升级。")
    return {"params": {"owner": account_owner()}}


def prepare_account_cookies(config: Settings) -> None:
    params = private_service_params(config)
    from app.adapters.downloader import cookie_storage_path
    from app.cookies import cookie_logout_path

    if cookie_logout_path(cookie_storage_path(config)).exists():
        service_request("DELETE", "/platforms/dy/cookies", config=config, **params)
        return
    if not params:
        return
    from app.adapters.downloader import resolve_cookies_file
    from app.cookies import summarize_cookie_file

    path = resolve_cookies_file(config)
    if path and summarize_cookie_file(path)["has_login_cookie"]:
        service_request("PUT", "/platforms/dy/cookies", config=config,
                        json={"content": path.read_text(encoding="utf-8")}, **params)


def start_for_analysis(url: str, client_id: str | None, config: Settings):
    with task_account_scope(client_id):
        if account_owner():
            try:
                url = canonical_media_url(url, resolve_short=True)
            except (UnsafeURL, httpx.HTTPError) as exc:
                raise HTTPException(400, f"评论链接无法安全解析：{exc}") from exc
        prepare_account_cookies(config)
    return service_request("POST", "/jobs", config=config, json={"url": url, "owner": client_id or ""})


def owner(request: Request) -> str:
    from app.account_auth import ACCOUNT_OWNER_PREFIX, account_owner

    if account_owner():
        return account_owner()
    value = (request.headers.get("x-client-id") or request.cookies.get("videoinsight_client_id") or "").strip()[:200]
    if value.startswith(ACCOUNT_OWNER_PREFIX):
        raise HTTPException(403, "任务账号身份不能通过客户端指定。")
    return value


@router.get("/comments", include_in_schema=False)
def comments_page():
    return FileResponse(Path(__file__).resolve().parent.parent / "web" / "comments.html")


@router.get("/comment-api/platforms")
def platforms():
    return service_request("GET", "/platforms", **private_service_params())


@router.get("/comment-api/health")
def health():
    return service_request("GET", "/health")


class CookieInput(BaseModel):
    content: str = Field(max_length=200000)


@router.put("/comment-api/platforms/{platform}/cookies")
def cookies(platform: str, payload: CookieInput):
    if platform not in {"dy", "ks", "bili", "xhs"}:
        raise HTTPException(404)
    return service_request("PUT", f"/platforms/{platform}/cookies", json=payload.model_dump(), **private_service_params())


@router.delete("/comment-api/platforms/{platform}/cookies")
def delete_cookies(platform: str):
    if platform not in {"dy", "ks", "bili", "xhs"}:
        raise HTTPException(404)
    return service_request("DELETE", f"/platforms/{platform}/cookies", **private_service_params())


class NewJob(BaseModel):
    url: str = Field(min_length=5, max_length=5000)


class BrowserImport(BaseModel):
    snapshot: dict


@router.post("/comment-api/import-browser", status_code=201)
def import_browser(payload: BrowserImport, request: Request):
    return service_request("POST", "/jobs/import-browser", json={"snapshot": payload.snapshot, "owner": owner(request)})


@router.post("/comment-api/jobs", status_code=202)
def new_job(payload: NewJob, request: Request):
    from app.link_parser import parse_share_text

    parsed = parse_share_text(payload.url)
    if not parsed.url:
        raise HTTPException(400, parsed.note)
    return start_for_analysis(parsed.url, owner(request), settings)


@router.get("/comment-api/jobs")
def jobs(request: Request):
    return service_request("GET", "/jobs", params={"owner": owner(request)})


def checked_id(job_id: str):
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(404)
    return job_id


@router.get("/comment-api/jobs/{job_id}")
def job(job_id: str, request: Request):
    return service_request("GET", f"/jobs/{checked_id(job_id)}", params={"owner": owner(request)})


@router.post("/comment-api/jobs/{job_id}/{action}")
def action(job_id: str, action: str, request: Request):
    if action not in {"retry", "cancel"}:
        raise HTTPException(404)
    if action == "retry":
        if account_owner():
            existing = service_request(
                "GET", f"/jobs/{checked_id(job_id)}", params={"owner": owner(request)},
            )
            try:
                safe_url = canonical_media_url(existing.get("url", ""), resolve_short=True)
            except (UnsafeURL, httpx.HTTPError) as exc:
                raise HTTPException(400, f"评论链接无法安全解析：{exc}") from exc
            if safe_url != existing.get("url"):
                raise HTTPException(400, "旧评论任务的链接需要更新，请使用作品的官方完整链接重新创建任务。")
        prepare_account_cookies(settings)
    return service_request("POST", f"/jobs/{checked_id(job_id)}/{action}", params={"owner": owner(request)})


@router.get("/comment-api/jobs/{job_id}/comments")
def comments(job_id: str, request: Request, offset: int = 0, limit: int = 100,
             sort: CommentSort = "collected", scope: CommentScope = "all",
             root_id: str | None = Query(default=None, min_length=1, max_length=200),
             top: int | None = Query(default=None, ge=1, le=1000)):
    params = {"owner": owner(request), "offset": max(0, offset), "limit": max(1, min(limit, 200)),
              "sort": sort, "scope": scope}
    if root_id is not None:
        params["root_id"] = root_id
    if top is not None:
        params["top"] = top
    return service_request("GET", f"/jobs/{checked_id(job_id)}/comments", params=params)


@router.get("/comment-api/jobs/{job_id}/export")
def export(job_id: str, request: Request, sort: CommentSort = "collected", scope: CommentScope = "all",
           root_id: str | None = Query(default=None, min_length=1, max_length=200),
           top: int | None = Query(default=None, ge=1, le=1000)):
    checked_id(job_id)
    if not settings.comment_crawler_url or not settings.comment_crawler_token:
        raise HTTPException(503, "评论服务尚未配置。")
    client = httpx.Client(timeout=60, trust_env=False, verify=service_tls_context())
    try:
        params = {"owner": owner(request), "sort": sort, "scope": scope}
        if root_id is not None:
            params["root_id"] = root_id
        if top is not None:
            params["top"] = top
        upstream = client.send(client.build_request("GET", settings.comment_crawler_url.rstrip("/") + f"/jobs/{job_id}/export", headers={"Authorization": f"Bearer {settings.comment_crawler_token}"}, params=params), stream=True)
        if upstream.status_code != 200:
            upstream.close()
            client.close()
            raise HTTPException(404, "评论任务不存在。")
    except httpx.HTTPError:
        client.close()
        raise HTTPException(503, "评论服务暂时不可用。")

    def chunks():
        try:
            yield from upstream.iter_bytes()
        finally:
            upstream.close()
            client.close()

    return StreamingResponse(chunks(), media_type="application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="comments-{job_id}.jsonl"'})
