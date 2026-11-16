from __future__ import annotations

import hashlib
from pathlib import Path


def cookie_path(state: Path, platform: str, owner: str = "") -> Path:
    if owner.startswith("account:"):
        suffix = owner.removeprefix("account:")
        if len(suffix) != 32 or any(c not in "0123456789abcdef" for c in suffix):
            raise ValueError("Invalid account owner")
        key = hashlib.sha256(owner.encode()).hexdigest()
        return state / "account-cookies" / key / f"{platform}.txt"
    return state / "cookies" / f"{platform}.txt"
