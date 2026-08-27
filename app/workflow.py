from __future__ import annotations

import json
from pathlib import Path

from app.adapters.asr import MockTranscriber, ParaformerTranscriber, format_transcript
from app.adapters.downloader import select_downloader
from app.adapters.llm import MockLlmAnalyzer, OpenAICompatibleAnalyzer
from app.config import Settings, settings
from app.errors import VideoInsightError
from app.schemas import AnalysisResult, AnalyzeRequest, MediaInfo
from app.task_store import SQLiteTaskStore


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
    output = llm.analyze(transcript_plain, request.mode)

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
    output = llm.analyze(transcript_plain, request.mode)

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
