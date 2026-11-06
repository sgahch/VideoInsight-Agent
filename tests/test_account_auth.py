from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.account_auth as accounts
import app.main as main
import app.security as security
from app.config import Settings
from app.task_store import SQLiteTaskStore


@pytest.fixture
def client(monkeypatch, tmp_path):
    config = Settings(
        mock_mode=True, auth_token=None, auth_username="ynchen",
        auth_password_hash=accounts.hash_password("test-password"),
        auth_session_secret="test-session-secret",
        tmp_dir=tmp_path / "tmp", media_dir=tmp_path / "media",
        results_dir=tmp_path / "results", db_path=tmp_path / "tasks.db",
    )
    config.ensure_dirs()
    for module in (main, security, accounts):
        monkeypatch.setattr(module, "settings", config)
    monkeypatch.setattr(main, "task_store", SQLiteTaskStore(config.db_path))
    accounts._attempts.clear()
    return TestClient(main.app)


def sign_in(client):
    return client.post("/auth/login", json={"username": "ynchen", "password": "test-password"})


def test_anonymous_page_redirects_and_api_is_protected(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/comments", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/comment-api/platforms").status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/health").json()["login_required"] is True
    for method, path in [("GET", "/tasks"), ("GET", "/docs"), ("GET", "/openapi.json"), ("POST", "/analyze-file"), ("GET", "/tasks/unknown/media"), ("POST", "/llm/test")]:
        assert client.request(method, path).status_code == 401
    assert client.post("/auth/session", json={"token": "anything"}).status_code == 401


def test_login_and_logout_with_cookie(client):
    response = sign_in(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "test-password" not in cookie
    assert client.get("/").status_code == 200
    assert client.get("/tasks").status_code == 200
    assert client.get("/auth/me").json()["username"] == "ynchen"
    assert client.get("/login", follow_redirects=False).headers["location"] == "/"
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/tasks").status_code == 401


@pytest.mark.parametrize("username,password", [("wrong", "test-password"), ("ynchen", "wrong"), ("用户名", "密码")])
def test_wrong_credentials_are_rejected(client, username, password):
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401
    assert accounts.SESSION_COOKIE_NAME not in response.cookies


def test_forged_and_expired_sessions_are_rejected(client, monkeypatch):
    valid = accounts.create_session()
    for value in ["forged", valid[:-1] + ("a" if valid[-1] != "a" else "b"), "1.nonce.%C3%A9"]:
        client.cookies.set(accounts.SESSION_COOKIE_NAME, value)
        assert client.get("/tasks").status_code == 401
    client.cookies.set(accounts.SESSION_COOKIE_NAME, valid)
    real_time = accounts.time.time()
    monkeypatch.setattr(accounts.time, "time", lambda: real_time + accounts.SESSION_MAX_AGE + 1)
    assert client.get("/tasks").status_code == 401


def test_password_change_invalidates_sessions(client, monkeypatch):
    sign_in(client)
    assert client.get("/tasks").status_code == 200
    monkeypatch.setattr(accounts, "settings", replace(accounts.settings, auth_password_hash=accounts.hash_password("new-password")))
    assert client.get("/tasks").status_code == 401


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post("/auth/login", json={"username": "ynchen", "password": "bad"}).status_code == 401
    response = sign_in(client)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"


def test_cross_origin_login_and_logout_are_rejected(client):
    for path in ["/auth/login", "/auth/logout"]:
        assert client.post(path, json={"username": "ynchen", "password": "test-password"}, headers={"Origin": "https://other.example"}).status_code == 403


def test_password_hash_is_salted_and_verified():
    first, second = accounts.hash_password("same"), accounts.hash_password("same")
    assert first != second
    assert accounts.verify_password("same", first)
    assert not accounts.verify_password("wrong", first)
    assert not accounts.verify_password("same", "invalid")


def test_server_model_metadata_excludes_credentials(client, monkeypatch):
    sign_in(client)
    config = replace(main.settings, llm_provider="deepseek", deepseek_api_key="private-test-key", llm_model="deepseek-flash")
    monkeypatch.setattr(main, "settings", config)
    response = client.get("/config")
    assert response.json()["llm"]["configured"] is True
    assert response.json()["llm"]["model"] == "deepseek-flash"
    assert "private-test-key" not in response.text


def test_default_model_probe_ignores_untrusted_endpoint(client, monkeypatch):
    sign_in(client)
    monkeypatch.setattr(main, "settings", replace(main.settings, llm_provider="deepseek", deepseek_api_key="private-test-key"))
    calls = []

    class Probe:
        def __init__(self, config, **kwargs):
            calls.append(kwargs)

        def probe(self):
            return {"ok": True}

    monkeypatch.setattr(main, "OpenAICompatibleAnalyzer", Probe)
    response = client.post("/llm/test", json={"llm_base_url": "https://attacker.example", "llm_provider": "custom"})
    assert response.json()["ok"] is True
    assert calls == [{}]
