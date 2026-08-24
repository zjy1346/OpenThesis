from __future__ import annotations

import os


def entrypoint() -> int:
    if os.environ.get("OPENTHESIS_SMOKE_TEST") == "1":
        from .smoke import run_smoke_test

        run_smoke_test()
        return 0

    # OpenThesis 2.0 ships one user interface: the Tauri desktop application.
    # The Python module is the headless JSON-RPC sidecar, not a second Tk UI.
    from .sidecar import main as sidecar_main

    return sidecar_main()


if __name__ == "__main__":
    raise SystemExit(entrypoint())
