from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

import httpx

from app.adapters.audio import resolve_ffmpeg
from app.config import Settings, settings
from app.errors import ExternalDependencyError, WorkflowError


@dataclass(frozen=True)
class DownloadedMedia:
    audio_path: Path | None
    title: str | None
    author: str | None
    duration: float | None
    source_url: str


class MockDownloader:
    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir

    def download_audio(self, url: str, task_id: str) -> DownloadedMedia:
        task_dir = self.tmp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        audio_path = task_dir / "mock-audio.txt"
        audio_path.write_text("mock audio placeholder", encoding="utf-8")
        return DownloadedMedia(
            audio_path=audio_path,
            title="Mock 视频：AI 面试复盘示例",
            author="VideoInsight Demo",
            duration=180,
            source_url=url,
        )


class YtDlpDownloader:
    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir

    def download_audio(self, url: str, task_id: str) -> DownloadedMedia:
        url = normalize_url(url)
        yt_dlp_command = self._yt_dlp_command()
        if yt_dlp_command is None:
            raise ExternalDependencyError("未找到 yt-dlp 命令，请先安装依赖：pip install -r requirements.txt")

        task_dir = self.tmp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(task_dir / "%(id)s.%(ext)s")
        info_json = task_dir / "info.json"
        ffmpeg = resolve_ffmpeg()

        cmd = [
            *yt_dlp_command,
            "-f",
            "bestaudio/best",
            "-x",
            "--audio-format",
            "mp3",
            "--write-info-json",
            "--print-json",
            "--ffmpeg-location",
            ffmpeg,
            "-o",
            output_template,
            url,
        ]
        cookies_file = resolve_cookies_file(settings)
        if cookies_file:
            cmd.extend(["--cookies", str(cookies_file)])
        elif settings.ytdlp_cookies_from_browser:
            cmd.extend(["--cookies-from-browser", settings.ytdlp_cookies_from_browser])

        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WorkflowError(f"视频下载失败：{friendly_download_error(detail)}")

        metadata = self._parse_metadata(completed.stdout, info_json)
        audio_candidates = sorted(task_dir.glob("*.mp3"))
        if not audio_candidates:
            raise WorkflowError("视频下载完成，但没有找到导出的 MP3 音频文件。")

        return DownloadedMedia(
            audio_path=audio_candidates[0],
            title=metadata.get("title"),
            author=metadata.get("uploader") or metadata.get("channel"),
            duration=metadata.get("duration"),
            source_url=url,
        )

    def download_video(self, url: str, task_id: str) -> Path:
        """按需下载完整视频（含画面），合并为 mp4，返回文件路径。

        用于链接任务只保存了音频（mp3）时的"源视频下载"：
        与 download_audio 共用 cookies / ffmpeg 配置。
        """
        url = normalize_url(url)
        yt_dlp_command = self._yt_dlp_command()
        if yt_dlp_command is None:
            raise ExternalDependencyError("未找到 yt-dlp 命令，请先安装依赖：pip install -r requirements.txt")

        task_dir = self.tmp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(task_dir / "%(id)s.video.%(ext)s")
        ffmpeg = resolve_ffmpeg()

        cmd = [
            *yt_dlp_command,
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "--ffmpeg-location",
            ffmpeg,
            "-o",
            output_template,
            url,
        ]
        cookies_file = resolve_cookies_file(settings)
        if cookies_file:
            cmd.extend(["--cookies", str(cookies_file)])
        elif settings.ytdlp_cookies_from_browser:
            cmd.extend(["--cookies-from-browser", settings.ytdlp_cookies_from_browser])

        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WorkflowError(f"源视频下载失败：{friendly_download_error(detail)}")

        candidates = sorted(task_dir.glob("*.video.mp4"))
        if not candidates:
            raise WorkflowError("源视频下载完成，但没有生成 mp4 文件。")
        return candidates[0]

    @staticmethod
    def _yt_dlp_command() -> list[str] | None:
        executable = shutil.which("yt-dlp")
        if executable:
            return [executable]
        if find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
        return None

    @staticmethod
    def _parse_metadata(stdout: str, info_json: Path) -> dict:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

        json_files = sorted(info_json.parent.glob("*.info.json"))
        if json_files:
            return json.loads(json_files[0].read_text(encoding="utf-8"))
        return {}


