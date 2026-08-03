from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from pathlib import Path


def default_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform_system: str | None = None,
) -> Path:
    """Return the platform-native OpenThesis data directory.

    Dependency parameters make the platform adapter deterministic in tests while
    keeping the zero-argument interface convenient for application callers.
    """

    values = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    system = platform.system() if platform_system is None else platform_system

    override = values.get("OPENTHESIS_DATA_DIR", "").strip()
    if override:
        return Path(override)

    if system == "Windows":
        local_app_data = values.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "OpenThesis"
        return user_home / "AppData" / "Local" / "OpenThesis"

    if system == "Darwin":
        return user_home / "Library" / "Application Support" / "OpenThesis"

    xdg_data_home = values.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home) / "OpenThesis"
    return user_home / ".local" / "share" / "OpenThesis"
