from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app

from conftest import wait_for_task


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_web_app() -> None:
    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "VideoInsight Agent" in response.text


def test_upload_cookies(monkeypatch, tmp_path) -> None:
    client = TestClient(app)
    cookie_file = tmp_path / "cookies.txt"
    monkeypatch.setattr(main_module, "default_cookies_file", lambda _: cookie_file)

    response = client.post(
        "/cookies",
        files={"file": ("cookies.txt", b"sessionid=abc; test=value", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert cookie_file.exists()
    assert ".douyin.com" in cookie_file.read_text(encoding="utf-8")


def test_submit_analysis_runs_background_task() -> None:
    client = TestClient(app)
    response = client.post(
        "/analyze",
        json={"url": "https://example.com/video", "mode": "summary", "use_mock": True},
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]

    body = wait_for_task(client, task_id)
    assert body["status"] == "succeeded"
    assert "视频总结" in body["result"]["output"]


def test_list_and_delete_tasks(monkeypatch, tmp_path) -> None:
    """任务按 client_id 隔离：只有带上同一身份标识才能看到/删除自己的任务。"""
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)
    headers = {"X-Client-Id": "client-A"}

    task_ids = []
    for url in ("https://example.com/a", "https://example.com/b"):
        response = client.post(
            "/analyze",
            json={"url": url, "mode": "summary", "use_mock": True},
            headers=headers,
        )
        assert response.status_code == 202
        task_ids.append(response.json()["task_id"])

    # 任务由专用线程池异步执行，等它们跑完再断言状态过滤
    for task_id in task_ids:
        wait_for_task(client, task_id, headers=headers)

    listing = client.get("/tasks", headers=headers).json()
    assert len(listing) == 2
    assert {task["task_id"] for task in listing} == {
        task.task_id for task in store.list_tasks(client_id="client-A")
    }

    succeeded = client.get("/tasks?status=succeeded", headers=headers).json()
    assert len(succeeded) == 2

    task_id = listing[0]["task_id"]
    delete_response = client.delete(f"/tasks/{task_id}", headers=headers)
    assert delete_response.status_code == 204
    assert client.get(f"/tasks/{task_id}", headers=headers).status_code == 404
    assert len(client.get("/tasks", headers=headers).json()) == 1


def test_list_tasks_isolates_clients(monkeypatch, tmp_path) -> None:
    """历史记录隔离：无身份或身份不同，都看不到他人任务。"""
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)

    client.post(
        "/analyze",
        json={"url": "https://example.com/a", "mode": "summary", "use_mock": True},
        headers={"X-Client-Id": "client-A"},
    )

    # 该库中没有无归属任务，所以无身份调用方看到的是空的无身份命名空间
    assert client.get("/tasks").json() == []
    assert client.get("/tasks", headers={"X-Client-Id": "client-B"}).json() == []  # 他人 → 空
    assert len(client.get("/tasks", headers={"X-Client-Id": "client-A"}).json()) == 1


def test_analyze_without_client_id_is_listable_anonymously(monkeypatch, tmp_path) -> None:
    """回归：不带身份提交的任务，必须能被不带身份的调用方在历史里看到。

    此前 create() 允许 client_id=None，而 list_tasks(client_id=None) 无条件
    返回 []，两者不对称：这类任务创建成功（202）、按 id 也能查到，却永远出现
    在历史列表里 —— 对 CLI / curl / 第三方集成来说就是个数据黑洞。
    """
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)

    response = client.post(
        "/analyze",
        json={"url": "https://example.com/a", "mode": "summary", "use_mock": True},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    assert [task["task_id"] for task in client.get("/tasks").json()] == [task_id]
    assert client.get(f"/tasks/{task_id}").status_code == 200


def test_unowned_task_is_not_reachable_by_identified_client(monkeypatch, tmp_path) -> None:
    """回归：无归属任务不得对「带身份」的调用方开放。

    此前 _owned_task 写作 `if task.request.client_id and task.request.client_id
    != client_id`，无归属任务会短路放行 —— 任何带 X-Client-Id 的调用方都能读取、
    删除它，还能对它发起 /chat 来消耗服务端 LLM 额度。
    """
    from app.schemas import AnalysisMode, AnalyzeRequest
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)

    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    attacker = {"X-Client-Id": "attacker"}

    assert client.get(f"/tasks/{task.task_id}", headers=attacker).status_code == 404
    assert client.delete(f"/tasks/{task.task_id}", headers=attacker).status_code == 404
    assert (
        client.post(
            f"/tasks/{task.task_id}/chat", json={"question": "讲了个啥"}, headers=attacker
        ).status_code
        == 404
    )

    # 无身份调用方仍可访问无归属任务：NULL 归属自成一个命名空间，而不是"公开"
    assert client.get(f"/tasks/{task.task_id}").status_code == 200


