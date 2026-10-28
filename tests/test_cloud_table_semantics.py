from dataclasses import replace
from pathlib import Path

import pytest

from openthesis.financial_cloud_proof import CloudUnitContext
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.vision_financials import VisionPageRequest, parse_vision_markdown
from test_vision_financials import _company, _filing


# Source-faithful cells from the frozen BYD FY2025 report, page 126; the
# accounting-basis declaration is on page 140. This is not a real API result.
BASIS = "本公司记账本位币和编制本财务报表所采用的货币均为人民币，除有特别说明外，均以人民币千元为单位表示。"
BYD = """# 合并利润表
2025年
| 项目 | 附注 | 2025年 | 2024年（经重述） |
| --- | --- | --- | --- |
| 一、营业收入 | 45 | 803,964,958 | 777,102,455 |
| 三、营业利润 | | 40,184,709 | 50,486,047 |
| 四、利润总额 | | 39,753,049 | 49,680,677 |
| 五、净利润 | | 33,760,758 | 41,587,940 |
| 归属于母公司股东的净利润 | | 32,619,022 | 40,254,346 |
| 少数股东损益 | | 1,141,736 | 1,333,594 |
| 基本每股收益 | | 3.58 | 4.61 |
"""


def extract(markdown=BYD, *, context=BASIS, filing=None, page_number=126):
    filing = filing or _filing()
    units = (CloudUnitContext(140, context, filing.content_hash),) if context else ()
    page = VisionPageRequest(page_number, b"frozen single-page test bytes", unit_contexts=units)
    return parse_vision_markdown(markdown, _company(), filing, (page,))


def compile_result(result, filing=None):
    return FinancialFactCompiler().compile_facts(_company(), (filing or _filing(),), result.facts,
        evidence=result.evidence, required_concepts=frozenset(fact.concept for fact in result.facts))


def test_byd_note_column_comparators_allocations_and_cross_page_unit_are_bound():
    result = extract()
    assert result.error_code is None
    assert len(result.facts) == 12
    values = {(fact.concept, fact.fiscal_year): fact.value for fact in result.facts}
    assert values["revenue", 2025] == 803_964_958_000
    assert values["revenue", 2024] == 777_102_455_000
    assert values["parent_net_income", 2025] == 32_619_022_000
    assert values["net_income", 2025] == 33_760_758_000
    assert all(fact.value != 45_000 for fact in result.facts)
    assert all(fact.revision == "restated" for fact in result.facts if fact.fiscal_year == 2024)
    unit_refs = [ref for ref in result.evidence if ref.evidence_id.startswith("vision-unit:")]
    assert len(unit_refs) == 1 and unit_refs[0].locator == "page:140"
    assert unit_refs[0].excerpt == BASIS
    assert all(fact.source_bbox is None for fact in result.facts)
    compiled = compile_result(result)
    assert len(compiled.accepted_facts) == 12
    assert not compiled.quarantined_facts


def test_actual_free_flash_image_receipt_preserves_split_label_columns_and_fullwidth_money():
    source = (Path(__file__).parent / "fixtures/cloud/mineru-flash-byd-fy2025-page126/image-only.md").read_text(encoding="utf-8")
    result = extract(source)
    assert result.error_code is None
    values = {(fact.concept, fact.fiscal_year): fact.value for fact in result.facts}
    expected = {
        "revenue": (803_964_958_000, 777_102_455_000),
        "operating_income": (40_184_709_000, 50_486_047_000),
        "profit_before_tax": (39_753_049_000, 49_680_677_000),
        "net_income": (33_760_758_000, 41_587_940_000),
        "parent_net_income": (32_619_022_000, 40_254_346_000),
        "minority_interest_profit": (1_141_736_000, 1_333_594_000),
    }
    for concept, (current, previous) in expected.items():
        assert values[concept, 2025] == current
        assert values[concept, 2024] == previous
    current = next(fact for fact in result.facts if fact.concept == "revenue" and fact.fiscal_year == 2025)
    assert current.normalization_lineage.raw_value == "803,964，958"
    assert all(fact.concept != "earnings_per_share" for fact in result.facts)
    compiled = compile_result(result)
    assert len(compiled.accepted_facts) == len(result.facts)
    assert not compiled.quarantined_facts


