from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError
from app.prompts import build_chat_prompt, build_prompt
from app.schemas import AnalysisMode

SYSTEM_PROMPT = (
    "你是严谨的视频内容分析助手。严格遵守用户下发的任务规则与输出格式，"
    "仅依据提供的转写文本与元信息作答；禁止脑补画面或外部知识，不确定信息标注[存疑]。"
)


class MockLlmAnalyzer:
    def analyze(
        self,
        transcript: str,
        mode: AnalysisMode,
        metadata: dict[str, Any] | None = None,
        include_timestamps: bool = False,
        comments_text: str | None = None,
    ) -> str:
        if mode == AnalysisMode.transcript:
            return transcript
        if mode == AnalysisMode.interview:
            return """## 面试复盘

### 面试岗位
AI 产品经理岗位 [示例]

### 面试问题列表
1. 请介绍这个项目的背景和核心流程。
2. 如何控制 LLM 生成中的幻觉？
3. 视频下载和内容分析有哪些合规风险？

### 回答要点
- 项目从链接解析、音频提取、ASR 到结构化总结形成固定工作流。
- 事实核查拆成声明抽取、联网搜索、证据比对和风险标注。
- 合规上需要用户授权、内容审核和原始音视频生命周期管理。

### 复盘建议
1. 先验证真实平台下载成功率。
2. 建立 ASR 术语表，提高技术词准确率。
3. 在输出中明确区分事实、推断和无法验证的信息。
"""
        return """## 视频总结

### 一句话核心
这段内容介绍了视频分析助手第一阶段应先跑通链接、转写和结构化总结的最小闭环。

### 内容大纲
- 项目流程：链接解析、音频提取、ASR 转写、LLM 总结。
- 质量控制：用事实核查降低幻觉风险。
- 合规要求：用户授权、敏感内容审核、原始文件自动删除。

### 行动建议
1. 用 5-10 个真实视频测试核心链路。
2. 记录下载成功率、转写耗时和摘要质量。
3. 第一阶段先保持 Workflow 架构，暂不引入复杂 Agent。
"""

    def chat(
        self,
        question: str,
        transcript: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        title = (metadata or {}).get("video_title") or "未命名视频"
        excerpt = transcript.strip()[:30] or "（转写稿为空）"
        return (
            "[Mock 模式] 当前未连接真实大模型，以下为示例回答，不代表真实分析结果。\n\n"
            f"- 你的问题：{question}\n"
            f"- 对话上下文：《{title}》，转写稿约 {len(transcript)} 字。\n\n"
            "真实模式下我会只依据这段转写稿作答，例如：\n"
            "1. 结论：（基于转写稿给出直接回答）\n"
            f"2. 依据：转写稿中提到「{excerpt}…」\n"
            "3. 兜底：超出转写稿范围的问题会直接回答「转写稿中未提及」，"
            "不确定的地方标注 [存疑]。\n\n"
            "提示：关闭 Mock 模式（深度思考模式）并配置 LLM API Key 后可获得真实回答。"
        )


class OpenAICompatibleAnalyzer:
    """OpenAI 兼容 Chat Completions 分析器。

    默认使用服务器 Settings 中的配置；也可通过 __init__ 传入用户自备的
    provider / base_url / model / api_key 覆盖（用于多用户场景，让用户
    用自己的模型与 Key 进行对话）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.settings = settings
        self._provider_override = provider
        self._base_url_override = base_url
        self._model_override = model
        self._api_key_override = api_key

    def analyze(
        self,
        transcript: str,
        mode: AnalysisMode,
        metadata: dict[str, Any] | None = None,
        include_timestamps: bool = False,
        comments_text: str | None = None,
    ) -> str:
        prompt = build_prompt(
            mode,
            transcript,
            metadata=metadata,
            include_timestamps=include_timestamps,
            comments_text=comments_text,
        )
        return self._complete(prompt)

    def chat(
        self,
        question: str,
        transcript: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        prompt = build_chat_prompt(question, transcript, metadata=metadata)
        return self._complete(prompt)

    def _complete(self, prompt: str) -> str:
        api_key = self._api_key_override or self._api_key()
        url = self._base_url().rstrip("/") + "/chat/completions"
        payload = {
            "model": self._model(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise WorkflowError(self._http_error_message(exc.response)) from exc
        except httpx.HTTPError as exc:
            raise WorkflowError("LLM 服务连接失败，请检查网络、API Base URL 后重试。") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise WorkflowError("LLM 返回了无法解析的响应，请检查 API Base URL 和模型配置。") from exc
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise WorkflowError(f"LLM 返回内容格式异常：{data}") from exc

    def _http_error_message(self, response: httpx.Response) -> str:
        status_code = response.status_code
        provider = self._provider_display_name()
        if status_code == 401:
            return f"{provider} API Key 无效或已过期，请检查密钥配置。"
        if status_code == 402:
            return (
                f"{provider} 账户余额不足或尚未开通计费。请前往供应商控制台充值，"
                "或在配置中切换到有可用额度的模型后重试。"
            )
        if status_code == 403:
            return f"{provider} 拒绝了请求，请检查 API Key 权限和模型访问权限。"
        if status_code == 429:
            return f"{provider} 请求过于频繁或额度已用尽，请稍后重试或检查账户配额。"
        if status_code >= 500:
            return f"{provider} 服务暂时不可用（HTTP {status_code}），请稍后重试。"
        return f"{provider} 请求失败（HTTP {status_code}），请检查模型与接口配置。"

    def _provider_display_name(self) -> str:
        provider = self._provider()
        if provider == "deepseek":
            return "DeepSeek"
        if provider == "openai":
            return "OpenAI"
        return provider or "LLM"

    def _base_url(self) -> str:
        return self._base_url_override or self.settings.llm_base_url

    def _model(self) -> str:
        return self._model_override or self.settings.llm_model

    def _provider(self) -> str:
        return (self._provider_override or self.settings.llm_provider).lower()

    def _api_key(self) -> str:
        provider = self._provider()
        if provider == "deepseek":
            key = self.settings.deepseek_api_key
        elif provider == "openai":
            key = self.settings.openai_api_key
        else:
            key = self.settings.deepseek_api_key or self.settings.openai_api_key
        if not key:
            raise ExternalDependencyError("未配置 LLM API Key，请设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。")
        return key
