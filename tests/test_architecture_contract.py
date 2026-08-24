from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = (
    "comparison.py",
    "domain.py",
    "filing_parser.py",
    "financials.py",
    "growth.py",
    "i18n.py",
    "model_catalog.py",
    "onboarding.py",
    "packs.py",
    "paths.py",
    "providers.py",
    "reporting.py",
    "research.py",
    "sec_client.py",
    "service.py",
    "sidecar.py",
    "storage.py",
)


class ArchitectureContractTests(unittest.TestCase):
    @staticmethod
    def _frontend_sources() -> list[Path]:
        frontend_root = PROJECT_ROOT / "desktop" / "src"
        return sorted(
            path
            for path in frontend_root.rglob("*")
            if path.is_file() and path.suffix in {".ts", ".tsx"}
        )

    def test_python_research_core_does_not_import_desktop_ui(self) -> None:
        forbidden_roots = {"tkinter", "tkinterweb", "openthesis.app"}
        violations: list[str] = []
        source_root = PROJECT_ROOT / "src" / "openthesis"
        for name in CORE_FILES:
            path = source_root / name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [node.module or ""]
                for module in imported:
                    if any(
                        module == root or module.startswith(f"{root}.")
                        for root in forbidden_roots
                    ):
                        violations.append(f"{name}:{node.lineno}:{module}")
        self.assertEqual(violations, [])

    def test_react_workbench_has_no_native_process_or_machine_path_access(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self._frontend_sources()
        )
        for forbidden in (
            "node:child_process",
            "std::process",
            "C:\\\\",
            "D:\\\\",
            "OPENTHESIS_SIDECAR_PATH",
        ):
            self.assertNotIn(forbidden, source)

    def test_native_export_and_external_links_stay_in_the_tauri_adapter(self) -> None:
        frontend_source = "\n".join(
            path.read_text(encoding="utf-8") for path in self._frontend_sources()
        )
        rust_adapter = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "src" / "lib.rs"
        ).read_text(encoding="utf-8")

        self.assertNotIn("@tauri-apps/plugin-dialog", frontend_source)
        self.assertNotIn("@tauri-apps/plugin-opener", frontend_source)
        self.assertIn("async fn export_report", rust_adapter)
        self.assertIn("fn open_external_url", rust_adapter)
        self.assertIn("only secure external links are allowed", rust_adapter)

    def test_tauri_transport_imports_are_local_to_the_desktop_adapter(self) -> None:
        violations = []
        for path in self._frontend_sources():
            if path.name == "backend.ts":
                continue
            if "@tauri-apps/" in path.read_text(encoding="utf-8"):
                violations.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(violations, [])

    def test_workbench_shell_delegates_feature_implementations(self) -> None:
        app_source = (PROJECT_ROOT / "desktop" / "src" / "App.tsx").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('from "./backend"', app_source)
        for module in (
            "features/about/AboutView",
            "features/report/ReportWorkspace",
            "features/research/NewResearchView",
            "features/settings/SettingsView",
            "features/thesis/ThesisView",
            "app/useWorkbenchSession",
        ):
            self.assertIn(module, app_source)

    def test_windows_gui_attribute_is_explicitly_target_gated(self) -> None:
        main_source = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "src" / "main.rs"
        ).read_text(encoding="utf-8")
        self.assertIn('target_os = "windows"', main_source)
        self.assertIn('windows_subsystem = "windows"', main_source)

    def test_macos_bundle_override_preserves_same_frontend_and_core_resources(self) -> None:
        config_path = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.macos.conf.json"
        )
        self.assertTrue(config_path.is_file(), "macOS Tauri override is missing")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["bundle"]["targets"], ["app", "dmg"])
        self.assertEqual(config["bundle"]["resources"], {"resources/": ""})

    def test_mobile_remains_outside_the_desktop_architecture_scope(self) -> None:
        adr = (
            PROJECT_ROOT / "docs" / "adr" / "0001-tauri-desktop-python-core.md"
        ).read_text(encoding="utf-8")
        rust_adapter = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "src" / "lib.rs"
        ).read_text(encoding="utf-8")
        self.assertIn("Mobile is explicitly outside the 1.0 scope.", adr)
        self.assertNotIn("cfg_attr(mobile", rust_adapter)

    def test_provider_logos_are_emitted_as_local_files_under_strict_csp(self) -> None:
        vite_config = (
            PROJECT_ROOT / "desktop" / "vite.config.ts"
        ).read_text(encoding="utf-8")
        tauri_config = json.loads(
            (PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
                encoding="utf-8"
            )
        )
        image_policy = tauri_config["app"]["security"]["csp"]

        self.assertIn("assetsInlineLimit: 0", vite_config)
        self.assertIn("img-src 'self'", image_policy)
        self.assertNotIn("data:", image_policy)

    def test_portable_packaging_runs_an_archive_privacy_gate(self) -> None:
        package_script = (
            PROJECT_ROOT / "scripts" / "package-desktop.ps1"
        ).read_text(encoding="utf-8")
        privacy_script = PROJECT_ROOT / "scripts" / "verify-release-privacy.ps1"

        self.assertTrue(privacy_script.is_file())
        self.assertIn("verify-release-privacy.ps1", package_script)
        privacy_contract = privacy_script.read_text(encoding="utf-8")
        self.assertIn(r"openthesis\.db", privacy_contract)
        self.assertIn("PRIVATE KEY", privacy_contract)
        self.assertIn(r"C:\\Users\\", privacy_contract)


if __name__ == "__main__":
    unittest.main()
