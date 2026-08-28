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
    client_id: str | None = Field(
        default=None,
        description="客户端身份标识（浏览器生成的持久化 UUID）。用于任务归属隔离，"
        "列表/详情/删除/对话/媒体均只允许访问本客户端创建的任务。",
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
    title: str = Field(
        default="",
        description="AI 生成的任务摘要标题（分析完成后由 workflow 提取一句话核心生成）。",
    )
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


class ChatRequest(BaseModel):
    question: str = Field(..., description="用户针对某个已完成任务转写稿提出的问题。")
    # 用户自备模型配置（可选）：提供时对话使用用户自己的 LLM，否则使用服务器配置
    llm_provider: str | None = Field(
        default=None,
        description="LLM 提供商（deepseek / openai / 其他），用于拼接密钥与提示。",
    )
    llm_base_url: str | None = Field(
        default=None,
        description="OpenAI 兼容 API Base URL，如 https://api.deepseek.com/v1",
    )
    llm_model: str | None = Field(
        default=None,
        description="模型名，如 deepseek-chat / gpt-4o-mini",
    )
    llm_api_key: str | None = Field(
        default=None,
        description="用户自己的 API Key（仅本次请求使用，不会持久化）。",
    )


class ChatResponse(BaseModel):
    answer: str = Field(..., description="仅依据该任务转写稿生成的回答（Markdown）。")

