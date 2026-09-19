from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.cleanup import (
    remove_task_artifacts,
    prune_expired_tasks,
    prune_orphan_task_dirs,
    prune_stale_intermediates,
    run_cleanup,
)
from app.config import Settings
from app.main import app
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo
from app.task_queue import TaskQueue
from app.task_store import SQLiteTaskStore

TASK_A = "a" * 32
TASK_B = "b" * 32


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        mock_mode=True,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        db_path=tmp_path / "tasks.db",
    )
    base.update(overrides)
    return Settings(**base)


def _settings_with_dirs(tmp_path: Path, **overrides) -> Settings:
    settings = _settings(tmp_path, **overrides)
    settings.ensure_dirs()
    return settings


def _make_task_dir(root: Path, name: str, payload: bytes = b"x" * 100) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "media.bin").write_bytes(payload)
    # 静默期护栏会跳过"刚创建"的目录，测试里把 mtime 拨回两小时前。
    old = time.time() - 7200
    os.utime(directory, (old, old))
    return directory


def test_prune_removes_orphan_task_dirs_and_reports_size(tmp_path: Path) -> None:
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    orphan = _make_task_dir(settings.tmp_dir, TASK_A, b"y" * 2048)

    report = prune_orphan_task_dirs(store, settings)

    assert report.dirs_removed == 1
    assert report.bytes_reclaimed >= 2048
    assert not orphan.exists()


def test_prune_keeps_live_task_dirs(tmp_path: Path) -> None:
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    live = _make_task_dir(settings.tmp_dir, task.task_id)

    report = prune_orphan_task_dirs(store, settings)

    assert report.dirs_removed == 0
    assert live.exists()


def test_prune_never_touches_user_assets(tmp_path: Path) -> None:
    """media/_cookies 与 media/_browser_profiles 是登录态资产，不是产物。"""
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    cookies = _make_task_dir(settings.media_dir, "_cookies")
    profile = _make_task_dir(settings.media_dir, "_browser_profiles")
    # 非 task_id 形状的目录（开发脚本留下的）也不该被误删
    scratch = _make_task_dir(settings.tmp_dir, "realtest")

    report = prune_orphan_task_dirs(store, settings)

    assert report.dirs_removed == 0
    assert cookies.exists() and profile.exists() and scratch.exists()


def test_prune_respects_min_age_guard(tmp_path: Path) -> None:
    """刚创建、可能仍在写入的目录本轮不动。"""
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    fresh = settings.tmp_dir / TASK_B
    fresh.mkdir(parents=True)
    (fresh / "in-flight.bin").write_bytes(b"z" * 10)

    report = prune_orphan_task_dirs(store, settings)  # 默认静默期 1 小时

    assert report.dirs_removed == 0
    assert fresh.exists()

    # 显式关掉静默期后就会被回收
    assert prune_orphan_task_dirs(store, settings, min_age_seconds=0).dirs_removed == 1
    assert not fresh.exists()


def _backdate_task(settings: Settings, task_id: str, hours: int) -> None:
    """把任务的 created_at 拨回 N 小时前，用来模拟历史数据。"""
    stamp = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE tasks SET created_at = ? WHERE task_id = ?", (stamp, task_id))
    conn.commit()
    conn.close()


def test_prune_expired_tasks_removes_record_and_artifacts(tmp_path: Path) -> None:
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    _backdate_task(settings, task.task_id, hours=48)
    _make_task_dir(settings.tmp_dir, task.task_id)
    _make_task_dir(settings.media_dir, task.task_id, b"v" * 4096)
    result_file = settings.results_dir / f"{task.task_id}.json"
    result_file.write_text("{}", encoding="utf-8")

    report = prune_expired_tasks(store, settings, retention_hours=24)

    assert report.tasks_removed == 1
    assert report.bytes_reclaimed >= 4096
    assert store.get(task.task_id) is None
    assert not result_file.exists()
    assert not (settings.tmp_dir / task.task_id).exists()
    assert not (settings.media_dir / task.task_id).exists()


def test_prune_expired_tasks_keeps_recent_and_is_off_by_default(tmp_path: Path) -> None:
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )

    # 默认（retention_hours=0）不按时间淘汰历史：静默删用户记录比占磁盘更糟
    assert run_cleanup(store, settings, retention_hours=0).tasks_removed == 0
    assert store.get(task.task_id) is not None

    # 保留期 24 小时，而任务刚创建 → 不该被删
    assert prune_expired_tasks(store, settings, retention_hours=24).tasks_removed == 0
    assert store.get(task.task_id) is not None


def test_prune_expired_tasks_uses_task_retention_setting(tmp_path: Path) -> None:
    settings = _settings_with_dirs(tmp_path, task_retention_hours=1)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    _backdate_task(settings, task.task_id, hours=2)

    # 未显式传参时走 settings.task_retention_hours
    assert run_cleanup(store, settings).tasks_removed == 1
    assert store.get(task.task_id) is None


