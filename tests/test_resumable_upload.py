from __future__ import annotations

import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.account_auth as account_auth
import app.main as main
import app.security as security
import app.workflow as workflow
from app.config import Settings
from app.task_queue import TaskQueue, TaskQueueFull
from app.task_store import SQLiteTaskStore
from conftest import wait_for_task


@pytest.fixture
def upload_env(monkeypatch, tmp_path):
    settings = Settings(
        mock_mode=True, llm_provider="mock", auth_token=None, auth_password_hash=None,
        registration_enabled=False, tmp_dir=tmp_path / "tmp", media_dir=tmp_path / "media",
        results_dir=tmp_path / "results", db_path=tmp_path / "tasks.db",
        max_upload_mb=1, upload_chunk_bytes=4,
    )
    settings.ensure_dirs()
    store = SQLiteTaskStore(settings.db_path)
    queue = TaskQueue(2)
    for module in (main, workflow, security, account_auth):
        monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(main, "task_store", store)
    monkeypatch.setattr(main, "task_queue", queue)
    client = TestClient(main.app, headers={"X-Client-Id": "alice"})
    yield client, settings, store, queue
    queue.shutdown(wait=True)
    client.close()


def new_upload(client, data=b"abcdefghij", **overrides):
    response = client.post("/uploads", json={
        "filename": "meeting.mp4", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), **overrides,
    })
    assert response.status_code == 201, response.text
    return response.json()


def put_chunk(client, upload_id, index, data, **headers):
    return client.put(f"/uploads/{upload_id}/chunks/{index}", content=data, headers={
        "Content-Type": "application/octet-stream",
        "X-Chunk-SHA256": hashlib.sha256(data).hexdigest(), **headers,
    })


def send_all(client, session, data=b"abcdefghij"):
    size = session["chunk_size"]
    for index, start in enumerate(range(0, len(data), size)):
        response = put_chunk(client, session["upload_id"], index, data[start:start + size])
        assert response.status_code == 200, response.text


def finish(client, session, **options):
    return client.post(f"/uploads/{session['upload_id']}/complete", json={"use_mock": True, **options})


def test_out_of_order_chunks_merge_to_original_and_start_analysis(upload_env):
    client, settings, store, _ = upload_env
    session = new_upload(client)
    for index, data in [(2, b"ij"), (0, b"abcd"), (1, b"efgh")]:
        assert put_chunk(client, session["upload_id"], index, data).status_code == 200
    response = finish(client, session, llm_api_key="must-not-be-persisted")
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    task = wait_for_task(client, task_id, headers={"X-Client-Id": "alice"})
    assert task["status"] == "succeeded"
    assert (settings.media_dir / task_id / "meeting.mp4").read_bytes() == b"abcdefghij"
    assert len(store.all_task_ids()) == 1
    for path in settings.tmp_dir.rglob("*.sqlite3"):
        assert b"must-not-be-persisted" not in path.read_bytes()
    assert "must-not-be-persisted" not in str(task)


def test_resume_survives_new_store_instance_and_skips_accepted_chunks(upload_env):
    client, settings, _, _ = upload_env
    session = new_upload(client)
    assert put_chunk(client, session["upload_id"], 0, b"abcd").status_code == 200
    from app.uploads import UploadStore
    restored = UploadStore(settings).status(session["upload_id"], "alice")
    assert restored["uploaded_chunks"] == [0]
    assert restored["uploaded_bytes"] == 4
    assert put_chunk(client, session["upload_id"], 0, b"abcd").status_code == 200
    assert put_chunk(client, session["upload_id"], 0, b"xxxx").status_code == 409
    status = client.get(f"/uploads/{session['upload_id']}").json()
    assert status["uploaded_bytes"] == 4


def test_duplicate_and_concurrent_completion_return_one_task(upload_env):
    client, _, store, queue = upload_env
    session = new_upload(client)
    send_all(client, session)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: finish(client, session), range(3)))
    assert [response.status_code for response in responses] == [202, 202, 202]
    assert len({response.json()["task_id"] for response in responses}) == 1
    assert len(store.all_task_ids()) == 1
    assert queue.snapshot()["accepted"] == 1


