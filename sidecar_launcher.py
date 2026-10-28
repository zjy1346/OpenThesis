"""PyInstaller entry point for the headless desktop research core."""

if __name__ == "__main__":
    # Frozen workers must dispatch before importing business services or
    # argparse. Otherwise every PDF child initializes the desktop RPC stack.
    import multiprocessing

    multiprocessing.freeze_support()
    from openthesis.sidecar import main

    raise SystemExit(main())
