"""Minimal positioned regressions drawn from independently replayed originals."""
from openthesis.financial_contracts import fact_row_evidence_ids
from openthesis.financial_ingestion import PdfCellAST, PdfRowAST, _period_columns
from openthesis.financial_validation import FinancialValidator
from tests.test_pdf_statement_semantics import Page, parse


def test_word_split_closing_and_opening_dates_keep_distinct_columns():
    cells = tuple(
        PdfCellAST(text, x, 20, x + len(text) * 6, 28)
        for text, x in (("Item", 50), ("31", 350), ("December", 368),
                        ("2023", 424), ("1", 505), ("January", 517), ("2023", 563))
    )
    row = PdfRowAST(cells, 20, (50, 20, 587, 28))
    columns = _period_columns((row,))
    assert [(column.year, column.header) for column in columns] == [
        (2023, "31 December 2023"), (2022, "1 January 2023"),
    ]


def test_operating_revenue_is_not_replaced_by_total_including_interest():
    facts, _ = parse([Page([
        ("合并利润表",), ("单位：人民币千元",), ("项目", "2025", "2024"),
        ("一、营业总收入", "110", "99"), ("其中：营业收入", "100", "90"),
        ("利息收入", "10", "9"),
    ])])
    values = {fact.concept: fact.normalization_lineage.normalized_value for fact in facts
              if fact.fiscal_year == 2025 and fact.consolidated_scope == "consolidated"}
    assert values["revenue"] == "100000"
    assert values["total_operating_revenue"] == "110000"
    assert values["interest_revenue"] == "10000"


def test_numbered_cas_english_cashflow_title_and_signed_total_are_recognized():
    facts, _ = parse([Page([
        ("5. Consolidated Cash Flow Statement",), ("Unit: RMB",),
        ("Item", "2025", "2024"),
        ("Net cash generated from/used in operating activities", "100", "(90)"),
    ])])
    values = {fact.fiscal_year: fact.normalization_lineage.normalized_value for fact in facts
              if fact.concept == "operating_cash_flow" and fact.consolidated_scope == "consolidated"}
    assert values == {2025: "100", 2024: "-90"}


def test_wrapped_equity_label_with_values_between_fragments_is_source_bound():
    facts, refs = parse([Page([
        ("合并资产负债表",), ("单位：人民币千元",), ("项目", "2025", "2024"),
        ("所有者权益（或股东权", "100", "90"), ("益）合计",),
    ], line_gap=12)])
    fact = next(fact for fact in facts if fact.concept == "total_equity" and fact.fiscal_year == 2025)
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    result = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period,
                 fact.consolidated_scope, fact.currency), {fact.fact_id: row},
        required_concepts={"total_equity"}, source_evidence=refs,
    )
    assert result.validation.accepted == (fact,)


def test_quarterly_tables_do_not_conflict_with_annual_statement_date():
    from openthesis.financial_ingestion import _manifest_for, _manifest_from_observed_pages
    from tests.test_statement_period_binding import filing

    source = filing("2021 Annual Report")
    source.period_end = "2021-12-31"
    source.filed_at = "2022-04-27"
    manifest = _manifest_from_observed_pages(_manifest_for(source), source, [
        "Consolidated Statement of Cash Flows\nYear ended December 31,\n2021 2020\n(in millions of RMB)",
        "Consolidated Statement of Cash Flows\nUnaudited\nThree months ended\nDecember 31, 2021 September 30, 2021\n(in millions of RMB)",
    ])
    assert manifest.period_end == "2021-12-31"
    assert manifest.revision != "period_header_conflict"


def test_cas_long_parent_statement_title_ends_consolidated_scope_before_continuation():
    facts, _ = parse([
        Page([("5. Consolidated Cash Flow Statement",), ("Unit: RMB",),
              ("Item", "2025", "2024"),
              ("Net cash generated from/used in operating activities", "100", "90"),
              ("6. Cash Flow Statement of the Company as the Parent",),
              ("Unit: RMB",), ("Item", "2025", "2024")]),
        Page([("Net cash generated from/used in operating activities", "10", "9")]),
    ])
    current = {fact.consolidated_scope: fact.normalization_lineage.normalized_value for fact in facts
               if fact.concept == "operating_cash_flow" and fact.fiscal_year == 2025}
    assert current == {"consolidated": "100", "parent": "10"}


def test_long_narrative_reference_to_parent_does_not_start_a_statement():
    from openthesis.financial_ingestion import _statement_context

    prose = "Cash Flow Statement was discussed by the parent in the following audit observations"
    assert _statement_context(prose) is None


def test_total_owners_equity_is_distinct_from_parent_attributed_equity():
    facts, _ = parse([Page([
        ("Consolidated Balance Sheet",), ("Unit: RMB",), ("Item", "2025", "2024"),
        ("Total equity attributable to owners of the parent company", "100", "90"),
        ("Non-controlling interests", "20", "18"),
        ("Total owners’ equity", "120", "108"),
    ])])
    current = {fact.concept: fact.normalization_lineage.normalized_value for fact in facts
               if fact.fiscal_year == 2025}
    assert current["equity"] == "100"
    assert current["total_equity"] == "120"
