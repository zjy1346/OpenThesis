from copy import deepcopy

from openthesis.research import verify_agent_output, _trusted_stage_result
from openthesis.research_materials import route_materials, material_capabilities, prompt_materials
from openthesis.research_source_context import observation_source_record
from openthesis.research_execution.model_output import normalize_stage_output


def source_context():
    observation = {'fact_id': 'ingest:damaged', 'company_cik': 'issuer', 'concept': 'revenue',
        'accession_number': 'annual', 'period_end': '2025-12-31', 'fiscal_period': 'FY',
        'scope': 'consolidated', 'source_binding': 'bound', 'source_hash': 'a' * 64,
        'source_url': 'https://example.test/annual.pdf', 'source_page': 8,
        'raw_text': '营业收入 120 110', 'candidate_value': 120,
        'source_bindings': [{'document_id': 'annual', 'document_sha256': 'a' * 64, 'page': 8}],
        'issue_codes': ['normalization_unit_declaration_mismatch']}
    return {'company': {'cik': 'issuer'}, 'facts': [], 'evidence': [], 'repair_observations': [observation]}


def test_damaged_original_row_can_enter_financial_interpretation_not_calculator():
    context = source_context()
    routed = route_materials(context, 'financial-analyst')
    assert routed['material_adequacy']['status'] == 'ready'
    assert routed['material_adequacy']['source_interpretation_only']
    assert routed['accepted_facts'] == []
    observation = routed['repair_observations'][0]
    assert observation['financial_admission'] is False
    assert observation['analysis_source_id'].startswith('observation:')
    assert 'sourced_financial_observations' in material_capabilities(context)
    assert 'quantitative_facts' not in material_capabilities(context)
    assert not route_materials(context, 'business-analyst')['repair_observations']


def test_wrong_company_unbound_and_invalid_hash_cannot_create_interpretation_capability():
    for updates in ({'company_cik': 'other'}, {'source_binding': 'unknown'},
                    {'source_hash': 'x' * 64}, {'source_page': None}, {'source_url': ''}):
        context = source_context()
        context['repair_observations'][0].update(updates)
        assert 'sourced_financial_observations' not in material_capabilities(context)


def test_observation_source_can_locate_analysis_but_never_hydrates_a_numeric_fact():
    record = observation_source_record(source_context()['repair_observations'][0])
    refs = {record['evidence_id']: record}
    result = {'summary': '币种仍需确认，因此尚不能据此判断营收变化。',
              'evidence_ids': list(refs), 'claims': []}
    verification = verify_agent_output(result, set(refs), 'zh-CN', refs)
    retained = _trusted_stage_result(result, verification, refs, 'zh-CN')
    assert retained['summary'] == result['summary']
    assert retained['_verification_state'] == 'completed_partial'
    check = verify_agent_output({'claims': [{'kind': 'fact', 'text': 'Revenue 120 CNY',
        'concept': 'revenue', 'value': 120, 'unit': 'CNY', 'evidence_ids': list(refs)}]},
        set(refs), 'en', refs)
    assert check['verified_claim_count'] == 0


def test_prompt_registry_preserves_complete_body_only_once():
    context = source_context()
    context['evidence'] = [{'evidence_id': 'filing:business', 'kind': 'filing_text',
        'document_id': 'annual', 'source_url': 'https://example.test/annual.pdf',
        'excerpt': 'UNIQUE_BODY_SENTINEL business details and limitations.', 'topic': 'business'}]
    original = deepcopy(context)
    payload = prompt_materials(context, 'business-analyst')
    import json
    assert json.dumps(payload).count('UNIQUE_BODY_SENTINEL') == 1
    assert context == original
    assert payload['evidence'] == ['filing:business']
    assert payload['source_blocks'][0]['evidence_ref'] == 'filing:business'


def test_punctuation_container_preserves_business_and_outer_host_controls():
    original = {': {': {'summary': 'Full analysis', 'claims': [], '_response_meta': {'refusal': None}},
                'structured_output_valid': True, '_response_meta': {'refusal': 'host refusal'}}
    result = normalize_stage_output(original, None)
    assert result['summary'] == 'Full analysis'
    assert result['_response_meta']['refusal'] == 'host refusal'
    assert ': {' in original
    for body in ({'unregistered_business': {'summary': 'extension'}},
                 {': {': {'summary': 'one'}, 'summary': 'two'}):
        assert normalize_stage_output(body, None) == body
