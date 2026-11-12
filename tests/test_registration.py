from __future__ import annotations

import sqlite3
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.account_auth as accounts
import app.comments_api as comments
import app.main as main
import app.registration as registration
import app.security as security
from app.account_store import MAX_CODE_ATTEMPTS, SQLiteAccountStore
from app.config import Settings
from app.task_store import SQLiteTaskStore


PASSWORD = "registration-test-password"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    config = Settings(
        auth_token=None, auth_username="owner",
        auth_password_hash=accounts.hash_password("admin-test-password"),
        auth_session_secret="registration-test-session-secret",
        registration_enabled=True, smtp_username="sender@example.com", smtp_password="private-test-smtp",
        auth_trusted_proxy_ips="127.0.0.1,::1",
        db_path=tmp_path / "tasks.db", tmp_dir=tmp_path / "tmp",
        media_dir=tmp_path / "media", results_dir=tmp_path / "results",
    )
    config.ensure_dirs()
    for module in (accounts, main, security):
        monkeypatch.setattr(module, "settings", config)
    monkeypatch.setattr(main, "task_store", SQLiteTaskStore(config.db_path))
    monkeypatch.setattr(main, "task_queue", SimpleNamespace(submit=lambda *args: None))
    sent = []
    monkeypatch.setattr(accounts, "send_verification_email", lambda config, email, code: sent.append((email, code)))
    accounts._attempts.clear()
    return TestClient(main.app), sent, config


