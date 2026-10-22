from __future__ import annotations

import unittest
import json
import hashlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import Company, FilingDocument, FinancialFact
from openthesis.financial_ingestion import (
    FinancialIngestionEngine,
    OfficialSource,
    ValidationStatus,
    _is_capex_cash_outflow_label,
    _manifest_for,
    _merge_capex_facts,
    _capital_return_label,
    _candidate_financial_pages_with_diagnostic,
    _page_sections,
    _pdf_page_count,
    _pdfium_candidate_pages_worker,
    _pdfium_page_count_worker,
    PdfCellAST,
    PdfRowAST,
    PdfTableContext,
    _PeriodColumn,
    _merge_visual_rows,
    parse_financial_pages,
    parse_structured_snapshot,
)
from openthesis.market_financials import _period_start
from openthesis.markets import build_company
from openthesis.financial_admission import FinancialAdmission
from openthesis.financial_compiler import FinancialFactCompiler as CanonicalFinancialFactCompiler
from openthesis.financial_contracts import (CandidateBatch, FactCandidate)
from openthesis.financial_pipeline import (CollectionPolicy as CompilerPolicy, FinancialCandidatePipeline as FinancialFactCompiler)
from openthesis.financial_sources import (InMemoryFactExtractor)


class FinancialIngestionV2Tests(unittest.TestCase):
    def test_complete_following_profit_label_never_inherits_nonoperating_expense_value(self) -> None:
        expense = PdfRowAST(
            (
                PdfCellAST("营业外支出", 50, 20, 110, 28),
                PdfCellAST("128,635,598.86", 400, 20, 480, 28),
            ),
            20,
            (50, 20, 480, 28),
        )
        profit_before_tax = PdfRowAST(
            (PdfCellAST("利润总额", 50, 29, 100, 37),),
            29,
            (50, 29, 100, 37),
        )

        candidate = _merge_visual_rows((expense, profit_before_tax), 0)

        self.assertEqual(
            tuple(cell.text for cell in candidate.cells),
            ("营业外支出", "128,635,598.86"),
            "a complete new financial label must not be joined to the prior row's number",
        )

    def test_wrapped_metric_label_still_merges_its_trailing_fragment(self) -> None:
        first_line = PdfRowAST(
            (
                PdfCellAST("加权平均净资产收益", 50, 20, 190, 28),
                PdfCellAST("10.2%", 400, 20, 440, 28),
            ),
            20,
            (50, 20, 440, 28),
        )
        wrapped_tail = PdfRowAST(
            (PdfCellAST("率", 50, 29, 58, 37),),
            29,
            (50, 29, 58, 37),
        )

        candidate = _merge_visual_rows((first_line, wrapped_tail), 0)

        self.assertEqual(
            tuple(cell.text for cell in candidate.cells),
            ("加权平均净资产收益", "10.2%", "率"),
            "an incomplete wrapped label tail must stay with its row's value",
        )

    def test_moutai_profit_before_tax_uses_its_own_fy2025_and_fy2024_rows(self) -> None:
        """Synthetic positioned table reproduces the official reported rows.

        The exact report PDF is not bundled here; this fixture locks down the
        official values and proves each amount is parsed from its own row and
        comparison column without claiming an original-PDF replay.
        """
        company = build_company("600519.SH", "Kweichow Moutai")
        filing = FilingDocument(
            document_id="sse:fixture-moutai-2025-fy",
            company_cik=company.security_id,
            accession_number="moutai-2025-fy",
            form_type="ANNUAL_REPORT",
            fiscal_period="FY",
            period_end="2025-12-31",
            filed_at="2026-04-17T00:00:00+00:00",
            primary_document="2025 年年度报告",
            source_url="https://example.invalid/moutai-2025.pdf",
            content_hash="a" * 64,
        )
        words: list[dict[str, object]] = []
        lines: list[tuple[int, str]] = []

        def add_row(label: str, top: int, values: tuple[str, str] = ()) -> None:
            lines.append((top, label + (" " + " ".join(values) if values else "")))
            words.append({
                "text": label, "x0": 50.0, "x1": 50.0 + len(label) * 7.0,
                "top": top, "bottom": top + 8,
            })
            for value, center in zip(values, (380.0, 510.0)):
                width = max(40.0, len(value) * 5.0)
                words.append({
                    "text": value, "x0": center - width / 2,
                    "x1": center + width / 2, "top": top, "bottom": top + 8,
                })

        add_row("合并利润表", 10)
        # The original statement explicitly supplies both display unit and
        # currency. Do not let this synthetic fixture infer CNY from issuer.
        add_row("单位：元 币种：人民币", 25)
        lines.append((40, "2025 2024"))
        words.extend((
            {"text": "2025", "x0": 370, "x1": 390, "top": 40, "bottom": 48},
            {"text": "2024", "x0": 500, "x1": 520, "top": 40, "bottom": 48},
        ))
        add_row("营业外支出", 60, ("128,635,598.86", "120,937,834.74"))
        add_row("利润总额", 75, ("114,755,261,605.08", "119,638,578,194.46"))

        class Page:
            def extract_words(self, **_kwargs):
                return words

            def extract_text(self):
                return "\n".join(text for _top, text in lines)

        class PdfReader:
            pages = [Page()]

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        with patch("pdfplumber.open", return_value=PdfReader()):
            facts, evidence = FinancialIngestionEngine()._parse_pdf_ast(
                "synthetic-moutai-2025.pdf", company, filing, _manifest_for(filing),
                candidate_pages=frozenset({1}), index_precomputed=True,
            )

        profit = {
            fact.fiscal_year: fact
            for fact in facts
            if fact.concept == "profit_before_tax"
        }
        self.assertEqual(set(profit), {2025, 2024})
        self.assertAlmostEqual(profit[2025].value, 114_755_261_605.08, places=2)
        self.assertAlmostEqual(profit[2024].value, 119_638_578_194.46, places=2)
        self.assertNotAlmostEqual(profit[2025].value, 128_635_598.86, places=2)
        self.assertNotAlmostEqual(profit[2024].value, 120_937_834.74, places=2)
        refs_by_id = {ref.evidence_id: ref for ref in evidence}
        engine = FinancialIngestionEngine()
        for year, fact in profit.items():
            self.assertEqual(fact.source_page, 1)
            self.assertEqual(fact.source_bbox[1], 75)
            self.assertIn("利润总额", fact.raw_text)
            self.assertIn(str(year), fact.source_column)
            ref = refs_by_id[f"fact:{fact.fact_id.removeprefix('ingest:')}"]
            self.assertEqual(ref.locator, "page:1")
            self.assertEqual(ref.bbox, fact.source_bbox)
            self.assertEqual(ref.excerpt, fact.raw_text)
            identity = (
                fact.accession_number,
                fact.end_date,
                fact.fiscal_period,
                fact.consolidated_scope,
                fact.currency,
            )
            validation = engine.validate_group(
                [fact],
                identity,
                {fact.fact_id: ref},
                required_concepts={"profit_before_tax"},
                expected_company_id=company.security_id,
                source_evidence=evidence,
            ).validation
            self.assertIn(fact, validation.accepted)

        mismapped = replace(profit[2025], source_column="2024")
        mismapped_ref = refs_by_id[
            f"fact:{mismapped.fact_id.removeprefix('ingest:')}"
        ]
        mismapped_validation = engine.validate_group(
            [mismapped],
            (
                mismapped.accession_number,
                mismapped.end_date,
                mismapped.fiscal_period,
                mismapped.consolidated_scope,
                mismapped.currency,
            ),
            {mismapped.fact_id: mismapped_ref},
            required_concepts={"profit_before_tax"},
            expected_company_id=company.security_id,
            source_evidence=evidence,
        ).validation
        self.assertNotIn(mismapped, mismapped_validation.accepted)
        self.assertIn(
            "profit_before_tax_period_column_mismatch", mismapped_validation.issues
        )

        invalid_duplicate = replace(mismapped, fact_id="ingest:invalid-column")
        invalid_duplicate_ref = replace(
            mismapped_ref, evidence_id="fact:invalid-column"
        )
        batch = CandidateBatch(
            filing,
            (
                FactCandidate(
                    invalid_duplicate, (invalid_duplicate_ref,), "a-unproven-source"
                ),
                FactCandidate(profit[2025], (mismapped_ref,), "z-positioned-source"),
            ),
            tuple(evidence),
        )
        compiled = FinancialFactCompiler().compile(
            company,
            (filing.period_end, filing.period_end),
            CompilerPolicy(
                filings=(filing,),
                extractors=(InMemoryFactExtractor({filing.document_id: batch}),),
                required_concepts=frozenset({"profit_before_tax"}),
                reporting_currency="CNY",
            ),
        )
        self.assertIn(profit[2025].fact_id, {
            fact.fact_id for fact in compiled.resolved_facts
        })
        self.assertIn(invalid_duplicate.fact_id, {
            fact.fact_id for fact in compiled.quarantined_facts
        })

    def test_recovery_pdfium_index_and_page_count_use_isolated_worker(self) -> None:
        path = "C:/private/company-report.pdf"
        with patch(
            "openthesis.financial_ingestion._run_pdfium_isolated",
            return_value=((frozenset({2, 4}), None), None),
        ) as isolated:
            indexed = _candidate_financial_pages_with_diagnostic(path)
        self.assertEqual(indexed, (frozenset({2, 4}), None))
        self.assertIs(isolated.call_args.args[0], _pdfium_candidate_pages_worker)
        self.assertEqual(isolated.call_args.args[1], path)

        with patch(
            "openthesis.financial_ingestion._run_pdfium_isolated",
            return_value=(7, None),
        ) as isolated:
            page_count = _pdf_page_count(path)
        self.assertEqual(page_count, 7)
        self.assertIs(isolated.call_args.args[0], _pdfium_page_count_worker)
        self.assertEqual(isolated.call_args.args[1], path)

    def test_pdfium_worker_failure_uses_safe_pypdf_index_fallback(self) -> None:
        path = "C:/private/report-with-sensitive-name.pdf"
        with (
            patch(
                "openthesis.financial_ingestion._run_pdfium_isolated",
                side_effect=[(None, "PDFIUM_WORKER_EXIT"),
                             ((frozenset({3, 8}), None), None)],
            ),
            patch(
                "openthesis.financial_ingestion._candidate_financial_pages_pypdf",
                side_effect=AssertionError("fallback must stay in a killable worker"),
            ),
        ):
            pages, diagnostic = _candidate_financial_pages_with_diagnostic(path)
        self.assertEqual(pages, frozenset({3, 8}))
        self.assertEqual(diagnostic, "PDFIUM_WORKER_EXIT")
        self.assertNotIn(path, diagnostic)

    def test_hkex_ifrs_wrapped_capex_uses_2025_column_and_rmb_thousands(self) -> None:
        """Minimal portable table from Meituan 2025 report p.220.

        The official table says ``Purchases or prepayments of property, plant
        and equipment and intangible assets`` across two lines; it reports
        2025/2024 values in RMB'000.  This synthetic geometry is checked into
        the test logic instead of retaining the full issuer PDF.
        """

        company = Company(
            "HK:SEHK:03690.HK", "03690.HK", "Meituan", "SEHK",
            "HKEX:03690", "HK", "HK:SEHK:03690.HK", "CNY", "CNY", "IFRS",
        )
        filing = FilingDocument(
            "hkex:fixture-meituan-2025", company.security_id,
            "fixture-meituan-2025", "ANNUAL_REPORT", "FY", "2025-12-31",
            "2026-04-24", "FY 2025 Annual Report",
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0424/2026042400179.pdf",
        )

        words: list[dict[str, object]] = []
        lines: list[tuple[int, str]] = []

        def add_label_row(label: str, top: int, values: tuple[str, str] = ()) -> None:
            lines.append((top, label + (" " + " ".join(values) if values else "")))
            x = 50.0
            for token in label.split():
                width = max(5.0, len(token) * 4.6)
                words.append({"text": token, "x0": x, "x1": x + width, "top": top, "bottom": top + 8})
                x += width + 3.0
            for value, left in zip(values, (350.0, 460.0)):
                words.append({"text": value, "x0": left, "x1": left + 60, "top": top, "bottom": top + 8})

        add_label_row("CONSOLIDATED STATEMENT OF CASH FLOWS", 10)
        add_label_row("Unit: RMB'000", 25)
        add_label_row("Year ended December 31,", 40)
        lines.append((55, "2025 2024"))
        words.extend((
            {"text": "2025", "x0": 370, "x1": 390, "top": 55, "bottom": 63},
            {"text": "2024", "x0": 480, "x1": 500, "top": 55, "bottom": 63},
        ))
        add_label_row("Cash flows from operating activities", 70)
        add_label_row("Net cash flows from operating activities", 85, ("57,146,784", "57,146,784"))
        add_label_row("Cash flows from investing activities", 100)
        add_label_row(
            "Purchases or prepayments of property, plant and equipment and",
            115,
            ("(13,270,708)", "(10,999,490)"),
        )
        add_label_row("intangible assets", 127)

        class Page:
            def extract_words(self, **_kwargs):
                return words

            def extract_text(self):
                return "\n".join(text for _top, text in lines)

        class PdfReader:
            pages = [Page()]

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        engine = FinancialIngestionEngine()
        with patch("pdfplumber.open", return_value=PdfReader()):
            facts, _evidence = engine._parse_pdf_ast(
                "synthetic-hkex.pdf", company, filing, _manifest_for(filing),
                candidate_pages=frozenset({1}), index_precomputed=True,
            )

        capex = [fact for fact in facts if fact.concept == "capital_expenditure"]
        by_year = {fact.fiscal_year: fact for fact in capex}
        self.assertEqual(set(by_year), {2024, 2025})
        self.assertEqual(
            (by_year[2025].end_date, by_year[2025].value),
            ("2025-12-31", 13_270_708_000.0),
        )
        self.assertEqual(
            (by_year[2024].end_date, by_year[2024].value, by_year[2024].usage_status),
            ("2024-12-31", 10_999_490_000.0, "comparator"),
        )
        self.assertTrue(all(fact.currency == "CNY" and fact.unit_scale == 1_000 for fact in capex))
        self.assertTrue(all("intangible assets" in fact.raw_text for fact in capex))
        self.assertTrue(_is_capex_cash_outflow_label(
            "Purchases or prepayments of property, plant and equipment and intangible assets"
        ))

    def test_distinct_capex_components_aggregate_once_and_reported_total_wins(self) -> None:
        def fact(fact_id: str, label: str, value: float) -> FinancialFact:
            return FinancialFact(
                fact_id, "HK:SEHK:03690.HK", "capital_expenditure", label,
                value, "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01",
                "2025-12-31", "2026-04-24", "meituan-2025",
                "https://example.invalid/annual.pdf", statement="cash_flow",
                consolidated_scope="consolidated", currency="CNY", unit_scale=1_000,
                source_page=220, raw_text=label,
            )

        ppe = fact("ppe", "Purchases of property, plant and equipment", 8_000_000)
        intangible = fact("intangibles", "Payments for purchase of intangible assets", 3_000_000)
        duplicate_ppe = fact("ppe-duplicate", "Purchase of property, plant and equipment", 8_000_000)
        reported_total = fact(
            "combined", "Purchases or prepayments of property, plant and equipment and intangible assets",
            11_000_000,
        )

        aggregate = _merge_capex_facts(ppe, intangible)
        self.assertIsNotNone(aggregate)
        self.assertEqual(aggregate.value, 11_000_000)
        self.assertIsNone(_merge_capex_facts(aggregate, duplicate_ppe))
        # A disclosed aggregate is authoritative when present alongside its
        # component lines; it replaces rather than adds to the component sum.
        total = _merge_capex_facts(aggregate, reported_total)
        self.assertIsNotNone(total)
        self.assertEqual(total.value, 11_000_000)
        self.assertFalse(_is_capex_cash_outflow_label("Proceeds from disposal of property, plant and equipment"))

    def test_segment_note_rows_do_not_inherit_consolidated_revenue_context(self) -> None:
        previous = PdfTableContext(
            "income_statement", "consolidated", 1_000.0, "CNY", True,
            (_PeriodColumn(2025, 380, 330, 420, "CNY", 1_000, "2025"),),
            10,
        )
        row = PdfRowAST(
            (
                PdfCellAST("Segment revenue", 50, 20, 210, 28),
                PdfCellAST("12,000", 350, 20, 390, 28),
            ),
            20,
            (50, 20, 390, 28),
        )

        sections = _page_sections(
            previous,
            "Note 8: Segment information\nSegment revenue 12,000",
            (row,), 11, "CNY",
        )

        self.assertEqual(sections, ())

        statement_title = PdfRowAST(
            (PdfCellAST("CONSOLIDATED INCOME STATEMENT", 50, 5, 260, 13),),
            5, (50, 5, 260, 13),
        )
        statement_unit = PdfRowAST(
            (PdfCellAST("Unit: RMB'000", 50, 14, 130, 22),),
            14, (50, 14, 130, 22),
        )
        consolidated_revenue = PdfRowAST(
            (PdfCellAST("Revenue", 50, 20, 110, 28), PdfCellAST("90,000", 350, 20, 390, 28)),
            20, (50, 20, 390, 28),
        )
        segment_heading = PdfRowAST(
            (PdfCellAST("Note 8: Segment information", 50, 35, 210, 43),),
            35, (50, 35, 210, 43),
        )
        segment_revenue = PdfRowAST(
            (PdfCellAST("Segment revenue", 50, 50, 210, 58), PdfCellAST("12,000", 350, 50, 390, 58)),
            50, (50, 50, 390, 58),
        )
        mixed_page_sections = _page_sections(
            None,
            "CONSOLIDATED INCOME STATEMENT\nUnit: RMB'000\nRevenue 90,000\nNote 8: Segment information\nUnit: RMB million\nSegment revenue 12,000",
            (statement_title, statement_unit, consolidated_revenue, segment_heading, segment_revenue),
            1, "CNY",
        )
        self.assertEqual(len(mixed_page_sections), 1)
        self.assertEqual(mixed_page_sections[0].context.multiplier, 1_000)
        self.assertTrue(any("Revenue" in item.text for item in mixed_page_sections[0].rows))
        self.assertFalse(any("Segment revenue" in item.text for item in mixed_page_sections[0].rows))

    def test_explicit_shareholder_capital_returns_preserve_period_unit_and_scope(self) -> None:
        self.assertEqual(
            _capital_return_label("Repurchase of ordinary shares"),
            ("share_repurchase", "repurchase of own shares"),
        )
        self.assertEqual(
            _capital_return_label("向股东支付股利"),
            ("shareholder_dividends_paid", "dividends paid to shareholders"),
        )
        for false_positive in (
            "Proceeds from disposal of treasury shares",
            "Proceeds from repurchase of ordinary shares",
            "Purchase of equity securities",
            "Repurchase of shares in an associate",
            "Dividends received from associates",
            "Purchase of subsidiary shares",
            "出售库存股所得款项",
        ):
            with self.subTest(label=false_positive):
                self.assertIsNone(_capital_return_label(false_positive))

        company = Company(
            "HK:SEHK:09999.HK", "09999.HK", "Example Holdings", "SEHK",
            "HKEX:09999", "HK", "HK:SEHK:09999.HK", "CNY", "CNY", "IFRS",
        )
        filing = FilingDocument(
            "hkex:capital-return-fixture", company.security_id,
            "capital-return-fixture", "ANNUAL_REPORT", "FY", "2025-12-31",
            "2026-03-20", "FY 2025 Annual Report", "https://example.invalid/report.pdf",
            content_hash="a" * 64,
        )
        words: list[dict[str, object]] = []
        lines: list[tuple[int, str]] = []

        def add_row(label: str, top: int, values: tuple[str, str] = ()) -> None:
            lines.append((top, label + (" " + " ".join(values) if values else "")))
            x = 50.0
            for token in label.split():
                width = max(5.0, len(token) * 4.6)
                words.append({"text": token, "x0": x, "x1": x + width, "top": top, "bottom": top + 8})
                x += width + 3.0
            for value, left in zip(values, (350.0, 460.0)):
                words.append({"text": value, "x0": left, "x1": left + 60, "top": top, "bottom": top + 8})

        add_row("CONSOLIDATED STATEMENT OF CASH FLOWS", 10)
        add_row("Unit: RMB'000", 25)
        add_row("Year ended December 31,", 40)
        lines.append((55, "2025 2024"))
        words.extend((
            {"text": "2025", "x0": 370, "x1": 390, "top": 55, "bottom": 63},
            {"text": "2024", "x0": 480, "x1": 500, "top": 55, "bottom": 63},
        ))
        add_row("Cash flows from financing activities", 70)
        add_row("Repurchase of ordinary shares", 85, ("(9,250)", "(6,200)"))
        add_row("Dividends paid to shareholders", 100, ("(4,500)", "(3,800)"))
        add_row("Proceeds from disposal of treasury shares", 115, ("1,100", "900"))
        add_row("Purchase of equity securities", 130, ("(2,000)", "(1,000)"))
        add_row("Repurchase of shares in associate", 145, ("(3,000)", "(2,000)"))

        class Page:
            def extract_words(self, **_kwargs):
                return words

            def extract_text(self):
                return "\n".join(text for _top, text in lines)

        class PdfReader:
            pages = [Page()]

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        with patch("pdfplumber.open", return_value=PdfReader()):
            facts, refs = FinancialIngestionEngine()._parse_pdf_ast(
                "synthetic-capital-return.pdf", company, filing, _manifest_for(filing),
                candidate_pages=frozenset({1}), index_precomputed=True,
            )

        by_key = {(fact.concept, fact.fiscal_year, fact.usage_status): fact for fact in facts}
        self.assertEqual(
            set((fact.concept for fact in facts)),
            {"share_repurchase", "shareholder_dividends_paid"},
        )
        expected = {
            ("share_repurchase", 2025, "audit_only"): 9_250_000,
            ("share_repurchase", 2024, "comparator"): 6_200_000,
            ("shareholder_dividends_paid", 2025, "audit_only"): 4_500_000,
            ("shareholder_dividends_paid", 2024, "comparator"): 3_800_000,
        }
        self.assertEqual({key: fact.value for key, fact in by_key.items()}, expected)
        self.assertEqual(sum(ref.evidence_id.startswith("fact:") for ref in refs), len(facts))
        self.assertEqual(sum(ref.evidence_id.startswith("unit:") for ref in refs), len(facts))
        titles = [ref for ref in refs if ref.evidence_id.startswith("statement:")]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].excerpt, "CONSOLIDATED STATEMENT OF CASH FLOWS")
        refs_by_id = {ref.evidence_id: ref for ref in refs}
        for fact in facts:
            self.assertEqual(fact.currency, "CNY")
            self.assertEqual(fact.unit_scale, 1_000)
            self.assertEqual(fact.statement, "cash_flow")
            self.assertEqual(fact.consolidated_scope, "consolidated")
            self.assertEqual(fact.source_page, 1)
            self.assertTrue(fact.raw_text)
            self.assertEqual(fact.classification_evidence_ids, (titles[0].evidence_id,))
            self.assertTrue(fact.normalization_lineage.unit_evidence_ids)
            self.assertTrue(
                all(
                    evidence_id in refs_by_id
                    for evidence_id in fact.normalization_lineage.unit_evidence_ids
                )
            )
        self.assertIn("Repurchase of ordinary shares", by_key[("share_repurchase", 2025, "audit_only")].raw_text)
        self.assertIn("Dividends paid to shareholders", by_key[("shareholder_dividends_paid", 2025, "audit_only")].raw_text)
        refs_by_fact = {
            f"ingest:{ref.evidence_id.removeprefix('fact:')}": ref for ref in refs
        }
        engine = FinancialIngestionEngine()
        for year in (2025, 2024):
            year_facts = [fact for fact in facts if fact.fiscal_year == year]
            identity = (
                filing.accession_number,
                year_facts[0].end_date,
                year_facts[0].fiscal_period,
                year_facts[0].consolidated_scope,
                year_facts[0].currency,
            )
            validation = engine.validate_group(
                year_facts,
                identity,
                refs_by_fact,
                required_concepts=frozenset(fact.concept for fact in year_facts),
                source_evidence=refs,
            ).validation
            self.assertEqual(validation.status, ValidationStatus.VERIFIED)
            self.assertEqual(
                {fact.concept for fact in validation.accepted},
                {fact.concept for fact in year_facts},
            )

    def test_empty_canonical_financial_view_never_reaches_provider_boundary(self) -> None:
        called = False
        def provider_factory(_facts: list[object]) -> None:
            nonlocal called
            called = True
        company = build_company("832982.BJ", "Jinbo")
        filing = FilingDocument(
            document_id="official:jinbo:q1-empty",
            company_cik=company.security_id,
            accession_number="jinbo-q1-empty",
            form_type="QUARTERLY_REPORT",
            fiscal_period="Q1",
            period_end="2026-03-31",
            filed_at="2026-04-30T00:00:00+00:00",
            primary_document="2026 第一季度报告",
            source_url="https://www.neeq.com.cn/",
        )
        facts = []
        canonical = CanonicalFinancialFactCompiler().compile_facts(
            company, [filing], facts,
        )
        admitted = FinancialAdmission().admit(
            company, canonical, selected_filings=[filing],
        )
        if admitted.model_ready:
            provider_factory(list(admitted.facts))
        self.assertEqual(admitted.facts, ())
        self.assertFalse(admitted.model_ready)
        self.assertFalse(called)

    def test_period_start_for_cumulative_q2_and_non_calendar_fy(self) -> None:
        filing = FilingDocument(
            document_id="period",
            company_cik="x",
            accession_number="x",
            form_type="QUARTERLY_REPORT",
            fiscal_period="Q2",
            period_end="2024-06-30",
            filed_at="2024-08-01",
            primary_document="Q2",
            source_url="https://example.invalid",
        )
        self.assertEqual(_period_start(filing, 2024), "2024-01-01")
        filing.fiscal_period = "H1"
        filing.period_end = "2024-03-31"
        self.assertEqual(_period_start(filing, 2024), "2023-10-01")

    def test_narrative_page_with_unit_and_financial_word_is_not_statement(self) -> None:
        company = build_company("832982.BJ", "Jinbo")
        filing = FilingDocument(
            document_id="official:narrative",
            company_cik=company.security_id,
            accession_number="narrative",
            form_type="ANNUAL_REPORT",
            fiscal_period="FY",
            period_end="2023-12-31",
            filed_at="2024-04-25T00:00:00+00:00",
            primary_document="2023 Annual Report",
            source_url="https://example.invalid/report.pdf",
        )
        facts, _ = parse_financial_pages(
            [(23, "单位：万元\n营业收入 1,234（本段为经营回顾文字，不是利润表）")],
            filing,
            company,
        )
        self.assertEqual(facts, [])

    def test_jinbo_unit_does_not_leak_from_page_23_to_page_146(self) -> None:
        company = build_company("832982.BJ", "Jinbo")
        filing = FilingDocument(
            document_id="official:jinbo:fy2023",
            company_cik=company.security_id,
            accession_number="jinbo-fy2023",
            form_type="ANNUAL_REPORT",
            fiscal_period="FY",
            period_end="2023-12-31",
            filed_at="2024-04-25T00:00:00+00:00",
            primary_document="2023 年年度报告",
            source_url="https://www.bse.cn/disclosure/announcement.html",
        )
        facts, _ = parse_financial_pages(
            [
                (23, "合并资产负债表\n单位：万元\n资产总计 123,456"),
                (146, "合并现金流量表\n经营活动产生的现金流量净额 295,566,382.43"),
            ],
            filing,
            company,
        )
        cash = next(fact for fact in facts if fact.concept == "operating_cash_flow")
        self.assertAlmostEqual(cash.value, 295_566_382.43)
        self.assertNotEqual(cash.value, 295_566_382.43 * 10_000)

    def test_official_fixture_matrix_covers_required_markets_and_periods(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "official_financial_sources.json"
        def reject_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise AssertionError(f"duplicate fixture key: {key}")
                result[key] = value
            return result
        payload = json.loads(fixture_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
        sources = payload["sources"]
        self.assertEqual({item["market"] for item in sources}, {"SSE_STAR", "BSE", "SZSE", "HKEX", "SEC"})
        self.assertGreaterEqual(sum(item["market"] == "SEC" for item in sources), 3)
        self.assertGreaterEqual(sum(item["market"] == "HKEX" for item in sources), 2)
        self.assertTrue({item["period"] for item in sources} >= {"FY2024", "H1-2024", "Q3-2024"})
        for item in sources:
            self.assertTrue(item["source_url"].startswith("https://"))
            self.assertEqual(len(item["expected_concepts"]), 6)
            raw = item["raw_excerpt"].encode("utf-8")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["raw_excerpt_sha256"])
            self.assertIn("source_excerpt", item)
            self.assertEqual(
                hashlib.sha256(item["source_excerpt"].encode("utf-8")).hexdigest(),
                item["source_excerpt_sha256"],
            )
            parsed_snapshot = parse_structured_snapshot(item["raw_excerpt"])
            self.assertGreaterEqual(len(parsed_snapshot), 3)
            core = {"revenue", "net_income", "assets", "liabilities", "equity", "operating_cash_flow"}
            self.assertGreaterEqual(len(set(item.get("expected_core_facts", {})) & core), 3)
            for concept, expected in item.get("expected_core_facts", {}).items():
                self.assertEqual(parsed_snapshot[concept], float(expected))
        for market in {"SSE_STAR", "HKEX", "SEC"}:
            market_items = [item for item in sources if item["market"] == market]
            self.assertTrue(any(len(parse_structured_snapshot(item["raw_excerpt"])) >= 6 for item in market_items))
    def test_utf8_statement_context_scales_rows_and_keeps_provenance(self) -> None:
        company = build_company("688981.SH", "SMIC")
        filing = FilingDocument(
            document_id="official:smic:2025-fy",
            company_cik=company.security_id,
            accession_number="smic-2025-fy",
            form_type="ANNUAL_REPORT",
            fiscal_period="FY",
            period_end="2025-12-31",
            filed_at="2026-03-30T00:00:00+00:00",
            primary_document="2025 年年度报告",
            source_url="https://www.sse.com.cn/disclosure/listedinfo/announcement/",
        )
        facts, evidence = parse_financial_pages(
            [
                (100, "合并利润表\n单位：人民币万元\n营业收入 12,345\n净利润 1,234"),
                (101, "合并现金流量表\n单位：人民币万元\n经营活动产生的现金流量净额 2,345"),
                (140, "经营活动产生的现金流量净额 99"),
            ],
            filing,
            company,
        )
        values = {fact.concept: fact for fact in facts}
        self.assertEqual(values["revenue"].value, 123_450_000)
        self.assertEqual(values["revenue"].unit_scale, 10_000)
        self.assertEqual(values["revenue"].statement, "income_statement")
        self.assertEqual(values["revenue"].source_page, 100)
        self.assertEqual(values["revenue"].parser_version, "financial-ingestion-compat-v1")
        self.assertIsNotNone(values["revenue"].normalization_lineage)
        self.assertEqual(values["revenue"].normalization_lineage.raw_value, "12,345")
        self.assertEqual(values["revenue"].normalization_lineage.scale, "10000")
        self.assertEqual(values["revenue"].normalization_lineage.normalized_value, "123450000")
        self.assertEqual(len(values["revenue"].normalization_lineage.unit_evidence_ids), 1)
        unit_id = values["revenue"].normalization_lineage.unit_evidence_ids[0]
        self.assertTrue(any(item.evidence_id == unit_id and item.locator == "page:100" for item in evidence))
        restored = FinancialFact(**json.loads(json.dumps(values["revenue"].to_dict())))
        self.assertEqual(restored.normalization_lineage, values["revenue"].normalization_lineage)
        corrupted = replace(values["revenue"], value=values["revenue"].value + 1)
        canonical = CanonicalFinancialFactCompiler().compile_facts(
            company, [filing], [corrupted], evidence=evidence,
        )
        record = canonical.coverage["fact_validation_records"][corrupted.fact_id]
        admitted = FinancialAdmission().admit(
            company, canonical, selected_filings=[filing],
        )
        self.assertFalse(record["accepted"])
        self.assertIn("normalized_amount_does_not_match_fact", record["issues"])
        self.assertFalse(admitted.model_ready)
        self.assertFalse(any(item.concept == "revenue" for item in admitted.facts))
        self.assertTrue(any(item.locator == "page:100" for item in evidence))
        # Page 140 is not an adjacent continuation and cannot inherit 万元.
        self.assertEqual(values["operating_cash_flow"].value, 2_345 * 10_000)

    def test_validation_gate_rejects_insufficient_core_coverage(self) -> None:
        company = build_company("832982.BJ", "Jinbo")
        filing = FilingDocument(
            document_id="official:jinbo:q1",
            company_cik=company.security_id,
            accession_number="jinbo-q1",
            form_type="QUARTERLY_REPORT",
            fiscal_period="Q1",
            period_end="2026-03-31",
            filed_at="2026-04-30T00:00:00+00:00",
            primary_document="2026 第一季度报告",
            source_url="https://www.neeq.com.cn/",
            content_hash="a" * 64,
        )
        source_page = "合并利润表\n单位：元\n营业收入 1,000"
        facts, evidence = parse_financial_pages(
            [(3, source_page)], filing, company,
        )
        self.assertEqual(facts[0].currency, "")
        self.assertEqual(facts[0].unit, "")
        canonical = CanonicalFinancialFactCompiler().compile_facts(
            company, [filing], facts, evidence=evidence,
        )
        admitted = FinancialAdmission().admit(
            company, canonical, selected_filings=[filing],
        )
        record = canonical.coverage["fact_validation_records"][facts[0].fact_id]
        group_issues = {issue for group in canonical.validations for issue in group.issues}
        self.assertFalse(record["accepted"])
        self.assertEqual(record["components"]["currency"]["state"], "unknown")
        self.assertIn("core_coverage_insufficient", group_issues)
        self.assertFalse(admitted.model_ready)
        self.assertFalse(admitted.facts)

    def test_explicit_rmb_unit_admits_revenue_but_preserves_profile_gap(self) -> None:
        company = build_company("832982.BJ", "Jinbo")
        filing = FilingDocument(
            document_id="official:jinbo:q1-explicit-rmb",
            company_cik=company.security_id,
            accession_number="jinbo-q1-explicit-rmb",
            form_type="QUARTERLY_REPORT",
            fiscal_period="Q1",
            period_end="2026-03-31",
            filed_at="2026-04-30T00:00:00+00:00",
            primary_document="2026 第一季度报告",
            source_url="https://www.neeq.com.cn/",
            content_hash="b" * 64,
        )
        source_page = "合并利润表\n单位：人民币元\n营业收入 1,000"
        facts, evidence = parse_financial_pages([(3, source_page)], filing, company)
        canonical = CanonicalFinancialFactCompiler().compile_facts(
            company, [filing], facts, evidence=evidence,
        )
        admitted = FinancialAdmission().admit(
            company, canonical, selected_filings=[filing],
        )
        record = canonical.coverage["fact_validation_records"][facts[0].fact_id]
        group_issues = {issue for group in canonical.validations for issue in group.issues}

        self.assertEqual(facts[0].currency, "CNY")
        self.assertEqual(facts[0].unit, "CNY")
        self.assertTrue(record["accepted"])
        self.assertEqual(record["components"]["currency"]["state"], "verified")
        self.assertFalse(canonical.allow_ai)
        self.assertIn("core_coverage_insufficient", group_issues)
        self.assertEqual([fact.concept for fact in admitted.facts], ["revenue"])

    def test_official_source_schema_carries_period_and_document_evidence(self) -> None:
        source = OfficialSource(
            entity="Tencent",
            market="HK",
            statement="income_statement",
            concept="revenue",
            fiscal_period="H1",
            period_start="2025-01-01",
            period_end="2025-06-30",
            consolidated_scope="consolidated",
            currency="HKD",
            unit="HKD:million",
            revision="original",
            source_url="https://www.tencent.com/en-us/investors/financial-news.html",
            document="2025 Interim Report",
            page=12,
            raw_text="Revenue 384,064",
            parser_version="xbrl-adapter-v1",
        )
        self.assertEqual(source.fiscal_period, "H1")
        self.assertEqual(source.page, 12)
        self.assertEqual(source.validation_status, ValidationStatus.READY_WITH_WARNINGS.value)


if __name__ == "__main__":
    unittest.main()
