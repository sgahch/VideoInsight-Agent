from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from fastapi import Cookie, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.cleanup import remove_task_artifacts, run_cleanup
from app.config import settings
from app.account_auth import ACCOUNT_OWNER_PREFIX, account_owner, login_required, router as account_router
from app.comments_api import router as comments_router
from app.adapters.asr import format_transcript
from app.adapters.downloader import YtDlpDownloader, default_cookies_file, resolve_cookies_file, select_downloader
from app.adapters.llm import MockLlmAnalyzer, OpenAICompatibleAnalyzer
from app.browser_cookies import BrowserCookieError, capture_douyin_cookies, cookie_browser_capabilities, cookie_browser_status, start_cookie_browser
from app.cookies import summarize_cookie_file, write_normalized_cookie_file
from app.errors import VideoInsightError
from app.qr_login import QRLoginError, check_qr_login, start_qr_login
from app.remote_qr import RemoteQRLoginError, remote_qr_manager
from app.link_parser import parse_share_text
from app.schemas import (
    AnalysisMode,
    AnalysisResult,
    AnalyzeRequest,
    AnalyzeResponse,
    AuthSessionRequest,
    ChatRequest,
    ChatResponse,
    LlmTestRequest,
    ParseLinkRequest,
    TaskRecord,
    TaskStatus,
    TaskSummary,
)
from app.security import (
    AUTH_COOKIE_MAX_AGE,
    AUTH_COOKIE_NAME,
    auth_middleware,
    token_matches,
)
from app.task_store import task_store
from app.task_queue import PendingTask, TaskQueue, TaskQueueFull, TaskRunning, reconcile_interrupted_tasks
from app.workflow import detect_platform, format_duration, run_analysis_task, run_file_analysis_task
from app.progress import get_snapshot
from app.streaming import STREAM_HEADERS, encode_event


LOGGER = logging.getLogger(__name__)

# 上传落盘的读写块大小。
UPLOAD_CHUNK_BYTES = 1 << 20

# 长任务走专用有界线程池，不再用 BackgroundTasks 挤占请求处理线程池。
task_queue = TaskQueue(
    settings.max_concurrent_tasks,
    core_workers=settings.core_task_workers,
    queue_capacity=settings.task_queue_capacity,
)


def _client_id_from_header(x_client_id: str | None = None) -> str | None:
    """从请求头取客户端身份标识，去空白；空串视为未提供。"""
    if account_owner():
        return account_owner()
    value = (x_client_id or "").strip()
    if value.startswith(ACCOUNT_OWNER_PREFIX):
        raise HTTPException(403, "任务账号身份不能通过客户端指定。")
    return value or None


CLIENT_COOKIE_NAME = "videoinsight_client_id"


def _client_id_from_media_request(
    x_client_id: str | None,
    client_cookie: str | None,
) -> str | None:
    return _client_id_from_header(x_client_id) or _client_id_from_header(client_cookie)


def _owned_task(task_id: str, client_id: str | None) -> TaskRecord | None:
    """按归属取任务：请求身份必须与任务归属**严格一致**，否则视为不存在。

    无归属（client_id=NULL）的任务只对同样无身份的请求可见。这里此前写作
    `if task.request.client_id and task.request.client_id != client_id`，
    等于把无归属任务对任意身份开放——任何带 X-Client-Id 的调用方都能读取、
    删除，并能对它发起 /chat（消耗服务端 LLM 额度）。
    """
    task = task_store.get(task_id)
    if task is None:
        return None
    if task.request.client_id != client_id:
        return None
    return task


def _resume_task(item: PendingTask) -> None:
    """按任务类型分派重新入队的工作（重启对账用）。"""
    if item.media_path is None:
        task_queue.submit(
            item.task_id, task_store, run_analysis_task, item.task_id, item.request, task_store
        )
    else:
        task_queue.submit(
            item.task_id,
            task_store,
            run_file_analysis_task,
            item.task_id,
            item.request,
            item.media_path,
            task_store,
        )


