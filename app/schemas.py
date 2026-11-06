from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AnalysisMode(str, Enum):
    transcript = "transcript"
    summary = "summary"
    interview = "interview"


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class ParseLinkRequest(BaseModel):
    text: str = Field(..., description="用户粘贴的分享文案或纯链接。")


class AuthSessionRequest(BaseModel):
    """用访问令牌换取 HttpOnly Cookie（供原生媒体请求使用鉴权）。"""

    token: str = Field(..., description="服务端配置的共享访问令牌。")


class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="Video URL to analyze. 支持纯链接，也支持平台分享文案（会自动裁剪）。")
    label: str | None = Field(
        default=None,
        description="可选的标准化标签（如从分享文案提取的标题）。用于历史记录展示，不要求填写。",
    )
    mode: AnalysisMode = AnalysisMode.summary
    use_mock: bool | None = Field(
        default=None,
        description="Override VIDEOINSIGHT_MOCK_MODE for this request.",
    )
    with_comments: bool = Field(
        default=True,
        description="是否抓取观众评论。配置完整评论服务后在后台遍历一级评论和子评论；旧版抖音直连仅返回热门评论。"
        "完整服务支持抖音、快手、B站和小红书，需相应平台的有效登录状态。"
        "抓取失败或平台不支持时自动跳过，不影响主分析链路。",
    )
    client_id: str | None = Field(
        default=None,
        description="客户端身份标识（浏览器生成的持久化 UUID）。用于任务归属隔离，"
        "列表/详情/删除/对话/媒体均只允许访问本客户端创建的任务。",
    )
    llm_provider: str | None = Field(default=None, exclude=True, repr=False)
    llm_base_url: str | None = Field(default=None, exclude=True, repr=False)
    llm_model: str | None = Field(default=None, exclude=True, repr=False)
    llm_protocol: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="接口协议：chat_completions（默认）/ responses / messages（Anthropic 原生）。",
    )
    llm_api_key: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="仅供本次 AI 分析调用，禁止写入任务记录。",
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


class CommentItem(BaseModel):
    """归一化后的观众评论（与 MediaCrawler 各平台评论字段对齐）。

    二级评论通过 parent_comment_id 串成树；sub_comments 为已展开的子评论列表。
    """

    comment_id: str
    parent_comment_id: str | None = None
    content: str = ""
    author_nickname: str | None = None
    like_count: int = 0
    create_time: str | None = None
    sub_comments: list["CommentItem"] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    mode: AnalysisMode
    transcript: str
    segments: list[TranscriptSegment] = Field(default_factory=list)
    output: str
    media: MediaInfo
    comments: list[CommentItem] | None = Field(
        default=None,
        description="兼容旧版评论数据；完整后台评论的任务 ID 位于 diagnostics.comment_job_id。",
    )
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
    # 实时进度（仅运行时由 progress 模块推算，不持久化）：
    # progress=百分比(0~100)，stage=阶段(downloading/transcribing/analyzing/done)，
    # eta_seconds=预计剩余秒数。Mock 模式或重启后追踪缺失时为 None。
    progress: float | None = None
    stage: str | None = None
    eta_seconds: float | None = None
    media_duration_seconds: float | None = None
    transcribe_done: int | None = None
    transcribe_total: int | None = None
    transcribe_seconds: float | None = None
    elapsed_seconds: float | None = None
    estimated_total_seconds: float | None = None


class TaskSummary(BaseModel):
    task_id: str
    status: TaskStatus
    request: AnalyzeRequest
    created_at: datetime
    updated_at: datetime
    title: str = ""
    media_title: str | None = None
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
    llm_protocol: str | None = Field(
        default=None,
        description="接口协议：chat_completions（默认）/ responses / messages（Anthropic 原生）。"
        "部分中转站只提供 responses 或 messages 端点。",
    )


class ChatResponse(BaseModel):
    answer: str = Field(..., description="仅依据该任务转写稿生成的回答（Markdown）。")


class LlmTestRequest(BaseModel):
    """模型配置连通性自检请求（不落库，仅发一条极短请求验证）。"""

    llm_provider: str | None = Field(default=None)
    llm_base_url: str | None = Field(default=None)
    llm_model: str | None = Field(default=None)
    llm_api_key: str = Field(default="", description="待验证的 API Key；留空测试服务端默认模型。")
    llm_protocol: str | None = Field(default=None, description="chat_completions / responses / messages")
