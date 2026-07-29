from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any


MAX_PACKAGE_BYTES = 10 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
ALLOWED_SUFFIXES = {".yaml", ".yml", ".json", ".md", ".txt"}
FORBIDDEN_SUFFIXES = {
    ".py",
    ".pyc",
    ".js",
    ".mjs",
    ".cjs",
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
}


class PackValidationError(ValueError):
    pass


@dataclass(slots=True)
class ResearchPack:
    root: Path
    manifest: dict[str, Any]
    workflow: dict[str, Any]
    content_hash: str

    @property
    def pack_id(self) -> str:
        return str(self.manifest["metadata"]["id"])

    @property
    def version(self) -> str:
        return str(self.manifest["metadata"]["version"])

    @property
    def name(self) -> str:
        return str(self.manifest["metadata"].get("name", self.pack_id))

    def prompt(self, relative_path: str) -> str:
        path = (self.root / relative_path).resolve()
        if self.root.resolve() not in path.parents:
            raise PackValidationError("Prompt 路径越过研究包目录")
        return path.read_text(encoding="utf-8")


def _read_json_yaml(path: Path) -> dict[str, Any]:
    # JSON is a valid YAML subset. v0.1 deliberately accepts the safe,
    # deterministic subset while avoiding an executable YAML loader.
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackValidationError(
            f"{path.name} 必须使用 JSON 兼容的 YAML 语法；第 {exc.lineno} 行格式错误"
        ) from exc
    if not isinstance(payload, dict):
        raise PackValidationError(f"{path.name} 顶层必须是对象")
    return payload


def _hash_directory(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_pack(root: Path) -> ResearchPack:
    manifest_path = root / "manifest.yaml"
    workflow_path = root / "workflow.yaml"
    if not manifest_path.exists() or not workflow_path.exists():
        raise PackValidationError("研究包必须包含 manifest.yaml 和 workflow.yaml")
    manifest = _read_json_yaml(manifest_path)
    workflow = _read_json_yaml(workflow_path)
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise PackValidationError("manifest.metadata 缺失")
    for key in ("id", "name", "version"):
        if not metadata.get(key):
            raise PackValidationError(f"manifest.metadata.{key} 缺失")
    if manifest.get("kind") != "ResearchPack":
        raise PackValidationError("manifest.kind 必须是 ResearchPack")
    permissions = manifest.get("permissions", {})
    for denied in ("network", "filesystem", "execute_code"):
        if permissions.get(denied) is True:
            raise PackValidationError(f"研究包 v0.1 不允许权限：{denied}")
    for step in workflow.get("workflow", {}).get("steps", []):
        prompt_path = step.get("prompt")
        if prompt_path and not (root / prompt_path).is_file():
            raise PackValidationError(f"工作流引用的 Prompt 不存在：{prompt_path}")
    return ResearchPack(root, manifest, workflow, _hash_directory(root))


def builtin_pack() -> ResearchPack:
    resource = files("openthesis").joinpath(
        "resources", "research-packs", "official.long-term-fundamentals"
    )
    return load_pack(Path(str(resource)))


def install_pack(archive: Path, destination_root: Path) -> ResearchPack:
    if archive.suffix.lower() != ".othesis":
        raise PackValidationError("研究包扩展名必须是 .othesis")
    if archive.stat().st_size > MAX_PACKAGE_BYTES:
        raise PackValidationError("研究包超过 10 MB 限制")
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if not members:
            raise PackValidationError("研究包为空")
        for member in members:
            name = PurePosixPath(member.filename)
            if name.is_absolute() or ".." in name.parts:
                raise PackValidationError(f"研究包包含不安全路径：{member.filename}")
            suffix = Path(member.filename).suffix.lower()
            if member.is_dir():
                continue
            if suffix in FORBIDDEN_SUFFIXES or suffix not in ALLOWED_SUFFIXES:
                raise PackValidationError(f"研究包包含禁止文件：{member.filename}")
            if member.file_size > MAX_MEMBER_BYTES:
                raise PackValidationError(f"研究包单个文件过大：{member.filename}")

        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        staging = destination_root / ".staging" / archive_hash
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        package.extractall(staging)

    roots = [staging]
    child_dirs = [item for item in staging.iterdir() if item.is_dir()]
    if not (staging / "manifest.yaml").exists() and len(child_dirs) == 1:
        roots = child_dirs
    candidate = load_pack(roots[0])
    target = destination_root / candidate.pack_id / candidate.version
    if target.exists():
        existing = load_pack(target)
        if existing.content_hash != candidate.content_hash:
            raise PackValidationError(
                "相同 ID 和版本的研究包已经存在，但内容哈希不同；请修改版本号"
            )
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(candidate.root, target)
    return load_pack(target)


def list_installed_packs(destination_root: Path) -> list[ResearchPack]:
    packs = [builtin_pack()]
    if destination_root.exists():
        for manifest in destination_root.glob("*/*/manifest.yaml"):
            try:
                packs.append(load_pack(manifest.parent))
            except PackValidationError:
                continue
    return packs

