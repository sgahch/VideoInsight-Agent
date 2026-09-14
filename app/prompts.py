"""提示词模板 V2（整合版）。

设计要点：
- 元信息注入：标题/作者/平台/时长（缺失时使用"未知"占位）
- 输出格式契约：固定 Markdown 板块 + 字数约束，方便前端排版
- 硬性兜底：歌词/噪音/空文本场景直接拒绝总结（输出固定判定串，便于前端识别）
- 忠实度：只依据转写文本，禁止脑补画面；不确定信息标注 [存疑]
- 时间戳开关：include_timestamps=True 时允许按时间组织大纲；否则明确禁止编造时间
- transcript 模式：workflow 层直接返回原文（跳过 LLM 调用），此处模板仅作兜底
"""

from __future__ import annotations

from typing import Any

from app.schemas import AnalysisMode, CommentItem

# ---------- 元信息注入 ----------

METADATA_TEMPLATE = """# 视频元信息
- 标题：{title}
- 作者：{author}
- 平台：{platform}
- 时长：{duration}
"""


# ---------- 观众评论上下文（可选输入块，不计入输出格式契约） ----------

COMMENTS_TEMPLATE = """# 观众评论（可选上下文，仅用于丰富分析视角）
以下是该视频的部分观众评论。**仅作为补充视角，不得脱离【转写文本】与【观众评论】本身编造**：
- 可结合评论中的高频观点、情绪倾向、争议点或追问，补充到「关键要点」与「行动建议」。
- 评论可能含噪声、跑题或营销内容，请自行甄别；无法确定是针对视频本身的，忽略即可。
- 不要新增输出板块，严格遵守上方格式契约。
{comments}
"""


# ---------- 总结模板 ----------

SUMMARY_PROMPT = """# 任务说明
你是严谨的视频内容分析助手，仅依据下方【视频元信息】与【转写文本】生成结构化总结。
禁止脑补视频画面、外部知识或未提及的信息；信息无法确定时标注 [存疑]，严禁编造。

{metadata}
# 硬性兜底规则（必须优先执行）
1. 若转写文本以歌词/歌曲为主、大量噪音、无连贯语义或有效内容极少，直接输出：
> ⚠️ 提示：当前转写内容多为歌词/噪音，无法生成有效总结
禁止强行总结。
2. 若转写文本为空或只有空白字符，输出：
> ⚠️ 提示：未获取到有效的视频文字稿，无法总结。

# 输出格式契约（严格 Markdown，板块不可删减、不可调换顺序、不得新增板块）
## 1. 一句话核心
（不超过 50 字，一句话概括视频主旨）

## 2. 内容大纲
分章节列出主要论点，每项以 `-` 开头、不超过 30 字
{timestamp_note}

## 3. 关键要点
提取 3-5 条最有价值的信息，每项以 `-` 开头、不超过 40 字

## 4. 行动建议
给出观众可落地的具体行动，每项以 `-` 开头、不超过 30 字；没有可行建议则写「无」

{comments_block}
# 转写文本
{transcript}
"""


# ---------- 面经提取模板 ----------

INTERVIEW_PROMPT = """# 任务说明
你是专业的面试复盘分析师，仅依据下方【视频元信息】与【面试转写文本】提取面经内容。
禁止编造不存在的问题或回答；信息不确定处标注 [存疑]，严禁编造。

{metadata}
# 硬性兜底规则（必须优先执行）
1. 若转写文本以歌词/歌曲为主、大量噪音、无连贯语义或有效内容极少，直接输出：
> ⚠️ 提示：当前转写内容多为歌词/噪音，无法提取面经
2. 若转写文本为空或只有空白字符，输出：
> ⚠️ 提示：未获取到有效的视频文字稿，无法提取面经
3. 若文本中未检测到任何面试相关问题，输出：
> ⚠️ 提示：未检测到有效的面试交互信息

# 输出格式契约（严格 Markdown，板块不可删减、不可调换顺序、不得新增板块）
## 面试岗位
（从文本提取岗位名称；未提及则写 [存疑]）

## 问题列表
按出现顺序列出所有面试问题，每项以 `-` 开头

## 回答要点
对应每个问题提炼受访者核心回答，每项以 `-` 开头、不超过 40 字

## 追问反馈
提取面试官的追问、评价或后续安排；无则写「无」

## 复盘建议
给出 3-5 条求职者可参考的复盘建议，每项以 `-` 开头、不超过 40 字

{comments_block}
# 转写文本
{transcript}
"""


