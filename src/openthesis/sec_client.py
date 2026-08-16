from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact


SEC_DATA_BASE = "https://data.sec.gov"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


CONCEPT_MAP: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditure": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "accounts_receivable": (
        "AccountsReceivableNetCurrent",
        "AccountsNotesAndLoansReceivableNetCurrent",
    ),
    "inventory": ("InventoryNet",),
    "shares_outstanding": ("EntityCommonStockSharesOutstanding",),
}

# IFRS filers (including foreign private issuers) publish the same core facts
# under ``ifrs-full``.  Keep this mapping explicit; never infer a US-GAAP tag
# from a translated label or silently convert currencies.
IFRS_CONCEPT_MAP: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenue",
        "RevenueAndOperatingIncome",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
    "net_income": (
        "ProfitLossAttributableToOwnersOfParent",
        "ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntity",
        "ProfitLoss",
    ),
    "operating_cash_flow": (
        "CashFlowsFromUsedInOperatingActivities",
        "CashFlowsFromUsedInOperations",
    ),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": ("EquityAttributableToOwnersOfParent", "Equity"),
    "total_equity": ("Equity",),
}

SEC_HK_ISSUERS: dict[str, tuple[str, str, str, str]] = {
    "00005.HK": ("0001089113", "HSBC", "USD", "IFRS"),
    "09988.HK": ("0001577552", "BABA", "CNY", "US_GAAP"),
}


