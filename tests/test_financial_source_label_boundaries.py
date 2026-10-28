from __future__ import annotations

import pytest

from openthesis.financial_ingestion import (
    PdfCellAST, PdfRowAST, _append_allocation_heading_proof,
    _attribution_context, _merge_visual_rows,
    _ownership_classification_heading_row,
)
from openthesis.financial_taxonomy import (
    comprehensive_income_row_concept,
    financial_label_body,
    profit_loss_value_transform,
    profit_row_concept,
    total_revenue_label_matches,
)


@pytest.mark.parametrize("label", ("IV. Net profit", "V.Net profit"))
def test_roman_numbered_net_profit_labels_are_total_net_income(label: str) -> None:
    assert profit_row_concept(label) == "net_income"


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("(II) Classified by ownership", "Classified by ownership"),
        ("（2）净利润", "净利润"),
        ("(3) Net profit", "Net profit"),
        ("IV. Net profit", "Net profit"),
    ),
)
def test_financial_label_body_strips_only_anchored_statement_ordinals(
    raw: str, expected: str,
) -> None:
    assert financial_label_body(raw) == expected


def test_bare_profit_allocations_need_exact_ownership_heading() -> None:
    assert profit_row_concept("Owners of the company") is None
    assert profit_row_concept("Non-controlling interests") is None
    assert profit_row_concept(
        "Owners of the company", "(II) Classified by ownership"
    ) == "parent_net_income"
    assert profit_row_concept(
        "Non-controlling interests", "(II) Classified by ownership"
    ) == "minority_interest_profit"
    assert profit_row_concept(
        "Non-controlling interests", "The company was classified by ownership in 2026"
    ) is None
    assert profit_row_concept(
        "Non-controlling interests", "Classified by function"
    ) is None
    assert profit_row_concept(
        "Non-controlling interests", "Total comprehensive income attributable to:"
    ) is None
    assert profit_row_concept(
        "Non-controlling interests", "Earnings per share"
    ) is None
    assert profit_row_concept(
        "EPS: Non-controlling interests", "Classified by ownership"
    ) is None
    assert profit_row_concept(
        "Profit attributable to non-controlling interests"
    ) == "minority_interest_profit"


def test_full_literal_ownership_row_proof_matches_only_its_numbered_leaf() -> None:
    proof = (
        "(II) Classified by ownership 2. Non-controlling interests "
        "-73,620,000.00 288,155,000.00"
    )
    assert profit_row_concept("2. Non-controlling interests", proof) == "minority_interest_profit"
    parser_reported_label = "(II) Classified by ownership 2. Non-controlling interests"
    assert profit_row_concept(parser_reported_label, proof) == "minority_interest_profit"
    actual_proof = (
        "(II) Classified by ownership shareholders of the parent company "
        "2. Non-controlling interests -73,620,000.00 288,155,000.00"
    )
    actual_reported_label = (
        "(II) Classified by ownership shareholders of the parent company "
        "2. Non-controlling interests"
    )
    assert profit_row_concept(actual_reported_label, actual_proof) is None
    assert profit_row_concept(
        "Non-controlling interests",
        "(II) Classified by ownership 1. Net profit attributable to shareholders of the parent company 4,084,551,000.00",
    ) is None
    assert profit_row_concept(
        "2. Non-controlling interests",
        "Disclosure says the company was classified by ownership: " + proof,
    ) is None
    assert profit_row_concept(
        "2. Non-controlling interests",
        "(II) Classified by ownership 2. Non-controlling interests",
    ) is None
    assert profit_row_concept(
        "2. Non-controlling interests",
        "(II) Classified by ownership EPS 2. Non-controlling interests -73,620,000.00",
    ) is None
    assert profit_row_concept(
        "2. Non-controlling interests",
        "(II) Classified by ownership Comprehensive income 2. Non-controlling interests -73,620,000.00",
    ) is None


