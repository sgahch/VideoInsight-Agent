from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    mock_mode: bool = _bool_env("VIDEOINSIGHT_MOCK_MODE", True)
    tmp_dir: Path = Path(os.getenv("VIDEOINSIGHT_TMP_DIR", "data/tmp"))
    results_dir: Path = Path(os.getenv("VIDEOINSIGHT_RESULTS_DIR", "data/results"))
    media_dir: Path = Path(os.getenv("VIDEOINSIGHT_MEDIA_DIR", "media"))
    db_path: Path = Path(os.getenv("VIDEOINSIGHT_DB_PATH", "data/videoinsight.db"))
    dashscope_api_key: str | None = os.getenv("DASHSCOPE_API_KEY") or None
    dashscope_workspace_id: str | None = os.getenv("DASHSCOPE_WORKSPACE_ID") or None
    dashscope_asr_model: str = os.getenv("DASHSCOPE_ASR_MODEL", "paraformer-realtime-v2")
    dashscope_asr_chunk_threshold_seconds: int = int(
        # 短音频也走并发分片，避免一次 ASR 请求的固定排队/上传开销
        # 把 2~3 分钟视频的实时倍速压到 2x 左右。
        os.getenv("DASHSCOPE_ASR_CHUNK_THRESHOLD_SECONDS", "120")
    )
    dashscope_asr_chunk_seconds: int = int(os.getenv("DASHSCOPE_ASR_CHUNK_SECONDS", "120"))
    dashscope_asr_max_workers: int = int(os.getenv("DASHSCOPE_ASR_MAX_WORKERS", "8"))
    # 抖音「转写」下载时请求的画质档位。转写只需要音轨，视频画质高低不影响识别
    # 质量，但直接影响下载体积与耗时——默认取低档（480p）可大幅缩短总耗时。
    # 设为 1080p 即恢复原行为；「下载源视频」功能不受此影响，恒用 1080p。
    douyin_audio_ratio: str = os.getenv("DOUYIN_AUDIO_RATIO", "480p")
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY") or None
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    ytdlp_cookies_file: Path | None = (
        Path(os.environ["YTDLP_COOKIES_FILE"]) if os.getenv("YTDLP_COOKIES_FILE") else None
    )
    ytdlp_cookies_from_browser: str | None = os.getenv("YTDLP_COOKIES_FROM_BROWSER") or None
    # ---- MediaCrawler 评论爬虫对接（可选）----
    # 方式一：指向 MediaCrawler 服务地址（其 api 在 :8080），用于按接口拉评论（端点待核实，默认走方式二）。
    media_crawler_api_url: str | None = os.getenv("MEDIA_CRAWLER_API_URL") or None
    # 方式二（推荐）：指向 MediaCrawler 的 data/ 输出目录，本项目直接读取它落盘的评论文件。
    media_crawler_output_dir: Path | None = (
        Path(os.environ["MEDIA_CRAWLER_OUTPUT_DIR"]) if os.getenv("MEDIA_CRAWLER_OUTPUT_DIR") else None
    )
    # ---- 上传与产物回收 ----
    # 单个上传文件的大小上限。必须有限制：上传端点无鉴权，没有上限时一个请求
    # 就能写满磁盘。默认 2048 MB，足够放下长视频。
    max_upload_mb: int = int(os.getenv("VIDEOINSIGHT_MAX_UPLOAD_MB", "2048"))
    # 隔离产物的回收周期；<=0 关闭周期性回收（启动时的一次性回收仍会执行）。
    cleanup_interval_hours: int = int(os.getenv("VIDEOINSIGHT_CLEANUP_INTERVAL_HOURS", "6"))
    # 同时执行的长任务上限。刻意有界：一个视频任务动辄十几分钟，不限并发会
    # 打满带宽/磁盘，也会撞 ASR 与 LLM 的配额。
    max_concurrent_tasks: int = int(os.getenv("VIDEOINSIGHT_MAX_CONCURRENT_TASKS", "2"))
    # 共享访问令牌。未设置（默认）时不做任何鉴权校验，本地行为与之前一致；
    # 设置后除 /、/static、/health、/auth/session 外的所有请求都必须带令牌。
    auth_token: str | None = os.getenv("VIDEOINSIGHT_AUTH_TOKEN") or None
    # 任务记录的保留时长；<=0（默认）表示不按时间淘汰历史，只回收孤儿目录。
    # 刻意默认关闭：静默删除用户的历史记录比占点磁盘更糟。
    task_retention_hours: int = int(os.getenv("VIDEOINSIGHT_TASK_RETENTION_HOURS", "0"))

    def ensure_dirs(self) -> None:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
