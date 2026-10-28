"""Historical financial-refresh chains retain complete original analysis."""
from copy import deepcopy

import pytest

from openthesis.report_revisions import resolve_report_artifact


def artifact(identity, content):
    return {"artifact_id": identity, "artifact_type": "research-report", "content": content}


def test_multiple_immutable_overlays_preserve_the_full_original_payload():
    original = {"report": {"business_model": {"analysis": "Full original analysis", "segments": [{"text": "Legitimate segment text"}]}},
        "custom_pack_section": {"nested": {"complete": "Unregistered but meaningful extension"}}}
    rows = [artifact("base", original)]
    for index in range(3):
        rows.append(artifact(f"overlay-{index}", {"mode": "financial-refresh-overlay",
            "base_report_artifact_id": rows[-1]["artifact_id"],
            "financial_refresh": {"fact_count": index + 1, "model_called": False, "qualitative_snapshot_stale": True}}))
    captured = deepcopy(rows)
    resolved = resolve_report_artifact(rows)
    assert resolved["content"]["report"] == original["report"]
    assert resolved["content"]["custom_pack_section"] == original["custom_pack_section"]
    assert resolved["content"]["financial_refresh"]["fact_count"] == 3
    assert resolved["content"]["base_report_artifact_id"] == "base"
    assert rows == captured


@pytest.mark.parametrize("base_id", ["missing", "cycle"])
def test_missing_or_cyclic_overlay_is_diagnostic_not_false_completion(base_id):
    rows = [artifact("unrelated", {"report": {"thesis": "Do not borrow unrelated healthy content"}}),
        artifact("cycle", {"mode": "financial-refresh-overlay", "base_report_artifact_id": base_id,
            "financial_refresh": {"model_called": False}})]
    captured = deepcopy(rows)
    resolved = resolve_report_artifact(rows)
    assert resolved["content"]["report"] == {}
    assert resolved["content"]["research_complete"] is False
    expected = "REPORT_OVERLAY_BASE_MISSING" if base_id == "missing" else "REPORT_OVERLAY_CHAIN_INVALID"
    assert resolved["content"]["diagnostic_code"] == expected
    assert rows == captured
