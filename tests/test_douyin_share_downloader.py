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


def _patch_fetch_sequence(monkeypatch, responses: list[FakeResponse | Exception]):
    sequence = iter(responses)
    calls = []
    waits = []

    def get(url, **kwargs):
        calls.append(url)
        response = next(sequence)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(downloader_module.httpx, "get", get)
    monkeypatch.setattr(downloader_module, "sleep", waits.append)
    return calls, waits


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


def test_fetch_item_without_router_data_has_bounded_retries(cookies_env, monkeypatch, tmp_path: Path) -> None:
    calls, waits = _patch_fetch_sequence(monkeypatch, [FakeResponse("<html>empty</html>")] * 3)
    downloader = DouyinShareDownloader(tmp_path)

    with pytest.raises(WorkflowError, match="已尝试 3 次") as caught:
        downloader._fetch_item(VIDEO_ID)
    assert "结构变化" not in str(caught.value)
    assert "稍后重试" in str(caught.value)
    assert len(calls) == 3
    assert waits == [1, 2]


@pytest.mark.parametrize("initial", [
    FakeResponse("<html>loading</html>"),
    FakeResponse("<script>window._ROUTER_DATA = {truncated</script>"),
    FakeResponse("<html>temporarily unavailable</html>", 503),
    downloader_module.httpx.ReadTimeout("temporary timeout"),
])
def test_fetch_item_recovers_transient_failures(initial, cookies_env, monkeypatch, tmp_path: Path) -> None:
    calls, waits = _patch_fetch_sequence(monkeypatch, [initial, FakeResponse(_share_page_html(_share_item()))])
    item = DouyinShareDownloader(tmp_path)._fetch_item(VIDEO_ID)
    assert item["desc"] == "测试视频标题"
    assert len(calls) == 2
    assert waits == [1]


def test_fetch_item_network_failures_stop_at_limit(cookies_env, monkeypatch, tmp_path: Path) -> None:
    calls, waits = _patch_fetch_sequence(monkeypatch, [downloader_module.httpx.ConnectError("offline")] * 3)
    with pytest.raises(WorkflowError, match="已尝试 3 次"):
        DouyinShareDownloader(tmp_path)._fetch_item(VIDEO_ID)
    assert len(calls) == 3
    assert waits == [1, 2]


@pytest.mark.parametrize(("status", "message"), [(401, "登录"), (403, "验证"), (429, "访问频率"), (404, "404")])
def test_fetch_item_does_not_repeat_explicit_denials(status, message, cookies_env, monkeypatch, tmp_path: Path) -> None:
    calls, waits = _patch_fetch_sequence(monkeypatch, [FakeResponse("<html>denied</html>", status)])
    with pytest.raises(WorkflowError, match=message):
        DouyinShareDownloader(tmp_path)._fetch_item(VIDEO_ID)
    assert len(calls) == 1
    assert waits == []


def test_fetch_item_reports_manual_verification_without_retry(cookies_env, monkeypatch, tmp_path: Path) -> None:
    calls, waits = _patch_fetch_sequence(monkeypatch, [FakeResponse("<html><title>抖音安全验证</title></html>")])
    with pytest.raises(WorkflowError, match="手动完成验证"):
        DouyinShareDownloader(tmp_path)._fetch_item(VIDEO_ID)
    assert len(calls) == 1
    assert waits == []


def test_fetch_item_allows_script_after_json_assignment(cookies_env, monkeypatch, tmp_path: Path) -> None:
    html = _share_page_html(_share_item()).replace("</script>", "; window.hydrated = true;</script>")
    _patch_fetch_sequence(monkeypatch, [FakeResponse(html)])
    item = DouyinShareDownloader(tmp_path)._fetch_item(VIDEO_ID)
    assert item["desc"] == "测试视频标题"


@pytest.mark.parametrize("data", [None, [], "loading", {"loaderData": []}, {"loaderData": None}])
def test_share_page_rejects_invalid_data_shapes(data, tmp_path: Path) -> None:
    html = "<script>window._ROUTER_DATA = " + json.dumps(data) + ";</script>"
    assert DouyinShareDownloader(tmp_path)._item_from_share_page(html, VIDEO_ID) is None


