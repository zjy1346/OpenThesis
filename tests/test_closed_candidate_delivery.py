"""Partial transport recovery still uses the real proof/commit/read contract."""
from copy import deepcopy
from unittest.mock import patch

from openthesis.domain import ResearchArtifact
from openthesis.demo import DEMO_COMPANY
from openthesis.research import _supported_synthesis_sections
from openthesis.research_execution.model_output import recover_closed_json
from openthesis.report_content import assemble_trusted_report_payload
from openthesis.service import AppService
from openthesis.storage import Storage
from test_trusted_emergency_content import seed, commit_report


def test_closed_json_body_is_verified_committed_and_read_after_restart(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    text = 'Operations serve industrial customers.'
    wire = '{"business_model":{"text":"' + text + '","evidence_ids":["filing:business"]},"revenue":123'
    candidate, issues = recover_closed_json(wire)
    assert candidate["business_model"]["text"] == text and "revenue" not in candidate and issues
    refs = {"filing:business": {"kind": "filing_text", "excerpt": text, "company_cik": DEMO_COMPANY.cik,
        "end_date": "2025-12-31", "fiscal_year": 2025, "fiscal_period": "FY"}}
    safe, proof = _supported_synthesis_sections(candidate, set(refs), refs, "en")
    store.save_artifact(ResearchArtifact("closed-json", "retained-run", "research-report", "Recovered",
        {"mode": "synthesis-incomplete", "report": safe, "verification": {"section_validation": proof}}))
    service = AppService(tmp_path)
    result = service.get_report("retained-run", language="en")
    assert text in result["markdown"] and text in result["html"]
    assert not result["research_complete"]


def test_progressive_commit_ignores_corrupt_auxiliary_row_but_keeps_diagnostic(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    original = commit_report(store)
    store.save_artifact(ResearchArtifact("current-input", "retained-run", "research-report", "Current input",
        {**deepcopy(original.content), "publication_input_digest": "same-input"}))
    payload = assemble_trusted_report_payload(store.get_artifacts("retained-run"), run_id="retained-run", input_digest="same-input")
    assert payload
    store.save_artifact(ResearchArtifact("bad-note", "retained-run", "diagnostic-note", "Note", {"note": "safe"}))
    with store.connect() as db:
        db.execute("UPDATE artifacts SET payload_json='{' WHERE artifact_id='bad-note'")
    store.save_artifact(ResearchArtifact("progressive", "retained-run", "research-report", "Retained", payload))
    result = AppService(tmp_path).get_report("retained-run", language="en")
    assert 'Operations serve industrial customers.' in result["markdown"]
    records = Storage(tmp_path).get_artifacts("retained-run")
    assert any(row["artifact_type"] == "storage-read-diagnostic" for row in records)


def test_unregistered_section_is_not_lost_but_unknown_role_stays_a_candidate(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    text = 'Operations serve industrial customers.'
    refs = {"filing:business": {"kind": "filing_text", "excerpt": text, "company_cik": DEMO_COMPANY.cik,
        "end_date": "2025-12-31", "fiscal_year": 2025, "fiscal_period": "FY"}}
    candidate = {"custom_pack_discussion": {"text": text, "evidence_ids": list(refs)},
        "unclassified_sibling": {"text": text, "role": "unregistered_power", "evidence_ids": list(refs)},
        "unclassified_parent": {"text": text, "role": "unregistered_power", "evidence_ids": list(refs),
            "independent_child": {"role": "source_summary", "text": text, "evidence_ids": list(refs)}}}
    safe, audit = _supported_synthesis_sections(candidate, set(refs), refs, "en")
    assert safe["custom_pack_discussion"]["text"] == text and "unclassified_sibling" not in safe
    assert {"path": "unclassified_sibling.role", "reason": "content_role_unclassified"} in audit["quarantined_paths"]
    assert "text" not in safe["unclassified_parent"]
    assert safe["unclassified_parent"]["independent_child"]["text"] == text
    store.save_artifact(ResearchArtifact("extensible-report", "retained-run", "research-report", "Supplementary",
        {"report": safe, "verification": {"section_validation": audit}}))
    result = AppService(tmp_path).get_report("retained-run", language="zh-CN")
    assert text in result["markdown"] and text in result["html"]
    assert "补充研究" in result["markdown"] and not result["research_complete"]
