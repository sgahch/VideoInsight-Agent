from __future__ import annotations

import httpx

from app.config import Settings
from app.errors import ExternalDependencyError, WorkflowError
from app.prompts import build_prompt
from app.schemas import AnalysisMode


class MockLlmAnalyzer:
    def analyze(self, transcript: str, mode: AnalysisMode) -> str:
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


class OpenAICompatibleAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def analyze(self, transcript: str, mode: AnalysisMode) -> str:
        api_key = self._api_key()
        prompt = build_prompt(mode, transcript)
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": "你是严谨的视频内容分析助手，只基于用户提供的转写文本回答。"},
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
        except httpx.HTTPError as exc:
            raise WorkflowError(f"LLM 调用失败：{exc}") from exc

        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _api_key(self) -> str:
        provider = self.settings.llm_provider.lower()
        if provider == "deepseek":
            key = self.settings.deepseek_api_key
        elif provider == "openai":
            key = self.settings.openai_api_key
        else:
            key = self.settings.deepseek_api_key or self.settings.openai_api_key
        if not key:
            raise ExternalDependencyError("未配置 LLM API Key，请设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。")
        return key

