from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_submit_file_analysis_with_mock() -> None:
    client = TestClient(app)
    response = client.post(
        "/analyze-file",
        data={"mode": "summary", "use_mock": "true"},
        files={"file": ("sample.txt", b"placeholder media", "text/plain")},
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]

    task_response = client.get(f"/tasks/{task_id}")
    assert task_response.status_code == 200
    body = task_response.json()
    assert body["status"] == "succeeded"
    assert body["result"]["diagnostics"]["source"] == "upload"
    assert "视频总结" in body["result"]["output"]
