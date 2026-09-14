# -*- coding: utf-8 -*-
"""DouyinShareDownloader 单元测试。

背景：yt-dlp 的抖音 extractor 被 a_bogus 签名墙 403（"Fresh cookies are
needed"），新下载器改走 m.douyin.com 移动分享页 SSR 数据 + 无水印直链。
全部网络调用用假对象替换，不触网。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.adapters.downloader as downloader_module
from app.adapters.downloader import (
    DouyinShareDownloader,
    is_douyin_url,
    select_downloader,
)
from app.errors import WorkflowError

VIDEO_ID = "7679416235511106843"


# --------------------------------------------------------------------------- fakes


def _write_cookies(tmp_path: Path, *, include_ttwid: bool = True) -> Path:
    path = tmp_path / "cookies.txt"
    lines = ["# Netscape HTTP Cookie File"]
    if include_ttwid:
        lines.append(".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\tfake-ttwid")
    lines.append(".douyin.com\tTRUE\t/\tFALSE\t0\ts_v_web_id\tverify_me")
    # 365yg 是第三方域，不应被带上
    lines.append("v95-ynkmtc-default.365yg.com\tTRUE\t/\tFALSE\t0\tsid_tt\tshould-not-appear")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _share_item() -> dict:
    return {
        "desc": "测试视频标题",
        "author": {"nickname": "测试作者"},
        "video": {
            "duration": 91500,
            "play_addr": {
                "uri": "v0d00fg10000da9afifog65s10jv8vk0",
                "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=fallback"],
            },
        },
    }


def _share_page_html(item: dict | None) -> str:
    loader = {"video_(id)/page": {"videoInfoRes": {"item_list": [item] if item else []}}}
    return (
        "<html><head></head><body><script>window._ROUTER_DATA = "
        + json.dumps({"loaderData": loader}, ensure_ascii=False)
        + "</script></body></html>"
    )


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeStreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise downloader_module.httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=downloader_module.httpx.Request("GET", "https://example.com"),
                response=None,
            )

    def iter_bytes(self, chunk_size: int):
        yield from self._chunks


class FakeStreamContext:
    def __init__(self, response: FakeStreamResponse) -> None:
        self._response = response

    def __enter__(self) -> FakeStreamResponse:
        return self._response

    def __exit__(self, *args) -> bool:
        return False


@pytest.fixture()
def cookies_env(tmp_path: Path, monkeypatch) -> Path:
    cookies = _write_cookies(tmp_path)
    monkeypatch.setattr(downloader_module, "resolve_cookies_file", lambda _settings: cookies)
    return cookies


def _patch_share_page(monkeypatch, item: dict | None) -> None:
    monkeypatch.setattr(
        downloader_module.httpx,
        "get",
        lambda *args, **kwargs: FakeResponse(_share_page_html(item)),
    )


def _patch_stream(monkeypatch, chunks_by_url: dict[str, list[bytes]]) -> None:
    def fake_stream(method: str, url: str, **kwargs):
        if url not in chunks_by_url:
            raise KeyError(f"unexpected stream url: {url}")
        return FakeStreamContext(FakeStreamResponse(chunks_by_url[url]))

    monkeypatch.setattr(downloader_module.httpx, "stream", fake_stream)


def _big_bytes(size: int = 64 * 1024) -> bytes:
    return b"x" * size


# --------------------------------------------------------------------------- routing


def test_is_douyin_url_variants() -> None:
    assert is_douyin_url("https://v.douyin.com/HopBInq9Gq4")
    assert is_douyin_url("https://www.douyin.com/video/1234567890")
    assert is_douyin_url("https://www.iesdouyin.com/share/video/1234567890/")
    assert not is_douyin_url("https://v.kuaishou.com/JExSMSsx")
    assert not is_douyin_url("https://www.bilibili.com/video/BV1xx411c7mD/")
    assert not is_douyin_url("https://example.com/video")


def test_select_downloader_routes_douyin_to_share_downloader(tmp_path: Path) -> None:
    downloader = select_downloader("https://v.douyin.com/HopBInq9Gq4", tmp_path, use_mock=False)
    assert isinstance(downloader, DouyinShareDownloader)
    # 快手与 B 站路由不受影响
    assert not isinstance(select_downloader("https://www.bilibili.com/video/BV1xx", tmp_path, False), DouyinShareDownloader)


# --------------------------------------------------------------------------- video id


def test_resolve_video_id_from_various_forms(tmp_path: Path) -> None:
    downloader = DouyinShareDownloader(tmp_path)
    assert downloader._resolve_video_id(f"https://www.douyin.com/video/{VIDEO_ID}?a=1") == VIDEO_ID
    assert downloader._resolve_video_id(f"https://www.douyin.com/note/{VIDEO_ID}") == VIDEO_ID
    assert downloader._resolve_video_id(f"https://www.iesdouyin.com/share/video/{VIDEO_ID}/") == VIDEO_ID
    assert downloader._resolve_video_id(f"https://www.douyin.com/discover?modal_id={VIDEO_ID}") == VIDEO_ID


def test_resolve_video_id_rejects_unresolvable_url(tmp_path: Path) -> None:
    downloader = DouyinShareDownloader(tmp_path)
    with pytest.raises(WorkflowError, match="视频 ID"):
        downloader._resolve_video_id("https://www.douyin.com/user/abc")


# --------------------------------------------------------------------------- cookie header


def test_cookie_header_only_takes_douyin_domain(cookies_env, tmp_path: Path) -> None:
    header = DouyinShareDownloader(tmp_path)._douyin_cookie_header()
    assert "ttwid=fake-ttwid" in header
    assert "s_v_web_id=verify_me" in header
    assert "should-not-appear" not in header


def test_missing_ttwid_raises_friendly_error(tmp_path: Path, monkeypatch) -> None:
    cookies = _write_cookies(tmp_path, include_ttwid=False)
    monkeypatch.setattr(downloader_module, "resolve_cookies_file", lambda _settings: cookies)
    downloader = DouyinShareDownloader(tmp_path)

    with pytest.raises(WorkflowError, match="ttwid"):
        downloader._fetch_item(VIDEO_ID)


def test_no_cookies_file_raises_friendly_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloader_module, "resolve_cookies_file", lambda _settings: None)
    downloader = DouyinShareDownloader(tmp_path)

    with pytest.raises(WorkflowError, match="ttwid"):
        downloader._fetch_item(VIDEO_ID)


# --------------------------------------------------------------------------- fetch item


def test_fetch_item_parses_router_data(cookies_env, monkeypatch, tmp_path: Path) -> None:
    _patch_share_page(monkeypatch, _share_item())
    item = DouyinShareDownloader(tmp_path)._fetch_item(VIDEO_ID)
    assert item["desc"] == "测试视频标题"
    assert item["video"]["play_addr"]["uri"] == "v0d00fg10000da9afifog65s10jv8vk0"


def test_fetch_item_without_data_raises_friendly_error(cookies_env, monkeypatch, tmp_path: Path) -> None:
    _patch_share_page(monkeypatch, None)
    downloader = DouyinShareDownloader(tmp_path)

    with pytest.raises(WorkflowError, match="私密"):
        downloader._fetch_item(VIDEO_ID)


def test_fetch_item_without_router_data_raises(cookies_env, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(downloader_module.httpx, "get", lambda *a, **k: FakeResponse("<html>empty</html>"))
    downloader = DouyinShareDownloader(tmp_path)

    with pytest.raises(WorkflowError, match="_ROUTER_DATA"):
        downloader._fetch_item(VIDEO_ID)


# --------------------------------------------------------------------------- download flow


def test_download_audio_full_flow(cookies_env, monkeypatch, tmp_path: Path) -> None:
    _patch_share_page(monkeypatch, _share_item())
    play_url = DouyinShareDownloader.PLAY_URL.format(uri="v0d00fg10000da9afifog65s10jv8vk0")
    _patch_stream(monkeypatch, {play_url: [_big_bytes(), b"tail"]})
    downloader = DouyinShareDownloader(tmp_path / "tmp")

    media = downloader.download_audio(f"https://www.douyin.com/video/{VIDEO_ID}", "task1")

    assert media.audio_path is not None and media.audio_path.is_file()
    assert media.audio_path.read_bytes() == _big_bytes() + b"tail"
    assert media.audio_path.name == f"douyin-{VIDEO_ID}.mp4"
    assert media.title == "测试视频标题"
    assert media.author == "测试作者"
    assert media.duration == 91.5  # 91500 ms -> 91.5 s
    assert media.source_url == f"https://www.douyin.com/video/{VIDEO_ID}"


def test_download_audio_without_play_addr_raises(cookies_env, monkeypatch, tmp_path: Path) -> None:
    item = _share_item()
    item["video"]["play_addr"] = {}
    _patch_share_page(monkeypatch, item)
    downloader = DouyinShareDownloader(tmp_path / "tmp")

    with pytest.raises(WorkflowError, match="图文"):
        downloader.download_audio(f"https://www.douyin.com/video/{VIDEO_ID}", "task1")


def test_download_mp4_falls_back_to_url_list(cookies_env, monkeypatch, tmp_path: Path) -> None:
    _patch_share_page(monkeypatch, _share_item())
    play_url = DouyinShareDownloader.PLAY_URL.format(uri="v0d00fg10000da9afifog65s10jv8vk0")
    fallback_url = "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=fallback"

    def flaky_stream(method: str, url: str, **kwargs):
        if url == play_url:
            raise downloader_module.httpx.ConnectError("simulated CDN failure")
        return FakeStreamContext(FakeStreamResponse([_big_bytes()]))

    monkeypatch.setattr(downloader_module.httpx, "stream", flaky_stream)
    monkeypatch.setattr(downloader_module, "sleep", lambda seconds: None)
    downloader = DouyinShareDownloader(tmp_path / "tmp")

    media = downloader.download_audio(f"https://www.douyin.com/video/{VIDEO_ID}", "task1")

    assert media.audio_path is not None and media.audio_path.stat().st_size >= 10_000


def test_download_mp4_rejects_tiny_response(cookies_env, monkeypatch, tmp_path: Path) -> None:
    item = _share_item()
    item["video"]["play_addr"] = {"uri": None, "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=tiny"]}
    _patch_share_page(monkeypatch, item)
    _patch_stream(monkeypatch, {"https://aweme.snssdk.com/aweme/v1/playwm/?video_id=tiny": [b"blocked"]})
    monkeypatch.setattr(downloader_module, "sleep", lambda seconds: None)
    downloader = DouyinShareDownloader(tmp_path / "tmp")

    with pytest.raises(WorkflowError, match="下载失败"):
        downloader.download_audio(f"https://www.douyin.com/video/{VIDEO_ID}", "task1")
    # 失败后不应留下脏文件
    task_dir = tmp_path / "tmp" / "task1"
    assert not list(task_dir.glob("*.mp4"))


def test_download_video_reuses_existing_audio_file(cookies_env, monkeypatch, tmp_path: Path) -> None:
    tmp_dir = tmp_path / "tmp"
    task_dir = tmp_dir / "task1"
    task_dir.mkdir(parents=True)
    existing = task_dir / f"douyin-{VIDEO_ID}.mp4"
    existing.write_bytes(_big_bytes())

    # 复用成功时不应再请求分享页/下载
    def boom(*args, **kwargs):
        raise AssertionError("应复用已下载文件，不应发起网络请求")

    monkeypatch.setattr(downloader_module.httpx, "get", boom)
    monkeypatch.setattr(downloader_module.httpx, "stream", boom)

    video_path = DouyinShareDownloader(tmp_dir).download_video(
        f"https://www.douyin.com/video/{VIDEO_ID}", "task1"
    )

    assert video_path.name == f"{VIDEO_ID}.video.mp4"
    assert video_path.read_bytes() == _big_bytes()


def test_download_video_downloads_when_missing(cookies_env, monkeypatch, tmp_path: Path) -> None:
    _patch_share_page(monkeypatch, _share_item())
    play_url = DouyinShareDownloader.PLAY_URL.format(uri="v0d00fg10000da9afifog65s10jv8vk0")
    _patch_stream(monkeypatch, {play_url: [_big_bytes()]})
    downloader = DouyinShareDownloader(tmp_path / "tmp")

    video_path = downloader.download_video(f"https://www.douyin.com/video/{VIDEO_ID}", "task2")

    assert video_path.name == f"{VIDEO_ID}.video.mp4"
    assert video_path.stat().st_size >= 10_000
