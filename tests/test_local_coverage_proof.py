from copy import deepcopy

from openthesis.demo import demo_facts
from openthesis.financials import calculate_metrics
from openthesis.research import build_fact_evidence, _supported_synthesis_sections, verify_agent_output
from openthesis.research_coverage import ResearchCoverageContract


def obligation(result, name):
    return next(row for row in result.obligations if row.obligation_id == name)


def records_for_business():
    return {'filing:business': {'kind': 'filing_text', 'evidence_id': 'filing:business',
            'company_cik': 'fixture-company', 'end_date': '2025-12-31', 'fiscal_year': 2025,
            'fiscal_period': 'FY', 'document_id': 'fixture-annual',
            'source_url': 'https://example.test/annual.pdf', 'locator': 'page:8',
            'excerpt': 'Operations serve industrial customers.'}}


def test_real_metric_producer_matches_coverage_without_handcrafted_year_alias():
    facts = demo_facts()
    refs = {row['evidence_id']: row for row in build_fact_evidence(facts)}
    result = ResearchCoverageContract().assess({}, {}, evidence_records=refs, metrics=calculate_metrics(facts))
    assert obligation(result, 'historical_financial_performance').state == 'fulfilled'


def test_discovered_latest_year_remains_missing_when_older_history_is_valid():
    facts = [row for row in demo_facts() if row['fiscal_year'] < 2025]
    refs = {row['evidence_id']: row for row in build_fact_evidence(facts)}
    plan = {'required_annual_periods': [
        {'year': year, 'period': 'FY', 'period_end': f'{year}-12-31'}
        for year in range(2021, 2026)]}
    row = obligation(ResearchCoverageContract().assess({}, {}, evidence_records=refs,
        metrics=calculate_metrics(facts), financial_period_coverage=plan), 'historical_financial_performance')
    assert row.state == 'missing_source_data'
    assert 'verified_annual_period_missing:2025:2025-12-31' in row.issues
    assert row.evidence_ids  # Safe older facts remain usable, not erased.


def test_non_calendar_plan_requires_the_exact_end_not_just_matching_year():
    facts = demo_facts()
    refs = {row['evidence_id']: row for row in build_fact_evidence(facts)}
    row = obligation(ResearchCoverageContract().assess({}, {}, evidence_records=refs,
        metrics=calculate_metrics(facts), financial_period_coverage={
            'required_annual_periods': [{'year': 2025, 'period': 'FY', 'period_end': '2025-03-31'}]}),
        'historical_financial_performance')
    assert row.state == 'missing_source_data'
    assert row.issues == ('verified_annual_period_missing:2025:2025-03-31',)


def test_independent_section_proof_survives_unrelated_bad_claim():
    records = records_for_business()
    report = {'business_model': {'analysis': 'Operations serve industrial customers.',
                                'evidence_ids': ['filing:business']}, 'claims': []}
    safe, audit = _supported_synthesis_sections(report, set(records), records, 'en')
    proof = verify_agent_output({'claims': []}, set(records), 'en', records)
    proof['section_validation'] = audit
    before = ResearchCoverageContract().assess(safe, proof, evidence_records=records)
    proof['unsupported_fact_count'] = 1
    after = ResearchCoverageContract().assess(safe, proof, evidence_records=records)
    # Verbatim source disclosure is preserved, but a quotation alone is not
    # the substantive business analysis required for complete research.
    assert obligation(before, 'company_business_model').state == 'missing_source_data'
    assert obligation(before, 'company_business_model').issues == ('source_quotation_without_analysis',)
    assert obligation(after, 'company_business_model') == obligation(before, 'company_business_model')


def test_claim_counter_and_model_passed_flag_do_not_manufacture_local_proof():
    records = records_for_business()
    report = {'business_model': {'analysis': 'Revenue was USD 999 billion.',
                                'evidence_ids': ['filing:business'], '_validation': {'passed': True}}}
    proof = {'verified_claim_count': 9, 'unsupported_fact_count': 0, 'passed': True}
    row = obligation(ResearchCoverageContract().assess(report, proof, evidence_records=records), 'company_business_model')
    assert row.state == 'verification_failed'
    assert 'local_content_proof_missing' in row.issues


def test_local_proof_is_bound_to_exact_body_and_does_not_bless_changed_prose():
    records = records_for_business()
    report = {'business_model': {'analysis': 'Operations serve industrial customers.',
                                'evidence_ids': ['filing:business']}, 'claims': []}
    safe, audit = _supported_synthesis_sections(report, set(records), records, 'en')
    safe['business_model']['analysis'] = 'Revenue was USD 999 billion.'
    row = obligation(ResearchCoverageContract().assess(safe, {'section_validation': audit}, evidence_records=records),
                     'company_business_model')
    assert row.state == 'verification_failed'


def test_unknown_reference_remains_rejected_despite_proven_sibling():
    records = records_for_business()
    report = {'business_model': {'analysis': 'Operations serve industrial customers.',
                                'evidence_ids': ['filing:business']}, 'claims': []}
    safe, audit = _supported_synthesis_sections(report, set(records), records, 'en')
    altered = deepcopy(safe)
    altered['business_model']['evidence_ids'].append('filing:unknown')
    row = obligation(ResearchCoverageContract().assess(altered, {'section_validation': audit}, evidence_records=records),
                     'company_business_model')
    assert row.state == 'verification_failed'


