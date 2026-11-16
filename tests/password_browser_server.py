"""Local-only browser fixtures: isolated storage, synthetic accounts, and no real SMTP."""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "output" / "password-browser" / uuid.uuid4().hex
PASSWORD = "browser-admin-password"
salt = "browser-fixture-salt"
digest = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt.encode(), 600_000).hex()
os.environ.update({
    "VIDEOINSIGHT_MOCK_MODE": "true",
    "VIDEOINSIGHT_AUTH_TOKEN": "",
    "VIDEOINSIGHT_AUTH_USERNAME": "browser-admin",
    "VIDEOINSIGHT_AUTH_PASSWORD_HASH": f"pbkdf2_sha256$600000${salt}${digest}",
    "VIDEOINSIGHT_SESSION_SECRET": uuid.uuid4().hex,
    "VIDEOINSIGHT_REGISTRATION_ENABLED": "true",
    "VIDEOINSIGHT_SMTP_USERNAME": "fixture@example.invalid",
    "VIDEOINSIGHT_SMTP_PASSWORD": "synthetic-smtp-not-used",
    "VIDEOINSIGHT_DB_PATH": str(DATA / "videoinsight.db"),
    "VIDEOINSIGHT_TMP_DIR": str(DATA / "tmp"),
    "VIDEOINSIGHT_RESULTS_DIR": str(DATA / "results"),
    "VIDEOINSIGHT_MEDIA_DIR": str(DATA / "media"),
    "VIDEOINSIGHT_CLEANUP_INTERVAL_HOURS": "0",
    "LLM_PROVIDER": "mock",
    "COOKIE_QR_BROWSER_URL": "",
})

import app.account_auth as accounts
from app.main import app
import uvicorn


accounts.secrets.randbelow = lambda _: 123456
accounts.send_verification_email = lambda *args: None
accounts.send_password_email = lambda *args: None

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8003)
