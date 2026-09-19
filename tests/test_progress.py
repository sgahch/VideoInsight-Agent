from __future__ import annotations

from pathlib import Path

import app.progress as progress
import pytest


def test_live_progress_uses_chunk_data_and_reaches_100_only_when_done(
    monkeypatch, tmp_path: Path
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])

    reporter = progress.ProgressReporter("progress-task", "summary", False)
    reporter.mark_download_done(120.0)
    reporter.mark_transcribe_started(120.0, 4)

    initial = progress.get_snapshot("progress-task", tmp_path)
    assert initial is not None
    assert initial["stage"] == "transcribing"
    assert initial["media_duration"] == 120.0
    assert initial["transcribe_done"] == 0
    assert initial["transcribe_total"] == 4
    assert initial["progress"] < 100

    clock[0] = 1005.0
    reporter.on_transcribe_chunk(1, 4)
    halfway = progress.get_snapshot("progress-task", tmp_path)
    assert halfway is not None
    assert halfway["transcribe_done"] == 1
    assert halfway["progress"] >= initial["progress"]

    clock[0] = 1020.0
    reporter.mark_transcribe_done(20.0, 120.0)
    reporter.mark_analyzing()
    analyzing = progress.get_snapshot("progress-task", tmp_path)
    assert analyzing is not None
    assert analyzing["stage"] == "analyzing"
    assert analyzing["progress"] >= halfway["progress"]
    assert analyzing["progress"] < 100

    reporter.mark_done()
    done = progress.get_snapshot("progress-task", tmp_path)
    assert done is not None
    assert done["stage"] == "done"
    assert done["progress"] == 100.0

    progress.drop_tracker("progress-task")


def test_upload_progress_does_not_add_download_estimate(monkeypatch, tmp_path: Path) -> None:
    clock = [2000.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])

    reporter = progress.ProgressReporter(
        "upload-progress-task",
        "transcript",
        False,
        download_required=False,
    )
    reporter.mark_download_done(None)
    reporter.mark_transcribe_started(60.0, 2)

    snapshot = progress.get_snapshot("upload-progress-task", tmp_path)
    assert snapshot is not None
    assert snapshot["stage"] == "transcribing"
    # 上传文件已在请求阶段准备好，ETA 不应额外加入固定下载阶段。
    assert snapshot["estimated_total_seconds"] == pytest.approx(60.0 / 7.7 + 77.0, abs=0.1)

    progress.drop_tracker("upload-progress-task")
