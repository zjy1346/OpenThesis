from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from openthesis.demo import DEMO_COMPANY
from openthesis.disclosure_coverage import DisclosureCoveragePlanner
from openthesis.domain import FilingDocument, ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from openthesis.market_capabilities import DEFAULT_MARKET_CAPABILITIES, MarketCapability, MarketCapabilityRegistry
from openthesis.model_scheduler import ModelRequestScheduler, shared_model_scheduler
from openthesis.report_revisions import resolve_report_artifact
from openthesis.research_continuity import (
    RESEARCH_STAGE_KINDS,
    ResearchContinuityCoordinator,
    StageOutcome,
)
from openthesis.safe_report import SafeReportAssembler
from openthesis.storage import Storage


def _run(run_id: str = "continuity-run") -> ResearchRun:
    return ResearchRun(
        run_id=run_id, company=DEMO_COMPANY, workflow_id="test",
        research_pack_id="test", research_pack_version="1",
        provider_id="none", model_id="", data_as_of=utc_now_iso(),
    )


class ResearchContinuityTests(unittest.TestCase):
    def test_start_is_durable_and_failure_keeps_a_separate_visible_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            coordinator = ResearchContinuityCoordinator(storage)
            run = coordinator.start(_run())
            self.assertIsNotNone(storage.get_run(run.run_id))

            coordinator.preserve_visible_result(
                run, stage="filing-download", outcome=StageOutcome.WAITING_RETRYABLE,
                error_code="FILING_FETCH_FAILED", message="Official source is temporarily unavailable",
                retryable=True,
            )

            stored = storage.get_run(run.run_id)
            self.assertEqual(stored["status"], RunStatus.PARTIAL.value)
            artifacts = storage.get_artifacts(run.run_id)
            self.assertTrue(any(item["artifact_type"] == "continuity-notice" for item in artifacts))
            self.assertFalse(any(item["artifact_type"] == "research-report" for item in artifacts))
            snapshot = coordinator.snapshot(run.run_id)
            self.assertEqual(snapshot["latest"]["outcome"], StageOutcome.WAITING_RETRYABLE.value)

    def test_artifacts_are_insert_only_and_conflicting_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            storage.save_run(_run())
            first = ResearchArtifact("same", "continuity-run", "stage", "Stage", {"value": 1})
            storage.save_artifact(first)
            storage.save_artifact(first)
            with self.assertRaisesRegex(RuntimeError, "ARTIFACT_IDENTITY_CONFLICT"):
                storage.save_artifact(
                    ResearchArtifact("same", "continuity-run", "stage", "Stage", {"value": 2})
                )
            self.assertEqual(storage.get_artifacts("continuity-run")[0]["content"], {"value": 1})

    def test_report_revision_is_monotonic_and_cas_protected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            storage.save_run(_run())
            first = storage.append_report_revision("continuity-run", {"report": {"thesis": "one"}})
            second = storage.append_report_revision(
                "continuity-run", {"report": {"thesis": "two"}}, expected_generation=1
            )
            self.assertEqual(second["generation"], 2)
            self.assertEqual(second["parent_revision_id"], first["revision_id"])
            with self.assertRaisesRegex(RuntimeError, "REPORT_REVISION_CONFLICT"):
                storage.append_report_revision(
                    "continuity-run", {"report": {"thesis": "stale"}}, expected_generation=1
                )

    def test_financial_overlay_preserves_base_qualitative_payload(self) -> None:
        base = {
            "artifact_id": "base", "artifact_type": "research-report",
            "content": {"mode": "synthesized", "report": {"thesis": "qualitative"}},
        }
        overlay = {
            "artifact_id": "overlay", "artifact_type": "research-report",
            "content": {
                "mode": "financial-refresh-overlay", "base_report_artifact_id": "base",
                "financial_refresh": {"fact_count": 42},
            },
        }
        resolved = resolve_report_artifact([base, overlay])
        self.assertEqual(resolved["content"]["report"]["thesis"], "qualitative")
        self.assertEqual(base["content"]["mode"], "synthesized")

    def test_safe_report_is_nonempty_when_normal_renderer_is_unavailable(self) -> None:
        report = SafeReportAssembler().assemble(
            run_id="run", company_name="ACME", status="partial",
            artifacts=[{"artifact_type": "growth-opportunities", "title": "Growth"}],
            continuity={"latest": {"error_code": "RENDER", "message": "Saved"}},
            language="en",
        )
        self.assertIn("Growth", report.markdown)
        self.assertIn("run_id: run", report.html)

    def test_coverage_contract_exposes_missing_years(self) -> None:
        filing = FilingDocument(
            document_id="a", company_cik="c", accession_number="a", form_type="10-K",
            fiscal_period="FY", period_end="2025-12-31", filed_at="2026-01-01",
            primary_document="a.htm", source_url="https://example.test/a",
        )
        coverage = DisclosureCoveragePlanner().evaluate(
            [filing], requested_count=3, authority="SEC"
        )
        self.assertEqual(coverage.found_periods, ("2025",))
        self.assertEqual(coverage.missing_periods, ("2024", "2023"))
        self.assertFalse(coverage.complete)

    def test_coverage_contract_does_not_treat_empty_discovery_as_complete(self) -> None:
        coverage = DisclosureCoveragePlanner().evaluate(
            [], requested_count=3, authority="HKEX"
        )
        self.assertEqual(coverage.found_periods, ())
        self.assertEqual(coverage.missing_periods, ())
        self.assertTrue(coverage.exhausted)
        self.assertFalse(coverage.complete)

    def test_market_registry_is_one_registration_seam(self) -> None:
        self.assertEqual(DEFAULT_MARKET_CAPABILITIES.require("HK").official_authority, "HKEX")
        registry = MarketCapabilityRegistry()
        registry.register(MarketCapability("TEST", "TEST", True, False, True, False, ("IFRS",), ("ANNUAL_REPORT",)))
        self.assertTrue(registry.require("test").discovery)
        with self.assertRaises(ValueError):
            registry.register(MarketCapability("TEST", "TEST", True, False, True, False, (), ()))

    def test_scheduler_limits_concurrency_without_invoking_work_twice(self) -> None:
        scheduler = ModelRequestScheduler(limit=1)
        calls = 0
        maximum = 0
        active = 0
        lock = threading.Lock()

        def work() -> None:
            nonlocal calls, maximum, active
            with scheduler.slot():
                with lock:
                    calls += 1
                    active += 1
                    maximum = max(maximum, active)
                threading.Event().wait(0.02)
                with lock:
                    active -= 1

        threads = [threading.Thread(target=work) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(calls, 3)
        self.assertEqual(maximum, 1)

    def test_shared_scheduler_cools_down_and_recovers_gradually_after_rate_limit(self) -> None:
        now = [100.0]
        scheduler = ModelRequestScheduler(limit=4, clock=lambda: now[0])
        shared = shared_model_scheduler("connection.rate-test", limit=1)
        self.assertIs(shared, shared_model_scheduler("connection.rate-test", limit=3))
        self.assertIsNot(scheduler, shared_model_scheduler("different-model.rate-test", limit=1))

        scheduler.mark_rate_limited(999)
        limited = scheduler.snapshot()
        self.assertEqual(limited.limit, 2)
        self.assertEqual(limited.cooldown_remaining_seconds, 999.0)

        now[0] += 998.0
        scheduler.mark_success()
        cooling = scheduler.snapshot()
        self.assertEqual(cooling.limit, 2)
        self.assertEqual(cooling.cooldown_remaining_seconds, 1.0)

        now[0] += 1.0
        scheduler.mark_success()
        self.assertEqual(scheduler.snapshot().limit, 3)
        scheduler.mark_success()
        self.assertEqual(scheduler.snapshot().limit, 4)

    def test_every_registered_stage_preserves_prior_artifacts_on_single_failure(self) -> None:
        """A single stage failure must leave the same run open and auditable."""

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            coordinator = ResearchContinuityCoordinator(storage)

            for index, stage in enumerate(RESEARCH_STAGE_KINDS):
                run = coordinator.start(_run(f"chaos-{index}"))
                sentinel = ResearchArtifact(
                    artifact_id=f"{run.run_id}:verified-input",
                    run_id=run.run_id,
                    artifact_type="verified-input",
                    title="Previously verified evidence",
                    content={"digest": "stable", "stage_before_failure": stage},
                )
                storage.save_artifact(sentinel)
                before = next(
                    item for item in storage.get_artifacts(run.run_id)
                    if item["artifact_id"] == sentinel.artifact_id
                )

                coordinator.preserve_visible_result(
                    run,
                    stage=stage,
                    outcome=StageOutcome.WAITING_RETRYABLE,
                    error_code="INJECTED_SINGLE_STAGE_FAILURE",
                    message=f"Retry {stage} after its dependency recovers",
                    retryable=True,
                )

                after = next(
                    item for item in storage.get_artifacts(run.run_id)
                    if item["artifact_id"] == sentinel.artifact_id
                )
                notices = [
                    item for item in storage.get_artifacts(run.run_id)
                    if item["artifact_type"] == "continuity-notice"
                ]
                snapshot = coordinator.snapshot(run.run_id)
                self.assertEqual(before["content"], after["content"], stage)
                self.assertEqual(storage.get_run(run.run_id)["status"], RunStatus.PARTIAL.value, stage)
                self.assertTrue(notices, stage)
                self.assertEqual(snapshot["latest"]["stage"], stage)
                self.assertTrue(notices[-1]["content"]["continuity"]["retryable"], stage)


if __name__ == "__main__":
    unittest.main()