def _run_artifact_cleanup() -> None:
    """同步执行一次产物回收，失败只记日志——回收绝不能影响服务启动。"""
    try:
        report = run_cleanup(task_store, settings)
    except Exception as exc:  # noqa: BLE001 - 回收是尽力而为的后台动作
        LOGGER.warning("产物回收失败，已跳过：%s", exc)
        return
    if report.dirs_removed or report.files_removed or report.tasks_removed:
        LOGGER.info(
            "产物回收完成：目录 %s 个、中间文件 %s 个、任务记录 %s 条、释放 %.1f MB",
            report.dirs_removed,
            report.files_removed,
            report.tasks_removed,
            report.megabytes_reclaimed,
        )
    for error in report.errors:
        LOGGER.warning("产物回收遗留错误：%s", error)


async def _periodic_cleanup() -> None:
    """按 cleanup_interval_hours 周期回收产物；<=0 表示关闭。"""
    interval_seconds = settings.cleanup_interval_hours * 3600
    if interval_seconds <= 0:
        return
    while True:
        await asyncio.sleep(interval_seconds)
        # 回收是阻塞 IO，丢到线程里跑，别卡住事件循环。
        await asyncio.to_thread(_run_artifact_cleanup)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.ensure_dirs()
    # 挂载事件循环异常处理器，屏蔽客户端断连噪音（WinError 10054）。
    _install_loop_exception_handler()
    # 启动时先对账一次：把上一次运行遗留的孤儿目录清掉，
    # 并把被杀死的进程留下的僵尸任务收敛掉（running → 失败，queued → 重新入队）。
    await asyncio.to_thread(_run_artifact_cleanup)
    report = await asyncio.to_thread(reconcile_interrupted_tasks, task_store, settings)
    async def resume_pending() -> None:
        for item in report.pending:
            while True:
                try:
                    _resume_task(item)
                    break
                except TaskQueueFull:
                    await asyncio.sleep(0.5)

    recovery_task = asyncio.create_task(resume_pending())
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    try:
        yield
    finally:
        recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await recovery_task
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await asyncio.to_thread(remote_qr_manager.shutdown)
        # 不等待：正在跑的下载/转写没法安全打断，留给进程退出处理。
        task_queue.shutdown(wait=False)


app = FastAPI(
    title="VideoInsight Agent",
    version="0.1.0",
    description="Phase 1 MVP for video link transcription and structured analysis.",
    lifespan=lifespan,
)


def _suppress_client_disconnects(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """屏蔽 Windows Proactor 事件循环里的客户端主动断连噪音。

    浏览器在拖动进度条、取消视频/音频流式下载时会直接重置 TCP 连接，Starlette
    的 FileResponse 流式发送随即触发 ConnectionResetError / BrokenPipeError。这些
    异常逃逸到 Proactor 的 `_call_connection_lost` 回调后，Python 3.13 会打印整段
    traceback，并在累积后导致 uvicorn 进程退出——但它们并非服务端错误。

    这里把它们降级为 debug 日志，避免污染输出、也避免进程被这类噪音拖垮。
    """
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        LOGGER.debug("Ignored client disconnect: %s", exc)
        return
    loop.default_exception_handler(context)


def _install_loop_exception_handler() -> None:
    """在事件循环上挂载断连抑制器（幂等，随主循环启动时调用）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.set_exception_handler(_suppress_client_disconnects)



@app.middleware("http")
async def persist_client_session(request: Request, call_next):
    response = await call_next(request)
    if account_owner():
        return response
    if request.headers.get("x-client-id", "").strip().startswith(ACCOUNT_OWNER_PREFIX):
        return response
    client_id = _client_id_from_header(request.headers.get("x-client-id"))
    if client_id and request.cookies.get(CLIENT_COOKIE_NAME) != client_id:
        response.set_cookie(
            CLIENT_COOKIE_NAME,
            client_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="strict",
        )
    return response


# 放在 persist_client_session 之后注册 —— Starlette 里后注册的中间件在最外层，
# 因此鉴权会先于其他任何处理执行，未授权请求不会走到业务逻辑。
app.middleware("http")(auth_middleware)
app.include_router(account_router)
app.include_router(comments_router)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"


def _cookie_storage_path() -> Path:
    """Keep the API endpoint aligned with the downloader's configured cookie path."""
    return settings.ytdlp_cookies_file or default_cookies_file(settings)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
def web_app() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/view/{task_id}", include_in_schema=False)
def task_view(task_id: str) -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "mock_mode": settings.mock_mode,
        "asr_provider": settings.asr_provider,
        "asr_model": settings.asr_model,
        # 前端据此决定是否要求用户填访问令牌
        "auth_required": settings.auth_token is not None or login_required(),
        "login_required": login_required(),
        "tmp_dir": str(settings.tmp_dir),
        "results_dir": str(settings.results_dir),
        "media_dir": str(settings.media_dir),
        "cookies_file": bool(resolve_cookies_file(settings)),
        "cookies_from_browser": settings.ytdlp_cookies_from_browser,
        # Report configuration only; platform access is checked by each crawl job.
        "comment_source": (
            "mediacrawler-service" if settings.comment_crawler_url and settings.comment_crawler_token
            else "douyin-builtin+api" if settings.media_crawler_api_url
            else "douyin-builtin+file" if settings.media_crawler_output_dir
            else "douyin-builtin"
        ),
    }


