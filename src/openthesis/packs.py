from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .ot import (
    MAX_ARCHIVE_BYTES,
    OtPackage,
    OtValidationError,
    read_ot,
)

MAX_PACKAGE_BYTES = MAX_ARCHIVE_BYTES
PackValidationError = OtValidationError


@dataclass(frozen=True, slots=True)
class ResearchPack:
    package: OtPackage
    workflow: dict[str, Any]

    @property
    def manifest(self) -> dict[str, Any]:
        return self.package.manifest

    @property
    def pack_id(self) -> str:
        return self.package.package_id

    @property
    def version(self) -> str:
        return self.package.version

    @property
    def name(self) -> str:
        return str(self.package.manifest["package"].get("name", self.pack_id))

    @property
    def content_hash(self) -> str:
        return self.package.content_identity

    def prompt(self, relative_path: str) -> str:
        candidates = [relative_path]
        if not relative_path.startswith("resources/"):
            candidates.append(f"resources/{relative_path}")
        for candidate in candidates:
            try:
                return self.package.read_text(candidate)
            except OtValidationError:
                continue
        raise PackValidationError([])


def _as_research_pack(package: OtPackage) -> ResearchPack:
    if package.kind != "openthesis.research-pack":
        raise PackValidationError([])
    try:
        workflow = package.read_text("resources/workflow.json")
        import json

        payload = json.loads(workflow)
    except (OtValidationError, ValueError) as exc:
        raise PackValidationError([]) from exc
    if not isinstance(payload, dict):
        raise PackValidationError([])
    return ResearchPack(package, payload)


def load_pack(path: Path) -> ResearchPack:
    if not path.is_file():
        raise PackValidationError([])
    return _as_research_pack(read_ot(path, for_execution=True))


def builtin_pack() -> ResearchPack:
    resource = files("openthesis").joinpath(
        "resources",
        "ot-packages",
        "official.long-term-fundamentals.ot",
    )
    return load_pack(Path(str(resource)))


def install_pack(archive: Path, destination_root: Path) -> ResearchPack:
    candidate = load_pack(archive)
    package_dir = destination_root / candidate.pack_id / candidate.version
    target = package_dir / f"{candidate.content_hash}.ot"
    package_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(package_dir.glob("*.ot"))
    for installed_path in existing:
        installed = load_pack(installed_path)
        if installed.content_hash == candidate.content_hash:
            return installed
        raise PackValidationError([])

    with tempfile.NamedTemporaryFile(
        prefix="ot-install-",
        suffix=".tmp",
        dir=package_dir,
        delete=False,
    ) as handle:
        staging = Path(handle.name)
    try:
        shutil.copyfile(archive, staging)
        with staging.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return load_pack(target)


def list_installed_packs(destination_root: Path) -> list[ResearchPack]:
    packs = [builtin_pack()]
    if destination_root.exists():
        for package_path in sorted(destination_root.glob("*/*/*.ot")):
            try:
                candidate = load_pack(package_path)
            except PackValidationError:
                continue
            if not any(
                item.pack_id == candidate.pack_id and item.version == candidate.version
                for item in packs
            ):
                packs.append(candidate)
    return packs


def archive_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
