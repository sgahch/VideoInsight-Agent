"""访问令牌鉴权测试。

重点覆盖"默认不启用"（本地零摩擦）与"启用后逐个入口都拦得住"。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.security as security_module
from app.config import Settings
from app.main import app
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo
from app.task_store import SQLiteTaskStore

TOKEN = "s3cret-token"
PROTECTED_ENDPOINTS = [
    ("GET", "/tasks"),
    ("GET", "/docs"),
    ("POST", "/parse-link"),
    ("POST", "/analyze"),
]


def _settings(tmp_path: Path, *, auth_token: str | None) -> Settings:
    settings = Settings(
        mock_mode=True,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        db_path=tmp_path / "tasks.db",
        auth_token=auth_token,
    )
    settings.ensure_dirs()
    return settings


@pytest.fixture
def secured(monkeypatch, tmp_path: Path):
    """启用鉴权的客户端（含独立 store）。"""
    settings = _settings(tmp_path, auth_token=TOKEN)
    store = SQLiteTaskStore(settings.db_path)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(security_module, "settings", settings)
    monkeypatch.setattr(main_module, "task_store", store)
    return TestClient(app), store


def test_auth_disabled_by_default(monkeypatch, tmp_path: Path) -> None:
    """未配置令牌时行为与之前完全一致：公开访问，且 /health 如实报告。"""
    settings = _settings(tmp_path, auth_token=None)
    store = SQLiteTaskStore(settings.db_path)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(security_module, "settings", settings)
    monkeypatch.setattr(main_module, "task_store", store)

    client = TestClient(app)
    assert client.get("/health").json()["auth_required"] is False
    assert client.get("/tasks").status_code == 200
    assert client.get("/docs").status_code == 200


def test_health_stays_public_when_auth_enabled(secured) -> None:
    """探活与页面本身必须匿名可达，否则前端没法提示用户填令牌。"""
    client, _ = secured
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["auth_required"] is True
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_protected_endpoints_reject_missing_token(secured) -> None:
    client, _ = secured
    for method, path in PROTECTED_ENDPOINTS:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} 未拦截"
        assert "未授权" in response.json()["detail"]


def test_protected_endpoints_reject_wrong_token(secured) -> None:
    client, _ = secured
    headers = {"Authorization": "Bearer wrong-token"}
    assert client.get("/tasks", headers=headers).status_code == 401
    assert client.get("/tasks", headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_bearer_token_is_accepted(secured) -> None:
    client, _ = secured
    response = client.get("/tasks", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


def test_x_auth_token_header_is_accepted(secured) -> None:
    client, _ = secured
    assert client.get("/tasks", headers={"X-Auth-Token": TOKEN}).status_code == 200


def test_cookie_token_is_accepted(secured) -> None:
    """媒体走浏览器原生请求，只有 Cookie 能带鉴权 —— 这条通道必须有效。"""
    client, _ = secured
    client.cookies.set(security_module.AUTH_COOKIE_NAME, TOKEN)
    assert client.get("/tasks").status_code == 200


def test_auth_session_exchanges_token_for_cookie(secured) -> None:
    client, _ = secured
    response = client.post("/auth/session", json={"token": TOKEN})

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "auth_required": True}
    assert security_module.AUTH_COOKIE_NAME in response.cookies
    # Cookie 生效后，不带任何请求头也能通过
    assert client.get("/tasks").status_code == 200


def test_auth_session_rejects_wrong_token(secured) -> None:
    client, _ = secured
    response = client.post("/auth/session", json={"token": "nope"})
    assert response.status_code == 401


def test_auth_session_is_noop_when_auth_disabled(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path, auth_token=None)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(security_module, "settings", settings)
    monkeypatch.setattr(main_module, "task_store", SQLiteTaskStore(settings.db_path))

    response = TestClient(app).post("/auth/session", json={"token": "anything"})
    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "auth_required": False}


def test_delete_auth_session_clears_cookie(secured) -> None:
    client, _ = secured
    # 走真实流程建立 Cookie，而不是手工塞进 cookie jar
    client.post("/auth/session", json={"token": TOKEN})
    assert client.get("/tasks").status_code == 200

    client.delete("/auth/session")

    assert client.get("/tasks").status_code == 401


def test_docs_are_protected_but_usable_once_authenticated(secured) -> None:
    """接口文档不公开，但鉴权后可用。

    刻意不关闭 /docs：Swagger UI 由浏览器发起同源请求，会自动带上鉴权 Cookie，
    所以对已授权的用户它是完整可用的 —— 关掉反而是自断功能。
    """
    client, _ = secured
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401

    client.post("/auth/session", json={"token": TOKEN})

    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_media_endpoint_requires_token(secured) -> None:
    """媒体端点是真正持有内容的入口，必须与任务端点同级保护。"""
    client, store = secured
    media_file = store.db_path.parent / "sample.mp4"
    media_file.write_bytes(b"video")
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    store.set_succeeded(
        task.task_id,
        AnalysisResult(
            mode=AnalysisMode.summary,
            transcript="t",
            output="o",
            media=MediaInfo(audio_path=str(media_file), source_url="https://example.com/a"),
        ),
    )

    assert client.get(f"/tasks/{task.task_id}/media").status_code == 401

    client.cookies.set(security_module.AUTH_COOKIE_NAME, TOKEN)
    assert client.get(f"/tasks/{task.task_id}/media").status_code == 200
