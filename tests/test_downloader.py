from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.adapters.downloader import is_kuaishou_url
from app.adapters.downloader import KuaishouDownloader
from app.adapters.downloader import normalize_url
import app.adapters.downloader as downloader_module
from app.adapters.downloader import _needs_fresh_cookies, _run_yt_dlp_with_cookie_recovery, cookie_storage_path, default_cookies_file, friendly_download_error, is_usable_cookies_file, normalize_url, resolve_cookies_file, resolve_douyin_short_url
from app.errors import WorkflowError


def test_normalize_markdown_link() -> None:
    assert (
        normalize_url("[https://v.douyin.com/Ep5d4W3NEIU/](https://v.douyin.com/Ep5d4W3NEIU/)")
    ).startswith("https://www.douyin.com/video/")


def test_normalize_plain_url() -> None:
    assert normalize_url(" https://example.com/video ") == "https://example.com/video"


def test_resolve_project_cookies_file(tmp_path: Path) -> None:
    settings = Settings(mock_mode=True, tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results", media_dir=tmp_path)
    cookie_file = default_cookies_file(settings)
    cookie_file.parent.mkdir(parents=True)
    cookie_file.write_text("# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tFALSE\t0\ttest\tvalue\n", encoding="utf-8")

    assert resolve_cookies_file(settings) == cookie_file


def test_cookie_storage_path_honors_configured_file(tmp_path: Path) -> None:
    configured = tmp_path / "exported" / "cookies.txt"
    runtime_settings = Settings(
        mock_mode=True,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        ytdlp_cookies_file=configured,
    )

    assert cookie_storage_path(runtime_settings) == configured


def test_header_only_cookies_file_is_not_usable(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    assert is_usable_cookies_file(cookie_file) is False


def test_friendly_download_error_for_browser_cookie_failure() -> None:
    message = friendly_download_error("ERROR: Could not copy Chrome cookie database.")

    assert "cookies.txt" in message
    assert "平台 Cookies" in message
    assert "Could not copy" not in message


def test_friendly_download_error_for_stale_configured_cookies() -> None:
    message = friendly_download_error(
        "ERROR: Fresh cookies are needed",
        cookies_configured=True,
    )

    assert "抖音拒绝了当前请求" in message
    assert "重新登录" in message
    assert "Fresh cookies" not in message


def test_friendly_download_error_for_unavailable_proxy() -> None:
    message = friendly_download_error("ProxyError: Unable to connect to proxy")

    assert "代理服务不可用" in message
    assert "HTTP_PROXY" in message


def test_friendly_download_error_for_missing_cookies() -> None:
    message = friendly_download_error("ERROR: Fresh cookies are needed")

    assert "需要登录态 cookies" in message
    assert "上传后重试" in message
    assert "Fresh cookies" not in message


def test_run_yt_dlp_ignores_proxy_environment(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_subprocess_run(*_args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_subprocess_run)

    downloader_module._run_yt_dlp(["yt-dlp", "https://example.com/video"])

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "HTTP_PROXY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["NO_PROXY"] == "*"


def test_detects_fresh_cookie_retry_condition() -> None:
    import subprocess

    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="ERROR: Fresh cookies are needed")
    succeeded = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")

    assert _needs_fresh_cookies(failed) is True
    assert _needs_fresh_cookies(succeeded) is False


def test_cookie_recovery_retries_twice(monkeypatch) -> None:
    import subprocess

    responses = [
        subprocess.CompletedProcess([], 1, stdout="", stderr="Fresh cookies are needed"),
        subprocess.CompletedProcess([], 1, stdout="", stderr="Fresh cookies are needed"),
        subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
    ]
    refreshes: list[bool] = []
    monkeypatch.setattr(downloader_module, "_run_yt_dlp", lambda cmd: responses.pop(0))
    monkeypatch.setattr(
        downloader_module,
        "_refresh_project_cookies_from_browser",
        lambda: refreshes.append(True) or True,
    )
    monkeypatch.setattr(downloader_module, "sleep", lambda seconds: None)

    completed = _run_yt_dlp_with_cookie_recovery(["yt-dlp"])

    assert completed.returncode == 0
    assert len(refreshes) == 2


def test_yt_dlp_retries_three_times_without_browser_refresh(monkeypatch) -> None:
    import subprocess

    responses = [
        subprocess.CompletedProcess([], 1, stdout="", stderr="temporary 403"),
        subprocess.CompletedProcess([], 1, stdout="", stderr="temporary 403"),
        subprocess.CompletedProcess([], 1, stdout="", stderr="temporary 403"),
        subprocess.CompletedProcess([], 1, stdout="", stderr="temporary 403"),
    ]
    calls: list[list[str]] = []
    delays: list[float] = []

    monkeypatch.setattr(downloader_module, "_run_yt_dlp", lambda cmd: calls.append(cmd) or responses.pop(0))
    monkeypatch.setattr(downloader_module, "sleep", delays.append)

    completed = _run_yt_dlp_with_cookie_recovery(["yt-dlp"])

    assert completed.returncode == 1
    assert len(calls) == 4
    assert delays == [0.5, 1.0, 1.5]


def test_audio_download_limits_to_single_item_and_skips_info_file(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stderr = ""
        stdout = '{"id": "demo", "title": "测试视频"}\n'

    downloader = downloader_module.YtDlpDownloader(tmp_path)
    monkeypatch.setattr(downloader, "_yt_dlp_command", lambda: ["yt-dlp"])
    monkeypatch.setattr(downloader_module, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(downloader_module, "resolve_cookies_file", lambda _settings: None)

    def fake_run(command) -> FakeResult:
        # 下载器现在传入「返回命令」的可调用对象，以便每次重试前重新暂存 cookies。
        cmd = command() if callable(command) else command
        commands.append(cmd)
        task_dir = tmp_path / "task"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "demo.mp3").write_bytes(b"audio")
        return FakeResult()

    monkeypatch.setattr(downloader_module, "_run_yt_dlp_with_cookie_recovery", fake_run)

    media = downloader.download_audio("https://example.com/video", "task")

    assert media.title == "测试视频"
    assert "--no-playlist" in commands[0]
    assert "--no-write-info-json" in commands[0]
    assert "--write-info-json" not in commands[0]


def test_resolve_douyin_short_url_falls_back_on_invalid_url() -> None:
    assert resolve_douyin_short_url("not-a-url") == "not-a-url"


def test_normalize_douyin_note_url_becomes_video() -> None:
    # 分享短链常重定向到 /note/，yt-dlp 只认 /video/，必须改写。
    assert (
        normalize_url("https://www.douyin.com/note/7682160066900347556?previous_page=app_code_link")
        == "https://www.douyin.com/video/7682160066900347556"
    )


def test_normalize_douyin_video_url_strips_query() -> None:
    assert (
        normalize_url("https://www.douyin.com/video/1234567890?foo=bar")
        == "https://www.douyin.com/video/1234567890"
    )


def test_normalize_douyin_modal_id() -> None:
    assert (
        normalize_url("https://www.douyin.com/discover?modal_id=1234567890")
        == "https://www.douyin.com/video/1234567890"
    )


def test_normalize_douyin_iesdouyin_share() -> None:
    assert (
        normalize_url("https://www.iesdouyin.com/share/video/1234567890/")
        == "https://www.douyin.com/video/1234567890"
    )


def test_normalize_non_douyin_url_untouched() -> None:
    assert normalize_url("https://www.bilibili.com/video/BV1xx411c7mD/") == (
        "https://www.bilibili.com/video/BV1xx411c7mD/"
    )


def test_cookie_args_stages_a_copy(tmp_path: Path) -> None:
    source = tmp_path / "shared-cookies.txt"
    original = "# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tFALSE\t0\ttest\tvalue\n"
    source.write_text(original, encoding="utf-8")
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    args = downloader_module._cookie_args(source, task_dir)

    assert args[0] == "--cookies"
    staged = Path(args[1])
    assert staged != source
    assert staged.parent == task_dir
    assert staged.read_text(encoding="utf-8") == original


def test_cookie_recovery_rebuilds_command_each_attempt(monkeypatch) -> None:
    import subprocess

    responses = [
        subprocess.CompletedProcess([], 1, stdout="", stderr="temporary 403"),
        subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
    ]
    built: list[int] = []
    monkeypatch.setattr(downloader_module, "_run_yt_dlp", lambda cmd: responses.pop(0))
    monkeypatch.setattr(downloader_module, "sleep", lambda seconds: None)

    def build() -> list[str]:
        built.append(1)
        return ["yt-dlp", "https://example.com/video"]

    completed = _run_yt_dlp_with_cookie_recovery(build)

    assert completed.returncode == 0
    assert len(built) == 2


def test_friendly_download_error_for_unsupported_url() -> None:
    message = friendly_download_error(
        "ERROR: Unsupported URL: https://www.douyin.com/note/7682160066900347556"
    )

    assert "无法识别" in message
    assert "上传文件分析" in message


def test_is_kuaishou_url_variants() -> None:
    assert is_kuaishou_url("https://v.kuaishou.com/JExSMSsx")
    assert is_kuaishou_url("https://www.kuaishou.com/short-video/abc123")
    assert is_kuaishou_url("https://v.m.chenzhongtech.com/fw/photo/abc123")
    assert is_kuaishou_url("https://m.gifshow.com/fw/photo/abc123")
    assert not is_kuaishou_url("https://v.douyin.com/lmwHu2zoSII/")
    assert not is_kuaishou_url("https://b23.tv/OEG7G4m")
    assert not is_kuaishou_url("https://example.com/video")


def test_kuaishou_extract_media_from_mobile_page(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = '{"mainMvUrls": [{"url": "https://cdn.example.com/v.mp4?pkey=1"}]}'

    monkeypatch.setattr("app.adapters.downloader.httpx.get", lambda *args, **kwargs: FakeResponse())
    downloader = KuaishouDownloader(Path("data/tmp"))

    url, _title, _author = downloader._extract_media("3xrv37rwy9nihuk")

    assert url == "https://cdn.example.com/v.mp4?pkey=1"


def test_kuaishou_photo_post_raises_friendly_error(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = '{"photoUrl": "https://cdn.example.com/img.jpg"}'

    monkeypatch.setattr("app.adapters.downloader.httpx.get", lambda *args, **kwargs: FakeResponse())
    downloader = KuaishouDownloader(Path("data/tmp"))

    try:
        downloader._extract_media("someid")
        raise AssertionError("图文帖应抛出 WorkflowError")
    except WorkflowError as exc:
        assert "上传分析" in str(exc)
