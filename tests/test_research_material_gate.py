from openthesis.research_materials import has_sourced_disclosure, route_materials


def disclosure(**overrides):
    return {
        'evidence_id': 'filing:business:source', 'kind': 'filing_text',
        'document_id': 'official-report', 'source_url': 'https://official.example/report.pdf',
        'topic': 'business', 'excerpt': 'The group sells electric vehicles and batteries.',
        **overrides,
    }


def test_primary_disclosure_material_uses_the_same_sourced_body_contract_as_role_routing():
    for topic in ('business', 'management_discussion', 'risk_factors', 'audit'):
        assert has_sourced_disclosure([disclosure(topic=topic)])


def test_nonempty_diagnostic_catalogue_does_not_authorize_model_research():
    records = [
        disclosure(kind='material_gap', reason='disclosure_text_unavailable'),
        disclosure(evidence_id='table:numeric', excerpt='Revenue 100 90'),
        # A retrieval hint or unit heading is not a sourced body on its own,
        # even when the surrounding source identity is present.
        disclosure(topic='unknown', excerpt='Unit: CNY millions'),
        disclosure(document_id=''), disclosure(source_url=''), disclosure(excerpt=''),
    ]
    assert not has_sourced_disclosure(records)
    assert not has_sourced_disclosure([])


def test_real_body_is_not_hidden_by_unrelated_failed_extraction_notice():
    records = [disclosure(kind='material_gap'), disclosure(topic='risk_factors')]
    assert has_sourced_disclosure(records)


def test_topic_is_only_a_retrieval_hint_for_all_source_body_roles():
    records = [disclosure(topic='unrecognized-section',
                          excerpt='The issuer describes its operations and principal risks.')]
    for role in ('business-analyst', 'growth-opportunity-analyst',
                 'skeptical-analyst', 'accounting-risk-analyst',
                 'forecast-analyst', 'research-synthesizer'):
        gate = route_materials({'evidence': records}, role)['material_adequacy']
        assert gate['status'] == 'ready', (role, gate)
        if role in {'business-analyst', 'growth-opportunity-analyst',
                    'skeptical-analyst', 'accounting-risk-analyst'}:
            assert gate['retrieval_topic_gaps'], role


def test_financial_role_still_requires_quantitative_facts_without_source_substitution():
    gate = route_materials({'evidence': [disclosure(topic='unrecognized-section')]},
                           'financial-analyst')['material_adequacy']
    assert gate['status'] == 'insufficient'
    assert gate['missing'] == ['quantitative_facts']
