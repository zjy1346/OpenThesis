"""Request-local source-reference aliases preserve evidence text verbatim."""
from __future__ import annotations

import copy
import json

import pytest

from openthesis.research_execution.reference_aliases import (
    decode_references,
    encode_references,
    reference_aliases,
    supplied_bindings,
)


def test_reference_aliases_collect_sorted_unique_source_and_fact_ids_only():
    context = {
        "evidence": [
            {"evidence_id": "z" * 64, "raw_text": "body only, not an ID"},
            {"evidence_id": "a" * 64},
            {"evidence_id": "z" * 64},
            {"raw_text": "missing evidence identity"},
        ],
        "facts": [{"fact_id": "fact-z"}, {"fact_id": "fact-a"}, {"fact_id": "fact-z"}],
        "claim": {"evidence_id": "derived-claim-id", "text": "not part of the frozen ledger"},
    }

    bindings = reference_aliases(json.dumps(context))

    assert bindings == {
        "@E0": "a" * 64,
        "@E1": "z" * 64,
        "@F0": "fact-a",
        "@F1": "fact-z",
    }


def test_encode_decode_changes_reference_fields_but_never_original_text_or_structure():
    evidence_hash = "evidence:" + "0123456789abcdef" * 4
    fact_hash = "fact:" + "fedcba9876543210" * 4
    bindings = {"@E0": evidence_hash, "@E1": "evidence:second", "@F0": fact_hash}
    original_id_text = f"{evidence_hash} {fact_hash} $12.40 FY2025 USD millions"
    value = {
        "evidence_id": evidence_hash,
        "evidence_ids": [evidence_hash, "unknown:evidence"],
        "supporting_evidence_ids": ["evidence:second"],
        "contradicting_evidence_ids": [evidence_hash],
        "source_evidence_ids": ["evidence:second"],
        "input_evidence_ids": [evidence_hash],
        "classification_evidence_ids": ["evidence:second"],
        "source_id": evidence_hash,
        "source_ids": ["evidence:second"],
        "required_evidence_ids": [evidence_hash],
        "fact_id": fact_hash,
        "fact_ids": [fact_hash],
        "input_fact_ids": [fact_hash],
        "accepted_fact_ref": fact_hash,
        "evidence_ref": "evidence:second",
        "source_block_id": "source-block:derived-hash",
        "raw_text": original_id_text,
        "excerpt": original_id_text,
        "text": original_id_text,
        "quote": original_id_text,
        "body": original_id_text,
        "analysis": {"evidence_id": "evidence:long-hash", "text": original_id_text},
        "summary": original_id_text,
        "expression": original_id_text,
        "amount": 12.4,
        "period": "FY2025",
        "unit": "USD millions",
        "source_url": "https://example.test/original",
        "document_sha256": "c" * 64,
        "request_path_ref": ["research_context", "evidence", 0],
        "material_ref": "opaque-material-key",
        "finding_ref": "opaque-finding-key",
        "material_registry": {evidence_hash: {"text": original_id_text}},
    }
    before = copy.deepcopy(value)

    encoded = encode_references(value, bindings)

    assert value == before
    assert encoded["evidence_id"] == "@E0"
    assert encoded["evidence_ids"] == ["@E0", "unknown:evidence"]
    assert encoded["classification_evidence_ids"] == ["@E1"]
    assert encoded["source_id"] == "@E0"
    assert encoded["source_ids"] == ["@E1"]
    assert encoded["fact_id"] == "@F0"
    assert encoded["accepted_fact_ref"] == "@F0"
    assert encoded["source_block_id"] == "source-block:derived-hash"
    for key in ("raw_text", "excerpt", "text", "quote", "body", "summary", "expression"):
        assert encoded[key] == original_id_text
    assert encoded["analysis"] == value["analysis"]
    for key in ("amount", "period", "unit", "source_url", "document_sha256", "request_path_ref",
                "material_ref", "finding_ref", "material_registry"):
        assert encoded[key] == value[key]
    assert decode_references(encoded, bindings) == value


