from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

import app.account_auth as accounts
import app.adapters.llm as llm_module
import app.main as main
import app.security as security
from app.adapters.llm import OpenAICompatibleAnalyzer
from app.config import Settings
from app.errors import WorkflowError
from app.url_safety import PublicOnlyTransport


SERVER_KEY = "synthetic-server-api-key-only"
USER_KEY = "synthetic-user-api-key-only"
OWNER = "account:" + "a" * 32


@pytest.fixture
def account_client(monkeypatch, tmp_path):
    config = Settings(
        auth_token=None, auth_password_hash=accounts.hash_password("test-admin-password"),
        auth_session_secret="llm-security-tests-only", db_path=tmp_path / "accounts.db",
        llm_provider="custom", llm_base_url="http://127.0.0.1/trusted-default",
        deepseek_api_key=SERVER_KEY, openai_api_key=None,
    )
    for module in (accounts, main, security):
        monkeypatch.setattr(module, "settings", config)
    store = accounts.account_store()
    now = int(time.time())
    store.reserve_code("user@example.com", "test-digest", "generation", [], now)
    user = store.register("user@example.com", "test-digest", "user-password", accounts.hash_password, now)
    assert user
    client = TestClient(main.app)
    client.cookies.set(accounts.SESSION_COOKIE_NAME, accounts.create_user_session(user))
    return client, config


@pytest.mark.parametrize("case", ["error", "string_error", "html", "nonobject", "malformed", "sample"])
def test_default_probe_never_echoes_server_key_in_errors_or_samples(account_client, monkeypatch, case):
    client, _ = account_client
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer " + SERVER_KEY
        assert url.startswith("http://127.0.0.1/trusted-default")
        request = httpx.Request("POST", url)
        if case == "error":
            return httpx.Response(200, request=request, json={"error": {"message": "debug " + SERVER_KEY}})
        if case == "string_error":
            return httpx.Response(200, request=request, json={"error": "debug " + SERVER_KEY})
        if case == "html":
            return httpx.Response(200, request=request, text="<html>debug " + SERVER_KEY + "</html>")
        if case == "nonobject":
            return httpx.Response(200, request=request, json=["debug " + SERVER_KEY])
        if case == "malformed":
            return httpx.Response(200, request=request, json={"debug": SERVER_KEY})
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": SERVER_KEY}}]})

    monkeypatch.setattr(httpx, "post", post)
    response = client.post("/llm/test", json={
        "llm_provider": "custom", "llm_base_url": "https://untrusted.example/private",
    })
    assert response.status_code == 200
    assert calls
    assert SERVER_KEY not in response.text
    assert "[REDACTED]" in response.json().get("error", response.json().get("sample", ""))
    assert response.json()["ok"] is (case == "sample")


def test_probe_hides_unexpected_exception_details(account_client, monkeypatch):
    client, _ = account_client

    def failure(*args, **kwargs):
        raise RuntimeError("database-secret " + SERVER_KEY)

    monkeypatch.setattr(OpenAICompatibleAnalyzer, "probe", failure)
    response = client.post("/llm/test", json={})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "连接测试失败，请检查模型配置或稍后重试。"}
    assert SERVER_KEY not in response.text and "database-secret" not in response.text