def _validate_by_q1_nci_literal(
    raw_text: str,
    reported_concept: str = "(II) Classified by ownership 2. Non-controlling interests",
):
    from openthesis.domain import EvidenceRef, FinancialFact
    from openthesis.financial_contracts import PDF_PARSER_VERSION, QuantityKind, SourceAmount
    from openthesis.financial_statement_identity import statement_title_evidence_id
    from openthesis.financial_validation import FinancialValidator

    document_id = "hkex:01211:byd-2026-q1"
    source_url = "https://www.hkexnews.hk/filings/byd-2026-q1.pdf"
    content_hash = "a" * 64
    filed_at = "2026-04-28T00:00:00Z"
    row_bbox = (30.0, 100.0, 520.0, 118.0)
    title_bbox = (30.0, 20.0, 520.0, 38.0)
    title_text = "Consolidated income statement"
    title_id = statement_title_evidence_id(document_id, 13, title_bbox, title_text)
    unit_id = "unit:byd-q1:13"
    fact = FinancialFact(
        fact_id="byd-q1-nci", company_cik="BYD:01211",
        concept="minority_interest_profit",
        reported_concept=reported_concept,
        value=-73_620_000.0, unit="CNY", fiscal_year=2026, fiscal_period="Q1",
        form_type="INTERIM", start_date="2026-01-01", end_date="2026-03-31",
        filed_at=filed_at, accession_number="2026042803001", source_url=source_url,
        scope="consolidated", entity="BYD Company Limited", market="HK",
        statement="income_statement", period_start="2026-01-01",
        consolidated_scope="consolidated", currency="CNY", unit_scale=1.0,
        unit_provenance="explicit", source_document=document_id,
        source_page=13, source_bbox=row_bbox, raw_text=raw_text,
        parser_version=PDF_PARSER_VERSION,
        normalization_lineage=SourceAmount(
            "-73,620,000.00", "1", QuantityKind.MONEY, "CNY", (unit_id,),
        ).normalize(),
        classification_evidence_ids=(title_id,),
    )
    row_ref = EvidenceRef(
        "row:byd-q1-nci", document_id, source_url, "Profit allocation row",
        "page:13", raw_text, filed_at, content_hash, row_bbox,
    )
    title_ref = EvidenceRef(
        title_id, document_id, source_url, title_text, "page:13", title_text,
        filed_at, content_hash, title_bbox,
    )
    unit_ref = EvidenceRef(
        unit_id, document_id, source_url, "Displayed currency unit", "page:13",
        "单位：人民币元", filed_at, content_hash, (30.0, 80.0, 150.0, 90.0),
    )
    result = FinancialValidator().validate_group(
        [fact],
        (fact.company_cik, fact.end_date, fact.fiscal_period, "consolidated", "CNY"),
        evidence_map={fact.fact_id: row_ref},
        required_concepts={"minority_interest_profit"},
        source_evidence=(title_ref, unit_ref),
    )
    return fact, result


def test_financial_validator_admits_only_full_ownership_row_proof() -> None:
    valid_literal = (
        "(II) Classified by ownership 2. Non-controlling interests "
        "-73,620,000.00 288,155,000.00"
    )
    fact, valid = _validate_by_q1_nci_literal(valid_literal)
    assert fact in valid.validation.accepted
    assert "profit_source_concept_mismatch" not in valid.validation.issues

    actual_proof = (
        "(II) Classified by ownership shareholders of the parent company "
        "2. Non-controlling interests -73,620,000.00 288,155,000.00"
    )
    actual_reported_label = (
        "(II) Classified by ownership shareholders of the parent company "
        "2. Non-controlling interests"
    )
    actual_fact, actual_valid = _validate_by_q1_nci_literal(
        actual_proof, actual_reported_label,
    )
    assert actual_fact not in actual_valid.validation.accepted
    assert "profit_source_concept_mismatch" in actual_valid.validation.issues

    narrative_prefix = "The report says: " + actual_proof
    rejected_fact, rejected = _validate_by_q1_nci_literal(
        narrative_prefix, actual_reported_label,
    )
    assert rejected_fact not in rejected.validation.accepted
    assert "profit_source_concept_mismatch" in rejected.validation.issues


def test_attribution_context_finds_ownership_heading_across_parent_rows() -> None:
    heading_text = "(II) Classified by ownership"
    rows = (
        _row(10, ((heading_text, 30, 280),)),
        _row(19, (("1. Owners of the company", 44, 250),)),
        _row(28, (("parent company", 60, 180),)),
        _row(37, (("Owners' share of profit", 60, 250), ("4,084,551,000", 350, 440))),
        _row(46, (("(2) Non-controlling interests", 74, 295), ("-73,620,000", 350, 440))),
    )

    context = _attribution_context(rows, 4)

    assert context == "(II)Classifiedbyownership"
    assert profit_row_concept("Owners of the company", context) == "parent_net_income"
    assert profit_row_concept("Non-controlling interests", context) == "minority_interest_profit"


