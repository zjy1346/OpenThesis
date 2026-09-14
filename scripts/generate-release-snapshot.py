"""Create a deterministic, compressed manifest of Windows release build inputs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


TREES = ("src", "desktop", "build_support", "scripts", ".build-tools", ".cargo")
ROOT_FILES = {"README.md", "LICENSE"}
ROOT_SUFFIXES = {".py", ".spec", ".toml", ".txt", ".json", ".lock", ".yml", ".yaml"}
EXCLUDED = (
    "desktop/dist",
    "desktop/src-tauri/target",
    "desktop/src-tauri/gen",
    "desktop/src-tauri/resources/bin",
    "desktop/node_modules/.vite",
    "desktop/node_modules/.cache",
)


def included(name: str) -> bool:
    path = Path(name)
    parts = path.parts
    selected = (
        bool(parts)
        and (
            parts[0] in TREES
            or name in ROOT_FILES
            or (len(parts) == 1 and path.suffix in ROOT_SUFFIXES)
        )
    )
    return selected and not (
        "__pycache__" in parts
        or ".git" in parts
        or name.endswith((".pyc", ".pyo"))
        or any(name == prefix or name.startswith(prefix + "/") for prefix in EXCLUDED)
    )


def entry(path: Path, name: str) -> dict[str, object]:
    info = path.lstat()
    link = path.is_symlink()
    return {
        "path": name,
        "type": "symlink" if link else "file",
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "symlink_target": os.readlink(path) if link else None,
        "bytes": info.st_size,
        "sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
    }


def tracked_deletions(root: Path) -> list[dict[str, object]]:
    raw_entries = subprocess.check_output(
        ["git", "ls-files", "-s", "-z"], cwd=root
    ).split(b"\0")
    rows: list[dict[str, object]] = []
    for raw in raw_entries:
        if not raw:
            continue
        metadata, encoded_name = raw.split(b"\t", 1)
        mode = metadata.decode("ascii").split()[0]
        name = encoded_name.decode("utf-8")
        if included(name) and not (root / name).exists():
            rows.append({
                "path": name,
                "type": "deleted",
                "mode": mode,
                "symlink_target": None,
                "bytes": 0,
                "sha256": None,
            })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("workflow")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    rows: list[dict[str, object]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_file() and included(path.name):
            rows.append(entry(path, path.name))
    for tree in TREES:
        base = root / tree
        if not base.exists():
            continue
        for current, dirs, files in os.walk(base):
            current_path = Path(current)
            dirs[:] = sorted(
                directory
                for directory in dirs
                if included((current_path / directory).relative_to(root).as_posix())
            )
            for filename in sorted(files):
                path = current_path / filename
                relative = path.relative_to(root).as_posix()
                if included(relative):
                    rows.append(entry(path, relative))
    rows.extend(tracked_deletions(root))
    rows.sort(key=lambda row: str(row["path"]))
    target = root / "docs" / "releases" / f"{args.workflow}-{args.version}-inputs.jsonl.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as raw_target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_target, mtime=0) as archive:
            for row in rows:
                archive.write(
                    (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
                )
    result = {
        "manifest": str(target),
        "entries": len(rows),
        "sha256": hashlib.file_digest(target.open("rb"), "sha256").hexdigest(),
        "base": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "trees": TREES,
        "root_files": sorted(ROOT_FILES),
        "root_suffixes": sorted(ROOT_SUFFIXES),
        "exclusions": EXCLUDED,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
