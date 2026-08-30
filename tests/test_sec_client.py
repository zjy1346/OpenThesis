from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import Company, FilingDocument, FinancialFact
from openthesis.download_safety import UnsafeDisclosurePayload, store_immutable_payload
from openthesis.sec_client import SecClient, SecFinancialSourceAdapter


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

    def test_download_does_not_trust_legacy_accession_cache_for_revision(self) -> None:
        filing = FilingDocument(
            "sec:revision", "0000001234", "0001-25-001", "10-K", "FY",
            "2025-12-31", "2026-02-01", "annual25.htm",
            "https://www.sec.gov/Archives/test/annual25.htm",
        )
        legacy = Path(self.temp.name) / "0001-25-001.htm"
        legacy.write_text("<html>original filing</html>", encoding="utf-8")
        revised = b"<html>corrected filing with a new official revision</html>"
        with patch.object(self.client, "_request_bytes", side_effect=[revised, revised]) as fetch:
            first = self.client.download_filing(filing, Path(self.temp.name))
            second = self.client.download_filing(filing, Path(self.temp.name))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "<html>original filing</html>")
        self.assertNotEqual(Path(first.local_path), legacy)
        self.assertEqual(Path(first.local_path).read_bytes(), revised)
        self.assertEqual(first.local_path, second.local_path)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_content_addressed_reuse_rejects_same_size_different_content(self) -> None:
        payload = b"official-content"
        target = Path(self.temp.name) / "accession.pdf"
        digest = __import__("hashlib").sha256(payload).hexdigest()
        destination = target.with_name(f"{target.stem}-{digest[:16]}{target.suffix}")
        destination.write_bytes(b"corrupt-content!")
        self.assertEqual(destination.stat().st_size, len(payload))
        with self.assertRaises(UnsafeDisclosurePayload):
            store_immutable_payload(target, payload)
        self.assertEqual(destination.read_bytes(), b"corrupt-content!")

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

    def test_foreign_private_issuer_20f_is_listed(self) -> None:
        submissions = {
            "filings": {"recent": {
                "form": ["20-F"], "accessionNumber": ["0001-26-001"],
                "primaryDocument": ["hsbc20f.htm"], "reportDate": ["2025-12-31"],
                "filingDate": ["2026-02-20"],
            }}
        }
        company = Company(cik="0001089113", ticker="HSBC", name="HSBC")
        with patch.object(self.client, "_get_json", return_value=submissions):
            filings = self.client.list_annual_filings(company)
        self.assertEqual(filings[0].form_type, "20-F")
        self.assertEqual(filings[0].fiscal_period, "FY")

    def test_ifrs_companyfacts_normalizes_core_with_provenance(self) -> None:
        def row(tag_value: float, *, start: str | None = "2025-01-01") -> dict[str, object]:
            return {"val": tag_value, "fy": 2025, "fp": "FY", "form": "20-F",
                    "start": start, "end": "2025-12-31", "filed": "2026-02-20",
                    "accn": "0001-26-001"}
        tags = {
            "Revenue": row(68_274_000_000),
            "ProfitLossAttributableToOwnersOfParent": row(21_102_000_000),
            "CashFlowsFromUsedInOperatingActivities": row(29_766_000_000),
            "Assets": row(3_233_034_000_000, start=None),
            "Liabilities": row(3_027_368_000_000, start=None),
            "EquityAttributableToOwnersOfParent": row(198_225_000_000, start=None),
            "Equity": row(205_666_000_000, start=None),
        }
        payload = {"facts": {"ifrs-full": {name: {"units": {"USD": [value]}} for name, value in tags.items()}, "dei": {}}}
        company = Company(cik="0001089113", ticker="HSBC", name="HSBC", reporting_currency="USD", accounting_standard="IFRS")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        by_concept = {fact.concept: fact for fact in facts}
        self.assertEqual(set(("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity", "total_equity")), set(by_concept))
        self.assertEqual(by_concept["net_income"].statement, "income_statement")
        self.assertEqual(by_concept["assets"].period_start, None)
        self.assertEqual(by_concept["revenue"].currency, "USD")
        self.assertIn("ifrs-full", by_concept["revenue"].raw_text)
        self.assertEqual(by_concept["revenue"].parser_version, "sec-companyfacts-v2")

    def test_structured_adapter_remaps_period_and_keeps_sec_evidence(self) -> None:
        class FakeClient:
            def get_company_facts(self, company):
                return [FinancialFact(
                    "sec-fact", company.cik, "revenue", "Revenue", 10.0, "USD", 2025,
                    "FY", "20-F", "2025-01-01", "2025-12-31", "2026-02-20",
                    "0001-26-001", "https://www.sec.gov/Archives/edgar/data/1089113/0001-26-001/",
                    statement="income_statement", currency="USD", consolidated_scope="consolidated",
                    source_document="SEC CompanyFacts ifrs-full:Revenue", raw_text="ifrs-full:Revenue=10 USD",
                    parser_version="sec-companyfacts-v2",
                )]
        company = Company(cik="HK:SEHK:00005.HK", ticker="00005.HK", name="HSBC", market="HK", reporting_currency="USD")
        filing = FilingDocument("hk:2025", company.cik, "hk-2025", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-01", "Annual Report", "https://hk.example/report")
        facts, evidence, failure = SecFinancialSourceAdapter(FakeClient()).fetch(company, filing)
        self.assertIsNone(failure)
        self.assertEqual(facts[0].accession_number, "0001-26-001")
        self.assertEqual(evidence[0].locator, "accession:0001-26-001")
        self.assertIn("sec.gov", evidence[0].source_url)


if __name__ == "__main__":
    unittest.main()
