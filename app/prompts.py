from __future__ import annotations

from app.schemas import AnalysisMode


SUMMARY_PROMPT = """请对以下视频转写文本进行结构化总结：

1. 一句话核心：用 1 句话概括视频主旨
2. 内容大纲：分章节列出主要论点
3. 关键要点：提取 3-5 条最有价值的信息
4. 行动建议：观众可以采取的具体行动

转写文本：
{transcript}
"""


INTERVIEW_PROMPT = """你是一位资深面试复盘助手。请从以下面试视频转写文本中提取：

1. 面试岗位：公司名和岗位名，如未提及请标注 [未提及]
2. 面试问题列表：按顺序列出所有问题
3. 回答要点：每个问题下提炼候选人的核心回答
4. 追问与反馈：提取面试官追问、评价或后续安排
5. 复盘建议：给出 3 条可执行改进建议

要求：
- 使用 Markdown
- 不确定的信息标注 [存疑]
- 不编造转写文本中不存在的事实

转写文本：
{transcript}
"""


def build_prompt(mode: AnalysisMode, transcript: str) -> str:
    if mode == AnalysisMode.interview:
        return INTERVIEW_PROMPT.format(transcript=transcript)
    return SUMMARY_PROMPT.format(transcript=transcript)

