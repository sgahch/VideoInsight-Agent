from __future__ import annotations

import json
from pathlib import Path

from app.adapters.asr import MockTranscriber, ParaformerTranscriber, format_transcript
from app.adapters.downloader import select_downloader
from app.adapters.llm import MockLlmAnalyzer, OpenAICompatibleAnalyzer
from app.config import Settings, settings
from app.errors import VideoInsightError
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo
from app.task_store import SQLiteTaskStore

# 提示词是否允许按时间戳组织大纲；workflow 当前传纯文本，保持 False（改 True 需同时传带时间戳的转写）
_INCLUDE_TIMESTAMPS_IN_PROMPT = False


def detect_platform(url: str) -> str:
    value = url.strip().lower()
    if value.startswith("upload://"):
        return "上传文件"
    if any(host in value for host in ("kuaishou.com", "chenzhongtech.com", "gifshow.com")):
        return "快手"
    if "douyin.com" in value:
        return "抖音"
    if "bilibili.com" in value or "b23.tv" in value:
        return "B站"
    if "xiaohongshu.com" in value or "xhslink" in value:
        return "小红书"
    return "未知平台"


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "未知"
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _build_metadata(
    *,
    title: str | None,
    author: str | None,
    duration: float | None,
    url: str,
) -> dict[str, str]:
    return {
        "video_title": title or "未知标题",
        "author": author or "未知作者",
        "platform": detect_platform(url),
        "duration": format_duration(duration),
    }


def _generate_output(
    llm: MockLlmAnalyzer | OpenAICompatibleAnalyzer,
    mode: AnalysisMode,
    transcript_plain: str,
    metadata: dict[str, str],
) -> str:
    if mode == AnalysisMode.transcript:
        # 文字稿模式直接返回原文，跳过 LLM 调用（省时省钱）
        return transcript_plain
    return llm.analyze(
        transcript_plain,
        mode,
        metadata=metadata,
        include_timestamps=_INCLUDE_TIMESTAMPS_IN_PROMPT,
    )


def _strip_markdown(line: str) -> str:
    """去掉行首的 Markdown 标记（标题井号、列表符号、引用）后返回。"""
    return line.strip().lstrip("#>-* ").strip()


def generate_result_title(output: str, mode: AnalysisMode, fallback: str | None = None) -> str:
    """从 AI 输出中提取一句话核心作为任务标题（对话摘要式命名）。

    优先级：
    1. 从输出中定位「一句话核心 / 核心观点 / 面试岗位」等摘要板块，取其后第一句；
    2. 兜底取输出中第一个非空标题行；
    3. 仍为空则用 fallback（媒体标题/文件名），再兜底用模式描述。
    """
    MAX_TITLE = 30
    mode_name = {
        AnalysisMode.summary: "AI 总结",
        AnalysisMode.transcript: "逐字稿",
        AnalysisMode.interview: "面经提取",
    }.get(mode, "任务")

    def clean(text: str) -> str:
        text = _strip_markdown(text).strip()
        if not text or text.startswith(("（", "(", "⚠️", "[")):
            return ""
        # 去掉常见占位提示行
        for head in ("不超过", "从文本", "提取", "按出现", "对应每个", "给出", "无"):
            if text.startswith(head):
                return ""
        text = " ".join(text.split())
        return text[:MAX_TITLE] + ("…" if len(text) > MAX_TITLE else "")

    # 1) 定位摘要板块
    markers = ("一句话核心", "核心观点", "面试岗位", "### 核心", "## 核心")
    for marker in markers:
        idx = output.find(marker)
        if idx == -1:
            continue
        tail = output[idx + len(marker):]
        for raw in tail.splitlines():
            candidate = clean(raw)
            if candidate:
                return candidate
        break

    # 2) 第一个非空标题行
    for raw in output.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") and not stripped.startswith("# 转写"):
            candidate = clean(stripped)
            if candidate:
                return candidate

    # 3) 第一句非空文本
    for raw in output.splitlines():
        candidate = clean(raw)
        if candidate:
            return candidate

    # 4) 兜底
    fallback_name = clean(fallback or "") if fallback else ""
    return fallback_name or mode_name


