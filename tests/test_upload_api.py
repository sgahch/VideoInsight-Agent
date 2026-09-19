from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
import app.workflow as workflow_module
from app.config import Settings
from app.main import app
from app.task_store import SQLiteTaskStore

from conftest import wait_for_task


def _isolated_settings(tmp_path: Path, **overrides) -> Settings:
    settings = Settings(
        mock_mode=True,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        db_path=tmp_path / "tasks.db",
        **overrides,
    )
    settings.ensure_dirs()
    return settings


def test_submit_file_analysis_with_mock() -> None:
    client = TestClient(app)
    response = client.post(
        "/analyze-file",
        data={"mode": "summary", "use_mock": "true"},
        files={"file": ("sample.txt", b"placeholder media", "text/plain")},
    )

    assert response.status_code == 202
    body = wait_for_task(client, response.json()["task_id"])
    assert body["status"] == "succeeded"
    assert body["result"]["diagnostics"]["source"] == "upload"
    assert "视频总结" in body["result"]["output"]


def test_upload_over_limit_is_rejected_and_leaves_no_trace(monkeypatch, tmp_path: Path) -> None:
    """回归：上传必须边写边限流，超限时既不留半成品也不留僵尸任务记录。

    上传端点没有鉴权，没有大小上限时一个请求就能写满磁盘；而先落盘再检查
    大小等于把磁盘交给客户端。这里顺带确认失败后不会留下永远 queued 的任务。
    """
    settings = _isolated_settings(tmp_path, max_upload_mb=1)
    store = SQLiteTaskStore(settings.db_path)
    # workflow 持有自己的一份 settings 引用（后台任务用它决定落盘位置），
    # 不一起 patch 的话任务产物会写进真实的 data/ 与 media/。
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(workflow_module, "settings", settings)
    monkeypatch.setattr(main_module, "task_store", store)

    client = TestClient(app)
    oversized = b"z" * (2 * 1024 * 1024)  # 2 MB > 1 MB 上限
    response = client.post(
        "/analyze-file",
        data={"mode": "summary", "use_mock": "true"},
        files={"file": ("big.mp4", oversized, "video/mp4")},
    )

    assert response.status_code == 413
    assert "上限" in response.json()["detail"]

    # 半成品与任务记录都不该留下
    assert list(settings.media_dir.iterdir()) == []
    assert store.all_task_ids() == set()


def test_upload_within_limit_succeeds(monkeypatch, tmp_path: Path) -> None:
    settings = _isolated_settings(tmp_path, max_upload_mb=1)
    store = SQLiteTaskStore(settings.db_path)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(workflow_module, "settings", settings)
    monkeypatch.setattr(main_module, "task_store", store)

    client = TestClient(app)
    response = client.post(
        "/analyze-file",
        data={"mode": "summary", "use_mock": "true"},
        files={"file": ("small.mp4", b"q" * 1024, "video/mp4")},
    )

    assert response.status_code == 202
    body = wait_for_task(client, response.json()["task_id"])
    assert body["result"] is not None  # 跑完并被标记为成功