def test_attribution_context_allows_actual_multiline_parent_row_before_nci() -> None:
    heading_text = "(II) Classified by ownership"
    rows = (
        _row(10, ((heading_text, 30, 280),)),
        _row(19, (("1. Net profit attributable to", 44, 270),)),
        _row(28, (("shareholders of the parent", 60, 255),)),
        _row(37, (("company", 76, 160),)),
        _row(46, (("2. Non-controlling interests", 44, 280), ("-73,620,000.00", 350, 440))),
    )

    assert _attribution_context(rows, 4) == "(II)Classifiedbyownership"


def test_visual_merge_does_not_absorb_next_numbered_ownership_allocation_leaf() -> None:
    heading = _row(10, (("(II) Classified by ownership", 30, 280),))
    parent_start = _row(19, (("1. Net profit attributable to", 44, 270),))
    parent_tail = _row(28, (("shareholders of the parent company", 44, 300),))
    nci = _row(
        37,
        (("2. Non-controlling interests", 44, 280),
         ("-73,620,000.00", 350, 440), ("288,155,000.00", 450, 540)),
    )
    rows = (heading, parent_start, parent_tail, nci)

    parent = _merge_visual_rows(rows, 1)
    assert parent.cells == parent_start.cells + parent_tail.cells
    assert _merge_visual_rows(rows, 3).cells == nci.cells
    context = _attribution_context(rows, 3)
    proof = _append_allocation_heading_proof(rows, 3, nci, context)
    assert proof.cells == (heading.cells[0], *nci.cells)


def test_attribution_context_allows_amount_bearing_canonical_parent_profit_row() -> None:
    heading = _row(10, (("(II) Classified by ownership", 30, 280),))
    parent_label = "1. Net profit attributable to shareholders of the parent company"
    parent = _row(19, ((parent_label, 44, 320), ("4,084,551,000.00", 350, 450)))
    nci = _row(28, (("2. Non-controlling interests", 44, 280), ("-73,620,000.00", 350, 440)))

    assert profit_row_concept(parent_label) == "parent_net_income"
    assert _attribution_context((heading, parent, nci), 2) == "(II)Classifiedbyownership"


def test_valueless_attribution_heading_stops_ownership_search_but_remains_explicit_context() -> None:
    ownership = _row(10, (("(II) Classified by ownership", 30, 280),))
    attribution = _row(19, (("Attributable to:", 44, 180),))
    nci = _row(28, (("Non-controlling interests", 60, 260),))

    assert _ownership_classification_heading_row((ownership, attribution, nci), 2) is None
    assert profit_row_concept("Non-controlling interests", "Attributable to:") == "minority_interest_profit"


def test_ownership_heading_proof_keeps_literal_cells_without_parent_amounts() -> None:
    heading = _row(10, (("(II) Classified by ownership", 30, 280),))
    first_parent_line = _row(19, (("1. Owners of the company", 44, 250),))
    second_parent_line = _row(28, (("parent company", 60, 180),))
    parent_amount = _row(
        37, (("Owners' share of profit", 60, 250), ("4,084,551,000", 350, 440))
    )
    nci = _row(
        46, (("(2) Non-controlling interests", 74, 295), ("-73,620,000", 350, 440))
    )
    rows = (heading, first_parent_line, second_parent_line, parent_amount, nci)
    context = _attribution_context(rows, 4)

    proof = _append_allocation_heading_proof(rows, 4, nci, context)

    assert proof.cells == (heading.cells[0], *nci.cells)
    assert proof.cells[0].text == "(II) Classified by ownership"
    assert proof.cells[-1] == nci.cells[-1]
    assert (proof.cells[-1].x0, proof.cells[-1].x1) == (350, 440)
    assert "4,084,551,000" not in {cell.text for cell in proof.cells}


@pytest.mark.parametrize(
    "boundary",
    (
        "Total comprehensive income attributable to:",
        "Earnings per share",
        "EPS",
        "(III) Classified by function",
        "Consolidated balance sheet",
    ),
)
def test_attribution_context_does_not_cross_allocation_or_statement_boundaries(
    boundary: str,
) -> None:
    ownership = _row(10, (("(II) Classified by ownership", 30, 280),))
    blocker = _row(19, ((boundary, 40, 300),))
    nci = _row(28, (("Non-controlling interests", 60, 260), ("-73,620,000", 350, 440)))

    context = _attribution_context((ownership, blocker, nci), 2)

    assert context != "(II)Classifiedbyownership"
    assert profit_row_concept("Non-controlling interests", context) is None


