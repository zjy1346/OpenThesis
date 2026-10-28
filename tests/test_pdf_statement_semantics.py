"""Positioned source fixtures for concept, scope and unit-evidence identity."""
from unittest.mock import patch
from dataclasses import replace

from openthesis.domain import FilingDocument
from openthesis.financial_ingestion import FinancialIngestionEngine, _manifest_for
from openthesis.markets import build_company
from openthesis.financial_validation import FinancialValidator
from openthesis.financial_contracts import fact_row_evidence_ids
from openthesis.domain import FinancialNormalizationLineage


class Page:
    def __init__(self, lines, line_gap=20):
        self.lines = lines
        self.line_gap = line_gap

    def extract_text(self):
        return "\n".join(" ".join(row) for row in self.lines)

    def extract_words(self, **_kwargs):
        words = []
        for index, row in enumerate(self.lines):
            top = index * self.line_gap + 10
            for position, text in enumerate(row):
                x = 50 if position == 0 else 370 + 140 * (position - 1)
                words.append({"text": text, "x0": x, "x1": x + min(len(text) * 6, 240),
                              "top": top, "bottom": top + 8})
        return words


class Pdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def parse(pages, selected=None):
    company = build_company("002594.SZ", "BYD")
    filing = FilingDocument(
        "byd-fixture", company.security_id, "byd-fixture", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-27T00:00:00+00:00", "2025 年年度报告",
        "https://example.invalid/byd.pdf", content_hash="a" * 64,
    )
    with patch("pdfplumber.open", return_value=Pdf(pages)):
        return FinancialIngestionEngine()._parse_pdf_ast(
            "nonexistent-positioned-fixture.pdf", company, filing, _manifest_for(filing),
            candidate_pages=frozenset(selected or range(1, len(pages) + 1)), index_precomputed=True,
        )


def test_group_parent_and_minority_profit_are_distinct_exact_source_facts():
    page = Page([
        ("合并利润表",), ("单位：人民币千元",), ("项目", "2025", "2024"),
        ("净利润", "33,760,758", "41,587,940"),
        ("归属于母公司所有者的净利润", "32,619,022", "40,254,346"),
        ("少数股东损益", "1,141,736", "1,333,594"),
    ])
    facts, refs = parse([page])
    values = {(fact.concept, fact.fiscal_year): fact for fact in facts}
    for concept, amount in (("net_income", "33760758000"),
                            ("parent_net_income", "32619022000"),
                            ("minority_interest_profit", "1141736000")):
        fact = values[concept, 2025]
        assert fact.normalization_lineage.normalized_value == amount
        assert fact.consolidated_scope == "consolidated"
        assert fact.source_page == 1
        assert any(ref.evidence_id in fact.normalization_lineage.unit_evidence_ids for ref in refs)
    assert values["net_income", 2024].normalization_lineage.normalized_value == "41587940000"


def test_parent_company_table_never_overwrites_consolidated_candidate():
    facts, _ = parse([
        Page([("合并利润表",), ("单位：人民币千元",), ("项目", "2025", "2024"),
              ("净利润", "100", "90")]),
        Page([("母公司利润表",), ("单位：人民币千元",), ("项目", "2025", "2024"),
              ("净利润", "200", "180")]),
    ])
    values = {(fact.consolidated_scope, fact.fiscal_year): fact for fact in facts
              if fact.concept == "net_income"}
    assert set(values) == {("consolidated", 2025), ("consolidated", 2024),
                           ("parent", 2025), ("parent", 2024)}
    assert values["consolidated", 2025].normalization_lineage.normalized_value == "100000"
    assert values["parent", 2025].normalization_lineage.normalized_value == "200000"


def test_equity_and_liabilities_total_never_conflicts_with_actual_total_equity():
    facts, _ = parse([Page([
        ("Consolidated Balance Sheets",), ("RMB in thousands",), ("Item", "2025", "2024"),
        ("Total equity", "150", "170"), ("Total equity and liabilities", "340", "320"),
    ])])
    values = [fact.normalization_lineage.normalized_value for fact in facts
              if fact.concept == "total_equity" and fact.fiscal_year == 2025]
    assert values == ["150000"]