def signup(client, sent, email="alice@example.com"):
    assert client.post("/auth/register/code", json={"email": email}).status_code == 200
    code = sent[-1][1]
    response = client.post("/auth/register", json={"email": email, "code": code, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return code


def test_verified_registration_logs_in_and_persists_hashed_password(setup):
    client, sent, config = setup
    assert client.get("/register").status_code == 200
    assert client.get("/auth/me").json()["registration_available"]
    code = signup(client, sent, "Alice@EXAMPLE.com")
    me = client.get("/auth/me").json()
    assert me["username"] == "alice@example.com" and me["authenticated"] and not me["is_admin"]
    user = SQLiteAccountStore(config.db_path).by_email("alice@example.com")
    assert accounts.verify_password(PASSWORD, user["password_hash"])
    assert user["password_hash"] != PASSWORD
    with sqlite3.connect(config.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM auth_email_codes").fetchone()[0] == 0
    assert PASSWORD not in client.cookies[accounts.SESSION_COOKIE_NAME]
    assert code not in str(me)
    client.post("/auth/logout")
    assert client.post("/auth/login", json={"username": "ALICE@example.com", "password": PASSWORD}).status_code == 200
    assert client.get("/auth/me").json()["username"] == "alice@example.com"


def test_disabled_or_incomplete_registration_fails_closed(setup, monkeypatch):
    client, _, config = setup
    for updates in [{"registration_enabled": False}, {"smtp_password": None}, {"smtp_username": None}]:
        monkeypatch.setattr(accounts, "settings", replace(config, **updates))
        assert not client.get("/auth/me").json()["registration_available"]
        assert client.post("/auth/register/code", json={"email": "alice@example.com"}).status_code == 503
        assert client.post("/auth/register", json={"email": "alice@example.com", "code": "123456", "password": PASSWORD}).status_code == 503


@pytest.mark.parametrize("email", [
    "not-email", "a@localhost", "a@@example.com", "alice <a@example.com>",
    "a@example.com\r\nBcc:evil@example.com", "a..b@example.com", ".a@example.com", "a@-bad.example",
])
def test_invalid_emails_are_rejected_before_sending(setup, email):
    client, sent, _ = setup
    assert client.post("/auth/register/code", json={"email": email}).status_code == 422
    assert not sent


def test_resend_cooldown_is_persistent_and_code_is_not_returned(setup):
    client, sent, config = setup
    response = client.post("/auth/register/code", json={"email": "alice@example.com"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["retry_after"] == 60 and response.json()["expires_in"] == 600
    assert sent[0][1] not in response.text
    accounts._store.cache_clear()
    response = client.post("/auth/register/code", json={"email": "alice@example.com"})
    assert response.status_code == 429 and 1 <= int(response.headers["retry-after"]) <= 60
    assert len(sent) == 1
    with sqlite3.connect(config.db_path) as connection:
        digest = connection.execute("SELECT digest FROM auth_email_codes").fetchone()[0]
    assert len(digest) == 64 and digest != sent[0][1]


def test_wrong_codes_lock_out_after_five_attempts(setup):
    client, sent, _ = setup
    client.post("/auth/register/code", json={"email": "alice@example.com"})
    real = sent[0][1]
    wrong = "000000" if real != "000000" else "000001"
    for _ in range(MAX_CODE_ATTEMPTS):
        assert client.post("/auth/register", json={"email": "alice@example.com", "code": wrong, "password": PASSWORD}).status_code == 400
    assert client.post("/auth/register", json={"email": "alice@example.com", "code": real, "password": PASSWORD}).status_code == 400
    assert not client.get("/auth/me").json()["authenticated"]


def test_expiry_and_resend_invalidate_old_code(setup, monkeypatch):
    client, sent, _ = setup
    now = accounts.time.time()
    monkeypatch.setattr(accounts.time, "time", lambda: now)
    monkeypatch.setattr(accounts.secrets, "randbelow", lambda _: 123456)
    client.post("/auth/register/code", json={"email": "alice@example.com"})
    monkeypatch.setattr(accounts.time, "time", lambda: now + 601)
    assert client.post("/auth/register", json={"email": "alice@example.com", "code": "123456", "password": PASSWORD}).status_code == 400
    monkeypatch.setattr(accounts.secrets, "randbelow", lambda _: 654321)
    assert client.post("/auth/register/code", json={"email": "alice@example.com"}).status_code == 200
    assert client.post("/auth/register", json={"email": "alice@example.com", "code": "123456", "password": PASSWORD}).status_code == 400
    assert client.post("/auth/register", json={"email": "alice@example.com", "code": "654321", "password": PASSWORD}).status_code == 201


def test_replay_and_existing_email_do_not_create_duplicate_accounts(setup):
    client, sent, config = setup
    code = signup(client, sent)
    client.post("/auth/logout")
    assert client.post("/auth/register", json={"email": "alice@example.com", "code": code, "password": PASSWORD}).status_code == 400
    response = client.post("/auth/register/code", json={"email": "alice@example.com"})
    assert response.status_code == 200 and len(sent) == 1
    with sqlite3.connect(config.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM auth_users").fetchone()[0] == 1


def test_smtp_failure_invalidates_pending_code_without_leaking_details(setup, monkeypatch):
    client, _, _ = setup
    attempted = []

    def fail(config, email, code):
        attempted.append(code)
        raise RuntimeError("smtp-private-password-and-internals")

    monkeypatch.setattr(accounts, "send_verification_email", fail)
    response = client.post("/auth/register/code", json={"email": "alice@example.com"})
    assert response.status_code == 503 and "smtp-private" not in response.text
    assert client.post("/auth/register", json={"email": "alice@example.com", "code": attempted[0], "password": PASSWORD}).status_code == 400


def test_mail_limit_is_shared_across_requests_and_store_instances(setup):
    client, sent, _ = setup
    for index in range(20):
        assert client.post("/auth/register/code", json={"email": f"user{index}@example.com"}).status_code == 200
    accounts._store.cache_clear()
    response = client.post("/auth/register/code", json={"email": "next@example.com"})
    assert response.status_code == 429 and len(sent) == 20


@pytest.mark.parametrize("path", ["/auth/register/code", "/auth/register"])
def test_registration_checks_origin(setup, path):
    client, sent, _ = setup
    response = client.post(path, json={"email": "alice@example.com", "code": "123456", "password": PASSWORD}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and not sent


def test_administrator_email_cannot_be_registered_as_a_different_account(setup, monkeypatch):
    client, sent, config = setup
    monkeypatch.setattr(accounts, "settings", replace(config, auth_username="owner@example.com"))
    monkeypatch.setattr(accounts.secrets, "randbelow", lambda _: 123456)
    assert client.post("/auth/register/code", json={"email": "owner@example.com"}).status_code == 200
    assert not sent
    assert client.post("/auth/register", json={"email": "owner@example.com", "code": "123456", "password": PASSWORD}).status_code == 400


def test_weak_password_and_invalid_code_formats_are_rejected(setup):
    client, sent, _ = setup
    client.post("/auth/register/code", json={"email": "alice@example.com"})
    assert client.post("/auth/register", json={"email": "alice@example.com", "code": sent[0][1], "password": "short"}).status_code == 422
    for code in ["", "12345", "1234567", "１２３４５６", "abcdef"]:
        assert client.post("/auth/register", json={"email": "alice@example.com", "code": code, "password": PASSWORD}).status_code == 422


def test_account_bound_tasks_ignore_body_header_and_cookie_ids(setup):
    alice, sent, _ = setup
    signup(alice, sent)
    task = alice.post("/analyze", json={"url": "https://example.com/video", "client_id": "victim-browser"}, headers={"X-Client-ID": "another-browser"}).json()
    task_id = task["task_id"]
    stored = main.task_store.get(task_id)
    assert stored.request.client_id.startswith("account:")
    assert alice.get(f"/tasks/{task_id}", headers={"X-Client-ID": "different-device"}).status_code == 200
    bob = TestClient(main.app)
    signup(bob, sent, "bob@example.com")
    forged = {"X-Client-ID": stored.request.client_id}
    assert not bob.get("/tasks", headers=forged).json()
    for method, path in [
        ("GET", f"/tasks/{task_id}"), ("DELETE", f"/tasks/{task_id}"),
        ("POST", f"/tasks/{task_id}/used"), ("GET", f"/tasks/{task_id}/media"),
        ("GET", f"/tasks/{task_id}/video"),
    ]:
        assert bob.request(method, path, headers=forged).status_code == 404
    alice.post("/auth/logout")
    alice.post("/auth/login", json={"username": "alice@example.com", "password": PASSWORD})
    assert alice.get(f"/tasks/{task_id}").status_code == 200
    assert accounts.account_owner() is None


def test_legacy_admin_history_is_preserved_and_reserved_owner_cannot_be_spoofed(setup):
    client, sent, _ = setup
    client.post("/auth/login", json={"username": "owner", "password": "admin-test-password"})
    task_id = client.post("/analyze", json={"url": "https://example.com/legacy"}, headers={"X-Client-ID": "legacy-browser"}).json()["task_id"]
    client.post("/auth/logout")
    signup(client, sent)
    assert client.get(f"/tasks/{task_id}", headers={"X-Client-ID": "legacy-browser"}).status_code == 404
    assert not client.get("/tasks").json()
    client.post("/auth/logout")
    client.post("/auth/login", json={"username": "owner", "password": "admin-test-password"})
    assert client.get(f"/tasks/{task_id}", headers={"X-Client-ID": "legacy-browser"}).status_code == 200
    assert client.get("/tasks", headers={"X-Client-ID": "account:" + "a" * 32}).status_code == 403


def test_registered_users_cannot_change_shared_platform_cookies(setup):
    client, sent, _ = setup
    signup(client, sent)
    for method, path in [
        ("POST", "/cookies"), ("POST", "/cookies/qr/start"),
        ("GET", "/cookies/qr/unknown/status"), ("POST", "/cookies/browser/start"),
        ("PUT", "/comment-api/platforms/bili/cookies"), ("DELETE", "/comment-api/platforms/bili/cookies"),
    ]:
        assert client.request(method, path).status_code == 403
    assert client.get("/cookies/status").status_code == 200


def test_comment_owner_is_also_account_bound(setup, monkeypatch):
    client, sent, _ = setup
    signup(client, sent)
    calls = []
    monkeypatch.setattr(comments, "service_request", lambda *args, **kwargs: calls.append(kwargs) or [])
    assert client.get("/comment-api/jobs", headers={"X-Client-ID": "forged"}).status_code == 200
    assert calls[0]["params"]["owner"].startswith("account:")


def test_user_session_tampering_and_password_changes_invalidate_login(setup):
    client, sent, config = setup
    signup(client, sent)
    value = client.cookies[accounts.SESSION_COOKIE_NAME]
    client.cookies.set(accounts.SESSION_COOKIE_NAME, value[:-1] + ("a" if value[-1] != "a" else "b"))
    assert client.get("/tasks").status_code == 401
    client.cookies.set(accounts.SESSION_COOKIE_NAME, value)
    assert client.get("/tasks").status_code == 200
    with sqlite3.connect(config.db_path) as connection:
        connection.execute("UPDATE auth_users SET password_hash=?", (accounts.hash_password("changed-password"),))
    assert client.get("/tasks").status_code == 401


def test_registration_is_atomic_under_concurrent_submissions(setup):
    client, sent, config = setup
    client.post("/auth/register/code", json={"email": "alice@example.com"})
    digest = registration.registration_digest(config, f"code:alice@example.com:{sent[0][1]}")
    store = SQLiteAccountStore(config.db_path)

    def attempt(_):
        return store.register("alice@example.com", digest, PASSWORD, lambda _: "dummy-hash", int(accounts.time.time()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1


def test_smtp_uses_verified_tls_and_no_secret_configuration_is_exposed(setup, monkeypatch):
    _, _, config = setup
    calls = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            calls.append((host, port, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, username, password):
            assert username == config.smtp_username and password == config.smtp_password

        def send_message(self, message):
            assert str(message["To"]) == "recipient@example.com"
            assert "123456" in message.get_content()

    monkeypatch.setattr(registration.smtplib, "SMTP_SSL", SMTP)
    registration.send_verification_email(config, "recipient@example.com", "123456")
    assert calls[0][:2] == ("smtp.qq.com", 465)
    assert calls[0][2]["context"].check_hostname
    assert calls[0][2]["context"].verify_mode == ssl.CERT_REQUIRED
    assert config.smtp_password not in repr(config)


def test_forwarded_peer_is_trusted_only_from_configured_proxy(setup, monkeypatch):
    client, _, config = setup
    observed = []
    original = accounts.peer_address

    def record(request):
        observed.append(original(request))
        return observed[-1]

    monkeypatch.setattr(accounts, "peer_address", record)
    client.post("/auth/login", json={"username": "bad", "password": "bad"}, headers={"X-Real-IP": "1.2.3.4"})
    assert observed[-1] == "testclient"
    monkeypatch.setattr(accounts, "settings", replace(config, auth_trusted_proxy_ips="testclient"))
    client.post("/auth/login", json={"username": "bad", "password": "bad"}, headers={"X-Real-IP": "5.6.7.8", "X-Forwarded-For": "spoofed"})
    assert observed[-1] == "5.6.7.8"
