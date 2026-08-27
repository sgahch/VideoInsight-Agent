"""真实链路验证脚本：逐个跑通各平台视频链接，记录每个阶段耗时与结果。

与生产 workflow 使用完全相同的适配器类（YtDlpDownloader / ParaformerTranscriber /
OpenAICompatibleAnalyzer），逐阶段计时，失败时记录错误并继续下一个样本。

用法:
    python verify_real_chain.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.errors import VideoInsightError
from app.schemas import AnalysisMode, AnalyzeRequest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SAMPLES = [
    ("快手-语录情感", "https://v.kuaishou.com/JExSMSsx", AnalysisMode.summary),
    ("小红书-文字帖", "https://xhslink.cn/o/6990bRO5ehG", AnalysisMode.summary),
    ("抖音-渣男五招", "https://v.douyin.com/lmwHu2zoSII/", AnalysisMode.summary),
    ("B站-哪里都是你", "https://b23.tv/OEG7G4m", AnalysisMode.summary),
]

REPORT_DIR = Path("data/verify")
RESULT_DIR = Path(settings.results_dir)


def run_sample(label: str, url: str, mode: AnalysisMode) -> dict:
    task_id = uuid4().hex
    entry: dict = {"label": label, "url": url, "task_id": task_id, "ok": False}
    phases: dict[str, float] = {}

    from app.adapters.asr import ParaformerTranscriber, format_transcript
    from app.adapters.downloader import select_downloader
    from app.adapters.llm import OpenAICompatibleAnalyzer

    try:
        # 阶段 1: 下载音频
        t0 = time.time()
        downloader = select_downloader(url, settings.tmp_dir, use_mock=False)
        media = downloader.download_audio(url, task_id)
        phases["download"] = round(time.time() - t0, 2)
        entry["media"] = {
            "title": media.title,
            "author": media.author,
            "duration": media.duration,
            "audio_path": str(media.audio_path) if media.audio_path else None,
        }

        # 阶段 2: ASR 转写
        t0 = time.time()
        transcriber = ParaformerTranscriber(settings)
        segments = transcriber.transcribe(media.audio_path, task_id)
        phases["asr"] = round(time.time() - t0, 2)
        transcript = format_transcript(segments, include_timestamps=True)
        transcript_plain = format_transcript(segments, include_timestamps=False)
        entry["transcript"] = {"chars": len(transcript_plain), "segments": len(segments), "head": transcript[:200]}

        # 阶段 3: LLM 总结
        t0 = time.time()
        llm = OpenAICompatibleAnalyzer(settings)
        output = llm.analyze(transcript_plain, mode)
        phases["llm"] = round(time.time() - t0, 2)
        entry["output"] = {"chars": len(output), "head": output[:300]}

        entry["ok"] = True
        entry["phases"] = phases
        entry["total"] = round(sum(phases.values()), 2)

        # 与生产一致的落盘
        from app.schemas import AnalysisResult, MediaInfo

        result = AnalysisResult(
            mode=mode,
            transcript=transcript,
            segments=segments,
            output=output,
            media=MediaInfo(
                audio_path=str(media.audio_path) if media.audio_path else None,
                title=media.title,
                author=media.author,
                duration=media.duration,
                source_url=url,
            ),
            diagnostics={"mock_mode": False, "source": "verify-real-chain"},
        )
        result_path = RESULT_DIR / f"{task_id}.json"
        result_path.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        entry["result_json"] = str(result_path)
    except VideoInsightError as exc:
        entry["error"] = f"VideoInsightError: {exc}"
        entry["phases"] = phases
    except Exception as exc:  # noqa: BLE001 - 验证脚本需要记录所有失败
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["phases"] = phases

    print(f"[{'OK' if entry['ok'] else 'FAIL'}] {label}  total={entry.get('total', 'n/a')}s  phases={entry.get('phases')}")
    if not entry["ok"]:
        print(f"        error: {entry['error']}")
    return entry


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    entries = [run_sample(label, url, mode) for label, url, mode in SAMPLES]
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_elapsed": round(time.time() - started, 2),
        "success": sum(1 for e in entries if e["ok"]),
        "total": len(entries),
        "samples": entries,
    }
    report_path = REPORT_DIR / "real_chain_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n==================== 汇总 ====================")
    print(f"成功 {report['success']}/{report['total']}，总耗时 {report['total_elapsed']}s")
    for e in entries:
        status = "OK  " if e["ok"] else "FAIL"
        detail = f"下载{e['phases'].get('download', 'n/a')}s 转写{e['phases'].get('asr', 'n/a')}s LLM{e['phases'].get('llm', 'n/a')}s"
        print(f"  [{status}] {e['label']}: {detail}")
        if not e["ok"]:
            print(f"          {e['error']}")
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