def run_analysis_task(task_id: str, request: AnalyzeRequest, store: SQLiteTaskStore) -> None:
    store.set_running(task_id)
    try:
        result = analyze(request, task_id, settings)
    except VideoInsightError as exc:
        store.set_failed(task_id, str(exc))
    except Exception as exc:  # Keep background task failures visible to API clients.
        store.set_failed(task_id, f"未预期错误：{exc}")
    else:
        store.set_succeeded(task_id, result)


def run_file_analysis_task(
    task_id: str,
    request: AnalyzeRequest,
    media_path: Path,
    store: SQLiteTaskStore,
) -> None:
    store.set_running(task_id)
    try:
        result = analyze_media_file(request, task_id, media_path, settings)
    except VideoInsightError as exc:
        store.set_failed(task_id, str(exc))
    except Exception as exc:
        store.set_failed(task_id, f"未预期错误：{exc}")
    else:
        store.set_succeeded(task_id, result)


def analyze(request: AnalyzeRequest, task_id: str, runtime_settings: Settings = settings) -> AnalysisResult:
    runtime_settings.ensure_dirs()
    use_mock = runtime_settings.mock_mode if request.use_mock is None else request.use_mock

    downloader = select_downloader(str(request.url), runtime_settings.tmp_dir, use_mock)
    transcriber = MockTranscriber() if use_mock else ParaformerTranscriber(runtime_settings)
    llm = MockLlmAnalyzer() if use_mock else OpenAICompatibleAnalyzer(runtime_settings)

    media = downloader.download_audio(str(request.url), task_id)
    segments = transcriber.transcribe(media.audio_path, task_id)
    transcript = format_transcript(segments, include_timestamps=True)
    transcript_plain = format_transcript(segments, include_timestamps=False)
    metadata = _build_metadata(
        title=media.title,
        author=media.author,
        duration=media.duration,
        url=str(request.url),
    )
    output = _generate_output(llm, request.mode, transcript_plain, metadata)

    media_info = MediaInfo(
        audio_path=str(media.audio_path) if media.audio_path else None,
        title=media.title,
        author=media.author,
        duration=media.duration,
        source_url=media.source_url,
    )
    result = AnalysisResult(
        mode=request.mode,
        transcript=transcript,
        segments=segments,
        output=output,
        media=media_info,
        diagnostics={"mock_mode": use_mock},
    )
    result.title = generate_result_title(output, request.mode, fallback=media_info.title)
    result_path = _write_result(runtime_settings.results_dir, task_id, result)
    result.artifacts["result_json"] = str(result_path)
    _write_result(runtime_settings.results_dir, task_id, result)
    return result


def analyze_media_file(
    request: AnalyzeRequest,
    task_id: str,
    media_path: Path,
    runtime_settings: Settings = settings,
) -> AnalysisResult:
    runtime_settings.ensure_dirs()
    use_mock = runtime_settings.mock_mode if request.use_mock is None else request.use_mock

    transcriber = MockTranscriber() if use_mock else ParaformerTranscriber(runtime_settings)
    llm = MockLlmAnalyzer() if use_mock else OpenAICompatibleAnalyzer(runtime_settings)

    segments = transcriber.transcribe(media_path, task_id)
    transcript = format_transcript(segments, include_timestamps=True)
    transcript_plain = format_transcript(segments, include_timestamps=False)
    metadata = _build_metadata(
        title=media_path.name,
        author=None,
        duration=None,
        url=str(request.url),
    )
    output = _generate_output(llm, request.mode, transcript_plain, metadata)

    media_info = MediaInfo(
        audio_path=str(media_path),
        title=media_path.name,
        author=None,
        duration=None,
        source_url=str(request.url),
    )
    result = AnalysisResult(
        mode=request.mode,
        transcript=transcript,
        segments=segments,
        output=output,
        media=media_info,
        diagnostics={"mock_mode": use_mock, "source": "upload"},
    )
    result.title = generate_result_title(output, request.mode, fallback=media_info.title)
    result_path = _write_result(runtime_settings.results_dir, task_id, result)
    result.artifacts["result_json"] = str(result_path)
    _write_result(runtime_settings.results_dir, task_id, result)
    return result


def _write_result(results_dir: Path, task_id: str, result: AnalysisResult) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{task_id}.json"
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result_path
