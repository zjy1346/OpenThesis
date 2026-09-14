from __future__ import annotations

import unittest

from openthesis.markets import (
    AccountingStandard,
    Exchange,
    IndustrySupport,
    Market,
    build_company,
    industry_support,
    normalize_symbol,
    search_companies,
)


class MarketDomainTests(unittest.TestCase):
    def test_symbols_resolve_across_every_1_2_launch_exchange(self) -> None:
        cases = {
            "600519.SH": ("600519.SH", Exchange.SSE, Market.CN_A),
            "000858.SZ": ("000858.SZ", Exchange.SZSE, Market.CN_A),
            "832982.BJ": ("832982.BJ", Exchange.BSE, Market.CN_A),
            "700.HK": ("00700.HK", Exchange.HKEX, Market.HK),
        }
        for symbol, expected in cases.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(normalize_symbol(symbol), expected)

    def test_plain_codes_respect_the_selected_market(self) -> None:
        self.assertEqual(normalize_symbol("600519", Market.CN_A)[1], Exchange.SSE)
        self.assertEqual(normalize_symbol("000858", Market.CN_A)[1], Exchange.SZSE)
        self.assertEqual(normalize_symbol("832982", Market.CN_A)[1], Exchange.BSE)
        self.assertEqual(normalize_symbol("700", Market.HK)[0], "00700.HK")

    def test_common_prefixed_a_share_and_us_share_class_symbols_are_normalized(self) -> None:
        cases = {
            "sh600519": ("600519.SH", Exchange.SSE, Market.CN_A),
            "sz000001": ("000001.SZ", Exchange.SZSE, Market.CN_A),
            "bj832982": ("832982.BJ", Exchange.BSE, Market.CN_A),
            "BRK/B": ("BRK.B", Exchange.NASDAQ, Market.US),
            "BRK-B": ("BRK.B", Exchange.NASDAQ, Market.US),
        }
        for symbol, expected in cases.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(normalize_symbol(symbol), expected)

    def test_build_company_sets_currency_standard_and_stable_ids(self) -> None:
        company = build_company("600519", "贵州茅台", market="CN_A")
        self.assertEqual(company.cik, "CN_A:SSE:600519.SH")
        self.assertEqual(company.security_id, company.cik)
        self.assertEqual(company.market, Market.CN_A.value)
        self.assertEqual(company.listing_currency, "CNY")
        self.assertEqual(company.accounting_standard, AccountingStandard.CAS.value)

    def test_financial_companies_are_explicitly_beta(self) -> None:
        self.assertEqual(industry_support("招商银行"), IndustrySupport.FINANCIAL_BETA)
        self.assertEqual(industry_support("AIA", "Insurance"), IndustrySupport.FINANCIAL_BETA)
        for name in (
            "中国平安", "中国人寿", "中国太保", "JPMorgan Chase & Co.",
            "The Goldman Sachs Group, Inc.", "Morgan Stanley", "Berkshire Hathaway Inc.",
        ):
            with self.subTest(name=name):
                self.assertEqual(industry_support(name), IndustrySupport.FINANCIAL_BETA)
        self.assertEqual(industry_support("贵州茅台"), IndustrySupport.STANDARD)
        self.assertEqual(industry_support("Ping An Healthcare and Technology"), IndustrySupport.STANDARD)

    def test_financial_industry_codes_and_multilingual_aliases_route_without_company_hardcoding(self) -> None:
        for industry in ("GICS:40", "CSRC:J66", "SIC:6021", "NAICS:522110", "資產管理", "financial services"):
            with self.subTest(industry=industry):
                self.assertEqual(industry_support("Unlisted Example", industry), IndustrySupport.FINANCIAL_BETA)
        self.assertEqual(industry_support("Bankable Software", "software"), IndustrySupport.STANDARD)
        self.assertEqual(
            industry_support("Unclassified issuer", identifier="JPM"),
            IndustrySupport.FINANCIAL_BETA,
        )

    def test_known_dual_listing_shares_issuer_but_not_security(self) -> None:
        a_share = search_companies("300750", market="CN_A")[0]
        h_share = search_companies("03750", market="HK")[0]
        self.assertEqual(a_share.issuer_id, h_share.issuer_id)
        self.assertNotEqual(a_share.security_id, h_share.security_id)

    def test_unknown_valid_symbol_can_be_entered_manually(self) -> None:
        company = search_companies("920001", market="CN_A")[0]
        self.assertEqual(company.exchange, Exchange.BSE.value)
        self.assertEqual(company.ticker, "920001.BJ")


if __name__ == "__main__":
    unittest.main()
