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
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY") or None
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    ytdlp_cookies_file: Path | None = (
        Path(os.environ["YTDLP_COOKIES_FILE"]) if os.getenv("YTDLP_COOKIES_FILE") else None
    )
    ytdlp_cookies_from_browser: str | None = os.getenv("YTDLP_COOKIES_FROM_BROWSER") or None

    def ensure_dirs(self) -> None:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