class KuaishouDownloader:
    """快手下载器：通过移动端分享页解析视频直链，不依赖 yt-dlp。

    背景：yt-dlp 当前版本没有快手 extractor，且快手网页版不暴露视频直链。
    真视频的 mp4 直链藏在移动端页面（v.m.chenzhongtech.com / m.gifshow.com）
    的 mainMvUrls 字段中；图文帖（BROWSE_SLIDE_PHOTO）没有该字段，
    会给出友好提示，引导走上传分析兜底。
    """

    MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )

    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir

    def download_audio(self, url: str, task_id: str) -> DownloadedMedia:
        photo_id = self._resolve_photo_id(url)
        mp4_url, title, author = self._extract_media(photo_id)
        task_dir = self.tmp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        audio_path = task_dir / f"kuaishou-{photo_id}.mp4"
        self._download_mp4(mp4_url, audio_path)
        return DownloadedMedia(
            audio_path=audio_path,
            title=title,
            author=author,
            duration=None,
            source_url=url,
        )

    def _resolve_photo_id(self, url: str) -> str:
        value = url.strip()
        if re.fullmatch(r"[0-9A-Za-z]{10,}", value):
            return value
        try:
            response = httpx.get(
                value,
                follow_redirects=True,
                timeout=20,
                headers={"User-Agent": self.MOBILE_UA},
                verify=False,
            )
        except httpx.HTTPError as exc:
            raise WorkflowError(f"快手链接无法访问：{exc}") from exc
        match = re.search(r"photoId=([0-9A-Za-z]+)", str(response.url))
        if match:
            return match.group(1)
        raise WorkflowError("无法从快手分享链接解析 photoId，请确认链接有效。")

    def _extract_media(self, photo_id: str) -> tuple[str, str | None, str | None]:
        try:
            page = httpx.get(
                f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}",
                timeout=25,
                headers={"User-Agent": self.MOBILE_UA},
                verify=False,
            )
        except httpx.HTTPError as exc:
            raise WorkflowError(f"快手页面获取失败：{exc}") from exc
        if page.status_code != 200:
            raise WorkflowError(f"快手页面返回异常状态码：{page.status_code}")

        match = re.search(r'"mainMvUrls"\s*:\s*(\[[^\]]+\])', page.text)
        if not match:
            raise WorkflowError(
                "该快手作品没有可解析的视频直链（可能为图文帖）。"
                "请改用上传文件分析：在 App 中下载该作品后使用“上传分析”。"
            )
        try:
            items = json.loads(match.group(1).encode().decode("unicode_escape"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"快手视频信息解析失败：{exc}") from exc
        url = next((item.get("url") for item in items if item.get("url")), None)
        if not url:
            raise WorkflowError("快手视频信息中未找到可用的直链。")
        return url, self._page_text_field(page.text, "caption"), (
            self._page_text_field(page.text, "userName")
            or self._page_text_field(page.text, "user_name")
        )

    @staticmethod
    def _page_text_field(html: str, key: str) -> str | None:
        match = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if not match:
            return None
        try:
            return match.group(1).encode().decode("unicode_escape")
        except UnicodeError:
            return match.group(1)

    def _download_mp4(self, url: str, target: Path) -> None:
        try:
            with httpx.stream(
                "GET",
                url,
                timeout=120,
                headers={"User-Agent": self.MOBILE_UA},
                verify=False,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                with target.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=1 << 16):
                        output.write(chunk)
        except httpx.HTTPError as exc:
            raise WorkflowError(f"快手视频下载失败：{exc}") from exc


def is_kuaishou_url(url: str) -> bool:
    value = url.strip().lower()
    return any(host in value for host in ("kuaishou.com", "chenzhongtech.com", "gifshow.com"))


def select_downloader(
    url: str, tmp_dir: Path, use_mock: bool
) -> MockDownloader | YtDlpDownloader | KuaishouDownloader:
    if use_mock:
        return MockDownloader(tmp_dir)
    if is_kuaishou_url(url):
        return KuaishouDownloader(tmp_dir)
    return YtDlpDownloader(tmp_dir)


def normalize_url(value: str) -> str:
    value = value.strip()
    markdown_link = re.fullmatch(r"\[[^\]]+\]\((https?://[^)]+)\)", value)
    if markdown_link:
        value = markdown_link.group(1)
    if "v.douyin.com" in value:
        return resolve_douyin_short_url(value)
    return value


def resolve_douyin_short_url(url: str) -> str:
    try:
        response = httpx.get(url, follow_redirects=False, verify=False, timeout=10)
    except httpx.HTTPError:
        return url

    location = response.headers.get("location")
    if not location:
        return url

    match = re.search(r"/video/(\d+)", location)
    if match:
        return f"https://www.douyin.com/video/{match.group(1)}"
    return location


def default_cookies_file(runtime_settings: Settings = settings) -> Path:
    return runtime_settings.media_dir / "_cookies" / "cookies.txt"


def resolve_cookies_file(runtime_settings: Settings = settings) -> Path | None:
    if runtime_settings.ytdlp_cookies_file:
        if not runtime_settings.ytdlp_cookies_file.exists():
            raise ExternalDependencyError(f"YTDLP_COOKIES_FILE 指向的文件不存在：{runtime_settings.ytdlp_cookies_file}")
        return runtime_settings.ytdlp_cookies_file

    project_cookies = default_cookies_file(runtime_settings)
    if is_usable_cookies_file(project_cookies):
        return project_cookies
    return None


def is_usable_cookies_file(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(line.strip() and not line.startswith("#") and "\t" in line for line in lines)


def friendly_download_error(detail: str) -> str:
    lower = detail.lower()
    if "could not copy chrome cookie database" in lower or "failed to decrypt with dpapi" in lower:
        return (
            "无法读取本机浏览器 cookies。请在浏览器中导出 Netscape 格式 cookies.txt，"
            "然后在网页左侧“平台 Cookies”区域上传后重试。\n\n原始错误："
            f"{detail}"
        )
    if "fresh cookies" in lower or "cookies are needed" in lower:
        return (
            "该平台需要登录态 cookies。请先导出对应平台的 cookies.txt，"
            "在网页左侧“平台 Cookies”区域上传后重试。\n\n原始错误："
            f"{detail}"
        )
    return detail
