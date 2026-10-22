from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DesktopPackagingRegressionTests(unittest.TestCase):
    def test_tauri_stages_vcruntime_under_a_non_dll_name(self):
        spec = (PROJECT_ROOT / "OpenThesisSidecar.spec").read_text(encoding="utf-8")
        package_script = (PROJECT_ROOT / "scripts" / "package-desktop.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("exclude_binaries=True", spec)
        self.assertIn("bundle = COLLECT(", spec)
        self.assertIn('collect_all("opencc")', spec)
        self.assertIn("opencc_datas", spec)
        self.assertIn("VCRUNTIME|MSVCP", package_script)
        self.assertIn("MSVCP", package_script)
        self.assertIn("$sidecarRuntimeFiles", package_script)
        self.assertIn("$sidecarBundle", package_script)

        backend = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "src" / "backend.rs"
        ).read_text(encoding="utf-8")
        self.assertIn("VCRUNTIME140.bin", backend)
        self.assertIn('value.starts_with("MSVCP")', backend)
        self.assertIn("std::fs::rename", backend)
        tauri_config = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
        ).read_text(encoding="utf-8")
        installer_hooks = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "windows" / "installer-hooks.nsh"
        ).read_text(encoding="utf-8")
        self.assertIn('"installerHooks": "windows/installer-hooks.nsh"', tauri_config)
        self.assertIn("NSIS_HOOK_POSTINSTALL", installer_hooks)
        self.assertIn("VCRUNTIME140.bin", installer_hooks)
        self.assertIn("VCRUNTIME140_1.bin", installer_hooks)
        self.assertIn("MSVCP140.bin", installer_hooks)

    def test_portable_verifier_checks_embedded_runtime_layout(self):
        verifier = (
            PROJECT_ROOT / "scripts" / "verify-desktop-portable.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Portable ZIP is missing the VCRUNTIME runtime", verifier)
        self.assertIn("Portable ZIP contains a Tauri-only VCRUNTIME staging name", verifier)

        backend = (
            PROJECT_ROOT / "desktop" / "src-tauri" / "src" / "backend.rs"
        ).read_text(encoding="utf-8")
        self.assertIn("std::fs::rename", backend)
        self.assertIn('value.starts_with("VCRUNTIME")', backend)

    def test_privacy_gate_normalizes_only_the_versioned_archive_root(self):
        verifier = (
            PROJECT_ROOT / "scripts" / "verify-release-privacy.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("^OpenThesis-[^/]+/", verifier)
        self.assertIn("$reviewedEntryName", verifier)
        self.assertIn("$reviewedLicenseHashes[$reviewedEntryName]", verifier)

    def test_persistent_sidecar_stdout_uses_utf8_before_process_start(self):
        verifier = (
            PROJECT_ROOT / "scripts" / "verify-desktop-runtime.ps1"
        ).read_text(encoding="utf-8")
        start = verifier.index("function Start-PersistentSidecar {")
        end = verifier.index("\nfunction Invoke-PersistentSidecarRpc", start)
        sidecar_start = verifier[start:end]
        encoding_line = "$startInfo.StandardOutputEncoding = [Text.Encoding]::UTF8"

        self.assertIn(encoding_line, sidecar_start)
        self.assertLess(
            sidecar_start.index(encoding_line),
            sidecar_start.index("if (-not $process.Start())"),
        )

    def test_json_probe_reads_utf8_json_output(self):
        verifier = (
            PROJECT_ROOT / "scripts" / "verify-desktop-runtime.ps1"
        ).read_text(encoding="utf-8")
        start = verifier.index("function Invoke-JsonProbe {")
        end = verifier.index("\nfunction Start-PersistentSidecar", start)
        json_probe = verifier[start:end]

        self.assertIn(
            "Get-Content -LiteralPath $ProbeFiles.Output -Raw -Encoding UTF8 | ConvertFrom-Json",
            json_probe,
        )

if __name__ == "__main__":
    unittest.main()
