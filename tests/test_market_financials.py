from __future__ import annotations

import unittest

from openthesis.domain import FilingDocument
from openthesis.market_financials import financial_quality_issues, parse_financial_pages
from openthesis.markets import build_company


class MarketFinancialNormalizationTests(unittest.TestCase):
    def test_cn_annual_values_keep_currency_unit_and_page_evidence(self) -> None:
        company = build_company("300750.SZ", "CATL")
        filing = _filing(company.security_id)
        facts, evidence = parse_financial_pages(
            [
                (
                    88,
                    "单位：百万元\n营业收入 362,013 400,917\n营业利润 50,941 44,120\n"
                    "归属于上市公司股东的净利润 44,121 39,100\n"
                    "经营活动产生的现金流量净额 62,700 52,000\n"
                    "购建固定资产、无形资产和其他长期资产支付的现金 38,100 31,000",
                )
            ],
            filing,
            company,
        )
        by_concept = {item.concept: item for item in facts}
        self.assertEqual(by_concept["revenue"].value, 362_013_000_000)
        self.assertEqual(by_concept["revenue"].unit, "CNY")
        self.assertEqual(by_concept["capital_expenditure"].value, 38_100_000_000)
        self.assertTrue(all(item.locator == "page:88" for item in evidence))
        self.assertTrue(all(item.source_url == filing.source_url for item in evidence))

    def test_hk_parentheses_are_negative_and_capex_is_positive(self) -> None:
        company = build_company("00700.HK", "Tencent")
        filing = _filing(company.security_id)
        facts, _ = parse_financial_pages(
            [(120, "HK$ million\nRevenue 660,257\nOperating profit (12,500)\nCapital expenditure (76,800)")],
            filing,
            company,
        )
        by_concept = {item.concept: item.value for item in facts}
        self.assertEqual(by_concept["revenue"], 660_257_000_000)
        self.assertEqual(by_concept["operating_income"], -12_500_000_000)
        self.assertEqual(by_concept["capital_expenditure"], 76_800_000_000)

    def test_unknown_lines_are_not_invented(self) -> None:
        company = build_company("832982.BJ", "Jinbo")
        facts, evidence = parse_financial_pages(
            [(1, "This page contains only an introduction and no financial table.")],
            _filing(company.security_id),
            company,
        )
        self.assertEqual(facts, [])
        self.assertEqual(evidence, [])

    def test_cn_statement_unit_is_inherited_and_narrative_equity_is_rejected(self) -> None:
        company = build_company("688981.SH", "SMIC")
        filing = _filing(company.security_id)
        facts, _ = parse_financial_pages(
            [
                (70, "本次激励对应权益总额的 90.00%，不代表资产负债表数据。"),
                (124, "合并资产负债表\n单位：千元 币种：人民币\n资产总计 367,718,196"),
                (
                    125,
                    "流动负债合计 46,627,600\n非流动负债合计 74,728,228\n负债合计 121,355,828",
                ),
                (
                    126,
                    "归属于母公司所有者权益合计 150,823,788\n所有者权益合计 246,362,368",
                ),
                (9, "加权平均净资产收益率（%） 3.4 2.8"),
            ],
            filing,
            company,
        )
        values = {item.concept: item.value for item in facts}
        self.assertEqual(values["assets"], 367_718_196_000)
        self.assertEqual(values["liabilities"], 121_355_828_000)
        self.assertEqual(values["equity"], 150_823_788_000)
        self.assertEqual(values["total_equity"], 246_362_368_000)
        self.assertAlmostEqual(values["reported_roe"], 0.034)
        self.assertEqual(financial_quality_issues(facts), [])

    def test_note_reference_and_split_roe_label_do_not_become_values(self) -> None:
        company = build_company("688981.SH", "SMIC")
        facts, _ = parse_financial_pages(
            [
                (
                    9,
                    "主要财务指标\n加权平均净资产收益率（%）\n(1)\n3.4 2.5 增加0.9个百分点 3.5",
                ),
                (
                    129,
                    "合并现金流量表\n单位：千元\n经营活动产生的现金流量净额 七、80 20,080,979 22,658,629",
                ),
            ],
            _filing(company.security_id),
            company,
        )
        values = {item.concept: item.value for item in facts}
        self.assertAlmostEqual(values["reported_roe"], 0.034)
        self.assertEqual(values["operating_cash_flow"], 20_080_979_000)

    def test_quality_gate_rejects_tiny_narrative_equity_and_missing_unit(self) -> None:
        company = build_company("688981.SH", "SMIC")
        filing = _filing(company.security_id)
        facts, _ = parse_financial_pages(
            [
                (1, "资产总计 367718196000\n负债合计 46627600"),
                (2, "归属于母公司所有者权益合计 90"),
            ],
            filing,
            company,
        )
        issues = financial_quality_issues(facts)
        self.assertIn("2025:implausible_liabilities_to_assets", issues)
        self.assertIn("2025:implausible_parent_equity", issues)

    def test_comprehensive_income_must_not_be_classified_as_revenue(self) -> None:
        company = build_company("688981.SH", "SMIC")
        facts, _ = parse_financial_pages(
            [(161, "年內綜合收益合計 – – (53,967) 1,817,942 1,763,975")],
            _filing(company.security_id),
            company,
        )

        self.assertNotIn("revenue", {item.concept for item in facts})

    def test_quality_gate_rejects_negative_revenue_and_extreme_growth_input(self) -> None:
        company = build_company("688981.SH", "SMIC")
        filing = _filing(company.security_id)
        facts, _ = parse_financial_pages(
            [
                (8, "主要会计数据\n单位：千元\n营业收入 -53,967"),
                (9, "资产总计 338,463,197\n负债合计 120,000,000\n所有者权益合计 218,463,197"),
            ],
            filing,
            company,
        )

        self.assertIn("2025:negative_revenue", financial_quality_issues(facts))

    def test_quality_gate_never_combines_annual_and_first_quarter_facts(self) -> None:
        company = build_company("688981.SH", "SMIC")
        annual = _filing(company.security_id)
        quarter = FilingDocument(
            document_id="official:q1-2026",
            company_cik=company.security_id,
            accession_number="q1-2026",
            form_type="QUARTERLY_REPORT",
            fiscal_period="Q1",
            period_end="2026-03-31",
            filed_at="2026-05-15T00:00:00+00:00",
            primary_document="2026 First Quarter Report",
            source_url="https://example.invalid/q1.pdf",
        )
        annual_facts, _ = parse_financial_pages(
            [(1, "单位：千元\n营业收入 57,795,570\n资产总计 367,718,196\n负债合计 121,355,828\n所有者权益合计 246,362,368")],
            annual,
            company,
        )
        quarter_facts, _ = parse_financial_pages(
            [(1, "单位：千元\n营业收入 16,010,000\n资产总计 380,000,000\n负债合计 125,000,000\n所有者权益合计 255,000,000")],
            quarter,
            company,
        )

        self.assertEqual(financial_quality_issues(annual_facts + quarter_facts), [])
        self.assertTrue(all(item.fiscal_period == "Q1" for item in quarter_facts))


def _filing(company_id: str) -> FilingDocument:
    return FilingDocument(
        document_id="official:annual-2025",
        company_cik=company_id,
        accession_number="annual-2025",
        form_type="ANNUAL_REPORT",
        fiscal_period="FY",
        period_end="2025-12-31",
        filed_at="2026-03-30T00:00:00+00:00",
        primary_document="2025 Annual Report",
        source_url="https://example.invalid/annual.pdf",
    )


if __name__ == "__main__":
    unittest.main()
