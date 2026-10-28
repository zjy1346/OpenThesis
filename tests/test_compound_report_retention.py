"""Offline compound recovery regressions through durable storage and public reads.

The first two tests are synthetic boundary fixtures: they supply a stage outcome
artifact directly. The final test exercises the real StageExecutor exception
boundary and FinancialValidator against positioned source evidence. None of
these fixtures is a real-company or 44-manifest acceptance run.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from openthesis import app as app_module
from openthesis.app import OpenThesisApp
from openthesis.application.reports import (
    ReportReadSnapshot,
    ReportService,
    artifacts_for_report_projection,
)
from openthesis.demo import DEMO_COMPANY
from openthesis.domain import FilingDocument, ResearchArtifact, ResearchRun, RunStatus
from openthesis.financial_contracts import fact_row_evidence_ids
from openthesis.financial_validation import FinancialValidator
from openthesis.markets import build_company
from openthesis.providers import ProviderError
from openthesis.report_content import assemble_trusted_report_payload, read_trusted_snapshots
from openthesis.report_html import render_research_html
from openthesis.reporting import render_research_run
from openthesis.research import _supported_synthesis_sections
from openthesis.research_assertions import AssertionSession, assertion_session, validate_response
from openthesis.research_execution.durable import DurableStageVerifier
from openthesis.research_execution.checkpoints import completed_checkpoint
from openthesis.research_execution.executor import StageExecutor, StageResult, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.resilience.contracts import digest
from openthesis.service import AppService
from openthesis.storage import Storage


SOURCE_BODY = "The issuer manufactures industrial refrigeration equipment and provides related services."
BODY = "The business combines industrial refrigeration equipment manufacturing with related service work."
SOURCE_URL = "https://example.org/issuer-annual-report.pdf"
BUSINESS_EVIDENCE_ID = "filing:business-2025"
FINANCIAL_EVIDENCE_ID = "fact:revenue-2025-currency-unknown"


class _StatusVar:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


def _evidence_records(company=DEMO_COMPANY) -> dict[str, dict]:
    return {
        BUSINESS_EVIDENCE_ID: {
            "kind": "filing_text",
            "evidence_id": BUSINESS_EVIDENCE_ID,
            "document_id": "fixture-filing-2025",
            "company_cik": company.cik,
            "accession_number": "fixture-2025",
            "end_date": "2025-12-31",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "excerpt": SOURCE_BODY,
            "source_url": SOURCE_URL,
            "locator": "page:12",
            "content_hash": f"{2025:064x}",
            "document_sha256": f"{2025:064x}",
        },
        FINANCIAL_EVIDENCE_ID: {
            "kind": "financial_fact",
            "evidence_id": FINANCIAL_EVIDENCE_ID,
            "company_cik": company.cik,
            "accession_number": "fixture-2025",
            "end_date": "2025-12-31",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "concept": "revenue",
            "value": 123000000,
            "unit": "currency",
            "unit_scale": 1,
            "unit_provenance": "unknown",
            "currency": "",
            "scope": "consolidated",
            "excerpt": "Revenue amount observed; source currency declaration is unavailable.",
            "source_url": SOURCE_URL,
            "locator": "page:24",
            "content_hash": "a" * 64,
        },
    }


def _verified_sections(candidate, records):
    """Offline semantic-provider fixture; real scope/span/proof validation stays on."""
    collected = AssertionSession()
    with assertion_session(collected):
        _supported_synthesis_sections(candidate, set(records), records, "en")
    queries = list(collected.queries.values())
    judgments = []
    for query in queries:
        # The fixture judges only this explicit paraphrase. It never approves
        # the bad currency sibling or arbitrary text merely because refs exist.
        if query["text"] != BODY:
            continue
        source = next(item for item in query["sources"] if item["evidence_id"] == BUSINESS_EVIDENCE_ID)
        assert source["raw_text"] == SOURCE_BODY
        judgments.append({"assertion_id": query["assertion_id"], "state": "supported", "source_spans": [{
            "evidence_id": source["evidence_id"], "start": 0, "end": len(SOURCE_BODY),
            "quote": SOURCE_BODY, "source_sha256": source["source_sha256"],
        }]})
    accepted = validate_response({"structured_output_valid": True, "results": judgments}, queries, "offline-semantic-fixture")
    with assertion_session(AssertionSession(accepted)):
        return _supported_synthesis_sections(candidate, set(records), records, "en")


def test_independent_custom_pack_analysis_is_readable_and_substantive_without_a_registered_name(tmp_path):
    store = Storage(tmp_path)
    run = _seed_partial_run(store, "custom-pack-analysis")
    section_id = "custom_equipment_business"
    candidate = {section_id: {"analysis": BODY, "evidence_ids": [BUSINESS_EVIDENCE_ID]}}
    safe, proof = _verified_sections(candidate, _evidence_records())
    assert any(node["role"] == "verified_analysis" for node in proof["nodes"])
    store.save_artifact(ResearchArtifact("custom-report", run.run_id, "research-report", "Custom analysis",
        {"mode": "synthesis-incomplete", "report": safe, "verification": {"section_validation": proof}}))
    service = AppService(tmp_path, provider_factory=lambda _: None)
    for language in ("en", "zh-CN", "zh-TW"):
        result = service.get_report(run.run_id, language=language)
        section = next(item for item in result["visible_sections"] if item["section_id"] == section_id)
        assert section["is_substantive"] is True
        assert result["report_read_state"] == "partial"
        assert result["research_complete"] is False
        assert BODY in result["markdown"] and BODY in result["html"]


def test_exact_source_quote_stays_visible_but_is_not_substantive_analysis(tmp_path):
    store = Storage(tmp_path)
    run = _seed_partial_run(store, "quote-only-report")
    candidate = {"business_model": {"analysis": SOURCE_BODY, "evidence_ids": [BUSINESS_EVIDENCE_ID]}}
    records = _evidence_records()
    safe, proof = _supported_synthesis_sections(candidate, set(records), records, "en")
    assert proof["nodes"][0]["role"] == "source_quote"
    store.save_artifact(ResearchArtifact("quote-report", run.run_id, "research-report", "Disclosure quote",
        {"mode": "synthesis-incomplete", "report": safe, "verification": {"section_validation": proof}}))
    result = AppService(tmp_path, provider_factory=lambda _: None).get_report(run.run_id, language="en")
    assert SOURCE_BODY in result["markdown"] and SOURCE_BODY in result["html"]
    assert result["is_substantive"] is False
    assert result["research_complete"] is False
    service = AppService(tmp_path, provider_factory=lambda _: None)

    def fail_renderer(*_args, **_kwargs):
        raise RuntimeError("INJECTED_RENDERER_FAILURE")

    service._report_service = ReportService(fail_renderer, fail_renderer)
    fallback = service.get_report(run.run_id, language="en")
    assert SOURCE_BODY in fallback["markdown"] and SOURCE_BODY in fallback["html"]
    assert fallback["is_substantive"] is False
    assert fallback["report_read_state"] == result["report_read_state"]
    assert all(not section["is_substantive"] for section in fallback["visible_sections"])


def _model_candidate(company=DEMO_COMPANY) -> dict:
    return {
        "business_model": {"analysis": BODY, "evidence_ids": [BUSINESS_EVIDENCE_ID]},
        "financial_quality": {
            "analysis": "Revenue was USD 999 billion.",
            "evidence_ids": [FINANCIAL_EVIDENCE_ID],
        },
        "claims": [{
            "claim_id": "unsupported-usd-revenue",
            "kind": "fact",
            "text": "Revenue was USD 999 billion.",
            "concept": "revenue",
            "value": 999000000000,
            "unit": "currency",
            "currency": "USD",
            "unit_scale": 1,
            "unit_provenance": "normalized",
            "scope": "consolidated",
            "company_cik": company.cik,
            "accession_number": "fixture-2025",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "evidence_ids": [FINANCIAL_EVIDENCE_ID],
        }],
    }


def _seed_partial_run(store: Storage, run_id: str, *, company=None) -> ResearchRun:
    company = deepcopy(company or DEMO_COMPANY)
    store.save_company(company)
    run = ResearchRun(
        run_id, company, "fixture-workflow", "official.long-term-fundamentals", "1",
        "offline-fixture", "offline-model", "2025-12-31", RunStatus.PARTIAL,
        report_language="en", research_configuration={"annual_history_years": 2},
    )
    store.save_run(run)
    store.save_filings([
        FilingDocument(
            f"fixture-filing-{year}", company.cik, f"fixture-{year}", "ANNUAL_REPORT",
            "FY", f"{year}-12-31", f"{year + 1}-03-01", "annual.pdf", SOURCE_URL,
            content_hash=f"{year:064x}",
        )
        for year in (2024, 2025)
    ])
    records = _evidence_records(company)
    # The source observation remains available for audit, but its empty currency
    # is deliberately not an admitted/normalized financial fact.
    store.save_artifact(ResearchArtifact(
        f"financial-source-observations:{run_id}", run_id,
        "deterministic-financial-summary", "Financial source observations",
        {"company": company.to_dict(), "evidence": list(records.values()), "metrics": [], "facts": []},
    ))
    return run


def _persist_failed_candidate_and_local_delta(
    store: Storage, run: ResearchRun, *, failure_code: str,
) -> tuple[dict, dict, str]:
    candidate = _model_candidate()
    records = _evidence_records()
    safe, audit = _verified_sections(candidate, records)
    assert safe.get("business_model", {}).get("analysis") == BODY
    assert "financial_quality" not in safe
    assert audit.get("nodes")
    assert audit.get("quarantined_paths")

    candidate_digest = digest(candidate)
    store.save_artifact(ResearchArtifact(
        f"final-synthesis-outcome:{run.run_id}", run.run_id, "stage-outcome",
        "Final synthesis transport failure",
        {"stage_id": "final-synthesis", "outcome": "waiting_retryable",
         "retryable": True, "error_code": failure_code,
         "candidate_digest": candidate_digest},
    ))

    spec = StageSpec("final-synthesis", "compound-retention-v1", "offline-candidate", "section-proof")
    stage_input = digest({"run_id": run.run_id, "source_snapshot": "fixture-2025"})
    fingerprint = "offline-model-fingerprint"

    def verify_local(value: dict) -> StageResult:
        independently_supported, proof = _verified_sections(value, records)
        assert independently_supported.get("business_model", {}).get("analysis") == BODY
        assert "financial_quality" not in independently_supported
        validation = StageValidation(
            False,
            independently_supported,
            "completed_partial",
            ("CURRENCY_PROOF_MISSING", failure_code),
            {"verification": {"section_validation": proof}},
        )
        return StageResult(
            spec.stage_id, spec.identity, stage_input, value, validation,
            digest(value), execution_fingerprint=fingerprint, has_candidate=True,
        )

    result = DurableStageVerifier(store, run.run_id)(
        spec, stage_input, candidate, fingerprint, verify_local,
    )
    assert result.usable and not result.validation.passed
    assert result.validation.state == "completed_partial"
    assert result.candidate == candidate  # bad sibling remains in the candidate audit

    with store.connect() as db:
        recovery = db.execute(
            "SELECT state,outcome_json FROM recovery_plans ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert recovery is not None and recovery["state"] == "partial_committed"
    outcome = json.loads(recovery["outcome_json"])
    assert outcome.get("accepted_delta")
    assert outcome.get("remaining_issue_ids")
    assert outcome.get("next_actions")

    artifacts = store.get_artifacts(run.run_id)
    assert any(row["artifact_type"] == "stage-checkpoint" for row in artifacts)
    assert any(row["artifact_type"] == "trusted-content-snapshot" for row in artifacts)
    original = next(row for row in artifacts if row["artifact_id"] == f"final-synthesis-outcome:{run.run_id}")
    assert original["content"]["candidate_digest"] == candidate_digest

    content = assemble_trusted_report_payload(artifacts, run_id=run.run_id)
    assert content is not None and content["mode"] == "progressive-local"
    assert content["readiness"]["complete"] is False
    report = ResearchArtifact(
        f"progressive-report:{run.run_id}", run.run_id, "research-report",
        "Progressive local report", content,
    )
    publication = store.capture_research_publication(run)
    store.publish_research_progress(publication, report, cancel_check=lambda: False)
    return candidate, audit, failure_code


def _formal_read_and_assert_retention(
    data_dir: Path, run_id: str, monkeypatch, *,
    failure_artifact_id: str | None = None,
) -> tuple[dict, str]:
    reopened = Storage(data_dir)
    artifacts = reopened.get_artifacts(run_id)
    assert any(row["artifact_type"] == "stage-checkpoint" for row in artifacts)
    assert any(row["artifact_type"] == "trusted-content-snapshot" for row in artifacts)
    failure_artifact_id = failure_artifact_id or f"final-synthesis-outcome:{run_id}"
    assert any(row["artifact_id"] == failure_artifact_id for row in artifacts)

    committed_nodes = read_trusted_snapshots(artifacts, run_id=run_id)
    assert committed_nodes
    retained = next(node for node in committed_nodes if node.get("body") == BODY)
    expected_node_id = retained["node_id"]
    assert retained["sources"] == [{
        "evidence_id": BUSINESS_EVIDENCE_ID,
        "source_url": SOURCE_URL,
        "locator": "page:12",
        "content_hash": f"{2025:064x}",
    }]

    service = AppService(data_dir, provider_factory=lambda _config: None)
    formal = service.get_report(run_id, language="en")
    assert formal["status"] == RunStatus.PARTIAL.value
    assert formal["report_read_state"] == "partial", formal["report_read_diagnostics"]
    assert formal["research_complete"] is False
    assert formal["action_required"] is True
    assert formal["report_readiness"]["complete"] is False
    assert formal["financial_status"]["next_action"]
    assert BODY in formal["markdown"] and BODY in formal["html"]
    assert "Revenue was USD 999 billion." not in formal["markdown"]
    assert "Revenue was USD 999 billion." not in formal["html"]
    assert SOURCE_URL in formal["markdown"] and SOURCE_URL in formal["html"]

    run = reopened.get_run(run_id)
    revision = reopened.latest_report_revision(run_id)
    projected_artifacts = artifacts_for_report_projection(
        artifacts, revision.get("payload") if revision else None,
    )
    report_service = ReportService(render_research_run, render_research_html)
    read = report_service.read(ReportReadSnapshot(
        run_id, run["name"], run["status"], projected_artifacts, formal["continuity"], "en",
        revision_id=str(revision.get("revision_id") or "") if revision else "",
        revision_generation=int(revision.get("generation") or 0) if revision else 0,
    ))
    assert read.read_state == "partial" and read.document is not None
    assert read.document.readiness.complete is False
    assert read.markdown == formal["markdown"]
    assert read.html == formal["html"]
    visible_node = next(
        node for section in read.document.sections for node in section.nodes
        if node.text == BODY
    )
    assert visible_node.validation_record.node_id == expected_node_id
    assert BUSINESS_EVIDENCE_ID in visible_node.source_ids

    # Exercise the actual UI export selector/write method. Only the native save
    # dialog is replaced; the strings are the formal AppService read values.
    ui = OpenThesisApp.__new__(OpenThesisApp)
    ui.current_report_text = formal["markdown"]
    ui.current_report_html = formal["html"]
    ui.report_focus_window = None
    ui.root = None
    ui.status_var = _StatusVar()
    ui._t = lambda value, **kwargs: value.format(**kwargs)
    markdown_path = data_dir / f"{run_id}-export.md"
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **_kwargs: str(markdown_path))
    ui._export_report()
    assert markdown_path.read_text(encoding="utf-8") == formal["markdown"]
    html_path = data_dir / f"{run_id}-export.html"
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **_kwargs: str(html_path))
    ui._export_report()
    assert html_path.read_text(encoding="utf-8") == formal["html"]
    return formal, expected_node_id


def test_currency_proof_gap_and_later_model_failure_keep_independent_node_after_restart(tmp_path, monkeypatch):
    data_dir = tmp_path / "currency-gap"
    store = Storage(data_dir)
    run = _seed_partial_run(store, "compound-currency-gap")
    candidate, audit, _ = _persist_failed_candidate_and_local_delta(
        store, run, failure_code="MODEL_TRANSPORT_FAILED",
    )

    records = _evidence_records()
    assert records[FINANCIAL_EVIDENCE_ID]["currency"] == ""
    assert records[FINANCIAL_EVIDENCE_ID]["unit_provenance"] == "unknown"
    assert candidate["financial_quality"]["evidence_ids"] == [FINANCIAL_EVIDENCE_ID]
    assert any("financial_quality" in item.get("path", "")
               for item in audit["quarantined_paths"])

    formal, _node_id = _formal_read_and_assert_retention(data_dir, run.run_id, monkeypatch)
    assert formal["financial_status"]["state"] in {"incomplete", "warning"}
    artifacts = Storage(data_dir).get_artifacts(run.run_id)
    failure = next(row for row in artifacts if row["artifact_id"] == f"final-synthesis-outcome:{run.run_id}")
    assert failure["content"]["error_code"] == "MODEL_TRANSPORT_FAILED"
    checkpoint = next(row for row in artifacts if row["artifact_type"] == "stage-checkpoint")
    assert checkpoint["content"]["result"]["candidate"] == candidate


def test_final_synthesis_and_complex_renderer_failures_keep_formal_and_exported_report(tmp_path, monkeypatch):
    data_dir = tmp_path / "synthesis-and-renderer-failure"
    store = Storage(data_dir)
    run = _seed_partial_run(store, "compound-renderer-failure")
    candidate, _audit, _ = _persist_failed_candidate_and_local_delta(
        store, run, failure_code="FINAL_SYNTHESIS_MODEL_FAILED",
    )

    broken_renderer_service = AppService(data_dir, provider_factory=lambda _config: None)

    def fail_complex_renderer(*_args, **_kwargs):
        raise RuntimeError("INJECTED_COMPLEX_RENDERER_FAILURE")

    # The failure is injected at the renderer adapter boundary. AppService's
    # production emergency projection, storage, and proof validator stay real.
    broken_renderer_service._report_service = ReportService(
        fail_complex_renderer, fail_complex_renderer,
    )
    fallback = broken_renderer_service.get_report(run.run_id, language="en")
    assert BODY in fallback["markdown"] and BODY in fallback["html"]
    assert SOURCE_URL in fallback["markdown"] and SOURCE_URL in fallback["html"]
    assert "Revenue was USD 999 billion." not in fallback["markdown"]
    assert "Revenue was USD 999 billion." not in fallback["html"]
    assert fallback["report_read_state"] == "partial"
    assert fallback["action_required"] is True
    assert any(item.get("code") == "REPORT_PROJECTION_FAILED"
               for item in fallback["report_read_diagnostics"])

    formal, node_id = _formal_read_and_assert_retention(data_dir, run.run_id, monkeypatch)
    assert any(section.get("section_id") == node_id for section in fallback["visible_sections"])
    artifacts = Storage(data_dir).get_artifacts(run.run_id)
    failure = next(row for row in artifacts if row["artifact_id"] == f"final-synthesis-outcome:{run.run_id}")
    assert failure["content"]["error_code"] == "FINAL_SYNTHESIS_MODEL_FAILED"
    checkpoint = next(row for row in artifacts if row["artifact_type"] == "stage-checkpoint")
    assert checkpoint["content"]["result"]["candidate"] == candidate
    assert formal["report_readiness"]["complete"] is False


def test_real_executor_provider_error_and_validator_currency_rejection_survive_public_reads(
    tmp_path, monkeypatch,
):
    """Offline integration: actual producer exception + actual source validator."""
    from tests.test_unit_declaration_integrity import observation

    data_dir = tmp_path / "actual-failure-boundary"
    store = Storage(data_dir)
    company = build_company("002594.SZ", "BYD")
    run_id = "compound-actual-provider-and-currency-failure"
    run = _seed_partial_run(store, run_id, company=company)

    # This is the positioned PDF-parser fixture, followed by the production
    # FinancialValidator. Its bare yuan unit proves amount/scale but not currency.
    fact, source_refs = observation("单位：元")
    row_ref = next(
        ref for ref in source_refs if ref.evidence_id in fact_row_evidence_ids(fact)
    )
    group = FinancialValidator().validate_group(
        [fact],
        (fact.accession_number, fact.end_date, fact.fiscal_period,
         fact.consolidated_scope, fact.currency),
        {fact.fact_id: row_ref},
        required_concepts={fact.concept},
        source_evidence=source_refs,
    ).validation
    fact_record = next(item for item in group.fact_records if item.fact_id == fact.fact_id)
    components = dict(fact_record.components)
    assert group.accepted == ()
    assert fact_record.accepted is False
    assert components["amount"].state == "observed"
    assert components["scale"].state == "observed"
    assert components["currency"].state == "unknown"

    # Keep the rejected candidate and its component-level proof as diagnostic
    # evidence only. Do not insert it through replace_financial_ingestion.
    store.save_artifact(ResearchArtifact(
        f"financial-candidate-observation:{run_id}", run_id,
        "financial-candidate-observation", "Rejected positioned financial candidate",
        {
            "candidate": fact.to_dict(),
            "validation": {
                "status": group.status.value,
                "issues": list(group.issues),
                "accepted_fact_ids": [item.fact_id for item in group.accepted],
                "quarantined_fact_ids": [item.fact_id for item in group.quarantined],
                "fact_validation_records": [item.to_dict() for item in group.fact_records],
            },
            "source_evidence": [ref.to_dict() for ref in source_refs],
        },
    ))

    records = _evidence_records(company)
    candidate = _model_candidate(company)
    business_spec = StageSpec(
        "business-analyst", "compound-actual-v1", "business-producer", "section-proof",
        output_contract="validated-analysis-v1",
    )
    final_spec = StageSpec(
        "final-synthesis", "compound-actual-v1", "final-producer", "section-proof",
        depends_on=(business_spec.stage_id,), output_contract="research-report-v1",
    )
    registry = StageRegistry((business_spec, final_spec))

    def produce_business(_spec, _inputs, _dependencies):
        return deepcopy(candidate)

    def fail_final(_spec, _inputs, _dependencies):
        # This crosses the real StageExecutor producer boundary, but never
        # invokes a network provider.
        raise ProviderError(
            "offline injected provider failure",
            retryable=True,
            code="MODEL_TRANSPORT_FAILED",
        )

    def validate_business(spec, value, _inputs):
        safe, proof = _verified_sections(value, records)
        assert safe.get("business_model", {}).get("analysis") == BODY
        assert "financial_quality" not in safe
        assert proof.get("nodes")
        return StageValidation(
            False,
            safe,
            "completed_partial",
            ("normalization_unit_declaration_mismatch",),
            {"verification": {"section_validation": proof}},
        )

    def checkpoint(result: StageResult) -> None:
        store.save_artifact(ResearchArtifact(
            f"executor-checkpoint:{run_id}:{result.stage_id}", run_id,
            "stage-checkpoint", result.stage_id, completed_checkpoint(result),
            model_id=result.execution_fingerprint, agent_id="offline-test-executor",
        ))

    execution_fingerprint = "offline-fixture-model-fingerprint"
    results = StageExecutor(
        {"business-producer": produce_business, "final-producer": fail_final},
        {"section-proof": validate_business},
        checkpoint=checkpoint,
    ).execute(registry, {"fixture": "positioned-disclosure"},
              execution_fingerprint=execution_fingerprint)

    business_result = results["business-analyst"]
    failed_result = results["final-synthesis"]
    assert business_result.usable and not business_result.validation.passed
    assert failed_result.validation.state == "needs_action"
    assert failed_result.error_code == "MODEL_TRANSPORT_FAILED"
    assert failed_result.retryable is True
    assert failed_result.candidate == {}

    # Durable local verification is the real commit seam. It independently
    # regenerates section proof from the candidate and source records.
    def verify_saved_candidate(value: dict) -> StageResult:
        safe, proof = _verified_sections(value, records)
        assert safe.get("business_model", {}).get("analysis") == BODY
        assert "financial_quality" not in safe
        validation = StageValidation(
            False,
            safe,
            "completed_partial",
            ("normalization_unit_declaration_mismatch",),
            {"verification": {"section_validation": proof}},
        )
        return StageResult(
            business_spec.stage_id,
            business_spec.identity,
            business_result.input_digest,
            value,
            validation,
            digest(value),
            execution_fingerprint=execution_fingerprint,
            has_candidate=True,
        )

    committed = DurableStageVerifier(store, run_id)(
        business_spec,
        business_result.input_digest,
        business_result.candidate,
        execution_fingerprint,
        verify_saved_candidate,
    )
    assert committed.usable and committed.validation.state == "completed_partial"
    with store.connect() as db:
        recovery = db.execute(
            "SELECT state,outcome_json FROM recovery_plans ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert recovery is not None and recovery["state"] == "partial_committed"
    recovery_outcome = json.loads(recovery["outcome_json"])
    assert recovery_outcome["accepted_delta"]
    assert recovery_outcome["remaining_issue_ids"]
    assert recovery_outcome["next_actions"]

    artifacts = store.get_artifacts(run_id)
    progressive = assemble_trusted_report_payload(artifacts, run_id=run_id)
    assert progressive is not None and progressive["mode"] == "progressive-local"
    assert progressive["readiness"]["complete"] is False
    report = ResearchArtifact(
        f"progressive-report:{run_id}", run_id, "research-report",
        "Progressive local report", progressive,
    )
    store.publish_research_progress(
        store.capture_research_publication(run), report, cancel_check=lambda: False,
    )

    # The rejected fact stays in the audit archive, never in accepted facts.
    reopened = Storage(data_dir)
    assert not any(item.get("fact_id") == fact.fact_id
                   for item in reopened.get_facts(fact.company_cik))
    reopened_artifacts = reopened.get_artifacts(run_id)
    candidate_archive = next(
        row for row in reopened_artifacts
        if row["artifact_id"] == f"financial-candidate-observation:{run_id}"
    )
    saved_record = candidate_archive["content"]["validation"]["fact_validation_records"][0]
    assert saved_record["accepted"] is False
    assert saved_record["components"]["currency"]["state"] == "unknown"
    assert saved_record["components"]["amount"]["state"] == "observed"
    business_checkpoints = [
        row for row in reopened_artifacts
        if row["artifact_type"] == "stage-checkpoint"
        and row["content"]["result"]["stage_id"] == "business-analyst"
    ]
    assert business_checkpoints
    assert all(row["content"]["result"]["candidate"] == candidate
               for row in business_checkpoints)
    assert all(
        "financial_quality" not in row["content"]["result"]["validation"]["trusted_output"]
        for row in business_checkpoints
    )

    formal, node_id = _formal_read_and_assert_retention(
        data_dir,
        run_id,
        monkeypatch,
        failure_artifact_id=f"executor-checkpoint:{run_id}:final-synthesis",
    )
    assert formal["financial_status"]["next_action"] == "retry_missing_periods"
    persisted_failure = next(
        row for row in Storage(data_dir).get_artifacts(run_id)
        if row["artifact_id"] == f"executor-checkpoint:{run_id}:final-synthesis"
    )
    result = persisted_failure["content"]["result"]
    assert result["validation"]["state"] == "needs_action"
    assert result["error_code"] == "MODEL_TRANSPORT_FAILED"
    assert result["retryable"] is True
    assert formal["research_complete"] is False
    assert BODY in formal["markdown"] and SOURCE_URL in formal["markdown"]
    assert node_id
