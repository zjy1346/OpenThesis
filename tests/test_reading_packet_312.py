import json
from copy import deepcopy

from openthesis.research_materials import canonical_request_materials, prompt_materials


def test_dynamic_findings_do_not_change_frozen_source_prefix_and_bodies_are_resolvable():
    body = "Complete official source wording and its important qualification. " * 20
    materials = {"sources": [{"evidence_id": "filing:source", "raw_text": body}]}
    first = canonical_request_materials(materials, {})
    second = canonical_request_materials(materials, {"new_analysis": {
        "evidence_id": "filing:new", "text": "Different dynamic analysis. " * 30}})
    assert first[0] == second[0]
    assert first[2] == second[2]
    assert len(second[1]["findings_registry"]) == 1
    assert all(not item["text"].startswith("Different dynamic") for item in second[2].values())
    source = second[0]["sources"][0]["raw_text"]["material_ref"]
    assert second[2][source]["text"] == body
    finding = second[1]["new_analysis"]["text"]["finding_ref"]
    assert second[1]["findings_registry"][finding]["text"] == "Different dynamic analysis. " * 30


def test_reading_view_removes_only_named_audit_fields_retains_units_and_opposing_sources():
    record = {"evidence_id": "fact:revenue", "fact_id": "revenue", "kind": "financial_fact",
        "concept": "revenue", "value": 100, "currency": "CNY", "unit": "CNY", "scope": "consolidated",
        "fiscal_year": 2025, "end_date": "2025-12-31", "raw_text": "Revenue (CNY): 100",
        "normalization_lineage": ["local-parser-audit"] * 100,
        "source_page": 7, "document_sha256": "a" * 64, "generation_id": "local-lease"}
    risk = {"evidence_id": "filing:risk", "kind": "filing_text", "topic": "risk_factors",
        "document_id": "official", "excerpt": "Business faces a serious opposing risk.", "source_url": "https://example.test/source"}
    unknown = {**risk, "evidence_id": "filing:unknown", "topic": "new_topic", "excerpt": "Unknown-topic original analysis context."}
    audit = {**risk, "evidence_id": "filing:audit", "topic": "audit", "excerpt": "Original audit qualifications remain relevant."}
    context = {"evidence": [record, risk, unknown, audit], "facts": [record], "company": {"cik": "fixture"}}
    before = deepcopy(context)
    packet = prompt_materials(context, "growth-opportunity-analyst", required_evidence_ids=["filing:audit"])
    fact = packet["accepted_facts"][0]
    for key in ("value", "currency", "unit", "scope", "fiscal_year", "end_date", "raw_text", "document_sha256"):
        assert fact[key] == record[key]
    assert "normalization_lineage" not in fact
    assert "generation_id" not in fact
    all_ids = {item["evidence_id"] for rows in packet["evidence_partitions"].values() for item in rows}
    assert {"filing:risk", "filing:unknown", "filing:audit"} <= all_ids
    assert packet["reading_packet"]["unknown_topic_reader"] == "growth-opportunity-analyst"
    assert context == before
    assert len(json.dumps(packet)) < len(json.dumps(prompt_materials(context, "financial-analyst"))) + 5000
