from __future__ import annotations

from pathlib import Path

from app.config import Settings


def test_settings_support_browser_cookies() -> None:
    settings = Settings(
        mock_mode=True,
        tmp_dir=Path("tmp"),
        results_dir=Path("results"),
        media_dir=Path("media"),
        ytdlp_cookies_from_browser="chrome",
    )

    assert settings.ytdlp_cookies_from_browser == "chrome"
