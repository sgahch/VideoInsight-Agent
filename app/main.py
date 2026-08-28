from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
import shutil

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.adapters.downloader import YtDlpDownloader, default_cookies_file, resolve_cookies_file
from app.browser_cookies import BrowserCookieError, capture_douyin_cookies, cookie_browser_status, start_cookie_browser
from app.cookies import summarize_cookie_file, write_normalized_cookie_file
from app.errors import VideoInsightError
from app.qr_login import QRLoginError, check_qr_login, start_qr_login
from app.schemas import AnalysisMode, AnalyzeRequest, AnalyzeResponse, TaskRecord
from app.task_store import task_store
from app.workflow import run_analysis_task, run_file_analysis_task


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
def submit_analysis(request: AnalyzeRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
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
) -> AnalyzeResponse:
    request = AnalyzeRequest(
        url=f"upload://{file.filename or 'uploaded-media'}",
        mode=mode,
        use_mock=use_mock,
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
) -> list[TaskRecord]:
    return task_store.list_tasks(limit=limit, status=status)


@app.get("/tasks/{task_id}", response_model=TaskRecord)
def get_task(task_id: str) -> TaskRecord:
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: str) -> None:
    if not task_store.delete(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    _remove_task_files(task_id)


def _remove_task_files(task_id: str) -> None:
    result_file = settings.results_dir / f"{task_id}.json"
    if result_file.exists():
        result_file.unlink()
    for directory in (settings.tmp_dir / task_id, settings.media_dir / task_id):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


@app.get("/tasks/{task_id}/media")
def get_task_media(task_id: str, download: bool = False) -> FileResponse:
    """返回任务关联的媒体文件（默认是转写用的音频/原始文件）：内联预览，?download=1 触发下载。"""
    media_path = _resolve_media_path(task_store.get(task_id))
    if media_path is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return _media_response(media_path, download)


@app.get("/tasks/{task_id}/video")
def get_task_video(task_id: str, download: bool = False) -> FileResponse:
    """返回任务的完整视频：已存文件是视频时直接返回；链接任务只存了音频时按需下载源视频。"""
    task = task_store.get(task_id)
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
