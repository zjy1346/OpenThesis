"""Explicit presentation currency is independent of overridden display scale."""
from decimal import Decimal

import pytest

from openthesis.domain import FilingDocument
from openthesis.financial_ingestion import _extra_money_unit_evidence, _unit_evidence_excerpt
from openthesis.financial_sources.document_units import discover_document_units
from openthesis.financial_unit_declarations import combined_money_unit
from openthesis.financial_contracts import fact_row_evidence_ids
from openthesis.financial_validation import FinancialValidator
from tests.test_pdf_statement_semantics import Page, parse


BASIS = "本公司记账本位币和编制本财务报表所采用的货币均为人民币，除有特别说明外，均以人民币千元为单位表示。"


def test_primary_unit_proof_uses_exact_table_header_not_note_table_units():
    text = "财务附注中报表的单位为：千元\n1、合并资产负债表\n编制单位：某公司\n单位：元\n2021年12月31日"
    assert _unit_evidence_excerpt(text) == "单位：元"


@pytest.mark.parametrize("header,scale", [("单位：元", "1"), ("单位：百万元", "1000000")])
def test_explicit_local_scale_and_source_currency_have_separate_bound_proof(header, scale):
    resolution = discover_document_units(["", BASIS])
    declaration = resolution.currency_for_scope("consolidated")
    assert declaration.currency == "CNY" and declaration.page == 2
    assert declaration.excerpt == BASIS.split("，", 1)[0]
    filing = FilingDocument("doc", "issuer", "acc", "ANNUAL_REPORT", "FY", "2021-12-31",
        "2022-03-29", "annual.pdf", "https://example.test/annual.pdf", content_hash="a" * 64)
    refs = _extra_money_unit_evidence("amount", filing, excerpt=header, column=None,
        page=1, currency="CNY", scale=Decimal(scale), scope="consolidated", document_units=resolution)
    assert len(refs) == 1 and refs[0].locator == "page:2"
    assert refs[0].content_hash == filing.content_hash and refs[0].document_id == filing.document_id
    assert combined_money_unit((header, refs[0].excerpt)).proves("CNY", scale)
    # An explicit scale conflict is still a conflict, not overridden by this
    # currency-only proof. The full default scale is retained separately.
    assert not combined_money_unit((header, BASIS)).proves("CNY", scale)
    assert resolution.for_scope("consolidated").scale == "1000"


@pytest.mark.parametrize("text", [
    "本公司的记账本位币为人民币，财务报表采用中国会计准则。",
    "本财务报表披露收入人民币100万元。",
    "Our financial statements recorded sales of RMB 100 million.",
    "Our functional currency is RMB. Financial statements comply with IFRS.",
])
def test_functional_currency_and_monetary_narrative_do_not_prove_presentation(text):
    assert discover_document_units([text]).currency_for_scope("consolidated") is None


def test_conflicting_or_parent_only_presentation_does_not_prove_group_currency():
    parent = "母公司财务报表以人民币列示。"
    resolution = discover_document_units([parent])
    assert resolution.currency_for_scope("consolidated") is None
    assert resolution.currency_for_scope("parent").currency == "CNY"
    conflict = discover_document_units([BASIS, "These financial statements are presented in US dollars."])
    assert conflict.currency_for_scope("consolidated") is None
    assert conflict.for_scope("consolidated") is None


def test_currency_only_basis_remains_insufficient_for_an_unknown_table_multiplier():
    resolution = discover_document_units(["These financial statements are presented in Renminbi."])
    assert resolution.currency_for_scope("consolidated").currency == "CNY"
    assert resolution.for_scope("consolidated") is None
    assert not combined_money_unit(("unit unknown",)).proves("CNY", "1000")


def test_two_formal_tables_on_one_page_do_not_share_the_last_units():
    facts, refs = parse([Page([
        ("合并利润表",), ("单位：人民币千元",), ("项目", "2025", "2024"),
        ("净利润", "100", "90"),
        ("母公司利润表",), ("单位：人民币元",), ("项目", "2025", "2024"),
        ("净利润", "200", "180"),
    ])])
    values = {(fact.consolidated_scope, fact.fiscal_year): fact for fact in facts if fact.concept == "net_income"}
    assert values["consolidated", 2025].normalization_lineage.normalized_value == "100000"
    assert values["parent", 2025].normalization_lineage.normalized_value == "200"
    by_id = {ref.evidence_id: ref for ref in refs}
    for (scope, _year), fact in values.items():
        unit_texts = [by_id[key].excerpt for key in fact.normalization_lineage.unit_evidence_ids]
        assert combined_money_unit(unit_texts).proves("CNY", "1000" if scope == "consolidated" else "1")


def test_same_table_conflicting_direct_units_do_not_hide_the_first_declaration():
    facts, refs = parse([Page([
        ("合并利润表",), ("单位：人民币千元",), ("单位：人民币百万元",),
        ("项目", "2025", "2024"), ("净利润", "100", "90"),
    ])])
    assert facts, "the original candidate remains auditable"
    for fact in facts:
        row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
        result = FinancialValidator().validate_group([fact],
            (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
            {fact.fact_id: row}, required_concepts={fact.concept}, source_evidence=refs)
        assert not result.validation.accepted
        assert "normalization_unit_declaration_mismatch" in result.validation.issues


@pytest.mark.parametrize("text", [
    "4、记账本位币\n以人民币作为记账本位币和报告货币。",
    "本公司以人民币作为报告货币。",
    "本集團以人民幣作為呈報貨幣。",
])
def test_explicit_reporting_currency_does_not_require_a_repeated_statement_title(text):
    resolution = discover_document_units(["", text])
    proof = resolution.currency_for_scope("consolidated")
    assert proof is not None and proof.currency == "CNY" and proof.page == 2
    assert proof.excerpt in text
    assert resolution.for_scope("consolidated") is None, "a currency does not establish the amount multiplier"


@pytest.mark.parametrize("text", [
    "以人民币作为记账本位币。",
    "境外子公司以美元作为报告货币。",
    "报告货币可能为人民币或美元，具体需另行确认。",
    "母公司以人民币作为报告货币。",
])
def test_unstated_or_other_entity_reporting_currency_does_not_prove_group_currency(text):
    assert discover_document_units([text]).currency_for_scope("consolidated") is None
