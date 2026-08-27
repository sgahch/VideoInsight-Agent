from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.errors import WorkflowError


def resolve_ffmpeg() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise WorkflowError("未找到 ffmpeg，请安装系统 ffmpeg 或运行：pip install imageio-ffmpeg") from exc

    return imageio_ffmpeg.get_ffmpeg_exe()


class FfmpegAudioPreprocessor:
    def convert_to_wav16k(self, input_audio: Path, output_wav: Path) -> Path:
        ffmpeg = resolve_ffmpeg()
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg,
            "-i",
            str(input_audio),
            "-c:a",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-y",
            str(output_wav),
        ]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WorkflowError(f"音频格式转换失败：{detail}")
        return output_wav