def test_missing_chunks_and_wrong_digests_cannot_create_task(upload_env):
    client, _, store, _ = upload_env
    session = new_upload(client)
    assert finish(client, session).status_code == 409
    assert put_chunk(client, session["upload_id"], 0, b"abcd", **{"X-Chunk-SHA256": "0" * 64}).status_code == 422
    assert client.get(f"/uploads/{session['upload_id']}").json()["uploaded_chunks"] == []
    wrong_file = new_upload(client, sha256="0" * 64)
    send_all(client, wrong_file)
    assert finish(client, wrong_file).status_code == 422
    assert store.all_task_ids() == set()


@pytest.mark.parametrize("index,data", [(-1, b"abcd"), (3, b"abcd"), (0, b"a"), (0, b"abcde"), (2, b"ijkl")])
def test_invalid_chunk_boundaries_do_not_change_progress(upload_env, index, data):
    client, _, _, _ = upload_env
    session = new_upload(client)
    assert put_chunk(client, session["upload_id"], index, data).status_code in {413, 422}
    assert client.get(f"/uploads/{session['upload_id']}").json()["uploaded_bytes"] == 0


def test_other_identity_cannot_inspect_write_complete_or_cancel(upload_env):
    client, _, _, _ = upload_env
    session = new_upload(client)
    path = f"/uploads/{session['upload_id']}"
    headers = {"X-Client-Id": "bob"}
    for method, suffix in [("GET", ""), ("DELETE", ""), ("POST", "/complete")]:
        assert client.request(method, path + suffix, headers=headers, json={} if method == "POST" else None).status_code == 404
    assert put_chunk(client, session["upload_id"], 0, b"abcd", **headers).status_code == 404
    assert client.get(path).status_code == 200


def test_size_limit_empty_file_and_pending_session_quota(upload_env):
    client, _, _, _ = upload_env
    body = {"filename": "large.mp4", "size": 1048577, "sha256": "a" * 64}
    assert client.post("/uploads", json=body).status_code == 413
    assert client.post("/uploads", json={**body, "size": 0}).status_code == 422
    for _ in range(4):
        new_upload(client)
    assert client.post("/uploads", json={**body, "size": 10}).status_code == 429


def test_queue_full_retains_chunks_and_allows_completion_retry(upload_env, monkeypatch):
    client, settings, store, queue = upload_env
    session = new_upload(client)
    send_all(client, session)
    original = queue.submit
    def reject(*args, **kwargs):
        raise TaskQueueFull("队列已满")
    monkeypatch.setattr(queue, "submit", reject)
    assert finish(client, session).status_code == 429
    assert store.all_task_ids() == set()
    status = client.get(f"/uploads/{session['upload_id']}").json()
    assert status["uploaded_chunks"] == [0, 1, 2]
    monkeypatch.setattr(queue, "submit", original)
    assert finish(client, session).status_code == 202


def test_cancel_releases_quota_and_removes_only_owned_upload(upload_env):
    client, settings, _, _ = upload_env
    session = new_upload(client)
    put_chunk(client, session["upload_id"], 0, b"abcd")
    other = new_upload(client)
    response = client.delete(f"/uploads/{session['upload_id']}")
    assert response.status_code == 204
    assert not (settings.tmp_dir / "_uploads" / session["upload_id"]).exists()
    assert client.get(f"/uploads/{other['upload_id']}").status_code == 200


def test_expired_uploads_are_cleaned_without_removing_active_upload(upload_env):
    client, settings, _, _ = upload_env
    expired = new_upload(client)
    active = new_upload(client)
    db = settings.tmp_dir / "_uploads" / expired["upload_id"] / "session.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE upload SET updated_at = ?", (time.time() - 25 * 3600,))
    connection.close()
    main._run_artifact_cleanup()
    assert not db.parent.exists()
    assert client.get(f"/uploads/{active['upload_id']}").status_code == 200


