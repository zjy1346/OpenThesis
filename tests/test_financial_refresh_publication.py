"""Financial report refresh must not supersede a competing report/source head."""
from copy import deepcopy
from dataclasses import replace
from concurrent.futures import CancelledError
from threading import Event

import pytest

from openthesis.application.financials import OfficialFinancialRequest
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from openthesis.service import AppService
from test_official_financial_preparation import setup_preparer


def seeded_service(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))
    run = ResearchRun("refresh-run", company, "wf", "pack", "1", "unused", "unused", "2026-10-06",
        status=RunStatus.PARTIAL, research_configuration={"annual_history_years": 1})
    report = ResearchArtifact("refresh-run:base", run.run_id, "research-report", "Original analysis",
        {"business_model": {"analysis": "Original complete text remains auditable"}})
    storage.save_run_with_artifacts(run, [report])
    service = AppService(storage.data_dir)
    def competing_source_publication():
        return preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1, force=True))
    return company, service, run, competing_source_publication


@pytest.mark.parametrize("competing_head", ["report", "financial", "run"])
def test_refresh_refuses_stale_inputs_without_partial_artifact_publication(tmp_path, competing_head):
    company, service, run, publish_source = seeded_service(tmp_path)
    before_artifacts = service.storage.get_artifacts(run.run_id)
    save = service.storage.save_run_with_artifacts
    winner = {}
    def race(*args, **kwargs):
        if competing_head == "report":
            winner.update(service.storage.append_report_revision(run.run_id, {"thesis": "newer accepted analysis"}))
        elif competing_head == "financial":
            publication = publish_source().publication
            assert publication["state"] == "promoted"
        else:
            service.storage.save_run(replace(run, errors=["newer run checkpoint"]))
        winner["stored_run"] = deepcopy(service.storage.get_run(run.run_id))
        return save(*args, **kwargs)
    service.storage.save_run_with_artifacts = race
    with pytest.raises(RuntimeError, match="(REPORT_REVISION|FINANCIAL_GENERATION|RESEARCH_RUN_SNAPSHOT)_CONFLICT"):
        service._rebuild_financial_artifacts(run.run_id, run.to_dict(), company)
    assert service.storage.get_artifacts(run.run_id) == before_artifacts
    assert service.storage.get_run(run.run_id) == winner["stored_run"]
    if competing_head == "report":
        assert service.storage.latest_report_revision(run.run_id)["revision_id"] == winner["revision_id"]


def test_refresh_preserves_analysis_and_marks_it_stale_without_calling_a_model(tmp_path):
    company, service, run, _publish_source = seeded_service(tmp_path)
    before = service.storage.get_artifacts(run.run_id)
    kinds = service._rebuild_financial_artifacts(run.run_id, run.to_dict(), company)
    assert set(kinds) == {"deterministic-financial-summary", "research-report"}
    after = service.storage.get_artifacts(run.run_id)
    for artifact in before:
        assert artifact in after
    overlay = service.storage.latest_report_revision(run.run_id)["payload"]
    assert overlay["base_report_artifact_id"] == "refresh-run:base"
    assert overlay["financial_refresh"]["qualitative_snapshot_stale"] is True
    assert overlay["financial_refresh"]["model_called"] is False
    summary = next(item for item in after if item["artifact_type"] == "deterministic-financial-summary")
    assert summary["content"]["metrics"][0]["revenue"] == 1000
    assert summary["content"]["evidence"]


def test_refresh_preserves_unregistered_run_extension_fields(tmp_path):
    import json
    company, service, run, _publish_source = seeded_service(tmp_path)
    stored = service.storage.get_run(run.run_id)
    payload = json.loads(stored["payload_json"])
    payload["research_extension"] = {"notes": ["complete text from an installed research pack"]}
    with service.storage.connect() as db:
        db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?", (json.dumps(payload), run.run_id))
    service._rebuild_financial_artifacts(run.run_id, payload, company)
    assert json.loads(service.storage.get_run(run.run_id)["payload_json"])["research_extension"] == payload["research_extension"]


