from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from openthesis.ot import compile_studio_draft, minimal_studio_draft
from openthesis.packs import PackValidationError, builtin_pack, install_pack


class ResearchPackTests(unittest.TestCase):
    def test_builtin_pack_loads(self) -> None:
        pack = builtin_pack()
        self.assertEqual(pack.pack_id, "official.long-term-fundamentals")
        self.assertTrue(pack.prompt("prompts/financial-analyst.md"))
        self.assertEqual(pack.manifest["package"]["kind"], "openthesis.research-pack")

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.ot"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../evil.md", "bad")
            with self.assertRaises(PackValidationError) as caught:
                install_pack(archive, root / "packs")
            self.assertIn(
                "OT_PATH_TRAVERSAL",
                {item.code for item in caught.exception.diagnostics},
            )

    def test_rejects_legacy_othesis_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, _ = compile_studio_draft(minimal_studio_draft())
            archive = root / "legacy.othesis"
            archive.write_bytes(raw)
            with self.assertRaises(PackValidationError) as caught:
                install_pack(archive, root / "packs")
            self.assertEqual(caught.exception.diagnostics[0].code, "OT_EXTENSION_REQUIRED")

    def test_installs_safe_ot_atomically_by_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "safe.ot"
            draft = minimal_studio_draft()
            draft["package"]["id"] = "test.pack"
            draft["package"]["name"] = "Test"
            raw, compiled = compile_studio_draft(draft)
            archive.write_bytes(raw)

            installed = install_pack(archive, root / "packs")
            installed_again = install_pack(archive, root / "packs")

            self.assertEqual(installed.pack_id, "test.pack")
            self.assertEqual(installed.content_hash, compiled.content_identity)
            self.assertEqual(installed_again.content_hash, installed.content_hash)
            installed_paths = list((root / "packs").glob("*/*/*.ot"))
            self.assertEqual(len(installed_paths), 1)
            self.assertEqual(installed_paths[0].name, f"{compiled.content_identity}.ot")


if __name__ == "__main__":
    unittest.main()
