"""快手真视频验证：移动端页面解析直链 -> 下载 mp4 -> ASR -> LLM 全流程。

用法:
    python verify_kuaishou_video.py <v.kuaishou.com短链或photoId>
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import settings
from app.errors import VideoInsightError
from app.schemas import AnalysisMode, AnalysisResult, AnalyzeRequest, MediaInfo

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def resolve_photo_id(url: str) -> str:
    if re.fullmatch(r"[0-9a-zA-Z]{10,}", url.strip()):
        return url.strip()
    resp = httpx.get(url, follow_redirects=True, timeout=20, headers={"User-Agent": UA_MOBILE}, verify=False)
    m = re.search(r"photoId=([0-9a-zA-Z]+)", str(resp.url))
    if m:
        return m.group(1)
    raise RuntimeError(f"无法从 {url} 解析 photoId，最终 URL: {resp.url}")


def extract_mp4_url(photo_id: str) -> str:
    page = httpx.get(
        f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}",
        timeout=25,
        headers={"User-Agent": UA_MOBILE},
        verify=False,
    )
    if page.status_code != 200:
        raise RuntimeError(f"移动端页面返回 {page.status_code}")
    m = re.search(r'"mainMvUrls"\s*:\s*(\[[^\]]+\])', page.text)
    if not m:
        raise RuntimeError("页面中未找到 mainMvUrls（可能是图文帖或无视频）")
    urls = json.loads(m.group(1).encode().decode("unicode_escape"))
    candidates = []
    for item in urls:
        u = item.get("url")
        if u:
            candidates.append(u)
    if not candidates:
        raise RuntimeError("mainMvUrls 中没有 url 字段")
    return candidates[0]


def download_mp4(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream(
        "GET", url, timeout=120, headers={"User-Agent": UA_MOBILE}, verify=False, follow_redirects=True
    ) as resp:
        resp.raise_for_status()
        with target.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 16):
                f.write(chunk)
    return target


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://v.kuaishou.com/nA6MvfL1"
    task_id = uuid4().hex
    phases: dict[str, float] = {}
    print(f"目标: {url}")

    try:
        from app.adapters.asr import ParaformerTranscriber, format_transcript
        from app.adapters.llm import OpenAICompatibleAnalyzer

        t0 = time.time()
        photo_id = resolve_photo_id(url)
        phases["resolve"] = round(time.time() - t0, 2)
        print(f"[resolve] photoId={photo_id} {phases['resolve']}s")

        t0 = time.time()
        mp4_url = extract_mp4_url(photo_id)
        phases["probe"] = round(time.time() - t0, 2)
        print(f"[probe  ] 直链: {mp4_url[:120]}... {phases['probe']}s")

        t0 = time.time()
        task_dir = settings.tmp_dir / task_id
        mp4_path = download_mp4(mp4_url, task_dir / f"kuaishou-{photo_id}.mp4")
        phases["download"] = round(time.time() - t0, 2)
        size_mb = round(mp4_path.stat().st_size / 1e6, 2)
        print(f"[download] {size_mb}MB -> {mp4_path} {phases['download']}s")

        t0 = time.time()
        transcriber = ParaformerTranscriber(settings)
        segments = transcriber.transcribe(mp4_path, task_id)
        phases["asr"] = round(time.time() - t0, 2)
        transcript = format_transcript(segments, include_timestamps=True)
        transcript_plain = format_transcript(segments, include_timestamps=False)
        print(f"[asr    ] {len(transcript_plain)} 字 / {len(segments)} 段 {phases['asr']}s")
        print("          转写头:", transcript_plain[:150].replace("\n", " | "))

        t0 = time.time()
        llm = OpenAICompatibleAnalyzer(settings)
        output = llm.analyze(transcript_plain, AnalysisMode.summary)
        phases["llm"] = round(time.time() - t0, 2)
        print(f"[llm    ] {len(output)} 字 {phases['llm']}s")
        print("          总结头:", output[:200].replace("\n", " || "))

        result = AnalysisResult(
            mode=AnalysisMode.summary,
            transcript=transcript,
            segments=segments,
            output=output,
            media=MediaInfo(
                audio_path=str(mp4_path),
                title=f"快手视频 {photo_id}",
                author=None,
                duration=round(segments[-1].end, 2) if segments else None,
                source_url=url,
            ),
            diagnostics={"mock_mode": False, "source": "verify-kuaishou", "photo_id": photo_id},
        )
        result_path = settings.results_dir / f"{task_id}.json"
        result_path.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[OK] 总耗时 {round(sum(phases.values()), 2)}s | 结果: {result_path}")
    except (VideoInsightError, RuntimeError, httpx.HTTPError) as exc:
        print(f"\n[FAIL] {type(exc).__name__}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
