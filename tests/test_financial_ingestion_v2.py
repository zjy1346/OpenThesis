from __future__ import annotations

import unittest
import json
import hashlib
from pathlib import Path

from openthesis.domain import FilingDocument
from openthesis.financial_ingestion import OfficialSource, ValidationStatus, parse_financial_pages, parse_structured_snapshot, prepare_facts_for_ai, validate_financial_facts
from openthesis.market_financials import _period_start
from openthesis.markets import build_company


class FinancialIngestionV2Tests(unittest.TestCase):
    def test_rejected_quality_group_never_reaches_provider_boundary(self) -> None:
        called = False
        def provider_factory(_facts: list[object]) -> None:
            nonlocal called
            called = True
        facts = []
        accepted, validation = prepare_facts_for_ai(facts)
        if validation.status is not ValidationStatus.REJECTED:
            provider_factory(accepted)
        self.assertEqual(accepted, [])
        self.assertEqual(validation.status, ValidationStatus.REJECTED)
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
                (100, "合并利润表\n单位：万元\n营业收入 12,345\n净利润 1,234"),
                (101, "合并现金流量表\n单位：万元\n经营活动产生的现金流量净额 2,345"),
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
        self.assertEqual(values["revenue"].parser_version, "financial-ingestion-v2")
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
        )
        facts, _ = parse_financial_pages([(3, "合并利润表\n单位：元\n营业收入 1,000")], filing, company)
        result = validate_financial_facts(facts)
        self.assertEqual(result.status, ValidationStatus.REJECTED)
        self.assertFalse(result.allow_ai)
        self.assertIn("core_coverage_insufficient", result.issues)

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
