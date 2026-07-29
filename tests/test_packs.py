from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from openthesis.packs import PackValidationError, builtin_pack, install_pack


class ResearchPackTests(unittest.TestCase):
    def test_builtin_pack_loads(self) -> None:
        pack = builtin_pack()
        self.assertEqual(pack.pack_id, "official.long-term-fundamentals")
        self.assertTrue(pack.prompt("prompts/financial-analyst.md"))

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.othesis"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../evil.md", "bad")
            with self.assertRaises(PackValidationError):
                install_pack(archive, root / "packs")

    def test_installs_safe_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "safe.othesis"
            manifest = {
                "api_version": "openthesis.io/v1alpha1",
                "kind": "ResearchPack",
                "metadata": {"id": "test.pack", "name": "Test", "version": "1.0.0"},
                "permissions": {
                    "network": False,
                    "filesystem": False,
                    "execute_code": False,
                },
            }
            workflow = {
                "workflow": {
                    "id": "test",
                    "steps": [
                        {"id": "one", "prompt": "prompts/one.md"},
                    ],
                }
            }
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("manifest.yaml", json.dumps(manifest))
                package.writestr("workflow.yaml", json.dumps(workflow))
                package.writestr("prompts/one.md", "Return JSON.")
            installed = install_pack(archive, root / "packs")
            self.assertEqual(installed.pack_id, "test.pack")


if __name__ == "__main__":
    unittest.main()

