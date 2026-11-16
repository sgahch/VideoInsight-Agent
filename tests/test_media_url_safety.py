from __future__ import annotations

import socket
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.account_auth as accounts
import app.adapters.downloader as downloader
import app.comments_api as comments
import app.main as main
import app.media_urls as media_urls
import app.security as security
import app.url_safety as url_safety
from app.account_auth import task_account_scope
from app.account_store import SQLiteAccountStore
from app.adapters.comment_fetcher import detect_platform_cn
from app.config import Settings
from app.errors import WorkflowError
from app.media_urls import canonical_media_url
from app.task_store import SQLiteTaskStore
from app.url_safety import PublicOnlyTransport, UnsafeURL
from app.workflow import detect_platform


OWNER = "account:" + "a" * 32
BILI = "https://www.bilibili.com/video/BV1fZPQzuERu"
XHS = "https://www.xiaohongshu.com/explore/674051740000000007027a15"
YOUTUBE = "https://www.youtube.com/watch?v=BaW_jenozKc"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])


@pytest.mark.parametrize("url, expected", [
    ("https://m.douyin.com/share/video/123?redirect=http://127.0.0.1", "https://www.douyin.com/video/123"),
    ("https://www.douyin.com/discover?modal_id=123", "https://www.douyin.com/video/123"),
    ("https://www.kuaishou.com/short-video/abc123", "https://www.kuaishou.com/short-video/abc123"),
    (BILI + "?p=2&next=http://127.0.0.1", BILI + "?p=2"),
    ("https://m.bilibili.com/video/av123/", "https://www.bilibili.com/video/av123"),
    (XHS.replace("/explore/", "/discovery/item/") + "?xsec_token=abc_123%3D&redirect=http://127.0.0.1",
     XHS + "?xsec_token=abc_123%3D"),
    ("https://youtu.be/BaW_jenozKc?url=http://127.0.0.1", YOUTUBE),
    ("https://m.youtube.com/shorts/BaW_jenozKc?list=private", YOUTUBE),
    ("https://youtube.com/watch?v=BaW_jenozKc&next=http://127.0.0.1", YOUTUBE),
    ("https://b23.tv/test?next=http://127.0.0.1", "https://b23.tv/test"),
    ("https://xhslink.com/a/token?target=http://127.0.0.1", "https://xhslink.com/a/token"),
    ("https://xhslink.cn/o/6990bRO5ehG", "https://xhslink.cn/o/6990bRO5ehG"),
])
def test_official_single_video_links_are_canonical_and_drop_other_parameters(url, expected):
    assert canonical_media_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private/video?kuaishou.com",
    "http://169.254.169.254/?douyin.com",
    "http://[::1]/?bilibili.com",
    "https://example.com/?url=https://www.douyin.com/video/123",
    "https://www.douyin.com.evil.example/video/123",
    "https://user@www.douyin.com/video/123",
    "https://www.douyin.com:8443/video/123",
    "https://www.douyin.com/video/123#fragment",
    "ftp://www.douyin.com/video/123",
    "https://www.douyin.com/video/123\r\nnext",
    "https://www.douyin.com\\@127.0.0.1/video/123",
    "https://www.douyin.com/video/123/../../private",
    "https://www.bilibili.com/redirect?url=http://127.0.0.1",
    "https://www.bilibili.com/video/BV1fZPQzuERu?p=0",
    "https://www.bilibili.com/video/BV1fZPQzuERu?p=1&p=2",
    "https://www.xiaohongshu.com/explore/not-an-id",
    XHS + "?xsec_token=http://127.0.0.1",
    "https://www.youtube.com/redirect?q=http://127.0.0.1",
    YOUTUBE + "&v=another_123",
    "https://b23.tv/redirect/elsewhere",
])
def test_unsafe_hosts_schemes_and_non_video_shapes_are_rejected(url):
    with pytest.raises(UnsafeURL):
        canonical_media_url(url)


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"],
    ["93.184.216.34", "192.168.1.1"],
])
def test_even_official_hostname_must_resolve_only_to_public_addresses(monkeypatch, addresses):
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
    ])
    with pytest.raises(UnsafeURL):
        canonical_media_url(BILI)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private/video?kuaishou.com",
    "https://kuaishou.com.evil.example/short-video/abc",
    "https://example.com/?douyin.com/video/123",
    "https://example.com/?url=bilibili.com/video/BV123",
])
def test_platform_routing_does_not_use_substring_matches(url, tmp_path):
    assert not downloader.is_douyin_url(url)
    assert not downloader.is_kuaishou_url(url)
    assert detect_platform(url) == detect_platform_cn(url) == "未知平台"
    assert isinstance(downloader.select_downloader(url, tmp_path, False), downloader.YtDlpDownloader)
    with task_account_scope(OWNER), pytest.raises(WorkflowError):
        downloader.select_downloader(url, tmp_path, False)


