from __future__ import annotations

import tempfile
import unittest
import json
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from openthesis.download_safety import store_immutable_payload
from openthesis.market_data import (
    CnInfoAdapter,
    HkexNewsAdapter,
    MarketDataError,
    MarketDataModule,
    OfficialDisclosureHttpClient,
    _classify_report,
    _hkex_filings_from_json,
    _hkex_filings_from_text,
    _hkex_json_rows,
)
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


class _HkexJsonTransport(_Transport):
    """Bounded servlet fixture mirroring official nested/list result variants."""

    def __init__(self) -> None:
        super().__init__()
        self.search_urls: list[str] = []

    def get_json(self, url: str):
        if "titleSearchServlet.do" not in url:
            return super().get_json(url)
        self.search_urls.append(url)
        title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("title", [""])[0]
        annual = [
            {
                "DATE_TIME": f"{28 - index:02d}/03/{year} 12:00",
                "TITLE": f"202{year - 2020} Annual Report",
                "FILE_LINK": f"/listedco/listconews/sehk/{year + 1}/0328/annual-{year}.pdf",
            }
            for index, year in enumerate(range(2025, 2020, -1))
        ]
        if title == "Annual Report":
            return {"result": json.dumps(annual)}
        if title in {"Interim Report", "Half-Year Report"}:
            return {
                "result": [
                    {
                        "DATE_TIME": "28/08/2026 09:00",
                        "TITLE": "Interim Report 2026",
                        "FILE_LINK": "/listedco/listconews/sehk/2026/0828/interim-2026.pdf",
                    }
                ]
            }
        return {"result": []}


