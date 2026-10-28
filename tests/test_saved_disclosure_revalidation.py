from dataclasses import replace
from unittest.mock import patch

import pytest

from openthesis.domain import FilingDocument, ResearchArtifact, ResearchRun, RunStatus
from openthesis.financial_sources.disclosures import cached_filing_evidence, saved_filing_evidence
from openthesis.markets import build_company
from openthesis.service import AppService
from openthesis.sidecar import JsonLineServer
from openthesis.storage import Storage
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.research_execution.model import ModelStageRequest
from openthesis.research_execution.registry import standard_model_registry


def capture(store):
    company = build_company("688981.SH", "Fixture", reporting_currency="CNY")
    store.save_company(company)
    run = ResearchRun("saved-disclosure", company, "long-term-fundamentals", "", "", "none", "", "2026-10-11", RunStatus.PARTIAL)
    store.save_run(run)
    filing = FilingDocument("official-2025", company.security_id, "2025", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-01", "annual.pdf", "https://example.invalid/annual.pdf", content_hash="a" * 64)
    store.save_filings([filing])
    source = {"evidence_id": "filing:captured-business", "kind": "filing_text", "document_id": filing.document_id,
        "company_cik": filing.company_cik, "source_url": filing.source_url, "content_hash": filing.content_hash,
        "end_date": filing.period_end, "fiscal_period": "FY", "fiscal_year": 2025, "topic": "business",
        "excerpt": "Operations serve industrial customers.", "locator": "page:1"}
    artifact = ResearchArtifact("captured-input", run.run_id, "deterministic-financial-summary", "Input",
        {"company": {"cik": company.cik}, "evidence": [source], "currency": "CNY", "metrics": []})
    store.save_artifact(artifact)
    return run, filing, source


@pytest.mark.parametrize("change", [{"content_hash": "b" * 64}, {"company_cik": "other"},
    {"source_url": "https://example.invalid/corrected.pdf"}, {"period_end": "2024-12-31"}])
def test_changed_source_cannot_revive_saved_disclosure(tmp_path, change):
    store = Storage(tmp_path)
    run, filing, _ = capture(store)
    assert saved_filing_evidence(store.get_artifacts(run.run_id), [replace(filing, **change)], company_cik=run.company.cik) == []


def test_missing_pdf_retains_host_captured_source_but_never_parses_in_local_mode(tmp_path):
    store = Storage(tmp_path)
    run, filing, source = capture(store)
    with patch("openthesis.financial_sources.disclosures.build_filing_evidence", side_effect=AssertionError("no parsing")), \
         patch("openthesis.financial_sources.disclosures.build_disclosure_index", side_effect=AssertionError("no index rebuild")):
        assert not any(item.get("kind") == "filing_text" for item in cached_filing_evidence(store, [filing], cache_only=True))
        restored = saved_filing_evidence(store.get_artifacts(run.run_id), [filing], company_cik=run.company.cik)
    assert restored[0]["excerpt"] == source["excerpt"]
    assert restored[0]["document_sha256"] == filing.content_hash


def test_formal_rpc_revalidation_is_zero_provider_and_persists_terminal_state(tmp_path):
    service = AppService(tmp_path)
    run, _, source = capture(service.storage)
    service._research_orchestrator.provider_factory = lambda *_: (_ for _ in ()).throw(AssertionError("no provider construction"))
    with patch("openthesis.financial_sources.disclosures.build_filing_evidence", side_effect=AssertionError("no parsing")), \
         patch("openthesis.financial_sources.disclosures.build_disclosure_index", side_effect=AssertionError("no parsing")):
        result = JsonLineServer(service).dispatch({"jsonrpc": "2.0", "method": "research.revalidate_saved",
            "params": {"run_id": run.run_id}})
    assert result["run_id"] == run.run_id
    reopened = AppService(tmp_path)
    assert reopened.storage.get_run(run.run_id)["status"] not in {"created", "running"}
    inputs = [row for row in reopened.storage.get_artifacts(run.run_id) if row["artifact_type"] == "deterministic-financial-summary"]
    assert any(source["evidence_id"] in [item.get("evidence_id") for item in row["content"]["evidence"]] for row in inputs)
    checkpoints = [row["content"]["result"] for row in reopened.storage.get_artifacts(run.run_id)
        if row["artifact_type"] == "stage-checkpoint"]
    assert checkpoints and any(item["error_code"] == "STAGE_SAVED_CANDIDATE_UNAVAILABLE" for item in checkpoints)
    assert all(item["validation"]["state"] not in {"queued", "running", "validating"} for item in checkpoints)


def test_formal_rpc_recovers_received_body_without_a_stage_checkpoint(tmp_path):
    service = AppService(tmp_path)
    run, _, source = capture(service.storage)
    journal = ModelRequestJournal(service.storage, run.run_id, "old-config", timeout_seconds=2, cancel_check=lambda: False)
    stage_id = "business-analyst"
    registry = standard_model_registry()
    generations = journal.prepare(registry, "original-material")
    candidate = {"structured_output_valid": True,
        "business_model": {"text": source["excerpt"], "evidence_ids": [source["evidence_id"]]},
        "claims": [{"claim_id": "saved-business", "kind": "inference", "text": source["excerpt"],
            "evidence_ids": [source["evidence_id"]]}]}
    journal.execute(ModelStageRequest(stage_id, "system", "old-request"), lambda: candidate,
        (stage_id, generations[stage_id]))
    service._research_orchestrator.provider_factory = lambda *_: pytest.fail("must not create provider")
    with patch("openthesis.financial_sources.disclosures.build_filing_evidence", side_effect=AssertionError("no parsing")):
        JsonLineServer(service).dispatch({"jsonrpc": "2.0", "method": "research.revalidate_saved", "params": {"run_id": run.run_id}})
    reopened = AppService(tmp_path)
    stages = [row["content"]["result"] for row in reopened.storage.get_artifacts(run.run_id)
        if row["artifact_type"] == "stage-checkpoint" and row["content"]["result"]["stage_id"] == stage_id]
    assert stages and stages[-1]["has_candidate"]
    assert stages[-1]["candidate"]["business_model"]["text"] == source["excerpt"]
    delivered = reopened.get_report(run.run_id, language="en")
    assert source["excerpt"] in delivered["markdown"]
    flows = [row["content"] for row in reopened.storage.get_artifacts(run.run_id)
        if row["artifact_type"] == "stage-content-flow" and row["content"].get("stage_id") == stage_id]
    assert flows and flows[-1]["candidate_available"] and flows[-1]["accepted_nodes"] > 0
    assert delivered["content_flow"]["presented_semantic_nodes"] > 0
    with reopened.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_request_journal WHERE run_id=?", (run.run_id,)).fetchone()[0] == 1
