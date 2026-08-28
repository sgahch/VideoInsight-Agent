from __future__ import annotations

from app.prompts import build_prompt
from app.schemas import AnalysisMode


def test_summary_prompt_injects_metadata() -> None:
    prompt = build_prompt(
        AnalysisMode.summary,
        "hello world",
        metadata={"video_title": "示例标题", "author": "作者A", "platform": "快手", "duration": "01:23"},
    )

    assert "示例标题" in prompt
    assert "作者A" in prompt
    assert "快手" in prompt
    assert "01:23" in prompt
    assert "hello world" in prompt
    assert "## 1. 一句话核心" in prompt
    assert "## 4. 行动建议" in prompt


def test_summary_prompt_metadata_fallback() -> None:
    prompt = build_prompt(AnalysisMode.summary, "x")

    assert "未知标题" in prompt
    assert "未知平台" in prompt


def test_transcript_mode_does_not_use_summary_template() -> None:
    prompt = build_prompt(AnalysisMode.transcript, "raw text here")

    assert "raw text here" in prompt
    assert "一句话核心" not in prompt
    assert "面试岗位" not in prompt


def test_timestamp_note_switch() -> None:
    without = build_prompt(AnalysisMode.summary, "x")
    with_ts = build_prompt(AnalysisMode.summary, "x", include_timestamps=True)

    assert "禁止编造时间" in without
    assert "时间范围" in with_ts


def test_interview_prompt_injects_metadata() -> None:
    prompt = build_prompt(
        AnalysisMode.interview,
        "q a q a",
        metadata={"video_title": "面经", "platform": "抖音"},
    )

    assert "面经" in prompt
    assert "抖音" in prompt
    assert "## 问题列表" in prompt
    assert "## 复盘建议" in prompt