@app.post("/auth/session")
def create_auth_session(payload: AuthSessionRequest, response: Response) -> dict[str, object]:
    """用访问令牌换取 HttpOnly Cookie。

    浏览器的 `<video src>` / `<a href>` 没法带自定义请求头，媒体预览和下载必须
    靠 Cookie 才能通过鉴权。这个端点是公开的 —— 它本身就是校验令牌的地方。
    """
    expected = settings.auth_token
    if expected is None:
        if login_required():
            raise HTTPException(status_code=401, detail="请使用账号密码登录。")
        return {"authenticated": True, "auth_required": False}
    if not token_matches(payload.token, expected):
        raise HTTPException(status_code=401, detail="访问令牌不正确。")
    response.set_cookie(
        AUTH_COOKIE_NAME,
        payload.token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"authenticated": True, "auth_required": True}


@app.delete("/auth/session")
def delete_auth_session(response: Response) -> dict[str, object]:
    response.delete_cookie(AUTH_COOKIE_NAME)
    return {"authenticated": False}


@app.get("/config")
def public_model_config() -> dict[str, object]:
    """Authenticated UI configuration; credentials never leave the server."""
    provider = settings.llm_provider.lower()
    key = settings.deepseek_api_key if provider == "deepseek" else settings.openai_api_key if provider == "openai" else settings.deepseek_api_key or settings.openai_api_key
    return {"llm": {"configured": bool(key) and provider != "mock", "provider": provider, "base_url": settings.llm_base_url, "model": settings.llm_model, "protocol": settings.llm_protocol}}


@app.post("/parse-link")
def parse_link(request: ParseLinkRequest) -> dict[str, object]:
    """从分享文案/纯链接中解析出干净 URL、平台、作者、标题与标准化标签。

    前端在用户输入时实时调用，用于预览与编辑标签；后端 /analyze 也会自动归一化，
    因此即使直接提交原始文案也能正常工作。
    """
    return parse_share_text(request.text).to_dict()


@app.post("/analyze", response_model=AnalyzeResponse, status_code=202)
def submit_analysis(
    request: AnalyzeRequest,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> AnalyzeResponse:
    if request.client_id and request.client_id.startswith(ACCOUNT_OWNER_PREFIX) and not account_owner():
        raise HTTPException(403, "任务账号身份不能通过客户端指定。")
    if account_owner() or not request.client_id:
        # 允许请求体显式携带，也允许通过 header 传递；取其一
        request.client_id = _client_id_from_header(x_client_id)

    # 分享文案归一化：从“复制打开抖音，看看【...】《...》... https://... lcN:/”中
    # 自动裁剪出干净链接；并自动填充从文案提取的标题/作者作为标签。
    parsed = parse_share_text(request.url)
    if parsed.url:
        request.url = parsed.url
        if not request.label and parsed.label:
            request.label = parsed.label
    task = task_store.create(request)
    try:
        task_queue.submit(task.task_id, task_store, run_analysis_task, task.task_id, request, task_store)
    except TaskQueueFull as exc:
        task_store.delete(task.task_id)
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "3"}) from exc
    return AnalyzeResponse(
        task_id=task.task_id,
        status=task.status,
        status_url=f"/tasks/{task.task_id}",
    )