def test_actual_free_flash_native_receipt_never_guesses_labels_for_displaced_amounts():
    source = (Path(__file__).parent / "fixtures/cloud/mineru-flash-byd-fy2025-page126/native.md").read_text(encoding="utf-8")
    result = extract(source)
    # Native PDF output genuinely moved the revenue label to the cost row and
    # separated operating profit from its numbers. The image representation
    # is the recovery, not labelling these unbound cells by expected values.
    assert all(fact.concept not in {"revenue", "operating_income"} for fact in result.facts)
    assert {(fact.fiscal_year, fact.value) for fact in result.facts if fact.concept == "profit_before_tax"} == {
        (2025, 39_753_049_000), (2024, 49_680_677_000)}


@pytest.mark.parametrize("raw", ("1，23", "1 234", "1,234 5,678", "8O3,964,958"))
def test_cloud_typography_normalization_does_not_repair_ambiguous_or_multiple_amounts(raw):
    result = extract(BYD.replace("803,964,958", raw))
    assert all(not (fact.concept == "revenue" and fact.fiscal_year == 2025) for fact in result.facts)


def test_html_headers_rowspan_and_reversed_year_columns_are_not_flattened():
    markup = """<h2>Consolidated Income Statement</h2><p>Unit: RMB million</p>
    <table><tr><th rowspan="2">Item</th><th rowspan="2">Note</th><th>2024 (restated)</th><th>2025</th></tr>
    <tr><th>RMB million</th><th>RMB million</th></tr>
    <tr><td>Revenue</td><td>45</td><td>777,102.455</td><td>803,964.958</td></tr></table>"""
    result = extract(markup, context="")
    assert result.error_code is None
    assert {(fact.fiscal_year, fact.value) for fact in result.facts} == {
        (2024, 777_102_455_000), (2025, 803_964_958_000)}
    assert not compile_result(result).quarantined_facts


@pytest.mark.parametrize("change", [
    lambda fact: replace(fact, concept="parent_net_income"),
    lambda fact: replace(fact, end_date="2024-12-31", fiscal_year=2024),
    lambda fact: replace(fact, source_column="2024年（经重述）"),
    lambda fact: replace(fact, revision="restated"),
    lambda fact: replace(fact, unit_scale=1_000_000),
    lambda fact: replace(fact, normalization_lineage=replace(fact.normalization_lineage,
        raw_value="777,102,455", normalized_value="777102455000"), value=777_102_455_000),
])
def test_independent_compiler_rejects_tampered_column_identity_and_self_consistent_amount(change):
    result = extract()
    current = next(fact for fact in result.facts if fact.concept == "revenue" and fact.fiscal_year == 2025)
    tampered = change(current)
    compiled = compile_result(replace(result, facts=(tampered,)))
    assert not compiled.accepted_facts
    assert compiled.quarantined_facts


def test_unit_must_be_explicit_and_from_same_document_not_listing_currency():
    for context in ("", "财务报表均以千元表示。", BASIS.replace("人民币", "美元")):
        assert not extract(context=context).facts
    page = VisionPageRequest(126, b"page", unit_contexts=(CloudUnitContext(140, BASIS, "another-document"),))
    assert not parse_vision_markdown(BYD, _company(), _filing(), (page,)).facts


def test_local_table_unit_overrides_document_unit_but_conflicting_local_units_are_not_guessed():
    local = BYD.replace("2025年\n", "2025年\n单位：人民币元\n", 1)
    result = extract(local)
    assert next(fact.value for fact in result.facts if fact.concept == "revenue" and fact.fiscal_year == 2025) == 803_964_958
    conflict = local.replace("单位：人民币元", "单位：人民币元\n单位：人民币百万元")
    assert not extract(conflict).facts


