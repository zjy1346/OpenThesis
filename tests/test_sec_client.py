from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import Company
from openthesis.sec_client import SecClient


class SecClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = SecClient(
            "OpenThesis test@example.com",
            Path(self.temp.name),
            min_interval=0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_requires_contact_email(self) -> None:
        with self.assertRaises(ValueError):
            SecClient("missing-contact", Path(self.temp.name))

    def test_company_search_and_annual_filing_mapping(self) -> None:
        tickers = {
            "0": {"cik_str": 1234, "ticker": "TEST", "title": "Test Systems Inc."},
            "1": {"cik_str": 9999, "ticker": "OTHER", "title": "Other Corp."},
        }
        with patch.object(self.client, "_get_json", return_value=tickers):
            matches = self.client.search_companies("test")
        self.assertEqual(matches[0].cik, "0000001234")
        self.assertEqual(matches[0].ticker, "TEST")

        submissions = {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-K", "10-K"],
                    "accessionNumber": ["a", "0001-25-001", "0001-24-001"],
                    "primaryDocument": ["a.htm", "annual25.htm", "annual24.htm"],
                    "reportDate": ["2025-11-01", "2025-12-31", "2024-12-31"],
                    "filingDate": ["2025-11-02", "2026-02-01", "2025-02-01"],
                }
            }
        }
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems")
        with patch.object(self.client, "_get_json", return_value=submissions):
            filings = self.client.list_annual_filings(company, limit=2)
        self.assertEqual(len(filings), 2)
        self.assertEqual(filings[0].form_type, "10-K")
        self.assertIn("annual25.htm", filings[0].source_url)

    def test_company_facts_normalization(self) -> None:
        payload = {
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "val": 1000,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-01",
                                    "accn": "0001-25-001",
                                }
                            ]
                        }
                    },
                    "NetIncomeLoss": {
                        "units": {
                            "USD": [
                                {
                                    "val": 120,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-01",
                                    "accn": "0001-25-001",
                                }
                            ]
                        }
                    },
                },
                "dei": {},
            }
        }
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        by_concept = {fact.concept: fact for fact in facts}
        self.assertEqual(by_concept["revenue"].value, 1000)
        self.assertEqual(by_concept["net_income"].value, 120)
        self.assertEqual(by_concept["revenue"].fiscal_year, 2025)

    def test_alternate_xbrl_concept_fills_missing_years(self) -> None:
        def row(year: int, value: int) -> dict[str, object]:
            return {
                "val": value,
                "fy": year,
                "fp": "FY",
                "form": "10-K",
                "start": f"{year}-01-01",
                "end": f"{year}-12-31",
                "filed": f"{year + 1}-02-01",
                "accn": f"0001-{str(year)[-2:]}-001",
            }

        payload = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [row(2025, 1200)]}
                    },
                    "Revenues": {
                        "units": {
                            "USD": [
                                row(2025, 9999),
                                row(2024, 900),
                            ]
                        }
                    },
                },
                "dei": {},
            }
        }
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        revenue = {
            fact.fiscal_year: (fact.value, fact.reported_concept)
            for fact in facts
            if fact.concept == "revenue"
        }
        self.assertEqual(
            revenue[2025],
            (1200, "RevenueFromContractWithCustomerExcludingAssessedTax"),
        )
        self.assertEqual(revenue[2024], (900, "Revenues"))


if __name__ == "__main__":
    unittest.main()
