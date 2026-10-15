"""Read-only Online Backup for a single diagnostic research replay.

The source is never opened through Storage (no migration/initialization), and
an absent source/run is an error. No candidate text or settings are printed.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("run_id")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    target = args.target.resolve()
    if target.exists() or source == target:
        raise ValueError("diagnostic target must be new")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10) as original:
        original.execute("PRAGMA query_only=ON")
        if original.execute("SELECT 1 FROM research_runs WHERE run_id=?", (args.run_id,)).fetchone() is None:
            raise ValueError("requested run is absent; source remains unchanged")
        with sqlite3.connect(target) as saved:
            original.backup(saved, pages=128, sleep=0.05)
            if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("diagnostic backup integrity failed")
    print("backup=" + str(target))
    with target.open("rb") as stream:
        print("sha256=" + hashlib.file_digest(stream, "sha256").hexdigest())


if __name__ == "__main__":
    main()
