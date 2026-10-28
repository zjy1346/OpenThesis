from dataclasses import replace

from openthesis.domain import FinancialFact
from openthesis.demo import demo_facts
from openthesis.financial_parser_compatibility import pdf_rules_compatible
from openthesis.research_materials import material_capabilities, usable_disclosure


def old_instant():
    from openthesis.financial_contracts import SourceAmount, QuantityKind
    return replace(FinancialFact(**demo_facts()[0]), concept='assets',
        statement='balance_sheet', parser_version='financial-ingestion-ast-v32',
        source_bbox=(0., 0., 20., 20.), source_page=1, raw_text='Total assets 100',
        source_column='2025年12月31日', end_date='2025-12-31', fiscal_year=2025,
        revision='original', usage_status='canonical_research',
        normalization_lineage=SourceAmount('100','1',QuantityKind.MONEY,'USD',('unit:1',)).normalize(),
        classification_evidence_ids=('statement:proof',))


def test_compatibility_is_concept_and_column_local_not_global_version_bypass():
    original = old_instant()
    assert pdf_rules_compatible(original)
    for change in ({'concept':'revenue'}, {'usage_status':'comparator'},
                   {'source_column':'2025年'}, {'source_column':'2024-12-31'},
                   {'normalization_lineage':None}, {'parser_version':'financial-ingestion-ast-v7'}):
        assert not pdf_rules_compatible(replace(original, **change))
    assert original.parser_version == 'financial-ingestion-ast-v32'


def test_table_observation_and_disallowed_fact_shape_do_not_enable_research():
    observation = {'evidence_id':'table-structure:raw', 'document_id':'source',
        'source_url':'https://example.test/original.pdf', 'excerpt':'unvalidated rows'}
    assert not usable_disclosure(observation)
    context = {'evidence':[{'evidence_id':'fact:unadmitted', 'kind':'financial_fact'}],
        'financial_capabilities':{'quantitative_facts':False}}
    assert 'quantitative_facts' not in material_capabilities(context)


def test_compatible_original_instant_is_revalidated_and_forged_units_still_fail():
    from openthesis.financial_contracts import fact_row_evidence_ids
    from openthesis.financial_validation import FinancialValidator
    from tests.test_pdf_statement_semantics import Page, parse
    facts, refs = parse([Page([
        ('Consolidated balance sheet',), ('RMB in millions',),
        ('Item','2025-12-31','2024-12-31'), ('Total assets','100','80')])])
    current = next(fact for fact in facts if fact.concept == 'assets' and fact.fiscal_year == 2025)
    old = replace(current, parser_version='financial-ingestion-ast-v32', revision='original')
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(old))
    def validate(fact):
        return FinancialValidator().validate_group([fact],
            (fact.accession_number,fact.end_date,fact.fiscal_period,fact.consolidated_scope,fact.currency),
            {fact.fact_id:row}, required_concepts={'assets'},source_evidence=refs).validation
    assert validate(old).accepted == (old,)
    assert validate(replace(old,unit_scale=1000,value=100_000,
        normalization_lineage=replace(old.normalization_lineage,scale='1000',normalized_value='100000'))).accepted == ()
