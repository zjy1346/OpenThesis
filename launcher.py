"""PyInstaller entry point that preserves the openthesis package context."""

import tempfile
import traceback
from pathlib import Path

from openthesis.__main__ import entrypoint


if __name__ == "__main__":
    try:
        entrypoint()
    except Exception:
        crash_path = Path(tempfile.gettempdir()) / "OpenThesis-crash.log"
        crash_path.write_text(traceback.format_exc(), encoding="utf-8")
        # Windowed builds do not have stderr. The log gives release automation
        # and users a stable diagnostic location.
        raise
