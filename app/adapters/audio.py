from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.errors import WorkflowError


# 转码超时上限。没有它，损坏的源文件会让 ffmpeg 永久挂在某个线程池工人上
# ——而长任务与所有同步端点共用同一个池，服务会逐渐失去响应。
FFMPEG_TIMEOUT_SECONDS = 1800


def resolve_ffmpeg() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise WorkflowError("未找到 ffmpeg，请安装系统 ffmpeg 或运行：pip install imageio-ffmpeg") from exc

    return imageio_ffmpeg.get_ffmpeg_exe()


def resolve_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def audio_duration(input_audio: Path) -> float | None:
    """用 ffprobe 取音频时长（秒）；无 ffprobe 或探测失败返回 None。

    None 表示「无法判定是否长音频」，调用方据此回退到整文件转码后测量的旧路径。
    """
    exe = resolve_ffprobe()
    if exe is None:
        return None
    try:
        completed = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(input_audio),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def run_ffmpeg_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """运行 ffmpeg，超时则中止。不删除任何产物（分片场景有多个输出文件）。

    与 _run_ffmpeg 的区别：_run_ffmpeg 在超时后删掉单个输出 WAV；这里把清理交给
    调用方，因为分段场景会产出多个文件。
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError(
            f"ffmpeg 处理超过 {FFMPEG_TIMEOUT_SECONDS // 60} 分钟仍未完成，已中止。"
        ) from exc


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
        completed = _run_ffmpeg(cmd, output_wav)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WorkflowError(f"音频格式转换失败：{detail}")
        return output_wav


def _run_ffmpeg(cmd: list[str], output_wav: Path) -> subprocess.CompletedProcess[str]:
    """执行 ffmpeg；超时则中止并清掉半成品 WAV。

    半成品必须删掉：它的 WAV 头是合法的，`_wav_duration` 会读出一个偏短的
    时长并继续送进 ASR，最终**静默产出一份不完整的转写稿**——比直接报错更糟。
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        try:
            output_wav.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkflowError(
            f"ffmpeg 转码超过 {FFMPEG_TIMEOUT_SECONDS // 60} 分钟仍未完成，已中止。"
            "请确认源文件未损坏且可正常播放，或先裁剪成更短的片段再上传。"
        ) from exc