def test_parent_statement_boundary_and_misplaced_balance_rows_do_not_borrow_consolidation():
    text = """Consolidated Income Statement
    Unit: RMB million
    Year ended 2025-12-31
    Revenue 10
    Total assets 100
    Parent company balance sheet
    Unit: RMB million
    | Item | 2025 | 2024 |
    | --- | --- | --- |
    | Total assets | 999 | 888 |
    """
    result = extract(text, context="")
    assert {(fact.concept, fact.value) for fact in result.facts
            if fact.consolidated_scope == 'consolidated'} == {("revenue", 10_000_000)}
    assert {(fact.concept, fact.value) for fact in result.facts
            if fact.consolidated_scope == 'parent'} == {('assets', 999_000_000), ('assets', 888_000_000)}


def test_fiscal_year_end_is_not_assumed_december_and_explicit_wrong_date_is_rejected():
    filing = replace(_filing(), period_end="2025-03-31")
    text = """Consolidated Income Statement
    Unit: RMB million
    | Item | 2025-03-31 | 2024-03-31 |
    | --- | --- | --- |
    | Revenue | 10 | 8 |
    """
    result = extract(text, context="", filing=filing)
    assert {(fact.end_date, fact.start_date) for fact in result.facts} == {
        ("2025-03-31", "2024-04-01"), ("2024-03-31", "2023-04-01")}
    assert not compile_result(result, filing).quarantined_facts
    assert not extract(text.replace("-03-31", "-12-31"), context="", filing=filing).facts


def test_rows_beyond_old_two_thousand_character_context_remain_evidenced():
    prefix = "Consolidated Income Statement\nUnit: RMB million\nYear ended 2025-12-31\n"
    result = extract(prefix + ("Unrelated disclosure text.\n" * 200) + "Revenue 10", context="")
    assert len(result.facts) == 1 and result.facts[0].raw_text == "Revenue | 10"
    assert not compile_result(result).quarantined_facts


def test_capex_ppe_is_a_component_not_a_complete_capital_expenditure():
    text = """Consolidated statement of cash flows
    Unit: RMB million
    | Item | 2025 | 2024 |
    | --- | --- | --- |
    | Purchase of property, plant and equipment | (10) | (8) |
    | Payments of lease liabilities | (3) | (2) |
    """
    result = extract(text, context="")
    assert {fact.concept for fact in result.facts} == {"capital_expenditure_ppe", "lease_liability_payment"}
    assert all(fact.value > 0 for fact in result.facts)
    assert not compile_result(result).quarantined_facts


def test_traditional_chinese_title_and_revenue_are_normalized_without_changing_source():
    text = """# 合併利潤表
    單位：人民幣千元
    | 項目 | 2025年 | 2024年 |
    | --- | --- | --- |
    | 一、營業收入 | 10 | 8 |
    """
    result = extract(text, context="")
    assert len(result.facts) == 2
    assert all(fact.reported_concept == "一、營業收入" for fact in result.facts)
    assert not compile_result(result).quarantined_facts


def test_html_formal_title_inside_table_and_standard_operations_title_are_supported():
    text = """<table>
    <tr><th colspan="3">Consolidated Statements of Operations</th></tr>
    <tr><th colspan="3">RMB million</th></tr>
    <tr><th>Item</th><th>2025</th><th>2024</th></tr>
    <tr><td>Revenue</td><td>10</td><td>8</td></tr></table>"""
    result = extract(text, context="")
    assert len(result.facts) == 2
    assert not compile_result(result).quarantined_facts


def test_plain_columns_keep_note_numbers_out_of_amounts():
    result = extract("合并利润表\n项目 附注 2025年 2024年\n一、营业收入 45 803,964,958 777,102,455")
    assert {(fact.fiscal_year, fact.value) for fact in result.facts} == {
        (2025, 803_964_958_000), (2024, 777_102_455_000)}
    assert not compile_result(result).quarantined_facts


def test_partial_local_unit_must_agree_with_the_broad_declaration():
    assert extract(BYD.replace("2025年\n", "2025年\n单位：千元\n", 1)).facts
    assert not extract(BYD.replace("2025年\n", "2025年\n单位：百万元\n", 1)).facts


def test_separate_local_currency_and_scale_lines_are_composed_without_implicit_base_units():
    result = extract(BYD.replace("2025年\n", "2025年\n币种：人民币\n单位：千元\n", 1), context="")
    assert result.facts
    assert all(fact.unit_scale == 1000 for fact in result.facts)
    assert not compile_result(result).quarantined_facts
