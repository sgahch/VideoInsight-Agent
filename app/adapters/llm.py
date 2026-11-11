from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError
from app.prompts import build_chat_prompt, build_prompt
from app.schemas import AnalysisMode
from app.streaming import iter_events

SYSTEM_PROMPT = (
    "你是严谨的视频内容分析助手。严格遵守用户下发的任务规则与输出格式，"
    "仅依据提供的转写文本与元信息作答；禁止脑补画面或外部知识，不确定信息标注[存疑]。"
)

# ---------------------------------------------------------------------------
# 接口协议：同一个中转站/网关往往同时提供多种 OpenAI 风格端点，
# 用户侧可选（前端「接口协议」下拉）：
#   chat_completions → POST {base}/chat/completions（最通用，默认）
#   responses        → POST {base}/responses（OpenAI Responses API，Claude/GPT-5 系常用）
#   messages         → POST {base}/messages（Anthropic 原生协议）
# ---------------------------------------------------------------------------
PROTOCOL_CHAT_COMPLETIONS = "chat_completions"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_MESSAGES = "messages"
SUPPORTED_PROTOCOLS = (PROTOCOL_CHAT_COMPLETIONS, PROTOCOL_RESPONSES, PROTOCOL_MESSAGES)
DEFAULT_PROTOCOL = PROTOCOL_CHAT_COMPLETIONS

PROTOCOL_LABELS = {
    PROTOCOL_CHAT_COMPLETIONS: "Chat Completions（/chat/completions）",
    PROTOCOL_RESPONSES: "Responses（/responses）",
    PROTOCOL_MESSAGES: "Anthropic Messages（/messages）",
}


def normalize_protocol(value: str | None) -> str:
    """把各种写法归一化到受支持的协议名，未知值回退到默认协议。"""
    text = (value or "").strip().lower().replace("-", "_").replace(" ", "")
    if text in SUPPORTED_PROTOCOLS:
        return text
    if text in ("", "chat", "completions", "chatcompletion", "v1/chat/completions"):
        return PROTOCOL_CHAT_COMPLETIONS
    if text in ("response", "responsesapi", "v1/responses", "openai_responses"):
        return PROTOCOL_RESPONSES
    if text in ("anthropic", "anthropicmessages", "claude", "v1/messages", "message"):
        return PROTOCOL_MESSAGES
    return DEFAULT_PROTOCOL


def normalize_base_url(raw: str | None) -> str:
    """规范化 Base URL：去尾斜杠、补全协议头。"""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def candidate_base_urls(raw: str | None) -> list[str]:
    """生成候选 Base URL 列表。

    绝大多数中转站要求 `/v1` 前缀，而用户常常只填域名，
    结果请求打到站点首页（返回 HTML）→ JSON 解析失败。
    这里同时准备「原样」与「补 /v1」两个候选，失败时自动回退。
    """
    base = normalize_base_url(raw)
    if not base:
        return []
    candidates = [base]
    segments = [seg for seg in urlsplit(base).path.split("/") if seg]
    if "v1" not in segments:
        candidates.append(base + "/v1")
    return candidates


def _snippet(text: str, limit: int = 160) -> str:
    """把响应体（可能是 HTML）压成一行可读摘要。"""
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or (text or ""))[:limit]


