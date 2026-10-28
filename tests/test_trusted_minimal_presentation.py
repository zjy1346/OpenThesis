"""Commit-time minimum content survives complex presentation failures."""
from copy import deepcopy
from unittest.mock import patch

from openthesis.domain import ResearchArtifact
from openthesis.report_document import assemble_report_document, render_html, render_markdown
from openthesis.research_section_validation import project_supported_sections
from openthesis.safe_report import SafeReportAssembler
from openthesis.storage import Storage
from test_trusted_emergency_content import seed, commit_report


def commit_engine_table(store):
    # This mapping stands for a deterministic computation supplied separately
    # to the validator, not a provider-controlled passed/role declaration.
    table = {'gross_margin': 0.3, 'operating_margin_range': [0.14, 0.2]}
    report = {'implied_expectations': {'metrics': deepcopy(table)}, 'claims': []}
    safe, proof = project_supported_sections(report, ('implied_expectations',),
        lambda _: {'claim_verifications': []}, lambda *_: False, set(),
        computed_fields={'implied_expectations.metrics': table})
    store.save_artifact(ResearchArtifact('report:engine-table', 'retained-run', 'research-report', 'Table',
        {'report': safe, 'verification': {'section_validation': proof}}))


def test_commit_freezes_a_minimum_table_with_all_cells(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    commit_engine_table(store)
    artifact = next(row for row in store.get_artifacts('retained-run')
                    if row['artifact_type'] == 'trusted-content-snapshot')
    minimum = artifact['content']['nodes'][0]['minimal_content']
    assert minimum['kind'] == 'table'
    assert minimum['cells'] == [
        {'field': 'gross_margin', 'value': 0.3, 'semantic_text': '30%'},
        {'field': 'operating_margin_range', 'value': [0.14, 0.2], 'semantic_text': '14%–20%'},
    ]


def test_normal_and_emergency_render_the_same_trusted_minimum_table(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    commit_engine_table(store)
    artifacts = Storage(tmp_path).get_artifacts('retained-run')
    document = assemble_report_document('retained-run', artifacts, language='en')
    section = next(row for row in document.sections if row.section_id == 'implied_expectations')
    assert any(node.kind == 'table' and node.validation_record.passed for node in section.nodes)
    emergency = SafeReportAssembler().assemble(run_id='retained-run', company_name='Fixture',
        status='partial', artifacts=artifacts, continuity={}, language='en')
    for markdown, html in ((render_markdown(document), render_html(document)),
                           (emergency.markdown, emergency.html)):
        assert '| ' in markdown and '<table' in html
        assert '30%' in markdown and '14%–20%' in markdown
        assert '30%' in html and '14%–20%' in html
        assert '0.3' not in markdown


def test_one_complex_node_failure_uses_its_minimum_body_and_preserves_sibling(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    commit_report(store)
    # A stage proof persists independently of the current report head.
    artifacts = store.get_artifacts('retained-run')
    report = next(row for row in artifacts if row['artifact_type'] == 'research-report')
    report['content']['report']['financial_quality'] = deepcopy(report['content']['report']['business_model'])
    from openthesis.research import _supported_synthesis_sections
    ref = {'filing:business': {'kind': 'filing_text', 'company_cik': '0000320193',
        'end_date': '2025-12-31', 'fiscal_year': 2025, 'fiscal_period': 'FY',
        'excerpt': 'Operations serve industrial customers.'}}
    safe, proof = _supported_synthesis_sections(report['content']['report'], set(ref), ref, 'en')
    store.save_artifact(ResearchArtifact('report:two-bodies', 'retained-run', 'research-report', 'Bodies',
        {'report': safe, 'verification': {'section_validation': proof}}))
    with patch('openthesis.report_document._semantic_nodes', side_effect=RuntimeError('injected-node-renderer')):
        document = assemble_report_document('retained-run', Storage(tmp_path).get_artifacts('retained-run'), language='en')
    for key in ('business_model', 'financial_quality'):
        section = next(row for row in document.sections if row.section_id == key)
        assert 'Operations serve industrial customers.' in section.body
        assert section.verification_state == 'verified'
    assert any(item.code == 'trusted_node_presentation_fallback' for item in document.delivery_diagnostics)


def test_emergency_minimum_table_does_not_reenter_failed_semantic_formatter(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    commit_engine_table(store)
    artifacts = Storage(tmp_path).get_artifacts('retained-run')
    with patch('openthesis.report_projection.format_report_semantic_value',
               side_effect=RuntimeError('semantic-presentation-unavailable')):
        emergency = SafeReportAssembler().assemble(run_id='retained-run', company_name='Fixture',
            status='partial', artifacts=artifacts, continuity={}, language='en')
    assert '30%' in emergency.markdown and '14%–20%' in emergency.html
    assert '0.3' not in emergency.markdown
