from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

OT_VERSION = "1.0"
OT_MANIFEST = "manifest.json"
OT_LOCKFILE = "ot.lock.json"
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_RESOURCE_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 256
MAX_COMPRESSION_RATIO = 100
KNOWN_REQUIRED_CAPABILITIES = {
    "openthesis.workflow.v1",
    "openthesis.deterministic-transform.v1",
    "openthesis.output-schema.v1",
}
ALLOWED_MEDIA_TYPES = {
    "application/json",
    "application/jsonl",
    "text/markdown",
    "text/plain",
    "text/csv",
}
FORBIDDEN_SUFFIXES = {
    ".py", ".pyc", ".pyo", ".js", ".mjs", ".cjs", ".exe", ".dll", ".so",
    ".dylib", ".bat", ".cmd", ".ps1", ".sh", ".com", ".scr", ".msi", ".jar",
    ".zip", ".othesis",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password)\b\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
)


class OtValidationError(ValueError):
    def __init__(self, diagnostics: Iterable["OtDiagnostic"]):
        self.diagnostics = tuple(diagnostics)
        message = "; ".join(f"{item.code}: {item.message}" for item in self.diagnostics)
        super().__init__(message or "OT validation failed")


@dataclass(frozen=True, slots=True)
class OtDiagnostic:
    code: str
    severity: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class OtPackage:
    manifest: dict[str, Any]
    resources: Mapping[str, bytes]
    content_identity: str
    diagnostics: tuple[OtDiagnostic, ...] = ()

    @property
    def package_id(self) -> str:
        return str(self.manifest["package"]["id"])

    @property
    def version(self) -> str:
        return str(self.manifest["package"]["version"])

    @property
    def kind(self) -> str:
        return str(self.manifest["package"]["kind"])

    def read_bytes(self, path: str) -> bytes:
        normalized = _safe_member_name(path)
        try:
            return self.resources[normalized]
        except KeyError as exc:
            raise OtValidationError(
                [OtDiagnostic("OT_RESOURCE_MISSING", "error", path, "资源不存在")]
            ) from exc

    def read_text(self, path: str) -> str:
        try:
            return self.read_bytes(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OtValidationError(
                [OtDiagnostic("OT_RESOURCE_ENCODING", "error", path, "文本资源必须使用 UTF-8")]
            ) from exc


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_member_name(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise OtValidationError(
            [OtDiagnostic("OT_PATH_INVALID", "error", value, "资源路径无效")]
        )
    normalized_unicode = unicodedata.normalize("NFC", value)
    if normalized_unicode != value:
        raise OtValidationError(
            [OtDiagnostic("OT_PATH_UNICODE", "error", value, "资源路径必须使用 Unicode NFC")]
        )
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or ":" in value:
        raise OtValidationError(
            [OtDiagnostic("OT_PATH_TRAVERSAL", "error", value, "资源路径必须是安全的相对路径")]
        )
    return path.as_posix()


def _contains_secret(value: bytes) -> bool:
    if len(value) > MAX_RESOURCE_BYTES:
        return False
    text = value.decode("utf-8", errors="ignore")
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return (unix_mode & 0o170000) == 0o120000


def _validate_manifest_shape(manifest: Any) -> list[OtDiagnostic]:
    diagnostics: list[OtDiagnostic] = []
    if not isinstance(manifest, dict):
        return [OtDiagnostic("OT_MANIFEST_TYPE", "error", OT_MANIFEST, "Manifest 顶层必须是对象")]
    if manifest.get("ot_version") != OT_VERSION:
        diagnostics.append(
            OtDiagnostic("OT_VERSION_UNSUPPORTED", "error", "ot_version", f"仅支持 OT {OT_VERSION}")
        )
    package = manifest.get("package")
    if not isinstance(package, dict):
        diagnostics.append(OtDiagnostic("OT_PACKAGE_MISSING", "error", "package", "缺少包元数据"))
    else:
        for field in ("id", "name", "version", "kind"):
            value = package.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                diagnostics.append(
                    OtDiagnostic("OT_PACKAGE_FIELD", "error", f"package.{field}", "包字段缺失或无效")
                )
        package_id = package.get("id")
        if isinstance(package_id, str) and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", package_id):
            diagnostics.append(
                OtDiagnostic("OT_PACKAGE_ID", "error", "package.id", "包 ID 必须是稳定的小写标识符")
            )
    permissions = manifest.get("permissions")
    if not isinstance(permissions, dict):
        diagnostics.append(OtDiagnostic("OT_PERMISSIONS_MISSING", "error", "permissions", "必须声明权限"))
    else:
        if permissions.get("execute_code") is not False:
            diagnostics.append(OtDiagnostic("OT_CODE_FORBIDDEN", "error", "permissions.execute_code", ".ot 禁止执行任意代码"))
        if permissions.get("secrets") != "prohibited":
            diagnostics.append(OtDiagnostic("OT_SECRETS_POLICY", "error", "permissions.secrets", ".ot 必须禁止秘密"))
        if permissions.get("filesystem") != "none":
            diagnostics.append(OtDiagnostic("OT_FILESYSTEM_FORBIDDEN", "error", "permissions.filesystem", "OT v1 不授予文件系统权限"))
        network = permissions.get("network", [])
        if network not in ([], None):
            diagnostics.append(OtDiagnostic("OT_NETWORK_FORBIDDEN", "error", "permissions.network", "OT v1 默认不授予网络权限"))
    resources = manifest.get("resources")
    if not isinstance(resources, list) or not resources:
        diagnostics.append(OtDiagnostic("OT_RESOURCES_MISSING", "error", "resources", "至少需要一个资源"))
    required = manifest.get("required_capabilities", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        diagnostics.append(OtDiagnostic("OT_CAPABILITY_TYPE", "error", "required_capabilities", "能力声明必须是字符串数组"))
    return diagnostics


def _validate_lockfile(lockfile: Any, manifest: Mapping[str, Any]) -> list[OtDiagnostic]:
    diagnostics: list[OtDiagnostic] = []
    if not isinstance(lockfile, dict):
        return [OtDiagnostic("OT_LOCKFILE_TYPE", "error", OT_LOCKFILE, "ot.lock.json 顶层必须是对象")]
    if lockfile.get("ot_version") != OT_VERSION:
        diagnostics.append(OtDiagnostic("OT_LOCKFILE_VERSION", "error", f"{OT_LOCKFILE}.ot_version", f"Lockfile 必须使用 OT {OT_VERSION}"))
    package = manifest.get("package", {}) if isinstance(manifest, Mapping) else {}
    if lockfile.get("package_id") != package.get("id"):
        diagnostics.append(OtDiagnostic("OT_LOCKFILE_PACKAGE", "error", f"{OT_LOCKFILE}.package_id", "Lockfile 包 ID 与 Manifest 不一致"))
    if lockfile.get("package_version") != package.get("version"):
        diagnostics.append(OtDiagnostic("OT_LOCKFILE_PACKAGE", "error", f"{OT_LOCKFILE}.package_version", "Lockfile 包版本与 Manifest 不一致"))
    dependencies = lockfile.get("dependencies")
    if not isinstance(dependencies, list):
        diagnostics.append(OtDiagnostic("OT_LOCKFILE_DEPENDENCIES", "error", f"{OT_LOCKFILE}.dependencies", "Lockfile 依赖必须是数组"))
    locked_resources = lockfile.get("resources")
    if not isinstance(locked_resources, list):
        diagnostics.append(OtDiagnostic("OT_LOCKFILE_RESOURCES", "error", f"{OT_LOCKFILE}.resources", "Lockfile 资源必须是数组"))
        return diagnostics

    locked: dict[str, str] = {}
    for index, record in enumerate(locked_resources):
        if not isinstance(record, dict):
            diagnostics.append(OtDiagnostic("OT_LOCKFILE_RESOURCE", "error", f"{OT_LOCKFILE}.resources[{index}]", "Lockfile 资源记录必须是对象"))
            continue
        resource_id = record.get("id")
        resource_hash = record.get("sha256")
        if not isinstance(resource_id, str) or not isinstance(resource_hash, str) or resource_id in locked:
            diagnostics.append(OtDiagnostic("OT_LOCKFILE_RESOURCE", "error", f"{OT_LOCKFILE}.resources[{index}]", "Lockfile 资源 ID/哈希无效或重复"))
            continue
        locked[resource_id] = resource_hash

    declared: dict[str, str] = {}
    resources = manifest.get("resources", []) if isinstance(manifest, Mapping) else []
    if isinstance(resources, list):
        for record in resources:
            if isinstance(record, Mapping) and isinstance(record.get("id"), str) and isinstance(record.get("sha256"), str):
                declared[str(record["id"])] = str(record["sha256"])
    if locked != declared:
        diagnostics.append(OtDiagnostic("OT_LOCKFILE_MISMATCH", "error", f"{OT_LOCKFILE}.resources", "Lockfile 资源集合或哈希与 Manifest 不一致"))
    return diagnostics


def _content_identity(manifest: Mapping[str, Any], resource_records: Iterable[Mapping[str, Any]]) -> str:
    identity_manifest = dict(manifest)
    identity_manifest.pop("integrity", None)
    digest = hashlib.sha256()
    digest.update(canonical_json(identity_manifest))
    for record in sorted(resource_records, key=lambda item: str(item["path"])):
        digest.update(str(record["path"]).encode("utf-8"))
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(str(record["bytes"]).encode("ascii"))
    return digest.hexdigest()


def read_ot(source: Path | bytes, *, for_execution: bool = False) -> OtPackage:
    raw = source.read_bytes() if isinstance(source, Path) else bytes(source)
    diagnostics: list[OtDiagnostic] = []
    if isinstance(source, Path) and source.suffix.lower() != ".ot":
        raise OtValidationError(
            [OtDiagnostic("OT_EXTENSION_REQUIRED", "error", str(source), "文件必须使用 .ot 扩展名")]
        )
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise OtValidationError(
            [OtDiagnostic("OT_ARCHIVE_TOO_LARGE", "error", "", "OT 包超过 10 MB 限制")]
        )
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise OtValidationError(
            [OtDiagnostic("OT_CONTAINER_INVALID", "error", "", ".ot 必须是有效的 OT ZIP 容器")]
        )

    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ENTRIES:
            raise OtValidationError(
                [OtDiagnostic("OT_ENTRY_BUDGET", "error", "", "OT 包条目数量无效")]
            )
        seen: set[str] = set()
        seen_casefold: set[str] = set()
        expanded = 0
        for info in infos:
            if info.is_dir():
                continue
            try:
                name = _safe_member_name(info.filename)
            except OtValidationError as exc:
                diagnostics.extend(exc.diagnostics)
                continue
            folded = name.casefold()
            if name in seen or folded in seen_casefold:
                diagnostics.append(OtDiagnostic("OT_PATH_DUPLICATE", "error", name, "OT 包包含重复或大小写冲突路径"))
                continue
            seen.add(name)
            seen_casefold.add(folded)
            suffix = Path(name).suffix.lower()
            if suffix in FORBIDDEN_SUFFIXES or (name != OT_MANIFEST and suffix == ".ot"):
                diagnostics.append(OtDiagnostic("OT_EXECUTABLE_FORBIDDEN", "error", name, "OT 包包含禁止的可执行或嵌套归档文件"))
            if _is_symlink(info):
                diagnostics.append(OtDiagnostic("OT_SYMLINK_FORBIDDEN", "error", name, "OT 包不允许符号链接"))
            if info.file_size > MAX_RESOURCE_BYTES:
                diagnostics.append(OtDiagnostic("OT_RESOURCE_TOO_LARGE", "error", name, "单个资源超过 2 MB"))
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                diagnostics.append(OtDiagnostic("OT_EXPANDED_BUDGET", "error", name, "OT 包解压后超过 20 MB"))
                break
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                diagnostics.append(OtDiagnostic("OT_COMPRESSION_RATIO", "error", name, "资源压缩率超过安全限制"))
            try:
                entries[name] = archive.read(info)
            except (RuntimeError, zipfile.BadZipFile):
                diagnostics.append(OtDiagnostic("OT_RESOURCE_READ", "error", name, "资源无法安全读取"))

    try:
        manifest = json.loads(entries.get(OT_MANIFEST, b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        manifest = {}
        diagnostics.append(OtDiagnostic("OT_MANIFEST_JSON", "error", OT_MANIFEST, "manifest.json 必须是 UTF-8 JSON"))
    diagnostics.extend(_validate_manifest_shape(manifest))
    try:
        lockfile = json.loads(entries.get(OT_LOCKFILE, b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        lockfile = {}
        diagnostics.append(OtDiagnostic("OT_LOCKFILE_JSON", "error", OT_LOCKFILE, "ot.lock.json 必须是 UTF-8 JSON"))
    diagnostics.extend(_validate_lockfile(lockfile, manifest))

    for path, payload in entries.items():
        if _contains_secret(payload):
            diagnostics.append(OtDiagnostic("OT_SECRET_DETECTED", "error", path, "容器疑似包含 API Key、Token 或密码"))

    declared_paths: set[str] = set()
    resource_records = manifest.get("resources", []) if isinstance(manifest, dict) else []
    if isinstance(resource_records, list):
        resource_ids: set[str] = set()
        for index, record in enumerate(resource_records):
            path_label = f"resources[{index}]"
            if not isinstance(record, dict):
                diagnostics.append(OtDiagnostic("OT_RESOURCE_RECORD", "error", path_label, "资源声明必须是对象"))
                continue
            resource_id = record.get("id")
            if not isinstance(resource_id, str) or resource_id in resource_ids:
                diagnostics.append(OtDiagnostic("OT_RESOURCE_ID", "error", f"{path_label}.id", "资源 ID 缺失或重复"))
            else:
                resource_ids.add(resource_id)
            try:
                path = _safe_member_name(str(record.get("path", "")))
            except OtValidationError as exc:
                diagnostics.extend(exc.diagnostics)
                continue
            if path in {OT_MANIFEST, OT_LOCKFILE}:
                diagnostics.append(OtDiagnostic("OT_RESOURCE_RESERVED", "error", path, "资源不能占用容器保留路径"))
                continue
            declared_paths.add(path)
            payload = entries.get(path)
            if payload is None:
                diagnostics.append(OtDiagnostic("OT_RESOURCE_MISSING", "error", path, "Manifest 声明的资源不存在"))
                continue
            if record.get("bytes") != len(payload):
                diagnostics.append(OtDiagnostic("OT_RESOURCE_SIZE", "error", path, "资源字节数与 Manifest 不一致"))
            if record.get("sha256") != sha256_bytes(payload):
                diagnostics.append(OtDiagnostic("OT_RESOURCE_HASH", "error", path, "资源 SHA-256 与 Manifest 不一致"))
            if record.get("media_type") not in ALLOWED_MEDIA_TYPES:
                diagnostics.append(OtDiagnostic("OT_MEDIA_TYPE", "error", path, "资源媒体类型不受支持"))
    allowed_container = {OT_MANIFEST, OT_LOCKFILE, "README.md"}
    undeclared = sorted(set(entries) - declared_paths - allowed_container)
    for path in undeclared:
        diagnostics.append(OtDiagnostic("OT_RESOURCE_UNDECLARED", "error", path, "容器文件未在 Manifest 中声明"))

    required = manifest.get("required_capabilities", []) if isinstance(manifest, dict) else []
    unknown_required = sorted(set(required) - KNOWN_REQUIRED_CAPABILITIES) if isinstance(required, list) else []
    for capability in unknown_required:
        diagnostics.append(
            OtDiagnostic(
                "OT_CAPABILITY_UNKNOWN",
                "error" if for_execution else "warning",
                "required_capabilities",
                f"未知必需能力：{capability}",
            )
        )

    if isinstance(manifest, dict) and isinstance(resource_records, list):
        expected_identity = _content_identity(manifest, [item for item in resource_records if isinstance(item, dict)])
        recorded_identity = manifest.get("integrity", {}).get("content_identity") if isinstance(manifest.get("integrity"), dict) else None
        if recorded_identity != expected_identity:
            diagnostics.append(OtDiagnostic("OT_CONTENT_IDENTITY", "error", "integrity.content_identity", "内容身份校验失败"))
    else:
        expected_identity = ""

    errors = [item for item in diagnostics if item.severity == "error"]
    if errors:
        raise OtValidationError(diagnostics)
    resource_payloads = {path: entries[path] for path in declared_paths if path in entries}
    return OtPackage(manifest, resource_payloads, expected_identity, tuple(diagnostics))


def validate_studio_draft(draft: Mapping[str, Any]) -> tuple[OtDiagnostic, ...]:
    diagnostics: list[OtDiagnostic] = []
    package = draft.get("package")
    if not isinstance(package, Mapping):
        diagnostics.append(OtDiagnostic("OT_DRAFT_PACKAGE", "error", "package", "请填写包信息"))
    else:
        package_id = str(package.get("id", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", package_id):
            diagnostics.append(OtDiagnostic("OT_PACKAGE_ID", "error", "package.id", "包 ID 需使用小写字母、数字、点、横线或下划线"))
        for key in ("name", "version", "kind", "description"):
            if not str(package.get(key, "")).strip():
                diagnostics.append(OtDiagnostic("OT_DRAFT_FIELD", "error", f"package.{key}", "此字段不能为空"))
        kind = str(package.get("kind", ""))
        if kind and kind != "openthesis.research-pack":
            diagnostics.append(OtDiagnostic("OT_PACKAGE_KIND", "error", "package.kind", "2.0 创作工作室当前只能导出 openthesis.research-pack"))
        version = str(package.get("version", ""))
        if version and not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
            diagnostics.append(OtDiagnostic("OT_PACKAGE_VERSION", "error", "package.version", "包版本必须使用 SemVer 形式"))

    settings = draft.get("settings")
    if not isinstance(settings, Mapping):
        diagnostics.append(OtDiagnostic("OT_SETTINGS_TYPE", "error", "settings", "设置必须是对象"))
    else:
        for key, minimum, maximum in (("horizon_years", 1, 20), ("depth", 1, 5), ("risk_emphasis", 1, 5)):
            value = settings.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                diagnostics.append(OtDiagnostic("OT_SETTING_RANGE", "error", f"settings.{key}", f"设置必须是 {minimum} 到 {maximum} 的整数"))
        evidence_policy = settings.get("evidence_policy")
        if evidence_policy is not None and not isinstance(evidence_policy, Mapping):
            diagnostics.append(OtDiagnostic("OT_SETTINGS_TYPE", "error", "settings.evidence_policy", "财报证据策略必须是对象"))
        elif isinstance(evidence_policy, Mapping):
            annual_history = evidence_policy.get("annual_history_years")
            if (
                not isinstance(annual_history, int)
                or isinstance(annual_history, bool)
                or not 2 <= annual_history <= 10
            ):
                diagnostics.append(OtDiagnostic(
                    "OT_SETTING_RANGE",
                    "error",
                    "settings.evidence_policy.annual_history_years",
                    "财报证据历史必须是 2 到 10 的整数",
                ))
        if settings.get("report_language") not in {"zh-CN", "zh-Hant", "en"}:
            diagnostics.append(OtDiagnostic("OT_REPORT_LANGUAGE", "error", "settings.report_language", "报告语言必须是 zh-CN、zh-Hant 或 en"))

    workflow = draft.get("workflow")
    steps = workflow.get("steps") if isinstance(workflow, Mapping) else None
    if not isinstance(steps, list) or not steps:
        diagnostics.append(OtDiagnostic("OT_WORKFLOW_EMPTY", "error", "workflow.steps", "工作流至少需要一个步骤"))
    else:
        seen: set[str] = set()
        for index, step in enumerate(steps):
            base = f"workflow.steps[{index}]"
            if not isinstance(step, Mapping):
                diagnostics.append(OtDiagnostic("OT_STEP_TYPE", "error", base, "步骤必须是对象"))
                continue
            step_id = str(step.get("id", ""))
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", step_id) or step_id in seen:
                diagnostics.append(OtDiagnostic("OT_STEP_ID", "error", f"{base}.id", "步骤 ID 无效或重复"))
            seen.add(step_id)
            if not str(step.get("prompt", "")).strip():
                diagnostics.append(OtDiagnostic("OT_STEP_PROMPT", "error", f"{base}.prompt", "步骤 Prompt 不能为空"))
            dependencies = step.get("depends_on", [])
            if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
                diagnostics.append(OtDiagnostic("OT_STEP_DEPENDENCIES", "error", f"{base}.depends_on", "依赖必须是步骤 ID 数组"))
        dependency_map: dict[str, set[str]] = {}
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            step_id = str(step.get("id", ""))
            role = step.get("role")
            output_schema = step.get("output_schema")
            if not isinstance(role, str) or not role.strip():
                diagnostics.append(OtDiagnostic("OT_STEP_ROLE", "error", f"workflow.steps[{index}].role", "步骤角色不能为空"))
            if not isinstance(output_schema, str) or not output_schema.strip():
                diagnostics.append(OtDiagnostic("OT_STEP_SCHEMA", "error", f"workflow.steps[{index}].output_schema", "步骤输出 Schema 不能为空"))
            dependencies = step.get("depends_on", [])
            if isinstance(dependencies, list):
                dependency_map[step_id] = {item for item in dependencies if isinstance(item, str) and item in seen}
                for dependency in dependencies:
                    if dependency not in seen:
                        diagnostics.append(OtDiagnostic("OT_DEPENDENCY_MISSING", "error", f"workflow.steps[{index}].depends_on", f"依赖不存在：{dependency}"))
        remaining = {key: set(value) for key, value in dependency_map.items()}
        while remaining:
            ready = {key for key, dependencies in remaining.items() if not dependencies.intersection(remaining)}
            if not ready:
                diagnostics.append(OtDiagnostic("OT_DEPENDENCY_CYCLE", "error", "workflow.steps", "工作流依赖不能形成循环"))
                break
            for key in ready:
                remaining.pop(key, None)

    outputs = draft.get("outputs")
    if not isinstance(outputs, Mapping):
        diagnostics.append(OtDiagnostic("OT_OUTPUTS_TYPE", "error", "outputs", "输出配置必须是对象"))
    else:
        formats = outputs.get("formats")
        if not isinstance(formats, list) or not formats or any(item not in {"markdown", "json", "html"} for item in formats):
            diagnostics.append(OtDiagnostic("OT_OUTPUT_FORMAT", "error", "outputs.formats", "输出格式只能包含 markdown、json 或 html"))
        if not isinstance(outputs.get("include_evidence"), bool):
            diagnostics.append(OtDiagnostic("OT_OUTPUT_EVIDENCE", "error", "outputs.include_evidence", "证据开关必须是布尔值"))
        transforms = outputs.get("deterministic_transforms")
        if not isinstance(transforms, list) or any(not isinstance(item, str) or not item for item in transforms):
            diagnostics.append(OtDiagnostic("OT_OUTPUT_TRANSFORMS", "error", "outputs.deterministic_transforms", "确定性变换必须是非空字符串数组"))

    for key in ("dependencies", "relationships"):
        if not isinstance(draft.get(key), list):
            diagnostics.append(OtDiagnostic("OT_DRAFT_COLLECTION", "error", key, "此字段必须是数组"))
    if not isinstance(draft.get("optional_extensions"), Mapping):
        diagnostics.append(OtDiagnostic("OT_EXTENSIONS_TYPE", "error", "optional_extensions", "可选扩展必须是对象"))

    serialized = canonical_json(draft)
    if len(serialized) > MAX_ARCHIVE_BYTES:
        diagnostics.append(OtDiagnostic("OT_DRAFT_TOO_LARGE", "error", "", "草稿超过 10 MB 限制"))
    if _contains_secret(serialized):
        diagnostics.append(OtDiagnostic("OT_SECRET_DETECTED", "error", "", "草稿疑似包含 API Key、Token 或密码"))
    return tuple(diagnostics)


def compile_studio_draft(draft: Mapping[str, Any]) -> tuple[bytes, OtPackage]:
    diagnostics = validate_studio_draft(draft)
    if any(item.severity == "error" for item in diagnostics):
        raise OtValidationError(diagnostics)

    package = dict(draft["package"])
    workflow = dict(draft["workflow"])
    steps = [dict(step) for step in workflow["steps"]]
    resources: dict[str, tuple[str, str, str, bytes]] = {}

    compiled_steps: list[dict[str, Any]] = []
    for step in steps:
        step_id = str(step["id"])
        prompt_path = _safe_member_name(str(step.pop("prompt_path", f"resources/prompts/{step_id}.md")))
        prompt = str(step.pop("prompt")).strip() + "\n"
        resources[prompt_path] = (
            f"prompt.{step_id}",
            "openthesis.prompt",
            "text/markdown",
            prompt.encode("utf-8"),
        )
        step["prompt"] = prompt_path
        compiled_steps.append(step)

    workflow_payload = {
        "schema": "ot://openthesis/workflow/1",
        "steps": compiled_steps,
        "settings": draft.get("settings", {}),
        "model_requirements": draft.get("model_requirements", {
            "capabilities": ["text_chat", "structured_json"],
        }),
    }
    resources["resources/workflow.json"] = (
        "workflow.main",
        "openthesis.workflow",
        "application/json",
        canonical_json(workflow_payload),
    )
    resources["resources/output.json"] = (
        "output.configuration",
        "openthesis.output-schema",
        "application/json",
        canonical_json(draft.get("outputs", {
            "formats": ["json", "markdown"],
            "include_evidence": True,
            "deterministic_transforms": [],
        })),
    )
    resources["resources/ui-form.json"] = (
        "ui.form",
        "openthesis.ui-form",
        "application/json",
        canonical_json(draft.get("ui", {})),
    )

    records: list[dict[str, Any]] = []
    for path, (resource_id, resource_type, media_type, payload) in sorted(resources.items()):
        records.append({
            "id": resource_id,
            "type": resource_type,
            "schema": f"ot://{resource_type.replace('.', '/')}/1",
            "media_type": media_type,
            "path": path,
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
            "purpose": "runtime",
            "privacy": "public",
        })

    manifest: dict[str, Any] = {
        "ot_version": OT_VERSION,
        "schema_version": "1",
        "package": {
            "id": package["id"],
            "name": package["name"],
            "version": package["version"],
            "kind": package.get("kind", "openthesis.research-pack"),
            "description": package["description"],
            "license": package.get("license", "Apache-2.0"),
            "compatibility": package.get("compatibility", {"openthesis": ">=2.0.0,<3.0.0"}),
        },
        "permissions": {
            "network": [],
            "filesystem": "none",
            "execute_code": False,
            "secrets": "prohibited",
        },
        "budgets": {
            "max_resources": MAX_ENTRIES,
            "max_resource_bytes": MAX_RESOURCE_BYTES,
            "max_expanded_bytes": MAX_EXPANDED_BYTES,
        },
        "required_capabilities": ["openthesis.workflow.v1", "openthesis.output-schema.v1"],
        "optional_extensions": draft.get("optional_extensions", {}),
        "resources": records,
        "relationships": draft.get("relationships", []),
        "provenance": draft.get("provenance", {"created_with": "OpenThesis OT Studio 2.0"}),
    }
    manifest["integrity"] = {
        "algorithm": "sha256",
        "content_identity": _content_identity(manifest, records),
    }
    lockfile = {
        "ot_version": OT_VERSION,
        "package_id": package["id"],
        "package_version": package["version"],
        "dependencies": draft.get("dependencies", []),
        "resources": [{"id": item["id"], "sha256": item["sha256"]} for item in records],
    }

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        payloads = {
            OT_MANIFEST: canonical_json(manifest),
            OT_LOCKFILE: canonical_json(lockfile),
            **{path: data[3] for path, data in resources.items()},
        }
        for path in sorted(payloads):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, payloads[path])

    raw = output.getvalue()
    package_result = read_ot(raw, for_execution=True)
    return raw, package_result


def minimal_studio_draft() -> dict[str, Any]:
    return {
        "package": {
            "id": "my.company-research",
            "name": "Company Research",
            "version": "1.0.0",
            "kind": "openthesis.research-pack",
            "description": "A traceable company-research workflow.",
            "license": "Apache-2.0",
        },
        "settings": {
            "horizon_years": 5,
            "evidence_policy": {"annual_history_years": 5},
            "depth": 3,
            "risk_emphasis": 3,
            "report_language": "en",
        },
        "workflow": {
            "steps": [
                {
                    "id": "company-analysis",
                    "role": "business-analysis",
                    "depends_on": [],
                    "prompt": "Analyze the company using only the supplied evidence. Identify uncertainty explicitly.",
                    "output_schema": "ot://openthesis/agent-artifact/1",
                },
                {
                    "id": "verification",
                    "role": "verification",
                    "depends_on": ["company-analysis"],
                    "prompt": "Verify every material claim against the supplied evidence and deterministic calculations.",
                    "output_schema": "ot://openthesis/diagnostic/1",
                },
            ]
        },
        "outputs": {
            "formats": ["json", "markdown"],
            "include_evidence": True,
            "deterministic_transforms": [],
        },
        "ui": {
            "mode": "beginner",
            "controls": ["horizon_years", "depth", "risk_emphasis", "report_language"],
        },
        "model_requirements": {
            "capabilities": ["text_chat", "structured_json"],
            "preferred_profile_alias": None,
        },
        "dependencies": [],
        "relationships": [],
        "optional_extensions": {},
    }
