from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openthesis.market_data import CnInfoAdapter, HkexNewsAdapter, MarketDataError, MarketDataModule
from openthesis.markets import Exchange, Market


class _Transport:
    def __init__(self):
        self.downloaded: list[str] = []

    def get_json(self, url: str):
        if "szse_stock" in url:
            return {"stockList": [{"code": "300750", "zwjc": "宁德时代", "orgId": "9900023894"}]}
        if "sse_stock" in url:
            return {"stockList": [{"code": "600519", "zwjc": "贵州茅台", "orgId": "gssh0600519"}]}
        if "bse_stock" in url:
            return {"stockList": [{"code": "832982", "zwjc": "锦波生物", "orgId": "9900000001"}]}
        if "activestock" in url:
            return [{"stockId": 3888, "code": "00700", "name": "腾讯控股"}]
        raise AssertionError(url)

    def post_form(self, url: str, fields: dict[str, str]):
        if "topSearch" in url:
            catalogue = {
                "300750": {"code": "300750", "zwjc": "宁德时代", "orgId": "9900023894"},
                "600519": {"code": "600519", "zwjc": "贵州茅台", "orgId": "gssh0600519"},
                "832982": {"code": "832982", "zwjc": "锦波生物", "orgId": "9900000001"},
            }
            query = fields["keyWord"]
            return [item for code, item in catalogue.items() if query in code or query in item["zwjc"]]
        if "hisAnnouncement" in url:
            return {
                "announcements": [
                    {
                        "announcementId": "1212345678",
                        "announcementTitle": "宁德时代：2025年年度报告",
                        "announcementTime": 1774828800000,
                        "adjunctUrl": "finalpage/2026-03-30/1212345678.PDF",
                    }
                ]
            }
        raise AssertionError(url)

    def get_text(self, url: str) -> str:
        if "prefix.do" in url:
            return 'openthesis({"stockInfo":[{"stockId":7609,"code":"00700"}]});'
        if "titlesearch.xhtml" in url:
            return '<div>Annual Report <a href="/listedco/listconews/sehk/2026/0408/2026040800613.pdf">2025 Annual Report</a></div>'
        raise AssertionError(url)

    def download(self, url: str, target: Path) -> Path:
        self.downloaded.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"official disclosure fixture")
        return target


