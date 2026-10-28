from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "historical_presentation_adjudications",
    ROOT / "scripts" / "historical-presentation-adjudications.py",
)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

COMPARISON_SPEC = importlib.util.spec_from_file_location(
    "compare_core_resilience",
    ROOT / "scripts" / "compare-core-resilience.py",
)
assert COMPARISON_SPEC is not None and COMPARISON_SPEC.loader is not None
comparison = importlib.util.module_from_spec(COMPARISON_SPEC)
COMPARISON_SPEC.loader.exec_module(comparison)


def _summary_artifact(*, latest: bool = True) -> dict:
    return {
        "artifact_type": "deterministic-financial-summary",
        "artifact_id": "run-1:deterministic-financial-summary:2" if latest else "run-1:deterministic-financial-summary:1",
        "content": {
            "interim_metrics": [
                {"year": 2025, "period": "Q1", "revenue_growth": 0.12},
                {"year": 2024, "period": "Q1", "revenue_growth": None},
            ],
        },
    }


def _valuation_artifact(
    *,
    artifact_type: str = "deterministic-valuation",
    artifact_id: str = "run-1:deterministic-valuation:1",
    currency: object = "USD",
    discount_rate: object = 0.1,
    horizon_years: object = 5,
    equity_market_value: object = 9_500_000_000,
) -> dict:
    expectations = {
        "currency": currency,
        "discount_rate": discount_rate,
        "horizon_years": horizon_years,
        "equity_market_value": equity_market_value,
    }
    if artifact_type == "research-report":
        content = {"report": {"implied_expectations": expectations}}
    else:
        content = {"implied_expectations": expectations}
    return {"artifact_type": artifact_type, "artifact_id": artifact_id, "content": content}


def _combined_bodies() -> tuple[str, str]:
    old = "\n".join([
        "# Frozen report",
        "| 期间 | 营业收入 | 同期增长 | 净利润 | 经营现金流 |",
        "| 2024 Q1 | 100 | 未提供 | 20 | 30 |",
        "- **其他：** USD",
        "- **其他：** 10%",
        "- **其他：** 5",
    ])
    new = "\n".join([
        "# Frozen report",
        "| 期间 | 营业收入 | 收入增长 | 净利润 | 经营现金流 |",
        "| 2024 Q1 | 100 | — | 20 | 30 |",
        "- **币种：** USD",
        "- **折现率：** 10%",
        "- **预测期限（年）：** 5",
        "- **权益市值：** 9,500,000,000",
    ])
    return old, new


