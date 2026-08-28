from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.schemas import AnalysisMode, AnalyzeRequest
from app.workflow import analyze, detect_platform, format_duration


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


def test_mock_workflow_transcript_mode_returns_raw_transcript(tmp_path: Path) -> None:
    settings = Settings(mock_mode=True, tmp_dir=tmp_path / "tmp", results_dir=tmp_path / "results")
    request = AnalyzeRequest(url="https://example.com/video", mode=AnalysisMode.transcript)

    result = analyze(request, task_id="test-task-transcript", runtime_settings=settings)

    # transcript 模式跳过 LLM，直接返回转写原文（不带总结结构）
    assert "面试复盘" not in result.output
    assert "视频总结" not in result.output
    assert "大家好" in result.output


def test_detect_platform_variants() -> None:
    assert detect_platform("https://v.kuaishou.com/JExSMSsx") == "快手"
    assert detect_platform("https://v.douyin.com/lmwHu2zoSII/") == "抖音"
    assert detect_platform("https://b23.tv/OEG7G4m") == "B站"
    assert detect_platform("https://xhslink.cn/o/6990bRO5ehG") == "小红书"
    assert detect_platform("upload://sample.mp4") == "上传文件"
    assert detect_platform("https://example.com/video") == "未知平台"


def test_format_duration() -> None:
    assert format_duration(90) == "01:30"
    assert format_duration(3661) == "01:01:01"
    assert format_duration(0) == "未知"
    assert format_duration(None) == "未知"