def test_prune_removes_stale_intermediates_of_terminal_tasks(tmp_path: Path) -> None:
    """终态任务里的转写中间 WAV 是纯死重量，应被回收。"""
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    store.set_succeeded(
        task.task_id,
        AnalysisResult(
            mode=AnalysisMode.summary,
            transcript="t",
            output="o",
            media=MediaInfo(source_url="https://example.com/a"),
        ),
    )
    directory = _make_task_dir(settings.tmp_dir, task.task_id, b"small")
    intermediate = directory / f"{task.task_id}-16k.wav"
    intermediate.write_bytes(b"w" * 8192)
    chunk = directory / f"{task.task_id}-asr-0000.wav"
    chunk.write_bytes(b"c" * 4096)

    report = prune_stale_intermediates(store, settings)

    assert report.files_removed == 2
    assert report.bytes_reclaimed >= 12288
    assert not intermediate.exists() and not chunk.exists()


def test_prune_keeps_intermediates_of_running_task(tmp_path: Path) -> None:
    """正在跑的任务还需要那个 WAV，绝不能碰。"""
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    store.set_running(task.task_id)
    directory = _make_task_dir(settings.tmp_dir, task.task_id, b"small")
    intermediate = directory / f"{task.task_id}-16k.wav"
    intermediate.write_bytes(b"w" * 8192)

    report = prune_stale_intermediates(store, settings)

    assert report.files_removed == 0
    assert intermediate.exists()


def test_prune_keeps_user_upload_that_resembles_an_intermediate(tmp_path: Path) -> None:
    """用户上传的源文件哪怕叫 *-16k.wav 也不能删：它正是 media.audio_path。"""
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    task = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    store.set_succeeded(
        task.task_id,
        AnalysisResult(
            mode=AnalysisMode.summary,
            transcript="t",
            output="o",
            media=MediaInfo(source_url="https://example.com/a"),
        ),
    )
    directory = _make_task_dir(settings.media_dir, task.task_id, b"small")
    uploaded = directory / "podcast-16k.wav"  # 用户自己的文件名
    uploaded.write_bytes(b"user audio")

    report = prune_stale_intermediates(store, settings)

    assert report.files_removed == 0
    assert uploaded.exists()


def test_remove_task_artifacts_is_idempotent(tmp_path: Path) -> None:
    settings = _settings_with_dirs(tmp_path)
    _make_task_dir(settings.tmp_dir, TASK_A)
    (settings.results_dir / f"{TASK_A}.json").write_text("{}", encoding="utf-8")

    assert remove_task_artifacts(settings, TASK_A) > 0
    assert remove_task_artifacts(settings, TASK_A) == 0  # 再删一次不报错


def test_all_task_ids(tmp_path: Path) -> None:
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    first = store.create(
        AnalyzeRequest(url="https://example.com/a", mode=AnalysisMode.summary, use_mock=True)
    )
    second = store.create(
        AnalyzeRequest(url="https://example.com/b", mode=AnalysisMode.summary, use_mock=True)
    )

    assert store.all_task_ids() == {first.task_id, second.task_id}


def _isolate_queue(monkeypatch) -> None:
    """让 lifespan 测试使用自己的队列实例。

    进程级的 `main.task_queue` 一旦被 shutdown 就永久关闭（生产环境里这正好
    等于进程退出）。测试若直接关掉这个共享单例，后面所有提交都会被当成
    "服务正在关闭"而不再执行 —— 测试顺序会因此互相污染。
    """
    monkeypatch.setattr(main_module, "task_queue", TaskQueue(1))


def test_lifespan_reclaims_orphans_at_startup(monkeypatch, tmp_path: Path) -> None:
    """集成：服务启动时确实跑了一次回收。

    必须用 `with TestClient(app)` —— 不用上下文管理器时 Starlette 不会触发
    lifespan，那样这条接线就完全没被覆盖到。
    """
    _isolate_queue(monkeypatch)
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    orphan = settings.tmp_dir / ("c" * 32)
    orphan.mkdir(parents=True)
    (orphan / "junk.bin").write_bytes(b"j" * 1024)
    old = time.time() - 7200  # 越过静默期护栏
    os.utime(orphan, (old, old))

    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "task_store", store)

    with TestClient(app):
        pass

    assert not orphan.exists()


def test_lifespan_survives_cleanup_failure(monkeypatch, tmp_path: Path) -> None:
    """回收失败绝不能拖垮服务启动。"""
    _isolate_queue(monkeypatch)
    settings = _settings_with_dirs(tmp_path)
    store = SQLiteTaskStore(settings.db_path)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "task_store", store)

    def boom(*_args, **_kwargs):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(main_module, "run_cleanup", boom)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
