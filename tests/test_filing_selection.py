from __future__ import annotations

import unittest

from openthesis.domain import FilingDocument
from openthesis.filing_selection import select_research_filings


class FilingSelectionTests(unittest.TestCase):
    def test_annual_suppresses_periodic_reports_for_the_same_year(self) -> None:
        result = select_research_filings(
            [
                _filing("a25", "ANNUAL_REPORT", "FY", "2025-12-31"),
                _filing("q25", "QUARTERLY_REPORT", "Q3", "2025-09-30"),
                _filing("h24", "INTERIM_REPORT", "H1", "2024-06-30"),
            ]
        )
        self.assertEqual([item.document_id for item in result.documents], ["a25", "h24"])

    def test_latest_periodic_keeps_prior_year_same_period_for_comparison(self) -> None:
        result = select_research_filings(
            [
                _filing("q1-26", "QUARTERLY_REPORT", "Q1", "2026-03-31"),
                _filing("fy-25", "ANNUAL_REPORT", "FY", "2025-12-31"),
                _filing("q1-25", "QUARTERLY_REPORT", "Q1", "2025-03-31"),
                _filing("q3-25", "QUARTERLY_REPORT", "Q3", "2025-09-30"),
            ]
        )

        self.assertEqual(
            [item.document_id for item in result.documents],
            ["q1-26", "fy-25", "q1-25"],
        )

    def test_new_listing_keeps_all_available_periods_and_listing_document(self) -> None:
        result = select_research_filings(
            [
                _filing("q1", "QUARTERLY_REPORT", "Q1", "2025-03-31"),
                _filing("h1", "INTERIM_REPORT", "H1", "2025-06-30"),
                _filing("q3", "QUARTERLY_REPORT", "Q3", "2025-09-30"),
                _filing("ipo", "PROSPECTUS", "IPO", "2025-01-01"),
            ]
        )
        self.assertTrue(result.used_listing_fallback)
        self.assertEqual({item.document_id for item in result.documents}, {"q1", "h1", "q3", "ipo"})

    def test_listing_document_is_not_used_when_an_annual_report_exists(self) -> None:
        result = select_research_filings(
            [
                _filing("annual", "ANNUAL_REPORT", "FY", "2024-12-31"),
                _filing("ipo", "PROSPECTUS", "IPO", "2024-01-01"),
            ]
        )
        self.assertEqual([item.document_id for item in result.documents], ["annual"])
        self.assertFalse(result.used_listing_fallback)

    def test_a_share_annual_report_wins_over_h_share_announcement_for_a_share_company(self) -> None:
        a_share = _filing("a-share", "ANNUAL_REPORT", "FY", "2023-12-31")
        a_share.primary_document = "中芯国际2023年年度报告"
        h_share = _filing("h-share", "ANNUAL_REPORT", "FY", "2023-12-31")
        h_share.primary_document = "港股公告：2023年报"
        h_share.filed_at = "2024-04-10T00:00:00+00:00"

        result = select_research_filings([h_share, a_share])

        self.assertEqual([item.document_id for item in result.documents], ["a-share"])

    def test_explicit_revision_supersedes_original_same_period(self) -> None:
        original = _filing("original", "ANNUAL_REPORT", "FY", "2023-12-31")
        corrected = _filing("corrected", "ANNUAL_REPORT", "FY", "2023-12-31")
        corrected.revision = "corrected"
        corrected.supersedes_document_id = original.document_id
        corrected.filed_at = "2024-05-30T00:00:00+00:00"
        result = select_research_filings([original, corrected])
        self.assertEqual([item.document_id for item in result.documents], ["corrected"])


def _filing(document_id: str, form_type: str, period: str, period_end: str) -> FilingDocument:
    return FilingDocument(
        document_id=document_id,
        company_cik="CN_A:SSE:688000.SH",
        accession_number=document_id,
        form_type=form_type,
        fiscal_period=period,
        period_end=period_end,
        filed_at=f"{int(period_end[:4]) + 1}-03-30T00:00:00+00:00",
        primary_document=document_id,
        source_url=f"https://example.invalid/{document_id}.pdf",
    )


if __name__ == "__main__":
    unittest.main()
