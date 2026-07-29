from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openthesis.domain import FilingDocument
from openthesis.filing_parser import (
    build_filing_evidence,
    extract_table_evidence,
    extract_topic_evidence,
)


class FilingParserTests(unittest.TestCase):
    def test_extracts_structured_topic_evidence(self) -> None:
        html = """
        <html><body>
          <h1>Item 1. Business</h1>
          <p>Example Corp provides subscription analytics software to enterprise
          customers. Revenue is generated from annual subscriptions and usage.</p>
          <h1>Item 1A. Risk Factors</h1>
          <p>Competition may reduce pricing power and customer retention. The
          company depends on a limited number of infrastructure providers.</p>
          <h1>Item 7. Management's Discussion and Analysis</h1>
          <p>Capital expenditures increased as the company expanded capacity.
          Management expects new products to require additional investment.</p>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filing.html"
            path.write_text(html, encoding="utf-8")
            filing = FilingDocument(
                document_id="test:filing",
                company_cik="0001",
                accession_number="test",
                form_type="10-K",
                fiscal_period="FY2025",
                period_end="2025-12-31",
                filed_at="2026-02-01",
                primary_document="filing.html",
                source_url="https://example.test/filing",
                local_path=str(path),
            )
            evidence = extract_topic_evidence(filing)
            topics = {item.title.split(" · ")[-1] for item in evidence}
            self.assertIn("business", topics)
            self.assertIn("risk_factors", topics)
            self.assertIn("management_discussion", topics)
            self.assertTrue(all(item.evidence_id.startswith("filing:") for item in evidence))

    def test_extracts_bounded_numeric_tables_as_evidence(self) -> None:
        html = """
        <html><body>
          <table><tr><td>decorative</td></tr></table>
          <table>
            <tr><th>Year</th><th>Revenue</th><th>Margin</th></tr>
            <tr><td>2024</td><td>$100</td><td>20%</td></tr>
            <tr><td>2025</td><td>$125</td><td>24%</td></tr>
          </table>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tables.html"
            path.write_text(html, encoding="utf-8")
            filing = FilingDocument(
                document_id="test:tables",
                company_cik="0001",
                accession_number="test",
                form_type="10-K",
                fiscal_period="FY2025",
                period_end="2025-12-31",
                filed_at="2026-02-01",
                primary_document="tables.html",
                source_url="https://example.test/tables",
                local_path=str(path),
            )
            tables = extract_table_evidence(filing)
            self.assertEqual(len(tables), 1)
            self.assertIn("Revenue", tables[0].excerpt)
            self.assertEqual(tables[0].locator, "table:2")
            combined = build_filing_evidence([filing])
            self.assertTrue(
                any(item["evidence_id"].startswith("table:") for item in combined)
            )


if __name__ == "__main__":
    unittest.main()