class _RetryableEndpointError(Exception):
    """端点层面不可用（404/405 或返回非 JSON），可换候选地址重试。"""


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

    def chat_stream(
        self, question: str, transcript: str, metadata: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        yield self.chat(question, transcript, metadata=metadata)


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
        protocol: str | None = None,
    ) -> None:
        self.settings = settings
        self._provider_override = provider
        self._base_url_override = base_url
        self._model_override = model
        self._api_key_override = api_key
        self._protocol_override = protocol
        self.last_endpoint: str | None = None  # 最近一次成功请求的完整 URL（供自检回显）

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

    def analyze_stream(
        self,
        transcript: str,
        mode: AnalysisMode,
        metadata: dict[str, Any] | None = None,
        include_timestamps: bool = False,
        comments_text: str | None = None,
    ) -> Iterator[str]:
        yield from self._complete_stream(build_prompt(
            mode, transcript, metadata=metadata,
            include_timestamps=include_timestamps, comments_text=comments_text,
        ))

    def chat_stream(
        self, question: str, transcript: str, metadata: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        yield from self._complete_stream(build_chat_prompt(question, transcript, metadata=metadata))

    def _complete_stream(self, prompt: str) -> Iterator[str]:
        api_key = self._api_key_override or self._api_key()
        candidates = candidate_base_urls(self._base_url())
        if not candidates:
            raise WorkflowError("未配置 API Base URL，请填写类似 https://api.example.com/v1 的地址。")
        for index, base in enumerate(candidates):
            try:
                yield from self._request_stream(base + self._endpoint_path(), api_key, prompt)
                return
            except _RetryableEndpointError as exc:
                if index == len(candidates) - 1:
                    raise WorkflowError("LLM 接口未返回有效数据，请检查 API Base URL 与协议配置。") from exc

    def _request_stream(self, url: str, api_key: str, prompt: str) -> Iterator[str]:
        payload = self._build_payload(prompt, None)
        payload["stream"] = True
        try:
            with httpx.stream(
                "POST", url, headers=self._build_headers(api_key), json=payload, timeout=120,
            ) as response:
                if response.status_code in (404, 405):
                    raise _RetryableEndpointError()
                if response.is_error:
                    raise WorkflowError(self._http_error_message(response))
                self.last_endpoint = url
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    # Some compatible gateways accept stream=true but still return JSON.
                    response.read()
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise _RetryableEndpointError() from exc
                    if not isinstance(data, dict) or data.get("error"):
                        raise WorkflowError("LLM 返回内容异常，请检查模型与协议配置。")
                    try:
                        text = self._extract_text(data)
                    except (KeyError, IndexError, TypeError) as exc:
                        raise WorkflowError("LLM 返回内容格式异常，请检查接口协议。") from exc
                    if not text.strip():
                        raise WorkflowError("LLM 返回了空内容，请稍后重试。")
                    yield text
                    return

                finished = False
                has_text = False
                for event, raw in iter_events(response.iter_lines()):
                    if raw == "[DONE]":
                        finished = True
                        break
                    try:
                        data = json.loads(raw)
                    except ValueError as exc:
                        raise WorkflowError("LLM 流式响应格式异常，请检查接口协议。") from exc
                    if not isinstance(data, dict):
                        raise WorkflowError("LLM 流式响应格式异常，请检查接口协议。")
                    kind = data.get("type") or event
                    if data.get("error") or kind in ("error", "response.failed", "response.incomplete"):
                        raise WorkflowError("LLM 生成未完成，请稍后重试。")
                    text = ""
                    if self._protocol() == PROTOCOL_RESPONSES:
                        if kind == "response.output_text.delta":
                            text = data.get("delta", "")
                        elif kind == "response.completed":
                            finished = True
                    elif self._protocol() == PROTOCOL_MESSAGES:
                        if kind == "content_block_delta" and data.get("delta", {}).get("type") == "text_delta":
                            text = data["delta"].get("text", "")
                        elif kind == "content_block_start" and data.get("content_block", {}).get("type") == "text":
                            text = data["content_block"].get("text", "")
                        elif kind == "message_stop":
                            finished = True
                    else:
                        choices = data.get("choices") or []
                        if choices:
                            text = (choices[0].get("delta") or {}).get("content") or ""
                            if choices[0].get("finish_reason") is not None:
                                finished = True
                    if isinstance(text, str) and text:
                        has_text = True
                        yield text
                    if finished:
                        break
                if not finished:
                    raise WorkflowError("LLM 连接意外中断，已保留生成的部分内容。")
                if not has_text:
                    raise WorkflowError("LLM 返回了空内容，请稍后重试。")
        except httpx.HTTPError as exc:
            raise WorkflowError("LLM 服务连接中断，请检查网络后重试；已生成的内容会保留。") from exc

    def _complete(
        self, prompt: str, *, max_tokens: int | None = None, timeout: float = 120,
        for_probe: bool = False,
    ) -> str:
        api_key = self._api_key_override or self._api_key()
        candidates = candidate_base_urls(self._base_url())
        if not candidates:
            raise WorkflowError("未配置 API Base URL，请填写类似 https://api.example.com/v1 的地址。")

        last_retryable: str | None = None
        for index, base in enumerate(candidates):
            url = base + self._endpoint_path()
            try:
                return self._request(
                    url, api_key, prompt, max_tokens=max_tokens, timeout=timeout,
                    for_probe=for_probe,
                )
            except _RetryableEndpointError as exc:
                last_retryable = str(exc)
                if index == len(candidates) - 1:
                    break
                # 换下一个候选地址（补 /v1）再试一次
                continue
        raise WorkflowError(last_retryable or "LLM 请求失败，请检查 API Base URL 与模型配置。")

    def _request(
        self,
        url: str,
        api_key: str,
        prompt: str,
        *,
        max_tokens: int | None,
        timeout: float,
        for_probe: bool = False,
    ) -> str:
        payload = self._build_payload(prompt, max_tokens, for_probe=for_probe)
        try:
            response = httpx.post(
                url,
                headers=self._build_headers(api_key),
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in (404, 405):
                raise _RetryableEndpointError(
                    f"接口端点不存在（HTTP {status_code}）：{url}。"
                    "请确认 API Base URL 是否缺少 /v1 前缀。"
                ) from exc
            raise WorkflowError(self._http_error_message(exc.response)) from exc
        except httpx.HTTPError as exc:
            raise WorkflowError("LLM 服务连接失败，请检查网络、API Base URL 后重试。") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise _RetryableEndpointError(
                "LLM 返回了无法解析的响应（不是 JSON，几乎都是 API Base URL 填错："
                f"缺少 /v1 前缀，或填成了官网/控制台地址）。响应开头：{_snippet(response.text)}"
            ) from exc
        if not isinstance(data, dict):
            raise _RetryableEndpointError(
                f"LLM 返回了非对象响应：{_snippet(response.text)}"
            ) from exc
        if data.get("error"):
            # 部分网关用 200 携带错误体
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise WorkflowError(f"LLM 接口返回错误：{message}")

        self.last_endpoint = url
        try:
            return self._extract_text(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise WorkflowError(
                f"LLM 返回内容格式异常（协议 {self._protocol()}）：{json.dumps(data, ensure_ascii=False)[:300]}"
            ) from exc

    # ---- 协议相关构造 ----

    def _endpoint_path(self) -> str:
        protocol = self._protocol()
        if protocol == PROTOCOL_RESPONSES:
            return "/responses"
        if protocol == PROTOCOL_MESSAGES:
            return "/messages"
        return "/chat/completions"

    def _build_headers(self, api_key: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if self._protocol() == PROTOCOL_MESSAGES:
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def _build_payload(
        self, prompt: str, max_tokens: int | None, *, for_probe: bool = False,
    ) -> dict[str, Any]:
        protocol = self._protocol()
        model = self._model()
        official_deepseek = self._is_official_deepseek()
        if official_deepseek and re.search(r"\s", model):
            raise WorkflowError(
                "DeepSeek 模型名称需要填写 API 模型 ID，例如 deepseek-flash "
                "或 deepseek-v4-flash，不能填写 DeepSeek V4 Flash 这样的显示名称。"
            )
        if protocol == PROTOCOL_RESPONSES:
            payload: dict[str, Any] = {
                "model": model,
                "input": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            }
            # Responses API 用 max_output_tokens；不传 temperature（GPT-5/Claude 系常拒绝）
            if max_tokens:
                payload["max_output_tokens"] = max_tokens
            if for_probe and official_deepseek:
                payload["reasoning"] = {"effort": "none"}
            return payload
        if protocol == PROTOCOL_MESSAGES:
            return {
                "model": model,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens or 4096,
            }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if for_probe and official_deepseek:
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _extract_text(self, data: dict[str, Any]) -> str:
        protocol = self._protocol()
        if protocol == PROTOCOL_RESPONSES:
            direct = data.get("output_text")
            if isinstance(direct, str) and direct.strip():
                return direct
            for item in data.get("output") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") not in (None, "message"):
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in (None, "output_text", "text"):
                        text = part.get("text")
                        if text:
                            return str(text)
            # 有些网关直接把 chat 结构塞回来
            choices = data.get("choices")
            if choices:
                return str(choices[0]["message"]["content"])
            raise KeyError("output_text")
        if protocol == PROTOCOL_MESSAGES:
            blocks = data.get("content") or []
            texts = [
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type") in (None, "text")
            ]
            if texts:
                return "\n".join(texts)
            raise KeyError("content")
        return str(data["choices"][0]["message"]["content"])

    def _protocol(self) -> str:
        return normalize_protocol(self._protocol_override or getattr(self.settings, "llm_protocol", None))

    def _is_official_deepseek(self) -> bool:
        return urlsplit(normalize_base_url(self._base_url())).hostname == "api.deepseek.com"

    def probe(self) -> dict[str, Any]:
        """配置连通性自检：发一条极短请求，回报协议 / 端点 / 耗时。"""
        started = time.perf_counter()
        text = self._complete("请只回复两个字：正常", max_tokens=32, timeout=45, for_probe=True)
        if not text.strip():
            raise WorkflowError("LLM 返回了空内容，连接测试未通过，请检查模型与接口协议后重试。")
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "protocol": self._protocol(),
            "protocol_label": PROTOCOL_LABELS.get(self._protocol(), self._protocol()),
            "endpoint": self.last_endpoint or "",
            "model": self._model(),
            "latency_ms": elapsed_ms,
            "sample": text.strip()[:80],
        }

    def _http_error_message(self, response: httpx.Response) -> str:
        status_code = response.status_code
        provider = self._provider_display_name()
        if status_code == 400:
            hint = (
                "请填写 API 模型 ID（例如 deepseek-flash 或 deepseek-v4-flash），"
                "不要填写 DeepSeek V4 Flash 这样的显示名称，并检查接口协议。"
                if self._is_official_deepseek()
                else "请确认模型名称是供应商提供的 API 模型 ID，而不是显示名称，并检查接口协议。"
            )
            return f"{provider} 请求参数无效（HTTP 400）。{hint}"
        if status_code == 404:
            return (
                f"{provider} 接口路径不存在（HTTP 404）。请确认 API Base URL 是否正确——"
                "多数 OpenAI 兼容中转站需要以 /v1 结尾，例如 https://api.example.com/v1。"
            )
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