def test_parent_equity_and_noncontrolling_balance_components_keep_source_hierarchy():
    facts, refs = parse([Page([
        ("Consolidated Balance Sheets",), ("RMB in thousands",), ("Item", "2025", "2024"),
        ("Total shareholders’ equity", "100", "90"),
        ("Noncontrolling interests", "10", "9"),
        ("Total equity", "110", "99"),
        ("Total liabilities, mezzanine equity and equity", "230", "210"),
    ])])
    values = {(fact.concept, fact.fiscal_year): fact for fact in facts}
    assert values["equity", 2025].normalization_lineage.normalized_value == "100000"
    assert values["total_equity", 2025].normalization_lineage.normalized_value == "110000"
    parent = values["equity", 2025]
    assert parent.classification_evidence_ids
    row_ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(parent))
    result = FinancialValidator().validate_group(
        [parent], (parent.accession_number, parent.end_date, parent.fiscal_period,
                   parent.consolidated_scope, parent.currency),
        {parent.fact_id: row_ref}, required_concepts={"equity"}, source_evidence=refs,
    )
    assert not result.validation.quarantined
    forged = replace(parent, classification_evidence_ids=())
    result = FinancialValidator().validate_group(
        [forged], (parent.accession_number, parent.end_date, parent.fiscal_period,
                   parent.consolidated_scope, parent.currency),
        {parent.fact_id: row_ref}, required_concepts={"equity"}, source_evidence=refs,
    )
    assert result.validation.quarantined


def test_profit_before_income_tax_literal_label_and_loss_sign_are_verified():
    facts, refs = parse([Page([
        ("Consolidated Income Statements",), ("RMB in thousands",), ("Item", "2025", "2024"),
        ("Loss before income tax", "20", "10"),
    ])])
    fact = next(fact for fact in facts if fact.concept == "profit_before_tax" and fact.fiscal_year == 2025)
    assert fact.normalization_lineage.normalized_value == "-20000"
    row_ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    result = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period,
                 fact.consolidated_scope, fact.currency),
        {fact.fact_id: row_ref}, required_concepts={"profit_before_tax"}, source_evidence=refs,
    )
    assert not result.validation.quarantined


def test_later_statement_basis_declaration_binds_earlier_table_units():
    pages = [
        Page([("合并利润表",), ("项目", "2025", "2024"), ("净利润", "100", "90")]),
        Page([("本公司编制本财务报表所采用的货币为人民币，除有特别说明外，均以人民币千元为单位表示。",)]),
    ]
    facts, refs = parse(pages, selected={1})
    fact = next(fact for fact in facts if fact.concept == "net_income" and fact.fiscal_year == 2025)
    assert fact.normalization_lineage is not None
    assert fact.normalization_lineage.normalized_value == "100000"
    assert fact.unit_provenance == "document_explicit"
    units = [ref for ref in refs if ref.evidence_id in fact.normalization_lineage.unit_evidence_ids]
    assert units and all(ref.locator == "page:2" and ref.content_hash == "a" * 64 for ref in units)


def test_local_header_wins_and_narrative_amount_is_not_document_unit():
    statement = Page([("合并利润表",), ("单位：人民币元",), ("项目", "2025", "2024"),
                      ("净利润", "100", "90")])
    narrative = Page([("本公司现金分红总额为人民币千元，财务报表请参阅后文。",)])
    broad = Page([("本财务报表除另有说明外，所有金额均以人民币千元表示。",)])
    facts, _ = parse([statement, narrative, broad], selected={1})
    fact = next(fact for fact in facts if fact.concept == "net_income" and fact.fiscal_year == 2025)
    assert fact.normalization_lineage.normalized_value == "100"
    assert fact.unit_provenance == "explicit"
    without_header = Page([("合并利润表",), ("项目", "2025", "2024"), ("净利润", "100", "90")])
    facts, _ = parse([without_header, narrative], selected={1})
    assert all(fact.unit_provenance == "unknown" and fact.normalization_lineage is None for fact in facts)


def test_conflicting_document_basis_never_picks_a_convenient_multiplier():
    facts, _ = parse([
        Page([("合并利润表",), ("项目", "2025", "2024"), ("净利润", "100", "90")]),
        Page([("本财务报表除另有说明外，所有金额均以人民币千元表示。",)]),
        Page([("本财务报表除另有说明外，所有金额均以人民币百万元表示。",)]),
    ], selected={1})
    assert all(fact.unit_provenance == "unknown" and fact.normalization_lineage is None for fact in facts)