def test_repeated_financial_refresh_does_not_lose_the_original_analysis(tmp_path):
    from openthesis.report_revisions import resolve_report_artifact
    company, service, run, _publish_source = seeded_service(tmp_path)
    original = service.storage.get_artifacts(run.run_id)
    for _index in range(3):
        service._rebuild_financial_artifacts(run.run_id, run.to_dict(), company)
    artifacts = service.storage.get_artifacts(run.run_id)
    latest = service.storage.latest_report_revision(run.run_id)["payload"]
    from openthesis.application.reports import artifacts_for_report_projection
    report = resolve_report_artifact(artifacts_for_report_projection(artifacts, latest))
    assert report["content"]["business_model"] == original[0]["content"]["business_model"]
    assert report["content"]["financial_refresh"]["model_called"] is False
    assert report["content"]["financial_refresh"]["qualitative_snapshot_stale"] is True
    assert all(item in artifacts for item in original)


@pytest.mark.parametrize("when", ["before_refresh", "after_staged_revision"])
def test_cancelled_refresh_cannot_commit_a_report_or_run_update(tmp_path, monkeypatch, when):
    company, service, run, _publish_source = seeded_service(tmp_path)
    before_run = service.storage.get_run(run.run_id)
    before_artifacts = service.storage.get_artifacts(run.run_id)
    before_head = service.storage.latest_report_revision(run.run_id)
    cancelled = Event()
    if when == "before_refresh":
        cancelled.set()
    else:
        append_revision = service.storage._append_report_revision_db
        def cancel_after_staging(*args, **kwargs):
            value = append_revision(*args, **kwargs)
            cancelled.set()
            return value
        monkeypatch.setattr(service.storage, "_append_report_revision_db", cancel_after_staging)
    with pytest.raises(CancelledError):
        service._financial_artifact_refresher.refresh(run.run_id, company, cancel_check=cancelled.is_set)
    assert cancelled.is_set()
    assert service.storage.get_run(run.run_id) == before_run
    assert service.storage.get_artifacts(run.run_id) == before_artifacts
    assert service.storage.latest_report_revision(run.run_id) == before_head


def test_refresh_refuses_a_changed_proof_even_when_fact_and_generation_heads_are_unchanged(tmp_path):
    company, service, run, _publish_source = seeded_service(tmp_path)
    before_artifacts = service.storage.get_artifacts(run.run_id)
    before_run = service.storage.get_run(run.run_id)
    before_head = service.storage.latest_report_revision(run.run_id)
    before_facts = service.storage.get_financial_research_snapshot(company.cik)
    save = service.storage.save_run_with_artifacts
    def replacing_evidence(*args, **kwargs):
        with service.storage.connect() as db:
            db.execute("UPDATE financial_evidence SET excerpt='changed proof after capture' WHERE evidence_id=("
                       "SELECT evidence_id FROM financial_evidence ORDER BY evidence_id LIMIT 1)")
        return save(*args, **kwargs)
    service.storage.save_run_with_artifacts = replacing_evidence
    with pytest.raises(RuntimeError, match="FINANCIAL_GENERATION_CONFLICT"):
        service._financial_artifact_refresher.refresh(run.run_id, company)
    assert service.storage.get_financial_research_snapshot(company.cik) == before_facts
    assert service.storage.get_artifacts(run.run_id) == before_artifacts
    assert service.storage.get_run(run.run_id) == before_run
    assert service.storage.latest_report_revision(run.run_id) == before_head


def test_refreshed_run_metadata_tracks_the_exact_published_financial_generation(tmp_path):
    import json
    company, service, run, _publish_source = seeded_service(tmp_path)
    run.data_snapshot = {"financial_generations": {"old-accession": "old-generation"}}
    service.storage.save_run(run)
    service._financial_artifact_refresher.refresh(run.run_id, company)
    payload = json.loads(service.storage.get_run(run.run_id)["payload_json"])
    summary = next(item for item in service.storage.get_artifacts(run.run_id)
                   if item["artifact_type"] == "deterministic-financial-summary")
    assert payload["data_snapshot"]["financial_generations"] == summary["content"]["financial_generations"]
    assert payload["data_snapshot"]["financial_generations"] == service.storage.get_financial_research_snapshot(
        company.cik)["generations"]