def test_attribution_context_does_not_cross_tables_or_more_than_eight_rows() -> None:
    ownership = _row(10, (("(II) Classified by ownership", 30, 280),))
    other_table = (_row(10, (("Non-controlling interests", 60, 260),)),)
    assert _attribution_context(other_table, 0) == ""

    within_eight = [ownership]
    within_eight.extend(
        _row(19 + index * 9, ((f"parent detail {index}", 45, 220),))
        for index in range(7)
    )
    within_eight.append(_row(100, (("Non-controlling interests", 60, 260),)))
    assert _attribution_context(
        tuple(within_eight), len(within_eight) - 1,
    ) == "(II)Classifiedbyownership"

    outside_eight = [ownership]
    outside_eight.extend(
        _row(19 + index * 9, ((f"parent detail {index}", 45, 220),))
        for index in range(8)
    )
    outside_eight.append(_row(
        110, (("Non-controlling interests", 60, 260), ("-73,620,000", 350, 440)),
    ))
    assert _attribution_context(
        tuple(outside_eight), len(outside_eight) - 1,
    ) == ""


@pytest.mark.parametrize(
    "disclosure",
    (
        '(net loss is represented by “-”)',
        '(净亏损以“-”号填列)',
    ),
)
def test_loss_sign_disclosure_alone_is_not_a_profit_row_or_sign_source(
    disclosure: str,
) -> None:
    assert profit_row_concept(disclosure) is None
    assert profit_loss_value_transform(disclosure, "minority_interest_profit") == "identity"


def test_mixed_net_profit_loss_convention_keeps_sign_but_net_loss_is_negative() -> None:
    mixed_heading = 'V.Net profit (net loss is represented by “-”)'

    assert profit_row_concept(mixed_heading) == "net_income"
    assert profit_loss_value_transform(mixed_heading, "net_income") == "identity"
    assert profit_loss_value_transform("Net loss", "net_income") == "negative_absolute_value"


def test_only_statutory_operating_revenue_breakdown_matches_total_revenue() -> None:
    aliases = ("operating revenue", "revenue")

    assert total_revenue_label_matches("Including: Operating revenue", aliases)
    assert not total_revenue_label_matches("Including: cloud revenue", aliases)
    assert not total_revenue_label_matches("cloud operating revenue", aliases)


def test_comprehensive_income_allocations_remain_separate_from_net_income() -> None:
    explicit_heading = "Total comprehensive income attributable to:"

    assert comprehensive_income_row_concept("Total comprehensive income") == "comprehensive_income"
    assert comprehensive_income_row_concept(
        "Owners of the company", explicit_heading
    ) == "parent_comprehensive_income"
    assert comprehensive_income_row_concept(
        "Non-controlling interests", explicit_heading
    ) == "minority_interest_comprehensive_income"

    for allocation in ("Owners of the company", "Non-controlling interests"):
        assert profit_row_concept(allocation, explicit_heading) is None


def _row(top: float, entries: tuple[tuple[str, float, float], ...]) -> PdfRowAST:
    cells = tuple(
        PdfCellAST(text, x0, top, x1, top + 8)
        for text, x0, x1 in entries
    )
    return PdfRowAST(
        cells,
        top,
        (
            min(cell.x0 for cell in cells),
            top,
            max(cell.x1 for cell in cells),
            top + 8,
        ),
    )


def test_visual_merge_keeps_parenthetical_profit_convention_with_heading_only() -> None:
    heading = _row(10, (("Net profit", 50, 170),))
    convention_and_values = _row(
        19,
        (
            ('(net loss is represented by “-”)', 50, 285),
            ("1,234", 350, 390),
            ("1,111", 450, 490),
        ),
    )
    parent_allocation = _row(
        28,
        (
            ("Profit attributable to owners", 50, 280),
            ("900", 350, 390),
            ("800", 450, 490),
        ),
    )
    rows = (heading, convention_and_values, parent_allocation)

    merged_heading = _merge_visual_rows(rows, 0)
    assert merged_heading.cells == heading.cells + convention_and_values.cells
    assert all(cell not in merged_heading.cells for cell in parent_allocation.cells)
    assert _merge_visual_rows(rows, 1).cells == convention_and_values.cells
