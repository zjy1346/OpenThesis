"""Lossless packet/delegation boundary tests using synthetic source records."""
from __future__ import annotations

import json

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.research import ResearchContext, _agent_prompt_bundle
from openthesis.research_execution.repair_context import repair_context
from openthesis.research_execution.reference_aliases import decode_references, encode_references, reference_aliases
from openthesis.research_execution.source_compiler import compact_record_tree, expand_record_tables


UNIT_ID = "unit:annual-2025:revenue"
STATEMENT_ID = "statement:annual-2025:income"
UNADMITTED_ID = "source-row:annual-2025:unadmitted"
FACT_ID = "fact:revenue-2025"
FACT_BODY = "The source filing reports consolidated revenue of USD 125.5 million for FY2025. " * 5
RISK_ID = "evidence:risk-denial-2025"
UNKNOWN_ID = "evidence:unknown-body-2025"
UNIT_BODY = "The original statement header defines this revenue row in USD millions for fiscal year 2025. " * 5
STATEMENT_BODY = "The original statement shows reported revenue with its period, document, and consolidated scope. " * 5
UNADMITTED_BODY = "This unadmitted source row remains an observation only and is not promoted to an accepted financial fact. " * 5
RISK_BODY = (
    "The audited annual filing explicitly states that no material product safety incidents occurred during FY2025; "
    "this denial is limited to the reported period and does not claim future risk is absent. "
) * 3
UNKNOWN_BODY = "The filing contains this uncategorized operational disclosure, which must remain available verbatim. " * 5


def _fact(index: int = 0) -> dict:
    return {
        "fact_id": "revenue-2025" if index == 0 else f"metric-{index}-2025",
        "concept": "revenue" if index == 0 else f"metric_{index}",
        "value": 125.5 + index,
        "currency": "USD",
        "unit": "USD millions",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "end_date": "2025-12-31",
        "scope": "consolidated",
        "document_id": "annual-2025",
        "document_sha256": "a" * 64,
        "source_url": "https://example.test/annual-2025",
    }


def _source(evidence_id: str, body: str, *, kind: str = "filing_text", topic: str = "other_topic") -> dict:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "topic": topic,
        "title": f"Annual filing · {topic}",
        "document_id": "annual-2025",
        "document_sha256": "a" * 64,
        "source_url": "https://example.test/annual-2025",
        "fiscal_period": "FY2025",
        "period_end": "2025-12-31",
        "unit": "USD millions",
        "raw_text": body,
        "excerpt": body,
    }


def _context(*, roles=("financial-analyst", "accounting-risk-analyst"), facts=True, fact_count=1):
    accepted_facts = [_fact(index) for index in range(fact_count)] if facts else []
    evidence = [
        {**_fact(), "evidence_id": FACT_ID, "kind": "financial_fact", "raw_text": FACT_BODY},
        _source(UNIT_ID, UNIT_BODY, kind="statement_unit_header"),
        _source(STATEMENT_ID, STATEMENT_BODY, kind="statement_line"),
        _source(UNADMITTED_ID, UNADMITTED_BODY, kind="unadmitted_source_row"),
        _source(RISK_ID, RISK_BODY, topic="risk_factors"),
        _source(UNKNOWN_ID, UNKNOWN_BODY, topic="issuer_specific_unknown_topic"),
    ]
    return ResearchContext(
        DEMO_COMPANY,
        accepted_facts,
        [],
        [],
        evidence,
        source_reader_roles=tuple(roles),
        research_as_of="2026-10-24",
    )


def _follow_path(root, path):
    value = root
    for component in path:
        if isinstance(value, dict) and value.get("record_table") == "columns-data-v1":
            value = expand_record_tables(value)
        value = value[component]
    return value


def _resolve_path_refs(value, root, resolving=()):
    if isinstance(value, dict):
        if set(value) == {"request_path_ref"}:
            path = value["request_path_ref"]
            assert isinstance(path, list)
            identity = tuple(path)
            assert identity not in resolving, f"request_path_ref cycle at {path!r}"
            target = _follow_path(root, path)
            return _resolve_path_refs(target, root, (*resolving, identity))
        return {key: _resolve_path_refs(child, root, resolving) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_path_refs(child, root, resolving) for child in value]
    return value


def _expand_request(payload, original_context_json):
    expanded = expand_record_tables(payload)
    resolved = _resolve_path_refs(expanded, expanded)
    decoded = decode_references(resolved, reference_aliases(original_context_json))
    registry = decoded.get("material_registry", {})
    facts = decoded.get("common_core", {}).get("accepted_facts", [])
    facts_by_id = {fact.get("fact_id"): fact for fact in facts if isinstance(fact, dict)}

    def material_refs(value):
        if isinstance(value, dict):
            if set(value) == {"material_ref"}:
                reference = value["material_ref"]
                assert reference in registry, f"unresolved material_ref: {reference}"
                return registry[reference]["text"]
            if isinstance(value.get("accepted_fact_ref"), str):
                reference = value["accepted_fact_ref"]
                assert reference in facts_by_id, f"unresolved accepted_fact_ref: {reference}"
                overrides = {key: material_refs(child) for key, child in value.items()
                             if key != "accepted_fact_ref"}
                return {**facts_by_id[reference], **overrides}
            return {key: material_refs(child) for key, child in value.items()}
        if isinstance(value, list):
            return [material_refs(child) for child in value]
        return value

    return material_refs(decoded)


