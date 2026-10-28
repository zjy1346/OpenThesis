from copy import deepcopy

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from openthesis.research import _supported_synthesis_sections
from openthesis.safe_report import SafeReportAssembler
from openthesis.storage import Storage
from openthesis.report_content import assemble_trusted_report_payload
from openthesis.report_document import assemble_report_document, render_html, render_markdown
from openthesis.storage_research_publication import ResearchPublicationConflict
import pytest


def seed(store):
    store.save_company(DEMO_COMPANY)
    run = ResearchRun('retained-run', deepcopy(DEMO_COMPANY), 'wf', 'pack', '1',
                      'offline', 'fixture', '2025-12-31', RunStatus.PARTIAL)
    store.save_run(run)
    return run


def commit_report(store, body='Operations serve industrial customers.'):
    refs = {'filing:business': {'kind': 'filing_text', 'evidence_id': 'filing:business',
            'company_cik': DEMO_COMPANY.cik, 'end_date': '2025-12-31', 'fiscal_year': 2025,
            'fiscal_period': 'FY', 'excerpt': body}}
    report = {'business_model': {'analysis': body, 'evidence_ids': ['filing:business']}, 'claims': []}
    safe, proof = _supported_synthesis_sections(report, set(refs), refs, 'en')
    assert safe['business_model']['analysis'] == body
    artifact = ResearchArtifact('report:verified', 'retained-run', 'research-report', 'Research',
                                {'report': safe, 'verification': {'passed': False, 'section_validation': proof}})
    store.save_artifact(artifact)
    return artifact


def render(store):
    return SafeReportAssembler().assemble(run_id='retained-run', company_name=DEMO_COMPANY.name,
        status='partial', artifacts=store.get_artifacts('retained-run'), continuity={}, language='en',
        renderer_error='injected-complex-renderer-failure')


