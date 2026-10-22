from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openthesis.domain import Company, FilingDocument, FinancialFact
from openthesis.financial_ingestion import (
    FinancialIngestionEngine,
    PdfCellAST,
    PdfRowAST,
    _PDF_PARSER_VERSION,
    _manifest_for,
    _merge_visual_rows,
    _row_label_text,
)
from openthesis.financial_sources.document_units import DocumentUnitResolution


_ROE_EVIDENCE_PDF = (
    Path(__file__).resolve().parents[1]
    / "tmp/qa-independent-3.1.3-20261021/sidecar-data/filings/CN_A_SSE_688981.SH/"
    "1225520297-6a44b7cfe77129d4.pdf"
)


def _row(top: float, bottom: float, cells: tuple[tuple[str, float, float], ...]) -> PdfRowAST:
    positioned = tuple(
        PdfCellAST(text, x0, top, x1, bottom)
        for text, x0, x1 in cells
    )
    return PdfRowAST(
        positioned,
        top,
        (
            min(cell.x0 for cell in positioned),
            top,
            max(cell.x1 for cell in positioned),
            bottom,
        ),
    )


class RoeRowBinding314Tests(unittest.TestCase):
    def test_adjacent_eps_values_are_not_rebound_to_roe_row(self) -> None:
        # Page 8 of accession 1225520297. Coordinates and cells are taken from
        # the positioned PDF AST; the EPS row's unit wraps onto its own line.
        rows = (
            _row(
                196.983,
                207.433,
                (
                    ("扣除非经常性损益后的基本每股收益", 89.88, 258.0),
                    ("0.37", 330.1, 351.1),
                    ("0.24", 419.0, 439.9),
                    ("54.2", 511.7, 532.5),
                ),
            ),
            _row(210.663, 221.113, (("（元／股）", 89.88, 142.33),)),
            _row(
                223.972,
                235.153,
                (
                    ("加权平均净资产收益率（%）(1)", 89.88, 228.8),
                    ("2.9", 335.4, 351.1),
                    ("1.5", 424.2, 439.9),
                    ("增加1.4个百分点", 453.8, 532.57),
                ),
            ),
            _row(
                238.863,
                249.313,
                (
                    ("扣除非经常性损益后的加权平均净资产", 89.88, 257.8),
                    ("2.0", 335.4, 351.1),
                    ("1.3", 424.2, 439.9),
                    ("增加0.7个百分点", 453.8, 532.57),
                ),
            ),
            _row(251.692, 262.872, (("收益率（%）(2)", 89.88, 165.85),)),
        )

        # Starting at either the EPS row or its wrapped unit line must not
        # absorb the next complete ROE row (otherwise 0.37 becomes ROE, or
        # 2.9 is emitted again with a contaminated bbox).
        for start in (0, 1):
            with self.subTest(start=start):
                merged = _merge_visual_rows(rows, start)
                self.assertNotIn("加权平均净资产收益率", _row_label_text(merged))
                self.assertNotIn("2.9", {cell.text for cell in merged.cells})

        # A real wrapped ROE label still joins its continuation while keeping
        # the two numeric period cells on that same logical row.
        adjusted = _merge_visual_rows(rows, 3)
        self.assertIn("加权平均净资产收益率", _row_label_text(adjusted))
        self.assertEqual(
            [cell.text for cell in adjusted.cells if cell.text in {"2.0", "1.3"}],
            ["2.0", "1.3"],
        )

    @unittest.skipUnless(_ROE_EVIDENCE_PDF.is_file(), "local 3.1.3 source PDF is not present")
    def test_accession_1225520297_binds_summary_roe_to_explicit_period_columns(self) -> None:
        source_hash = hashlib.sha256(_ROE_EVIDENCE_PDF.read_bytes()).hexdigest()
        company = Company(
            "688981.SH", "688981.SH", "中芯国际", "SSE", market="CN_A",
            reporting_currency="CNY", accounting_standard="CAS",
        )
        filing = FilingDocument(
            "1225520297", company.cik, "1225520297", "INTERIM_REPORT", "H1",
            "2026-06-30", "2026-08-29T00:00:00+00:00", "2026年半年度报告",
            "https://example.invalid/1225520297.pdf", local_path=str(_ROE_EVIDENCE_PDF),
            content_hash=source_hash,
        )
        manifest = _manifest_for(filing)
        self.assertIsNotNone(manifest)
        with patch("openthesis.financial_ingestion.cached_page_texts", return_value=None):
            facts, _refs = FinancialIngestionEngine()._parse_pdf_ast(
                str(_ROE_EVIDENCE_PDF), company, filing, manifest,
                candidate_pages=frozenset({8}), index_precomputed=True,
                document_units=DocumentUnitResolution(),
            )

        reported = [fact for fact in facts if fact.concept == "reported_roe"]
        adjusted = [
            fact for fact in facts
            if fact.concept == "reported_roe_excluding_nonrecurring"
        ]
        self.assertEqual(len(reported), 2)
        self.assertEqual(len(adjusted), 2)
        self.assertEqual(
            {(fact.source_column, fact.value, fact.fiscal_year, fact.fiscal_period,
              fact.start_date, fact.end_date) for fact in reported},
            {
                ("本报告期", 0.029, 2026, "H1", "2026-01-01", "2026-06-30"),
                ("上年同期", 0.015, 2025, "H1", "2025-01-01", "2025-06-30"),
            },
        )
        self.assertEqual(
            {(fact.source_column, fact.value, fact.fiscal_year, fact.fiscal_period)
             for fact in adjusted},
            {("本报告期", 0.020, 2026, "H1"), ("上年同期", 0.013, 2025, "H1")},
        )
        self.assertTrue(all("扣除非经常性损益后" in fact.reported_concept for fact in adjusted))
        self.assertTrue(all("扣除非经常性损益后" in fact.raw_text for fact in adjusted))
        self.assertFalse(any(fact.value == 0.0037 for fact in facts))
        self.assertFalse(any(fact.value in {0.014, 0.007} for fact in facts))

    def test_parse_cache_mapping_marker_invalidates_only_old_roe_payloads(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / "tmp"
        ) as directory:
            cache_dir = Path(directory)

            def persist(concept: str, key: str) -> Path:
                fact = FinancialFact(
                    f"ingest:{key}", "688981.SH", concept, concept, 0.029, "ratio",
                    2026, "H1", "INTERIM_REPORT", "2026-01-01", "2026-06-30",
                    "2026-08-29T00:00:00+00:00", "1225520297",
                    "https://example.invalid/1225520297.pdf", parser_version=_PDF_PARSER_VERSION,
                )
                FinancialIngestionEngine(cache_dir=cache_dir)._store_parse_cache(key, [fact], [])
                path = cache_dir / f"{key}.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["roe_row_mapping_version"], "roe-row-v2")
                payload.pop("roe_row_mapping_version")
                path.write_text(json.dumps(payload), encoding="utf-8")
                return path

            for concept in ("reported_roe", "reported_roe_excluding_nonrecurring"):
                with self.subTest(concept=concept):
                    persist(concept, concept)
                    self.assertIsNone(
                        FinancialIngestionEngine(cache_dir=cache_dir)._load_parse_cache(concept)
                    )

            persist("revenue", "revenue")
            self.assertIsNotNone(
                FinancialIngestionEngine(cache_dir=cache_dir)._load_parse_cache("revenue")
            )


if __name__ == "__main__":
    unittest.main()
