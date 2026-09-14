import json
import unittest
from unittest.mock import Mock
from pathlib import Path
from tempfile import TemporaryDirectory

from openthesis.domain import FilingDocument
from openthesis.filing_parser import extract_topic_evidence
from openthesis.research import _agent_prompt_bundle
from openthesis.research import ResearchWorkflow, verify_agent_output
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.storage import Storage


def test_chinese_official_body_reaches_topic_evidence():
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'annual.html'
        path.write_text('<h1>主营业务</h1>' + '公司主要生产储能设备，客户集中度上升，需要跟踪产品交付。' * 12, encoding='utf-8')
        filing = FilingDocument('doc', 'issuer', 'acc', 'ANNUAL_REPORT', 'FY', '2025-12-31',
                                '2026-03-01', 'annual.html', 'https://example.test/annual', str(path))
        refs = extract_topic_evidence(filing)
        assert any('business' in ref.title and '储能设备' in ref.excerpt for ref in refs)


def test_actual_prompt_partitions_body_after_35_financial_facts():
    facts = [{'evidence_id': f'fact:{i}', 'kind': 'financial_fact', 'value': i} for i in range(35)]
    body = {'evidence_id': 'filing:business', 'title': 'ANNUAL_REPORT · business',
            'excerpt': 'We manufacture storage equipment and depend on concentrated customers.',
            'source_url': 'https://example.test/annual', 'document_id': 'doc'}
    _, prompt = _agent_prompt_bundle('business-analyst', 'Analyze business', 'en',
                                     json.dumps({'evidence': facts + [body]}), {})
    context = json.loads(prompt)['research_context']
    assert context['evidence_partitions']['business_disclosures'][0] == body
    assert len(context['evidence_partitions']['quantitative_facts']) == 35
    assert context['material_adequacy']['status'] == 'ready'


def test_numeric_cell_text_does_not_satisfy_qualitative_gate():
    evidence = [{'evidence_id': 'fact:pdf:1', 'title': 'Annual business report',
                 'excerpt': 'Revenue 2025 100 2024 90', 'locator': 'page:100'}]
    _, prompt = _agent_prompt_bundle('business-analyst', 'Analyze business', 'en',
                                     json.dumps({'evidence': evidence}), {})
    gate = json.loads(prompt)['research_context']['material_adequacy']
    assert gate['status'] == 'insufficient'
    assert gate['missing'] == ['business_disclosures']


def test_missing_body_blocks_provider_memory_and_persists_explicit_unknown():
    with TemporaryDirectory() as directory:
        provider = Mock()
        workflow = ResearchWorkflow(Storage(Path(directory)), builtin_pack(), provider, ModelConfig(), report_language='en')
        result = workflow._run_agent('business-analyst', 'prompts/business-analyst.md',
                                     json.dumps({'evidence': [{'evidence_id': 'fact:1', 'kind': 'financial_fact'}]}), {})
        provider.generate.assert_not_called()
        assert result['kind'] == 'unknown'
        assert result['_material_adequacy']['missing'] == ['business_disclosures']
        assert not verify_agent_output(result, {'fact:1'}, 'en')['passed']


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items()
                              if name.startswith('test_') and callable(value))