@pytest.mark.parametrize("streaming", [False, True])
def test_stream_and_regular_output_redact_keys_even_when_split_across_events(monkeypatch, streaming):
    def handler(request):
        if streaming:
            frames = [
                {"choices": [{"delta": {"content": "before " + SERVER_KEY[:12]}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": SERVER_KEY[12:] + " after"}, "finish_reason": "stop"}]},
            ]
            body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "before " + SERVER_KEY + " after"}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(httpx, "stream", client.stream)
        monkeypatch.setattr(httpx, "post", client.post)
        analyzer = OpenAICompatibleAnalyzer(
            Settings(deepseek_api_key=SERVER_KEY, openai_api_key=None), api_key=SERVER_KEY,
        )
        answer = "".join(analyzer.chat_stream("question", "transcript")) if streaming else analyzer._complete("test")
    assert answer == "before [REDACTED] after"
    assert SERVER_KEY not in answer


@pytest.mark.parametrize("action", ["probe", "complete", "stream"])
def test_normal_users_cannot_reach_private_llm_destinations(action):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"local-only"}}]}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with accounts.task_account_scope(OWNER):
            analyzer = OpenAICompatibleAnalyzer(
                Settings(), base_url=f"http://127.0.0.1:{server.server_port}/private#",
                api_key=USER_KEY,
            )
        # Policy is captured during construction, including streamed/background use.
        with pytest.raises(WorkflowError, match="公网"):
            if action == "probe":
                analyzer.probe()
            elif action == "stream":
                list(analyzer.chat_stream("question", "transcript"))
            else:
                analyzer._complete("test")
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_normal_user_llm_probe_endpoint_blocks_private_hosts(account_client):
    client, _ = account_client
    response = client.post("/llm/test", json={
        "llm_base_url": "http://127.0.0.1/private", "llm_api_key": USER_KEY,
    })
    assert response.status_code == 200
    assert not response.json()["ok"] and "公网" in response.json()["error"]


@pytest.mark.parametrize("action", ["probe", "complete", "stream"])
def test_normal_user_public_llm_still_completes_all_request_modes(monkeypatch, action):
    requests = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["host"] == "gateway.example"
        assert request.extensions["sni_hostname"] == "gateway.example"
        return httpx.Response(200, json={"choices": [{"message": {"content": "normal"}}]})

    monkeypatch.setattr(llm_module, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    with accounts.task_account_scope(OWNER):
        analyzer = OpenAICompatibleAnalyzer(Settings(), base_url="https://gateway.example/v1", api_key=USER_KEY)
    if action == "probe":
        assert analyzer.probe()["sample"] == "normal"
    elif action == "stream":
        assert "".join(analyzer.chat_stream("question", "transcript")) == "normal"
    else:
        assert analyzer._complete("test") == "normal"
    assert len(requests) == 1


@pytest.mark.parametrize("action", ["probe", "complete", "stream"])
def test_normal_user_public_llm_uses_pinned_transport_and_does_not_follow_redirects(monkeypatch, action):
    requests = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "gateway.example"
        assert request.extensions["sni_hostname"] == "gateway.example"
        assert request.headers["authorization"] == "Bearer " + USER_KEY
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(llm_module, "PublicOnlyTransport", lambda: PublicOnlyTransport(
        transport_factory=lambda: httpx.MockTransport(handler),
    ))
    with accounts.task_account_scope(OWNER):
        analyzer = OpenAICompatibleAnalyzer(Settings(), base_url="https://gateway.example/v1", api_key=USER_KEY)
    with pytest.raises(WorkflowError):
        if action == "probe":
            analyzer.probe()
        elif action == "stream":
            list(analyzer.chat_stream("question", "transcript"))
        else:
            analyzer._complete("test")
    assert len(requests) == 1


def test_trusted_private_default_and_admin_private_override_still_work(monkeypatch):
    requests = []

    def post(url, **kwargs):
        requests.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": "normal"}}],
        })

    monkeypatch.setattr(httpx, "post", post)
    config = Settings(
        llm_provider="custom", llm_base_url="http://10.0.0.8/v1",
        deepseek_api_key=SERVER_KEY, openai_api_key=None,
    )
    with accounts.task_account_scope(OWNER):
        assert OpenAICompatibleAnalyzer(config).probe()["ok"]
    with accounts.task_account_scope(None):
        assert OpenAICompatibleAnalyzer(
            config, base_url="http://127.0.0.1:1234/v1", api_key=USER_KEY,
        ).probe()["ok"]
    assert requests == [
        "http://10.0.0.8/v1/chat/completions", "http://127.0.0.1:1234/v1/chat/completions",
    ]