class SecClientError(RuntimeError):
    pass


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "ix:hidden"}:
            self._hidden_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "ix:hidden"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = unescape("".join(self.parts))
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path, min_interval: float = 0.12):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC User-Agent 必须包含联系邮箱，例如 OpenThesis name@example.com")
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self._last_request = 0.0

    def _request_bytes(self, url: str) -> bytes:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "identity",
                "Accept": "application/json,text/html,*/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SecClientError(f"SEC 请求失败：{url}\n{exc}") from exc
        finally:
            self._last_request = time.monotonic()
        return payload

    def _get_json(self, url: str, cache_name: str | None = None) -> dict[str, Any]:
        cache_path = self.cache_dir / cache_name if cache_name else None
        if cache_path and cache_path.exists():
            age_seconds = time.time() - cache_path.stat().st_mtime
            if age_seconds < 24 * 60 * 60:
                return json.loads(cache_path.read_text(encoding="utf-8"))
        payload = self._request_bytes(url)
        if cache_path:
            cache_path.write_bytes(payload)
        return json.loads(payload.decode("utf-8"))

    def search_companies(self, query: str, limit: int = 15) -> list[Company]:
        query = query.strip().lower()
        if not query:
            return []
        payload = self._get_json(SEC_TICKERS_URL, "company_tickers.json")
        matches: list[tuple[int, Company]] = []
        for item in payload.values():
            ticker = str(item.get("ticker", ""))
            name = str(item.get("title", ""))
            haystack = f"{ticker} {name}".lower()
            if query not in haystack:
                continue
            score = 0
            if ticker.lower() == query:
                score += 100
            if name.lower() == query:
                score += 80
            if ticker.lower().startswith(query):
                score += 40
            if name.lower().startswith(query):
                score += 20
            matches.append(
                (
                    score,
                    Company(
                        cik=str(item["cik_str"]).zfill(10),
                        ticker=ticker.upper(),
                        name=name,
                    ),
                )
            )
        matches.sort(key=lambda pair: (-pair[0], pair[1].ticker))
        return [company for _, company in matches[:limit]]

    def list_annual_filings(self, company: Company, limit: int = 5) -> list[FilingDocument]:
        submissions = self._get_json(
            f"{SEC_DATA_BASE}/submissions/CIK{company.cik}.json",
            f"submissions-{company.cik}.json",
        )
        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filings: list[FilingDocument] = []
        for index, form_type in enumerate(forms):
            if form_type not in {"10-K", "20-F", "40-F"}:
                continue
            accession = recent["accessionNumber"][index]
            accession_plain = accession.replace("-", "")
            primary_document = recent["primaryDocument"][index]
            cik_plain = str(int(company.cik))
            source_url = (
                f"{SEC_ARCHIVES_BASE}/{cik_plain}/{accession_plain}/{primary_document}"
            )
            filings.append(
                FilingDocument(
                    document_id=f"sec:{company.cik}:{accession}",
                    company_cik=company.cik,
                    accession_number=accession,
                    form_type=form_type,
                    fiscal_period="FY",
                    period_end=str(recent.get("reportDate", [""])[index]),
                    filed_at=str(recent.get("filingDate", [""])[index]),
                    primary_document=primary_document,
                    source_url=source_url,
                )
            )
            if len(filings) >= limit:
                break
        return filings

    def download_filing(self, filing: FilingDocument, target_dir: Path) -> FilingDocument:
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filing.primary_document).suffix or ".html"
        target_path = target_dir / f"{filing.accession_number}{suffix}"
        if not target_path.exists():
            target_path.write_bytes(self._request_bytes(filing.source_url))
        payload = target_path.read_bytes()
        filing.local_path = str(target_path)
        filing.content_hash = hashlib.sha256(payload).hexdigest()
        return filing

    @staticmethod
    def extract_filing_text(path: Path) -> str:
        content = path.read_text(encoding="utf-8", errors="replace")
        parser = TextExtractor()
        parser.feed(content)
        return parser.text()

    def get_company_facts(self, company: Company) -> list[FinancialFact]:
        payload = self._get_json(
            f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{company.cik}.json",
            f"companyfacts-{company.cik}.json",
        )
        facts_payload = payload.get("facts", {})
        us_gaap = facts_payload.get("us-gaap", {})
        ifrs_full = facts_payload.get("ifrs-full", {})
        dei = facts_payload.get("dei", {})
        facts: list[FinancialFact] = []
        mappings: list[tuple[str, dict[str, Any], tuple[str, ...]]] = [
            *[(concept, us_gaap, tags) for concept, tags in CONCEPT_MAP.items()],
            *[(concept, ifrs_full, tags) for concept, tags in IFRS_CONCEPT_MAP.items()],
            ("shares_outstanding", dei, CONCEPT_MAP["shares_outstanding"]),
        ]
        seen: set[tuple[str, str, str, str]] = set()
        for normalized, namespace, candidates in mappings:
            selected_by_year: dict[int, tuple[int, str, dict[str, Any], str]] = {}
            for priority, reported in enumerate(candidates):
                if reported not in namespace:
                    continue
                fact = namespace[reported]
                units: dict[str, list[dict[str, Any]]] = fact.get("units", {})
                preferred_unit = self._preferred_unit(
                    normalized, units, getattr(company, "reporting_currency", "")
                )
                if not preferred_unit:
                    continue
                for row in self._select_annual_facts(units[preferred_unit], allow_foreign=True):
                    year = int(row["fy"])
                    current = selected_by_year.get(year)
                    candidate = (priority, reported, row, preferred_unit)
                    if current is None:
                        selected_by_year[year] = candidate
                        continue
                    # Prefer the canonical tag order. Within the same tag,
                    # retain the latest-filed annual value.
                    if priority < current[0] or (
                        priority == current[0]
                        and str(row.get("filed", ""))
                        >= str(current[2].get("filed", ""))
                    ):
                        selected_by_year[year] = candidate
            for year in sorted(selected_by_year, reverse=True)[:10]:
                _, reported, row, preferred_unit = selected_by_year[year]
                accession = str(row.get("accn", ""))
                accession_plain = accession.replace("-", "")
                cik_plain = str(int(company.cik))
                source_url = f"{SEC_ARCHIVES_BASE}/{cik_plain}/{accession_plain}/"
                fact_key = (
                    f"{company.cik}|{normalized}|{row.get('fy')}|{row.get('end')}|"
                    f"{row.get('filed')}|{row.get('val')}"
                )
                key = (normalized, str(row.get("end", "")), accession, reported)
                if key in seen:
                    continue
                seen.add(key)
                namespace_name = "ifrs-full" if namespace is ifrs_full else "us-gaap" if namespace is us_gaap else "dei"
                statement = {
                    "revenue": "income_statement", "net_income": "income_statement",
                    "operating_cash_flow": "cash_flow", "assets": "balance_sheet",
                    "liabilities": "balance_sheet", "equity": "balance_sheet",
                    "total_equity": "balance_sheet",
                }.get(normalized, "")
                currency = preferred_unit.upper() if preferred_unit else ""
                facts.append(
                    FinancialFact(
                        fact_id=hashlib.sha256(fact_key.encode()).hexdigest()[:24],
                        company_cik=company.cik,
                        concept=normalized,
                        reported_concept=reported,
                        value=float(row["val"]),
                        unit=preferred_unit,
                        fiscal_year=year,
                        fiscal_period=str(row.get("fp", "FY")),
                        form_type=str(row.get("form", "10-K")),
                        start_date=row.get("start"),
                        end_date=str(row.get("end", "")),
                        filed_at=str(row.get("filed", "")),
                        accession_number=accession,
                        source_url=source_url,
                        scope="consolidated",
                        entity=company.name,
                        market=company.market,
                        statement=statement,
                        period_start=row.get("start"),
                        consolidated_scope="consolidated",
                        currency=currency,
                        unit_scale=1.0,
                        revision="original",
                        source_document=f"SEC CompanyFacts {namespace_name}:{reported}",
                        raw_text=f"{namespace_name}:{reported}={row.get('val')} {preferred_unit}",
                        parser_version="sec-companyfacts-v2",
                        validation_status="ready_with_warnings",
                    )
                )
        return facts

    @staticmethod
    def _preferred_unit(
        normalized: str, units: dict[str, Any], reporting_currency: str = ""
    ) -> str | None:
        preferences = ["shares"] if normalized == "shares_outstanding" else []
        if reporting_currency:
            preferences.append(reporting_currency.upper())
        if normalized != "shares_outstanding":
            preferences.append("USD")
        for unit in preferences:
            if unit in units:
                return unit
        return next(iter(units), None)

    @staticmethod
    def _select_annual_facts(rows: list[dict[str, Any]], *, allow_foreign: bool = False) -> list[dict[str, Any]]:
        forms = {"10-K", "20-F", "40-F"} if allow_foreign else {"10-K"}
        candidates = [
            row
            for row in rows
            if row.get("form") in forms
            and row.get("fp") == "FY"
            and isinstance(row.get("fy"), int)
        ]
        # A later 10-K repeats prior years. Keep the latest-filed value for each
        # fiscal year/end-date pair, then the most recent end date per fiscal year.
        by_year: dict[int, dict[str, Any]] = {}
        for row in sorted(candidates, key=lambda item: str(item.get("filed", ""))):
            year = int(row["fy"])
            current = by_year.get(year)
            if current is None or str(row.get("end", "")) >= str(current.get("end", "")):
                by_year[year] = row
        return [by_year[year] for year in sorted(by_year, reverse=True)[:10]]


