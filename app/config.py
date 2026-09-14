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
        os.getenv("DASHSCOPE_ASR_CHUNK_THRESHOLD_SECONDS", "900")
    )
    dashscope_asr_chunk_seconds: int = int(os.getenv("DASHSCOPE_ASR_CHUNK_SECONDS", "600"))
    dashscope_asr_max_workers: int = int(os.getenv("DASHSCOPE_ASR_MAX_WORKERS", "3"))
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

    def ensure_dirs(self) -> None:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
