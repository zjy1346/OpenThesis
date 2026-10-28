import pytest

from openthesis.domain import ResearchArtifact
from openthesis.markets import build_company
from openthesis.service import AppService, _canonical_retry_snapshot
from openthesis.storage import Storage
from test_trusted_minimal_presentation import seed, commit_report


def test_numeric_gap_is_local_resume_issue_not_a_blanket_analysis_veto(tmp_path):
    store = Storage(tmp_path)
    company = build_company("688981.SH", "Fixture", reporting_currency="CNY")
    store.save_company(company)
    issues = []
    assert _canonical_retry_snapshot(store, company, {}, partial_issues=issues) == ()
    assert issues == [{"code": "FINANCIAL_DATA_QUALITY_FAILED", "path": "financial_snapshot"}]
    with pytest.raises(ValueError, match="FINANCIAL_DATA_QUALITY_FAILED"):
        _canonical_retry_snapshot(store, company, {})


def test_formal_report_read_retains_body_when_unrelated_artifact_is_corrupt(tmp_path):
    service = AppService(tmp_path)
    seed(service.storage)
    commit_report(service.storage)
    before = service.get_report("retained-run")
    service.storage.save_artifact(ResearchArtifact("bad-optional", "retained-run", "diagnostic-note", "Note", {"note": "safe"}))
    with service.storage.connect() as db:
        db.execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?", ('{"bad":', "bad-optional"))
    after = AppService(tmp_path).get_report("retained-run")
    assert "Operations serve industrial customers." in after["markdown"]
    assert "Operations serve industrial customers." in before["markdown"]
    assert after["report_read_state"] != "unavailable"
    assert any(item.get("artifact_id") == "bad-optional" for item in after.get("report_read_diagnostics", []))