def test_conflicting_same_identity_rows_are_preserved_for_pure_compiler():
    facts, _ = parse([
        Page([("合并利润表",), ("单位：人民币千元",), ("项目", "2025", "2024"),
              ("净利润", "100", "90"), ("净利润", "200", "180")]),
    ])
    values = {fact.normalization_lineage.normalized_value for fact in facts
              if fact.concept == "net_income" and fact.fiscal_year == 2025}
    assert values == {"100000", "200000"}


def test_unambiguous_reported_loss_label_retains_explicit_sign_lineage():
    facts, _ = parse([Page([
        ("Consolidated income statement",), ("RMB thousands",), ("Item", "2025", "2024"),
        ("Net income", "100", "90"),
        ("Net loss attributable to non-controlling interests", "5", "(3)"),
    ])])
    minority = {fact.fiscal_year: fact for fact in facts if fact.concept == "minority_interest_profit"}
    assert minority[2025].value == -5000
    assert minority[2024].value == -3000
    assert minority[2025].normalization_lineage.raw_value == "5"
    assert minority[2025].normalization_lineage.value_transform == "negative_absolute_value"
    assert "Net loss attributable" in minority[2025].reported_concept


def test_mixed_profit_loss_heading_does_not_invert_positive_cells():
    facts, _ = parse([Page([
        ("Consolidated income statement",), ("RMB thousands",), ("Item", "2025", "2024"),
        ("(Loss)/profit for the year", "100", "(90)"),
    ])])
    values = {fact.fiscal_year: fact.value for fact in facts if fact.concept == "net_income"}
    assert values == {2025: 100000, 2024: -90000}


def test_loss_sign_is_independently_reverified_against_bound_source_row():
    facts, refs = parse([Page([
        ("Consolidated income statement",), ("RMB thousands",), ("Item", "2025", "2024"),
        ("Net loss attributable to non-controlling interests", "5", "3"),
    ])])
    fact = next(fact for fact in facts if fact.fiscal_year == 2025)
    row_ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    identity = (fact.accession_number, fact.end_date, fact.fiscal_period, fact.scope, fact.currency)
    validator = FinancialValidator()

    def validate(candidate, source=row_ref):
        return validator.validate_group(
            [candidate], identity, {candidate.fact_id: source}, required_concepts={candidate.concept},
            expected_company_id=fact.company_cik, source_evidence=refs,
        ).validation

    assert validate(fact).accepted == (fact,)
    # A transform declaration cannot establish a loss on a different row.
    assert not validate(replace(fact, reported_concept="Net income attributable to non-controlling interests")).accepted
    assert not validate(fact, replace(row_ref, excerpt="Net income 5 3")).accepted


def test_source_decimal_not_present_in_pdf_row_is_rejected_even_when_arithmetic_matches():
    facts, refs = parse([Page([
        ("Consolidated income statement",), ("RMB thousands",), ("Item", "2025", "2024"),
        ("Net income", "100", "90"),
    ])])
    fact = next(fact for fact in facts if fact.fiscal_year == 2025)
    row_ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    forged = replace(fact, value=120000, normalization_lineage=replace(
        fact.normalization_lineage, raw_value="120", normalized_value="120000",
    ))
    result = FinancialValidator().validate_group(
        [forged], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.scope, fact.currency),
        {fact.fact_id: row_ref}, required_concepts={fact.concept}, source_evidence=refs,
    ).validation
    assert not result.accepted
    assert "normalization_raw_value_not_in_source_row" in result.issues


def test_interest_revenue_detail_is_not_a_second_company_total():
    facts, refs = parse([Page([
        ("Consolidated income statement",), ("RMB thousands",), ("Item", "2025", "2024"),
        ("Revenues", "364,854,746", "337,591,576"),
        ("Including: Interest revenue", "1,746,150", "1,964,341"),
    ])])
    totals = [fact for fact in facts if fact.concept == "revenue" and fact.fiscal_year == 2025]
    assert [fact.value for fact in totals] == [364854746000]
    detail = next(fact for fact in facts if fact.concept == "interest_revenue" and fact.fiscal_year == 2025)
    assert detail.value == 1746150000
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(detail))
    validation = FinancialValidator().validate_group(
        [detail], (detail.accession_number, detail.end_date, detail.fiscal_period, detail.scope, detail.currency),
        {detail.fact_id: row}, required_concepts={"interest_revenue"}, source_evidence=refs,
    ).validation
    assert validation.accepted == (detail,)
    assert "interest_source_hierarchy_mismatch" not in validation.issues


