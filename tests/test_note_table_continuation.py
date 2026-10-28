"""Adjacent note captions require original page/period/scope witnesses."""
from dataclasses import replace

import pytest

from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_ingestion import _candidate_pages_from_text
from openthesis.financial_sources.document_units import discover_document_units
from openthesis.financial_sources.note_disclosures import extract_note_candidates
from test_explicit_financial_disclosures import candidate
from test_scoped_note_disclosures import PAGES, check

PREVIOUS = ('冰轮环境2025年年度报告全文\n126\n40、未分配利润\n'
            '期末未分配利润 4,041,115,093.50 3,605,500,894.34\n'
            '41、营业收入和营业成本\n(1) 营业收入和营业成本情况')
CURRENT = ('冰轮环境2025年年度报告全文\n127\n项目 本期发生额 上期发生额\n'
           '营业收入 营业成本 营业收入 营业成本\n'
           '主营业务小计 6,542,914,995.30 4,761,975,120.39 7,218,193,144.89 5,408,099,996.57\n'
           '其他业务小计 92,024,468.36 39,514,351.42 276,888,316.11 193,513,043.02\n'
           '合计 6,634,939,463.66 4,801,489,471.81 7,495,081,461.00 5,601,613,039.59\n'
           '(2) 营业收入、营业成本的分解信息\n项目 本期发生额 上期发生额\n'
           '营业收入 营业成本 营业收入 营业成本\n合计 9.00 8.00 7.00 6.00')


def extract(previous=PREVIOUS, current=CURRENT, *, boundary=None, scope_heading=None):
    pages = [PAGES[0], PAGES[1], previous, current, PAGES[3]]
    if scope_heading is not None:
        pages[1] = scope_heading
    if boundary:
        pages[2] = boundary + '\n' + previous
    company, filing, _, _ = candidate()
    units = discover_document_units(pages)
    return company, filing, extract_note_candidates(company, filing, 4, current,
        (10., 20., 500., 760.), units.note_for_page(4),
        continuation=units.note_continuation_for_page(4))


def test_adjacent_original_caption_restores_only_the_first_total_table():
    company, filing, (facts, refs) = extract()
    assert {fact.concept: fact.value for fact in facts} == {
        'revenue': 6634939463.66, 'cost_of_revenue': 4801489471.81}
    assert all(check(fact, refs).validation.accepted for fact in facts)
    assert all(len(fact.classification_evidence_ids) == 5 for fact in facts)
    caption = next(ref for ref in refs if ref.evidence_id.startswith('note-continuation:'))
    assert caption.locator == 'page:3' and caption.excerpt == PREVIOUS
    assert all(fact.raw_text in CURRENT and fact.source_page == 4 for fact in facts)
    assert {fact.concept for fact in FinancialFactCompiler().compile_facts(
        company, [filing], facts, evidence=refs).research_facts} == {'revenue', 'cost_of_revenue'}


@pytest.mark.parametrize('previous,current,boundary', [
    (PREVIOUS + '\n42、税金及附加', CURRENT, None),
    (PREVIOUS.replace('41、营业收入和营业成本', '41、子公司营业收入和营业成本'), CURRENT, None),
    (PREVIOUS.replace('2025年年度报告', '2024年年度报告'), CURRENT, None),
    (PREVIOUS.replace('(1)', '(2)'), CURRENT, None),
    (PREVIOUS, CURRENT.replace('项目 本期发生额 上期发生额', '项目 上期发生额 本期发生额'), None),
    (PREVIOUS, CURRENT.replace('营业收入 营业成本 营业收入 营业成本', '营业成本 营业收入 营业成本 营业收入'), None),
    (PREVIOUS, CURRENT, '六、合并范围的变更'),
    (PREVIOUS, CURRENT.replace('127\n', '127\n42、税金及附加\n'), None),
    (PREVIOUS + '\n单位：美元', CURRENT, None),
])
def test_not_a_continuation_never_borrows_the_old_caption(previous, current, boundary):
    assert not extract(previous, current, boundary=boundary)[2][0]


def test_validator_rejects_modified_caption_page_hash_or_year():
    _, _, (facts, refs) = extract()
    caption = next(ref for ref in refs if ref.evidence_id.startswith('note-continuation:'))
    for changed in (replace(caption, locator='page:2'), replace(caption, content_hash='8' * 64),
                    replace(caption, excerpt=caption.excerpt.replace('2025', '2024'))):
        assert not check(facts[0], [changed if ref is caption else ref for ref in refs]).validation.accepted


def test_candidate_page_prepass_uses_discovered_cross_page_note_context():
    previous_2024 = PREVIOUS.replace('2025', '2024')
    current_2024 = CURRENT.replace('2025', '2024')
    statement_pages = [
        'CONSOLIDATED INCOME STATEMENT',
        'CONSOLIDATED BALANCE SHEET',
        'CONSOLIDATED STATEMENT OF CASH FLOWS',
    ]
    pages = [*statement_pages, PAGES[0], PAGES[1], previous_2024,
             current_2024, PAGES[3], PAGES[4]]
    current_page = 7

    units = discover_document_units(pages)
    assert units.note_text_for_page(current_page) == current_2024
    assert units.note_continuation_for_page(current_page) == (current_page - 1, previous_2024)
    selected = _candidate_pages_from_text(pages, continuation_pages=0)
    assert selected is not None and current_page in selected

    # A conflicting next note heading severs the adjacent-caption proof. The
    # page has no formal statement title or explicit audit-disclosure channel.
    bad_pages = [*statement_pages, PAGES[0], PAGES[1],
                 previous_2024 + '\n42、税金及附加', current_2024, PAGES[3], PAGES[4]]
    bad_units = discover_document_units(bad_pages)
    assert bad_units.note_text_for_page(current_page) is None
    bad_selected = _candidate_pages_from_text(bad_pages, continuation_pages=0)
    assert bad_selected is not None and current_page not in bad_selected


@pytest.mark.parametrize('scope_heading,scope', [
    ('五、合并财务报表主要项目附注', 'consolidated'),
    ('五、母公司财务报表主要项目附注', 'parent'),
])
def test_explicit_note_chapter_alias_retains_observed_accounting_scope(scope_heading, scope):
    _, _, (facts, refs) = extract(scope_heading=scope_heading)
    assert {fact.concept for fact in facts} == {'revenue', 'cost_of_revenue'}
    assert all(fact.consolidated_scope == scope and check(fact, refs).validation.accepted for fact in facts)
    assert next(ref for ref in refs if ref.evidence_id.startswith('note-scope:')).excerpt == scope_heading


@pytest.mark.parametrize('scope_heading', [
    '五、子公司财务报表主要项目附注', '五、财务报表主要项目附注',
    '五、合并财务报表主要项目附注与母公司财务报表附注',
])
def test_note_chapter_alias_never_invents_an_unspecified_or_mixed_scope(scope_heading):
    assert not extract(scope_heading=scope_heading)[2][0]
