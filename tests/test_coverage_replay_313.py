from copy import deepcopy
from openthesis.report_content import coverage_content_view, trusted_snapshot_for_artifact
from openthesis.research_section_validation import project_supported_sections
from openthesis.research_coverage import ResearchCoverageContract
from test_assertion_retention_312 import SOURCE


def artifacts_for(body):
    safe, audit = project_supported_sections({"stage": {"analysis": body, "evidence_ids": [SOURCE["evidence_id"]]}},
        ["stage"], lambda _: {}, lambda *_: True, {SOURCE["evidence_id"]})
    stage = {"run_id": "qa313", "artifact_id": "stage313", "artifact_type": "stage-checkpoint",
        "content": {"result": {"stage_id": "business-analyst", "input_digest": "unchanged313", "validation": {
            "state": "completed_verified", "trusted_output": {**safe["stage"], "_section_validation": audit}}}}}
    snapshot = {"run_id": "qa313", "artifact_type": "trusted-content-snapshot",
        "content": trusted_snapshot_for_artifact(stage, evidence_records=[SOURCE])}
    final = {"run_id": "qa313", "artifact_id": "final313", "artifact_type": "research-report",
        "content": {"report": {"claims": []}, "verification": {"passed": False}}}
    return final, [stage, snapshot, final]


def test_displayed_committed_stage_proofs_reach_obligation_without_model_calls():
    final, artifacts = artifacts_for("Equipment sales are the core operating activity.")
    before = deepcopy(artifacts)
    report, verification = coverage_content_view(final, artifacts)
    row = next(row for row in ResearchCoverageContract().assess(report, verification,
        evidence_records={SOURCE["evidence_id"]: SOURCE}).obligations if row.obligation_id == "company_business_model")
    assert row.state == "fulfilled"
    assert verification["passed"] is False, "local section recovery cannot override overall verification"
    assert artifacts == before


def test_existing_changed_prose_never_borrows_a_different_stage_proof():
    final, artifacts = artifacts_for("Equipment sales are the core operating activity.")
    final["content"]["report"]["business_model"] = {"analysis": "The company no longer sells equipment.", "evidence_ids": [SOURCE["evidence_id"]]}
    report, verification = coverage_content_view(final, artifacts)
    row = next(row for row in ResearchCoverageContract().assess(report, verification,
        evidence_records={SOURCE["evidence_id"]: SOURCE}).obligations if row.obligation_id == "company_business_model")
    assert row.state == "verification_failed"
    assert report["business_model"]["analysis"] == final["content"]["report"]["business_model"]["analysis"]