class HistoricalPresentationAdjudicationTests(unittest.TestCase):
    def test_adjudicate_accepts_interim_header_and_missing_growth_only(self) -> None:
        old = "| 期间 | 营业收入 | 同期增长 | 净利润 | 经营现金流 |\n| 2024 Q1 | 100 | 未提供 | 20 | 30 |"
        new = "| 期间 | 营业收入 | 收入增长 | 净利润 | 经营现金流 |\n| 2024 Q1 | 100 | — | 20 | 30 |"

        result = verifier.adjudicate(old, new, [_summary_artifact()])

        self.assertEqual(result["contract"], verifier.VERSION)
        self.assertEqual(len(result["changes"]), 2)
        self.assertEqual(result["changes"][0]["proof"]["artifact_id"], "run-1:deterministic-financial-summary:2")
        self.assertEqual(result["changes"][1]["proof"]["value"], None)

    def test_adjudicate_accepts_latest_valuation_and_research_report_lanes(self) -> None:
        old = "\n".join([
            "- **其他：** USD",
            "- **其他：** 10%",
            "- **其他：** 5",
        ])
        new = "\n".join([
            "- **币种：** USD",
            "- **折现率：** 10%",
            "- **预测期限（年）：** 5",
        ])
        result = verifier.adjudicate(old, new, [_valuation_artifact()])
        self.assertEqual(len(result["changes"]), 3)

        wrapped = verifier.adjudicate(
            "- **其他：** CNY", "- **币种：** CNY",
            [_valuation_artifact(artifact_type="research-report", artifact_id="report-1", currency="CNY")],
        )
        self.assertEqual(wrapped["changes"][0]["proof"]["artifact_id"], "report-1")

    def test_adjudicate_accepts_equity_market_value_from_same_source(self) -> None:
        result = verifier.adjudicate(
            "Frozen", "Frozen\n- **权益市值：** 9,500,000,000", [_valuation_artifact()]
        )
        self.assertEqual(result["changes"][0]["new_line"], "- **权益市值：** 9,500,000,000")

    def test_header_without_latest_summary_or_period_match_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "PRESENTATION_HEADING_SOURCE_UNPROVEN"):
            verifier.adjudicate(
                "| 期间 | 营业收入 | 同期增长 | 净利润 | 经营现金流 |",
                "| 期间 | 营业收入 | 收入增长 | 净利润 | 经营现金流 |",
                [{
                    "artifact_type": "deterministic-financial-summary",
                    "artifact_id": "summary-empty",
                    "content": {"interim_metrics": []},
                }],
            )
        with self.assertRaisesRegex(ValueError, "PRESENTATION_GROWTH_SOURCE_UNPROVEN"):
            verifier.adjudicate(
                "| 2024 Q1 | 100 | 未提供 | 20 | 30 |",
                "| 2024 Q1 | 100 | — | 20 | 30 |",
                [{
                    "artifact_type": "deterministic-financial-summary",
                    "artifact_id": "summary-old-period",
                    "content": {"interim_metrics": [{
                        "year": 2023, "period": "Q1", "revenue_growth": None,
                    }]},
                }],
            )

    def test_load_manifest_requires_fixed_adjudicated_bytes(self) -> None:
        manifest_path = ROOT / "docs" / "releases" / "core-resilience-3.0.0-presentation-errata.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        baseline = {
            "historical_reports": {
                "source_sha256": manifest["source_database_sha256"],
                "runs": [{"run_id": item["run_id"]} for item in manifest["entries"]],
            },
            "source_hashes": manifest["frozen_renderer_source_hashes"],
        }
        loaded = verifier.load_manifest(manifest_path, baseline)
        self.assertEqual(len(loaded["entries"]), 11)
        self.assertEqual(verifier.MANIFEST_SHA256, verifier.MANIFEST_SHA256.lower())

        mutated = deepcopy(manifest)
        mutated["review_evidence"]["sha256"] = "receipt-rewritten"
        with tempfile.TemporaryDirectory() as directory:
            changed_path = Path(directory) / manifest_path.name
            changed_path.write_text(json.dumps(mutated, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PRESENTATION_MANIFEST_NOT_ADJUDICATED"):
                verifier.load_manifest(changed_path, baseline)

    def test_compare_keeps_strict_fail_and_marks_only_frozen_language_adjudication(self) -> None:
        old, new = _combined_bodies()
        artifacts = [_summary_artifact(), _valuation_artifact()]
        adjudicated = verifier.adjudicate(old, new, artifacts)
        source_hashes = {item["artifact_id"]: f"hash-{index}" for index, item in enumerate(artifacts)}
        entry = {**adjudicated, "run_id": "run-1", "artifact_input_hashes": source_hashes}
        expected = [{
            "run_id": "run-1", "company_cik": "CN:SZSE:TEST", "status": "completed",
            "markdown_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
            "visible_sections": [],
        }]
        source_runs = [{"run_id": "run-1", "company_cik": "CN:SZSE:TEST", "ticker": "CN:SZSE:TEST"}]
        report_before = {
            "run_id": "run-1", "status": "completed", "ticker": "CN:SZSE:TEST",
            "report_language": "zh-CN", "markdown": new, "visible_sections": [],
        }
        report_snapshot = deepcopy(report_before)

        class Storage:
            def latest_report_revision(self, run_id):
                self.latest_run_id = run_id
                return {"payload": {}}

            def get_artifacts(self, run_id):
                self.artifact_run_id = run_id
                return artifacts

        class Service:
            def __init__(self):
                self.storage = Storage()
                self.reports = []

            def get_report(self, run_id, *, language):
                self.reports.append((run_id, language))
                return deepcopy({**report_before, "report_language": {
                    "zh-CN": "zh-CN", "zh-TW": "zh-Hant", "en": "en",
                }[language]})

        service = Service()
        result = comparison.read_historical_reports(
            service, expected, source_runs, {"run-1": "zh-CN"},
            presentation_entries={"run-1": entry},
            source_hashes={"run-1": source_hashes},
        )

        frozen = result["runs"][0]
        self.assertEqual(frozen["state"], "FAIL")
        self.assertIn("HISTORICAL_BODY_CHANGED", [item["code"] for item in frozen["issues"]])
        self.assertEqual(
            frozen["presentation_adjudication"]["state"],
            "PASS_APPROVED_PRESENTATION_ERRATUM",
        )
        self.assertEqual(result["actual_public_read_calls"], 3)
        self.assertEqual(result["language_read_pending"], 2)
        self.assertEqual(result["language_read_failed"], 1)
        self.assertEqual(service.reports, [("run-1", "zh-CN"), ("run-1", "zh-TW"), ("run-1", "en")])
        self.assertEqual(report_before, report_snapshot)

    def test_approved_comparison_accepts_only_exact_source_bound_changes(self) -> None:
        old, new = _combined_bodies()
        artifacts = [_summary_artifact(), _valuation_artifact()]
        adjudicated = verifier.adjudicate(old, new, artifacts)
        source_hashes = {item["artifact_id"]: f"hash-{index}" for index, item in enumerate(artifacts)}
        entry = {
            **adjudicated,
            "run_id": "run-1",
            "artifact_input_hashes": source_hashes,
        }
        strict = {
            "run_id": "run-1",
            "state": "FAIL",
            "issues": [{"code": "HISTORICAL_BODY_CHANGED"}],
            "markdown_sha256": hashlib.sha256(new.encode("utf-8")).hexdigest(),
        }
        expected = {"markdown_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest()}

        approved = verifier.approved_comparison(
            strict, expected, entry, source_hashes=source_hashes, artifacts=artifacts
        )
        self.assertEqual(approved["state"], "PASS_APPROVED_PRESENTATION_ERRATUM")
        self.assertEqual(strict["state"], "FAIL")

    def test_rejects_amount_currency_period_and_extra_body_changes(self) -> None:
        valuation = [_valuation_artifact()]
        with self.assertRaises(ValueError):
            verifier.adjudicate("Frozen", "Frozen\n- **权益市值：** 9,400,000,000", valuation)
        with self.assertRaises(ValueError):
            verifier.adjudicate("- **其他：** CNY", "- **币种：** USD", valuation)
        with self.assertRaises(ValueError):
            verifier.adjudicate(
                "| 2023 Q1 | 100 | 未提供 | 20 | 30 |",
                "| 2023 Q1 | 100 | — | 20 | 30 |",
                [_summary_artifact()],
            )
        with self.assertRaisesRegex(ValueError, "PRESENTATION_UNAPPROVED_ADDITION"):
            verifier.adjudicate("Frozen", "Frozen\n- arbitrary text", valuation)

    def test_rejects_missing_source_old_lane_and_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "PRESENTATION_AMOUNT_SOURCE_UNPROVEN"):
            verifier.adjudicate("Frozen", "Frozen\n- **权益市值：** 9,500,000,000", [])
        with self.assertRaisesRegex(ValueError, "PRESENTATION_AMOUNT_SOURCE_UNPROVEN"):
            verifier.adjudicate(
                "Frozen", "Frozen\n- **权益市值：** 9,500,000,000",
                [_valuation_artifact(artifact_id="old"),
                 _valuation_artifact(artifact_id="latest", equity_market_value=1_000_000)],
            )
        with self.assertRaises(ValueError):
            verifier.adjudicate(
                "- **其他：** 10%", "- **折现率：** 10%",
                [_valuation_artifact(discount_rate=float("nan"))],
            )
        with self.assertRaises(ValueError):
            verifier.adjudicate(
                "- **其他：** true", "- **预测期限（年）：** true",
                [_valuation_artifact(horizon_years=True)],
            )
        with self.assertRaises(ValueError):
            verifier.adjudicate(
                "- **其他：** true", "- **币种：** true",
                [_valuation_artifact(currency=True)],
            )

    def test_approved_comparison_rejects_identity_and_strict_failures_without_mutating_strict(self) -> None:
        old, new = _combined_bodies()
        artifacts = [_summary_artifact(), _valuation_artifact()]
        source_hashes = {item["artifact_id"]: f"hash-{index}" for index, item in enumerate(artifacts)}
        adjudicated = verifier.adjudicate(old, new, artifacts)
        entry = {**adjudicated, "run_id": "run-1", "artifact_input_hashes": source_hashes}
        strict = {
            "run_id": "run-1", "state": "FAIL",
            "issues": [{"code": "HISTORICAL_BODY_CHANGED"}],
            "markdown_sha256": hashlib.sha256(new.encode("utf-8")).hexdigest(),
        }
        expected = {"markdown_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest()}
        before = deepcopy(strict)

        for mutation in (
            lambda value: {**value, "run_id": "other"},
            lambda value: {**value, "artifact_input_hashes": {"wrong": "hash"}},
            lambda value: {**value, "changes_sha256": "wrong"},
        ):
            with self.assertRaises(ValueError):
                verifier.approved_comparison(
                    strict, expected, mutation(entry), source_hashes=source_hashes, artifacts=artifacts
                )
        self.assertEqual(strict, before)

        bad_strict = deepcopy(strict)
        bad_strict["issues"].append({"code": "OTHER_FAILURE"})
        with self.assertRaisesRegex(ValueError, "PRESENTATION_NOT_BODY_ONLY_FAILURE"):
            verifier.approved_comparison(
                bad_strict, expected, entry, source_hashes=source_hashes, artifacts=artifacts
            )

    def test_approved_comparison_rederives_fake_proof_path_value_and_count(self) -> None:
        old, new = _combined_bodies()
        artifacts = [_summary_artifact(), _valuation_artifact()]
        source_hashes = {item["artifact_id"]: f"hash-{index}" for index, item in enumerate(artifacts)}
        result = verifier.adjudicate(old, new, artifacts)
        base = {**result, "run_id": "run-1", "artifact_input_hashes": source_hashes}
        strict = {
            "run_id": "run-1", "state": "FAIL",
            "issues": [{"code": "HISTORICAL_BODY_CHANGED"}],
            "markdown_sha256": hashlib.sha256(new.encode("utf-8")).hexdigest(),
        }
        expected = {"markdown_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest()}

        def assert_rejected(mutator):
            candidate = deepcopy(base)
            mutator(candidate["changes"])
            candidate["changes_sha256"] = verifier.fingerprint(candidate["changes"])
            with self.assertRaisesRegex(ValueError, "PRESENTATION_CHANGE_SOURCE_CONFLICT|PRESENTATION_CHANGE_COUNT_INVALID"):
                verifier.approved_comparison(
                    strict, expected, candidate,
                    source_hashes=source_hashes, artifacts=artifacts,
                )

        assert_rejected(lambda changes: changes[0]["proof"].update(path="content.fake"))
        assert_rejected(lambda changes: changes[-1]["proof"].update(value=123))
        assert_rejected(lambda changes: changes[0].update(count=True))

    def test_proof_boolean_cannot_impersonate_numeric_one_even_with_new_digest(self) -> None:
        artifacts = [_valuation_artifact(equity_market_value=1)]
        old, new = "Frozen", "Frozen\n- **权益市值：** 1"
        entry = {**verifier.adjudicate(old, new, artifacts), "run_id": "run-1",
                 "artifact_input_hashes": {artifacts[0]["artifact_id"]: "original-payload-hash"}}
        entry["changes"][0]["proof"]["value"] = True
        entry["changes_sha256"] = verifier.fingerprint(entry["changes"])
        strict = {"run_id": "run-1", "state": "FAIL",
                  "issues": [{"code": "HISTORICAL_BODY_CHANGED"}],
                  "markdown_sha256": hashlib.sha256(new.encode()).hexdigest()}
        with self.assertRaisesRegex(ValueError, "PRESENTATION_CHANGE_SOURCE_CONFLICT"):
            verifier.approved_comparison(strict,
                {"markdown_sha256": hashlib.sha256(old.encode()).hexdigest()}, entry,
                source_hashes=entry["artifact_input_hashes"], artifacts=artifacts)


if __name__ == "__main__":
    unittest.main()