class MarketDataAdapterTests(unittest.TestCase):
    def test_cninfo_one_quarter_alias_maps_to_q1_period_end(self) -> None:
        self.assertEqual(_classify_report("宁德时代2026年一季度报告"), ("QUARTERLY_REPORT", "Q1"))
        self.assertEqual(_classify_report("宁德时代2026年1季度报告"), ("QUARTERLY_REPORT", "Q1"))
        self.assertEqual(_classify_report("宁德时代2026年三季度报告"), ("QUARTERLY_REPORT", "Q3"))

    def test_hkex_english_quarterly_aliases_map_to_period_end(self) -> None:
        self.assertEqual(_classify_report("2026 FIRST QUARTERLY REPORT"), ("QUARTERLY_REPORT", "Q1"))
        self.assertEqual(_classify_report("2026 1ST QUARTERLY REPORT"), ("QUARTERLY_REPORT", "Q1"))
        self.assertEqual(_classify_report("2025 THIRD QUARTERLY REPORT"), ("QUARTERLY_REPORT", "Q3"))
        self.assertEqual(_classify_report("EARNINGS RELEASE FOR FIRST QUARTER 2026"), ("", ""))

        company = CnInfoAdapter(_Transport()).resolve("300750")[0]
        filings = _hkex_filings_from_json(
            company,
            [
                {
                    "DATE_TIME": "28/04/2026 09:00",
                    "TITLE": "2026 FIRST QUARTERLY REPORT",
                    "FILE_LINK": "/listedco/listconews/sehk/2026/0428/byd-q1-2026.pdf",
                },
                {
                    "DATE_TIME": "28/10/2025 09:00",
                    "TITLE": "2025 THIRD QUARTERLY REPORT",
                    "FILE_LINK": "/listedco/listconews/sehk/2025/1028/byd-q3-2025.pdf",
                },
            ],
        )
        self.assertEqual(
            [(item.fiscal_period, item.period_end) for item in filings],
            [("Q1", "2026-03-31"), ("Q3", "2025-09-30")],
        )

    def test_hkex_link_context_strips_markup_and_uses_title_year(self) -> None:
        company = CnInfoAdapter(_Transport()).resolve("300750")[0]
        text = '<div class="doc-link"><a href="/listedco/listconews/sehk/2026/0408/2026040800613.pdf">2025 Annual Report</a></div>'
        filing = _hkex_filings_from_text(company, text, limit=1)[0]
        self.assertEqual(filing.primary_document, "2025 Annual Report")
        self.assertEqual(filing.period_end, "2025-12-31")
        self.assertNotIn("<", filing.primary_document)

    def test_hkex_annual_period_uses_explicit_non_calendar_year_end(self) -> None:
        company = CnInfoAdapter(_Transport()).resolve("300750")[0]
        filings = _hkex_filings_from_json(
            company,
            [
                {
                    "DATE_TIME": "08/04/2026 09:00",
                    "TITLE": "2026 Annual Report for the year ended 31 March 2025",
                    "FILE_LINK": "/listedco/listconews/sehk/2026/0408/annual-2025-mar.pdf",
                }
            ],
        )
        self.assertEqual(filings[0].period_end, "2025-03-31")
        self.assertEqual(filings[0].revision, "period_end_verified")

    def test_hkex_anchor_titles_ignore_earnings_release_neighbors(self) -> None:
        company = CnInfoAdapter(_Transport()).resolve("300750")[0]
        text = (
            '<div><a href="/listedco/listconews/sehk/2026/0219/2026021900123.pdf">'
            'Annual Report and Accounts 2025 (with employee share plans)</a></div>'
            '<div><a href="/listedco/listconews/sehk/2026/0508/2026050800456.pdf">'
            'EARNINGS RELEASE FOR FIRST QUARTER 2026</a></div>'
        )
        filings = _hkex_filings_from_text(company, text, limit=5)
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0].primary_document, "Annual Report and Accounts 2025 (with employee share plans)")
        self.assertEqual(filings[0].period_end, "2025-12-31")

    def test_hkex_json_nested_result_selects_five_annuals_and_periodic(self) -> None:
        transport = _HkexJsonTransport()
        adapter = HkexNewsAdapter(transport)
        company = adapter.resolve("00700")[0]

        filings = adapter.list_financial_filings(company, limit=5)

        annuals = [item for item in filings if item.form_type == "ANNUAL_REPORT"]
        self.assertEqual({item.period_end[:4] for item in annuals}, {"2021", "2022", "2023", "2024", "2025"})
        self.assertTrue(any(item.fiscal_period == "H1" for item in filings))
        self.assertTrue(transport.search_urls)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(transport.search_urls[0]).query)
        self.assertEqual(query["rowRange"], ["100"])
        self.assertEqual(query["title"], ["Annual Report"])
        for key in ("stockId", "fromDate", "toDate", "documentType", "sortByOptions", "lang"):
            self.assertIn(key, query)
        self.assertRegex(query["fromDate"][0], r"^20\d{6}$")
        self.assertRegex(query["toDate"][0], r"^20\d{6}$")
        self.assertTrue(query["fromDate"][0].endswith("0101"))
        self.assertNotIn("/", query["fromDate"][0] + query["toDate"][0])
        self.assertNotIn("titlesearch.xhtml", transport.search_urls[0])

    def test_hkex_discovery_keeps_n_plus_one_annual_comparator(self) -> None:
        adapter = HkexNewsAdapter(_HkexJsonTransport())
        company = adapter.resolve("00700")[0]

        filings = adapter.list_financial_filings(company, limit=2)

        annuals = [item for item in filings if item.form_type == "ANNUAL_REPORT"]
        self.assertEqual([item.period_end[:4] for item in annuals], ["2025", "2024", "2023"])

    def test_hkex_json_rows_support_list_and_malformed_distinction(self) -> None:
        rows = [{"TITLE": "Annual Report 2025", "FILE_LINK": "/listedco/listconews/sehk/2026/0328/a.pdf", "DATE_TIME": "28/03/2026 10:30"}]
        self.assertEqual(_hkex_json_rows({"result": rows}), rows)
        self.assertEqual(_hkex_json_rows({"result": json.dumps(rows)}), rows)
        self.assertEqual(_hkex_json_rows({"result": []}), [])
        self.assertIsNone(_hkex_json_rows({"result": "not-json"}))
        self.assertIsNone(_hkex_json_rows({"result": ["unexpected"]}))

        company = CnInfoAdapter(_Transport()).resolve("300750")[0]
        filings = _hkex_filings_from_json(
            company,
            rows + [
                # Duplicate URL is ignored, and a neighbouring earnings
                # release is not a report candidate.
                rows[0],
                {"TITLE": "EARNINGS RELEASE FOR FIRST QUARTER 2026", "FILE_LINK": "/listedco/listconews/sehk/2026/0508/earnings.pdf", "DATE_TIME": "08/05/2026 09:00"},
            ],
        )
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0].filed_at, "2026-03-28T10:30:00+00:00")

    def test_hkex_json_hsbc_annual_and_interim_titles_are_clean(self) -> None:
        company = CnInfoAdapter(_Transport()).resolve("300750")[0]
        filings = _hkex_filings_from_json(
            company,
            [
                {
                    "DATE_TIME": "19/02/2025 07:00",
                    "TITLE": "Annual Report and Accounts 2024 (with employee share plans)",
                    "FILE_LINK": "https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0219/hsbc-annual.pdf",
                },
                {
                    "DATE_TIME": "21/02/2024 07:00",
                    "TITLE": "2023 Annual Report",
                    "FILE_LINK": "/listedco/listconews/sehk/2024/0221/hsbc-annual-2023.pdf",
                },
                {
                    "DATE_TIME": "02/08/2024 07:00",
                    "TITLE": "Interim Report 2024",
                    "FILE_LINK": "/listedco/listconews/sehk/2024/0802/hsbc-interim-2024.pdf",
                },
            ],
        )
        self.assertEqual([item.primary_document for item in filings], [
            "Annual Report and Accounts 2024 (with employee share plans)",
            "2023 Annual Report",
            "Interim Report 2024",
        ])
        self.assertEqual([item.period_end for item in filings], ["2024-12-31", "2023-12-31", "2024-06-30"])

    def test_hkex_json_source_failure_falls_back_to_html(self) -> None:
        class BrokenJsonTransport(_Transport):
            def __init__(self) -> None:
                super().__init__()
                self.json_attempts = 0

            def get_json(self, url: str):
                if "titleSearchServlet.do" in url:
                    self.json_attempts += 1
                    raise ValueError("malformed servlet response")
                return super().get_json(url)

        transport = BrokenJsonTransport()
        adapter = HkexNewsAdapter(transport)
        company = adapter.resolve("00700")[0]
        filings = adapter.list_financial_filings(company)
        self.assertGreaterEqual(transport.json_attempts, 1)
        self.assertEqual(filings[0].primary_document, "2025 Annual Report")

    def test_hkex_malformed_json_payload_falls_back_to_html(self) -> None:
        class MalformedJsonTransport(_Transport):
            def get_json(self, url: str):
                if "titleSearchServlet.do" in url:
                    title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("title", [""])[0]
                    return {"result": "not-json"} if title == "Annual Report" else {"result": []}
                return super().get_json(url)

        transport = MalformedJsonTransport()
        adapter = HkexNewsAdapter(transport)
        company = adapter.resolve("00700")[0]
        filings = adapter.list_financial_filings(company)
        self.assertEqual(filings[0].primary_document, "2025 Annual Report")

    def test_hkex_json_explicit_empty_does_not_scrape_unfiltered_html(self) -> None:
        class EmptyJsonTransport(_Transport):
            def get_json(self, url: str):
                if "titleSearchServlet.do" in url:
                    return {"result": []}
                return super().get_json(url)

            def get_text(self, url: str) -> str:
                if "titlesearch.xhtml" in url:
                    raise AssertionError("explicit JSON empty must not invoke unfiltered HTML")
                return super().get_text(url)

        transport = EmptyJsonTransport()
        adapter = HkexNewsAdapter(transport)
        company = adapter.resolve("00700")[0]
        self.assertEqual(adapter.list_financial_filings(company), [])
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

    def test_official_download_never_overwrites_and_reuses_content_address(self) -> None:
        client = OfficialDisclosureHttpClient()
        payload = b"%PDF-1.7\nnew official filing\n%%EOF\n"
        client._request = lambda _url, **_kwargs: payload  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "2026.pdf"
            target.write_bytes(b"%PDF-1.7\nold filing\n%%EOF\n")
            saved = client.download("https://static.cninfo.com.cn/2026.pdf", target)
            self.assertNotEqual(saved, target)
            self.assertEqual(target.read_bytes(), b"%PDF-1.7\nold filing\n%%EOF\n")
            self.assertTrue(saved.name.startswith("2026-"))
            self.assertEqual(saved.read_bytes(), payload)
            repeated = client.download("https://static.cninfo.com.cn/2026.pdf", target)
            self.assertEqual(repeated, saved)

    def test_official_download_rejects_non_pdf_payload_for_pdf_target(self) -> None:
        client = OfficialDisclosureHttpClient()
        client._request = lambda _url, **_kwargs: b"not a pdf"  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unsafe.pdf"
            with self.assertRaises(MarketDataError) as raised:
                client.download("https://static.cninfo.com.cn/unsafe.pdf", target)
            self.assertEqual(raised.exception.code, "FILING_CONTENT_UNSAFE")
            self.assertFalse(target.exists())

    def test_failed_atomic_publish_cleans_temp_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "annual.pdf"
            history = Path(directory) / "annual-previous.pdf"
            history.write_bytes(b"previous successful disclosure")
            with patch("openthesis.download_safety.os.rename", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    store_immutable_payload(target, b"new disclosure")
            self.assertEqual(history.read_bytes(), b"previous successful disclosure")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_module_exposes_one_interface_for_cn_and_hk_adapters(self) -> None:
        module = MarketDataModule(
            cn_adapter=CnInfoAdapter(_Transport()),
            hk_adapter=HkexNewsAdapter(_Transport()),
        )
        self.assertEqual(module.resolve("832982", "CN_A")[0].exchange, "BSE")
        self.assertEqual(module.resolve("00700", "HK")[0].exchange, "HKEX")


if __name__ == "__main__":
    unittest.main()