# ---------- AI 对话模板（围绕单个任务的转写稿问答） ----------

CHAT_PROMPT = """# 任务说明
你是视频内容分析助手，正在围绕下方【视频转写稿】与用户进行多轮对话。
用户可能要求分段总结、提取关键观点、整理时间轴或解答针对内容的疑问。

{metadata}
# 回答规则（必须严格遵守）
1. 唯一依据是下方【视频转写稿】；禁止使用转写稿之外的外部知识，禁止脑补画面。
2. 用户的问题若超出转写稿范围，直接回答「转写稿中未提及」，不要猜测或补充。
3. 信息不确定、转写不清或存在歧义的地方，在对应位置标注 [存疑]。
4. 用 Markdown 输出：必要时使用小标题或列表，条理清晰，不要复述用户的问题。
5. 回答控制在 300 字以内，使用中文。

# 视频转写稿
{transcript}

# 用户问题
{question}
"""


# ---------- transcript 模式模板（workflow 通常直接返回原文，此处仅兜底） ----------

TRANSCRIPT_PROMPT = """# 任务说明
请直接原样输出以下转写文本，**不做任何总结、修改、润色或分段**，仅输出转写内容本身。

# 转写文本
{transcript}
"""


def render_metadata(metadata: dict[str, Any] | None = None) -> str:
    """渲染【视频元信息】区块，缺失字段使用"未知"占位。"""
    meta = metadata or {}
    return METADATA_TEMPLATE.format(
        title=meta.get("video_title") or "未知标题",
        author=meta.get("author") or "未知作者",
        platform=meta.get("platform") or "未知平台",
        duration=meta.get("duration") or "未知时长",
    )


def build_chat_prompt(
    question: str,
    transcript: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """构建 AI 对话提示词：把任务转写稿作为唯一依据注入。

    参数：
        question: 用户提问
        transcript: 该任务的转写稿（可含时间戳）
        metadata: 包含 video_title / author / platform / duration 的字典（可缺省）
    """
    return CHAT_PROMPT.format(
        metadata=render_metadata(metadata),
        transcript=transcript,
        question=question,
    )


def render_comments_block(comments: list[CommentItem]) -> str:
    """把归一化评论渲染成提示词里的观众评论上下文块（Markdown 列表）。"""
    lines: list[str] = []
    for comment in comments:
        prefix = f"@{comment.author_nickname} " if comment.author_nickname else ""
        lines.append(f"- {prefix}（赞 {comment.like_count}）{comment.content}")
        for sub in comment.sub_comments:
            sub_prefix = f"@{sub.author_nickname} " if sub.author_nickname else ""
            lines.append(f"    - ↳ {sub_prefix}（赞 {sub.like_count}）{sub.content}")
    return "\n".join(lines)


def build_prompt(
    mode: AnalysisMode,
    transcript: str,
    metadata: dict[str, Any] | None = None,
    include_timestamps: bool = False,
    comments_text: str | None = None,
) -> str:
    """根据模式构建提示词。

    参数：
        mode: summary / interview / transcript
        transcript: 转写文本（include_timestamps=True 时应传入带时间戳的版本）
        metadata: 包含 video_title / author / platform / duration 的字典（可缺省）
        include_timestamps: 转写文本是否带时间戳（决定大纲是否允许标注时间范围）
        comments_text: 观众评论上下文块（由 render_comments_block 生成），有则注入
    """
    metadata_block = render_metadata(metadata)

    timestamp_note = (
        "（转写文本含时间戳：内容大纲可按时间顺序组织，并可标注每章时间范围）"
        if include_timestamps
        else "（转写文本不含时间戳：请按逻辑顺序组织，禁止编造时间或时间范围）"
    )

    if mode == AnalysisMode.transcript:
        return TRANSCRIPT_PROMPT.format(transcript=transcript)

    comments_block = COMMENTS_TEMPLATE.format(comments=comments_text) if comments_text else ""
    template = INTERVIEW_PROMPT if mode == AnalysisMode.interview else SUMMARY_PROMPT
    return template.format(
        metadata=metadata_block,
        timestamp_note=timestamp_note,
        comments_block=comments_block,
        transcript=transcript,
    )
