from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from openthesis.ot import (
    MAX_ENTRIES,
    OtValidationError,
    compile_studio_draft,
    minimal_studio_draft,
    read_ot,
    validate_studio_draft,
)


def _rewrite_archive(raw: bytes, changes: dict[str, bytes], additions: dict[str, bytes] | None = None) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(raw))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as target:
        for item in source.infolist():
            payload = changes.get(item.filename, source.read(item.filename))
            clone = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            clone.create_system = item.create_system
            clone.external_attr = item.external_attr
            clone.compress_type = zipfile.ZIP_STORED
            target.writestr(clone, payload)
        for name, payload in (additions or {}).items():
            target.writestr(name, payload)
    return output.getvalue()


class OtFormatTests(unittest.TestCase):
    def test_compile_is_byte_deterministic_and_round_trips(self) -> None:
        first_raw, first = compile_studio_draft(minimal_studio_draft())
        second_raw, second = compile_studio_draft(minimal_studio_draft())

        self.assertEqual(first_raw, second_raw)
        self.assertEqual(first.content_identity, second.content_identity)
        loaded = read_ot(first_raw, for_execution=True)
        self.assertEqual(loaded.content_identity, first.content_identity)
        self.assertTrue(loaded.read_text("resources/workflow.json"))

    def test_studio_rejects_a_kind_the_research_runtime_cannot_import(self) -> None:
        draft = minimal_studio_draft()
        draft["package"]["kind"] = "research_workflow"

        diagnostics = validate_studio_draft(draft)

        self.assertIn("OT_PACKAGE_KIND", {item.code for item in diagnostics})
        with self.assertRaises(OtValidationError):
            compile_studio_draft(draft)

    def test_file_extension_is_part_of_the_public_contract(self) -> None:
        raw, _ = compile_studio_draft(minimal_studio_draft())
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "workflow.othesis"
            legacy.write_bytes(raw)
            with self.assertRaises(OtValidationError) as caught:
                read_ot(legacy)
        self.assertEqual(caught.exception.diagnostics[0].code, "OT_EXTENSION_REQUIRED")

    def test_draft_rejects_secrets_before_compilation(self) -> None:
        draft = minimal_studio_draft()
        draft["workflow"]["steps"][0]["prompt"] = "Use api_key=sk-secret-value"
        diagnostics = validate_studio_draft(draft)
        self.assertIn("OT_SECRET_DETECTED", {item.code for item in diagnostics})
        with self.assertRaises(OtValidationError):
            compile_studio_draft(draft)

    def test_runtime_rejects_hash_mismatch_and_undeclared_files(self) -> None:
        raw, package = compile_studio_draft(minimal_studio_draft())
        prompt_path = package.manifest["resources"][0]["path"]
        tampered = _rewrite_archive(
            raw,
            {prompt_path: b"tampered\n"},
            {"resources/unlisted.txt": b"not declared"},
        )
        with self.assertRaises(OtValidationError) as caught:
            read_ot(tampered, for_execution=True)
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("OT_RESOURCE_HASH", codes)
        self.assertIn("OT_RESOURCE_UNDECLARED", codes)

    def test_unknown_required_capability_is_diagnostic_and_blocks_execution(self) -> None:
        raw, _ = compile_studio_draft(minimal_studio_draft())
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest["required_capabilities"].append("vendor.future-capability.v9")
        manifest["integrity"]["content_identity"] = ""
        inspection_raw = _rewrite_archive(
            raw,
            {"manifest.json": json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()},
        )
        with self.assertRaises(OtValidationError) as inspected:
            read_ot(inspection_raw)
        self.assertIn("OT_CAPABILITY_UNKNOWN", {item.code for item in inspected.exception.diagnostics})
        with self.assertRaises(OtValidationError) as executed:
            read_ot(inspection_raw, for_execution=True)
        severity = {item.code: item.severity for item in executed.exception.diagnostics}
        self.assertEqual(severity["OT_CAPABILITY_UNKNOWN"], "error")

    def test_lockfile_is_required_and_bound_to_manifest_resources(self) -> None:
        raw, _ = compile_studio_draft(minimal_studio_draft())
        broken = _rewrite_archive(
            raw,
            {"ot.lock.json": json.dumps({
                "ot_version": "1.0",
                "package_id": "my.company-research",
                "package_version": "1.0.0",
                "dependencies": [],
                "resources": [],
            }, sort_keys=True, separators=(",", ":")).encode()},
        )
        with self.assertRaises(OtValidationError) as caught:
            read_ot(broken, for_execution=True)
        self.assertIn("OT_LOCKFILE_MISMATCH", {item.code for item in caught.exception.diagnostics})

    def test_container_scans_manifest_for_secrets_and_enforces_filesystem_policy(self) -> None:
        raw, _ = compile_studio_draft(minimal_studio_draft())
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest["permissions"]["filesystem"] = "read-write"
        manifest["optional_extensions"]["vendor"] = {"api_key": "sk-this-must-never-ship"}
        changed = _rewrite_archive(
            raw,
            {"manifest.json": json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()},
        )
        with self.assertRaises(OtValidationError) as caught:
            read_ot(changed, for_execution=True)
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("OT_FILESYSTEM_FORBIDDEN", codes)
        self.assertIn("OT_SECRET_DETECTED", codes)

    def test_studio_rejects_out_of_range_settings_cycles_and_unknown_outputs(self) -> None:
        draft = minimal_studio_draft()
        draft["settings"]["risk_emphasis"] = 6
        draft["workflow"]["steps"][0]["depends_on"] = ["verification"]
        draft["outputs"]["formats"].append("executable")
        diagnostics = validate_studio_draft(draft)
        codes = {item.code for item in diagnostics}
        self.assertIn("OT_SETTING_RANGE", codes)
        self.assertIn("OT_DEPENDENCY_CYCLE", codes)
        self.assertIn("OT_OUTPUT_FORMAT", codes)

    def test_entry_budget_is_enforced_before_extraction(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for index in range(MAX_ENTRIES + 1):
                archive.writestr(f"resources/{index}.txt", b"x")
        with self.assertRaises(OtValidationError) as caught:
            read_ot(output.getvalue())
        self.assertEqual(caught.exception.diagnostics[0].code, "OT_ENTRY_BUDGET")


if __name__ == "__main__":
    unittest.main()
