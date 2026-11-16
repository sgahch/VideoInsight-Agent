from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.account_auth as accounts
import app.main as main
import app.media_urls as media_urls
import app.security as security
from app.account_store import MAX_CODE_ATTEMPTS, SQLiteAccountStore
from app.config import Settings
from app.registration import send_password_email
from app.task_store import SQLiteTaskStore


OLD = "original-test-password"
NEW = "updated-test-password"
ADMIN = "admin-test-password"
EMAIL = "alice@example.com"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(media_urls, "validate_public_url", lambda url, **kwargs: url)
    config = Settings(
        auth_token=None, auth_username="owner", auth_password_hash=accounts.hash_password(ADMIN),
        auth_session_secret="password-recovery-test-secret", registration_enabled=True,
        smtp_username="sender@example.com", smtp_password="test-smtp-only",
        db_path=tmp_path / "tasks.db", tmp_dir=tmp_path / "tmp",
        media_dir=tmp_path / "media", results_dir=tmp_path / "results",
    )
    config.ensure_dirs()
    for module in (accounts, main, security):
        monkeypatch.setattr(module, "settings", config)
    monkeypatch.setattr(main, "task_store", SQLiteTaskStore(config.db_path))
    monkeypatch.setattr(main, "task_queue", SimpleNamespace(submit=lambda *args: None))
    sent = []
    monkeypatch.setattr(accounts, "send_verification_email", lambda _, email, code: sent.append(("register", email, code)))
    monkeypatch.setattr(accounts, "send_password_email", lambda _, email, code, purpose: sent.append((purpose, email, code)))
    accounts._attempts.clear()
    return TestClient(main.app), sent, config


def signup(client, sent, email=EMAIL):
    client.post("/auth/register/code", json={"email": email}).raise_for_status()
    response = client.post("/auth/register", json={"email": email, "code": sent[-1][2], "password": OLD})
    assert response.status_code == 201, response.text
    return response


def reset(client, code, email=EMAIL, password=NEW):
    return client.post("/auth/password/reset", json={"email": email, "code": code, "password": password})


def admin_login(client, password=ADMIN):
    response = client.post("/auth/login", json={"username": "owner", "password": password})
    assert response.status_code == 200, response.text


def bind_admin(client, sent, email="owner@example.com", password=ADMIN):
    response = client.post("/auth/email/bind/code", json={"email": email, "current_password": password})
    assert response.status_code == 200, response.text
    response = client.post("/auth/email/bind", json={
        "email": email, "current_password": password, "code": sent[-1][2],
    })
    assert response.status_code == 200, response.text


def test_forgot_password_and_security_pages_are_reachable_and_guarded(setup):
    client, _, _ = setup
    page = client.get("/forgot-password")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert 'id="forgotPassword"' in page.text and 'id="resetForm"' in page.text
    assert client.get("/account", follow_redirects=False).headers["location"] == "/login"
    admin_login(client)
    assert 'id="bindingForm"' in client.get("/account").text
    assert 'href="/account"' in client.get("/").text