def test_deleted_task_is_not_recreated_by_retrying_completion(upload_env):
    client, _, store, queue = upload_env
    session = new_upload(client)
    send_all(client, session)
    task_id = finish(client, session).json()["task_id"]
    wait_for_task(client, task_id, headers={"X-Client-Id": "alice"})
    queue.shutdown(wait=True)
    assert client.delete(f"/tasks/{task_id}").status_code == 204
    assert finish(client, session).status_code == 410
    assert store.all_task_ids() == set()


def test_initialization_retry_reuses_session_and_checks_metadata(upload_env):
    client, _, _, _ = upload_env
    first = new_upload(client, idempotency_key="1" * 32)
    second = new_upload(client, idempotency_key="1" * 32)
    assert first["upload_id"] == second["upload_id"]
    conflict = client.post("/uploads", json={
        "filename": "different.mp4", "size": 10, "sha256": "a" * 64,
        "idempotency_key": "1" * 32,
    })
    assert conflict.status_code == 409


def test_completion_recovers_after_task_commit_but_session_commit_was_lost(upload_env):
    client, settings, store, queue = upload_env
    session = new_upload(client)
    send_all(client, session)
    original = finish(client, session).json()["task_id"]
    db = settings.tmp_dir / "_uploads" / session["upload_id"] / "session.sqlite3"
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("UPDATE upload SET state = 'uploading', task_id = NULL")
    connection.close()
    restored = finish(client, session)
    assert restored.status_code == 202
    assert restored.json()["task_id"] == original
    assert queue.snapshot()["accepted"] == 1
    assert store.all_task_ids() == {original}


def test_merge_works_when_tmp_and_media_are_on_different_mounts(upload_env, monkeypatch):
    import errno
    import app.uploads as uploads
    client, _, _, _ = upload_env
    session = new_upload(client)
    send_all(client, session)
    replace = uploads.os.replace
    def same_mount_replace(source, target):
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return replace(source, target)
    monkeypatch.setattr(uploads.os, "replace", same_mount_replace)
    assert finish(client, session).status_code == 202


@pytest.mark.parametrize("kind", ["missing", "empty", "corrupt"])
def test_interrupted_initialization_does_not_block_new_uploads_and_is_eventually_cleaned(upload_env, kind):
    import os
    client, settings, _, _ = upload_env
    leftover = settings.tmp_dir / "_uploads" / ("0" * 32)
    leftover.mkdir(parents=True)
    if kind != "missing":
        (leftover / "session.sqlite3").write_bytes(b"" if kind == "empty" else b"not sqlite")
    new_upload(client)
    assert leftover.is_dir(), "a fresh initialization directory must not be cleaned prematurely"
    os.utime(leftover, (time.time() - 25 * 3600,) * 2)
    main._run_artifact_cleanup()
    assert not leftover.exists()


def test_cleanup_does_not_treat_a_busy_session_as_corrupt(upload_env, monkeypatch):
    import os
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.uploads import UploadStore
    client, settings, _, _ = upload_env
    session = new_upload(client)
    put_chunk(client, session["upload_id"], 0, b"abcd")
    directory = settings.tmp_dir / "_uploads" / session["upload_id"]
    connection = sqlite3.connect(directory / "session.sqlite3")
    with connection:
        connection.execute("UPDATE upload SET updated_at = ?", (time.time() - 25 * 3600,))
    connection.close()
    os.utime(directory, (time.time() - 25 * 3600,) * 2)
    uploads = UploadStore(settings)
    original = uploads._connect
    @contextmanager
    def busy_connection(path):
        with original(path) as database:
            def execute(sql, *args):
                if sql == "BEGIN IMMEDIATE":
                    raise sqlite3.OperationalError("database is locked")
                return database.execute(sql, *args)
            yield SimpleNamespace(execute=execute)
    monkeypatch.setattr(uploads, "_connect", busy_connection)
    uploads.cleanup_expired()
    assert (directory / "0.part").read_bytes() == b"abcd"