def test_committed_proof_retains_body_and_refs_after_restart_without_complex_renderer(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    commit_report(store)
    safe = render(Storage(tmp_path))
    assert 'Operations serve industrial customers.' in safe.markdown
    assert 'Operations serve industrial customers.' in safe.html
    assert 'filing:business' in safe.markdown and 'filing:business' in safe.html
    assert len(safe.nodes) == 1
    assert safe.nodes[0]['source_artifact_id'] == 'report:verified'
    assert Storage(tmp_path).latest_report_revision('retained-run') is not None


def test_raw_model_validation_flag_never_grants_emergency_trust(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    store.save_artifact(ResearchArtifact('agent:raw', 'retained-run', 'agent-analysis', 'Business',
        {'analysis': 'Unverified model body must not be delivered.', '_validation': {'passed': True}}))
    safe = render(store)
    assert not safe.nodes
    assert 'Unverified model body must not be delivered.' not in safe.markdown


def test_corrupt_snapshot_is_isolated_without_dropping_durable_original(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    commit_report(store)
    artifacts = store.get_artifacts('retained-run')
    snapshot = next(row for row in artifacts if row['artifact_type'] == 'trusted-content-snapshot')
    snapshot['content']['nodes'][0]['body'] = 'Revenue was USD 999 billion.'
    safe = SafeReportAssembler().assemble(run_id='retained-run', company_name='Fixture', status='partial',
        artifacts=artifacts, continuity={}, language='en')
    assert not safe.nodes
    assert '999' not in safe.markdown
    assert 'Operations serve industrial customers.' in str(store.get_artifacts('retained-run'))


def test_emergency_html_escapes_proven_source_text(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    body = 'Operations serve industrial customers. <script>alert(1)</script>'
    commit_report(store, body)
    safe = render(store)
    assert '<script>' not in safe.html
    assert '&lt;script&gt;' in safe.html


def test_normal_document_consumes_same_node_proof_not_global_failed_flag(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    commit_report(store)
    document = assemble_report_document('retained-run', store.get_artifacts('retained-run'), language='en')
    section = next(section for section in document.sections if section.section_id == 'business_model')
    body_node = next(node for node in section.nodes if node.text == 'Operations serve industrial customers.')
    assert body_node.validation_record.passed is True
    assert body_node.validation_record.node_id
    assert body_node.source_ids == ('filing:business',)
    assert 'Operations serve industrial customers.' in render_markdown(document)
    assert 'Operations serve industrial customers.' in render_html(document)
    assert not document.readiness.complete


def test_progressive_report_is_cas_bound_and_does_not_complete_run(tmp_path):
    store = Storage(tmp_path)
    run = seed(store)
    commit_report(store)
    snapshot = store.capture_research_publication(run)
    content = assemble_trusted_report_payload(store.get_artifacts('retained-run'), run_id='retained-run')
    artifact = ResearchArtifact('report:progressive', 'retained-run', 'research-report', 'Progressive', content)
    new_head = store.publish_research_progress(snapshot, artifact, cancel_check=lambda: False)
    assert new_head.report_generation > snapshot.report_generation
    assert store.get_run('retained-run')['status'] == RunStatus.PARTIAL.value
    assert not artifact.content['readiness']['complete']
    assert 'Operations serve industrial customers.' in render(Storage(tmp_path)).markdown
    with pytest.raises(ResearchPublicationConflict, match='RESEARCH_PUBLICATION_REVISION_CONFLICT'):
        store.publish_research_progress(snapshot, artifact, cancel_check=lambda: False)


def test_persisted_snapshot_retains_source_links_and_context_labels(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    body = 'Operations serve industrial customers.'
    refs = {'filing:business': {'kind': 'filing_text', 'evidence_id': 'filing:business',
        'company_cik': DEMO_COMPANY.cik, 'end_date': '2025-12-31', 'fiscal_year': 2025,
        'fiscal_period': 'FY', 'excerpt': body, 'source_url': 'https://example.org/official.pdf',
        'locator': 'page:20', 'content_hash': 'a' * 64}}
    store.save_artifact(ResearchArtifact('financial', 'retained-run', 'deterministic-financial-summary',
        'Financial', {'evidence': list(refs.values()), 'metrics': []}))
    report = {'growth_opportunities': [{'title': 'Industrial services', 'mechanism': body,
              'evidence_ids': ['filing:business']}], 'claims': []}
    safe, proof = _supported_synthesis_sections(report, set(refs), refs, 'en')
    store.save_artifact(ResearchArtifact('growth:verified', 'retained-run', 'research-report', 'Research',
        {'report': safe, 'verification': {'passed': False, 'section_validation': proof}}))
    retained = render(Storage(tmp_path))
    assert 'Industrial services' in retained.markdown and 'Industrial services' in retained.html
    assert 'https://example.org/official.pdf' in retained.markdown and 'https://example.org/official.pdf' in retained.html
    assert retained.nodes[0]['sources'][0]['locator'] == 'page:20'
    document = assemble_report_document('retained-run', store.get_artifacts('retained-run'), language='en')
    section = next(item for item in document.sections if item.section_id == 'growth_opportunities')
    assert section.verification_state == 'verified'
    assert any(item.text == 'Industrial services' and item.materiality == 'non_material' for item in section.nodes)
    assert body in render_markdown(document)


def test_equal_bodies_in_different_opportunities_preserve_both_labels(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    body = 'Operations serve industrial customers.'
    refs = {'filing:business': {'kind': 'filing_text', 'evidence_id': 'filing:business',
        'company_cik': DEMO_COMPANY.cik, 'end_date': '2025-12-31', 'fiscal_year': 2025,
        'fiscal_period': 'FY', 'excerpt': body}}
    report = {'growth_opportunities': [
        {'title': title, 'mechanism': body, 'evidence_ids': ['filing:business']}
        for title in ('Service expansion', 'Regional expansion')]}
    safe, proof = _supported_synthesis_sections(report, set(refs), refs, 'en')
    store.save_artifact(ResearchArtifact('growth:distinct-labels', 'retained-run', 'research-report', 'Research',
        {'report': safe, 'verification': {'section_validation': proof}}))
    retained = render(Storage(tmp_path))
    assert len(retained.nodes) == 2
    assert 'Service expansion' in retained.markdown and 'Regional expansion' in retained.markdown


def test_progressive_commit_preserves_frozen_link_after_source_id_is_reused(tmp_path):
    store = Storage(tmp_path)
    run = seed(store)
    body = 'Operations serve industrial customers.'
    source = {'evidence_id': 'filing:business', 'kind': 'filing_text', 'excerpt': body,
              'source_url': 'https://example.org/original.pdf', 'locator': 'page:7', 'content_hash': 'a' * 64}
    store.save_artifact(ResearchArtifact('sources:original', run.run_id, 'deterministic-financial-summary',
        'Financial', {'evidence': [source], 'metrics': []}))
    commit_report(store, body)
    snapshot = store.capture_research_publication(run)
    content = assemble_trusted_report_payload(store.get_artifacts(run.run_id), run_id=run.run_id)
    store.save_artifact(ResearchArtifact('sources:later', run.run_id, 'deterministic-financial-summary',
        'Financial', {'evidence': [{**source, 'source_url': 'https://example.org/later.pdf'}], 'metrics': []}))
    # A source summary append is not a new committed body; the local payload
    # is bound to the original proof rather than the current lookup by ID.
    snapshot = store.capture_research_publication(run)
    store.publish_research_progress(snapshot, ResearchArtifact('progress:frozen', run.run_id,
        'research-report', 'Progress', content), cancel_check=lambda: False)
    safe = render(Storage(tmp_path))
    assert 'https://example.org/original.pdf' in safe.markdown
    assert 'https://example.org/later.pdf' not in safe.markdown
    document = assemble_report_document(run.run_id, Storage(tmp_path).get_artifacts(run.run_id), language='en')
    assert 'https://example.org/original.pdf' in render_markdown(document)
    assert 'https://example.org/original.pdf' in render_html(document)
    assert 'https://example.org/later.pdf' not in render_markdown(document)