def _partition_records(payload):
    return [record for records in payload["research_context"].get("evidence_partitions", {}).values()
            for record in records if isinstance(record, dict)]


def _reading_delegations(payload):
    return payload["reading_assignment"]["reading_packet"].get("delegated_sources", [])


def test_financial_roles_share_identical_prefix_through_agent_field_but_keep_independent_instructions():
    context = _context()
    original_context_json = context.compact_json()
    financial = _agent_prompt_bundle(
        "financial-analyst", "Financial interpretation instructions.", "en", original_context_json, {}
    )[1]
    accounting = _agent_prompt_bundle(
        "accounting-risk-analyst", "Independent accounting-risk instructions.", "en", original_context_json, {}
    )[1]
    financial_agent_key = financial.index('"agent":')
    accounting_agent_key = accounting.index('"agent":')

    assert financial[:financial_agent_key] == accounting[:accounting_agent_key]
    financial_payload = json.loads(financial)
    accounting_payload = json.loads(accounting)
    assert financial_payload["agent"] == "financial-analyst"
    assert accounting_payload["agent"] == "accounting-risk-analyst"
    assert financial_payload["task_instructions"] != accounting_payload["task_instructions"]
    assert financial_payload["source_package"] == accounting_payload["source_package"]
    assert financial_payload["research_context"]["material_adequacy"] == {
        "request_path_ref": ["reading_assignment", "material_adequacy"]
    }
    assert accounting_payload["research_context"]["reading_packet"] == {
        "request_path_ref": ["reading_assignment", "reading_packet"]
    }
    financial_resolved = _expand_request(financial_payload, original_context_json)
    accounting_resolved = _expand_request(accounting_payload, original_context_json)
    assert financial_resolved["reading_assignment"]["reading_packet"]["reader"] == "financial-analyst"
    assert accounting_resolved["reading_assignment"]["reading_packet"]["reader"] == "accounting-risk-analyst"
    assert UNIT_BODY in json.dumps(financial_resolved, ensure_ascii=False)
    assert UNIT_BODY in json.dumps(accounting_resolved, ensure_ascii=False)


def test_business_packet_delegates_financial_proofs_only_with_real_reader_and_admitted_facts():
    context = _context()
    original_context_json = context.compact_json()
    prompt = _agent_prompt_bundle(
        "business-analyst", "Analyze supplied business evidence.", "en", original_context_json, {}
    )[1]
    payload = _expand_request(json.loads(prompt), original_context_json)
    delegated = {row["evidence_id"]: row["reader"] for row in _reading_delegations(payload)}

    assert {FACT_ID, UNIT_ID, STATEMENT_ID, UNADMITTED_ID} <= set(delegated)
    assert all(delegated[key] == "financial-analyst" for key in (FACT_ID, UNIT_ID, STATEMENT_ID, UNADMITTED_ID))
    partition_records = _partition_records(payload)
    retained_ids = {record.get("evidence_id") for record in partition_records}
    assert not ({UNIT_ID, STATEMENT_ID, UNADMITTED_ID} & retained_ids)
    assert payload["common_core"]["accepted_facts"] == [_fact()]
    assert RISK_BODY in json.dumps(payload["research_context"], ensure_ascii=False)
    assert UNKNOWN_BODY in json.dumps(payload["research_context"], ensure_ascii=False)

    financial_prompt = _agent_prompt_bundle(
        "financial-analyst", "Read complete financial source records.", "en", original_context_json, {}
    )[1]
    financial_payload = _expand_request(json.loads(financial_prompt), original_context_json)
    financial_ids = {record.get("evidence_id") for record in _partition_records(financial_payload)}
    assert {FACT_ID, UNIT_ID, STATEMENT_ID, UNADMITTED_ID} <= financial_ids


@pytest.mark.parametrize(
    ("roles", "facts"),
    [(("business-analyst",), True), (("financial-analyst",), False)],
    ids=("financial-reader-not-in-frozen-graph", "no-admitted-facts"),
)
def test_business_packet_keeps_financial_proofs_when_delegation_preconditions_are_missing(roles, facts):
    context = _context(roles=roles, facts=facts)
    original_context_json = context.compact_json()
    prompt = _agent_prompt_bundle(
        "business-analyst", "Analyze supplied business evidence.", "en", original_context_json, {}
    )[1]
    payload = _expand_request(json.loads(prompt), original_context_json)
    delegated_ids = {row["evidence_id"] for row in _reading_delegations(payload)}
    retained_ids = {record.get("evidence_id") for record in _partition_records(payload)}

    assert not ({UNIT_ID, STATEMENT_ID, UNADMITTED_ID} & delegated_ids)
    assert {UNIT_ID, STATEMENT_ID, UNADMITTED_ID} <= retained_ids


