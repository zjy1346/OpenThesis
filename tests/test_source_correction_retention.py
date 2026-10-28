"""Confirmed corrections revoke old proofs, not unrelated saved work."""
from copy import deepcopy
from dataclasses import replace

import pytest

from openthesis.domain import ResearchArtifact, ResearchRun
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_evidence import build_fact_evidence
from openthesis.report_content import (assemble_trusted_report_payload, read_content_invalidations,
                                      read_trusted_snapshots, project_financial_source_corrections)
from openthesis.research import _supported_synthesis_sections
from openthesis.safe_report import SafeReportAssembler
from openthesis.service import AppService
from openthesis.storage import Storage
from test_scoped_note_revenue import notes


BAD = 'Revenue was CNY 7,000,000,000.'
GOOD = 'Operations serve industrial customers.'


def setup(store):
    company, filing, facts, refs = notes()
    dataset = FinancialFactCompiler().compile_facts(company, [filing], facts, evidence=refs)
    new = next(fact for fact in dataset.research_facts if fact.concept == 'revenue')
    old = replace(new, fact_id='ingest:old-incorrect', value=7000000000., raw_text=BAD,
                  normalization_lineage=None)
    store.save_company(company)
    store.save_filings([filing])
    store.save_facts([old])
    run = ResearchRun('correction-run', company, 'wf', 'pack', '1', '', '', '2025-12-31')
    store.save_run(run)
    evidence = build_fact_evidence(store.get_facts(company.cik))
    evidence.append({'evidence_id': 'filing:business', 'kind': 'filing_text', 'excerpt': GOOD,
        'company_cik': company.cik, 'end_date': '2025-12-31', 'fiscal_year': 2025,
        'fiscal_period': 'FY', 'source_url': filing.source_url, 'content_hash': filing.content_hash})
    records = {item['evidence_id']: item for item in evidence}
    raw = {'financial_quality': {'analysis': BAD, 'evidence_ids': ['fact:' + old.fact_id]},
           'business_model': {'analysis': GOOD, 'evidence_ids': ['filing:business']}, 'claims': []}
    safe, proof = _supported_synthesis_sections(raw, set(records), records, 'en')
    assert safe['financial_quality']['analysis'] == BAD
    store.save_artifact(ResearchArtifact('source-summary', run.run_id, 'deterministic-financial-summary',
        'Sources', {'evidence': evidence, 'metrics': []}))
    store.save_artifact(ResearchArtifact('report:original', run.run_id, 'research-report', 'Original',
        {'report': safe, 'publication_input_digest': 'same-source-input',
         'verification': {'section_validation': proof}}))
    return run, filing, new, dataset


def publish(store, filing, new, dataset, *, generation='new-source', records=None, **kwargs):
    return store.replace_financial_ingestion(filing.company_cik, [filing.accession_number], [new],
        generation_id=generation, parser_version='source-correction-test',
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        fact_validation_records=(dataset.coverage['fact_validation_records'] if records is None else records),
        **kwargs)


def test_proven_source_correction_withdraws_only_dependencies_and_survives_formal_reads(tmp_path):
    store = Storage(tmp_path)
    run, filing, new, dataset = setup(store)
    before = read_trusted_snapshots(store.get_artifacts(run.run_id), run_id=run.run_id)
    good = next(node for node in before if node['body'] == GOOD)
    # Persist a progressive copy as well, so an older report cannot revive it.
    progress = assemble_trusted_report_payload(store.get_artifacts(run.run_id), run_id=run.run_id,
                                              input_digest='same-source-input')
    store.save_artifact(ResearchArtifact('report:progress', run.run_id, 'research-report', 'Progress', progress))
    assert publish(store, filing, new, dataset)['state'] == 'promoted'
    reopened = Storage(tmp_path)
    artifacts = reopened.get_artifacts(run.run_id)
    retained = read_trusted_snapshots(artifacts, run_id=run.run_id)
    assert {node['body'] for node in retained} == {GOOD}
    assert retained[0]['node_id'] == good['node_id']
    assert read_content_invalidations(artifacts, run_id=run.run_id)
    assert BAD in str(next(row for row in artifacts if row['artifact_id'] == 'report:original'))
    formal = AppService(tmp_path, provider_factory=lambda _: None).get_report(run.run_id, language='en')
    emergency = SafeReportAssembler().assemble(run_id=run.run_id, company_name='Fixture', status='partial',
        artifacts=artifacts, continuity={}, language='en')
    for text in (formal['markdown'], formal['html'], emergency.markdown, emergency.html):
        assert GOOD in text and BAD not in text
        assert 'corrected financial facts' in text
    assert formal['research_complete'] is False
    # A new independently validated body remains admissible after correction.
    changed = build_fact_evidence(reopened.get_facts(run.company.cik))
    refs = {row['evidence_id']: row for row in changed}
    body = 'Revenue was CNY 7,051,834,638.32.'
    from openthesis.research_assertions import AssertionSession, assertion_session, validate_response
    def validate_corrected():
        return _supported_synthesis_sections(
            {'financial_quality': {'analysis': body, 'evidence_ids': list(refs)}, 'claims': []},
            set(refs), refs, 'en')
    pending = AssertionSession()
    with assertion_session(pending):
        validate_corrected()
    queries = list(pending.queries.values())
    answers = {'structured_output_valid': True, 'results': [
        {'assertion_id': query['assertion_id'], 'state': 'supported', 'reason': 'Offline annotated corrected revenue',
         'source_spans': [{'evidence_id': source['evidence_id'], 'start': 0, 'end': len(source['raw_text']),
             'quote': source['raw_text'], 'source_sha256': source['source_sha256']} for source in query['sources']]}
        for query in queries if query['text'] == body]}
    with assertion_session(AssertionSession(validate_response(answers, queries, 'offline-correction-fixture'))):
        safe, proof = validate_corrected()
    assert safe['financial_quality']['analysis'] == body
    reopened.save_artifact(ResearchArtifact('report:corrected', run.run_id, 'research-report', 'Corrected',
        {'report': safe, 'publication_input_digest': 'same-source-input',
         'verification': {'section_validation': proof}}))
    assert body in AppService(tmp_path, provider_factory=lambda _: None).get_report(run.run_id, language='en')['markdown']
    from openthesis.report_content import unresolved_content_invalidations
    assert not unresolved_content_invalidations(reopened.get_artifacts(run.run_id), run_id=run.run_id)
    assert read_content_invalidations(reopened.get_artifacts(run.run_id), run_id=run.run_id)


