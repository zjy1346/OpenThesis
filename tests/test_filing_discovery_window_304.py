from openthesis.domain import Company, FilingDocument
from openthesis.financial_recovery import FinancialRecoveryController
from openthesis.storage import Storage


def test_announcement_count_does_not_silently_shrink_requested_annual_history(tmp_path):
    company = Company("CN_A:SZSE:000811.SZ", "000811.SZ", "冰轮环境", market="CN_A")
    def filing(year, period="FY"):
        end = f"{year}-12-31" if period == "FY" else f"{year}-06-30"
        return FilingDocument(f"{year}-{period}", company.cik, f"{year}-{period}",
            "ANNUAL_REPORT" if period == "FY" else "INTERIM_REPORT", period, end,
            f"{year+1}-04-30" if period == "FY" else f"{year}-08-30",
            f"{year}年年度报告" if period == "FY" else f"{year}年半年度报告",
            f"https://official.example/{year}-{period}.pdf")
    all_filings = [filing(2026, "H1"), filing(2025), filing(2025, "H1"),
                   filing(2024), filing(2024, "H1"), filing(2023), filing(2023, "H1"),
                   filing(2022), filing(2021), filing(2020)]
    calls = []
    class Source:
        def list_financial_filings(self, _company, limit):
            calls.append(limit)
            return all_filings[:limit]
    result = FinancialRecoveryController(Storage(tmp_path / "data")).discover(Source(), company, annual_limit=5)
    annual = {f.period_end[:4] for f in result.filings if f.fiscal_period == "FY"}
    assert annual >= {"2020", "2021", "2022", "2023", "2024", "2025"}
    assert calls == [8, 16]


def test_exhausted_source_is_not_queried_forever_to_invent_missing_years(tmp_path):
    company = Company("issuer", "FIX", "Fixture")
    calls = []
    class Source:
        def list_annual_filings(self, _company, limit):
            calls.append(limit)
            return [FilingDocument("source", company.cik, "source", "10-K", "FY",
                "2025-12-31", "2026-04-30", "2025 annual report", "https://official.example/one.pdf")]
    result = FinancialRecoveryController(Storage(tmp_path / "data")).discover(Source(), company, annual_limit=5)
    assert len(result.filings) == 1
    assert calls == [8]
