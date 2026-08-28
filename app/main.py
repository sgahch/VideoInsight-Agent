from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
import shutil

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.adapters.asr import format_transcript
from app.adapters.downloader import YtDlpDownloader, default_cookies_file, resolve_cookies_file
from app.adapters.llm import MockLlmAnalyzer, OpenAICompatibleAnalyzer
from app.browser_cookies import BrowserCookieError, capture_douyin_cookies, cookie_browser_status, start_cookie_browser
from app.cookies import summarize_cookie_file, write_normalized_cookie_file
from app.errors import VideoInsightError
from app.qr_login import QRLoginError, check_qr_login, start_qr_login
from app.schemas import (
    AnalysisMode,
    AnalysisResult,
    AnalyzeRequest,
    AnalyzeResponse,
    ChatRequest,
    ChatResponse,
    TaskRecord,
    TaskStatus,
)
from app.task_store import SQLiteTaskStore, task_store
from app.workflow import detect_platform, format_duration, run_analysis_task, run_file_analysis_task


def _client_id_from_header(x_client_id: str | None = None) -> str | None:
    """从请求头取客户端身份标识，去空白；空串视为未提供。"""
    value = (x_client_id or "").strip()
    return value or None


def _owned_task(task_id: str, client_id: str | None) -> TaskRecord | None:
    """按归属取任务：无归属（老数据）或属于该客户端的任务可访问，他人任务视为不存在。"""
    task = task_store.get(task_id)
    if task is None:
        return None
    if task.request.client_id and task.request.client_id != client_id:
        return None
    return task


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.ensure_dirs()
    yield


app = FastAPI(
    title="VideoInsight Agent",
    version="0.1.0",
    description="Phase 1 MVP for video link transcription and structured analysis.",
    lifespan=lifespan,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
def web_app() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "mock_mode": settings.mock_mode,
        "tmp_dir": str(settings.tmp_dir),
        "results_dir": str(settings.results_dir),
        "media_dir": str(settings.media_dir),
        "cookies_file": bool(resolve_cookies_file(settings)),
        "cookies_from_browser": settings.ytdlp_cookies_from_browser,
    }


@app.post("/analyze", response_model=AnalyzeResponse, status_code=202)
def submit_analysis(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> AnalyzeResponse:
    if not request.client_id:
        # 允许请求体显式携带，也允许通过 header 传递；取其一
        request.client_id = _client_id_from_header(x_client_id)
    task = task_store.create(request)
    background_tasks.add_task(run_analysis_task, task.task_id, request, task_store)
    return AnalyzeResponse(
        task_id=task.task_id,
        status=task.status,
        status_url=f"/tasks/{task.task_id}",
    )


@app.post("/analyze-file", response_model=AnalyzeResponse, status_code=202)
def submit_file_analysis(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    mode: AnalysisMode = Form(AnalysisMode.summary),
    use_mock: bool | None = Form(None),
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> AnalyzeResponse:
    request = AnalyzeRequest(
        url=f"upload://{file.filename or 'uploaded-media'}",
        mode=mode,
        use_mock=use_mock,
        client_id=_client_id_from_header(x_client_id),
    )
    task = task_store.create(request)
    media_path = _save_upload(file, task.task_id)
    background_tasks.add_task(run_file_analysis_task, task.task_id, request, media_path, task_store)
    return AnalyzeResponse(
        task_id=task.task_id,
        status=task.status,
        status_url=f"/tasks/{task.task_id}",
    )


@app.post("/cookies")
def upload_cookies(file: UploadFile = File(...)) -> dict[str, object]:
    target = default_cookies_file(settings)
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
    return summarize_cookie_file(default_cookies_file(settings))


@app.post("/cookies/qr/start")
def start_qr_cookie_login() -> dict[str, object]:
    try:
        return start_qr_login(settings)
    except QRLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/cookies/qr/{session_id}/status")
def get_qr_cookie_login_status(session_id: str) -> dict[str, object]:
    try:
        return check_qr_login(session_id, settings)
    except QRLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cookies/browser/start")
def start_browser_cookie_capture() -> dict[str, object]:
    try:
        return start_cookie_browser(settings)
    except BrowserCookieError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/cookies/browser/status")
def get_browser_cookie_capture_status() -> dict[str, object]:
    try:
        return cookie_browser_status()
    except BrowserCookieError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cookies/browser/capture")
def capture_browser_cookies() -> dict[str, object]:
    try:
        return capture_douyin_cookies(settings)
    except BrowserCookieError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/tasks", response_model=list[TaskRecord])
def list_tasks(
    status: TaskStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> list[TaskRecord]:
    return task_store.list_tasks(
        limit=limit,
        status=status,
        client_id=_client_id_from_header(x_client_id),
    )


@app.get("/tasks/{task_id}", response_model=TaskRecord)
def get_task(
    task_id: str,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> TaskRecord:
    task = _owned_task(task_id, _client_id_from_header(x_client_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: str,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> None:
    task = _owned_task(task_id, _client_id_from_header(x_client_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task_store.delete(task_id)
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
        )
    else:
        llm = MockLlmAnalyzer() if use_mock else OpenAICompatibleAnalyzer(settings)
    metadata = _chat_metadata(task.result)
    try:
        answer = llm.chat(question, transcript, metadata=metadata)
    except VideoInsightError as exc:
        raise HTTPException(status_code=502, detail=f"AI 对话失败：{exc}") from exc
    return ChatResponse(answer=answer)


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
    result_file = settings.results_dir / f"{task_id}.json"
    if result_file.exists():
        result_file.unlink()
    for directory in (settings.tmp_dir / task_id, settings.media_dir / task_id):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


@app.get("/tasks/{task_id}/media")
def get_task_media(
    task_id: str,
    download: bool = False,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> FileResponse:
    """返回任务关联的媒体文件（默认是转写用的音频/原始文件）：内联预览，?download=1 触发下载。"""
    media_path = _resolve_media_path(_owned_task(task_id, _client_id_from_header(x_client_id)))
    if media_path is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return _media_response(media_path, download)


@app.get("/tasks/{task_id}/video")
def get_task_video(
    task_id: str,
    download: bool = False,
    x_client_id: str | None = Header(default=None, description="客户端身份标识（浏览器持久化 UUID）"),
) -> FileResponse:
    """返回任务的完整视频：已存文件是视频时直接返回；链接任务只存了音频时按需下载源视频。"""
    task = _owned_task(task_id, _client_id_from_header(x_client_id))
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
    downloader = YtDlpDownloader(settings.tmp_dir)
    return downloader.download_video(str(task.request.url), task.task_id)


def _save_upload(file: UploadFile, task_id: str) -> Path:
    original_name = Path(file.filename or "uploaded-media").name
    target_dir = settings.media_dir / task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / original_name
    with target.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    return target