def test_reconciliation_adjustment_columns_are_not_company_period_values():
    facts, _ = parse([
        Page([("Consolidated Income Statements",), ("RMB in millions",),
              ("Item", "2025", "2024"), ("Net income", "100", "90")]),
        Page([("Notes to Consolidated Financial Statements",),
              ("Reconciliation of Consolidated Income Statement (Extract)",),
              ("Year ended March 31, 2025",),
              ("As reported under U.S. GAAP", "2025", "Amounts under IFRS"),
              ("Net income", "(3)", "105")]),
    ])
    # The single date applies to the reconciliation as a whole, not to an
    # adjustment column. It cannot masquerade as another annual statement.
    current = [fact for fact in facts if fact.concept == "net_income" and fact.fiscal_year == 2025]
    assert [fact.value for fact in current] == [100000000]


def test_mezzanine_equity_row_is_distinct_from_combined_liability_equity_total():
    facts, refs = parse([Page([
        ("Consolidated Balance Sheets",), ("RMB in millions",), ("Item", "2025", "2024"),
        ("Mezzanine equity", "8", "12"),
        ("Total liabilities, mezzanine equity and equity", "1909", "1804"),
    ])])
    mezzanine = [fact for fact in facts if fact.concept == "mezzanine_equity" and fact.fiscal_year == 2025]
    assert [fact.value for fact in mezzanine] == [8000000]
    fact = mezzanine[0]
    row_ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    forged_ref = replace(row_ref, excerpt="Total liabilities, mezzanine equity and equity 8 12")
    forged = replace(fact, raw_text=forged_ref.excerpt,
                     reported_concept="Total liabilities, mezzanine equity and equity")
    result = FinancialValidator().validate_group(
        [forged], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.scope, fact.currency),
        {fact.fact_id: forged_ref}, required_concepts={fact.concept}, source_evidence=refs,
    ).validation
    assert not result.accepted
    assert "balance_component_source_concept_mismatch" in result.issues


def test_cash_flow_reconciliation_heading_cannot_supply_a_cash_flow_amount():
    facts, refs = parse([Page([
        ("Consolidated Statements of Cash Flows",), ("RMB in millions",), ("Item", "2025", "2024"),
        ("Adjustments to reconcile net income to net cash provided by",),
        ("operating activities:",),
        ("Revaluation gain on previously held equity interest", "(628)", "(500)"),
        ("Net cash provided by operating activities", "163509", "182593"),
    ], line_gap=12)])
    flows = [fact for fact in facts if fact.concept == "operating_cash_flow" and fact.fiscal_year == 2025]
    assert [fact.value for fact in flows] == [163509000000]
    fact = flows[0]
    ref = next(item for item in refs if item.evidence_id in fact_row_evidence_ids(fact))
    heading = "Adjustments to reconcile net income to net cash provided by operating activities"
    forged = replace(fact, reported_concept=heading, raw_text=heading + " 163509 182593")
    result = FinancialValidator().validate_group(
        [forged], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.scope, fact.currency),
        {fact.fact_id: replace(ref, excerpt=forged.raw_text)}, required_concepts={fact.concept}, source_evidence=refs,
    ).validation
    assert not result.accepted
    assert "cash_flow_source_concept_mismatch" in result.issues


def test_liability_equity_combined_total_is_not_a_liability_fact():
    facts, _ = parse([Page([
        ("Consolidated Balance Sheets",), ("RMB in millions",), ("Item", "2025", "2024"),
        ("Total liabilities", "783", "714"),
        ("Total liabilities, mezzanine equity and equity", "1909", "1804"),
    ])])
    liabilities = [fact for fact in facts if fact.concept == "liabilities" and fact.fiscal_year == 2025]
    assert [fact.value for fact in liabilities] == [783000000]