class SecFinancialSourceAdapter:
    """Cached SEC Company Facts adapter used before native PDF parsing.

    Facts retain the SEC accession and archive URL as their provenance.  The
    adapter only remaps the target period to the HK filing; it never converts
    currencies or fabricates PDF evidence.
    """

    def __init__(self, client: SecClient):
        self.client = client
        self._facts: dict[str, list[FinancialFact]] = {}

    def fetch(
        self, company: Company, filing: FilingDocument
    ) -> tuple[list[FinancialFact], list[EvidenceRef], str | None]:
        mapped = SEC_HK_ISSUERS.get(company.ticker.upper())
        if mapped is None:
            return [], [], "sec_structured_source_not_mapped"
        sec_cik, sec_ticker, currency, standard = mapped
        sec_company = Company(
            cik=sec_cik.zfill(10), ticker=sec_ticker, name=company.name,
            exchange="SEC", issuer_id=sec_ticker, market="US",
            security_id=sec_cik.zfill(10), listing_currency=currency,
            reporting_currency=currency, accounting_standard=standard,
        )
        try:
            cache_key = sec_company.cik
            if cache_key not in self._facts:
                self._facts[cache_key] = self.client.get_company_facts(sec_company)
            selected = [
                fact for fact in self._facts[cache_key]
                if fact.end_date == filing.period_end
                and (fact.fiscal_period or "FY").upper() == (filing.fiscal_period or "FY").upper()
                and fact.currency.upper() == currency.upper()
            ]
        except Exception as exc:
            return [], [], f"sec_structured_source_failed:{type(exc).__name__}"
        if not selected:
            return [], [], "sec_structured_period_not_found"
        refs = [
            EvidenceRef(
                evidence_id=f"sec:{fact.fact_id}",
                document_id=filing.document_id,
                source_url=fact.source_url,
                title=f"SEC CompanyFacts {fact.reported_concept}",
                locator=f"accession:{fact.accession_number}",
                excerpt=fact.raw_text,
                published_at=fact.filed_at,
                content_hash="",
                bbox=None,
            )
            for fact in selected
        ]
        return selected, refs, None