@pytest.mark.parametrize('mode', ['missing-proof', 'incomplete-currency', 'lost-cas', 'unchanged'])
def test_ordinary_reparse_failure_or_missing_proof_never_revokes_prior_content(tmp_path, mode):
    store = Storage(tmp_path)
    run, filing, new, dataset = setup(store)
    records = deepcopy(dataset.coverage['fact_validation_records'])
    kwargs = {}
    if mode == 'missing-proof':
        records = {}
    elif mode == 'incomplete-currency':
        records[new.fact_id]['components']['currency']['state'] = 'unknown'
    elif mode == 'lost-cas':
        kwargs['expected_generations'] = {filing.accession_number: 'wrong-active-generation'}
    elif mode == 'unchanged':
        # Equal normalized amount is not a correction even if the parser changes.
        store.save_facts([replace(new, fact_id='ingest:old-incorrect', value=7000000000.)])
        new = replace(new, value=7000000000.)
    publish(store, filing, new, dataset, records=records, **kwargs)
    artifacts = Storage(tmp_path).get_artifacts(run.run_id)
    assert not read_content_invalidations(artifacts, run_id=run.run_id)
    assert {node['body'] for node in read_trusted_snapshots(artifacts, run_id=run.run_id)} == {GOOD, BAD}


def test_dependency_metadata_is_bound_to_validator_node_identity():
    store_records = {'filing:business': {'kind': 'filing_text', 'excerpt': GOOD,
        'company_cik': 'fixture', 'end_date': '2025-12-31', 'fiscal_year': 2025, 'fiscal_period': 'FY'}}
    safe, proof = _supported_synthesis_sections(
        {'business_model': {'analysis': GOOD, 'evidence_ids': list(store_records)}, 'claims': []},
        set(store_records), store_records, 'en')
    from openthesis.research_section_validation import proven_content_nodes
    assert proven_content_nodes(safe, proof)[0]['depends_on'] == ['filing:business']
    proof['nodes'][0]['depends_on'] = []
    assert not proven_content_nodes(safe, proof)


def test_deterministic_table_withdraws_corrected_input_and_only_its_derived_fields(tmp_path):
    from openthesis.financials import calculate_metrics
    store = Storage(tmp_path)
    run, filing, new, dataset = setup(store)
    facts = store.get_facts(run.company.cik)
    # A second independent fact demonstrates per-cell retention in the table.
    facts.append({**facts[0], 'fact_id': 'independent-profit', 'concept': 'net_income',
                  'value': 630008343.39, 'raw_text': 'Net income was CNY 630,008,343.39.'})
    metrics = calculate_metrics(facts)
    assert metrics[0]['net_margin'] is not None
    summary = ResearchArtifact('summary:with-metrics', run.run_id, 'deterministic-financial-summary',
        'Financial', {'evidence': build_fact_evidence(facts), 'metrics': metrics, 'currency': 'CNY'})
    store.save_artifact(summary)
    publish(store, filing, new, dataset)
    artifacts = Storage(tmp_path).get_artifacts(run.run_id)
    projected = project_financial_source_corrections(artifacts, run_id=run.run_id)
    view = next(row for row in projected if row['artifact_id'] == summary.artifact_id)['content']
    assert view['metrics'][0]['revenue'] is None
    assert view['metrics'][0]['net_margin'] is None
    assert view['metrics'][0]['net_income'] == 630008343.39
    assert summary.content['metrics'][0]['revenue'] == 7000000000.
    from openthesis.research_section_validation import content_digest
    assert content_digest(next(row for row in artifacts if row['artifact_id'] == summary.artifact_id)['content']) == content_digest(summary.content)
    formal = AppService(tmp_path, provider_factory=lambda _: None).get_report(run.run_id, language='en')
    assert '7.00B' not in formal['markdown'] and '7.00B' not in formal['html']
    assert '630.01M' in formal['markdown'] and '630.01M' in formal['html']
    # Recalculation is token-free and uses the current canonical inputs.
    current = Storage(tmp_path).get_facts(run.company.cik)
    corrected = ResearchArtifact('summary:recomputed', run.run_id, 'deterministic-financial-summary',
        'Recomputed', {'metrics': calculate_metrics(current), 'evidence': build_fact_evidence(current)})
    store.save_artifact(corrected)
    latest = project_financial_source_corrections(Storage(tmp_path).get_artifacts(run.run_id), run_id=run.run_id)[-1]
    assert latest['artifact_id'] == corrected.artifact_id
    assert latest['content']['metrics'][0]['revenue'] == new.value
    assert '_source_correction_fields' not in latest['content']
