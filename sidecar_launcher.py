"""PyInstaller entry point for the headless desktop research core."""

from openthesis.sidecar import main


if __name__ == "__main__":
    raise SystemExit(main())
