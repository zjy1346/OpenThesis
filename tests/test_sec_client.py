from __future__ import annotations

import tempfile
import unittest
import urllib.error
from decimal import Decimal, localcontext
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import (
    Company, FilingDocument, FinancialFact,
    FinancialNormalizationLineage,
)
from openthesis.download_safety import UnsafeDisclosurePayload, store_immutable_payload
from openthesis.financial_contracts import SourceAmount
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

    def test_sec_json_parser_preserves_decimal_source_precision(self) -> None:
        raw_value = "9007199254740993.125"
        with patch.object(
            self.client, "_request_bytes",
            return_value=(f'{{"val":{raw_value}}}').encode("ascii"),
        ):
            payload = self.client._get_json("https://example.invalid/companyfacts")
        self.assertIsInstance(payload["val"], Decimal)
        self.assertEqual(str(payload["val"]), raw_value)

    def test_rate_limit_honors_retry_after_then_streams_response(self) -> None:
        class Response:
            headers = {}
            def __init__(self) -> None:
                self.parts = [b"ok", b""]
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1): return self.parts.pop(0)

        headers = Message()
        headers["Retry-After"] = "1"
        limited = urllib.error.HTTPError("https://sec.test", 429, "limited", headers, None)
        with patch("openthesis.sec_client.urllib.request.urlopen", side_effect=[limited, Response()]), \
             patch("openthesis.sec_client.time.sleep") as sleeper, \
             patch("openthesis.sec_client.random.uniform", return_value=0.0):
            payload = self.client._request_bytes("https://sec.test")
        self.assertEqual(payload, b"ok")
        sleeper.assert_called_with(1.0)

    def test_response_size_limit_stops_streaming(self) -> None:
        class Response:
            headers = {}
            def __init__(self) -> None: self.sent = False
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1):
                if self.sent: return b""
                self.sent = True
                return b"12345"

        client = SecClient(
            "OpenThesis test@example.com", Path(self.temp.name), min_interval=0,
            max_response_bytes=4,
        )
        with patch("openthesis.sec_client.urllib.request.urlopen", return_value=Response()):
            with self.assertRaisesRegex(Exception, "SEC_RESPONSE_TOO_LARGE"):
                client._request_bytes("https://sec.test")

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
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [1234, "Test Systems Inc.", "TEST", "Nasdaq"],
                [9999, "Other Corp.", "OTHER", "NYSE"],
            ],
        }
        with patch.object(self.client, "_get_json", return_value=tickers):
            matches = self.client.search_companies("test")
        self.assertEqual(matches[0].cik, "0000001234")
        self.assertEqual(matches[0].ticker, "TEST")
        self.assertEqual(matches[0].exchange, "NASDAQ")

        with patch.object(self.client, "_get_json", return_value={
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1067983, "Berkshire Hathaway Inc.", "BRK-B", "NYSE"]],
        }):
            share_class = self.client.search_companies("BRK/B")[0]
        self.assertEqual((share_class.ticker, share_class.exchange), ("BRK.B", "NYSE"))

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

    def test_annual_filings_reads_historical_submission_shards(self) -> None:
        recent = {
            "form": ["10-K", "10-K", "10-K"],
            "accessionNumber": ["0001-25-001", "0001-24-001", "0001-23-001"],
            "primaryDocument": ["annual25.htm", "annual24.htm", "annual23.htm"],
            "reportDate": ["2025-12-31", "2024-12-31", "2023-12-31"],
            "filingDate": ["2026-02-01", "2025-02-01", "2024-02-01"],
        }
        historical = {
            "form": ["10-K", "10-K"],
            "accessionNumber": ["0001-22-001", "0001-21-001"],
            "primaryDocument": ["annual22.htm", "annual21.htm"],
            "reportDate": ["2022-12-31", "2021-12-31"],
            "filingDate": ["2023-02-01", "2022-02-01"],
        }
        submissions = {
            "filings": {
                "recent": recent,
                "files": [{"name": "CIK0000001234-submissions-001.json"}],
            }
        }
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems")

        def payload(url, cache_name=None):
            if url.endswith("submissions/CIK0000001234.json"):
                return submissions
            self.assertTrue(url.endswith("CIK0000001234-submissions-001.json"))
            return {"filings": historical}

        with patch.object(self.client, "_get_json", side_effect=payload):
            filings = self.client.list_annual_filings(company, limit=5)
        self.assertEqual(
            [item.period_end for item in filings],
            ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31", "2021-12-31"],
        )
        self.assertEqual(len({item.accession_number for item in filings}), 5)

    def test_historical_shard_failure_keeps_recent_and_records_diagnostic(self) -> None:
        recent = {
            "form": ["10-K"], "accessionNumber": ["0001-25-001"],
            "primaryDocument": ["annual25.htm"], "reportDate": ["2025-12-31"],
            "filingDate": ["2026-02-01"],
        }
        submissions = {"filings": {"recent": recent, "files": [{"name": "CIK0000001234-submissions-001.json"}]}}
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems")
        with patch.object(self.client, "_get_json", side_effect=[submissions, OSError("offline")]):
            filings = self.client.list_annual_filings(company, limit=3)
        self.assertEqual([item.period_end for item in filings], ["2025-12-31"])
        self.assertTrue(any(item.startswith("historical_shard_failed:") for item in self.client.discovery_diagnostics))

    def test_company_search_supports_exact_chinese_alias_and_bounded_typo(self) -> None:
        tickers = {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [320193, "Apple Inc.", "AAPL", "Nasdaq"],
                [1045810, "NVIDIA Corporation", "NVDA", "Nasdaq"],
                [789019, "Microsoft Corporation", "MSFT", "Nasdaq"],
            ],
        }
        with patch.object(self.client, "_get_json", return_value=tickers):
            self.assertEqual(self.client.search_companies("苹果")[0].ticker, "AAPL")
            self.assertEqual(self.client.search_companies("nvida")[0].ticker, "NVDA")
            self.assertEqual(self.client.search_companies("msftx")[0].ticker, "MSFT")
            self.assertEqual(self.client.search_companies("aple")[0].ticker, "AAPL")
            self.assertEqual(self.client.search_companies("meta"), [])

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
        self.assertEqual(by_concept["parent_net_income"].value, 120)
        self.assertEqual(by_concept["parent_net_income"].reported_concept, "NetIncomeLoss")
        self.assertEqual(by_concept["revenue"].fiscal_year, 2025)
        self.assertEqual(by_concept["revenue"].unit_provenance, "structured_normalized")

    def test_companyfacts_keeps_parent_and_total_income_and_equity_tags_separate(self) -> None:
        def row(value: int, *, instant: bool = False) -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "10-K",
                "start": None if instant else "2025-01-01", "end": "2025-12-31",
                "filed": "2026-02-01", "accn": "0001-25-001",
            }

        payload = {"facts": {"us-gaap": {
            "NetIncomeLoss": {"units": {"USD": [row(120)]}},
            "ProfitLoss": {"units": {"USD": [row(150)]}},
            "StockholdersEquity": {"units": {"USD": [row(800, instant=True)]}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {
                "units": {"USD": [row(850, instant=True)]}
            },
        }, "dei": {}}}
        company = Company(
            cik="0000001234", ticker="TEST", name="Test Systems",
            reporting_currency="USD",
        )

        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)

        by_concept = {fact.concept: fact for fact in facts}
        self.assertEqual(
            set(by_concept),
            {"parent_net_income", "net_income", "equity", "total_equity"},
        )
        self.assertEqual(
            (by_concept["parent_net_income"].value,
             by_concept["parent_net_income"].reported_concept),
            (120, "NetIncomeLoss"),
        )
        self.assertEqual(
            (by_concept["net_income"].value, by_concept["net_income"].reported_concept),
            (150, "ProfitLoss"),
        )
        self.assertEqual(
            (by_concept["equity"].value, by_concept["equity"].reported_concept),
            (800, "StockholdersEquity"),
        )
        self.assertEqual(
            (by_concept["total_equity"].value,
             by_concept["total_equity"].reported_concept),
            (850, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        )

    def test_companyfacts_annual_flow_prefers_full_fiscal_year_over_later_quarter_fact(self) -> None:
        annual = {
            "val": 1000, "fy": 2020, "fp": "FY", "form": "10-K",
            "start": "2019-09-29", "end": "2020-09-26",
            "filed": "2020-10-30", "accn": "0001-20-001",
        }
        q4 = {
            **annual, "val": 250, "start": "2020-06-28",
            "filed": "2020-11-05", "accn": "0001-20-002",
        }
        payload = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [q4, annual]}},
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        revenue = [fact for fact in facts if fact.concept == "revenue"]
        self.assertEqual(len(revenue), 1)
        self.assertEqual((revenue[0].value, revenue[0].start_date), (1000, "2019-09-29"))

    def test_companyfacts_period_semantics_accepts_52_53_week_and_leap_year_flows(self) -> None:
        rows = [
            {"val": 52, "fy": 2024, "fp": "FY", "form": "10-K", "start": "2023-12-31", "end": "2024-12-28", "filed": "2025-02-01", "accn": "0001-25-001"},
            {"val": 53, "fy": 2025, "fp": "FY", "form": "10-K", "start": "2024-12-29", "end": "2026-01-03", "filed": "2026-02-01", "accn": "0001-26-001"},
            {"val": 366, "fy": 2020, "fp": "FY", "form": "10-K", "start": "2020-01-01", "end": "2020-12-31", "filed": "2021-02-01", "accn": "0001-21-001"},
        ]
        # A 53-week year can end in early January of the following calendar year;
        # SEC's `fy` still identifies the filer-designated fiscal year.
        from openthesis.sec_client import PeriodSemantics

        accepted = SecClient._select_annual_facts(rows, period_kind="duration")
        self.assertEqual({row["val"] for row in accepted}, {52, 53, 366})
        self.assertTrue(all(PeriodSemantics.for_row(row, "duration").complete for row in rows))

    def test_aapl_fy2021_revenue_uses_full_fy2020_not_fy_labeled_q4(self) -> None:
        # Regression for the archived AAPL report: CompanyFacts labels the
        # three-month Q4 row fp=FY and gives it the same fiscal-year end as
        # the full-year fact. The period itself, not that label, distinguishes it.
        q4_2020 = {
            "val": 64_698_000_000, "fy": 2020, "fp": "FY", "form": "10-K",
            "start": "2020-06-28", "end": "2020-09-26",
            "filed": "2020-10-30", "accn": "0000320193-20-000096",
        }
        fy2020 = {
            **q4_2020, "val": 274_515_000_000,
            "start": "2019-09-29", "end": "2020-09-26",
        }
        fy2021 = {
            "val": 365_817_000_000, "fy": 2021, "fp": "FY", "form": "10-K",
            "start": "2020-09-27", "end": "2021-09-25",
            "filed": "2021-10-29", "accn": "0000320193-21-000105",
        }
        company = Company(
            cik="0000320193", ticker="AAPL", name="Apple Inc.", reporting_currency="USD"
        )
        payload = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [q4_2020, fy2021, fy2020]}},
        }, "dei": {}}}
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)

        revenue = [fact for fact in facts if fact.concept == "revenue"]
        by_year = {fact.fiscal_year: fact for fact in revenue}
        self.assertEqual(set(by_year), {2020, 2021})
        self.assertEqual(by_year[2020].value, 274_515_000_000)
        self.assertEqual(by_year[2020].start_date, "2019-09-29")

        from openthesis.financials import calculate_metrics

        latest = calculate_metrics([fact.to_dict() for fact in facts])[0]
        expected_growth = 365_817_000_000 / 274_515_000_000 - 1
        self.assertAlmostEqual(latest["revenue_growth"], expected_growth, places=8)
        self.assertAlmostEqual(latest["revenue_growth"], 0.333, places=3)
        self.assertIn(by_year[2020].fact_id, latest["comparison_fact_ids"])

    def test_companyfacts_short_transition_duration_does_not_enter_annual_facts(self) -> None:
        transition = {
            "val": 200, "fy": 2025, "fp": "FY", "form": "10-K",
            "start": "2025-01-01", "end": "2025-06-30",
            "filed": "2025-08-15", "accn": "0001-25-001",
        }
        from openthesis.sec_client import PeriodSemantics

        semantics = PeriodSemantics.for_row(transition, "duration")
        self.assertEqual(semantics.kind, "short_duration")
        self.assertFalse(semantics.complete)
        self.assertEqual(SecClient._select_annual_facts([transition], period_kind="duration"), [])

    def test_companyfacts_instant_requires_fiscal_year_end_and_no_duration_start(self) -> None:
        fiscal_year_end = {
            "val": 100, "fy": 2025, "fp": "FY", "form": "10-K",
            "start": None, "end": "2025-09-27", "filed": "2025-11-01", "accn": "0001-25-001",
        }
        cover_date = {**fiscal_year_end, "val": 99, "end": "2026-01-28", "filed": "2026-01-30", "accn": "0001-26-001"}
        malformed_duration = {**fiscal_year_end, "val": 101, "start": "2025-07-01"}
        accepted = SecClient._select_annual_facts(
            [cover_date, malformed_duration, fiscal_year_end], period_kind="instant"
        )
        self.assertEqual(accepted, [fiscal_year_end])

    def test_companyfacts_instant_cover_date_must_match_verified_annual_period_end(self) -> None:
        annual_revenue = {
            "val": 1000, "fy": 2025, "fp": "FY", "form": "10-K",
            "start": "2024-02-01", "end": "2025-01-31",
            "filed": "2025-03-14", "accn": "0001-25-001",
        }
        year_end_shares = {
            "val": 100, "fy": 2025, "fp": "FY", "form": "10-K",
            "start": None, "end": "2025-01-31",
            "filed": "2025-03-14", "accn": "0001-25-001",
        }
        cover_date_shares = {
            **year_end_shares, "val": 200, "end": "2025-03-01",
        }
        payload = {"facts": {
            "us-gaap": {"Revenues": {"units": {"USD": [annual_revenue]}}},
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [cover_date_shares, year_end_shares]}}},
        }}
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        shares = [fact for fact in facts if fact.concept == "shares_outstanding"]
        self.assertEqual([(fact.value, fact.end_date) for fact in shares], [(100.0, "2025-01-31")])

    def test_companyfacts_restatement_and_duplicate_accession_choose_latest_complete_source(self) -> None:
        original = {
            "val": 1000, "fy": 2025, "fp": "FY", "form": "10-K",
            "start": "2025-01-01", "end": "2025-12-31",
            "filed": "2026-02-01", "accn": "0001-26-001",
        }
        amended = {**original, "val": 950, "form": "10-K/A", "filed": "2026-03-01", "accn": "0001-26-002"}
        duplicate = dict(original)
        accepted = SecClient._select_annual_facts(
            [original, duplicate, amended], period_kind="duration"
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual((accepted[0]["val"], accepted[0]["accn"]), (950, "0001-26-002"))

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

    def test_companyfacts_maps_operating_income_and_capex_statements(self) -> None:
        def row(value: int) -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "10-K",
                "start": "2025-01-01", "end": "2025-12-31",
                "filed": "2026-02-01", "accn": "0001-25-001",
            }

        payload = {"facts": {"us-gaap": {
            "OperatingIncomeLoss": {"units": {"USD": [row(300)]}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [row(40)]}},
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="TEST", name="Test Systems", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        by_concept = {fact.concept: fact for fact in facts}
        self.assertEqual(by_concept["operating_income"].statement, "income_statement")
        self.assertEqual(
            by_concept["capital_expenditure_ppe"].reported_concept,
            "PaymentsToAcquirePropertyPlantAndEquipment",
        )
        self.assertEqual(by_concept["capital_expenditure_ppe"].statement, "cash_flow")
        self.assertNotIn("capital_expenditure", by_concept)

    def test_companyfacts_derives_liabilities_with_explicit_nci_formula(self) -> None:
        def row(value: int, accession: str = "0001-25-001") -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "10-K",
                "start": None, "end": "2025-12-31", "filed": "2026-02-01",
                "accn": accession,
            }

        payload = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [row(100)]}},
            "StockholdersEquity": {"units": {"USD": [row(40)]}},
            "MinorityInterestInConsolidatedEntity": {"units": {"USD": [row(5)]}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {
                "units": {"USD": [row(45)]}
            },
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="KO", name="Example", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        liabilities = [fact for fact in facts if fact.concept == "liabilities"]
        self.assertEqual(len(liabilities), 1)
        self.assertEqual(liabilities[0].value, 55)
        self.assertIn("LiabilitiesAndStockholdersEquity - StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", liabilities[0].reported_concept)
        self.assertIn("inputs: LiabilitiesAndStockholdersEquity=100, StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest=45", liabilities[0].raw_text)
        self.assertEqual(liabilities[0].statement, "balance_sheet")
        self.assertEqual(liabilities[0].unit_provenance, "structured_normalized")
        self.assertEqual(liabilities[0].parser_version, "sec-companyfacts-v4")
        lineage = liabilities[0].normalization_lineage
        self.assertEqual(lineage.method, "sec-xbrl-arithmetic-v1")
        self.assertEqual(lineage.value_transform, "subtract")
        self.assertEqual(lineage.normalized_value, "55")
        self.assertEqual([item.raw_value for item in lineage.inputs], ["100", "45"])
        self.assertEqual(len(lineage.unit_evidence_ids), 2)

    def test_companyfacts_does_not_guess_liabilities_from_parent_equity(self) -> None:
        def row(value: int) -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "10-K",
                "start": None, "end": "2025-12-31", "filed": "2026-02-01",
                "accn": "0001-25-001",
            }

        payload = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [row(100)]}},
            "StockholdersEquity": {"units": {"USD": [row(40)]}},
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="KO", name="Example", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        self.assertFalse([fact for fact in facts if fact.concept == "liabilities"])

    def test_companyfacts_never_infers_zero_nci_from_multi_year_absence(self) -> None:
        def row(value: int, year: int) -> dict[str, object]:
            return {
                "val": value, "fy": year, "fp": "FY", "form": "10-K",
                "start": None, "end": f"{year}-12-31", "filed": f"{year + 1}-02-01",
                "accn": f"0001-{year}-001",
            }

        payload = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {
                "units": {"USD": [row(100 + year, year) for year in (2023, 2024, 2025)]}
            },
            "StockholdersEquity": {
                "units": {"USD": [row(40 + year, year) for year in (2023, 2024, 2025)]}
            },
        }, "dei": {}}}
        company = Company(cik="0001018724", ticker="AMZN", name="Amazon", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        liabilities = [fact for fact in facts if fact.concept == "liabilities"]
        self.assertEqual(liabilities, [])

    def test_companyfacts_does_not_apply_no_nci_history_path_when_any_nci_is_reported(self) -> None:
        def row(value: int, year: int) -> dict[str, object]:
            return {
                "val": value, "fy": year, "fp": "FY", "form": "10-K",
                "start": None, "end": f"{year}-12-31", "filed": f"{year + 1}-02-01",
                "accn": f"0001-{year}-001",
            }

        payload = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {
                "units": {"USD": [row(100, year) for year in (2024, 2025)]}
            },
            "StockholdersEquity": {
                "units": {"USD": [row(40, year) for year in (2024, 2025)]}
            },
            "MinorityInterestInConsolidatedEntity": {
                "units": {"USD": [row(5, 2025)]}
            },
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="NCI", name="NCI issuer", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        liabilities = [fact for fact in facts if fact.concept == "liabilities"]
        self.assertEqual([(fact.fiscal_year, fact.value) for fact in liabilities], [(2025, 55.0)])

    def test_companyfacts_derives_ko_style_nine_years_with_cross_validation(self) -> None:
        def row(value: int, year: int) -> dict[str, object]:
            return {
                "val": value, "fy": year, "fp": "FY", "form": "10-K",
                "start": None, "end": f"{year}-12-31", "filed": f"{year + 1}-02-01",
                "accn": f"0001-{year}-001",
            }

        tags: dict[str, dict[str, dict[str, list[dict[str, object]]]]] = {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": []}},
            "StockholdersEquity": {"units": {"USD": []}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {"units": {"USD": []}},
            "MinorityInterestInConsolidatedEntity": {"units": {"USD": []}},
            "LiabilitiesCurrent": {"units": {"USD": []}},
            "LiabilitiesNoncurrent": {"units": {"USD": []}},
        }
        for year in range(2017, 2026):
            total_equity = 625 + year
            liabilities = 375 + year
            tags["LiabilitiesAndStockholdersEquity"]["units"]["USD"].append(row(total_equity + liabilities, year))
            tags["StockholdersEquity"]["units"]["USD"].append(row(total_equity - 25, year))
            tags["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]["units"]["USD"].append(row(total_equity, year))
            tags["MinorityInterestInConsolidatedEntity"]["units"]["USD"].append(row(25, year))
            tags["LiabilitiesCurrent"]["units"]["USD"].append(row(200 + year, year))
            tags["LiabilitiesNoncurrent"]["units"]["USD"].append(row(175, year))
        payload = {"facts": {"us-gaap": tags, "dei": {}}}
        company = Company(cik="0000001234", ticker="KO", name="Coca-Cola", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        liabilities = {fact.fiscal_year: fact for fact in facts if fact.concept == "liabilities"}
        self.assertEqual(set(liabilities), set(range(2017, 2026)))
        self.assertTrue(all(fact.value == 375 + year for year, fact in liabilities.items()))

    def test_companyfacts_uses_parent_and_explicit_nci_as_reverse_path(self) -> None:
        def row(value: int) -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "10-K",
                "start": None, "end": "2025-12-31", "filed": "2026-02-01",
                "accn": "0001-25-001",
            }

        payload = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [row(100)]}},
            "StockholdersEquity": {"units": {"USD": [row(40)]}},
            "MinorityInterestInConsolidatedEntity": {"units": {"USD": [row(5)]}},
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="KO", name="Example", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        liabilities = [fact for fact in facts if fact.concept == "liabilities"]
        self.assertEqual(len(liabilities), 1)
        self.assertEqual(liabilities[0].value, 55)
        self.assertIn("- MinorityInterestInConsolidatedEntity", liabilities[0].reported_concept)

    def test_companyfacts_official_liabilities_wins_over_derived_paths(self) -> None:
        def row(value: int) -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "10-K",
                "start": None, "end": "2025-12-31", "filed": "2026-02-01",
                "accn": "0001-25-001",
            }

        payload = {"facts": {"us-gaap": {
            "Liabilities": {"units": {"USD": [row(55)]}},
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [row(100)]}},
            "StockholdersEquity": {"units": {"USD": [row(40)]}},
            "LiabilitiesCurrent": {"units": {"USD": [row(30)]}},
            "LiabilitiesNoncurrent": {"units": {"USD": [row(20)]}},
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="KO", name="Example", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)
        liabilities = [fact for fact in facts if fact.concept == "liabilities"]
        self.assertEqual(len(liabilities), 1)
        self.assertEqual(liabilities[0].value, 55)
        self.assertEqual(liabilities[0].reported_concept, "Liabilities")

    def test_companyfacts_rejects_cross_period_or_inconsistent_liability_derivation(self) -> None:
        def row(value: int, end: str, accession: str = "0001-25-001") -> dict[str, object]:
            return {
                "val": value, "fy": int(end[:4]), "fp": "FY", "form": "10-K",
                "start": None, "end": end, "filed": "2026-02-01", "accn": accession,
            }

        cross_period = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [row(100, "2025-12-31")]}},
            "StockholdersEquity": {"units": {"USD": [row(40, "2024-12-31", "0001-24-001")]}},
        }, "dei": {}}}
        company = Company(cik="0000001234", ticker="KO", name="Example", reporting_currency="USD")
        with patch.object(self.client, "_get_json", return_value=cross_period):
            facts = self.client.get_company_facts(company)
        self.assertFalse([fact for fact in facts if fact.concept == "liabilities"])

        def same(value: int, tag: str) -> dict[str, object]:
            return {"val": value, "fy": 2025, "fp": "FY", "form": "10-K", "start": None,
                    "end": "2025-12-31", "filed": "2026-02-01", "accn": "0001-25-001"}
        inconsistent = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [same(100, "a")]}},
            "StockholdersEquity": {"units": {"USD": [same(40, "b")]}},
            "MinorityInterestInConsolidatedEntity": {"units": {"USD": [same(5, "e")]}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {"units": {"USD": [same(45, "f")]}},
            "LiabilitiesCurrent": {"units": {"USD": [same(35, "c")] }},
            "LiabilitiesNoncurrent": {"units": {"USD": [same(30, "d")]}},
        }, "dei": {}}}
        with patch.object(self.client, "_get_json", return_value=inconsistent):
            facts = self.client.get_company_facts(company)
        self.assertFalse([fact for fact in facts if fact.concept == "liabilities"])

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
        self.assertEqual(set(("revenue", "parent_net_income", "operating_cash_flow", "assets", "liabilities", "equity", "total_equity")), set(by_concept))
        self.assertEqual(by_concept["parent_net_income"].statement, "income_statement")
        self.assertEqual(by_concept["parent_net_income"].reported_concept, "ProfitLossAttributableToOwnersOfParent")
        self.assertEqual(by_concept["assets"].period_start, None)
        self.assertEqual(by_concept["revenue"].currency, "USD")
        self.assertIn("ifrs-full", by_concept["revenue"].raw_text)
        self.assertEqual(by_concept["revenue"].parser_version, "sec-companyfacts-v4")
        self.assertEqual(by_concept["revenue"].normalization_lineage.raw_value, "68274000000")
        self.assertEqual(by_concept["revenue"].normalization_lineage.scale, "1")

    def test_ifrs_companyfacts_maps_operating_profit_and_capex_taxonomy_concepts(self) -> None:
        def row(value: float) -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "20-F",
                "start": "2025-01-01", "end": "2025-12-31", "filed": "2026-02-20",
                "accn": "0001-26-001",
            }

        payload = {"facts": {"ifrs-full": {
            "ProfitLossFromOperatingActivities": {"units": {"USD": [row(12_000)]}},
            "PurchaseOfPropertyPlantAndEquipment": {"units": {"USD": [row(3_000)]}},
        }, "dei": {}}}
        company = Company(
            cik="0001089113", ticker="IFRS", name="IFRS issuer",
            reporting_currency="USD", accounting_standard="IFRS",
        )

        with patch.object(self.client, "_get_json", return_value=payload):
            facts = self.client.get_company_facts(company)

        by_concept = {fact.concept: fact for fact in facts}
        self.assertEqual(by_concept["operating_income"].reported_concept, "ProfitLossFromOperatingActivities")
        self.assertEqual(by_concept["operating_income"].statement, "income_statement")
        self.assertEqual(by_concept["capital_expenditure_ppe"].reported_concept, "PurchaseOfPropertyPlantAndEquipment")
        self.assertEqual(by_concept["capital_expenditure_ppe"].statement, "cash_flow")
        self.assertNotIn("capital_expenditure", by_concept)

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
                    normalization_lineage=FinancialNormalizationLineage(
                        "10", "1", "10", "money", "USD", ("unit:sec-fact",)
                    ),
                )]
        company = Company(cik="HK:SEHK:00005.HK", ticker="00005.HK", name="HSBC", market="HK", reporting_currency="USD")
        filing = FilingDocument("hk:2025", company.cik, "hk-2025", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-01", "Annual Report", "https://hk.example/report")
        facts, evidence, failure = SecFinancialSourceAdapter(FakeClient()).fetch(company, filing)
        self.assertIsNone(failure)
        self.assertEqual(facts[0].accession_number, "0001-26-001")
        self.assertEqual(evidence[0].evidence_id, "fact:sec-fact")
        self.assertEqual(evidence[0].locator, "accession:0001-26-001")
        self.assertIn("sec.gov", evidence[0].source_url)
        self.assertEqual(evidence[1].evidence_id, "unit:sec-fact")
        self.assertTrue(evidence[1].locator.startswith("xbrl:"))


class SecDerivedEvidenceAdapterTests(unittest.TestCase):
    def test_companyfacts_derived_lineage_preserves_decimal_input_arithmetic(self):
        def row(value: Decimal) -> dict[str, object]:
            return {
                "val": value, "fy": 2025, "fp": "FY", "form": "10-K",
                "start": None, "end": "2025-12-31", "filed": "2026-02-01",
                "accn": "0001-25-001",
            }

        payload = {"facts": {"us-gaap": {
            "LiabilitiesAndStockholdersEquity": {"units": {"CNY": [row(Decimal("100.125"))]}},
            "StockholdersEquity": {"units": {"CNY": [row(Decimal("40"))]}},
            "MinorityInterestInConsolidatedEntity": {"units": {"CNY": [row(Decimal("5.025"))]}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {
                "units": {"CNY": [row(Decimal("45.025"))]}
            },
        }, "dei": {}}}
        company = Company(
            cik="0001577552", ticker="BABA", name="Alibaba",
            reporting_currency="CNY", accounting_standard="US_GAAP",
        )
        client = object.__new__(SecClient)
        with localcontext() as context, patch.object(client, "_get_json", return_value=payload):
            context.prec = 3
            facts = client.get_company_facts(company)
        liabilities = [fact for fact in facts if fact.concept == "liabilities"]
        self.assertEqual(len(liabilities), 1)
        lineage = liabilities[0].normalization_lineage
        self.assertEqual(lineage.normalized_value, "55.100")
        self.assertEqual([item.raw_value for item in lineage.inputs], ["100.125", "45.025"])
        self.assertEqual(lineage.value_transform, "subtract")
        self.assertEqual(liabilities[0].parser_version, "sec-companyfacts-v4")

    def test_derived_liability_exposes_each_formula_input_unit_reference(self):
        accession = "0001-25-001"
        end_date = "2025-12-31"
        source_url = "https://www.sec.gov/Archives/edgar/data/1577552/000125001/"
        unit_ids = (
            "unit:sec-input:0001577552:us-gaap:LiabilitiesAndStockholdersEquity:CNY:0001-25-001:2025-12-31",
            "unit:sec-input:0001577552:us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest:CNY:0001-25-001:2025-12-31",
        )
        inputs = (
            SourceAmount("100", "1", "money", "CNY", (unit_ids[0],)).normalize(),
            SourceAmount("45", "1", "money", "CNY", (unit_ids[1],)).normalize(),
        )
        fact = FinancialFact(
            fact_id="derived-liabilities", company_cik="0001577552", concept="liabilities",
            reported_concept="derived liabilities (total equity less NCI)", value=55.0,
            unit="CNY", fiscal_year=2025, fiscal_period="FY", form_type="20-F",
            start_date=None, end_date=end_date, filed_at="2026-02-01",
            accession_number=accession, source_url=source_url, statement="balance_sheet",
            currency="CNY", unit_scale=1.0, unit_provenance="structured_normalized",
            source_document="SEC CompanyFacts us-gaap:derived-liabilities",
            raw_text="Derived liabilities = 100 - 45", parser_version="sec-companyfacts-v3",
            normalization_lineage=FinancialNormalizationLineage(
                "", "1", "55", "money", "CNY", unit_ids,
                method="sec-xbrl-arithmetic-v1", value_transform="subtract", inputs=inputs,
            ),
        )

        class FakeClient:
            def get_company_facts(self, _company):
                return [fact]

        company = Company(
            cik="HK:SEHK:09988.HK", ticker="09988.HK", name="Alibaba",
            market="HK", reporting_currency="CNY",
        )
        filing = FilingDocument(
            "hk:09988:2025", company.cik, accession, "ANNUAL_REPORT", "FY",
            end_date, "2026-02-01", "Annual Report", "https://hk.example/report",
        )
        facts, evidence, failure = SecFinancialSourceAdapter(FakeClient()).fetch(company, filing)
        self.assertIsNone(failure)
        self.assertEqual(facts, [fact])
        input_refs = [ref for ref in evidence if ref.evidence_id in unit_ids]
        self.assertEqual(len(input_refs), 2)
        self.assertTrue(all(ref.locator.startswith("xbrl:us-gaap/") for ref in input_refs))
        self.assertTrue(any("LiabilitiesAndStockholdersEquity" in ref.locator for ref in input_refs))
        self.assertTrue(any("StockholdersEquityIncludingPortion" in ref.locator for ref in input_refs))


if __name__ == "__main__":
    unittest.main()