@app.post("/analyze-file", response_model=AnalyzeResponse, status_code=202)
def submit_file_analysis(
    file: UploadFile = File(...),
    mode: AnalysisMode = Form(AnalysisMode.summary),
    use_mock: bool | None = Form(None),
    llm_provider: str | None = Form(None),
    llm_base_url: str | None = Form(None),
    llm_model: str | None = Form(None),
    llm_api_key: str | None = Form(None),
    llm_protocol: str | None = Form(None),
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> AnalyzeResponse:
    request = AnalyzeRequest(
        url=f"upload://{file.filename or 'uploaded-media'}",
        label=file.filename,
        mode=mode,
        use_mock=use_mock,
        client_id=_client_id_from_header(x_client_id),
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_protocol=llm_protocol,
    )
    task = task_store.create(request)
    try:
        media_path = _save_upload(file, task.task_id)
    except BaseException:
        # 上传失败不能留下一条永远停在 queued 的僵尸记录（此前会留）。
        task_store.delete(task.task_id)
        remove_task_artifacts(settings, task.task_id)
        raise
    try:
        task_queue.submit(
            task.task_id, task_store, run_file_analysis_task, task.task_id, request, media_path, task_store
        )
    except TaskQueueFull as exc:
        task_store.delete(task.task_id)
        remove_task_artifacts(settings, task.task_id)
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "3"}) from exc
    return AnalyzeResponse(
        task_id=task.task_id,
        status=task.status,
        status_url=f"/tasks/{task.task_id}",
    )


