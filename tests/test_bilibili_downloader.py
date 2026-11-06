from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

import app.adapters.bilibili as bilibili
import app.adapters.downloader as downloader_module
from app.adapters.bilibili import is_bilibili_video_url, resolve_bilibili_info
from app.adapters.downloader import YtDlpDownloader, select_downloader
from app.errors import WorkflowError


URL = "https://www.bilibili.com/video/BV1fZPQzuERu"


def _detail() -> dict:
    return {
        "bvid": "BV1fZPQzuERu", "title": "测试视频", "duration": 197,
        "owner": {"name": "测试作者"},
        "pages": [{"page": 1, "cid": 123, "part": "第一集", "duration": 197}],
    }


def _play_info() -> dict:
    return {"timelength": 197000, "dash": {
        "audio": [{"id": 30280, "baseUrl": "https://cdn.example/audio.m4s",
                   "codecs": "mp4a.40.2", "bandwidth": 192000}],
        "video": [{"id": 64, "base_url": "https://cdn.example/video.m4s",
                   "codecs": "avc1.640028", "bandwidth": 1200000, "height": 720}],
    }}


@pytest.fixture
def api(monkeypatch):
    state = {"detail": _detail(), "play_info": _play_info(), "code": 0, "status": 200, "view_status": 200, "requests": []}
    real_client = httpx.Client

    def handler(request):
        state["requests"].append(request)
        if request.url.host == "b23.tv":
            return httpx.Response(302, headers={"location": URL + "?p=2"})
        assert request.url.host == "api.bilibili.com", "不得访问视频 HTML"
        if request.url.path.endswith("/pagelist"):
            return httpx.Response(state["status"], json={"code": state["code"], "data": state["detail"]["pages"]})
        key = "detail" if request.url.path.endswith("/view") else "play_info"
        status = state["view_status"] if key == "detail" and state["view_status"] != 200 else state["status"]
        return httpx.Response(status, json={"code": state["code"], "data": state[key]})

    monkeypatch.setattr(bilibili.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw,
    ))
    return state


@pytest.mark.parametrize("url, expected", [
    (URL, True), ("https://m.bilibili.com/video/av123?p=2", True),
    ("https://b23.tv/abc", True), ("https://bili2233.cn/abc", True),
    ("https://www.bilibili.com/bangumi/play/ep123", False),
    ("https://bilibili.com.evil.example/video/BV123", False),
    ("https://example.com/?url=bilibili.com/video/BV123", False),
])
def test_bilibili_video_routing(url, expected):
    assert is_bilibili_video_url(url) is expected


def test_resolves_audio_video_and_metadata_without_html(api):
    info = resolve_bilibili_info(URL, [])
    assert info["title"] == "测试视频"
    assert info["uploader"] == "测试作者"
    assert info["duration"] == 197
    assert info["http_headers"]["Referer"] == "https://www.bilibili.com/"
    assert {f["ext"] for f in info["formats"]} == {"m4a", "mp4"}
    audio, video = info["formats"]
    assert audio["vcodec"] == "none" and audio["tbr"] == 192
    assert video["acodec"] == "none" and video["height"] == 720
    assert len(api["requests"]) == 2
    assert api["requests"][1].url.params["cid"] == "123"


def test_short_url_preserves_requested_page(api):
    api["detail"]["pages"].append({"page": 2, "cid": 456, "part": "第二集", "duration": 99})
    info = resolve_bilibili_info("https://b23.tv/test", [])
    assert info["title"] == "第二集" and info["duration"] == 99
    assert info["id"].endswith("_456")
    assert info["webpage_url"].endswith("?p=2")
    assert api["requests"][-1].url.params["cid"] == "456"


def test_av_link_uses_aid(api):
    resolve_bilibili_info("https://www.bilibili.com/video/av123", [])
    assert api["requests"][0].url.params["aid"] == "123"