def test_reset_uses_generic_response_and_never_exposes_code_or_credentials(setup):
    client, sent, config = setup
    signup(client, sent)
    client.post("/auth/logout")
    known = client.post("/auth/password/reset/code", json={"email": EMAIL})
    unknown = client.post("/auth/password/reset/code", json={"email": "unknown@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert known.headers["cache-control"] == "no-store"
    assert len([item for item in sent if item[0] == "reset"]) == 1
    assert sent[-1][2] not in known.text
    with sqlite3.connect(config.db_path) as connection:
        digest = connection.execute("SELECT digest FROM auth_action_codes WHERE email=?", (EMAIL,)).fetchone()[0]
    assert len(digest) == 64 and digest != sent[-1][2]
    public = client.get("/auth/me").json()
    assert public["email"] is None and not public["email_binding_required"]
    assert config.smtp_password not in str(public) and "password_hash" not in str(public)
    for email in (EMAIL, "unknown@example.com"):
        assert client.post("/auth/password/reset/code", json={"email": email}).status_code == 429


def test_password_reset_preserves_account_tasks_and_invalidates_every_old_session(setup):
    alice, sent, config = setup
    signup(alice, sent)
    before = accounts.account_store().by_email(EMAIL)
    task_id = alice.post("/analyze", json={"url": "https://www.douyin.com/video/123"}).json()["task_id"]
    second = TestClient(main.app)
    second.post("/auth/login", json={"username": EMAIL, "password": OLD}).raise_for_status()
    resetting = TestClient(main.app)
    resetting.post("/auth/password/reset/code", json={"email": "ALICE@example.com"}).raise_for_status()
    response = reset(resetting, sent[-1][2])
    assert response.status_code == 200 and not response.json()["authenticated"]
    assert response.headers["cache-control"] == "no-store"
    assert accounts.SESSION_COOKIE_NAME not in resetting.cookies
    assert alice.get("/tasks").status_code == second.get("/tasks").status_code == 401
    assert resetting.post("/auth/login", json={"username": EMAIL, "password": OLD}).status_code == 401
    assert resetting.post("/auth/login", json={"username": EMAIL, "password": NEW}).status_code == 200
    assert resetting.get(f"/tasks/{task_id}").status_code == 200
    after = SQLiteAccountStore(config.db_path).by_email(EMAIL)
    assert after["user_id"] == before["user_id"]
    assert accounts.verify_password(NEW, after["password_hash"])
    assert after["password_hash"] not in str(response.json()) and NEW not in response.text


def test_change_requires_login_current_password_and_email_code(setup):
    client, sent, _ = setup
    assert client.post("/auth/password/change/code", json={"current_password": OLD}).status_code == 401
    signup(client, sent)
    assert client.post("/auth/password/change/code", json={"current_password": "wrong"}).status_code == 400
    assert client.post("/auth/password/change", json={
        "current_password": OLD, "code": "000000", "password": NEW,
    }).status_code == 400
    response = client.post("/auth/password/change/code", json={"current_password": OLD, "email": "other@example.com"})
    assert response.status_code == 200 and sent[-1][:2] == ("change", EMAIL)
    response = client.post("/auth/password/change", json={
        "current_password": OLD, "code": sent[-1][2], "password": NEW, "email": "other@example.com",
    })
    assert response.status_code == 200
    assert client.get("/tasks").status_code == 401
    assert client.post("/auth/login", json={"username": EMAIL, "password": NEW}).status_code == 200


def test_wrong_codes_lock_out_and_do_not_change_password(setup):
    client, sent, _ = setup
    signup(client, sent)
    client.post("/auth/password/reset/code", json={"email": EMAIL})
    code = sent[-1][2]
    wrong = "000000" if code != "000000" else "000001"
    for _ in range(MAX_CODE_ATTEMPTS):
        assert reset(client, wrong).status_code == 400
    assert reset(client, code).status_code == 400
    assert client.get("/tasks").status_code == 200
    assert accounts.verify_password(OLD, accounts.account_store().by_email(EMAIL)["password_hash"])


def test_registration_reset_and_change_codes_are_not_interchangeable(setup, monkeypatch):
    client, sent, _ = setup
    codes = iter([100001, 200002, 300003])
    monkeypatch.setattr(accounts.secrets, "randbelow", lambda _: next(codes))
    signup(client, sent)
    register_code = sent[-1][2]
    client.post("/auth/password/reset/code", json={"email": EMAIL})
    reset_code = sent[-1][2]
    client.post("/auth/password/change/code", json={"current_password": OLD})
    change_code = sent[-1][2]
    assert reset(client, register_code).status_code == 400
    assert reset(client, change_code).status_code == 400
    assert client.post("/auth/password/change", json={
        "current_password": OLD, "code": reset_code, "password": NEW,
    }).status_code == 400
    assert reset(client, reset_code).status_code == 200
    assert reset(client, reset_code, password="another-password").status_code == 400
    client.post("/auth/login", json={"username": EMAIL, "password": NEW})
    assert client.post("/auth/password/change", json={
        "current_password": NEW, "code": change_code, "password": "another-password",
    }).status_code == 400


def test_codes_cannot_reset_another_account(setup):
    alice, sent, _ = setup
    signup(alice, sent)
    bob = TestClient(main.app)
    signup(bob, sent, "bob@example.com")
    alice.post("/auth/password/reset/code", json={"email": EMAIL})
    alice_code = sent[-1][2]
    assert reset(bob, alice_code, email="bob@example.com").status_code == 400
    assert accounts.verify_password(OLD, accounts.account_store().by_email("bob@example.com")["password_hash"])


def test_expiry_and_resend_replace_old_code(setup, monkeypatch):
    client, sent, _ = setup
    signup(client, sent)
    now = accounts.time.time()
    monkeypatch.setattr(accounts.time, "time", lambda: now)
    monkeypatch.setattr(accounts.secrets, "randbelow", lambda _: 123456)
    client.post("/auth/password/reset/code", json={"email": EMAIL})
    monkeypatch.setattr(accounts.time, "time", lambda: now + 601)
    assert reset(client, "123456").status_code == 400
    monkeypatch.setattr(accounts.secrets, "randbelow", lambda _: 654321)
    client.post("/auth/password/reset/code", json={"email": EMAIL})
    assert reset(client, "123456").status_code == 400
    assert reset(client, "654321").status_code == 200


def test_smtp_failure_for_reset_is_generic_and_invalidates_code(setup, monkeypatch):
    client, sent, _ = setup
    signup(client, sent)
    attempted = []
    def fail(_, email, code, purpose):
        attempted.append(code)
        raise RuntimeError("secret-smtp-details")
    monkeypatch.setattr(accounts, "send_password_email", fail)
    response = client.post("/auth/password/reset/code", json={"email": EMAIL})
    assert response.status_code == 200 and "secret-smtp" not in response.text
    assert reset(client, attempted[0]).status_code == 400
    assert client.post("/auth/password/change/code", json={"current_password": OLD}).status_code == 503
    assert client.post("/auth/password/change", json={
        "current_password": OLD, "code": attempted[-1], "password": NEW,
    }).status_code == 400


def test_admin_binding_and_password_recovery_persist_without_rewriting_settings(setup):
    client, sent, config = setup
    admin_login(client)
    old_cookie = client.cookies[accounts.SESSION_COOKIE_NAME]
    assert client.get("/auth/me").json()["email_binding_required"]
    assert client.post("/auth/password/change/code", json={"current_password": ADMIN}).status_code == 409
    bind_admin(client, sent)
    assert client.get("/auth/me").json()["email"] == "owner@example.com"
    client.post("/auth/password/reset/code", json={"email": "owner@example.com"})
    assert reset(client, sent[-1][2], email="owner@example.com").status_code == 200
    accounts._store.cache_clear()
    stale = TestClient(main.app)
    stale.cookies.set(accounts.SESSION_COOKIE_NAME, old_cookie)
    assert stale.get("/tasks").status_code == 401
    assert client.post("/auth/login", json={"username": "owner", "password": ADMIN}).status_code == 401
    admin_login(client, NEW)
    assert client.get("/auth/me").json()["is_admin"]
    assert accounts.account_owner() is None
    client.post("/auth/logout")
    assert client.post("/auth/login", json={"username": "OWNER@example.com", "password": NEW}).status_code == 200
    assert client.get("/auth/me").json()["username"] == "owner"
    assert config.auth_password_hash == accounts.settings.auth_password_hash


def test_admin_binding_rejects_wrong_password_replay_and_registered_email(setup):
    client, sent, _ = setup
    signup(client, sent)
    assert client.post("/auth/email/bind/code", json={"email": "owner@example.com", "current_password": OLD}).status_code == 403
    client.post("/auth/logout")
    admin_login(client)
    assert client.post("/auth/email/bind/code", json={"email": "owner@example.com", "current_password": "wrong"}).status_code == 400
    client.post("/auth/email/bind/code", json={"email": EMAIL, "current_password": ADMIN})
    assert client.post("/auth/email/bind", json={
        "email": EMAIL, "current_password": ADMIN, "code": sent[-1][2],
    }).status_code == 400
    bind_admin(client, sent)
    assert client.post("/auth/email/bind/code", json={"email": "other@example.com", "current_password": ADMIN}).status_code == 409
    assert client.post("/auth/register/code", json={"email": "owner@example.com"}).status_code == 200
    assert sent[-1][0] == "bind"
    assert client.post("/auth/register", json={
        "email": "owner@example.com", "code": sent[-1][2], "password": OLD,
    }).status_code == 400


def test_bootstrap_admin_password_rotation_overrides_saved_password_and_recovery_email(setup, monkeypatch):
    client, sent, config = setup
    admin_login(client)
    bind_admin(client, sent)
    changed = replace(config, auth_password_hash=accounts.hash_password("server-rotated-password"))
    monkeypatch.setattr(accounts, "settings", changed)
    assert client.get("/tasks").status_code == 401
    assert client.post("/auth/login", json={"username": "owner", "password": ADMIN}).status_code == 401
    admin_login(client, "server-rotated-password")
    assert client.get("/auth/me").json()["email_binding_required"]
    bind_admin(client, sent, password="server-rotated-password")
    assert not client.get("/auth/me").json()["email_binding_required"]


@pytest.mark.parametrize("path", [
    "/auth/password/reset/code", "/auth/password/reset", "/auth/password/change/code",
    "/auth/password/change", "/auth/email/bind/code", "/auth/email/bind",
])
def test_all_password_operations_reject_cross_origin_requests(setup, path):
    client, sent, _ = setup
    admin_login(client)
    payload = {"email": EMAIL, "current_password": ADMIN, "password": NEW, "code": "123456"}
    response = client.post(path, json=payload, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and not sent


@pytest.mark.parametrize("password", ["short", "x" * 129])
def test_reset_validates_new_password_before_consuming_code(setup, password):
    client, sent, _ = setup
    signup(client, sent)
    client.post("/auth/password/reset/code", json={"email": EMAIL})
    code = sent[-1][2]
    assert reset(client, code, password=password).status_code == 422
    assert reset(client, code).status_code == 200


def test_email_recovery_does_not_depend_on_registration_being_open(setup, monkeypatch):
    client, sent, config = setup
    signup(client, sent)
    monkeypatch.setattr(accounts, "settings", replace(config, registration_enabled=False))
    assert not client.get("/auth/me").json()["registration_available"]
    assert client.get("/auth/me").json()["password_reset_available"]
    assert client.post("/auth/password/reset/code", json={"email": EMAIL}).status_code == 200
    monkeypatch.setattr(accounts, "settings", replace(config, smtp_password=None))
    assert not client.get("/auth/me").json()["password_reset_available"]
    assert client.post("/auth/password/reset/code", json={"email": EMAIL}).status_code == 503
    assert reset(client, sent[-1][2]).status_code == 503


def test_rate_limits_persist_and_are_shared_with_registration(setup):
    client, sent, _ = setup
    for index in range(19):
        assert client.post("/auth/password/reset/code", json={"email": f"unknown{index}@example.com"}).status_code == 200
    accounts._store.cache_clear()
    assert client.post("/auth/register/code", json={"email": EMAIL}).status_code == 200
    assert client.post("/auth/password/reset/code", json={"email": "next@example.com"}).status_code == 429
    assert len(sent) == 1


def test_password_code_consumption_is_atomic_under_concurrency(setup):
    client, sent, config = setup
    signup(client, sent)
    client.post("/auth/password/reset/code", json={"email": EMAIL})
    user = accounts.account_store().by_email(EMAIL)
    digest = accounts._action_digest("reset", EMAIL, user["password_hash"], sent[-1][2])
    store = SQLiteAccountStore(config.db_path)
    def update(_):
        return store.update_password("reset", EMAIL, digest, "new-hash", int(accounts.time.time()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(update, range(2))) == 1


def test_stale_smtp_failure_cannot_invalidate_a_newer_generation(setup):
    _, _, config = setup
    store = SQLiteAccountStore(config.db_path)
    store.reserve_action_code("reset", EMAIL, "first", "old-generation", "version", [], 1000)
    store.reserve_action_code("reset", EMAIL, "second", "new-generation", "version", [], 1061)
    store.invalidate_action_code("reset", EMAIL, "old-generation")
    with store._connection() as connection:
        assert store._valid_action_code(connection, "reset", EMAIL, "second", "version", 1062) is True


def test_email_content_identifies_its_action_and_does_not_include_passwords(setup, monkeypatch):
    _, _, config = setup
    import app.registration as mail
    messages = []
    monkeypatch.setattr(mail, "_send_email", lambda *args: messages.append(args))
    for purpose in ("reset", "change", "bind"):
        send_password_email(config, EMAIL, "123456", purpose)
    assert len({item[2] for item in messages}) == 3
    assert all("123456" in item[3] and "10 分钟" in item[3] for item in messages)
    assert all(ADMIN not in item[3] and NEW not in item[3] for item in messages)


def test_registration_and_admin_binding_cannot_claim_the_same_email_concurrently(setup):
    _, _, config = setup
    store = SQLiteAccountStore(config.db_path)
    now = int(accounts.time.time())
    store.reserve_code(EMAIL, "register-digest", "registration", [], now)
    store.reserve_action_code("bind", EMAIL, "binding-digest", "binding", config.auth_password_hash, [], now)
    def claim(kind):
        if kind == "register":
            return bool(store.register(EMAIL, "register-digest", OLD, lambda _: "user-hash", now))
        return store.bind_admin_email("owner", config.auth_password_hash, EMAIL, "binding-digest", now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, ("register", "bind"))) == 1
    with sqlite3.connect(config.db_path) as connection:
        count = connection.execute("SELECT count(*) FROM auth_users").fetchone()[0]
        count += connection.execute("SELECT count(*) FROM auth_admin_credentials").fetchone()[0]
    assert count == 1


def test_schema_upgrade_preserves_legacy_user_records(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL)")
        connection.execute("INSERT INTO auth_users VALUES (?, ?, ?, ?)", ("a" * 32, EMAIL, "legacy-password-hash", 1000))
    store = SQLiteAccountStore(path)
    assert store.by_email(EMAIL)["password_hash"] == "legacy-password-hash"
    assert store.by_email(EMAIL)["user_id"] == "a" * 32
    with sqlite3.connect(path) as connection:
        tables = {item[0] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"auth_action_codes", "auth_admin_credentials", "auth_users"} <= tables


@pytest.mark.parametrize("administrator", [False, True])
def test_logout_revokes_copied_session_without_signing_out_other_devices(setup, administrator):
    client, sent, config = setup
    if administrator:
        admin_login(client)
        username, password = "owner", ADMIN
    else:
        signup(client, sent)
        username, password = EMAIL, OLD
    copied = client.cookies[accounts.SESSION_COOKIE_NAME]
    other_device = TestClient(main.app)
    other_device.post("/auth/login", json={"username": username, "password": password}).raise_for_status()
    replay = TestClient(main.app)
    replay.cookies.set(accounts.SESSION_COOKIE_NAME, copied)
    assert replay.get("/tasks").status_code == 200
    client.post("/auth/logout").raise_for_status()
    assert replay.get("/tasks").status_code == 401
    assert not replay.get("/auth/me").json()["authenticated"]
    assert other_device.get("/tasks").status_code == 200
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200
    assert client.get("/tasks").status_code == 200
    with sqlite3.connect(config.db_path) as connection:
        rows = list(connection.execute("SELECT session_hash,expires_at FROM auth_session_revocations"))
    assert len(rows) == 1 and len(rows[0][0]) == 64
    assert copied not in str(rows)


def test_session_revocations_expire_and_share_the_callers_transaction(tmp_path):
    store = SQLiteAccountStore(tmp_path / "sessions.db")
    with pytest.raises(RuntimeError):
        with store._connection() as connection:
            store.revoke_session("a" * 64, 1100, 1000, connection=connection)
            assert connection.execute("SELECT count(*) FROM auth_session_revocations").fetchone()[0] == 1
            raise RuntimeError("roll back the surrounding logout transaction")
    assert not store.session_revoked("a" * 64, 1000)
    store.revoke_session("a" * 64, 1100, 1000)
    assert store.session_revoked("a" * 64, 1099)
    assert not store.session_revoked("a" * 64, 1100)
    store.revoke_session("b" * 64, 1200, 1100)
    with store._connection() as connection:
        rows = list(connection.execute("SELECT session_hash FROM auth_session_revocations"))
    assert [row[0] for row in rows] == ["b" * 64]
