"""A page receipt cannot masquerade as an original-document identity."""
from dataclasses import replace
import json

import pytest

from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.vision_financials import parse_vision_json
from test_vision_financials import RecordingVisionProvider, _company, _filing, _page


def candidate():
    payload = RecordingVisionProvider().generate_vision("", "original_page 130", b"png")
    return parse_vision_json(payload, _company(), _filing(), (_page(),))


def compile_result(result):
    return FinancialFactCompiler().compile_facts(
        _company(), (_filing(),), result.facts, required_concepts=frozenset(fact.concept for fact in result.facts),
        evidence=result.evidence,
    )


def test_separate_document_and_page_hashes_survive_production_admission():
    result = candidate()
    fact = result.facts[0]
    row = next(ref for ref in result.evidence if ref.evidence_id == f"fact:{fact.fact_id}")
    proof = next(ref for ref in result.evidence if ref.evidence_id in fact.classification_evidence_ids)
    captured = json.loads(proof.excerpt)
    assert row.content_hash == _filing().content_hash
    assert captured["document_hash"] == row.content_hash
    assert captured["page_hash"] == _page().content_hash != row.content_hash
    admitted = compile_result(result).accepted_facts
    assert [(item.fact_id, item.value) for item in admitted] == [(fact.fact_id, fact.value)]


@pytest.mark.parametrize("mutation", ["page", "receipt", "amount", "missing", "legacy"])
def test_forged_page_observation_is_quarantined_by_the_same_compiler(mutation):
    result = candidate()
    fact = result.facts[0]
    if mutation == "page":
        result = replace(result, facts=(replace(fact, source_page=999),))
    elif mutation == "receipt":
        refs = []
        for ref in result.evidence:
            if ref.evidence_id in fact.classification_evidence_ids:
                payload = json.loads(ref.excerpt)
                payload["page_hash"] = "0" * 64
                ref = replace(ref, excerpt=json.dumps(payload))
            refs.append(ref)
        result = replace(result, evidence=tuple(refs))
    elif mutation == "amount":
        result = replace(result, facts=(replace(fact, value=999.0,
            normalization_lineage=replace(fact.normalization_lineage, raw_value="999", normalized_value="999")),))
    elif mutation == "missing":
        result = replace(result, evidence=tuple(ref for ref in result.evidence
                                                if ref.evidence_id not in fact.classification_evidence_ids))
    elif mutation == "legacy":
        result = replace(result, facts=(replace(fact, parser_version="vision-json-v3"),))
    compiled = compile_result(result)
    assert not compiled.accepted_facts
    assert compiled.quarantined_facts
    assert any("gateway" in issue for group in compiled.group_validations for issue in group.validation.issues)


def test_accounting_parentheses_are_verified_as_negative_not_positive():
    payload = RecordingVisionProvider().generate_vision("", "original_page 130", b"png")
    observed = payload["facts"][0]
    observed["concept"] = "net_income"
    observed["value"] = "(130)"
    observed["raw_text"] = observed["raw_text"].replace("Revenue 130", "Net profit (130)")
    result = parse_vision_json(payload, _company(), _filing(), (_page(),))
    assert result.facts[0].value == -130
    admitted = compile_result(result).accepted_facts
    assert [(item.fact_id, item.value) for item in admitted] == [(result.facts[0].fact_id, -130)]