def test_small_font_baseline_variations_do_not_reorder_financial_row_words():
    class JitterPage(Page):
        def extract_words(self, **kwargs):
            words = super().extract_words(**kwargs)
            result = []
            for word in words:
                if word["text"] != "Total shareholders' equity":
                    result.append(word)
                    continue
                for token, x, drift in (("Total", 50, .02), ("shareholders'", 85, -.02), ("equity", 170, .03)):
                    result.append({**word, "text": token, "x0": x, "x1": x + len(token) * 6,
                                   "top": word["top"] + drift, "bottom": word["bottom"] + drift})
            return result

    facts, refs = parse([JitterPage([
        ("Consolidated Balance Sheets",), ("RMB in millions",), ("Item", "2025", "2024"),
        ("Total shareholders' equity", "948479", "937470"),
        ("Noncontrolling interests", "124059", "137491"),
        ("Total equity", "1072538", "1074961"),
    ])])
    parent = next(fact for fact in facts if fact.concept == "equity" and fact.fiscal_year == 2025)
    assert parent.reported_concept == "Total shareholders' equity"
    assert parent.raw_text.startswith("Total shareholders' equity ")
    row_ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(parent))
    result = FinancialValidator().validate_group(
        [parent], (parent.accession_number, parent.end_date, parent.fiscal_period, parent.scope, parent.currency),
        {parent.fact_id: row_ref}, required_concepts={parent.concept}, source_evidence=refs,
    ).validation
    assert result.accepted == (parent,)


def test_equity_section_heading_with_unmapped_glyphs_is_not_part_of_numeric_row():
    facts, refs = parse([Page([
        ("Consolidated Balance Sheets",), ("RMB in millions",), ("Item", "2025", "2024"),
        ("Mezzanine equity", "11713", "10728"),
        ("Shareholders\ufffd\ufffd equity:",),
        ("Additional paid-in capital", "381379", "397999"),
    ], line_gap=11)])
    mezzanine = [fact for fact in facts if fact.concept == "mezzanine_equity" and fact.fiscal_year == 2025]
    assert len(mezzanine) == 1
    fact = mezzanine[0]
    assert fact.reported_concept == "Mezzanine equity"
    assert fact.normalization_lineage.normalized_value == "11713000000"
    ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    result = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.scope, fact.currency),
        {fact.fact_id: ref}, required_concepts={fact.concept}, source_evidence=refs,
    ).validation
    assert result.accepted == (fact,)


def test_pdf_fact_cannot_use_structured_arithmetic_to_bypass_source_amount_proof():
    facts, refs = parse([Page([
        ("Consolidated Income Statements",), ("RMB thousands",), ("Item", "2025", "2024"),
        ("Net income", "100", "90"),
    ])])
    fact = next(fact for fact in facts if fact.fiscal_year == 2025)
    ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    # Arithmetic is internally consistent but one operand was invented. The
    # PDF row only contains 100 and 90, not a derived annual net income of 120.
    child = replace(fact.normalization_lineage, raw_value="120", normalized_value="120000")
    lineage = FinancialNormalizationLineage(
        "", "1000", "120000", "money", fact.currency, child.unit_evidence_ids,
        method="sec-xbrl-arithmetic-v1", value_transform="sum", inputs=(child,),
    )
    forged = replace(fact, value=120000, normalization_lineage=lineage)
    result = FinancialValidator().validate_group(
        [forged], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.scope, fact.currency),
        {fact.fact_id: ref}, required_concepts={fact.concept}, source_evidence=refs,
    ).validation
    assert not result.accepted
    assert "normalization_lineage_invalid" in result.issues


def test_pdf_capex_aggregate_rechecks_each_source_operand_not_only_total():
    facts, refs = parse([Page([
        ("Consolidated Statements of Cash Flows",), ("RMB thousands",), ("Item", "2025", "2024"),
        ("Purchase of property, plant and equipment", "(100)", "(90)"),
        ("Purchase of intangible assets", "(20)", "(15)"),
    ])])
    fact = next(fact for fact in facts if fact.concept == "capital_expenditure" and fact.fiscal_year == 2025)
    assert len(fact.normalization_lineage.inputs) == 2
    ref = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))

    def validate(candidate):
        return FinancialValidator().validate_group(
            [candidate], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.scope, fact.currency),
            {candidate.fact_id: ref}, required_concepts={fact.concept}, source_evidence=refs,
        ).validation

    assert validate(fact).accepted == (fact,)
    left, right = fact.normalization_lineage.inputs
    forged = replace(fact, value=125000, normalization_lineage=replace(
        fact.normalization_lineage, normalized_value="125000", inputs=(
            left, replace(right, raw_value="(25)", normalized_value="25000"),
        ),
    ))
    assert not validate(forged).accepted
    assert "normalization_raw_value_not_in_source_row" in validate(forged).issues