@pytest.mark.parametrize("short, target, expected", [
    ("https://b23.tv/test", BILI + "?p=2&next=http://127.0.0.1", BILI + "?p=2"),
    ("https://xhslink.com/a/token", XHS + "?xsec_token=abc_123%3D", XHS + "?xsec_token=abc_123%3D"),
    ("https://xhslink.cn/o/token", XHS + "?xsec_token=abc_123%3D", XHS + "?xsec_token=abc_123%3D"),
    ("https://v.douyin.com/test", "https://www.douyin.com/note/123", "https://www.douyin.com/video/123"),
    ("https://v.kuaishou.com/test", "https://v.m.chenzhongtech.com/fw/photo/abc123?photoId=abc123",
     "https://www.kuaishou.com/short-video/abc123"),
])
def test_short_links_are_pinned_then_normalized_before_cli(monkeypatch, short, target, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    monkeypatch.setattr(media_urls, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    assert canonical_media_url(short, resolve_short=True) == expected
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == httpx.URL(short).host


@pytest.mark.parametrize("target", [
    "http://127.0.0.1/private",
    "https://evil.example/video/BV1fZPQzuERu",
    "https://b23.tv.evil.example/test",
    "https://www.bilibili.com/redirect?url=http://127.0.0.1",
    "https://www.douyin.com/video/123",
    BILI + "#fragment",
])
def test_short_links_never_follow_unknown_internal_or_cross_platform_redirects(monkeypatch, target):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    monkeypatch.setattr(media_urls, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    with pytest.raises(UnsafeURL):
        canonical_media_url("https://b23.tv/test", resolve_short=True)
    assert len(requests) == 1, "The rejected redirect must never be requested."


def test_short_link_checks_each_host_even_when_next_hop_is_another_short(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.headers["host"])
        target = "https://www.b23.tv/again" if len(requests) == 1 else "http://127.0.0.1/private"
        return httpx.Response(302, headers={"location": target})

    monkeypatch.setattr(media_urls, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    with pytest.raises(UnsafeURL):
        canonical_media_url("https://b23.tv/test", resolve_short=True)
    assert requests == ["b23.tv", "www.b23.tv"]


@pytest.mark.parametrize("kind", ["get", "stream"])
def test_native_requests_block_private_redirects_before_connecting(monkeypatch, tmp_path, kind):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(downloader, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    with task_account_scope(OWNER), pytest.raises(WorkflowError, match="非公网"):
        if kind == "get":
            downloader._media_get("https://m.douyin.com/share/video/123", follow_redirects=True)
        else:
            downloader.DouyinShareDownloader(tmp_path)._download_candidate(
                "https://cdn.example/video.mp4", tmp_path / "video.mp4",
            )
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert not (tmp_path / "video.mp4").exists()


def test_native_media_download_cannot_use_private_url_from_page(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text='{"mainMvUrls": [{"url": "http://169.254.169.254/private"}]}')

    monkeypatch.setattr(downloader, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    with task_account_scope(OWNER):
        instance = downloader.KuaishouDownloader(tmp_path)
        url, _, _ = instance._extract_media("abc123")
        with pytest.raises(WorkflowError, match="非公网"):
            instance._download_mp4(url, tmp_path / "video.mp4")
    assert len(requests) == 1
    assert not (tmp_path / "video.mp4").exists()


def test_native_redirect_rechecks_dns_and_never_reuses_a_private_rebound_answer(monkeypatch):
    lookups, requests = [], []

    def dns(host, *args, **kwargs):
        lookups.append(host)
        address = "93.184.216.34" if len(lookups) == 1 else "10.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://cdn.example/final.mp4"})

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", dns)
    monkeypatch.setattr(downloader, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    with task_account_scope(OWNER), pytest.raises(WorkflowError, match="非公网"):
        downloader._media_get("https://cdn.example/video.mp4", follow_redirects=True)
    assert lookups == ["cdn.example", "cdn.example"]
    assert len(requests) == 1 and requests[0].url.host == "93.184.216.34"


def test_recovered_tasks_and_direct_cli_inputs_are_checked_again(tmp_path):
    with task_account_scope(OWNER):
        for mock in (False, True):
            with pytest.raises(WorkflowError):
                downloader.select_downloader("http://127.0.0.1/?kuaishou.com", tmp_path, mock)
        with pytest.raises(WorkflowError):
            downloader.YtDlpDownloader._input_args("http://127.0.0.1/video", tmp_path, None)
        assert downloader.YtDlpDownloader._input_args(
            XHS + "?redirect=http://127.0.0.1&xsec_token=abc", tmp_path, None,
        ) == [XHS + "?xsec_token=abc"]
        assert downloader.YtDlpDownloader._input_args(
            "https://youtu.be/BaW_jenozKc?next=http://127.0.0.1", tmp_path, None,
        ) == [YOUTUBE]
    assert downloader.YtDlpDownloader._input_args("http://127.0.0.1/video", tmp_path, None) == [
        "http://127.0.0.1/video",
    ], "Local administrator's generic yt-dlp remains available."


@pytest.fixture
def account_client(monkeypatch, tmp_path):
    config = Settings(
        auth_token=None, auth_username="owner", auth_password_hash=accounts.hash_password("admin-test-password"),
        auth_session_secret="media-url-test-secret", db_path=tmp_path / "tasks.db",
        media_dir=tmp_path / "media", tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results",
    )
    config.ensure_dirs()
    for module in (accounts, main, security):
        monkeypatch.setattr(module, "settings", config)
    store = SQLiteAccountStore(config.db_path)
    now = int(time.time())
    store.reserve_code("alice@example.com", "digest", "generation", [], now)
    user = store.register("alice@example.com", "digest", "test-password", accounts.hash_password, now)
    tasks = SQLiteTaskStore(config.db_path)
    monkeypatch.setattr(main, "task_store", tasks)
    submitted = []
    monkeypatch.setattr(main, "task_queue", SimpleNamespace(submit=lambda *args: submitted.append(args)))
    client = TestClient(main.app)
    client.cookies.set(accounts.SESSION_COOKIE_NAME, accounts.create_user_session(user))
    return client, tasks, submitted, config


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private/video?kuaishou.com",
    "https://example.com/video",
    "https://www.bilibili.com/redirect?url=http://127.0.0.1",
])
def test_account_analyze_rejects_before_task_creation(account_client, url):
    client, _, submitted, _ = account_client
    response = client.post("/analyze", json={"url": url, "use_mock": False})
    assert response.status_code == 400, response.text
    assert not submitted


@pytest.mark.parametrize("url, expected", [
    (BILI + "?p=2&next=http://127.0.0.1", BILI + "?p=2"),
    (XHS + "?xsec_token=abc&redirect=http://127.0.0.1", XHS + "?xsec_token=abc"),
    ("https://youtu.be/BaW_jenozKc?next=http://127.0.0.1", YOUTUBE),
])
def test_account_analyze_keeps_supported_platforms_and_stores_only_canonical_url(account_client, url, expected):
    client, tasks, submitted, _ = account_client
    response = client.post("/analyze", json={"url": url, "use_mock": False})
    assert response.status_code == 202, response.text
    task = tasks.get(response.json()["task_id"])
    assert str(task.request.url) == expected
    assert task.request.client_id.startswith("account:")
    assert len(submitted) == 1


def test_account_comment_submission_rejects_before_cookie_sync_or_service(monkeypatch):
    calls = []
    monkeypatch.setattr(comments, "service_request", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(comments, "prepare_account_cookies", lambda *args: calls.append(args))
    with pytest.raises(HTTPException) as error:
        comments.start_for_analysis("http://127.0.0.1/private?kuaishou.com", OWNER, Settings())
    assert error.value.status_code == 400
    assert calls == []


@pytest.mark.parametrize("stored", [
    "https://www.douyin.com/redirect?url=http://127.0.0.1",
    BILI + "?redirect=http://127.0.0.1",
])
def test_account_cannot_retry_an_old_comment_job_without_a_canonical_video_url(account_client, monkeypatch, stored):
    client, _, _, _ = account_client
    calls = []

    def service(method, path, **kwargs):
        calls.append((method, path))
        return {"url": stored}

    monkeypatch.setattr(comments, "service_request", service)
    monkeypatch.setattr(comments, "prepare_account_cookies", lambda *args: calls.append(("sync", "cookies")))
    response = client.post("/comment-api/jobs/" + "1" * 32 + "/retry")
    assert response.status_code == 400, response.text
    assert calls == [("GET", "/jobs/" + "1" * 32)]


def test_account_can_retry_a_canonical_comment_job(account_client, monkeypatch):
    client, _, _, _ = account_client
    calls = []

    def service(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"url": BILI}

    monkeypatch.setattr(comments, "service_request", service)
    monkeypatch.setattr(comments, "prepare_account_cookies", lambda *args: None)
    response = client.post("/comment-api/jobs/" + "1" * 32 + "/retry")
    assert response.status_code == 200, response.text
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert all(call[2]["params"]["owner"].startswith("account:") for call in calls)
