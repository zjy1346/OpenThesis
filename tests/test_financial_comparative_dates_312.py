"""Comparative statement columns keep their own point/range period identity."""

import sys
from types import SimpleNamespace
from unittest.mock import patch

from openthesis.financial_ingestion import (
    FilingManifest,
    PdfCellAST,
    PdfPageSection,
    PdfRowAST,
    PdfTableContext,
    FinancialIngestionEngine,
    _checkpoint_context_from_dict,
    _checkpoint_context_to_dict,
    _bind_comparison_period,
    _period_columns,
)
from openthesis.domain import Company, FilingDocument
from openthesis.financial_sources.document_units import DocumentUnitResolution


def _row(cells, top=10):
    values = tuple(PdfCellAST(text, left, top, right, top + 10) for text, left, right in cells)
    return PdfRowAST(
        values,
        top,
        (min(cell.x0 for cell in values), top, max(cell.x1 for cell in values), top + 10),
    )


def _manifest(period="H1", end="2026-06-30"):
    return FilingManifest(
        "doc", "acc", "https://example.test/report.pdf", "report.pdf", "INTERIM_REPORT",
        period, end, "original", "", "source-hash",
    )


def test_balance_sheet_comparator_uses_explicit_header_date_not_manifest_year_shift():
    header = _row([
        ("项目", 10, 60),
        ("2026年6月30日", 250, 360),
        ("2025年12月31日", 430, 550),
    ])
    current, comparator = _period_columns((header,))

    binding = _bind_comparison_period(comparator, "balance_sheet", _manifest())

    assert current.period_end == "2026-06-30"
    assert comparator.period_end == "2025-12-31"
    assert binding.period_end == "2025-12-31"
    assert binding.period_start is None
    assert binding.fiscal_period == "FY"
    assert binding.basis == "explicit_point_in_time_header"


def test_income_statement_comparator_preserves_the_interim_interval():
    header = _row([
        ("项目", 10, 60),
        ("2026年1-6月", 250, 360),
        ("2025年1-6月", 430, 540),
    ])
    _current, comparator = _period_columns((header,))

    binding = _bind_comparison_period(comparator, "income_statement", _manifest())

    assert binding.period_start == "2025-01-01"
    assert binding.period_end == "2025-06-30"
    assert binding.fiscal_period == "H1"
    assert binding.basis == "explicit_header_interval"


def test_explicit_april_to_june_interval_does_not_inherit_manifest_h1():
    header = _row([
        ("项目", 10, 60),
        ("2026年1-6月", 250, 360),
        ("2025年4-6月", 430, 540),
    ])
    _current, comparator = _period_columns((header,))

    binding = _bind_comparison_period(comparator, "income_statement", _manifest())

    assert binding.period_start == "2025-04-01"
    assert binding.period_end == "2025-06-30"
    assert binding.fiscal_period == "INTERVAL"
    assert binding.basis == "explicit_header_interval"


def test_explicit_july_to_december_interval_does_not_inherit_manifest_h1():
    header = _row([
        ("项目", 10, 60),
        ("2026年1-6月", 250, 360),
        ("2025年7-12月", 430, 550),
    ])
    _current, comparator = _period_columns((header,))

    binding = _bind_comparison_period(comparator, "cash_flow", _manifest())

    assert binding.period_start == "2025-07-01"
    assert binding.period_end == "2025-12-31"
    assert binding.fiscal_period == "INTERVAL"
    assert binding.basis == "explicit_header_interval"


def test_ambiguous_balance_sheet_year_retains_an_unresolved_period():
    header = _row([
        ("项目", 10, 60),
        ("2026年6月30日", 250, 360),
        ("2025", 430, 500),
    ])
    _current, comparator = _period_columns((header,))

    binding = _bind_comparison_period(comparator, "balance_sheet", _manifest())

    assert comparator.year == 2025
    assert binding.resolved is False
    assert binding.fiscal_period == ""
    assert binding.period_start is None
    assert binding.period_end == ""
    assert binding.basis == "unresolved_ambiguous_point_in_time_header"


def test_unresolved_comparison_number_is_retained_without_an_invented_period():
    rows = (
        _row([("Item", 10, 100), ("2026年6月30日", 250, 360), ("2025", 430, 520)]),
        _row([("Total assets", 10, 150), ("1,000", 250, 360), ("900", 430, 520)], 30),
    )
    columns = _period_columns(rows, preferred_currency="CNY")
    context = PdfTableContext(
        "balance_sheet", "consolidated", 1.0, "CNY", True, columns, 1,
        unit_provenance="explicit", unit_evidence="Unit: CNY", unit_evidence_page=1,
    )
    section = PdfPageSection(context, rows)

    class Page:
        def extract_words(self, **_kwargs):
            return [{"text": "fixture"}]

        def extract_text(self):
            return "Consolidated Balance Sheet\nUnit: CNY"

    class Pdf:
        pages = (Page(),)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    company = Company("CN_A:SZSE:000001.SZ", "000001.SZ", "Fixture", "SZSE", "CN:FIXTURE",
                      "CN_A", "CN_A:SZSE:000001.SZ", "CNY", "CNY", "CAS")
    filing = FilingDocument(
        "doc", company.security_id, "acc", "INTERIM_REPORT", "H1", "2026-06-30",
        "2026-08-15", "2026 H1", "https://example.test/report.pdf", content_hash="source-hash",
    )
    manifest = _manifest()
    with patch.dict(sys.modules, {"pdfplumber": SimpleNamespace(open=lambda _path: Pdf())}), \
         patch("openthesis.financial_ingestion._positioned_pdf_rows", return_value=rows), \
         patch("openthesis.financial_ingestion._page_sections", return_value=(section,)), \
         patch("openthesis.financial_ingestion.extract_disclosure_candidates", return_value=([], [])), \
         patch("openthesis.financial_ingestion.extract_note_candidates", return_value=([], [])):
        facts, refs = FinancialIngestionEngine().extract_pdf_candidates(
            "missing-fixture.pdf", company, filing, manifest, candidate_pages=frozenset({1}),
        )

    comparator = next(fact for fact in facts if fact.usage_status == "comparator")
    assert comparator.concept == "assets"
    assert comparator.value == 900
    assert comparator.fiscal_year == 2025
    assert comparator.fiscal_period == ""
    assert comparator.end_date == ""
    assert comparator.source_column == "2025"
    comparator_ref = next(
        ref for ref in refs
        if ref.evidence_id == f"fact:{comparator.fact_id.removeprefix('ingest:')}"
    )
    assert "900" in comparator_ref.excerpt
    assert "period basis:unresolved_ambiguous_point_in_time_header" in comparator_ref.title


def test_checkpoint_context_round_trip_keeps_period_header_provenance():
    header = _row([("Item", 10, 60), ("2026-06-30", 250, 360), ("2025-12-31", 430, 550)])
    columns = _period_columns((header,))
    context = PdfTableContext("balance_sheet", "consolidated", 1, "CNY", True, columns, 1)

    restored = _checkpoint_context_from_dict(_checkpoint_context_to_dict(context))

    assert restored is not None
    assert [(item.period_end, item.period_basis) for item in restored.periods] == [
        ("2026-06-30", "explicit_header_end_date"),
        ("2025-12-31", "explicit_header_end_date"),
    ]
