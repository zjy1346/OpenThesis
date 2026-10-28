"""Source pre-reading is preserved without impersonating final counter work."""
import pytest

from openthesis.research import _trusted_stage_result, verify_agent_output
from openthesis.report_content import trusted_snapshot_for_artifact
from openthesis.report_document import assemble_report_document, render_markdown
from test_analysis_presentation_policy import SOURCE, TEXT


@pytest.mark.parametrize("stage,section", [
    ("source-industry-analysis", "industry_changes_and_value_allocation"),
    ("source-risk-analysis", "source_risk_review"),
])
@pytest.mark.parametrize("language", ["en", "zh-CN", "zh-Hant"])
def test_source_stage_body_has_its_own_readable_chapter(stage, section, language):
    refs = {SOURCE["evidence_id"]: SOURCE}
    raw = {"analysis": TEXT, "evidence_ids": list(refs), "claims": []}
    verification = verify_agent_output(raw, set(refs), language, refs)
    trusted = _trusted_stage_result(raw, verification, refs, language)
    assert trusted["_verification_state"] == "completed_partial"
    artifact = {"artifact_id": stage, "run_id": "source-read", "artifact_type": "stage-checkpoint",
        "content": {"result": {"stage_id": stage, "input_digest": "source-bound-input",
            "validation": {"state": "completed_partial", "trusted_output": trusted}}}}
    snapshot = trusted_snapshot_for_artifact(artifact, evidence_records=list(refs.values()))
    assert snapshot and all(node["display_section_id"] == section for node in snapshot["nodes"])
    document = assemble_report_document("source-read", [artifact], language=language)
    chapter = next(item for item in document.sections if item.section_id == section)
    assert TEXT in render_markdown(document)
    assert chapter.title != "Supplementary Research"
    assert not document.readiness.complete
    assert not any(item.section_id == "opposing-views" and item.is_substantive for item in document.sections)
