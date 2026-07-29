from __future__ import annotations

import os


def entrypoint() -> None:
    if os.environ.get("OPENTHESIS_SMOKE_TEST") == "1":
        from .smoke import run_smoke_test

        run_smoke_test()
        return
    from .app import main

    main()


if __name__ == "__main__":
    entrypoint()

