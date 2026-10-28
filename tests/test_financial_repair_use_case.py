"""Model-free repairs exercise the same original/compiler/publication seam."""
from concurrent.futures import CancelledError
from dataclasses import replace

import pytest

from openthesis.application.financial_repair import FinancialRepairer
from openthesis.application.financial_refresh import FinancialArtifactRefresher
from openthesis.application.financials import OfficialFinancialRequest
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from test_official_financial_preparation import setup_preparer


def setup_repair(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    run = ResearchRun("repair-interface", company, "wf", "pack", "1", "none", "none", "2026-10-06",
        status=RunStatus.PARTIAL, research_configuration={"annual_history_years": 1})
    original = ResearchArtifact("repair-interface:original", run.run_id, "research-report", "Original",
        {"business_model": {"analysis": "Original full analysis must remain unchanged."}})
    storage.save_run_with_artifacts(run, [original])
    refresher = FinancialArtifactRefresher(storage, preparer.pipeline)
    repairer = FinancialRepairer(storage, preparer, refresher)
    request = OfficialFinancialRequest(company, adapter, annual_history_years=1,
        repair=True, run_id=run.run_id)
    return filing, adapter, preparer, storage, refresher, repairer, request, original


def test_repair_compiles_original_then_healthy_retry_is_a_noop(tmp_path):
    filing, adapter, _preparer, storage, _refresher, repairer, request, original = setup_repair(tmp_path)
    raw = (tmp_path / "original.htm").read_bytes()
    result = repairer.repair(replace(request, force=True))
    assert result.status == "succeeded" and result.targets == (filing.accession_number,)
    assert len(result.accepted) == 6 and not result.rejected
    assert set(result.updated_artifacts) == {"deterministic-financial-summary", "research-report"}
    assert result.recovery_outcome["state"] == "committed"
    assert adapter.download_calls == 0
    assert (tmp_path / "original.htm").read_bytes() == raw
    artifacts = storage.get_artifacts(request.run_id)
    assert next(item for item in artifacts if item["artifact_id"] == original.artifact_id)["content"] == original.content
    head = storage.latest_report_revision(request.run_id)
    financial = storage.get_financial_research_snapshot(request.company.cik)
    second = repairer.repair(request)
    assert second.status == "succeeded" and second.targets == ()
    assert not second.accepted and not second.rejected and not second.updated_artifacts
    assert storage.get_artifacts(request.run_id) == artifacts
    assert storage.latest_report_revision(request.run_id) == head
    assert storage.get_financial_research_snapshot(request.company.cik) == financial


def test_refresh_failure_retains_accepted_facts_but_is_not_committed(tmp_path, monkeypatch):
    _filing, _adapter, _preparer, storage, refresher, repairer, request, original = setup_repair(tmp_path)
    before = storage.latest_report_revision(request.run_id)
    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("REPORT_REVISION_CONFLICT")
    monkeypatch.setattr(refresher, "refresh", fail_refresh)
    result = repairer.repair(replace(request, force=True))
    assert len(storage.get_facts(request.company.cik)) == 6
    assert len(result.accepted) == 6 and result.status == "partial"
    assert result.recovery_outcome["state"] == "needs_action"
    assert result.recovery_outcome["diagnostic_code"] == "REPORT_REVISION_CONFLICT"
    assert result.next_action == "refresh_recovery_plan"
    assert not result.updated_artifacts
    assert storage.latest_report_revision(request.run_id) == before
    assert storage.get_artifacts(request.run_id)[0]["content"] == original.content


def test_unknown_failure_remains_internal_and_preserves_the_previous_report(tmp_path, monkeypatch):
    _filing, _adapter, preparer, storage, _refresher, repairer, request, _original = setup_repair(tmp_path)
    before = storage.get_artifacts(request.run_id)
    calls = []
    def fail_prepare(*_args, **_kwargs):
        calls.append(1)
        raise TypeError("private arbitrary exception text")
    monkeypatch.setattr(preparer, "prepare", fail_prepare)
    result = repairer.repair(request)
    assert calls == [1]
    assert result.status == "failed" and result.terminal_state == "failed_internal"
    assert result.recovery_outcome["diagnostic_code"] == "FINANCIAL_REPAIR_INTERNAL_ERROR"
    assert "private arbitrary" not in str(result.to_dict())
    assert storage.get_artifacts(request.run_id) == before
    assert storage.get_facts(request.company.cik) == []


def test_cancelled_repair_does_not_discover_or_publish(tmp_path):
    _filing, adapter, _preparer, storage, _refresher, repairer, request, _original = setup_repair(tmp_path)
    before = storage.get_artifacts(request.run_id)
    with pytest.raises(CancelledError):
        repairer.repair(request, cancel_check=lambda: True)
    assert adapter.list_limits == [] and adapter.download_calls == 0
    assert storage.get_artifacts(request.run_id) == before
    assert storage.get_facts(request.company.cik) == []


@pytest.mark.parametrize("run_id,repair", [("", True), ("repair-interface", False)])
def test_interface_rejects_non_repair_or_unbound_requests(tmp_path, run_id, repair):
    _filing, _adapter, _preparer, storage, _refresher, repairer, request, _original = setup_repair(tmp_path)
    before = storage.get_artifacts(request.run_id)
    with pytest.raises(ValueError, match="CAPTURED_FINANCIAL_REPAIR_REQUEST_REQUIRED"):
        repairer.repair(replace(request, repair=repair, run_id=run_id))
    assert storage.get_artifacts(request.run_id) == before


def test_repair_rejects_a_different_company_before_contacting_any_source(tmp_path):
    _filing, adapter, _preparer, storage, _refresher, repairer, request, _original = setup_repair(tmp_path)
    before = storage.get_artifacts(request.run_id)
    wrong_company = replace(request.company, cik="0000000001", security_id="US:0000000001")
    with pytest.raises(ValueError, match="FINANCIAL_REPAIR_RUN_IDENTITY_MISMATCH"):
        repairer.repair(replace(request, company=wrong_company))
    assert adapter.list_limits == [] and adapter.download_calls == 0
    assert storage.get_artifacts(request.run_id) == before