def test_delete_missing_task_returns_404(monkeypatch, tmp_path) -> None:
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)

    response = client.delete("/tasks/does-not-exist")
    assert response.status_code == 404


def test_task_media_endpoint(monkeypatch, tmp_path) -> None:
    from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)

    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    task = store.create(
        AnalyzeRequest(url="https://example.com/v", mode=AnalysisMode.summary, use_mock=True)
    )
    store.set_succeeded(
        task.task_id,
        AnalysisResult(
            mode=AnalysisMode.summary,
            transcript="t",
            output="o",
            media=MediaInfo(audio_path=str(media_file), source_url="https://example.com/v"),
        ),
    )

    inline = client.get(f"/tasks/{task.task_id}/media")
    assert inline.status_code == 200
    assert "inline" in inline.headers.get("content-disposition", "")
    assert inline.content == b"\x00\x00\x00\x18ftypmp42"

    download = client.get(f"/tasks/{task.task_id}/media?download=1")
    assert download.status_code == 200
    assert "attachment" in download.headers.get("content-disposition", "")
    assert "clip.mp4" in download.headers.get("content-disposition", "")

    assert client.get("/tasks/does-not-exist/media").status_code == 404


def test_task_video_endpoint_serves_stored_video(monkeypatch, tmp_path) -> None:
    from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)

    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    task = store.create(
        AnalyzeRequest(url="https://example.com/v", mode=AnalysisMode.summary, use_mock=True)
    )
    store.set_succeeded(
        task.task_id,
        AnalysisResult(
            mode=AnalysisMode.summary,
            transcript="t",
            output="o",
            media=MediaInfo(audio_path=str(media_file), source_url="https://example.com/v"),
        ),
    )

    # 已存文件是视频时，/video 直接返回该文件，不触发网络下载
    response = client.get(f"/tasks/{task.task_id}/video")
    assert response.status_code == 200
    assert response.content == b"\x00\x00\x00\x18ftypmp42"

    download = client.get(f"/tasks/{task.task_id}/video?download=1")
    assert download.status_code == 200
    assert "attachment" in download.headers.get("content-disposition", "")

    assert client.get("/tasks/does-not-exist/video").status_code == 404


def test_owned_media_uses_client_session_cookie(monkeypatch, tmp_path) -> None:
    from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)
    media_file = tmp_path / "clip.mp3"
    media_file.write_bytes(b"ID3" + b"audio" * 20)
    task = store.create(
        AnalyzeRequest(
            url="https://example.com/v",
            mode=AnalysisMode.transcript,
            use_mock=False,
            client_id="browser-owner",
        )
    )
    store.set_succeeded(
        task.task_id,
        AnalysisResult(
            mode=AnalysisMode.transcript,
            transcript="t",
            output="t",
            media=MediaInfo(audio_path=str(media_file), source_url="https://example.com/v"),
        ),
    )

    assert client.get(f"/tasks/{task.task_id}/media").status_code == 404
    health = client.get("/health", headers={"X-Client-ID": "browser-owner"})
    assert health.status_code == 200
    assert "videoinsight_client_id" in health.cookies

    inline = client.get(f"/tasks/{task.task_id}/media")
    download = client.get(f"/tasks/{task.task_id}/media?download=1")

    assert inline.status_code == 200
    assert inline.content == media_file.read_bytes()
    assert download.status_code == 200
    assert "attachment" in download.headers.get("content-disposition", "")
