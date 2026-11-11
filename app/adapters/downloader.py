from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from time import sleep

import httpx

from app.adapters.audio import resolve_ffmpeg
from app.adapters.bilibili import is_bilibili_video_url, resolve_bilibili_info
from app.config import Settings, settings
from app.errors import ExternalDependencyError, WorkflowError


LOGGER = logging.getLogger(__name__)
DOWNLOAD_RETRY_COUNT = 3
# yt-dlp 单次尝试的超时上限（重试由 _run_yt_dlp_with_cookie_recovery 负责，
# 但超时不重试：4 次 × 30 分钟会把一个线程池工人占满两小时）。
DOWNLOAD_TIMEOUT_SECONDS = 1800


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
        ffmpeg = resolve_ffmpeg()

        cmd_base = [
            *yt_dlp_command,
            "-f",
            "bestaudio/best",
            "-x",
            "--audio-format",
            "mp3",
            # 下载页经常包含推荐作品；明确只处理提交的单条链接，避免额外解析。
            "--no-playlist",
            # 元数据由 --print-json 返回，无需再落一份 .info.json 并重复写盘。
            "--no-write-info-json",
            "--print-json",
            "--ffmpeg-location",
            ffmpeg,
            "-o",
            output_template,
        ]
        cookies_file = resolve_cookies_file(settings)
        # 传入可调用对象：每次尝试前重新暂存 cookies，这样浏览器刷新 cookies 后重试才生效。
        completed = _run_yt_dlp_with_cookie_recovery(
            lambda: [
                *cmd_base, *_cookie_args(cookies_file, task_dir),
                *self._input_args(url, task_dir, cookies_file),
            ]
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            LOGGER.warning("yt-dlp audio download failed: %s", detail)
            cookies_configured = bool(cookies_file or settings.ytdlp_cookies_from_browser)
            raise WorkflowError(
                f"视频下载失败：{friendly_download_error(detail, cookies_configured=cookies_configured)}"
            )

        metadata = self._parse_metadata(completed.stdout)
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

        cmd_base = [
            *yt_dlp_command,
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "--no-playlist",
            "--ffmpeg-location",
            ffmpeg,
            "-o",
            output_template,
        ]
        cookies_file = resolve_cookies_file(settings)
        completed = _run_yt_dlp_with_cookie_recovery(
            lambda: [
                *cmd_base, *_cookie_args(cookies_file, task_dir),
                *self._input_args(url, task_dir, cookies_file),
            ]
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            LOGGER.warning("yt-dlp video download failed: %s", detail)
            cookies_configured = bool(cookies_file or settings.ytdlp_cookies_from_browser)
            raise WorkflowError(
                f"源视频下载失败：{friendly_download_error(detail, cookies_configured=cookies_configured)}"
            )

        candidates = sorted(task_dir.glob("*.video.mp4"))
        if not candidates:
            raise WorkflowError("源视频下载完成，但没有生成 mp4 文件。")
        return candidates[0]

    @staticmethod
    def _input_args(url: str, task_dir: Path, cookies_file: Path | None) -> list[str]:
        if not is_bilibili_video_url(url):
            return [url]
        info = resolve_bilibili_info(url, _cookie_args(cookies_file, task_dir))
        info_file = task_dir / ".bilibili-source.json"
        info_file.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        # B 站 CDN 可能拒绝整文件 GET（567），但接受 Range；先测候选节点，
        # 再以分块请求完整下载。重试时重新解析，避免复用已过期的签名地址。
        return [
            "--http-chunk-size", "1M", "--check-formats",
            "--retries", "2", "--fragment-retries", "2",
            "--socket-timeout", "15", "--abort-on-unavailable-fragments",
            "--load-info-json", str(info_file),
        ]

    @staticmethod
    def _yt_dlp_command() -> list[str] | None:
        executable = shutil.which("yt-dlp")
        if executable:
            return [executable]
        if find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
        return None

    @staticmethod
    def _parse_metadata(stdout: str) -> dict:
        """从 yt-dlp 的 JSON 输出取元数据，避免额外读写 info.json。"""
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
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
                trust_env=False,
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
                trust_env=False,
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
                follow_redirects=True,
                trust_env=False,
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


class DouyinShareDownloader:
    """抖音下载器：解析 m.douyin.com 移动分享页 SSR 数据，直接下载无水印 mp4。

    背景：yt-dlp 的抖音 extractor 走 www.douyin.com/aweme/v1/web/aweme/detail/，
    该接口已强制 a_bogus 签名校验（yt-dlp 至 2026.08.19 未实现，源码仅留 TODO），
    即使 cookies 齐全也一律 403，报 "Fresh cookies (not necessarily logged in)
    are needed"——重登录/换 cookies 都无效。

    实测可行的通道：m.douyin.com/share/video/<id> 的 SSR 页面只要请求带 ttwid
    cookie，就会把完整视频数据内嵌进 window._ROUTER_DATA（item_list[0].video.
    play_addr.uri），再用 uri 拼经典直链 aweme.snssdk.com/aweme/v1/play/
    302 到 douyinvod CDN 拿无水印 mp4。与其他登录态 cookies 无关。
    """

    MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
    SHARE_PAGE_URL = "https://m.douyin.com/share/video/{video_id}/"
    PLAY_URL = "https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio={ratio}&line=0"
    # 「下载源视频」用原画质；转写只取音轨，画质不影响识别，故用 douyin_audio_ratio。
    DEFAULT_VIDEO_RATIO = "1080p"
    _ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*", re.S)
    _SHARE_FETCH_ATTEMPTS = 3
    # CDN 偶发返回几十字节的风控占位内容；小于该阈值一律视为失败并重试。
    _MIN_VIDEO_BYTES = 10_000
    _DOWNLOAD_ATTEMPTS_PER_URL = 2

    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir

    def download_audio(self, url: str, task_id: str) -> DownloadedMedia:
        video_id = self._resolve_video_id(url)
        item = self._fetch_item(video_id)
        task_dir = self.tmp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        audio_path = task_dir / f"douyin-{video_id}.mp4"
        # 转写只需要音轨：用低画质档位（默认 480p）大幅减少下载体积与耗时。
        self._download_mp4(item, audio_path, ratio=self._audio_ratio())
        duration_ms = (item.get("video") or {}).get("duration")
        return DownloadedMedia(
            audio_path=audio_path,
            title=item.get("desc"),
            author=(item.get("author") or {}).get("nickname"),
            duration=round(duration_ms / 1000, 1) if duration_ms else None,
            source_url=url,
        )

    def download_video(self, url: str, task_id: str) -> Path:
        """按需下载完整视频（含画面，原画质）。

        download_audio 为了省流量默认取低画质（只要音轨），因此**只有**它取的
        就是原画质时才能复用；否则必须单独下原画质，避免把降质版当成「源视频」
        交给用户。
        """
        video_id = self._resolve_video_id(url)
        task_dir = self.tmp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        target = task_dir / f"{video_id}.video.mp4"
        audio_copy = task_dir / f"douyin-{video_id}.mp4"
        reusable = (
            self._audio_ratio() == self.DEFAULT_VIDEO_RATIO
            and audio_copy.is_file()
            and audio_copy.stat().st_size >= self._MIN_VIDEO_BYTES
        )
        if reusable:
            # 覆盖旧文件：删除失败不能中断（文件被占用/只读目录），copyfile 会直接覆盖。
            _best_effort_unlink(target)
            shutil.copyfile(audio_copy, target)
            return target
        item = self._fetch_item(video_id)
        self._download_mp4(item, target, ratio=self.DEFAULT_VIDEO_RATIO)
        return target

    def _resolve_video_id(self, url: str) -> str:
        """从任意抖音分享形态解析视频 ID（短链在 normalize_url 内部解析）。"""
        value = normalize_url(url.strip())
        for pattern in _DOUYIN_ITEM_PATTERNS:
            match = pattern.search(value)
            if match:
                return match.group(1)
        raise WorkflowError("无法从抖音链接解析视频 ID，请确认分享链接是否完整。")

    def _fetch_item(self, video_id: str) -> dict:
        cookie_header = self._douyin_cookie_header()
        if "ttwid" not in cookie_header:
            raise WorkflowError(
                "缺少抖音 Cookies（ttwid），无法读取移动分享页。"
                "请在左侧「平台 Cookies」扫码登录并抓取最新状态后重试。"
            )
        last_issue = ""
        for attempt in range(self._SHARE_FETCH_ATTEMPTS):
            try:
                response = httpx.get(
                    self.SHARE_PAGE_URL.format(video_id=video_id),
                    headers={
                        "User-Agent": self.MOBILE_UA,
                        "Cookie": cookie_header,
                        "Referer": "https://www.douyin.com/",
                    },
                    follow_redirects=True,
                    timeout=25,
                    trust_env=False,
                )
            except httpx.HTTPError as exc:
                last_issue = f"分享页暂时无法访问（{type(exc).__name__}）"
            else:
                if response.status_code == 429:
                    raise WorkflowError("抖音限制了当前访问频率（HTTP 429），请稍后重试，或减少同时提交的抖音任务。")
                if response.status_code in (401, 403):
                    raise WorkflowError(
                        f"抖音拒绝读取分享页（HTTP {response.status_code}）。"
                        "请在浏览器中打开原链接，按提示完成登录或验证，再更新平台 Cookies 后重试。"
                    )
                if response.status_code == 200:
                    html = response.text
                    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
                    if title and any(word in title.group(1) for word in ("验证码", "安全验证", "访问验证")):
                        raise WorkflowError(
                            "抖音要求安全验证。请在浏览器中打开原链接并手动完成验证，"
                            "再更新平台 Cookies 后重试，或上传本地视频分析。"
                        )
                    item = self._item_from_share_page(html, video_id)
                    if item is not None:
                        return item
                    last_issue = "分享页未完整返回视频数据"
                elif 500 <= response.status_code < 600 or response.status_code in (408, 425):
                    last_issue = f"分享页暂时不可用（HTTP {response.status_code}）"
                else:
                    raise WorkflowError(f"抖音分享页返回异常状态码：{response.status_code}")
            LOGGER.warning(
                "抖音分享页读取失败：video_id=%s，尝试 %s/%s：%s",
                video_id, attempt + 1, self._SHARE_FETCH_ATTEMPTS, last_issue,
            )
            if attempt + 1 < self._SHARE_FETCH_ATTEMPTS:
                sleep(attempt + 1)
        raise WorkflowError(
            f"抖音{last_issue}（已尝试 {self._SHARE_FETCH_ATTEMPTS} 次）。"
            "可能是临时访问限制或页面加载异常，并不一定是作品失效。"
            "请稍后重试；若浏览器提示登录或验证，请先完成验证并更新平台 Cookies，"
            "也可上传本地视频分析。"
        )

    def _item_from_share_page(self, html: str, video_id: str) -> dict | None:
        match = self._ROUTER_DATA_RE.search(html)
        if not match:
            return None
        try:
            # Decode only the JSON value; scripts may continue after the assignment.
            data, _ = json.JSONDecoder().raw_decode(html, match.end())
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("loaderData"), dict):
            return None
        for value in data["loaderData"].values():
            if not isinstance(value, dict):
                continue
            info = value.get("videoInfoRes")
            if not isinstance(info, dict):
                continue
            item_list = info.get("item_list")
            if not isinstance(item_list, list):
                continue
            for item in item_list:
                if isinstance(item, dict) and (
                    not item.get("aweme_id") or str(item["aweme_id"]) == video_id
                ):
                    return item
        raise WorkflowError(
            "抖音分享页未返回视频数据：作品可能已删除、设为私密或被临时风控。"
            "请稍后重试，或改用上传本地视频分析。"
        )

    def _douyin_cookie_header(self) -> str:
        """从项目 cookies 文件构造 Cookie 头，只取 douyin.com 域（含 iesdouyin）。

        分享页数据只依赖 ttwid；发送其余同名 cookie 无害，但 365yg/amemv 等
        第三方域的 cookie 一律不发送。
        """
        cookies_file = resolve_cookies_file(settings)
        if cookies_file is None:
            return ""
        pairs: dict[str, str] = {}
        try:
            lines = cookies_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, name, value = parts[0], parts[5], parts[6]
            if not domain.endswith("douyin.com"):
                continue
            pairs.setdefault(name, value)
        return "; ".join(f"{name}={value}" for name, value in pairs.items())

    def _audio_ratio(self) -> str:
        """转写只需音轨，用低画质档位换下载体积（默认 480p）。"""
        return (settings.douyin_audio_ratio or self.DEFAULT_VIDEO_RATIO).strip()

    def _download_mp4(self, item: dict, target: Path, ratio: str | None = None) -> None:
        play_addr = (item.get("video") or {}).get("play_addr") or {}
        uri = play_addr.get("uri")
        url_list = [u for u in (play_addr.get("url_list") or []) if u]
        if not uri and not url_list:
            raise WorkflowError("该抖音作品没有可下载的视频流（可能为图文笔记），请改用上传文件分析。")

        ratio = ratio or self.DEFAULT_VIDEO_RATIO
        candidates: list[str] = []
        if uri:
            candidates.append(self.PLAY_URL.format(uri=uri, ratio=ratio))
            # 指定画质不可用时先退回原画质，再不行才用带水印直链：
            # 避免「低画质不存在」直接把任务带崩。
            if ratio != self.DEFAULT_VIDEO_RATIO:
                candidates.append(self.PLAY_URL.format(uri=uri, ratio=self.DEFAULT_VIDEO_RATIO))
        candidates += url_list  # playwm 直链做兜底（带水印但可用）
        errors: list[str] = []
        for candidate in candidates:
            for attempt in range(self._DOWNLOAD_ATTEMPTS_PER_URL):
                try:
                    self._download_candidate(candidate, target)
                    LOGGER.info(
                        "抖音视频下载完成：%s（%.2f MB）",
                        target.name,
                        target.stat().st_size / 1048576,
                    )
                    return
                except (httpx.HTTPError, WorkflowError) as exc:
                    errors.append(f"{candidate[:80]}: {exc}")
                    LOGGER.warning(
                        "抖音 mp4 下载失败（%s，第 %s 次尝试）：%s", target.name, attempt + 1, exc
                    )
                    sleep(0.5 * (attempt + 1))
        # 失败后不留脏文件；删除失败同样只记日志（见 _best_effort_unlink 说明）。
        _best_effort_unlink(target)
        raise WorkflowError(
            "抖音视频下载失败（已重试全部直链）：" + (errors[-1] if errors else "未知错误")
        )

    def _download_candidate(self, url: str, target: Path) -> None:
        with httpx.stream(
            "GET",
            url,
            timeout=180,
            headers={"User-Agent": self.MOBILE_UA, "Referer": "https://m.douyin.com/"},
            follow_redirects=True,
            trust_env=False,
        ) as response:
            response.raise_for_status()
            with target.open("wb") as output:
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    output.write(chunk)
        if target.stat().st_size < self._MIN_VIDEO_BYTES:
            raise WorkflowError(f"下载内容过小（{target.stat().st_size} 字节），疑似风控拦截页")


def is_douyin_url(url: str) -> bool:
    value = url.strip().lower()
    return any(host in value for host in ("douyin.com", "iesdouyin.com", "snssdk.com"))


def select_downloader(
    url: str, tmp_dir: Path, use_mock: bool
) -> MockDownloader | YtDlpDownloader | KuaishouDownloader | DouyinShareDownloader:
    if use_mock:
        return MockDownloader(tmp_dir)
    if is_douyin_url(url):
        # yt-dlp 的抖音通道被 a_bogus 签名墙 403（详见 DouyinShareDownloader 注释），
        # 必须优先走移动分享页方案。
        return DouyinShareDownloader(tmp_dir)
    if is_kuaishou_url(url):
        return KuaishouDownloader(tmp_dir)
    return YtDlpDownloader(tmp_dir)


_DOUYIN_ITEM_PATTERNS = (
    # /video/<id>、/note/<id>、iesdouyin.com/share/video|note/<id> 统一在此匹配。
    re.compile(r"/(?:video|note)/(\d+)"),
    # 网页端「探索/发现」弹窗形式的分享链接。
    re.compile(r"[?&]modal_id=(\d+)"),
)


def normalize_url(value: str) -> str:
    value = value.strip()
    markdown_link = re.fullmatch(r"\[[^\]]+\]\((https?://[^)]+)\)", value)
    if markdown_link:
        value = markdown_link.group(1)
    if "v.douyin.com" in value:
        value = resolve_douyin_short_url(value)
    return canonicalize_douyin_url(value)


def canonicalize_douyin_url(value: str) -> str:
    """把抖音各种分享形态统一成 yt-dlp 支持的 /video/<id>。

    yt-dlp 的抖音 extractor 只匹配 /video/<id>；而分享短链常重定向到
    /note/<id>（图文笔记或改版分享页），会退化成 generic 并报
    "Unsupported URL"。这里统一改写为 /video/<id>，让 extractor 接管。
    """
    if "douyin.com" not in value.lower():
        return value
    for pattern in _DOUYIN_ITEM_PATTERNS:
        match = pattern.search(value)
        if match:
            return f"https://www.douyin.com/video/{match.group(1)}"
    return value


def resolve_douyin_short_url(url: str) -> str:
    try:
        response = httpx.get(url, follow_redirects=False, timeout=10, trust_env=False)
    except httpx.HTTPError:
        return url

    location = response.headers.get("location")
    if not location:
        return url
    return canonicalize_douyin_url(location)


def default_cookies_file(runtime_settings: Settings = settings) -> Path:
    return runtime_settings.media_dir / "_cookies" / "cookies.txt"


def cookie_storage_path(runtime_settings: Settings = settings) -> Path:
    """Return the cookie file used by both the UI and the downloader."""
    return runtime_settings.ytdlp_cookies_file or default_cookies_file(runtime_settings)


def resolve_cookies_file(runtime_settings: Settings = settings) -> Path | None:
    if runtime_settings.ytdlp_cookies_file:
        if not runtime_settings.ytdlp_cookies_file.exists():
            raise ExternalDependencyError(f"YTDLP_COOKIES_FILE 指向的文件不存在：{runtime_settings.ytdlp_cookies_file}")
        return runtime_settings.ytdlp_cookies_file

    project_cookies = cookie_storage_path(runtime_settings)
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


def _best_effort_unlink(path: Path) -> None:
    """删除文件，失败只记日志不抛出。

    用 BaseException 是刻意的：清理属于收尾动作，某些运行环境（沙箱/内嵌
    解释器）会把 unlink 拦截成 SystemExit，它不属于 Exception，若用
    except Exception 仍会击穿调用方，让已经成功的下载/转写前功尽弃。
    """
    try:
        path.unlink(missing_ok=True)
    except BaseException as exc:  # noqa: BLE001 - 清理路径，吞掉一切错误
        LOGGER.warning("文件删除失败，已忽略：%s（%s）", path, exc)


def _run_yt_dlp(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """运行 yt-dlp，忽略失效的系统代理，避免本地代理关闭导致下载失败。

    timeout 是必须的：没有它，一个挂死的 yt-dlp 会永久占住一个线程池工人
    （后台长任务与所有同步端点共用同一个池），服务会逐渐失去响应。
    这里刻意让超时直接抛 WorkflowError 而不是返回非 0 码——否则外层
    cookie 恢复逻辑会把「超时」当成可重试失败，再跑 3 轮。
    """
    environment = os.environ.copy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(name, None)
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError(
            f"下载超过 {DOWNLOAD_TIMEOUT_SECONDS // 60} 分钟仍未完成，已中止。"
            "请确认网络可达，或改用「上传文件分析」绕开平台下载。"
        ) from exc


def _cookie_args(cookies_file: Path | None, task_dir: Path) -> list[str]:
    """构造 yt-dlp 的 cookies 参数。

    关键：把 cookies 复制成任务目录下的临时副本再交给 yt-dlp。yt-dlp 退出时会
    回写 cookies 文件，若直接指向共享文件，并发任务/文件被占用/只读时会抛
    PermissionError，并连带让整个下载失败。
    """
    if cookies_file is not None:
        staged = task_dir / ".yt-dlp-cookies.txt"
        try:
            shutil.copyfile(cookies_file, staged)
            return ["--cookies", str(staged)]
        except OSError:
            LOGGER.warning("暂存 cookies 失败，回退使用原始文件：%s", cookies_file)
            return ["--cookies", str(cookies_file)]
    if settings.ytdlp_cookies_from_browser:
        return ["--cookies-from-browser", settings.ytdlp_cookies_from_browser]
    return []


def _run_yt_dlp_with_cookie_recovery(
    cmd: list[str] | Callable[[], list[str]],
) -> subprocess.CompletedProcess[str]:
    """执行 yt-dlp；失败时重试，遇到 cookies 失效先刷新再重试。

    cmd 既可以是完整命令列表，也可以是「返回命令列表」的可调用对象——
    后者用于每次重试前重新暂存 cookies，使浏览器刷新的 cookies 能生效。
    """
    build = cmd if callable(cmd) else (lambda: cmd)
    completed = _run_yt_dlp(build())
    for attempt in range(DOWNLOAD_RETRY_COUNT):
        if completed.returncode == 0:
            break

        # yt-dlp 的 Fresh cookies/403 可能只是抖音临时风控；即使当前
        # 没有可用浏览器，也要继续重试，而不是第一次失败就直接结束任务。
        if _needs_fresh_cookies(completed):
            _refresh_project_cookies_from_browser()

        LOGGER.warning(
            "yt-dlp attempt %s/%s failed; retrying in %.1fs.",
            attempt + 1,
            DOWNLOAD_RETRY_COUNT + 1,
            0.5 * (attempt + 1),
        )
        sleep(0.5 * (attempt + 1))
        completed = _run_yt_dlp(build())
    return completed


def _needs_fresh_cookies(completed: subprocess.CompletedProcess[str]) -> bool:
    if completed.returncode == 0:
        return False
    detail = f"{completed.stderr}\n{completed.stdout}".lower()
    return "fresh cookies" in detail or "cookies are needed" in detail


def _refresh_project_cookies_from_browser() -> bool:
    if settings.ytdlp_cookies_file is not None:
        return False
    try:
        from app.browser_cookies import capture_douyin_cookies, cookie_browser_status

        status = cookie_browser_status()
        if not status.get("running") or not status.get("has_douyin_tab"):
            return False
        captured = capture_douyin_cookies(settings)
        if captured.get("saved"):
            LOGGER.info("Refreshed Douyin cookies from the dedicated browser; retrying yt-dlp once.")
            return True
    except Exception as exc:
        LOGGER.warning("Could not refresh Douyin cookies from the dedicated browser: %s", exc)
    return False


def friendly_download_error(detail: str, *, cookies_configured: bool = False) -> str:
    lower = detail.lower()
    if "could not copy chrome cookie database" in lower or "failed to decrypt with dpapi" in lower:
        return (
            "无法读取本机浏览器 cookies。请在浏览器中导出 Netscape 格式 cookies.txt，"
            "然后在网页左侧“平台 Cookies”区域上传后重试。"
        )
    if "unable to connect to proxy" in lower or "proxyerror" in lower:
        return "本机代理服务不可用，无法连接到配置的代理地址。请启动代理服务或清除 HTTP_PROXY/HTTPS_PROXY 后重试。"
    if "unsupported url" in lower:
        return (
            "yt-dlp 无法识别该链接。抖音图文/笔记（/note/）或改版分享页通常无法直接下载，"
            "请在 App 内下载后改用“上传文件分析”，或换用该作品的视频链接。"
        )
    if "http error 567" in lower:
        return (
            "B 站下载节点拒绝了请求（HTTP 567）。请稍后重试，"
            "或上传有效的 B 站 Cookies；仍失败可改用「上传文件分析」。"
        )
    if "no video formats found" in lower or "requested format is not available" in lower:
        return "该链接没有可用的视频流（可能是图文帖、直播回放或已下架），请改用上传文件分析。"
    if "fresh cookies" in lower or "cookies are needed" in lower:
        if cookies_configured:
            return (
                "抖音拒绝了当前请求，即使已检测到 Cookies。请在左侧“平台 Cookies”重新登录并抓取最新状态；"
                "若仍失败，可能是抖音临时风控，请稍后重试或改用上传本地视频。"
            )
        return (
            "该平台需要登录态 cookies。请先导出对应平台的 cookies.txt，"
            "在网页左侧“平台 Cookies”区域上传后重试。"
        )
    return detail
