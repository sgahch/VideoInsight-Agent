from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


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

    task_response = client.get(f"/tasks/{task_id}")
    assert task_response.status_code == 200
    body = task_response.json()
    assert body["status"] == "succeeded"
    assert "视频总结" in body["result"]["output"]


def test_list_and_delete_tasks(monkeypatch, tmp_path) -> None:
    from app.task_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main_module, "task_store", store)
    client = TestClient(app)

    for url in ("https://example.com/a", "https://example.com/b"):
        response = client.post(
            "/analyze",
            json={"url": url, "mode": "summary", "use_mock": True},
        )
        assert response.status_code == 202

    listing = client.get("/tasks").json()
    assert len(listing) == 2
    assert {task["task_id"] for task in listing} == {task.task_id for task in store.list_tasks()}

    succeeded = client.get("/tasks?status=succeeded").json()
    assert len(succeeded) == 2

    task_id = listing[0]["task_id"]
    delete_response = client.delete(f"/tasks/{task_id}")
    assert delete_response.status_code == 204
    assert client.get(f"/tasks/{task_id}").status_code == 404
    assert len(client.get("/tasks").json()) == 1


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
