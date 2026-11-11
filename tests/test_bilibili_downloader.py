from __future__ import annotations

import json
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

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


@pytest.mark.parametrize("backup_key", ["backupUrl", "backup_url"])
def test_dash_cdn_backups_keep_quality_and_prefer_primary(api, backup_key):
    audio = api["play_info"]["dash"]["audio"][0]
    audio[backup_key] = [
        audio["baseUrl"], "https://backup.example/audio.m4s",
        "https://backup.example/audio.m4s", None, "file:///private",
    ]
    formats = resolve_bilibili_info(URL, [])["formats"]
    audios = [f for f in formats if f["vcodec"] == "none"]
    assert [f["url"] for f in audios] == [audio["baseUrl"], "https://backup.example/audio.m4s"]
    assert [f["source_preference"] for f in audios] == [0, -1]
    assert all(f["tbr"] == 192 for f in audios)
    assert len({f["format_id"] for f in formats}) == len(formats)


def test_legacy_backup_keeps_every_segment(api):
    api["play_info"] = {"durl": [
        {"url": "https://cdn.example/1.flv", "length": 100000,
         "backup_url": ["https://backup.example/1.flv"]},
        {"url": "https://cdn.example/2.flv", "length": 97000},
    ]}
    formats = resolve_bilibili_info(URL, [])["formats"]
    assert len(formats) == 2
    assert [f["url"] for f in formats[1]["fragments"]] == [
        "https://backup.example/1.flv", "https://cdn.example/2.flv",
    ]
    assert all(sum(p["duration"] for p in f["fragments"]) == 197 for f in formats)


def test_missing_legacy_segment_is_not_silently_dropped(api):
    api["play_info"] = {"durl": [
        {"url": "https://cdn.example/1.flv", "length": 100000},
        {"length": 97000},
    ]}
    with pytest.raises(WorkflowError, match="未返回可用"):
        resolve_bilibili_info(URL, [])


def test_retry_refreshes_signed_bilibili_urls(api, monkeypatch, tmp_path):
    instance = YtDlpDownloader(tmp_path)
    monkeypatch.setattr(instance, "_yt_dlp_command", lambda: ["yt-dlp"])
    monkeypatch.setattr(downloader_module, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(downloader_module, "resolve_cookies_file", lambda _: None)

    def run(build):
        command = build()
        source = Path(command[command.index("--load-info-json") + 1])
        assert json.loads(source.read_text(encoding="utf-8"))["formats"][0]["url"].endswith("audio.m4s")
        api["play_info"]["dash"]["audio"][0]["baseUrl"] = "https://cdn.example/refreshed.m4s"
        command = build()
        info = json.loads(source.read_text(encoding="utf-8"))
        assert info["formats"][0]["url"].endswith("refreshed.m4s")
        (source.parent / "test.mp3").write_bytes(b"audio")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(info), stderr="")

    monkeypatch.setattr(downloader_module, "_run_yt_dlp_with_cookie_recovery", run)
    instance.download_audio(URL, "task")
    assert len(api["requests"]) == 4


@pytest.fixture
def cdn():
    state = {"requests": [], "block_primary": False, "block_all": False,
             "data": bytes(range(256)) * 8192}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested_range = self.headers.get("Range", "")
            state["requests"].append((self.path, requested_range))
            if state["block_all"] or not requested_range or (state["block_primary"] and self.path == "/primary"):
                self.send_response(567)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested_range)
            assert match is not None
            start = int(match[1])
            end = min(int(match[2]) if match[2] else len(state["data"]) - 1, len(state["data"]) - 1)
            body = state["data"][start:end + 1]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(state['data'])}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("block_primary", [False, True])
def test_real_yt_dlp_ranges_and_cdn_failover(api, cdn, tmp_path, block_primary):
    cdn["block_primary"] = block_primary
    audio = api["play_info"]["dash"]["audio"][0]
    audio["baseUrl"] = cdn["base"] + "/primary"
    audio["backupUrl"] = [cdn["base"] + "/backup"]
    output = tmp_path / "download.m4a"
    args = YtDlpDownloader._input_args(URL, tmp_path, None)
    assert args[args.index("--retries") + 1] == "2"
    assert "--abort-on-unavailable-fragments" in args
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--ignore-config", "--proxy", "",
         *args, "--retries", "0", "-f", "bestaudio/best", "--no-progress", "-o", str(output)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == cdn["data"], "Range probe must not truncate the complete download"
    assert all(requested_range for _, requested_range in cdn["requests"])
    assert any(path == "/primary" for path, _ in cdn["requests"])
    assert any(path == "/backup" for path, _ in cdn["requests"]) is block_primary
    assert len(cdn["requests"]) < 10


def test_all_cdn_candidates_failing_does_not_report_success(api, cdn, tmp_path):
    cdn["block_all"] = True
    audio = api["play_info"]["dash"]["audio"][0]
    audio["baseUrl"] = cdn["base"] + "/primary"
    audio["backup_url"] = [cdn["base"] + "/backup"]
    api["play_info"]["dash"]["video"] = []
    output = tmp_path / "download.m4a"
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--ignore-config", "--proxy", "",
         *YtDlpDownloader._input_args(URL, tmp_path, None), "--retries", "0",
         "-f", "bestaudio/best", "--no-progress", "-o", str(output)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert not output.exists()
    assert {path for path, _ in cdn["requests"]} == {"/primary", "/backup"}
    assert len(cdn["requests"]) == 2


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