def test_share_page_does_not_use_another_videos_data(tmp_path: Path) -> None:
    item = {**_share_item(), "aweme_id": "1111111111111111111"}
    with pytest.raises(WorkflowError, match="未返回视频数据"):
        DouyinShareDownloader(tmp_path)._item_from_share_page(_share_page_html(item), VIDEO_ID)


# --------------------------------------------------------------------------- download flow


def test_download_audio_full_flow(cookies_env, monkeypatch, tmp_path: Path) -> None:
    _patch_share_page(monkeypatch, _share_item())
    downloader = DouyinShareDownloader(tmp_path / "tmp")
    # 转写默认取低画质（只要音轨），URL 里应是 _audio_ratio() 而非原画质。
    play_url = DouyinShareDownloader.PLAY_URL.format(
        uri="v0d00fg10000da9afifog65s10jv8vk0", ratio=downloader._audio_ratio()
    )
    _patch_stream(monkeypatch, {play_url: [_big_bytes(), b"tail"]})

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
    downloader = DouyinShareDownloader(tmp_path / "tmp")
    low_url = DouyinShareDownloader.PLAY_URL.format(
        uri="v0d00fg10000da9afifog65s10jv8vk0", ratio=downloader._audio_ratio()
    )
    high_url = DouyinShareDownloader.PLAY_URL.format(
        uri="v0d00fg10000da9afifog65s10jv8vk0", ratio=DouyinShareDownloader.DEFAULT_VIDEO_RATIO
    )

    def flaky_stream(method: str, url: str, **kwargs):
        # 两个画质直链都失败时，才回退到 url_list（带水印直链）
        if url in (low_url, high_url):
            raise downloader_module.httpx.ConnectError("simulated CDN failure")
        return FakeStreamContext(FakeStreamResponse([_big_bytes()]))

    monkeypatch.setattr(downloader_module.httpx, "stream", flaky_stream)
    monkeypatch.setattr(downloader_module, "sleep", lambda seconds: None)

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
    # 只有当「转写下载的画质」就是原画质时才能复用（否则会把降质版当源视频）。
    monkeypatch.setattr(
        DouyinShareDownloader,
        "_audio_ratio",
        lambda self: DouyinShareDownloader.DEFAULT_VIDEO_RATIO,
    )
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
    play_url = DouyinShareDownloader.PLAY_URL.format(
        uri="v0d00fg10000da9afifog65s10jv8vk0",
        ratio=DouyinShareDownloader.DEFAULT_VIDEO_RATIO,
    )
    _patch_stream(monkeypatch, {play_url: [_big_bytes()]})
    downloader = DouyinShareDownloader(tmp_path / "tmp")

    video_path = downloader.download_video(f"https://www.douyin.com/video/{VIDEO_ID}", "task2")

    assert video_path.name == f"{VIDEO_ID}.video.mp4"
    assert video_path.stat().st_size >= 10_000


def test_download_video_does_not_reuse_downgraded_audio(cookies_env, monkeypatch, tmp_path: Path) -> None:
    """转写默认下低画质，源视频必须重新下原画质，不能把降质版当成源视频。"""
    tmp_dir = tmp_path / "tmp"
    task_dir = tmp_dir / "task3"
    task_dir.mkdir(parents=True)
    (task_dir / f"douyin-{VIDEO_ID}.mp4").write_bytes(_big_bytes())

    _patch_share_page(monkeypatch, _share_item())
    high_url = DouyinShareDownloader.PLAY_URL.format(
        uri="v0d00fg10000da9afifog65s10jv8vk0",
        ratio=DouyinShareDownloader.DEFAULT_VIDEO_RATIO,
    )
    _patch_stream(monkeypatch, {high_url: [_big_bytes()]})
    monkeypatch.setattr(downloader_module, "sleep", lambda seconds: None)

    video_path = DouyinShareDownloader(tmp_dir).download_video(
        f"https://www.douyin.com/video/{VIDEO_ID}", "task3"
    )

    assert video_path.name == f"{VIDEO_ID}.video.mp4"
    assert video_path.stat().st_size >= 10_000
