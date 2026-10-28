import json
from copy import deepcopy

import pytest

from openthesis.research_execution.repair_context import repair_context
from openthesis.research_materials import canonical_request_materials
from openthesis.research import ResearchWorkflow
from openthesis.report_document import _stage_section_fallbacks
from test_model_request_identity import setup
from openthesis.research_execution.model import ModelStageRequest


def test_exact_wire_candidate_reuse_after_audit_only_generation_change(tmp_path):
    store, registry, journal, scope = setup(tmp_path)
    request = ModelStageRequest('analysis', 'system', 'same full sources')
    body = {'analysis': 'Retained original judgment.', 'structured_output_valid': True}
    assert journal.execute(request, lambda: deepcopy(body), scope) == body
    generation = journal.prepare(registry, 'new-host-audit', invalidated=('analysis',))['analysis']
    assert journal.execute(request, lambda: pytest.fail('duplicate physical generation'), ('analysis', generation)) == body
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM model_request_journal').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM model_result_reuse').fetchone()[0] == 1
    changed = ModelStageRequest('analysis', 'system', 'changed source bytes')
    assert journal.execute(changed, lambda: {'analysis': 'new sources'}, ('analysis', generation))['analysis'] == 'new sources'


def test_repair_reference_closure_and_numeric_risk_context_are_self_contained():
    source = 'Complete source and its important opposing qualification. ' * 20
    materials = {'accepted_facts': [{'fact_id': 'f', 'value': 100, 'unit': 'CNY', 'fiscal_year': 2025}],
        'evidence_partitions': {'business_disclosures': [{'evidence_id': 'e', 'raw_text': source}],
                                'risk_disclosures': [{'evidence_id': 'risk', 'raw_text': source}]}}
    findings = {'risk': {'document_id': 'd', 'text': 'Dynamic opposing finding. ' * 20}}
    context, prior, registry = canonical_request_materials(materials, findings)
    result = repair_context(json.dumps({'research_context': context, 'prior_artifacts': prior,
        'material_registry': registry, 'request_reference_contract': 'same-request reference lookup'}), ('growth_opportunities',))
    assert result['research_context']['accepted_facts'][0]['value'] == 100
    assert result['research_context']['evidence_partitions']['risk_disclosures']
    key = result['prior_artifacts']['risk']['text']['finding_ref']
    assert result['prior_artifacts']['findings_registry'][key]['text'] == findings['risk']['text']
    assert result['request_reference_contract']
    del registry[next(iter(registry))]
    with pytest.raises(ValueError, match='SOURCE_REFERENCE_UNRESOLVED'):
        repair_context(json.dumps({'research_context': context, 'material_registry': registry}), ('financial_quality',))


def test_length_supplement_calls_each_missing_section_once_and_keeps_closed_body():
    from openthesis.packs import builtin_pack
    from openthesis.providers import ModelConfig
    workflow = ResearchWorkflow(None, builtin_pack(), None, ModelConfig())
    calls = []
    original = {'business_model': {'analysis': 'Closed original and opposing view.'},
                'claims': [], '_output_incomplete': {'state': 'pending'}}

    def supplement(*args, **kwargs):
        calls.append((args, kwargs))
        missing = args[3]['sectional_delivery']['sections']
        return {section: {'analysis': 'New complete section.'} for section in missing}
    workflow._run_agent_adaptive = supplement
    result = workflow._recover_synthesis_output_sections('{}', {}, original)
    all_sections = calls[0][0][3]['sectional_delivery']['all_sections']
    missing_sections = [section for section in all_sections if section not in original]
    requested_sections = [call[0][3]['sectional_delivery']['sections'] for call in calls]
    assert len(calls) == len(missing_sections)
    assert requested_sections == [[section] for section in missing_sections]
    assert all('business_model' not in sections for sections in requested_sections)
    assert all(result[section]['analysis'] == 'New complete section.' for section in missing_sections)
    assert result['business_model'] == original['business_model']


def test_legacy_technical_product_revisions_do_not_sum_and_reordered_ids_are_not_identities():
    def opportunity(identity, title, text, source='same-original'):
        return {'opportunity_id': identity, 'title': title, 'mechanism': text, 'supporting_evidence_ids': [source]}
    old = [opportunity('growth-1', '28nm超低漏电平台扩大应用', 'Old wording.'),
           opportunity('growth-2', '65nm RF-SOI平台扩大客户', 'Retain this old valid item.'),
           opportunity('stable-other', '28nm超低漏电平台扩大应用', 'A genuinely different explicitly identified opportunity.')]
    new = [opportunity('growth-2', '28nm超低漏电平台扩展应用', 'Updated wording.')]
    artifacts = [{'artifact_type': 'growth-opportunities', 'artifact_id': name, 'content': {'opportunities': values}}
                 for name, values in [('old', old), ('new', new)]]
    result = _stage_section_fallbacks(artifacts)['growth_opportunities']
    assert len(result['opportunities']) == 3
    text = str(result['opportunities'])
    assert 'Updated wording.' in text and 'Retain this old valid item.' in text
    assert 'Old wording.' not in text
    assert 'genuinely different' in text


def test_committed_growth_snapshot_leaves_follow_item_selection_not_old_context_positions():
    from openthesis.report_document import _select_growth_content_nodes, _scalar_text
    current = {'opportunities': [{'title': '28nm平台扩展应用', 'mechanism': 'Current analysis.'}],
        '_stage_lineage': {'superseded_items': [{'title': '28nm平台扩大应用'}]}}
    def node(title, body, generation):
        return {'source_path': 'stage.opportunities[0].mechanism', 'body': body, 'body_sha256': body,
            'context': [{'field': 'title', 'text': title, 'path': 'stage.opportunities[0]'}],
            'validation_state': 'partial', 'source_artifact_id': generation}
    nodes = [node('28nm平台扩大应用', 'Old analysis.', 'old'),
             node('28nm平台扩展应用', 'Current analysis.', 'new'),
             node('28nm平台扩展应用', 'Current analysis.', 'report')]
    before = deepcopy(nodes)
    result = _select_growth_content_nodes(nodes, current)
    assert len(result) == 1 and result[0]['body'] == 'Current analysis.'
    assert nodes == before
    assert _scalar_text(0.3, 'probability_range', 'zh-CN') == '30%'
