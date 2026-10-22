from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from openthesis.market_capabilities import DEFAULT_MARKET_CAPABILITIES
from openthesis.research_continuity import RESEARCH_STAGE_KINDS, StageOutcome


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT
CORE_FILES = (
    "comparison.py", "domain.py", "filing_parser.py", "financials.py",
    "growth.py", "i18n.py", "model_catalog.py", "onboarding.py", "packs.py",
    "paths.py", "providers.py", "reporting.py", "research.py", "sec_client.py",
    "service.py", "sidecar.py", "storage.py",
)


class ArchitectureContractTests(unittest.TestCase):
    @staticmethod
    def _frontend_sources() -> list[Path]:
        frontend_root = PROJECT_ROOT / "desktop" / "src"
        return sorted(
            path for path in frontend_root.rglob("*")
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
            path.read_text(encoding="utf-8") for path in self._frontend_sources()
        )
        for forbidden in (
            "node:child_process", "std::process", "C:\\\\", "D:\\\\",
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
            "features/about/AboutView", "features/report/ReportWorkspace",
            "features/research/NewResearchView", "features/settings/SettingsView",
            "features/thesis/ThesisView", "app/useWorkbenchSession",
        ):
            self.assertIn(module, app_source)

    def test_windows_gui_attribute_is_explicitly_target_gated(self) -> None:
        main_source = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "src" / "main.rs"
        ).read_text(encoding="utf-8")
        self.assertIn('target_os = "windows"', main_source)
        self.assertIn('windows_subsystem = "windows"', main_source)

    def test_macos_bundle_override_preserves_same_frontend_and_core_resources(self) -> None:
        config_path = PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.macos.conf.json"
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
        vite_config = (PROJECT_ROOT / "desktop" / "vite.config.ts").read_text(
            encoding="utf-8"
        )
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
        package_script = (PROJECT_ROOT / "scripts" / "package-desktop.ps1").read_text(
            encoding="utf-8"
        )
        privacy_script = PROJECT_ROOT / "scripts" / "verify-release-privacy.ps1"
        self.assertTrue(privacy_script.is_file())
        self.assertIn("verify-release-privacy.ps1", package_script)
        privacy_contract = privacy_script.read_text(encoding="utf-8")
        self.assertIn(r"openthesis\.db", privacy_contract)
        self.assertIn("PRIVATE KEY", privacy_contract)
        self.assertIn(r"C:\\Users\\", privacy_contract)

    def test_core_continuity_vocabulary_is_typed_and_unique(self) -> None:
        self.assertEqual(len(RESEARCH_STAGE_KINDS), len(set(RESEARCH_STAGE_KINDS)))
        self.assertGreaterEqual(len(RESEARCH_STAGE_KINDS), 10)
        self.assertIn("final-synthesis", RESEARCH_STAGE_KINDS)
        self.assertIn("report-rendering", RESEARCH_STAGE_KINDS)
        self.assertEqual(
            {item.value for item in StageOutcome},
            {
                "complete", "degraded", "waiting_retryable", "needs_action",
                "blocked_integrity", "cancelled", "waiting_external", "exhausted", "failed_internal",
            },
        )

    def test_supported_markets_are_registered_through_one_capability_seam(self) -> None:
        for market in ("US", "HK", "CN_A"):
            capability = DEFAULT_MARKET_CAPABILITIES.require(market)
            self.assertEqual(capability.market, market)
            self.assertTrue(capability.official_authority)

        service_source = (ROOT / "src/openthesis/service.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_MARKET_CAPABILITIES.require", service_source)

    def test_research_delegates_provider_calls_to_scheduled_model_stage_executor(self) -> None:
        research_path = ROOT / "src/openthesis/research.py"
        research_tree = ast.parse(research_path.read_text(encoding="utf-8"))

        def direct_provider_calls(tree: ast.AST) -> list[ast.Call]:
            return [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "generate"
                and (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "provider"
                    or isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "provider"
                )
            ]

        self.assertEqual(direct_provider_calls(research_tree), [])

        model_path = ROOT / "src/openthesis/research_execution/model.py"
        model_tree = ast.parse(model_path.read_text(encoding="utf-8"))
        provider_calls = direct_provider_calls(model_tree)
        self.assertEqual(len(provider_calls), 1)

        parents = {
            child: parent
            for parent in ast.walk(model_tree)
            for child in ast.iter_child_nodes(parent)
        }
        ancestors: list[ast.AST] = []
        parent = parents.get(provider_calls[0])
        while parent is not None:
            ancestors.append(parent)
            parent = parents.get(parent)
        self.assertTrue(
            any(isinstance(node, ast.ClassDef) and node.name == "ModelStageExecutor"
                for node in ancestors),
            "the model-stage executor must own the research provider invocation",
        )
        guarded = any(
            isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "slot"
                and isinstance(item.context_expr.func.value, ast.Attribute)
                and item.context_expr.func.value.attr == "scheduler"
                for item in node.items
            )
            and any(descendant is provider_calls[0] for descendant in ast.walk(node))
            for node in ast.walk(model_tree)
        )
        self.assertTrue(guarded, "ModelStageExecutor provider call must be inside its scheduler slot")

    def test_artifact_store_is_append_only_and_report_pointer_is_cas_guarded(self) -> None:
        source = (ROOT / "src/openthesis/storage.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERT OR REPLACE INTO research_artifacts", source.upper())
        self.assertIn("ARTIFACT_IDENTITY_CONFLICT", source)
        self.assertIn("REPORT_REVISION_CONFLICT", source)
        self.assertIn("expected_generation", source)

    def test_financial_period_rejection_is_never_accession_wide(self) -> None:
        source = (ROOT / "src/openthesis/financial_ingestion.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("rejected_accessions", source)
        self.assertIn("_fact_period_identity", source)

    def test_continuity_notice_cannot_masquerade_as_a_research_report(self) -> None:
        source = (ROOT / "src/openthesis/research_continuity.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        assigned_artifact_types = {
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "artifact_type"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        self.assertIn("continuity-notice", assigned_artifact_types)
        self.assertNotIn("research-report", assigned_artifact_types)

    def test_financial_source_and_compiler_dependencies_respect_layer_boundaries(self) -> None:
        source_root = ROOT / "src/openthesis"

        def imports_for(path: Path) -> list[tuple[int, str]]:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative_parts = path.relative_to(source_root).with_suffix("").parts
            package_parts = ("openthesis", *relative_parts[:-1])
            imports: list[tuple[int, str]] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend((node.lineno, alias.name) for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        base = package_parts[:len(package_parts) - node.level + 1]
                        if node.module:
                            imported = [".".join((*base, *node.module.split(".")))]
                        else:
                            imported = [".".join((*base, alias.name)) for alias in node.names]
                    else:
                        imported = [node.module or ""]
                    imports.extend((node.lineno, name) for name in imported)
            return imports

        def matches(module: str, forbidden: tuple[str, ...]) -> bool:
            return any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden)

        adapters_root = source_root / "financial_sources"
        adapter_forbidden = (
            "openthesis.financial_compiler",
            "openthesis.service",
        )
        adapter_violations = [
            f"{path.relative_to(ROOT)}:{line}:{module}"
            for path in sorted(adapters_root.rglob("*.py"))
            for line, module in imports_for(path)
            if matches(module, adapter_forbidden)
        ]
        self.assertEqual(adapter_violations, [])

        compiler_path = source_root / "financial_compiler.py"
        compiler_forbidden = (
            "openthesis.financial_ingestion",
            "openthesis.providers",
            "openthesis.financial_sources",
            "openthesis.sec_client",
            "openthesis.market_data",
            "openthesis.filing_parser",
            "openthesis.disclosure_index",
            "openthesis.financial_inline_xbrl",
            "openthesis.vision_financials",
            "openthesis.service",
            "requests",
            "urllib",
            "httpx",
            "sqlite3",
            "pathlib",
            "socket",
        )
        compiler_violations = [
            f"{compiler_path.relative_to(ROOT)}:{line}:{module}"
            for line, module in imports_for(compiler_path)
            if matches(module, compiler_forbidden)
        ]
        self.assertEqual(compiler_violations, [])

    def test_public_recovery_operations_share_one_service_execution_seam(self) -> None:
        service_source = (ROOT / "src/openthesis/service.py").read_text(encoding="utf-8")
        self.assertNotIn("build_financial_profile(", service_source)
        tree = ast.parse(service_source)
        public_retries = {
            node.name: ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name in {
                "retry_research_model_stages",
                "retry_research_growth",
                "retry_research_synthesis",
            }
        }
        self.assertEqual(len(public_retries), 3)
        for source in public_retries.values():
            self.assertIn("_execute_recovery_plan", source)

    def test_growth_stage_has_no_implicit_model_repair_call(self) -> None:
        source = (ROOT / "src/openthesis/research.py").read_text(encoding="utf-8")
        self.assertNotIn("Return one complete growth-opportunity", source)
        self.assertNotIn("正在进行一次修复", source)

    def test_build_identity_resources_and_packager_are_bom_safe(self) -> None:
        for relative in (
            "src/openthesis/resources/build-info.json",
            "desktop/src-tauri/resources/build-info.json",
        ):
            payload = (ROOT / relative).read_bytes()
            self.assertTrue(payload.startswith(b"{"), relative)
            self.assertFalse(payload.startswith(b"\xef\xbb\xbf"), relative)
            self.assertEqual(json.loads(payload)["build_id"], "development")
        package_source = (ROOT / "scripts/package-desktop.ps1").read_text(encoding="utf-8")
        self.assertIn("System.Text.UTF8Encoding($false)", package_source)
        self.assertIn("Write-Utf8NoBom", package_source)


if __name__ == "__main__":
    unittest.main()
