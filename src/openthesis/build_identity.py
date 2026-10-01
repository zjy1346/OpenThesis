from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_build_info() -> dict[str, str]:
    path = Path(__file__).resolve().parent / "resources" / "build-info.json"
    try:
        # ``utf-8-sig`` accepts both canonical UTF-8 and older Windows
        # PowerShell output carrying a BOM.  Release packaging writes new
        # identities without a BOM, while this keeps previously staged builds
        # from degrading to an unsafe ``unknown`` identity.
        raw: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "version": str(raw.get("version", "unknown")),
        "commit": str(raw.get("commit", "unknown")),
        "build_time_utc": str(raw.get("build_time_utc", "unknown")),
        "contract_version": str(raw.get("contract_version", "unknown")),
        "build_id": str(raw.get("build_id", "unknown")),
    }
