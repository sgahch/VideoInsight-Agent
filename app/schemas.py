from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class AnalysisMode(str, Enum):
    transcript = "transcript"
    summary = "summary"
    interview = "interview"


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class AnalyzeRequest(BaseModel):
    url: HttpUrl | str = Field(..., description="Video URL to analyze.")
    mode: AnalysisMode = AnalysisMode.summary
    use_mock: bool | None = Field(
        default=None,
        description="Override VIDEOINSIGHT_MOCK_MODE for this request.",
    )


class AnalyzeResponse(BaseModel):
    task_id: str
    status: TaskStatus
    status_url: str


class TranscriptSegment(BaseModel):
    start: float = 0
    end: float = 0
    text: str


class MediaInfo(BaseModel):
    audio_path: str | None = None
    title: str | None = None
    author: str | None = None
    duration: float | None = None
    source_url: str


class AnalysisResult(BaseModel):
    mode: AnalysisMode
    transcript: str
    segments: list[TranscriptSegment] = Field(default_factory=list)
    output: str
    media: MediaInfo
    artifacts: dict[str, str] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    task_id: str
    status: TaskStatus
    request: AnalyzeRequest
    created_at: datetime
    updated_at: datetime
    result: AnalysisResult | None = None
    error: str | None = None

