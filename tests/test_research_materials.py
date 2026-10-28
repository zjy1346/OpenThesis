import json
import unittest
from unittest.mock import Mock
from pathlib import Path
from tempfile import TemporaryDirectory

from openthesis.domain import FilingDocument
from openthesis.filing_parser import extract_topic_evidence
from openthesis.research import _agent_prompt_bundle
from openthesis.research import ResearchWorkflow, verify_agent_output
from openthesis.research_execution.reference_aliases import decode_references, reference_aliases
from openthesis.research_execution.source_compiler import expand_record_tables
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.storage import Storage


def _reading_material_adequacy(payload):
    reference = payload["research_context"]["material_adequacy"]["request_path_ref"]
    value = payload
    for component in reference:
        value = value[component]
    return value


def _expand_and_decode_request(payload, original_context_json):
    expanded = expand_record_tables(payload)

    def resolve(node, path_stack=()):
        if isinstance(node, dict):
            if set(node) == {"request_path_ref"}:
                path = node["request_path_ref"]
                assert isinstance(path, list)
                identity = tuple(path)
                assert identity not in path_stack, f"request_path_ref cycle at {path!r}"
                target = expanded
                for component in path:
                    target = target[component]
                return resolve(target, (*path_stack, identity))
            return {key: resolve(child, path_stack) for key, child in node.items()}
        if isinstance(node, list):
            return [resolve(child, path_stack) for child in node]
        return node

    resolved = resolve(expanded)
    return decode_references(resolved, reference_aliases(original_context_json))


def _expand_and_decode_request(payload, original_context_json):
    expanded = expand_record_tables(payload)

    def resolve(node, path_stack=()):
        if isinstance(node, dict):
            if set(node) == {"request_path_ref"}:
                path = node["request_path_ref"]
                assert isinstance(path, list)
                identity = tuple(path)
                assert identity not in path_stack, f"request_path_ref cycle at {path!r}"
                target = expanded
                for component in path:
                    target = target[component]
                return resolve(target, (*path_stack, identity))
            return {key: resolve(child, path_stack) for key, child in node.items()}
        if isinstance(node, list):
            return [resolve(child, path_stack) for child in node]
        return node

    resolved = resolve(expanded)
    return decode_references(resolved, reference_aliases(original_context_json))


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
    original_context_json = json.dumps({'evidence': facts + [body]})
    _, prompt = _agent_prompt_bundle('business-analyst', 'Analyze business', 'en',
                                     original_context_json, {})
    payload = _expand_and_decode_request(json.loads(prompt), original_context_json)
    context = payload['research_context']
    assert context['evidence_partitions']['business_disclosures'][0] == body
    quantitative = expand_record_tables(context['evidence_partitions']['quantitative_facts'])
    assert len(quantitative) == 35
    assert payload['reading_assignment']['material_adequacy']['status'] == 'ready'


def test_numeric_cell_text_does_not_satisfy_qualitative_gate():
    evidence = [{'evidence_id': 'fact:pdf:1', 'title': 'Annual business report',
                 'excerpt': 'Revenue 2025 100 2024 90', 'locator': 'page:100'}]
    _, prompt = _agent_prompt_bundle('business-analyst', 'Analyze business', 'en',
                                     json.dumps({'evidence': evidence}), {})
    gate = _reading_material_adequacy(json.loads(prompt))
    assert gate['status'] == 'insufficient'
    assert gate['missing'] == ['business_disclosures']


def test_missing_body_blocks_provider_memory_and_persists_explicit_unknown():
    with TemporaryDirectory() as directory:
        # A real provider lacking optional budget hooks, not Mock's fabricated
        # callable output_allowance/for_output_budget returning more Mocks.
        provider = Mock(spec=['generate'])
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