def test_cited_source_that_was_delegated_is_physically_restored_in_late_evidence():
    context = _context()
    original_context_json = context.compact_json()
    prior = {"business_stage": {"claims": [{"text": "Revenue was reported for FY2025.", "evidence_ids": [FACT_ID]}]}}
    prompt = _agent_prompt_bundle(
        "business-analyst", "Analyze supplied business evidence.", "en", original_context_json, prior
    )[1]
    payload = _expand_request(json.loads(prompt), original_context_json)
    delegated_ids = {row["evidence_id"] for row in _reading_delegations(payload)}
    late_records = payload.get("late_evidence", [])

    assert FACT_ID in delegated_ids
    matched = [record for record in late_records if record.get("evidence_id") == FACT_ID]
    assert len(matched) == 1
    assert matched[0]["raw_text"] == FACT_BODY
    assert matched[0]["value"] == 125.5
    assert matched[0]["unit"] == "USD millions"
    assert matched[0]["document_id"] == "annual-2025"


def test_repair_context_resolves_columns_core_aliases_and_preserves_risk_denial_source():
    context = _context(fact_count=8)
    original_context_json = context.compact_json()
    payload = json.loads(_agent_prompt_bundle(
        "research-synthesizer", "Repair the named financial-quality section.", "en",
        original_context_json, {},
    )[1])

    assert payload["common_core"]["accepted_facts"]["record_table"] == "columns-data-v1"
    assert payload["research_context"]["accepted_facts"] == {
        "request_path_ref": ["common_core", "accepted_facts"]
    }
    _expand_request(payload, original_context_json)  # Proves request aliases resolve without a cycle or dangling path.
    repaired = repair_context(json.dumps(payload, ensure_ascii=False), ("financial_quality",))
    repaired = _expand_request(repaired, original_context_json)

    assert len(repaired["research_context"]["accepted_facts"]) == 8
    assert repaired["research_context"]["accepted_facts"] == [_fact(index) for index in range(8)]
    assert "risk_disclosures" in repaired["research_context"]["evidence_partitions"]
    assert not any("request_path_ref" in item or "record_table" in item
                   for item in _walk_dicts(repaired))
    assert any(record.get("text") == RISK_BODY for record in repaired["material_registry"].values())
    assert any(record.get("text") == UNKNOWN_BODY for record in repaired["material_registry"].values())


def test_compact_record_tree_strictly_roundtrips_nested_mixed_values_and_duplicate_rows():
    shared_text = "This complete original source paragraph is shared, not summarized or rewritten. " * 40
    records = []
    for index in range(8):
        nested = {"values": [None, True, 0, 1.25 + index / 2, {"deep": ["text", False, None]}]}
        if index == 0:
            nested["optional"] = None
        elif index > 1:
            nested["optional"] = index / 4
        records.append({
            "evidence_id": f"evidence:nested-{index}",
            "document_id": "nested-source-doc",
            "raw_text": shared_text,
            "excerpt": shared_text,
            "nested_payload": nested,
        })
    records.append(json.loads(json.dumps(records[3])))
    original = {"outer": {"records": records, "scalar_values": [None, True, False, 0, 0.0, 1.25, ""]}}

    compacted = compact_record_tree(original)

    assert compacted["outer"]["records"]["record_table"] == "columns-data-v1"
    assert expand_record_tables(compacted) == original


def test_evidence_index_short_id_roundtrip_does_not_change_source_body():
    evidence_id = "evidence:" + "a" * 64
    body = f"The original source body includes the literal identifier {evidence_id}; keep it byte-for-byte. " * 3
    source = _source(evidence_id, body, topic="risk_factors")
    original_context_json = json.dumps({"evidence": [source], "facts": []}, ensure_ascii=False)
    bindings = reference_aliases(original_context_json)
    alias = next(alias for alias, original in bindings.items() if original == evidence_id)
    original_request = {
        "research_context": {
            "evidence": [evidence_id],
            "evidence_partitions": {"risk_disclosures": [source]},
        }
    }

    encoded = encode_references(original_request, bindings)

    assert encoded["research_context"]["evidence"] == [alias]
    encoded_source = encoded["research_context"]["evidence_partitions"]["risk_disclosures"][0]
    assert encoded_source["evidence_id"] == alias
    assert encoded_source["raw_text"] == body
    assert encoded_source["excerpt"] == body
    assert decode_references(encoded, bindings) == original_request
    assert decode_references(encoded, bindings)["research_context"]["evidence_partitions"]["risk_disclosures"][0]["raw_text"] == body


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)
