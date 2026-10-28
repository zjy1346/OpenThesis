"""Revenue note totals require an observed caption and both column levels."""
from dataclasses import replace

import pytest

from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_sources.document_units import discover_document_units
from openthesis.financial_sources.note_disclosures import extract_note_candidates
from test_explicit_financial_disclosures import candidate
from test_scoped_note_disclosures import PAGES, check


TABLE = ('冰轮环境2025年年度报告全文\n'
    '(四十四)营业收入和营业成本\n1.营业收入和营业成本情况\n'
    '项目 本期发生额 上期发生额\n收入 成本 收入 成本\n'
    '主营业务 6,955,497,204.97 5,106,487,372.49 6,542,914,995.30 4,761,975,120.39\n'
    '其他业务 96,337,433.35 46,226,527.88 92,024,468.36 39,514,351.42\n'
    '合计 7,051,834,638.32 5,152,713,900.37 6,634,939,463.66 4,801,489,471.81\n'
    '2.营业收入、营业成本分解信息\n收入分类 本期发生额 上期发生额\n'
    '营业收入 营业成本 营业收入 营业成本\n'
    '合计 7,051,834,638.32 5,152,713,900.37 6,634,939,463.66 4,801,489,471.81')


def notes(text=TABLE):
    company, filing, _, _ = candidate()
    pages = [PAGES[0], PAGES[1], text, PAGES[3]]
    region = discover_document_units(pages).note_for_page(3)
    facts, refs = extract_note_candidates(company, filing, 3, text, (10., 20., 500., 760.), region)
    return company, filing, facts, refs


def test_four_column_note_total_independently_proves_exact_revenue_and_cost():
    company, filing, facts, refs = notes()
    assert {fact.concept: fact.value for fact in facts} == {
        'revenue': 7051834638.32, 'cost_of_revenue': 5152713900.37}
    assert len({fact.fact_id for fact in facts}) == 2
    assert all(check(fact, refs).validation.accepted for fact in facts)
    dataset = FinancialFactCompiler().compile_facts(company, [filing], facts, evidence=refs)
    assert {fact.concept for fact in dataset.research_facts} == {'revenue', 'cost_of_revenue'}
    for fact in facts:
        record = dataset.coverage['fact_validation_records'][fact.fact_id]
        assert record['accepted']
        assert all(component['state'] == 'verified' for component in record['components'].values())


def test_note_index_retains_original_table_order_without_rewriting_coordinate_text():
    pages = [PAGES[0], PAGES[1], TABLE, PAGES[3]]
    units = discover_document_units(pages)
    assert units.note_text_for_page(3) == TABLE
    assert units.note_text_for_page(4) is None
    assert units.for_scope('consolidated') is None
    # A different backend's line order is not repaired by guessing which
    # dangling number belongs in the missing second column.
    reordered = TABLE.replace('项目 本期发生额 上期发生额', '本期发生额 上期发生额\n项目')
    assert not notes(reordered)[2]


@pytest.mark.parametrize('text', [
    TABLE.replace('(四十四)营业收入和营业成本', '(四十四)子公司营业收入和营业成本'),
    TABLE.replace('1.营业收入和营业成本情况', '1.按经营地区分类'),
    TABLE.replace('项目 本期发生额 上期发生额', '项目 上期发生额 本期发生额'),
    TABLE.replace('收入 成本 收入 成本', '成本 收入 成本 收入'),
    TABLE.replace('收入 成本 收入 成本', '收入 成本'),
    TABLE.replace('合计 ', '主营业务小计 '),
    TABLE.replace('(四十四)营业收入和营业成本', ''),
    TABLE.replace('1.营业收入和营业成本情况', ''),
    TABLE.replace('项目 本期发生额', '单位：美元\n项目 本期发生额'),
    TABLE.replace('2025年年度报告', '2024年年度报告'),
])
def test_ambiguous_subtotal_scope_period_or_columns_never_create_revenue_facts(text):
    assert not notes(text)[2]


@pytest.mark.parametrize('field,value', [
    ('value', 6634939463.66), ('source_column', '项目 上期发生额 本期发生额'),
    ('concept', 'cost_of_revenue'), ('consolidated_scope', 'parent'), ('currency', 'USD'),
])
def test_candidate_cannot_swap_current_prior_amount_or_column_concept(field, value):
    _, _, facts, refs = notes()
    revenue = next(fact for fact in facts if fact.concept == 'revenue')
    assert not check(replace(revenue, **{field: value}), refs).validation.accepted


def test_exact_note_and_rounded_audit_are_compiled_together_without_losing_revenue():
    company, filing, facts, refs = notes()
    _, _, audit, audit_refs = candidate()
    result = FinancialFactCompiler().compile_facts(company, [filing], [*facts, *audit],
                                                  evidence=[*refs, *audit_refs])
    assert {fact.concept: fact.value for fact in result.research_facts} == {
        'revenue': 7051834638.32, 'cost_of_revenue': 5152713900.37}
    assert not result.conflicts
    proof = result.coverage['candidate_resolutions'][0]
    assert proof['policy'] == 'same-original-display-quantization-v1'
    assert proof['rounded_candidate_ids'] == (audit[0].fact_id,)
    assert {record['fact_id'] for record in proof['observations']} == {
        audit[0].fact_id, next(fact.fact_id for fact in facts if fact.concept == 'revenue')}
    # No currency or extra digits have been transferred from the rounded
    # audit. The selected note had its own independent five-component proof.
    chosen = next(fact for fact in result.research_facts if fact.concept == 'revenue')
    assert chosen.normalization_lineage.raw_value == '7051834638.32'
    record = result.coverage['fact_validation_records'][chosen.fact_id]
    assert record['accepted'] and all(part['state'] == 'verified'
                                     for part in record['components'].values())


def test_note_audit_real_disagreement_stays_quarantined():
    company, filing, facts, refs = notes()
    _, _, audit, audit_refs = candidate(
        '冰轮环境2025年年度报告全文\n合并财务报表中2025年度的营业收入为人民币705,184.46万元。')
    result = FinancialFactCompiler().compile_facts(company, [filing], [*facts, *audit],
                                                  evidence=[*refs, *audit_refs])
    assert {fact.concept for fact in result.research_facts} == {'cost_of_revenue'}
    assert result.conflicts and result.conflicts[0]['concept'] == 'revenue'
