from copy import deepcopy

from openthesis.demo import demo_facts
from openthesis.research import _collect_stage_claims, _supported_synthesis_sections, build_fact_evidence
from test_research_workflow import _verified_demo_revenue_claim


def supported(report):
    records = {item['evidence_id']: item for item in build_fact_evidence(demo_facts())}
    return _supported_synthesis_sections(report, set(records), records, 'en')


def test_verified_duplicates_merge_without_losing_section_bindings_or_raising_confidence():
    first = {**_verified_demo_revenue_claim(), 'claim_id': 'business-revenue', 'section_id': 'business_model',
             'text': 'Revenue was USD 4,200,000,000.', 'confidence': 0.9}
    second = {**first, 'claim_id': 'summary-revenue', 'section_id': 'executive_summary',
              'text': 'FY2021 annual revenue of USD 4,200,000,000.0', 'confidence': 0.7}
    report = {'claims': [first, second], 'business_model': first['text'], 'executive_summary': second['text']}
    original = deepcopy(report)
    safe, audit = supported(report)
    assert len(safe['claims']) == 1
    assert safe['claims'][0]['section_ids'] == ['business_model', 'executive_summary']
    assert safe['claims'][0]['confidence'] == 0.7
    assert safe['business_model'] == first['text']
    assert safe['executive_summary'] == second['text']
    assert audit['claim_deduplication']['removed_count'] == 1
    assert audit['verified_claim_count'] == 1
    assert report == original


def test_duplicate_id_aliases_keep_section_and_calculation_references_resolvable():
    first = {**_verified_demo_revenue_claim(), 'claim_id': 'a', 'text': 'Revenue was USD 4,200,000,000.'}
    duplicate = {**first, 'claim_id': 'b'}
    analysis = {'analysis': first['text'], 'claims': [duplicate], 'claim_ids': ['b']}
    safe, audit = supported({'claims': [first, duplicate], 'financial_quality': analysis})
    assert len(safe['claims']) == 1
    assert safe['financial_quality']['claim_ids'] == ['a']
    assert audit['claim_deduplication']['claim_id_aliases'] == {'b': 'a'}


def test_raw_aggregation_never_numeric_merges_unverified_distinct_amount_prose():
    first = {**_verified_demo_revenue_claim(), 'claim_id': 'a', 'text': 'Revenue was USD 4,200,000,000.'}
    other = {**first, 'claim_id': 'b', 'text': 'Annual revenue was USD 999 billion.'}
    assert len(_collect_stage_claims({'claims': [first]}, {'claims': [other]})) == 2
    safe, audit = supported({'claims': [first, other], 'executive_summary': first['text']})
    assert len(safe['claims']) == 1
    assert '999' not in str(safe)
    assert any(item['path'] == 'claims[1]' for item in audit['quarantined_paths'])


def test_forward_section_references_bind_to_independently_verified_local_claims():
    claim = {**_verified_demo_revenue_claim(), 'claim_id': 'later', 'text': 'Revenue was USD 4,200,000,000.'}
    safe, audit = supported({'business_model': {'analysis': claim['text'], 'claim_ids': ['later']},
                             'financial_quality': {'analysis': claim['text'], 'claims': [claim]}})
    assert safe['business_model']['claim_ids'] == ['later']
    assert safe['business_model']['analysis'] == claim['text']
    assert not any(item['reason'] == 'unknown_or_invalid_claim_reference'
                   for item in audit['quarantined_paths'])


def test_ambiguous_claim_ids_cannot_bind_unrelated_prose():
    first = {**_verified_demo_revenue_claim(), 'claim_id': 'conflict', 'text': 'Revenue was USD 4,200,000,000.'}
    second = {**first, 'text': 'Revenue was USD 999 billion.'}
    safe, audit = supported({'claims': [first, second],
                             'business_model': {'analysis': second['text'], 'claim_ids': ['conflict']}})
    assert '999' not in str(safe)
    assert 'business_model' not in safe