@app.post("/cookies")
def upload_cookies(file: UploadFile = File(...)) -> dict[str, object]:
    target = _cookie_storage_path()
    raw = file.file.read()
    try:
        write_normalized_cookie_file(target, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Cookies 格式无法解析：{exc}") from exc
    return {
        "saved": True,
        "path": str(target),
        "size": target.stat().st_size,
        **summarize_cookie_file(target),
    }


@app.get("/cookies/status")
def get_cookies_status() -> dict[str, object]:
    return {
        **summarize_cookie_file(_cookie_storage_path()),
        "authorization": cookie_browser_capabilities(settings),
    }


@app.post("/cookies/qr/start")
def start_qr_cookie_login(
    response: Response, x_client_id: str | None = Header(default=None),
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return start_qr_login(settings, _client_id_from_header(x_client_id))
    except RemoteQRLoginError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except QRLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/cookies/qr/{session_id}/status")
def get_qr_cookie_login_status(
    session_id: str, response: Response, x_client_id: str | None = Header(default=None),
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return check_qr_login(session_id, settings, _client_id_from_header(x_client_id))
    except RemoteQRLoginError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except QRLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/cookies/qr/{session_id}")
def cancel_qr_cookie_login(
    session_id: str, response: Response, x_client_id: str | None = Header(default=None),
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return remote_qr_manager.cancel(session_id, _client_id_from_header(x_client_id))
    except RemoteQRLoginError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/cookies/browser/start")
def start_browser_cookie_capture() -> dict[str, object]:
    try:
        return start_cookie_browser(settings)
    except BrowserCookieError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/cookies/browser/status")
def get_browser_cookie_capture_status() -> dict[str, object]:
    try:
        return cookie_browser_status(settings)
    except BrowserCookieError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cookies/browser/capture")
def capture_browser_cookies() -> dict[str, object]:
    try:
        return capture_douyin_cookies(settings)
    except BrowserCookieError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/tasks", response_model=list[TaskRecord] | list[TaskSummary])
def list_tasks(
    status: TaskStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
    view: Literal["full", "summary"] = "full",
) -> list[TaskRecord] | list[TaskSummary]:
    listing = task_store.list_summaries if view == "summary" else task_store.list_tasks
    return listing(
        limit=limit,
        status=status,
        client_id=_client_id_from_header(x_client_id),
    )


@app.get("/queue")
def queue_overview(
    x_client_id: str | None = Header(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    client_id = _client_id_from_header(x_client_id)
    pool = task_queue.snapshot()
    positions = {task_id: index + 1 for index, task_id in enumerate(pool.pop("queued_task_ids"))}
    pool.pop("active_task_ids")
    tasks = task_store.list_summaries(limit=limit, client_id=client_id)
    seen = {task.task_id for task in tasks}
    for status in (TaskStatus.running, TaskStatus.queued):
        for task in task_store.list_summaries(limit=200, status=status, client_id=client_id):
            if task.task_id not in seen:
                tasks.append(task)
                seen.add(task.task_id)
    for task in tasks:
        if task.status == TaskStatus.running:
            snap = get_snapshot(task.task_id, settings.results_dir)
            if snap:
                task.progress = snap["progress"]
                task.stage = snap["stage"]
        elif task.status == TaskStatus.succeeded:
            task.progress = 100
            task.stage = "done"
        elif task.status == TaskStatus.queued:
            task.queue_position = positions.get(task.task_id)
    return {
        "pool": pool,
        "counts": task_store.status_counts(client_id),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "engine": {
            "asr": "cloud",
            "asr_provider": settings.asr_provider,
            "asr_model": settings.asr_model,
            "asr_global_workers": settings.asr_global_workers,
            "local_gpu_inference": False,
        },
    }


@app.get("/tasks/{task_id}", response_model=TaskRecord)
def get_task(
    task_id: str,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> TaskRecord:
    task = _owned_task(task_id, _client_id_from_header(x_client_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    # 运行中且存在实时追踪时，把真实进度/阶段/ETA 填入响应（Mock 或追踪缺失则留空）。
    if task.status == TaskStatus.running:
        snap = get_snapshot(task_id, settings.results_dir)
        if snap is not None:
            task.progress = snap["progress"]
            task.stage = snap["stage"]
            task.eta_seconds = snap["eta_seconds"]
            task.media_duration_seconds = snap["media_duration"]
            task.transcribe_done = snap["transcribe_done"]
            task.transcribe_total = snap["transcribe_total"]
            task.transcribe_seconds = snap["transcribe_seconds"]
            task.elapsed_seconds = snap["elapsed_seconds"]
            task.estimated_total_seconds = snap["estimated_total_seconds"]
    return task


@app.post("/tasks/{task_id}/used")
def mark_task_used(
    task_id: str,
    x_client_id: str | None = Header(default=None),
) -> dict[str, str]:
    used_at = task_store.mark_used(task_id, _client_id_from_header(x_client_id))
    if used_at is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "used_at": used_at.isoformat()}


@app.get("/tasks/{task_id}/events")
def task_events(
    task_id: str,
    x_client_id: str | None = Header(default=None),
) -> StreamingResponse:
    initial = get_task(task_id, x_client_id)

    async def events():
        task = initial
        transcript: str | None = None
        output = ""
        first = True
        while True:
            if task.status in (TaskStatus.succeeded, TaskStatus.failed):
                yield encode_event("complete", task.model_dump(mode="json"))
                return
            current_transcript = task.result.transcript if task.result else None
            current_output = task.result.output if task.result else ""
            if first or current_transcript != transcript or not current_output.startswith(output):
                yield encode_event("snapshot", task.model_dump(mode="json"))
                transcript, output, first = current_transcript, current_output, False
            else:
                if current_output != output:
                    yield encode_event("delta", {"text": current_output[len(output):]})
                    output = current_output
                progress = task.model_dump(mode="json", exclude={"result", "request", "error"})
                yield encode_event("progress", progress)
            await asyncio.sleep(0.5)
            try:
                task = await asyncio.to_thread(get_task, task_id, x_client_id)
            except HTTPException as exc:
                yield encode_event("error", {"detail": exc.detail, "status": exc.status_code})
                return

    return StreamingResponse(events(), media_type="text/event-stream", headers=STREAM_HEADERS)


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: str,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> None:
    task = _owned_task(task_id, _client_id_from_header(x_client_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        task_queue.cancel_pending(task_id, task_store)
    except TaskRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _remove_task_files(task_id)


@app.post("/tasks/{task_id}/chat", response_model=ChatResponse)
def chat_with_task(
    task_id: str,
    request: ChatRequest,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> ChatResponse:
    """基于某个已完成任务的转写稿回答用户提问（真实调用 LLM / Mock 分析器）。

    优先使用请求中携带的用户自备模型配置（llm_api_key 等）；
    未提供时回退到服务器 Settings 配置（真实或 Mock）。
    """
    llm, question, transcript, metadata = _prepare_task_chat(task_id, request, x_client_id)
    try:
        answer = llm.chat(question, transcript, metadata=metadata)
    except VideoInsightError as exc:
        raise HTTPException(status_code=502, detail=f"AI 对话失败：{exc}") from exc
    return ChatResponse(answer=answer)


def _prepare_task_chat(task_id: str, request: ChatRequest, x_client_id: str | None):
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="请输入要提问的内容。")

    task = _owned_task(task_id, _client_id_from_header(x_client_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status is not TaskStatus.succeeded or task.result is None:
        raise HTTPException(status_code=422, detail="任务尚未完成，请等待分析完成后再提问")

    transcript = _task_transcript(task.result)
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="该任务没有可用的转写稿，无法进行对话。")

    use_mock = settings.mock_mode if task.request.use_mock is None else task.request.use_mock
    if request.llm_api_key:
        # 用户自备模型：使用用户提供的 provider / base_url / model / api_key
        llm = OpenAICompatibleAnalyzer(
            settings,
            provider=request.llm_provider,
            base_url=request.llm_base_url,
            model=request.llm_model,
            api_key=request.llm_api_key,
            protocol=request.llm_protocol,
        )
    else:
        llm = MockLlmAnalyzer() if use_mock else OpenAICompatibleAnalyzer(settings)
    return llm, question, transcript, _chat_metadata(task.result)


@app.post("/tasks/{task_id}/chat/stream")
def stream_task_chat(
    task_id: str,
    request: ChatRequest,
    x_client_id: str | None = Header(default=None),
) -> StreamingResponse:
    llm, question, transcript, metadata = _prepare_task_chat(task_id, request, x_client_id)

    def events():
        try:
            for text in llm.chat_stream(question, transcript, metadata=metadata):
                yield encode_event("delta", {"text": text})
            yield encode_event("done", {})
        except VideoInsightError as exc:
            yield encode_event("error", {"detail": f"AI 对话失败：{exc}"})
        except Exception:
            LOGGER.exception("AI chat stream failed for task %s", task_id)
            yield encode_event("error", {"detail": "AI 对话暂时中断，请稍后重试。"})

    return StreamingResponse(events(), media_type="text/event-stream", headers=STREAM_HEADERS)


@app.post("/llm/test")
def test_llm_config(request: LlmTestRequest) -> dict[str, object]:
    """模型配置连通性自检：用一条极短请求验证 Base URL / 模型 / Key / 协议是否可用。

    不写入任何任务记录，Key 用完即弃（与对话链路一致，服务端不保存）。
    """
    if not request.llm_api_key.strip() and not public_model_config()["llm"]["configured"]:
        raise HTTPException(status_code=422, detail="请先填写 API Key。")
    llm = OpenAICompatibleAnalyzer(
        settings,
        provider=request.llm_provider,
        base_url=request.llm_base_url,
        model=request.llm_model,
        api_key=request.llm_api_key,
        protocol=request.llm_protocol,
    ) if request.llm_api_key.strip() else OpenAICompatibleAnalyzer(settings)
    try:
        result = llm.probe()
    except VideoInsightError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 自检接口要把任何异常转成可读结果
        return {"ok": False, "error": f"连接失败：{exc}"}
    return result


def _task_transcript(result: AnalysisResult) -> str:
    """取任务转写稿；transcript 为空时回退用 segments 拼接（保留时间戳）。"""
    if result.transcript.strip():
        return result.transcript
    return format_transcript(result.segments, include_timestamps=True)


def _chat_metadata(result: AnalysisResult) -> dict[str, str]:
    media = result.media
    return {
        "video_title": media.title or "未知标题",
        "author": media.author or "未知作者",
        "platform": detect_platform(media.source_url),
        "duration": format_duration(media.duration),
    }


def _remove_task_files(task_id: str) -> None:
    """删除任务的全部磁盘产物（结果 JSON、临时目录、媒体目录）。

    真正的清理逻辑在 cleanup.remove_task_artifacts —— 让删除接口和保留期回收
    共用同一份"什么算产物"的定义，避免两处规则漂移。
    """
    remove_task_artifacts(settings, task_id)


@app.get("/tasks/{task_id}/media")
def get_task_media(
    task_id: str,
    download: bool = False,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
    client_cookie: str | None = Cookie(default=None, alias=CLIENT_COOKIE_NAME),
) -> FileResponse:
    """返回任务关联的媒体文件（默认是转写用的音频/原始文件）：内联预览，?download=1 触发下载。"""
    client_id = _client_id_from_media_request(x_client_id, client_cookie)
    media_path = _resolve_media_path(_owned_task(task_id, client_id))
    if media_path is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return _media_response(media_path, download)


@app.get("/tasks/{task_id}/video")
def get_task_video(
    task_id: str,
    download: bool = False,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
    client_cookie: str | None = Cookie(default=None, alias=CLIENT_COOKIE_NAME),
) -> FileResponse:
    """返回任务的完整视频：已存文件是视频时直接返回；链接任务只存了音频时按需下载源视频。"""
    client_id = _client_id_from_media_request(x_client_id, client_cookie)
    task = _owned_task(task_id, client_id)
    stored = _resolve_media_path(task)
    if stored is None:
        raise HTTPException(status_code=404, detail="Media not found")

    if _is_video_extension(stored) or str(task.request.url).startswith("upload://"):
        return _media_response(stored, download)

    try:
        video_path = _ensure_source_video(task, stored)
    except VideoInsightError as exc:
        raise HTTPException(status_code=422, detail=f"源视频下载失败：{exc}") from exc
    return _media_response(video_path, download)


def _resolve_media_path(task: TaskRecord | None) -> Path | None:
    if task is None or task.result is None or not task.result.media.audio_path:
        return None
    media_path = Path(task.result.media.audio_path)
    if not media_path.is_absolute():
        media_path = PROJECT_ROOT / media_path
    return media_path if media_path.is_file() else None


def _is_video_extension(path: Path) -> bool:
    return path.suffix.lower() in {".mp4", ".webm", ".m4v", ".mov", ".ogv", ".mkv", ".avi", ".flv", ".ts"}


def _media_response(path: Path, download: bool) -> FileResponse:
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="attachment" if download else "inline",
    )


def _ensure_source_video(task: TaskRecord, stored: Path) -> Path:
    task_dir = stored.parent
    cached = sorted(task_dir.glob("*.video.mp4"))
    if cached:
        return cached[0]
    url = str(task.request.url)
    # 抖音链接走分享页下载器（yt-dlp 被 a_bogus 签名墙拦截）；其余平台维持 yt-dlp。
    downloader = select_downloader(url, settings.tmp_dir, use_mock=False)
    if not hasattr(downloader, "download_video"):
        downloader = YtDlpDownloader(settings.tmp_dir)
    return downloader.download_video(url, task.task_id)


def _save_upload(file: UploadFile, task_id: str) -> Path:
    """把上传文件落到 media/<task_id>/，超过上限立即中止并删掉半成品。

    必须边写边计数：Content-Length 由客户端提供、不可信，而"先整份落盘再判断
    大小"等于让一个请求写满磁盘。上传端点没有鉴权，这是唯一的守门人。
    """
    original_name = Path(file.filename or "uploaded-media").name
    max_bytes = settings.max_upload_mb * 1024 * 1024
    target_dir = settings.media_dir / task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / original_name
    written = 0
    try:
        with target.open("wb") as output:
            while chunk := file.file.read(UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {settings.max_upload_mb} MB 上限，请裁剪后再上传。",
                    )
                output.write(chunk)
    except BaseException:
        # 含 HTTPException 与客户端中途断开；半成品一律不留。
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target
