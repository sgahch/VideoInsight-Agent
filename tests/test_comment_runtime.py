from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import types
from pathlib import Path

import pytest
from dotenv import dotenv_values

from crawler_service import manage, runtime, worker
from crawler_service.pagination import Pager
from crawler_service.store import Store


def test_local_configuration_preserves_application_secrets_and_service_token(monkeypatch, tmp_path):
    monkeypatch.setattr(manage, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("COMMENT_CRAWLER_URL", raising=False)
    monkeypatch.delenv("COMMENT_CRAWLER_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text("# keep this comment\nLLM_MODEL=existing-model\nOPENAI_API_KEY=keep-secret\n", encoding="utf-8")
    manage.configure_local(8091)
    first = dotenv_values(env)
    manage.configure_local(8091)
    second = dotenv_values(env)
    assert first["COMMENT_CRAWLER_TOKEN"] == second["COMMENT_CRAWLER_TOKEN"]
    assert len(first["COMMENT_CRAWLER_TOKEN"]) >= 48
    assert second["COMMENT_CRAWLER_URL"] == "http://127.0.0.1:8091"
    assert second["OPENAI_API_KEY"] == "keep-secret"
    assert second["LLM_MODEL"] == "existing-model"
    assert "# keep this comment" in env.read_text(encoding="utf-8")
    assert Path(second["COMMENT_CRAWLER_STATE"]).is_absolute()


def test_local_configuration_does_not_replace_an_external_service(monkeypatch, tmp_path):
    monkeypatch.setattr(manage, "PROJECT_ROOT", tmp_path)
    path = tmp_path / ".env"
    original = "COMMENT_CRAWLER_URL=http://private-crawler:8090\nCOMMENT_CRAWLER_TOKEN=keep\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(RuntimeError, match="external"):
        manage.configure_local(8090)
    assert path.read_text(encoding="utf-8") == original


def test_runtime_paths_are_absolute_and_preflight_fails_without_source(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEDIACRAWLER_DIR", "not-installed")
    monkeypatch.setenv("COMMENT_CRAWLER_STATE", "comments")
    assert runtime.state_dir() == tmp_path / "comments"
    assert runtime.upstream_dir() == tmp_path / "not-installed"
    report = runtime.check_runtime()
    assert report["ready"] is False
    assert report["source_installed"] is False
    assert "install" in report["error"]


def test_blank_path_overrides_use_local_defaults(monkeypatch):
    monkeypatch.setenv("MEDIACRAWLER_DIR", "")
    monkeypatch.setenv("COMMENT_CRAWLER_STATE", "")
    assert runtime.upstream_dir().name == "MediaCrawler"
    assert runtime.state_dir().name in {"comments", "state"}


@pytest.mark.parametrize("channel,options", [("", {}), ("chrome", {"channel": "chrome"}), ("msedge", {"channel": "msedge"})])
def test_browser_channel_is_explicit(monkeypatch, channel, options):
    monkeypatch.setenv("COMMENT_CRAWLER_BROWSER_CHANNEL", channel)
    assert runtime.browser_options() == options


def test_invalid_browser_channel_is_rejected(monkeypatch):
    monkeypatch.setenv("COMMENT_CRAWLER_BROWSER_CHANNEL", "/arbitrary/program")
    with pytest.raises(ValueError):
        runtime.browser_options()


@pytest.mark.parametrize("windows", [True, False])
def test_worker_process_uses_native_process_group_and_absolute_state(monkeypatch, tmp_path, windows):
    api = importlib.import_module("crawler_service.api")
    store = Store(tmp_path / "comments.db")
    job = store.create("owner", "https://www.douyin.com/video/123456789")
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "STATE", tmp_path)
    monkeypatch.setattr(api.sys, "platform", "win32" if windows else "linux")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    calls = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        store.status(job["id"], "completed")
        return types.SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(api.subprocess, "Popen", spawn)
    api.execute(job["id"])
    command, options = calls[0]
    assert command[-1] == job["id"]
    assert options["cwd"] == runtime.PROJECT_ROOT
    assert options["env"]["COMMENT_CRAWLER_STATE"] == str(tmp_path)
    if windows:
        assert options["creationflags"] == 512
        assert "start_new_session" not in options
    else:
        assert options["start_new_session"] is True
    assert job["id"] not in api.processes


def test_windows_cancellation_terminates_only_the_worker_tree(monkeypatch):
    api = importlib.import_module("crawler_service.api")
    calls = []
    monkeypatch.setattr(api.sys, "platform", "win32")
    monkeypatch.setattr(api.subprocess, "run", lambda command, **options: calls.append(command))
    api.stop_process(types.SimpleNamespace(pid=12345, poll=lambda: None))
    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]
    api.stop_process(types.SimpleNamespace(pid=67890, poll=lambda: 0))
    assert len(calls) == 1


def test_failed_worker_spawn_does_not_leave_running_status(monkeypatch, tmp_path):
    api = importlib.import_module("crawler_service.api")
    store = Store(tmp_path / "comments.db")
    job = store.create("owner", "https://www.douyin.com/video/123456789")
    monkeypatch.setattr(api, "store", store)

    def fail(*args, **kwargs):
        raise OSError("private details must not be shown")

    monkeypatch.setattr(api.subprocess, "Popen", fail)
    api.execute(job["id"])
    assert store.job(job["id"])["status"] == "failed"
    assert "private details" not in store.job(job["id"])["message"]


def test_worker_drives_upstream_crawler_into_paginated_comment_store(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMENT_CRAWLER_STATE", str(tmp_path))
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    (cookies / "dy.txt").write_text("sessionid=private-value", encoding="utf-8")
    monkeypatch.setattr(worker, "upstream_dir", lambda: tmp_path)
    monkeypatch.setattr(worker, "runtime_status", lambda: {
        "source_installed": True, "playwright_installed": True, "node_installed": True,
    })
    monkeypatch.setattr(worker, "Pager", lambda *args: Pager(*args, interval=0))
    config = types.ModuleType("config")
    monkeypatch.setitem(sys.modules, "config", config)
    context = types.SimpleNamespace()
    context.closed = False

    async def add_cookies(entries):
        assert entries[0]["name"] == "sessionid"

    async def close():
        context.closed = True

    context.add_cookies, context.close = add_cookies, close

    class Client:
        async def get_aweme_all_comments(self, *args, **kwargs):
            raise AssertionError("The upstream bounded collector must be replaced.")

        async def get_aweme_comments(self, video, cursor):
            return {
                "comments": [{"cid": str(i), "text": f"Comment {i}", "digg_count": i,
                              "reply_comment_total": 2 if i == 1 else 0}
                             for i in (range(1, 13) if cursor == 0 else range(13, 25))],
                "cursor": 12 if cursor == 0 else 24, "has_more": cursor == 0,
            }

        async def get_sub_comments(self, video, root, cursor):
            return {"comments": [{"cid": str(100 + cursor), "text": "Reply"}],
                    "cursor": cursor + 1, "has_more": cursor == 0}

    class Crawler:
        async def launch_browser(self, chromium):
            return context

        async def start(self):
            self.browser_context = await self.launch_browser(types.SimpleNamespace())
            await Client().get_aweme_all_comments("123")

    core = types.ModuleType("media_platform.douyin.core")
    core.DouYinCrawler = Crawler
    client_module = types.ModuleType("media_platform.douyin.client")
    client_module.DouYinClient = Client
    monkeypatch.setitem(sys.modules, core.__name__, core)
    monkeypatch.setitem(sys.modules, client_module.__name__, client_module)
    store = Store(tmp_path / "comments.db")
    job = store.create("owner", "https://www.douyin.com/video/123")
    original_dir = Path.cwd()
    asyncio.run(worker.run(job["id"]))
    result = store.job(job["id"])
    assert result["status"] == "completed"
    assert (result["roots"], result["replies"], result["total"]) == (24, 2, 26)
    assert config.ENABLE_GET_SUB_COMMENTS is True
    assert config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES > 10
    assert store.comments(job["id"], 0, 10, sort="likes", scope="roots", top=10)[0]["comment_id"] == "24"
    assert context.closed
    assert Path.cwd() == original_dir
