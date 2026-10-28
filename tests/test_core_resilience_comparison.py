from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_core_resilience", ROOT / "scripts/compare-core-resilience.py"
)
assert SPEC is not None and SPEC.loader is not None
comparison = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparison)


class CoreResilienceComparisonTests(unittest.TestCase):
    def test_source_read_failure_preserves_diagnostic_instead_of_missing_counter_error(self) -> None:
        baseline = json.loads(comparison.BASELINE_PATH.read_text(encoding="utf-8"))
        denied = PermissionError(13, "read denied", "isolated-history.db")
        with patch.object(comparison.FREEZER, "freeze_corpus", return_value=baseline["golden_samples"]), \
                patch.object(comparison, "_read_source_snapshot", side_effect=denied):
            result = comparison.compare_baseline(
                comparison.BASELINE_PATH, ROOT / "tmp" / "absent-history-for-read-failure.db")
        self.assertEqual(result["state"], "FAIL")
        history = result["historical_reports"]
        self.assertEqual(history["actual_public_read_calls"], 0)
        self.assertEqual(history["provider_calls"], 0)
        self.assertEqual(history["language_read_failed"], 0)
        self.assertFalse(history["all_historical_reads_pass"])
        failure = next(item for item in history["issues"]
                       if item["code"] == "HISTORY_COMPARISON_FAILED")
        self.assertEqual(failure["error_type"], "PermissionError")
        self.assertIn("isolated-history.db", failure["error_detail"])

    def test_diagnostic_only_historical_read_is_not_counted_as_pass(self) -> None:
        expected = {
            "run_id": "run-1",
            "company_cik": "US:NYSE:TEST",
            "status": "completed",
            "markdown_sha256": "c8a09b23aa45dc9374fcc605931fbf586fa62a48121fd46fec455fac332eeefb",
            "visible_sections": [{
                "section_id": "executive_summary",
                "source_artifact_ids": ["run-1:research-report:abc"],
                "is_substantive": True,
            }],
        }
        actual = {
            "run_id": "run-1",
            "company_cik": "US:NYSE:TEST",
            "ticker": "US:NYSE:TEST",
            "status": "completed",
            "markdown": "No analysis available.",
            "read_state": "diagnostic_only",
            "is_substantive": False,
            "visible_sections": [{
                "section_id": "executive_summary",
                "source_artifact_ids": ["run-1:research-report:abc"],
                "is_substantive": False,
                "is_visible": True,
            }],
        }

        result = comparison.compare_historical_report(expected, actual)

        self.assertEqual(result["state"], "PENDING")
        self.assertFalse(result["counted_as_pass"])
        self.assertEqual(result["sections"][0]["role"], "unknown")
        self.assertIn({
            "code": "LEGACY_SECTION_ROLE_UNPROVEN",
            "run_id": "run-1",
            "section_id": "executive_summary",
            "artifact_ids": ["run-1:research-report:abc"],
        }, result["issues"])
        self.assertIn("HISTORICAL_READ_DIAGNOSTIC_ONLY", [
            item["code"] for item in result["issues"]
        ])

    def test_explicit_placeholder_role_cannot_be_substantive(self) -> None:
        expected = {
            "run_id": "run-2", "company_cik": "CN:SZSE:TEST", "status": "partial",
            "markdown_sha256": "body-hash", "visible_sections": [],
        }
        actual = {
            "run_id": "run-2", "ticker": "CN:SZSE:TEST", "status": "partial",
            "markdown": "Rendered text is not used to infer the role.",
            "read_state": "partial", "is_substantive": True,
            "visible_sections": [{
                "section_id": "thesis", "source_artifact_ids": ["run-2:research-report:def"],
                "role": "placeholder", "is_substantive": True, "is_visible": True,
            }],
        }

        result = comparison.compare_historical_report(expected, actual)

        self.assertEqual(result["state"], "FAIL")
        self.assertIn({
            "code": "PLACEHOLDER_COUNTED_SUBSTANTIVE",
            "run_id": "run-2", "section_id": "thesis",
            "artifact_ids": ["run-2:research-report:def"],
        }, result["issues"])

    def test_source_backed_analysis_role_preserves_unknown_validation_state(self) -> None:
        markdown = "Readable legacy analysis."
        expected = {
            "run_id": "run-4", "company_cik": "US:NASDAQ:TEST", "status": "completed",
            "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "visible_sections": [{
                "section_id": "thesis", "source_artifact_ids": ["run-4:research-report:ghi"],
            }],
        }
        actual = {
            "run_id": "run-4", "company_cik": "US:NASDAQ:TEST", "status": "completed",
            "markdown": markdown, "read_state": "partial", "is_substantive": True,
            "visible_sections": [{
                "section_id": "thesis",
                "source_artifact_ids": ["run-4:research-report:ghi"],
                "is_visible": True, "is_substantive": True,
                "content_roles": ["analysis"],
                "content_semantics": [{
                    "kind": "paragraph", "role": "analysis", "source_ids": ["source-1"],
                    "materiality": "material", "validation_state": "unknown",
                    "validation_source_artifact_id": "",
                }],
            }],
        }

        result = comparison.compare_historical_report(expected, actual)

        self.assertEqual(result["state"], "PASS")
        self.assertTrue(result["sections"][0]["role_proven"])
        self.assertTrue(result["sections"][0]["is_visible"])
        self.assertTrue(result["sections"][0]["is_substantive"])
        self.assertEqual(
            result["sections"][0]["content_semantics"][0]["validation_state"],
            "unknown",
        )

    def test_analysis_role_without_source_semantics_remains_pending_and_visible(self) -> None:
        markdown = "Readable legacy analysis."
        expected = {
            "run_id": "run-5", "company_cik": "US:NASDAQ:TEST", "status": "completed",
            "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "visible_sections": [{
                "section_id": "thesis", "source_artifact_ids": ["run-5:research-report:jkl"],
            }],
        }
        actual = {
            "run_id": "run-5", "company_cik": "US:NASDAQ:TEST", "status": "completed",
            "markdown": markdown, "read_state": "partial", "is_substantive": True,
            "visible_sections": [{
                "section_id": "thesis", "source_artifact_ids": ["run-5:research-report:jkl"],
                "is_visible": True, "is_substantive": True,
                "content_roles": ["analysis"],
                "content_semantics": [{
                    "kind": "paragraph", "role": "analysis", "source_ids": [],
                    "materiality": "material", "validation_state": "unknown",
                    "validation_source_artifact_id": "",
                }],
            }],
        }

        result = comparison.compare_historical_report(expected, actual)

        self.assertEqual(result["state"], "PENDING")
        self.assertTrue(result["sections"][0]["is_visible"])
        self.assertTrue(result["sections"][0]["is_substantive"])
        self.assertIn("LEGACY_SECTION_ROLE_UNPROVEN", [item["code"] for item in result["issues"]])

    def test_corpus_diff_identifies_the_changed_sample_and_field(self) -> None:
        expected = [{
            "ticker": "US:NYSE:TEST", "market": "US", "sample_status": "audited",
            "fact_count": 40, "run_id": "golden-run", "sample_path": "sample/test",
            "artifact_byte_hashes": {"financial_facts.json": "old-hash"},
            "originals": [{"source_id": "filing-1", "sha256": "source-hash", "bytes": 12}],
        }]
        actual = [{**expected[0], "artifact_byte_hashes": {"financial_facts.json": "new-hash"}}]

        result = comparison.compare_golden_samples(expected, actual)

        self.assertFalse(result["matches"])
        self.assertEqual(result["differences"], [{
            "ticker": "US:NYSE:TEST",
            "field": "artifact_byte_hashes.financial_facts.json",
            "expected": "old-hash", "actual": "new-hash",
        }])

    def test_visible_non_material_metadata_extension_does_not_change_frozen_body(self) -> None:
        markdown = "The original report already rendered this notice."
        expected = {
            "run_id": "run-notice", "company_cik": "HK:TEST", "status": "partial",
            "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "visible_sections": [],
        }
        actual = {
            **expected, "markdown": markdown, "read_state": "partial",
            "visible_sections": [{
                "section_id": "methodology", "source_artifact_ids": [],
                "is_visible": True, "is_substantive": False,
                "content_roles": ["notice"],
                "content_semantics": [{
                    "kind": "paragraph", "role": "notice", "source_ids": [],
                    "materiality": "non_material", "validation_state": "unknown",
                    "validation_source_artifact_id": "",
                }],
            }],
        }
        result = comparison.compare_historical_report(expected, actual)
        self.assertEqual(result["state"], "PASS")
        self.assertTrue(result["body_matches"])
        self.assertTrue(result["sections"][0]["role_proven"])
        self.assertEqual(result["visibility_extensions"], ["methodology"])

    def test_added_material_section_still_requires_review_even_if_body_matches(self) -> None:
        markdown = "Frozen analysis."
        expected = {
            "run_id": "run-added", "company_cik": "HK:TEST", "status": "completed",
            "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "visible_sections": [],
        }
        actual = {
            **expected, "markdown": markdown, "read_state": "partial",
            "visible_sections": [{
                "section_id": "thesis", "source_artifact_ids": ["artifact-1"],
                "is_visible": True, "is_substantive": True,
                "content_roles": ["analysis"],
                "content_semantics": [{
                    "kind": "paragraph", "role": "analysis", "source_ids": ["source-1"],
                    "materiality": "material", "validation_state": "unknown",
                    "validation_source_artifact_id": "",
                }],
            }],
        }
        result = comparison.compare_historical_report(expected, actual)
        self.assertEqual(result["state"], "PENDING")
        self.assertIn("HISTORICAL_MATERIAL_VISIBILITY_CHANGED", [item["code"] for item in result["issues"]])

    def test_non_material_extension_cannot_hide_missing_original_analysis(self) -> None:
        markdown = "Frozen analysis."
        expected = {
            "run_id": "run-missing", "company_cik": "HK:TEST", "status": "completed",
            "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "visible_sections": [{"section_id": "thesis", "is_substantive": True}],
        }
        actual = {**expected, "markdown": markdown, "read_state": "partial", "visible_sections": []}
        result = comparison.compare_historical_report(expected, actual)
        self.assertEqual(result["state"], "FAIL")
        self.assertIn("HISTORICAL_SECTION_MISSING", [item["code"] for item in result["issues"]])

    def test_non_frozen_language_body_is_pending_not_a_regression(self) -> None:
        expected = {
            "run_id": "run-3", "company_cik": "HK:TEST", "status": "completed",
            "markdown_sha256": "frozen-default-language-hash", "visible_sections": [],
        }
        actual = {
            "run_id": "run-3", "company_cik": "HK:TEST", "status": "completed",
            "markdown": "Different language rendering", "read_state": "partial",
            "is_substantive": True, "visible_sections": [],
        }

        result = comparison.compare_historical_report(expected, actual, compare_body=False)

        self.assertEqual(result["state"], "PENDING")
        self.assertIsNone(result["body_matches"])
        self.assertNotIn("HISTORICAL_BODY_CHANGED", [item["code"] for item in result["issues"]])

    def test_every_frozen_run_is_read_in_all_three_languages(self) -> None:
        expected_runs = []
        source_runs = []
        default_languages = {}
        for index in range(12):
            run_id = f"run-{index:02}"
            cik = f"HK:TEST:{index:02}"
            default_markdown = f"{run_id}|zh-CN"
            expected_runs.append({
                "run_id": run_id, "company_cik": cik, "status": "completed",
                "markdown_sha256": hashlib.sha256(default_markdown.encode("utf-8")).hexdigest(),
                "visible_sections": [],
            })
            source_runs.append({"run_id": run_id, "company_cik": cik, "ticker": cik})
            default_languages[run_id] = "zh-CN"

        class PublicReportService:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def get_report(self, run_id: str, *, language: str) -> dict[str, object]:
                self.calls.append((run_id, language))
                canonical_language = {"zh-TW": "zh-Hant"}.get(language, language)
                return {
                    "run_id": run_id,
                    "ticker": next(item["company_cik"] for item in source_runs if item["run_id"] == run_id),
                    "status": "completed",
                    "report_language": canonical_language,
                    "markdown": f"{run_id}|{canonical_language}",
                    "read_state": "partial",
                    "is_substantive": True,
                    "visible_sections": [],
                }

        service = PublicReportService()
        result = comparison.read_historical_reports(
            service, expected_runs, source_runs, default_languages
        )

        self.assertEqual(len(service.calls), 36)
        self.assertEqual(result["expected_public_read_calls"], 36)
        self.assertEqual(result["actual_public_read_calls"], 36)
        self.assertEqual(result["actual_runs"], 12)
        self.assertEqual(len(result["language_reads"]), 36)
        self.assertEqual(result["requested_language_counts"], {"zh-CN": 12, "zh-TW": 12, "en": 12})
        self.assertEqual(result["actual_language_counts"], {"zh-CN": 12, "zh-Hant": 12, "en": 12})
        self.assertEqual(len(result["runs"]), 12)
        self.assertTrue(all(item["state"] == "PASS" for item in result["runs"]))
        self.assertEqual(result["language_read_pending"], 24)
        self.assertEqual(result["language_read_failed"], 0)
        self.assertEqual(set(service.calls), {
            (run["run_id"], language)
            for run in expected_runs for language in ("zh-CN", "zh-TW", "en")
        })


if __name__ == "__main__":
    unittest.main()
