"""Run the offline Phase 7 evidence-to-render acceptance matrix.

The harness consumes only official files already present below ``build``.  It
parses each cached PDF/XBRL source, runs the same deterministic metric and
report render functions used by the application, and writes a fresh matrix;
the manifest validator is therefore a second check, not the source of status.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from openthesis.domain import Company, FilingDocument
from openthesis.financial_ingestion import FinancialIngestionEngine
from openthesis.financials import calculate_interim_metrics, calculate_metrics
from openthesis.report_html import render_research_html
from openthesis.reporting import render_research_run
from openthesis.sec_client import SecClient


ROOT = Path(__file__).resolve().parents[1]
CORE = {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities"}
EQUITY_ALTERNATIVES = {"equity", "total_equity"}


def _has_full_core(concepts: set[str]) -> bool:
    return CORE.issubset(concepts) and bool(concepts & EQUITY_ALTERNATIVES)


PDF_CASES = (
    ("600519.SH", "贵州茅台", "CN_A:SSE:600519.SH", "600519-2025FY.pdf", "172054171890.91", "CNY", "2025-12-31", "https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF", "1225114741"),
    ("000858.SZ", "五粮液", "CN_A:SZSE:000858.SZ", "000858-2025FY.pdf", "40528509770.23", "CNY", "2025-12-31", "https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-30/7b179575-3e07-4607-8d7e-d1bb9c4c8786.PDF", "7b179575-3e07-4607-8d7e-d1bb9c4c8786"),
    ("300750.SZ", "CATL", "CN_A:SZSE:300750.SZ", "300750-2025FY.pdf", "", "CNY", "2025-12-31", "https://static.cninfo.com.cn/finalpage/2026-03-10/1225002214.PDF", "1225002214"),
    ("688981.SH", "SMIC", "CN_A:SSE:688981.SH", "688981-2025FY.pdf", "", "CNY", "2025-12-31", "https://static.cninfo.com.cn/finalpage/2026-03-27/1225037057.PDF", "1225037057"),
    ("832982.BJ", "Jinbo", "CN_A:BSE:832982.BJ", "832982-2025FY.pdf", "", "CNY", "2025-12-31", "https://static.cninfo.com.cn/finalpage/2026-04-29/1225267792.PDF", "1225267792"),
    ("00700.HK", "Tencent", "HK:SEHK:00700.HK", "00700.HK_2026040901231.pdf", "", "CNY", "2025-12-31", "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/2026040901231.pdf", "2026040901231"),
    ("09988.HK", "Alibaba", "HK:SEHK:09988.HK", "09988.HK_2026061800844.pdf", "", "CNY", "2026-03-31", "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0618/2026061800844.pdf", "2026061800844"),
    ("01211.HK", "BYD", "HK:SEHK:01211.HK", "01211.HK_2026032703008.pdf", "", "CNY", "2025-12-31", "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0327/2026032703008.pdf", "2026032703008"),
    ("03690.HK", "Meituan", "HK:SEHK:03690.HK", "03690.HK_2026042400179.pdf", "", "CNY", "2025-12-31", "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0424/2026042400179.pdf", "2026042400179"),
    # HSBC is accepted from SEC 20-F structured facts below; this PDF is still
    # parsed here to exercise the documented fallback path.
    ("00005.HK", "HSBC", "HK:SEHK:00005.HK", "00005.HK_2026032700187.pdf", "", "USD", "2025-12-31", "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0327/2026032700187.pdf", "2026032700187"),
)

SEC_CASES = {
    "AAPL": ("0000320193", "Apple", "USD"),
    "MSFT": ("0000789019", "Microsoft", "USD"),
    "NVDA": ("0001045810", "NVIDIA", "USD"),
    "TSLA": ("0001318605", "Tesla", "USD"),
    "BRK.B": ("0001067983", "Berkshire Hathaway", "USD"),
    "00005.HK": ("0001089113", "HSBC", "USD"),
}


def _facts_dict(facts: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "concept": fact.concept,
            "value": fact.value,
            "fiscal_year": fact.fiscal_year,
            "fiscal_period": fact.fiscal_period,
            "filed_at": fact.filed_at,
        }
        for fact in facts
    ]


def _render(symbol: str, name: str, currency: str, facts: list[Any]) -> tuple[str, str]:
    metrics = calculate_metrics(_facts_dict(facts))
    if not metrics:
        raise AssertionError(f"{symbol}: no deterministic metrics")
    for row in metrics:
        for key, value in row.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise AssertionError(f"{symbol}: non-finite metric {key}")
        ratio = row.get("debt_to_assets")
        if ratio is not None and abs(float(ratio)) > 2:
            raise AssertionError(f"{symbol}: absurd debt ratio")
    artifacts = [
        {
            "artifact_type": "deterministic-financial-summary",
            "title": "财务概览",
            "agent_id": "deterministic-engine",
            "model_id": "deterministic",
            "content": {"metrics": metrics, "currency": currency, "evidence": []},
        },
        {
            "artifact_type": "research-report",
            "title": "研究报告",
            "agent_id": "acceptance-harness",
            "model_id": "deterministic",
            "content": {
                "report": {"executive_summary": f"{symbol} 的确定性验收摘要。"},
                "verification": {"passed": True, "issues": []},
            },
        },
    ]
    markdown = render_research_run(symbol, artifacts, "zh-CN", company_name=name, include_technical=False)
    html = render_research_html(symbol, artifacts, "zh-CN", company_name=name, include_technical=False)
    forbidden = ("counterargument", "severity", "summary", "calculation", "unknowns", "evidence_ids")
    # Ignore HTML element ids/classes (e.g. ``executive-summary``), and check
    # only user-visible text for protocol keys.
    visible_html = re.sub(r"<[^>]*>", " ", html)
    lowered = (markdown + visible_html).casefold()
    if any(key in lowered for key in forbidden):
        raise AssertionError(f"{symbol}: internal report key leaked")
    return markdown, html


def _pdf_rows() -> list[dict[str, Any]]:
    engine = FinancialIngestionEngine()
    rows: list[dict[str, Any]] = []
    for symbol, name, security_id, filename, _, currency, end, url, accession in PDF_CASES:
        is_cn = symbol.endswith((".SH", ".SZ", ".BJ"))
        path = ROOT / "build" / "acceptance" / ("cn-filings" if is_cn else "hk-filings") / filename
        if is_cn and not path.is_file():
            local_app_data = os.environ.get("LOCALAPPDATA")
            exchange = "SSE" if symbol.endswith(".SH") else "BSE" if symbol.endswith(".BJ") else "SZSE"
            if local_app_data:
                path = Path(local_app_data) / "OpenThesis" / "filings" / f"CN_A_{exchange}_{symbol}" / f"{accession}.pdf"
        if not path.is_file():
            raise FileNotFoundError(path)
        company = Company(security_id, symbol, name, "ACCEPTANCE", symbol, "CN_A" if is_cn else "HK", security_id, currency, currency, "IFRS")
        filing = FilingDocument(
            f"acceptance:{accession}", company.security_id, accession, "ANNUAL_REPORT", "FY", end,
            "2026-08-01", f"{name} annual report", url, local_path=str(path),
        )
        dataset = engine.ingest(company, [filing])
        facts = list(dataset.accepted_facts)
        concepts = {fact.concept for fact in facts}
        if symbol != "00005.HK" and (dataset.status.value != "VERIFIED" or not _has_full_core(concepts)):
            raise AssertionError(f"{symbol}: {dataset.status.value} {sorted(CORE - concepts)}")
        if symbol != "00005.HK":
            _render(symbol, name, currency, facts)
        else:
            # Structured SEC facts are the accepted HSBC source; a PDF parse
            # failure is retained as fallback diagnostics, never promoted.
            facts = []
        if symbol == "00005.HK":
            continue
        rows.append({"company": symbol, "market": "CN_A" if is_cn else "HK", "official_source": url, "latest_period": end, "currency": currency, "core_concepts": sorted(concepts & (CORE | EQUITY_ALTERNATIVES)), "status": "VERIFIED", "report_render": "deterministic_render_verified"})
    return rows


def _byd_q1_yoy_check() -> dict[str, Any]:
    """Parse two official Q1 reports and prove like-for-like BYD growth."""
    company = Company(
        "HK:SEHK:01211.HK", "01211.HK", "BYD", "SEHK", "HKEX:01211",
        "HK", "HK:SEHK:01211.HK", "CNY", "CNY", "CAS",
    )
    cases = (
        (
            "2026042803001", "2026-03-31", "2026-04-28",
            "First Quarterly Report 2026",
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0428/2026042803001.pdf",
        ),
        (
            "2025042502125", "2025-03-31", "2025-04-25",
            "First Quarterly Report 2025",
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0425/2025042502125.pdf",
        ),
    )
    filings: list[FilingDocument] = []
    for accession, end, filed, title, url in cases:
        path = ROOT / "build" / "acceptance" / "hk-filings" / f"{accession}.pdf"
        if not path.is_file():
            raise FileNotFoundError(path)
        filings.append(FilingDocument(
            f"acceptance:{accession}", company.security_id, accession,
            "QUARTERLY_REPORT", "Q1", end, filed, title, url,
            local_path=str(path), content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        ))
    dataset = FinancialIngestionEngine().ingest(company, filings)
    if dataset.status.value != "VERIFIED":
        raise AssertionError(f"BYD Q1 comparison: {dataset.status.value}")
    by_period: dict[tuple[int, str], set[str]] = {}
    for fact in dataset.accepted_facts:
        by_period.setdefault((fact.fiscal_year, fact.fiscal_period), set()).add(fact.concept)
    for period in ((2025, "Q1"), (2026, "Q1")):
        if not _has_full_core(by_period.get(period, set())):
            raise AssertionError(f"BYD {period}: incomplete core facts")
    interim = calculate_interim_metrics(_facts_dict(list(dataset.accepted_facts)))
    latest = next(item for item in interim if item["year"] == 2026 and item["period"] == "Q1")
    expected = 150_225_314_000.0 / 170_360_448_000.0 - 1.0
    if latest.get("comparison_period") != "2025 Q1" or latest.get("comparison_gap") is not None:
        raise AssertionError("BYD Q1 comparison period unavailable")
    if not math.isclose(float(latest["revenue_growth"]), expected, rel_tol=0, abs_tol=1e-12):
        raise AssertionError("BYD Q1 revenue growth mismatch")
    return {
        "status": "VERIFIED",
        "company": "01211.HK",
        "period": "2026 Q1",
        "comparison_period": "2025 Q1",
        "revenue_growth": latest["revenue_growth"],
        "official_sources": [filing.source_url for filing in filings],
    }


def _sec_rows() -> list[dict[str, Any]]:
    cache_dir = ROOT / "build" / "acceptance" / "sec-cache"
    client = SecClient("OpenThesis acceptance@example.invalid", cache_dir, min_interval=0)
    original = client._get_json
    client._get_json = lambda url, cache_name=None: json.loads((cache_dir / str(cache_name)).read_text(encoding="utf-8")) if cache_name else original(url, cache_name)
    rows: list[dict[str, Any]] = []
    for ticker, (cik, name, currency) in SEC_CASES.items():
        company = Company(cik=cik, ticker=ticker, name=name, reporting_currency=currency, listing_currency=currency, market="US")
        facts = client.get_company_facts(company)
        submissions_path = cache_dir / f"submissions-{cik}.json"
        submissions = json.loads(submissions_path.read_text(encoding="utf-8"))
        recent = submissions.get("filings", {}).get("recent", {})
        annual = [
            (str(form_date), str(report_date))
            for form_date, report_date, form in zip(
                recent.get("filingDate", ()), recent.get("reportDate", ()), recent.get("form", ())
            )
            if form in {"10-K", "20-F", "40-F"} and report_date
        ]
        if not annual:
            raise AssertionError(f"{ticker}: no annual filing metadata")
        # Bind CompanyFacts to the latest official annual filing, rather than
        # silently selecting an older complete group when the newest filing is
        # incomplete.  The service uses the same fail-closed policy.
        latest_end = max(annual, key=lambda item: item[0])[1]
        latest = [fact for fact in facts if fact.end_date == latest_end and (fact.fiscal_period or "FY").upper() == "FY"]
        concepts = {fact.concept for fact in latest}
        if not _has_full_core(concepts):
            raise AssertionError(f"{ticker}: latest SEC FY {latest_end} incomplete")
        _render(ticker, name, currency, latest)
        rows.append({"company": ticker, "market": "HK" if ticker.endswith(".HK") else "US", "official_source": f"SEC CompanyFacts CIK{cik}", "latest_period": latest_end, "currency": currency, "core_concepts": sorted(concepts & (CORE | EQUITY_ALTERNATIVES)), "status": "VERIFIED", "report_render": "deterministic_render_verified"})
    return rows


def main() -> int:
    output = ROOT / "build" / "acceptance" / "phase7_matrix.json"
    if len(sys.argv) > 1 and sys.argv[1] == "--validate":
        payload = json.loads(output.read_text(encoding="utf-8"))
        rows = payload.get("rows", [])
        if len(rows) != 15 or len({row.get("company") for row in rows}) != 15:
            raise AssertionError("phase7 matrix must contain 15 unique rows")
        for row in rows:
            concepts = set(row.get("core_concepts", ()))
            if row.get("status") != "VERIFIED" or not _has_full_core(concepts):
                raise AssertionError(f"{row.get('company')}: invalid status/core")
        byd_q1 = payload.get("supplemental_checks", {}).get("byd_q1_yoy", {})
        if byd_q1.get("status") != "VERIFIED" or byd_q1.get("comparison_period") != "2025 Q1":
            raise AssertionError("BYD Q1 supplemental comparison is not verified")
        print("phase7 manifest validator OK: 15/15 VERIFIED rows")
        return 0
    rows = _pdf_rows() + _sec_rows()
    if len(rows) != 15:
        raise AssertionError(f"expected 15 locally executable rows, got {len(rows)}")
    rows.sort(key=lambda row: row["company"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "generated_by": "OpenThesis Phase 7 acceptance harness",
        "acceptance_level": "ingestion+deterministic-render",
        "rows": rows,
        "supplemental_checks": {"byd_q1_yoy": _byd_q1_yoy_check()},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"phase7 acceptance generated: {len(rows)}/15 rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
