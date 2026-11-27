from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.errors import WorkflowError
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo
from app.task_store import SQLiteTaskStore


@pytest.fixture
def video_task(monkeypatch, tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main, "task_store", store)
    monkeypatch.setattr(main, "settings", replace(main.settings, tmp_dir=tmp_path))
    calls = []

    def create(url):
        task = store.create(AnalyzeRequest(url=url, client_id="owner", mode=AnalysisMode.transcript))
        directory = tmp_path / task.task_id
        directory.mkdir()
        stored = directory / "douyin-123.mp4"
        stored.write_bytes(b"480p transcription cache")
        store.set_succeeded(task.task_id, AnalysisResult(
            mode=AnalysisMode.transcript, transcript="text", output="text",
            media=MediaInfo(audio_path=str(stored), source_url=url),
        ))
        return task, stored

    def download(url, task_id):
        target = tmp_path / task_id / "123.video.mp4"
        if target.is_file():
            return target
        calls.append((url, task_id))
        target.write_bytes(b"1080p source video")
        return target

    monkeypatch.setattr(main, "select_downloader", lambda *args, **kwargs: SimpleNamespace(download_video=download))
    client = TestClient(main.app, headers={"X-Client-Id": "owner"})
    try:
        yield client, create, calls
    finally:
        client.close()


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/video/123",
    "https://v.douyin.com/test/",
    "https://www.iesdouyin.com/share/video/123/",
])
def test_douyin_source_route_downloads_high_quality_and_reuses_it(video_task, url):
    client, create, calls = video_task
    task, stored = create(url)
    path = f"/tasks/{task.task_id}/video?download=1"
    first = client.get(path)
    assert first.status_code == 200
    assert first.content == b"1080p source video"
    assert calls == [(url, task.task_id)]
    assert stored.read_bytes() == b"480p transcription cache"
    assert client.get(path).content == first.content
    assert len(calls) == 1


@pytest.mark.parametrize("url", ["upload://recording.mp4", "https://example.com/video"])
def test_other_stored_videos_are_returned_directly(video_task, url):
    client, create, calls = video_task
    task, stored = create(url)
    response = client.get(f"/tasks/{task.task_id}/video")
    assert response.status_code == 200
    assert response.content == stored.read_bytes()
    assert not calls


def test_douyin_source_download_failure_does_not_return_low_quality(video_task, monkeypatch):
    client, create, _ = video_task
    task, stored = create("https://www.douyin.com/video/123")

    def fail(*args):
        raise WorkflowError("synthetic download failure")

    monkeypatch.setattr(main, "select_downloader", lambda *args, **kwargs: SimpleNamespace(download_video=fail))
    response = client.get(f"/tasks/{task.task_id}/video")
    assert response.status_code == 422
    assert stored.read_bytes() == b"480p transcription cache"


def test_douyin_source_route_checks_owner_before_downloading(video_task):
    client, create, calls = video_task
    task, _ = create("https://www.douyin.com/video/123")
    assert client.get(f"/tasks/{task.task_id}/video", headers={"X-Client-Id": "other"}).status_code == 404
    assert not calls