def test_one_proven_leaf_cannot_bless_an_unverified_sibling():
    records = records_for_business()
    report = {'business_model': {'analysis': 'Operations serve industrial customers.',
                                'evidence_ids': ['filing:business']}, 'claims': []}
    safe, audit = _supported_synthesis_sections(report, set(records), records, 'en')
    safe['business_model']['extra_analysis'] = 'Revenue was USD 999 billion.'
    row = obligation(ResearchCoverageContract().assess(safe, {'section_validation': audit}, evidence_records=records),
                     'company_business_model')
    assert row.state == 'verification_failed'


def test_isolated_verified_asset_does_not_satisfy_financial_history():
    facts = [fact for fact in demo_facts() if fact['concept'] == 'assets']
    refs = {row['evidence_id']: row for row in build_fact_evidence(facts)}
    row = obligation(ResearchCoverageContract().assess({}, {}, evidence_records=refs,
                     metrics=calculate_metrics(facts)), 'historical_financial_performance')
    assert row.state == 'missing_source_data'


def test_opinion_role_cannot_bypass_reported_numeric_facts():
    records = records_for_business()
    report = {'business_model': {'kind': 'opinion', 'analysis': 'Revenue USD 999 billion.',
                                'evidence_ids': ['filing:business']}}
    safe, audit = _supported_synthesis_sections(report, set(records), records, 'en')
    assert 'business_model' not in safe
    assert not audit['nodes']


def test_future_clause_does_not_bless_historical_quantity_in_same_opinion():
    for body in ('Revenue in 2025 USD 999 billion, but it could grow.',
                 'The margin is 99%; future expansion could improve profitability.'):
        report = {'business_model': {'kind': 'opinion', 'analysis': body}}
        safe, audit = _supported_synthesis_sections(report, set(), {}, 'en')
        assert 'business_model' not in safe
        assert not audit['nodes']


def test_explicit_future_quantity_is_retained_as_labelled_assumption_only():
    report = {'scenarios': [{'name': 'Upside', 'kind': 'assumption',
              'analysis': 'Future revenue in 2028 could reach USD 1 billion.'}]}
    safe, audit = _supported_synthesis_sections(report, set(), {}, 'en')
    assert safe['scenarios'][0]['analysis']
    assert audit['nodes'][0]['role'] == 'assumption'
    assert audit['nodes'][0]['validation_state'] == 'labelled'


def test_explicit_question_and_method_do_not_require_invented_financial_claims():
    report = {'unresolved_questions': {'kind': 'question', 'analysis': 'Which customers are repeat buyers?'},
              'evidence_and_methodology': {'kind': 'assumption', 'analysis': 'Compare annual statements and retain unresolved source gaps.'}}
    safe, audit = _supported_synthesis_sections(report, set(), {}, 'en')
    coverage = ResearchCoverageContract().assess(safe, {'section_validation': audit}, evidence_records={})
    assert obligation(coverage, 'unresolved_questions').state == 'fulfilled'
    # A declared method is displayable, but cannot establish that sources were
    # actually read when no source exists. Completeness remains independent.
    assert obligation(coverage, 'evidence_methodology').state == 'missing_evidence'


def test_bare_numeric_assertion_is_not_blessed_by_opinion_metadata():
    for text in ('Revenue is 999.', 'The company employs 900 people.',
                 '营业收入为999。', '公司拥有900名员工。'):
        report = {'business_model': {'kind': 'opinion', 'analysis': text}}
        safe, proof = _supported_synthesis_sections(report, set(), {}, 'en')
        assert 'business_model' not in safe
        assert not proof['nodes']


def test_period_only_question_and_method_remain_labelled_without_fact_invention():
    report = {
        'unresolved_questions': {'kind': 'question',
            'analysis': 'What explains the difference between the 2024 and 2025 annual reports?'},
        'evidence_and_methodology': {'kind': 'method',
            'analysis': 'Compare the 2024 and 2025 annual statements and retain unresolved source gaps.'},
    }
    safe, proof = _supported_synthesis_sections(report, set(), {}, 'en')
    coverage = ResearchCoverageContract().assess(safe, {'section_validation': proof}, evidence_records={})
    assert obligation(coverage, 'unresolved_questions').state == 'fulfilled'
    assert obligation(coverage, 'evidence_methodology').state == 'fulfilled'


def test_question_or_method_metadata_cannot_bless_a_reported_amount():
    for kind, body in (
        ('question', 'Revenue was USD 999 billion; which customers drove it?'),
        ('method', 'Revenue is 999; compare it with the next annual report.'),
    ):
        safe, proof = _supported_synthesis_sections(
            {'unresolved_questions': {'kind': kind, 'analysis': body}}, set(), {}, 'en')
        assert not proof['nodes']
        assert 'unresolved_questions' not in safe
