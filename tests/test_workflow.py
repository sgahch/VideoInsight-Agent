from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.schemas import AnalysisMode, AnalyzeRequest
from app.workflow import analyze


def test_mock_workflow_creates_summary(tmp_path: Path) -> None:
    settings = Settings(mock_mode=True, tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results")
    request = AnalyzeRequest(url="https://example.com/video", mode=AnalysisMode.summary)

    result = analyze(request, task_id="test-task", runtime_settings=settings)

    assert result.mode == AnalysisMode.summary
    assert "视频总结" in result.output
    assert result.segments
    assert Path(result.artifacts["result_json"]).exists()


def test_mock_workflow_creates_interview_report(tmp_path: Path) -> None:
    settings = Settings(mock_mode=True, tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results")
    request = AnalyzeRequest(url="https://example.com/video", mode=AnalysisMode.interview)

    result = analyze(request, task_id="test-task-interview", runtime_settings=settings)

    assert "面试问题列表" in result.output
    assert result.media.title