def test_record_table_uses_logical_column_names_and_keeps_order_and_text():
    bindings = {"@E0": "evidence:one", "@E1": "evidence:two",
                "@F0": "fact:one", "@F1": "fact:two"}
    source_text = "Original row cites evidence:one and fact:one; $125.5 for FY2025 in USD millions."
    table = {
        "record_table": "columns-data-v1",
        "groups": [
            {
                "shared": {"document_id": "annual-2025", "unit": "USD millions",
                           "source_evidence_ids": ["evidence:one"]},
                "columns": ["evidence_id", "fact_id", "raw_text", "value", "period"],
                "data": [
                    ["evidence:one", "fact:one", source_text, 125.5, "FY2025"],
                    ["evidence:two", "fact:two", source_text, 90.0, "FY2024"],
                ],
            }
        ],
        "order": [[0, 1], [0, 0]],
    }

    encoded = encode_references(table, bindings)

    assert encoded["groups"][0]["shared"]["source_evidence_ids"] == ["@E0"]
    assert encoded["groups"][0]["columns"] == table["groups"][0]["columns"]
    assert encoded["groups"][0]["data"] == [
        ["@E0", "@F0", source_text, 125.5, "FY2025"],
        ["@E1", "@F1", source_text, 90.0, "FY2024"],
    ]
    assert encoded["order"] == table["order"]
    assert decode_references(encoded, bindings) == table


def test_supplied_bindings_requires_current_complete_source_or_fact_and_unknown_aliases_stay_unknown():
    all_bindings = {
        "@E0": "evidence:body",
        "@E1": "evidence:delegated-only",
        "@E2": "evidence:accepted-fact-backed",
        "@E3": "evidence:incomplete-fact-backed",
        "@E4": "evidence:material-registry-body",
        "@F0": "fact:complete",
        "@F1": "fact:incomplete",
        "@F2": "fact:not-present",
    }
    prompt = {
        "research_context": {
            "evidence": [
                {"evidence_id": "@E0", "raw_text": "complete evidence body"},
                {"evidence_id": "@E2", "accepted_fact_ref": "@F0"},
                {"evidence_id": "@E3", "accepted_fact_ref": "@F1"},
                {"evidence_id": "@E4", "raw_text": {"material_ref": "m:visible"}},
                {"evidence_id": "@E99", "raw_text": "not in original bindings"},
            ],
            "facts": [
                {"fact_id": "@F0", "concept": "revenue", "value": 125.5},
                {"fact_id": "@F1", "concept": "revenue", "value": None},
            ],
            "citations": {"request_path_ref": ["research_context", "evidence", 0]},
        },
        "material_registry": {"m:visible": {"text": "physically supplied source body"}},
        "reading_assignment": {
            "reading_packet": {"delegated_sources": [
                {"evidence_id": "@E1", "reader": "financial-analyst", "topic": "quantitative_facts"},
            ]},
        },
        "claim": {"evidence_ids": ["@E0", "@E1", "@E2", "@E3", "@E4", "@F0", "@F1", "@E99"]},
    }

    supplied = supplied_bindings(json.dumps(prompt), all_bindings)

    assert supplied == {
        "@E0": "evidence:body",
        "@E2": "evidence:accepted-fact-backed",
        "@E4": "evidence:material-registry-body",
        "@F0": "fact:complete",
    }
    decoded = decode_references(prompt["claim"], supplied)
    assert decoded["evidence_ids"] == [
        "evidence:body", "@E1", "evidence:accepted-fact-backed", "@E3",
        "evidence:material-registry-body", "@F0", "@F1", "@E99",
    ]


def test_supplied_bindings_rejects_cyclic_or_overdeep_request_path_refs():
    all_bindings = {"@E0": "evidence:body"}
    cyclic = {"self": {"request_path_ref": ["self"]}}
    with pytest.raises(ValueError):
        supplied_bindings(json.dumps(cyclic), all_bindings)

    deep = {"source": {"evidence_id": "@E0", "raw_text": "complete"}}
    for index in reversed(range(33)):
        deep[f"path-{index}"] = {"request_path_ref": ["path-" + str(index + 1)] if index < 32 else ["source"]}
    with pytest.raises(ValueError):
        supplied_bindings(json.dumps(deep), all_bindings)


def test_prior_analysis_cannot_authorize_its_own_source_alias():
    prompt = {"research_context": {"evidence_partitions": {}}, "prior_artifacts": {
        "claim": {"evidence_id": "@E0", "text": "This is analysis, not the original source."},
        "number": {"fact_id": "@F0", "concept": "revenue", "value": 120}}}
    assert supplied_bindings(prompt, {"@E0": "source:original", "@F0": "fact:original"}) == {}
