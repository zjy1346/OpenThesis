#!/usr/bin/env python3
"""Extract the packaged OpenThesis Python modules from a PyInstaller sidecar.

This is a read-only archive replay aid. It never launches the sidecar executable
and does not recreate the embedded Python runtime, native libraries, bootloader,
or GUI environment.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import marshal
import os
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from types import CodeType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_OUTPUT_ROOT = REPO_ROOT / "tmp"
SIDECAR_EXE_SUFFIX = ("bin", "openthesis-sidecar", "openthesis-sidecar.exe")
SIDECAR_RESOURCES_SUFFIX = "_internal/openthesis/"
PYZ_MAGIC_PATTERN = b"PYZ\0"
PYC_HEADER_PADDING = bytes(12)
REPARSE_POINT_ATTRIBUTE = 0x400


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract packaged OpenThesis Python code objects from one PyInstaller "
            "sidecar without executing it."
        )
    )
    parser.add_argument("--archive", required=True, type=Path, help="input portable ZIP archive")
    parser.add_argument("--output", required=True, type=Path, help="new output directory under the repository tmp/")
    parser.add_argument("--expected-sha256", required=True, help="expected SHA-256 of the ZIP archive")
    parser.add_argument("--version", help="optional expected version from packaged build-info.json")
    return parser


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(path, "is_junction", lambda: False)():
        return True
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _validate_new_output_path(value: Path) -> Path:
    """Return an absolute, safe output path without creating it."""
    tmp_root = Path(os.path.abspath(os.fspath(ALLOWED_OUTPUT_ROOT)))
    candidate = Path(os.path.abspath(os.fspath(value if value.is_absolute() else Path.cwd() / value)))

    try:
        relative = candidate.relative_to(tmp_root)
    except ValueError as exc:
        raise ValueError(f"output must be inside {tmp_root}") from exc
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("output must be a new child directory of tmp/")

    if not tmp_root.is_dir() or _is_reparse_point(tmp_root):
        raise ValueError(f"output root is missing, not a directory, or a link: {tmp_root}")
    # Reject reparse points in every existing ancestor. With all ancestors
    # checked, lexical containment is also physical containment without
    # relying on Windows GetFinalPathNameByHandle permissions.
    for ancestor in reversed(tmp_root.parents):
        if _lexists(ancestor) and _is_reparse_point(ancestor):
            raise ValueError(f"output root ancestry contains a link/reparse point: {ancestor}")

    cursor = tmp_root
    for part in relative.parts:
        cursor = cursor / part
        if _lexists(cursor):
            if _is_reparse_point(cursor):
                raise ValueError(f"output path contains a link/reparse point: {cursor}")
            if cursor == candidate:
                raise FileExistsError(f"output already exists; refusing overwrite: {candidate}")

    return candidate


def _validate_archive_path(value: Path) -> Path:
    path = Path(os.path.abspath(os.fspath(value if value.is_absolute() else Path.cwd() / value)))
    if not path.is_file() or _is_reparse_point(path):
        raise ValueError(f"archive must be a regular, non-link file: {path}")
    for ancestor in path.parents:
        if _lexists(ancestor) and _is_reparse_point(ancestor):
            raise ValueError(f"archive path contains a link/reparse point: {ancestor}")
    return path


def _safe_zip_parts(name: str) -> tuple[str, ...]:
    if "\\" in name or name.startswith("/") or "\x00" in name:
        raise ValueError(f"unsafe ZIP path: {name!r}")
    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe ZIP path: {name!r}")
    if parts[0].endswith(":"):
        raise ValueError(f"drive-qualified ZIP path is not allowed: {name!r}")
    return parts


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = info.external_attr >> 16
    return stat.S_ISLNK(unix_mode)


def _copy_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with zf.open(info, "r") as source, destination.open("xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)
            digest.update(chunk)
    return digest.hexdigest().upper()


def _ensure_output_descendant(output: Path, destination: Path) -> Path:
    output_absolute = Path(os.path.abspath(os.fspath(output)))
    destination_absolute = Path(os.path.abspath(os.fspath(destination)))
    try:
        destination_absolute.relative_to(output_absolute)
    except ValueError as exc:
        raise ValueError(f"refusing to write outside output directory: {destination}") from exc
    cursor = output_absolute
    for part in destination_absolute.relative_to(output_absolute).parts:
        cursor = cursor / part
        if _lexists(cursor) and _is_reparse_point(cursor):
            raise ValueError(f"output contains a link/reparse point: {cursor}")
    return destination_absolute


def _load_build_info(zf: zipfile.ZipFile, resource_prefix: str) -> tuple[str, dict[str, Any]]:
    expected_suffix = resource_prefix + "resources/build-info.json"
    matches = [info for info in zf.infolist() if info.filename == expected_suffix]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one packaged build-info.json, found {len(matches)}")
    info = matches[0]
    if info.is_dir() or _is_zip_symlink(info):
        raise ValueError("packaged build-info.json must be a regular ZIP member")
    try:
        value = json.loads(zf.read(info).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("packaged build-info.json is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("packaged build-info.json must contain a JSON object")
    return info.filename, value


def _load_pyinstaller_readers() -> tuple[Any, Any, Any, Any]:
    tools_root = REPO_ROOT / ".build-tools"
    if str(tools_root) not in sys.path:
        sys.path.insert(0, str(tools_root))
    try:
        from PyInstaller.archive.readers import CArchiveReader, PKG_ITEM_PYZ
        from PyInstaller.loader.pyimod01_archive import (
            PYTHON_MAGIC_NUMBER,
            PYZ_ITEM_MODULE,
            PYZ_ITEM_NSPKG,
            PYZ_ITEM_PKG,
        )
    except ImportError as exc:
        raise RuntimeError(f"could not load the repository's local PyInstaller readers: {exc}") from exc
    return CArchiveReader, PKG_ITEM_PYZ, PYTHON_MAGIC_NUMBER, (PYZ_ITEM_MODULE, PYZ_ITEM_PKG, PYZ_ITEM_NSPKG)


def _extract_code_objects(
    *,
    pyz: Any,
    exe_path: Path,
    code_root: Path,
    host_magic: bytes,
    type_codes: tuple[int, int, int],
    output: Path,
) -> tuple[list[dict[str, Any]], bytes]:
    module_type, package_type, namespace_type = type_codes
    pyz_offset = int(pyz._start_offset)
    with exe_path.open("rb") as stream:
        stream.seek(pyz_offset)
        if stream.read(len(PYZ_MAGIC_PATTERN)) != PYZ_MAGIC_PATTERN:
            raise ValueError("embedded archive does not start with the expected PYZ signature")
        pyz_magic = stream.read(len(host_magic))
    if pyz_magic != host_magic:
        raise ValueError(
            "embedded PYZ Python magic does not match the host interpreter: "
            f"PYZ={pyz_magic.hex()} host={host_magic.hex()}"
        )

    records: list[dict[str, Any]] = []
    for name in sorted(pyz.toc):
        if name != "openthesis" and not name.startswith("openthesis."):
            continue
        module_parts = name.split(".")
        if any(not part or part in (".", "..") or "/" in part or "\\" in part for part in module_parts):
            raise ValueError(f"unsafe Python module name in PYZ: {name!r}")
        package_path = code_root.joinpath(*module_parts)

        type_code = pyz.toc[name][0]
        if type_code == namespace_type:
            target = _ensure_output_descendant(output, package_path)
            target.mkdir(parents=True, exist_ok=True)
            records.append(
                {
                    "name": name,
                    "kind": "namespace",
                    "pyz_type": type_code,
                    "path": target.relative_to(output).as_posix(),
                }
            )
            continue
        if type_code not in (module_type, package_type):
            continue

        code = pyz.extract(name)
        if not isinstance(code, CodeType):
            raise TypeError(f"PYZ entry {name!r} did not produce a code object")
        code_bytes = marshal.dumps(code)
        if type_code == package_type:
            pyc_path = package_path / "__init__.pyc"
        else:
            pyc_path = package_path.with_suffix(".pyc")
        pyc_path = _ensure_output_descendant(output, pyc_path)
        pyc_path.parent.mkdir(parents=True, exist_ok=True)
        pyc_bytes = importlib.util.MAGIC_NUMBER + PYC_HEADER_PADDING + code_bytes
        with pyc_path.open("xb") as stream:
            stream.write(pyc_bytes)
        records.append(
            {
                "name": name,
                "kind": "package" if type_code == package_type else "module",
                "pyz_type": type_code,
                "code_sha256": _sha256_bytes(code_bytes),
                "pyc_path": pyc_path.relative_to(output).as_posix(),
                "pyc_sha256": _sha256_bytes(pyc_bytes),
            }
        )
    return records, pyz_magic


def _extract_resources(
    zf: zipfile.ZipFile,
    *,
    resource_prefix: str,
    code_root: Path,
    output: Path,
) -> list[dict[str, Any]]:
    resources: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    seen: set[str] = set()
    for info in zf.infolist():
        if not info.filename.startswith(resource_prefix):
            continue
        if info.is_dir():
            continue
        if _is_zip_symlink(info):
            raise ValueError(f"refusing to copy symlink-like ZIP resource: {info.filename!r}")
        parts = _safe_zip_parts(info.filename)
        prefix_parts = tuple(resource_prefix.rstrip("/").split("/"))
        if parts[: len(prefix_parts)] != prefix_parts:
            raise ValueError(f"resource escaped its sidecar package directory: {info.filename!r}")
        relative_parts = parts[len(prefix_parts) :]
        if not relative_parts:
            raise ValueError(f"resource path is empty: {info.filename!r}")
        relative_name = "/".join(relative_parts)
        if relative_name in seen:
            raise ValueError(f"duplicate packaged resource path: {relative_name!r}")
        seen.add(relative_name)
        resources.append((info, relative_parts))

    records: list[dict[str, Any]] = []
    for info, relative_parts in resources:
        target = code_root.joinpath(*relative_parts)
        target = _ensure_output_descendant(output, target)
        digest = _copy_zip_member(zf, info, target)
        records.append(
            {
                "path": target.relative_to(output).as_posix(),
                "size_bytes": info.file_size,
                "sha256": digest,
            }
        )
    return records


def extract_archive(archive_arg: Path, output_arg: Path, expected_sha256: str, version: str | None) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(f"this replay extractor requires host Python 3.12; got {sys.version.split()[0]}")

    normalized_expected = expected_sha256.strip().upper()
    if len(normalized_expected) != 64 or any(char not in "0123456789ABCDEF" for char in normalized_expected):
        raise ValueError("--expected-sha256 must be exactly 64 hexadecimal characters")

    archive = _validate_archive_path(archive_arg)
    archive_sha256 = _sha256_file(archive)
    if archive_sha256 != normalized_expected:
        raise ValueError(f"archive SHA-256 mismatch: expected {normalized_expected}, got {archive_sha256}")

    output = _validate_new_output_path(output_arg)

    with zipfile.ZipFile(archive, "r") as zf:
        all_infos = zf.infolist()
        exe_matches: list[zipfile.ZipInfo] = []
        for info in all_infos:
            try:
                parts = _safe_zip_parts(info.filename.rstrip("/"))
            except ValueError:
                # Unrelated ZIP entries are not extracted. Unsafe candidate paths are
                # caught by the exact candidate scan below when they match the suffix.
                normalized = info.filename.replace("\\", "/")
                if normalized.endswith("/" + "/".join(SIDECAR_EXE_SUFFIX)):
                    raise
                continue
            if len(parts) == 4 and parts[1:] == SIDECAR_EXE_SUFFIX:
                exe_matches.append(info)
        if len(exe_matches) != 1:
            raise ValueError(f"expected exactly one */bin/openthesis-sidecar/openthesis-sidecar.exe, found {len(exe_matches)}")
        exe_info = exe_matches[0]
        if exe_info.is_dir() or _is_zip_symlink(exe_info):
            raise ValueError("selected sidecar executable must be a regular ZIP member")
        exe_parts = _safe_zip_parts(exe_info.filename)
        sidecar_root = "/".join(exe_parts[:-1])
        resource_prefix = sidecar_root + "/" + SIDECAR_RESOURCES_SUFFIX
        build_info_name, build_info = _load_build_info(zf, resource_prefix)
        if version is not None and str(build_info.get("version", "")) != version:
            raise ValueError(
                f"packaged version mismatch: expected {version!r}, got {build_info.get('version')!r}"
            )

        # Validate resource names before creating output; copying later is exclusive.
        resource_infos = [info for info in all_infos if info.filename.startswith(resource_prefix) and not info.is_dir()]
        seen_resource_names: set[str] = set()
        prefix_parts = tuple(resource_prefix.rstrip("/").split("/"))
        for info in resource_infos:
            if _is_zip_symlink(info):
                raise ValueError(f"refusing to copy symlink-like ZIP resource: {info.filename!r}")
            parts = _safe_zip_parts(info.filename)
            if parts[: len(prefix_parts)] != prefix_parts or len(parts) <= len(prefix_parts):
                raise ValueError(f"resource escaped its sidecar package directory: {info.filename!r}")
            rel = "/".join(parts[len(prefix_parts) :])
            if rel in seen_resource_names:
                raise ValueError(f"duplicate packaged resource path: {rel!r}")
            seen_resource_names.add(rel)

        output.mkdir(parents=True, exist_ok=False)
        if _is_reparse_point(output):
            raise ValueError(f"created output directory is unexpectedly a link: {output}")
        tool_dir = output / "tool"
        tool_dir.mkdir()
        exe_path = _ensure_output_descendant(output, tool_dir / "openthesis-sidecar.exe")
        _copy_zip_member(zf, exe_info, exe_path)

        CArchiveReader, pkg_item_pyz, local_pymagic, pyz_type_codes = _load_pyinstaller_readers()
        host_magic = importlib.util.MAGIC_NUMBER
        if local_pymagic != host_magic:
            raise ValueError(
                "local PyInstaller loader magic does not match host interpreter: "
                f"loader={local_pymagic.hex()} host={host_magic.hex()}"
            )
        carchive = CArchiveReader(str(exe_path))
        pyz_entries = [name for name, entry in carchive.toc.items() if entry[-1] == pkg_item_pyz]
        if len(pyz_entries) != 1:
            raise ValueError(f"expected exactly one CArchive type-z PYZ entry, found {len(pyz_entries)}")
        pyz_name = pyz_entries[0]
        pyz = carchive.open_embedded_archive(pyz_name)

        code_root = output / "code-root"
        code_root.mkdir()
        module_records, pyz_magic = _extract_code_objects(
            pyz=pyz,
            exe_path=exe_path,
            code_root=code_root,
            host_magic=host_magic,
            type_codes=pyz_type_codes,
            output=output,
        )
        resource_records = _extract_resources(
            zf,
            resource_prefix=resource_prefix,
            code_root=code_root / "openthesis",
            output=output,
        )

        manifest = {
            "schema": "openthesis-packaged-python-extraction-v1",
            "archive": {
                "path": str(archive),
                "sha256": archive_sha256,
            },
            "build_info": build_info,
            "build_info_path_in_archive": build_info_name,
            "sidecar": {
                "zip_path": exe_info.filename,
                "output_path": exe_path.relative_to(output).as_posix(),
                "sha256": _sha256_file(exe_path),
            },
            "pyz": {
                "entry_name": pyz_name,
                "python_magic_hex": pyz_magic.hex(),
                "entry_count": len(pyz.toc),
            },
            "host_python": {
                "version": sys.version.split()[0],
                "magic_hex": host_magic.hex(),
                "code_root": code_root.relative_to(output).as_posix(),
            },
            "modules": module_records,
            "module_count": sum(record["kind"] != "namespace" for record in module_records),
            "namespace_count": sum(record["kind"] == "namespace" for record in module_records),
            "resources": resource_records,
            "resource_count": len(resource_records),
            "execution_boundary": {
                "mode": "host-python-pyc-replay-only",
                "host_runtime_is_embedded": False,
                "sidecar_executed": False,
                "included": "PYZ code objects and adjacent _internal/openthesis resource files only",
                "excluded": [
                    "the package's embedded Python runtime and bootloader",
                    "native/shared libraries and other sidecar dependencies",
                    "GUI behavior, application lifecycle, and packaged runtime integration",
                ],
                "co_filename_modified": False,
            },
        }
        manifest_path = _ensure_output_descendant(output, output / "manifest.json")
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")

    return output


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = extract_archive(args.archive, args.output, args.expected_sha256, args.version)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"output={output}")
    print(f"archive_sha256={manifest['archive']['sha256']}")
    print(f"version={manifest['build_info'].get('version', 'unknown')}")
    print(f"modules={manifest['module_count']}")
    print(f"namespaces={manifest['namespace_count']}")
    print(f"resources={manifest['resource_count']}")
    print("host_runtime_embedded=false")
    print("sidecar_executed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
