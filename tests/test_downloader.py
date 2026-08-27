from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.adapters.downloader import is_kuaishou_url
from app.adapters.downloader import KuaishouDownloader
from app.adapters.downloader import normalize_url
from app.adapters.downloader import default_cookies_file, friendly_download_error, is_usable_cookies_file, normalize_url, resolve_cookies_file, resolve_douyin_short_url
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


def test_header_only_cookies_file_is_not_usable(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    assert is_usable_cookies_file(cookie_file) is False


def test_friendly_download_error_for_browser_cookie_failure() -> None:
    message = friendly_download_error("ERROR: Could not copy Chrome cookie database.")

    assert "cookies.txt" in message
    assert "平台 Cookies" in message


def test_resolve_douyin_short_url_falls_back_on_invalid_url() -> None:
    assert resolve_douyin_short_url("not-a-url") == "not-a-url"


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
