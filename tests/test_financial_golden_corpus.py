from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from openthesis.domain import Company, EvidenceRef, FilingDocument, FinancialFact
from openthesis.financial_compiler import (
    CandidateBatch,
    CompilerPolicy,
    FactCandidate,
    FinancialFactCompiler,
    InMemoryFactExtractor,
)


ROOT = Path(__file__).parent / "fixtures"
REQUIRED = {
    "revenue",
    "net_income",
    "operating_cash_flow",
    "assets",
    "liabilities",
    "equity",
}


def _expected_facts(period: dict) -> dict:
    """Return the per-period expectation map, defaulting unresolved fields to null."""
    return period.get("expected_facts") or {concept: None for concept in REQUIRED}


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate corpus key: {key}")
        result[key] = value
    return result


def _load_corpus() -> dict:
    path = ROOT / "official_financial_sources.json"
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)


def _load_evaluator():
    path = Path(__file__).parents[1] / "scripts" / "evaluate-financial-corpus.py"
    spec = importlib.util.spec_from_file_location("financial_corpus_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _complete_candidates(entry: dict, period: dict):
    year = int(period["fiscal_year"])
    end = period["period_end"]
    company = Company(
        entry["issuer_id"], entry["ticker"], entry["issuer"],
        market=entry["market"], reporting_currency=entry["currency"],
        listing_currency=entry["currency"], accounting_standard=entry["accounting_standard"],
    )
    filing = FilingDocument(
        f"golden:{entry['ticker']}:{year}", company.security_id,
        period["accession"], "ANNUAL_REPORT", "FY", end,
        f"{year + 1}-04-01", period["document"], period["source_url"],
        content_hash=f"golden-{entry['ticker']}-{year}",
    )
    candidates = []
    expected_facts = dict(period["expected_facts"])
    expected_facts.update(period.get("expected_supporting_facts", {}))
    for concept, value in expected_facts.items():
        if not isinstance(value, (int, float)):
            continue
        fact_id = f"golden:{entry['ticker']}:{year}:{concept}"
        statement = "balance_sheet" if concept in {"assets", "liabilities", "equity", "total_equity"} else (
            "cash_flow" if concept == "operating_cash_flow" else "income_statement"
        )
        fact = FinancialFact(
            fact_id, company.security_id, concept, concept, float(value),
            entry["currency"], year, "FY", "ANNUAL_REPORT",
            None if statement == "balance_sheet" else f"{year}-01-01", end,
            filing.filed_at, filing.accession_number, period["source_url"],
            scope="consolidated", entity=entry["issuer"], market=entry["market"],
            statement=statement, period_start=None if statement == "balance_sheet" else f"{year}-01-01",
            consolidated_scope="consolidated", currency=entry["currency"],
            unit_scale=float(period.get("unit_scale", entry["unit_scale"])), source_document=period["document"],
            source_page=(period.get("pages") or [1])[0],
            source_bbox=(0.0, 0.0, 100.0, 20.0), raw_text=period["source_excerpt"],
            parser_version="golden-corpus-fixture-v1",
        )
        ref = EvidenceRef(
            f"fact:{fact_id}", filing.document_id, period["source_url"],
            period["document"], f"page:{fact.source_page}", period["source_excerpt"],
            filing.filed_at, filing.content_hash, fact.source_bbox,
        )
        candidates.append(FactCandidate(fact, (ref,), "golden-corpus"))
    return company, filing, tuple(candidates)


class FinancialGoldenCorpusTests(unittest.TestCase):
    def test_corpus_has_six_issuers_and_five_contiguous_fy_records(self):
        corpus = _load_corpus()
        self.assertEqual(corpus["schema_version"], "2.2.1-golden-corpus.v1")
        self.assertEqual(
            {item["ticker"] for item in corpus["issuers"]},
            {"300750.SZ", "688981.SH", "00700.HK", "03690.HK", "AAPL", "MSFT"},
        )
        for issuer in corpus["issuers"]:
            with self.subTest(issuer=issuer["ticker"]):
                periods = issuer["periods"]
                self.assertEqual([item["fiscal_year"] for item in periods], [2021, 2022, 2023, 2024, 2025])
                self.assertEqual({item["fiscal_period"] for item in periods}, {"FY"})
                for period in periods:
                    self.assertTrue(period["source_url"].startswith("https://"))
                    self.assertTrue(period["accession"])
                    expected = _expected_facts(period)
                    self.assertEqual(set(expected), REQUIRED)
                    for value in expected.values():
                        self.assertTrue(value is None or isinstance(value, (int, float)))
                    self.assertEqual(period["resolution_status"], "CONFIRMED")
                    self.assertEqual(period.get("currency", issuer["currency"]), issuer["currency"])
                    self.assertEqual(period.get("scope", issuer["scope"]), "consolidated")
                    self.assertGreater(period.get("unit_scale", issuer["unit_scale"]), 0)
                    self.assertIn("source_excerpt", period)
                    self.assertEqual(
                        hashlib.sha256(period["source_excerpt"].encode("utf-8")).hexdigest(),
                        period["source_excerpt_sha256"],
                    )
                    self.assertTrue(all(
                        isinstance(value, (int, float))
                        for value in expected.values()
                    ))
                    self.assertTrue(period.get("pages"))

    def test_known_complete_rows_compile_through_canonical_gate(self):
        corpus = _load_corpus()
        complete = [
            (issuer, period)
            for issuer in corpus["issuers"]
            for period in issuer["periods"]
            if all(isinstance(value, (int, float)) for value in _expected_facts(period).values())
        ]
        self.assertGreaterEqual(len(complete), 5)
        for issuer, period in complete:
            with self.subTest(issuer=issuer["ticker"], year=period["fiscal_year"]):
                company, filing, candidates = _complete_candidates(issuer, period)
                dataset = FinancialFactCompiler().compile(
                    company, (filing.period_end, filing.period_end),
                    CompilerPolicy(
                        filings=(filing,), reporting_currency=issuer["currency"],
                        extractors=(InMemoryFactExtractor({filing.document_id: CandidateBatch(filing, candidates)}),),
                    ),
                )
                self.assertTrue(dataset.allow_ai)
                self.assertEqual(dataset.status, "VERIFIED")
                self.assertTrue(REQUIRED.issubset({fact.concept for fact in dataset.research_facts}))

    def test_all_selected_rows_are_confirmed_and_complete(self):
        corpus = _load_corpus()
        unresolved = [
            period for issuer in corpus["issuers"] for period in issuer["periods"]
            if period["resolution_status"] == "UNRESOLVED"
        ]
        self.assertEqual(unresolved, [])
        self.assertEqual(sum(len(issuer["periods"]) for issuer in corpus["issuers"]), 30)

    def test_sec_five_year_rows_are_confirmed_with_archive_urls_and_report_locators(self):
        corpus = _load_corpus()
        for ticker in ("AAPL", "MSFT"):
            issuer = next(item for item in corpus["issuers"] if item["ticker"] == ticker)
            self.assertEqual(len(issuer["periods"]), 5)
            for period in issuer["periods"]:
                with self.subTest(ticker=ticker, year=period["fiscal_year"]):
                    self.assertEqual(period["resolution_status"], "CONFIRMED")
                    self.assertNotIn("UNRESOLVED", period["accession"])
                    self.assertTrue(
                        period["source_url"].startswith(
                            "https://www.sec.gov/Archives/edgar/data/"
                        )
                    )
                    self.assertIn(
                        period["accession"].replace("-", ""), period["source_url"]
                    )
                    self.assertTrue(period["source_url"].endswith(".htm"))
                    expected = _expected_facts(period)
                    self.assertEqual(set(expected), REQUIRED)
                    self.assertTrue(all(isinstance(value, int) for value in expected.values()))
                    self.assertTrue(period.get("source_excerpt"))
                    self.assertEqual(
                        hashlib.sha256(period["source_excerpt"].encode("utf-8")).hexdigest(),
                        period["source_excerpt_sha256"],
                    )
                    self.assertTrue(period["pages"])
                    self.assertTrue(all(
                        isinstance(report, str) and report.startswith("R")
                        for report in period["pages"]
                    ))
                    excerpt_without_commas = period["source_excerpt"].replace(",", "")
                    for value in expected.values():
                        self.assertIn(str(value), excerpt_without_commas)

    def test_a_h_equity_keeps_parent_value_and_total_equity_supporting_value(self):
        corpus = _load_corpus()
        for ticker in ("300750.SZ", "688981.SH", "00700.HK", "03690.HK"):
            issuer = next(item for item in corpus["issuers"] if item["ticker"] == ticker)
            for period in issuer["periods"]:
                with self.subTest(ticker=ticker, year=period["fiscal_year"]):
                    expected = _expected_facts(period)
                    supporting = period.get("expected_supporting_facts", {})
                    self.assertIn("equity", expected)
                    self.assertIn("total_equity", supporting)
                    self.assertIn("equity attributable to parent", period["source_excerpt"])
                    self.assertIn("total equity", period["source_excerpt"])
                    self.assertNotEqual(expected["equity"], supporting["total_equity"])

    def test_a_h_total_equity_strictly_reconciles_assets_and_liabilities(self):
        corpus = _load_corpus()
        for ticker in ("300750.SZ", "688981.SH", "00700.HK", "03690.HK"):
            issuer = next(item for item in corpus["issuers"] if item["ticker"] == ticker)
            for period in issuer["periods"]:
                with self.subTest(ticker=ticker, year=period["fiscal_year"]):
                    expected = _expected_facts(period)
                    total_equity = period["expected_supporting_facts"]["total_equity"]
                    self.assertEqual(
                        expected["assets"] - expected["liabilities"] - total_equity,
                        0,
                    )

    def test_legacy_snapshots_use_corrected_smic_net_income_and_msft_filing(self):
        corpus = _load_corpus()
        smic = next(item for item in corpus["sources"] if item.get("ticker") == "688981.SH")
        self.assertEqual(smic["announcement_id"], "1212750692")
        self.assertEqual(smic["expected_core_facts"]["net_income"], 10733098)
        self.assertIn("10,733,098", smic["source_excerpt"])

        msft = next(
            item for item in corpus["sources"]
            if item.get("ticker") == "MSFT" and item.get("period") == "FY2024"
        )
        self.assertEqual(msft["accession"], "0000950170-24-087843")
        self.assertIn("000095017024087843", msft["source_url"])
        self.assertEqual(
            msft["expected_core_facts"],
            {
                "revenue": 245122000000,
                "net_income": 88136000000,
                "operating_cash_flow": 118548000000,
                "assets": 512163000000,
                "liabilities": 243686000000,
                "equity": 268477000000,
            },
        )

    def test_fixture_negative_candidates_do_not_bypass_gate(self):
        corpus = _load_corpus()
        issuer = next(item for item in corpus["issuers"] if item["ticker"] == "AAPL")
        period = next(item for item in issuer["periods"] if item["fiscal_year"] == 2024)
        company, filing, candidates = _complete_candidates(issuer, period)
        revenue = next(item for item in candidates if item.fact.concept == "revenue")
        wrong_revenue = FinancialFact(
            **{**revenue.fact.to_dict(), "fact_id": "golden:AAPL:2024:revenue:second-source",
               "value": revenue.fact.value + 1.0}
        )
        altered = candidates + (
            FactCandidate(wrong_revenue, revenue.evidence, "second-official-source"),
        )
        dataset = FinancialFactCompiler().compile(
            company, (filing.period_end, filing.period_end),
            CompilerPolicy(
                filings=(filing,), reporting_currency=issuer["currency"],
                extractors=(InMemoryFactExtractor({filing.document_id: CandidateBatch(filing, altered)}),),
            ),
        )
        self.assertFalse(dataset.allow_ai)
        self.assertTrue(dataset.conflicts or dataset.quarantined_facts)

    def test_evaluator_year_filter_selects_exact_period_without_running_parser(self):
        evaluator = _load_evaluator()
        original = evaluator._evaluate_bounded
        calls = []
        evaluator._evaluate_bounded = lambda issuer, period: (
            calls.append((issuer["ticker"], period["fiscal_year"]))
            or {"ticker": issuer["ticker"], "fiscal_year": period["fiscal_year"]}
        )
        try:
            result = evaluator.evaluate(ticker="AAPL", year=2024)
        finally:
            evaluator._evaluate_bounded = original
        self.assertEqual(calls, [("AAPL", 2024)])
        self.assertEqual(result["rows"], [{"ticker": "AAPL", "fiscal_year": 2024}])

    def test_evaluator_actual_fact_projection_is_safe_and_complete(self):
        evaluator = _load_evaluator()
        facts = evaluator._safe_actual_facts([SimpleNamespace(
            concept="revenue", value=123.45, currency="CNY", unit_scale=1000,
            source_page=7, consolidated_scope="consolidated", scope="parent",
            end_date="2025-12-31", raw_text="Revenue 123.45",
            local_path="SHOULD_NOT_BE_EXPOSED",
        )])
        self.assertEqual(facts[0]["concept"], "revenue")
        self.assertEqual(facts[0]["value"], "123.45")
        self.assertEqual(facts[0]["source_page"], 7)
        self.assertEqual(facts[0]["scope"], "consolidated")
        self.assertTrue(facts[0]["raw_excerpt_sha256"])
        self.assertNotIn("local_path", json.dumps(facts))
        self.assertNotIn("Revenue 123.45", json.dumps(facts))


if __name__ == "__main__":
    unittest.main()
