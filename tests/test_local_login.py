from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
import httpx
from fastapi.testclient import TestClient

import app.account_auth as accounts
import app.local_login as helper
import app.main as main
import app.security as security
from app.adapters.downloader import cookie_storage_path
from app.config import Settings


A, B = "a" * 32, "b" * 32
LOGIN = {"name": "sessionid", "value": "synthetic-only", "domain": ".douyin.com", "path": "/"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = Settings(
        db_path=tmp_path / "data.db", media_dir=tmp_path / "media",
        tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results",
        auth_token=None, auth_username="helper-admin",
        auth_password_hash=accounts.hash_password("helper-admin-password"),
        auth_session_secret="helper-test-secret",
        ytdlp_cookies_file=tmp_path / "admin.txt",
    )
    for module in (accounts, helper, main, security):
        monkeypatch.setattr(module, "settings", config)
    store = accounts.account_store()
    users = []
    with store._connection() as db:
        for user_id in (A, B):
            user = {"user_id": user_id, "email": user_id[0] + "@example.invalid",
                    "password_hash": accounts.hash_password("helper-user-password")}
            db.execute("INSERT INTO auth_users VALUES (?,?,?,?)",
                       (user_id, user["email"], user["password_hash"], int(time.time())))
            users.append(user)
    clients = []
    for user in users:
        client = TestClient(main.app, base_url="http://127.0.0.1:8003")
        client.cookies.set(accounts.SESSION_COOKIE_NAME, accounts.create_user_session(user))
        clients.append(client)
    admin = TestClient(main.app, base_url="http://127.0.0.1:8003")
    admin.cookies.set(accounts.SESSION_COOKIE_NAME, accounts.create_session())
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8003")
    config.ytdlp_cookies_file.write_text("admin-cookie-preserved", encoding="utf-8")
    return config, clients[0], clients[1], admin, anonymous


def start(client):
    response = client.post("/cookies/helper/start")
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    value = response.json()
    token = parse_qs(urlsplit(value["launch_url"]).query)["token"][0]
    return value["session_id"], token


def claim(client, token):
    response = client.post("/cookies/helper/claim", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.text
    return response.json()["upload_token"]


def complete(client, token, value="synthetic-only", **extra):
    return client.post("/cookies/helper/complete", headers={"Authorization": "Bearer " + token},
                       json={"cookies": [{**LOGIN, "value": value}], **extra})


def private_path(config, user_id):
    with accounts.task_account_scope("account:" + user_id):
        return cookie_storage_path(config)


def test_ordinary_account_automatic_save_is_private_and_preserves_other_platforms(setup):
    config, alice, bob, admin, anonymous = setup
    path = private_path(config, A)
    path.parent.mkdir(parents=True)
    path.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\town-bilibili\n",
                    encoding="utf-8")
    session, token = start(alice)
    upload = claim(anonymous, token)
    saved = complete(anonymous, upload)
    assert saved.status_code == 200 and saved.json()["saved"]
    assert "sessionid" not in saved.text
    assert "synthetic-only" not in saved.text
    assert "own-bilibili" in path.read_text(encoding="utf-8")
    assert "synthetic-only" in path.read_text(encoding="utf-8")
    assert not private_path(config, B).exists()
    assert config.ytdlp_cookies_file.read_text() == "admin-cookie-preserved"
    assert bob.get("/cookies/helper/" + session + "/status").status_code == 404
    assert admin.get("/cookies/helper/" + session + "/status").status_code == 404
    assert bob.delete("/cookies/helper/" + session).status_code == 404
    assert alice.get("/cookies/helper/" + session + "/status").json()["completed"]


def test_ticket_cannot_authenticate_other_apis_or_be_reclaimed(setup):
    _, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    for credential in (token, upload):
        headers = {"Authorization": "Bearer " + credential}
        assert anonymous.get("/tasks", headers=headers).status_code == 401
        assert anonymous.post("/cookies/browser/start", headers=headers).status_code == 401
        assert anonymous.post("/cookies/helper/start", headers=headers).status_code == 401
    assert anonymous.post("/cookies/helper/claim", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_administrator_also_uses_helper_without_copying_to_normal_users(setup):
    config, alice, bob, admin, anonymous = setup
    config.ytdlp_cookies_file.write_text(
        "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tadmin-bilibili\n",
        encoding="utf-8",
    )
    _, token = start(admin)
    upload = claim(anonymous, token)
    assert complete(anonymous, upload, "admin-new-private").status_code == 200
    text = config.ytdlp_cookies_file.read_text()
    assert "admin-bilibili" in text and "admin-new-private" in text
    assert not alice.get("/cookies/status").json()["exists"]
    assert not bob.get("/cookies/status").json()["exists"]


def test_completion_is_idempotent_but_replay_cannot_overwrite(setup):
    config, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    assert complete(anonymous, upload, "original-private").status_code == 200
    assert complete(anonymous, upload, "replay-replacement").json()["already_saved"]
    text = private_path(config, A).read_text()
    assert "original-private" in text and "replay-replacement" not in text
    assert complete(anonymous, token).status_code == 401


@pytest.mark.parametrize("when", ["pending", "claimed"])
def test_cancelled_or_replaced_session_cannot_restore_cookies(setup, when):
    config, alice, _, _, anonymous = setup
    session, token = start(alice)
    upload = claim(anonymous, token) if when == "claimed" else token
    assert alice.delete("/cookies/helper/" + session).status_code == 200
    assert complete(anonymous, upload).status_code == 401
    assert not private_path(config, A).exists()
    _, second = start(alice)
    upload = claim(anonymous, second)
    start(alice)
    assert complete(anonymous, upload).status_code == 401


def test_clear_and_logout_revoke_pending_helpers(setup):
    config, alice, _, _, anonymous = setup
    session, token = start(alice)
    upload = claim(anonymous, token)
    assert alice.delete("/cookies").status_code == 200
    assert complete(anonymous, upload).status_code == 401
    assert not private_path(config, A).exists()
    assert alice.get("/cookies/helper/" + session + "/status").json()["status"] == "cancelled"
    _, token = start(alice)
    upload = claim(anonymous, token)
    assert alice.post("/auth/logout").status_code == 200
    assert complete(anonymous, upload).status_code == 401


def test_password_change_revokes_handoff_before_claim_and_upload(setup):
    config, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    with sqlite3.connect(config.db_path) as db:
        db.execute("UPDATE auth_users SET password_hash=? WHERE user_id=?", (accounts.hash_password("changed-password"), A))
    assert complete(anonymous, upload).status_code == 410
    assert not private_path(config, A).exists()


def test_expired_session_fails_closed(setup):
    config, alice, _, _, anonymous = setup
    session, token = start(alice)
    upload = claim(anonymous, token)
    with sqlite3.connect(config.db_path) as db:
        db.execute("UPDATE cookie_helper_sessions SET expires_at=0 WHERE session_id=?", (session,))
    assert complete(anonymous, upload).status_code == 410
    assert alice.get("/cookies/helper/" + session + "/status").json()["status"] == "expired"


def test_client_cannot_choose_owner_and_foreign_cookies_are_not_saved(setup):
    config, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    assert complete(anonymous, upload, owner="account:" + B).status_code == 400
    headers = {"Authorization": "Bearer " + upload, "X-Client-Id": "account:" + B}
    assert anonymous.post("/cookies/helper/complete", headers=headers, json={"cookies": [
        {**LOGIN, "domain": ".notdouyin.com"}, {**LOGIN, "domain": ".douyin.com.evil.example"},
    ]}).status_code == 400
    assert not private_path(config, A).exists()
    assert complete(anonymous, upload).status_code == 200
    assert not private_path(config, B).exists()


@pytest.mark.parametrize("item", [
    {**LOGIN, "value": "injected\ncookie"},
    {**LOGIN, "domain": ".douyin.com\tinjected"},
    {**LOGIN, "name": "sessionid\tinjected"},
    {**LOGIN, "expires": 123},
    {**LOGIN, "name": "ttwid"},
    {**LOGIN, "value": ""},
])
def test_invalid_or_nonlogin_cookies_do_not_overwrite(setup, item):
    config, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    response = anonymous.post("/cookies/helper/complete", headers={"Authorization": "Bearer " + upload},
                              json={"cookies": [item]})
    assert response.status_code == 400
    assert "injected" not in response.text
    assert not private_path(config, A).exists()


def test_oversized_body_rejected_and_secrets_only_stored_as_hashes(setup):
    config, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    assert anonymous.post("/cookies/helper/complete", headers={"Authorization": "Bearer " + upload},
                          content=b" " * (helper.MAX_BODY + 1)).status_code == 413
    with sqlite3.connect(config.db_path) as db:
        stored = str(db.execute("SELECT * FROM cookie_helper_sessions").fetchall())
    assert token not in stored and upload not in stored and "synthetic-only" not in stored


def test_authentication_csrf_and_plain_http_blocked(setup):
    _, alice, _, _, anonymous = setup
    assert anonymous.post("/cookies/helper/start").status_code == 401
    assert alice.post("/cookies/helper/start", headers={"Origin": "https://evil.invalid"}).status_code == 403
    alice.base_url = "http://public.example"
    assert alice.post("/cookies/helper/start").status_code == 409
    anonymous.base_url = "http://public.example"
    assert anonymous.post("/cookies/helper/claim", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 400


def test_forwarded_https_is_only_trusted_from_configured_proxy(setup):
    _, alice, _, _, _ = setup
    session = alice.cookies.get(accounts.SESSION_COOKIE_NAME)
    for peer, expected in [("127.0.0.1", 200), ("203.0.113.10", 409)]:
        client = TestClient(main.app, base_url="http://public.example", client=(peer, 12345))
        client.cookies.set(accounts.SESSION_COOKIE_NAME, session)
        response = client.post("/cookies/helper/start", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == expected
        if expected == 200:
            assert parse_qs(urlsplit(response.json()["launch_url"]).query)["server"] == ["https://public.example"]


def test_rate_limit_and_restart_survival(setup):
    config, alice, _, _, anonymous = setup
    session, token = start(alice)
    helper._store.cache_clear()
    upload = claim(anonymous, token)
    assert complete(anonymous, upload).status_code == 200
    for _ in range(7):
        start(alice)
    assert alice.post("/cookies/helper/start").status_code == 429


def test_concurrent_accounts_keep_separate_files(setup):
    config, alice, bob, _, anonymous = setup
    _, token_a = start(alice)
    _, token_b = start(bob)
    upload_a, upload_b = claim(anonymous, token_a), claim(anonymous, token_b)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(complete, anonymous, upload_a, "alice-only")
        b = pool.submit(complete, anonymous, upload_b, "bob-only")
        assert a.result().status_code == b.result().status_code == 200
    text_a, text_b = private_path(config, A).read_text(), private_path(config, B).read_text()
    assert "alice-only" in text_a and "bob-only" not in text_a
    assert "bob-only" in text_b and "alice-only" not in text_b


def test_clear_racing_with_upload_cannot_resurrect_authorization(setup, monkeypatch):
    config, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    entered, release = threading.Event(), threading.Event()
    real_save = helper.save_douyin_login_cookies

    def save(*args):
        entered.set()
        assert release.wait(5)
        return real_save(*args)

    monkeypatch.setattr(helper, "save_douyin_login_cookies", save)
    with ThreadPoolExecutor(2) as pool:
        saving = pool.submit(complete, anonymous, upload)
        assert entered.wait(5)
        clearing = pool.submit(alice.delete, "/cookies")
        release.set()
        assert saving.result().status_code == 200
        assert clearing.result().status_code == 200
    assert not private_path(config, A).exists()
    assert complete(anonymous, upload).status_code == 401


def test_logout_racing_with_helper_start_cannot_create_a_new_handoff(setup, monkeypatch):
    config, alice, _, _, _ = setup
    checked, resume = threading.Event(), threading.Event()
    real_account = helper._account

    def account(request):
        result = real_account(request)
        if not checked.is_set():
            checked.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(helper, "_account", account)
    with ThreadPoolExecutor(1) as pool:
        starting = pool.submit(alice.post, "/cookies/helper/start")
        assert checked.wait(5)
        try:
            assert alice.post("/auth/logout").status_code == 200
        finally:
            resume.set()
        assert starting.result().status_code == 401
    with sqlite3.connect(config.db_path) as db:
        assert db.execute("SELECT count(*) FROM cookie_helper_sessions").fetchone()[0] == 0


def test_completion_database_and_cookie_io_run_outside_async_event_loop(setup, monkeypatch):
    _, alice, _, _, anonymous = setup
    _, token = start(alice)
    upload = claim(anonymous, token)
    real_save = helper.save_douyin_login_cookies

    async def check():
        loop_thread = threading.get_ident()

        def save(*args):
            assert threading.get_ident() != loop_thread, "Synchronous persistence blocks the ASGI event loop"
            return real_save(*args)

        monkeypatch.setattr(helper, "save_douyin_login_cookies", save)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1:8003",
        ) as client:
            response = await client.post(
                "/cookies/helper/complete", headers={"Authorization": "Bearer " + upload},
                json={"cookies": [LOGIN]},
            )
            assert response.status_code == 200 and response.json()["saved"]

    asyncio.run(check())
