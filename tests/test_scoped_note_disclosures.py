from dataclasses import replace

import pytest

from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_sources.document_units import discover_document_units
from openthesis.financial_sources.note_disclosures import extract_note_candidates
from openthesis.financial_validation import FinancialValidator
from test_explicit_financial_disclosures import candidate


TABLE = "冰轮环境2025年年度报告全文\n（六十一）现金流量表补充资料\n项目 本期发生额 上期发生额\n净利润 630,008,343.39 574,246,448.99\n经营活动产生的现金流量净额 820,769,562.21 720,672,785.08"
PAGES = ["财务报表附注\n（除特别注明外，本附注金额单位均为人民币元）\n本公司会计年度为公历年度，即每年1月1日起至12月31日止。",
         "五、合并财务报表重要项目注释", TABLE, "六、合并范围的变更", TABLE]


def notes(pages=PAGES, page=3):
    company, filing, _, _ = candidate()
    units = discover_document_units(pages)
    facts, refs = extract_note_candidates(company, filing, page, pages[page - 1],
        (10., 20., 500., 760.), units.note_for_page(page))
    return company, filing, facts, refs


def check(fact, refs):
    row = next(ref for ref in refs if ref.evidence_id == "fact:" + fact.fact_id)
    return FinancialValidator().validate_group([fact],
        (fact.company_cik, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: row}, expected_company_id=fact.company_cik, source_evidence=refs)


def test_note_declaration_is_unavailable_to_formal_statements():
    units = discover_document_units(PAGES)
    assert units.for_scope("consolidated") is None
    assert units.currency_for_scope("consolidated") is None
    assert units.note_for_page(3).scope == "consolidated"
    assert units.note_for_page(1) is None
    assert units.note_for_page(4) is None


def test_explicit_note_columns_restore_two_independent_amounts_with_full_source_chain():
    company, filing, facts, refs = notes()
    assert {item.concept for item in facts} == {"net_income", "operating_cash_flow"}
    assert all(check(item, refs).validation.accepted for item in facts)
    dataset = FinancialFactCompiler().compile_facts(company, [filing], facts, evidence=refs)
    values = {item.concept: item.value for item in dataset.research_facts}
    assert values == {"net_income": 630008343.39, "operating_cash_flow": 820769562.21}
    assert all(item.currency == "CNY" and len(item.classification_evidence_ids) == 4 for item in facts)
    records = dataset.coverage["fact_validation_records"]
    for fact in facts:
        assert records[fact.fact_id]["accepted"]
        assert all(value["state"] == "verified" for value in records[fact.fact_id]["components"].values())


@pytest.mark.parametrize("field,value", [
    ("currency", "USD"), ("consolidated_scope", "parent"), ("fiscal_year", 2024),
    ("source_page", 5), ("source_column", "项目 上期发生额 本期发生额"),
    ("value", 574246448.99), ("end_date", "2025-03-31"),
])
def test_note_candidate_metadata_cannot_change_original_identity(field, value):
    _, _, facts, refs = notes()
    assert not check(replace(facts[0], **{field: value}), refs).validation.accepted


def test_chapter_scope_never_reaches_later_note_or_formal_page():
    assert not notes(page=5)[2]
    _, _, facts, refs = notes()
    context = next(ref for ref in refs if ref.evidence_id.startswith("note-context:"))
    changed = [replace(ref, locator="page:5") if ref.evidence_id in {context.evidence_id, "fact:" + facts[0].fact_id}
               else ref for ref in refs]
    assert not check(replace(facts[0], source_page=5), changed).validation.accepted


@pytest.mark.parametrize("pages", [
    [PAGES[0].replace("本附注金额单位均为人民币元", "本公司以人民币为记账本位币"), *PAGES[1:]],
    [PAGES[0].replace("会计年度为公历年度，即每年1月1日起至12月31日止", "会计年度从4月1日至3月31日"), *PAGES[1:]],
    [PAGES[0], PAGES[1], TABLE.replace("本期发生额 上期发生额", "上期发生额 本期发生额"), *PAGES[3:]],
    [PAGES[0], PAGES[1], TABLE.replace("2025年年度报告", "2024年年度报告"), *PAGES[3:]],
    [PAGES[0], PAGES[1], TABLE.replace("净利润", "子公司净利润").replace("经营活动产生的现金流量净额", "其中经营活动产生的现金流量净额"), *PAGES[3:]],
    [PAGES[0], PAGES[1], TABLE.replace("现金流量表补充资料", "重要子公司财务信息"), *PAGES[3:]],
    [PAGES[0], PAGES[1], TABLE.replace("项目 本期", "单位：美元\n项目 本期"), *PAGES[3:]],
    [PAGES[0], PAGES[1], TABLE.replace("项目 本期", "单位：万元\n项目 本期"), *PAGES[3:]],
])
def test_missing_or_ambiguous_note_proof_does_not_create_candidates(pages):
    assert not notes(pages)[2]


def test_note_declarations_retain_verbatim_extraction_whitespace():
    pages = [PAGES[0].replace("人民币元", "人民\n币元").replace("1月1日", "1 月 1 日"), *PAGES[1:]]
    unit = discover_document_units(pages).note_for_page(3)
    assert unit.declaration.excerpt in pages[0]
    assert unit.period_excerpt in pages[0]


def test_numbered_table_component_does_not_terminate_main_note_chapter():
    pages = [*PAGES[:2], "一、账面原值", TABLE, PAGES[3]]
    region = discover_document_units(pages).note_for_page(4)
    assert region is not None and region.end_page == 5
    assert notes(pages, page=4)[2]