@pytest.mark.parametrize("page", ["0", "9", "bad", "-1"])
def test_invalid_page_is_not_silently_replaced(api, page):
    with pytest.raises(WorkflowError, match="分 P"):
        resolve_bilibili_info(URL + "?p=" + page, [])


@pytest.mark.parametrize("code, message", [
    (-352, "风控"), (-412, "风控"), (-101, "登录"), (-404, "不存在"),
])
def test_api_rejection_is_actionable(api, code, message):
    api["code"] = code
    with pytest.raises(WorkflowError, match=message):
        resolve_bilibili_info(URL, [])
    assert len(api["requests"]) == (2 if code in {-352, -412} else 1)


def test_http_412_has_friendly_error_without_repeated_requests(api):
    api["status"] = 412
    with pytest.raises(WorkflowError, match="访问风控"):
        resolve_bilibili_info(URL, [])
    assert [r.url.path for r in api["requests"]] == ["/x/web-interface/view", "/x/player/pagelist"]


def test_view_412_recovers_through_pagelist(api):
    api["view_status"] = 412
    info = resolve_bilibili_info(URL, [])
    assert info["title"] == "第一集" and info["duration"] == 197
    assert info["uploader"] is None
    assert len(info["formats"]) == 2
    assert [r.url.path for r in api["requests"]] == [
        "/x/web-interface/view", "/x/player/pagelist", "/x/player/playurl",
    ]


def test_missing_streams_does_not_return_success(api):
    api["play_info"] = {}
    with pytest.raises(WorkflowError, match="未返回可用"):
        resolve_bilibili_info(URL, [])


def test_legacy_segments_are_all_retained(api):
    api["play_info"] = {"durl": [
        {"url": "https://cdn.example/1.flv", "length": 100000},
        {"url": "https://cdn.example/2.flv", "length": 97000},
    ]}
    info = resolve_bilibili_info(URL, [])
    fmt = info["formats"][0]
    assert fmt["protocol"] == "http_dash_segments"
    assert len(fmt["fragments"]) == 2
    assert sum(f["duration"] for f in fmt["fragments"]) == 197


def test_cookies_are_scoped_to_bilibili(api, tmp_path):
    cookies_file = tmp_path / "cookies.txt"
    contents = (
        "# Netscape HTTP Cookie File\n"
        ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tbili-session\n"
        ".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tdouyin-session\n"
    )
    cookies_file.write_text(contents, encoding="utf-8")
    resolve_bilibili_info(URL, ["--cookies", str(cookies_file)])
    assert cookies_file.read_text(encoding="utf-8") == contents
    for request in api["requests"]:
        assert request.headers["cookie"] == "SESSDATA=bili-session"


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_downloads_use_api_info_instead_of_webpage(api, monkeypatch, tmp_path, kind):
    downloader = select_downloader(URL, tmp_path, use_mock=False)
    monkeypatch.setattr(downloader, "_yt_dlp_command", lambda: ["yt-dlp"])
    monkeypatch.setattr(downloader_module, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(downloader_module, "resolve_cookies_file", lambda _: None)

    def run(command):
        cmd = command()
        assert URL not in cmd
        source = Path(cmd[cmd.index("--load-info-json") + 1])
        info = json.loads(source.read_text(encoding="utf-8"))
        filename = "test.mp3" if kind == "audio" else "test.video.mp4"
        (tmp_path / "task" / filename).write_bytes(b"downloaded")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(info), stderr="")

    monkeypatch.setattr(downloader_module, "_run_yt_dlp_with_cookie_recovery", run)
    if kind == "audio":
        media = downloader.download_audio(URL, "task")
        assert media.title == "测试视频" and media.duration == 197
        assert media.audio_path.read_bytes() == b"downloaded"
    else:
        assert downloader.download_video(URL, "task").read_bytes() == b"downloaded"


def test_non_bilibili_inputs_remain_urls(tmp_path):
    url = "https://example.com/video"
    assert YtDlpDownloader._input_args(url, tmp_path, None) == [url]