class MarketDataAdapterTests(unittest.TestCase):
    def test_cninfo_discovers_first_quarter_reports_separately(self) -> None:
        class FirstQuarterTransport(_Transport):
            def __init__(self) -> None:
                super().__init__()
                self.categories: list[str] = []

            def post_form(self, url: str, fields: dict[str, str]):
                if "topSearch" in url:
                    return super().post_form(url, fields)
                if "hisAnnouncement" in url:
                    category = fields.get("category", "")
                    self.categories.append(category)
                    if category == "category_ndbg_szsh;":
                        return {
                            "totalAnnouncement": 1,
                            "announcements": [{
                                "announcementId": "annual-2025",
                                "announcementTitle": "中芯国际2025年年度报告",
                                "announcementTime": 1774569600000,
                                "adjunctUrl": "finalpage/2026-03-27/annual-2025.PDF",
                            }],
                        }
                    if "category_yjdbg_szsh;" in category:
                        return {
                            "totalAnnouncement": 1,
                            "announcements": [{
                                "announcementId": "quarter-2026-q1",
                                "announcementTitle": "中芯国际2026年第一季度报告",
                                "announcementTime": 1778793600000,
                                "adjunctUrl": "finalpage/2026-05-15/quarter-2026-q1.PDF",
                            }],
                        }
                    return {"totalAnnouncement": 0, "announcements": []}
                return super().post_form(url, fields)

        transport = FirstQuarterTransport()
        adapter = CnInfoAdapter(transport)
        company = adapter.resolve("600519")[0]

        filings = adapter.list_financial_filings(company)

        self.assertIn("category_yjdbg_szsh;", "".join(transport.categories))
        q1 = next(item for item in filings if item.accession_number == "quarter-2026-q1")
        self.assertEqual(q1.fiscal_period, "Q1")
        self.assertEqual(q1.period_end, "2026-03-31")

    def test_cninfo_treats_explicit_zero_count_with_null_list_as_no_filings(self) -> None:
        class EmptyTransport(_Transport):
            def post_form(self, url: str, fields: dict[str, str]):
                if "hisAnnouncement" in url:
                    return {"totalAnnouncement": 0, "announcements": None}
                return super().post_form(url, fields)

        adapter = CnInfoAdapter(EmptyTransport())
        company = adapter.resolve("300750")[0]

        self.assertEqual(adapter.list_financial_filings(company), [])

    def test_cninfo_uses_prospectus_when_no_annual_report_exists(self) -> None:
        class NewListingTransport(_Transport):
            def post_form(self, url: str, fields: dict[str, str]):
                if "hisAnnouncement" in url and fields.get("searchkey") == "招股说明书":
                    return {
                        "announcements": [
                            {
                                "announcementId": "ipo-1",
                                "announcementTitle": "宁德时代：首次公开发行股票招股说明书",
                                "announcementTime": 1774828800000,
                                "adjunctUrl": "finalpage/2026-03-30/ipo-1.PDF",
                            }
                        ]
                    }
                if "hisAnnouncement" in url:
                    return {"totalAnnouncement": 0, "announcements": None}
                return super().post_form(url, fields)

        adapter = CnInfoAdapter(NewListingTransport())
        company = adapter.resolve("300750")[0]

        filings = adapter.list_financial_filings(company)

        self.assertEqual([item.form_type for item in filings], ["PROSPECTUS"])

    def test_cninfo_rejects_ambiguous_null_announcement_list(self) -> None:
        class AmbiguousTransport(_Transport):
            def post_form(self, url: str, fields: dict[str, str]):
                if "hisAnnouncement" in url:
                    return {"announcements": None}
                return super().post_form(url, fields)

        adapter = CnInfoAdapter(AmbiguousTransport())
        company = adapter.resolve("300750")[0]

        with self.assertRaises(MarketDataError) as raised:
            adapter.list_financial_filings(company)
        self.assertEqual(raised.exception.code, "FILING_STATUS_UNVERIFIED")

    def test_cninfo_rejects_nonempty_but_unsupported_announcement_results(self) -> None:
        class UnsupportedTransport(_Transport):
            def post_form(self, url: str, fields: dict[str, str]):
                if "hisAnnouncement" in url:
                    return {
                        "totalAnnouncement": 1,
                        "announcements": [{"announcementTitle": "临时公告"}],
                    }
                return super().post_form(url, fields)

        adapter = CnInfoAdapter(UnsupportedTransport())
        company = adapter.resolve("300750")[0]

        with self.assertRaises(MarketDataError) as raised:
            adapter.list_financial_filings(company)
        self.assertEqual(raised.exception.code, "FILING_FORMAT_UNSUPPORTED")

    def test_cninfo_covers_shenzhen_shanghai_and_beijing(self) -> None:
        adapter = CnInfoAdapter(_Transport())
        self.assertEqual(adapter.resolve("宁德")[0].exchange, Exchange.SZSE.value)
        self.assertEqual(adapter.resolve("600519")[0].exchange, Exchange.SSE.value)
        self.assertEqual(adapter.resolve("锦波")[0].exchange, Exchange.BSE.value)

    def test_cninfo_returns_traceable_financial_filings(self) -> None:
        adapter = CnInfoAdapter(_Transport())
        company = adapter.resolve("300750")[0]
        filing = adapter.list_financial_filings(company)[0]
        self.assertEqual(filing.form_type, "ANNUAL_REPORT")
        self.assertEqual(filing.company_cik, company.security_id)
        self.assertTrue(filing.source_url.startswith("https://static.cninfo.com.cn/"))

    def test_cninfo_paginates_before_selecting_annual_years(self) -> None:
        class PaginatedTransport(_Transport):
            def post_form(self, url: str, fields: dict[str, str]):
                if "hisAnnouncement" in url and fields.get("pageNum") == "1":
                    return {
                        "totalAnnouncement": 31,
                        "announcements": [
                            {
                                "announcementId": f"q-{index}",
                                "announcementTitle": f"宁德时代：2025年第三季度报告 {index}",
                                "announcementTime": 1774828800000 - index,
                                "adjunctUrl": f"finalpage/2026/q-{index}.PDF",
                            }
                            for index in range(30)
                        ],
                    }
                if "hisAnnouncement" in url and fields.get("pageNum") == "2":
                    return {
                        "announcements": [
                            {
                                "announcementId": "annual-2024",
                                "announcementTitle": "宁德时代：2024年年度报告",
                                "announcementTime": 1743292800000,
                                "adjunctUrl": "finalpage/2025/annual-2024.PDF",
                            }
                        ]
                    }
                return super().post_form(url, fields)

        adapter = CnInfoAdapter(PaginatedTransport())
        company = adapter.resolve("300750")[0]

        filings = adapter.list_financial_filings(company)

        self.assertIn("annual-2024", {item.accession_number for item in filings})

    def test_cninfo_queries_annual_reports_separately_from_periodic_noise(self) -> None:
        class AnnualFirstTransport(_Transport):
            def post_form(self, url: str, fields: dict[str, str]):
                if "hisAnnouncement" in url and fields.get("category") == "category_ndbg_szsh;":
                    return {
                        "totalAnnouncement": 1,
                        "announcements": [{
                            "announcementId": "native-annual-2023",
                            "announcementTitle": "中芯国际：2023年年度报告",
                            "announcementTime": 1711641600000,
                            "adjunctUrl": "finalpage/2024-03-29/native-annual-2023.PDF",
                        }],
                    }
                if "hisAnnouncement" in url:
                    return {
                        "totalAnnouncement": 1,
                        "announcements": [{
                            "announcementId": "h-share-noise",
                            "announcementTitle": "港股公告：2023年报",
                            "announcementTime": 1712707200000,
                            "adjunctUrl": "finalpage/2024-04-10/h-share-noise.PDF",
                        }],
                    }
                return super().post_form(url, fields)

        adapter = CnInfoAdapter(AnnualFirstTransport())
        company = adapter.resolve("600519")[0]

        filings = adapter.list_financial_filings(company)

        self.assertEqual([item.accession_number for item in filings], ["native-annual-2023"])

    def test_hkex_catalogue_and_report_results_are_normalized(self) -> None:
        adapter = HkexNewsAdapter(_Transport())
        company = adapter.resolve("腾讯")[0]
        filing = adapter.list_financial_filings(company)[0]
        self.assertEqual(company.ticker, "00700.HK")
        self.assertEqual(company.market, Market.HK.value)
        self.assertEqual(filing.form_type, "ANNUAL_REPORT")
        self.assertEqual(filing.source_url, "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0408/2026040800613.pdf")

    def test_download_is_bounded_to_the_adapter_target(self) -> None:
        transport = _Transport()
        adapter = CnInfoAdapter(transport)
        company = adapter.resolve("300750")[0]
        filing = adapter.list_financial_filings(company)[0]
        with tempfile.TemporaryDirectory() as directory:
            saved = adapter.download_filing(filing, Path(directory))
            self.assertTrue(Path(saved.local_path).exists())
            self.assertTrue(saved.content_hash)

    def test_module_exposes_one_interface_for_cn_and_hk_adapters(self) -> None:
        module = MarketDataModule(
            cn_adapter=CnInfoAdapter(_Transport()),
            hk_adapter=HkexNewsAdapter(_Transport()),
        )
        self.assertEqual(module.resolve("832982", "CN_A")[0].exchange, "BSE")
        self.assertEqual(module.resolve("00700", "HK")[0].exchange, "HKEX")


if __name__ == "__main__":
    unittest.main()
