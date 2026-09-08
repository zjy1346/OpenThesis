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
from .download_safety import UnsafeDisclosurePayload, store_immutable_payload


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

# Keep the SEC adapter's statement metadata in lockstep with the canonical
# ingestion validator.  An empty statement is not an innocuous omission: the
# validator treats it as a fatal mismatch for known financial concepts.
SEC_STATEMENT_BY_CONCEPT: dict[str, str] = {
    "revenue": "income_statement",
    "net_income": "income_statement",
    "operating_income": "income_statement",
    "profit_before_tax": "income_statement",
    "profit_after_tax": "income_statement",
    "gross_profit": "income_statement",
    "operating_cash_flow": "cash_flow",
    "capital_expenditure": "cash_flow",
    "assets": "balance_sheet",
    "liabilities": "balance_sheet",
    "equity": "balance_sheet",
    "total_equity": "balance_sheet",
    "reported_roe": "summary",
}

_LIABILITY_DERIVATION_TAGS: tuple[tuple[str, ...], ...] = (
    (
        "LiabilitiesAndStockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    ("LiabilitiesCurrent", "LiabilitiesNoncurrent"),
)

# ``StockholdersEquity`` is the parent shareholders' equity and therefore
# does not, by itself, balance the consolidated statement. These tags are
# explicit non-controlling-interest facts that may be used to turn
# ``LiabilitiesAndStockholdersEquity - StockholdersEquity`` into a real
# liabilities value. Never infer NCI from another equity fact.
_NONCONTROLLING_INTEREST_TAGS: tuple[str, ...] = (
    "MinorityInterest",
    "MinorityInterestInConsolidatedEntity",
    "NoncontrollingInterestInConsolidatedEntity",
    "EquityAttributableToNoncontrollingInterest",
)

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
        # An accession URL can be revised in place. Never let a legacy
        # accession-only cache suppress an authoritative refresh. Publish the
        # fetched bytes by content address and leave the legacy object intact.
        payload = self._request_bytes(filing.source_url)
        try:
            saved = store_immutable_payload(
                target_path,
                payload,
                maximum_bytes=100_000_000,
                require_pdf=suffix.lower() == ".pdf",
            )
        except UnsafeDisclosurePayload as exc:
            raise SecClientError("SEC filing failed safety validation") from exc
        filing.local_path = str(saved)
        filing.content_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
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
                statement = SEC_STATEMENT_BY_CONCEPT.get(normalized, "")
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
        # Some US-GAAP issuers do not publish a scalar ``Liabilities`` fact.
        # Derive it only from rows sharing every period/provenance dimension;
        # this prevents silently combining different filings or currencies.
        #
        # Accounting semantics matter here. ``L+E - parent equity`` is not
        # liabilities when a consolidated filer has NCI: it is liabilities
        # plus NCI. It is a valid path only with an explicitly reported NCI
        # fact for the same accession/period/currency/scope.
        official_liability_keys = {
            (
                fact.accession_number,
                fact.end_date,
                fact.fiscal_year,
                (fact.fiscal_period or "FY").upper(),
                fact.form_type,
                fact.currency.upper(),
                "consolidated",
            )
            for fact in facts
            if fact.concept == "liabilities"
        }

        def annual_rows(tags: tuple[str, ...]) -> dict[tuple[str, str, int, str, str, str, str], tuple[str, dict[str, Any], str]]:
            selected: dict[tuple[str, str, int, str, str, str, str], tuple[str, dict[str, Any], str]] = {}
            for tag in tags:
                definition = us_gaap.get(tag)
                if not isinstance(definition, dict):
                    continue
                units = definition.get("units", {})
                preferred_unit = self._preferred_unit("liabilities", units, getattr(company, "reporting_currency", ""))
                if not preferred_unit:
                    continue
                for row in self._select_annual_facts(units.get(preferred_unit, []), allow_foreign=True):
                    try:
                        year = int(row["fy"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    key = (
                        str(row.get("accn", "")), str(row.get("end", "")), year,
                        str(row.get("fp", "FY")).upper(), str(row.get("form", "10-K")),
                        preferred_unit.upper(), "consolidated",
                    )
                    current = selected.get(key)
                    if current is None or str(row.get("filed", "")) >= str(current[1].get("filed", "")):
                        selected[key] = (tag, row, preferred_unit)
            return selected

        formula_candidates: dict[tuple[str, str, int, str, str, str, str], list[tuple[float, str, tuple[tuple[str, float], ...], dict[str, Any], str]]] = {}
        for formula_tags in _LIABILITY_DERIVATION_TAGS:
            rows_by_tag = [annual_rows((tag,)) for tag in formula_tags]
            common_keys = set(rows_by_tag[0]).intersection(*rows_by_tag[1:])
            for key in common_keys:
                inputs = tuple((rows_by_tag[index][key][0], float(rows_by_tag[index][key][1]["val"])) for index in range(len(rows_by_tag)))
                value = inputs[0][1] - inputs[1][1] if len(inputs) == 2 and formula_tags[0].startswith("LiabilitiesAnd") else sum(item[1] for item in inputs)
                formula = (
                    f"{inputs[0][0]} - {inputs[1][0]}"
                    if len(inputs) == 2 and formula_tags[0].startswith("LiabilitiesAnd")
                    else " + ".join(item[0] for item in inputs)
                )
                formula_candidates.setdefault(key, []).append(
                    (value, formula, inputs, rows_by_tag[0][key][1], rows_by_tag[0][key][2])
                )

        # Parent equity plus an explicitly reported NCI is a separate,
        # semantically complete path. A missing NCI must not be guessed from
        # total equity minus parent equity.
        parent_rows = annual_rows(("StockholdersEquity",))
        nci_rows = annual_rows(_NONCONTROLLING_INTEREST_TAGS)
        total_equity_rows = annual_rows(
            ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",)
        )
        balance_rows = annual_rows(("LiabilitiesAndStockholdersEquity",))
        for key, parent_item in parent_rows.items():
            nci_item = nci_rows.get(key)
            balance_item = balance_rows.get(key)
            if nci_item is None or balance_item is None:
                continue
            total_item = total_equity_rows.get(key)
            if total_item is not None:
                parent_value = float(parent_item[1]["val"])
                nci_value = float(nci_item[1]["val"])
                total_value = float(total_item[1]["val"])
                if not self._values_reconcile(parent_value + nci_value, total_value):
                    continue
            balance_value = float(balance_item[1]["val"])
            parent_value = float(parent_item[1]["val"])
            nci_value = float(nci_item[1]["val"])
            inputs = (
                (balance_item[0], balance_value),
                (parent_item[0], parent_value),
                (nci_item[0], nci_value),
            )
            formula = f"{balance_item[0]} - {parent_item[0]} - {nci_item[0]}"
            formula_candidates.setdefault(key, []).append(
                (
                    balance_value - parent_value - nci_value,
                    formula,
                    inputs,
                    balance_item[1],
                    balance_item[2],
                )
            )

        for key, candidates in formula_candidates.items():
            if key in official_liability_keys:
                continue
            values = [candidate[0] for candidate in candidates]
            if not values:
                continue
            # Compare only semantically equivalent true-liability paths.
            if not all(self._values_reconcile(values[0], value) for value in values[1:]):
                continue
            def formula_priority(candidate: tuple[float, str, tuple[tuple[str, float], ...], dict[str, Any], str]) -> tuple[int, str]:
                formula = candidate[1]
                if "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest" in formula:
                    return (0, formula)
                if formula.startswith("LiabilitiesCurrent"):
                    return (1, formula)
                return (2, formula)

            value, formula, inputs, row, preferred_unit = sorted(candidates, key=formula_priority)[0]
            accession = str(row.get("accn", ""))
            accession_plain = accession.replace("-", "")
            source_url = f"{SEC_ARCHIVES_BASE}/{str(int(company.cik))}/{accession_plain}/"
            input_text = ", ".join(f"{tag}={input_value:g}" for tag, input_value in inputs)
            fact_key = f"{company.cik}|liabilities|derived|{key}|{formula}|{value}"
            facts.append(
                FinancialFact(
                    fact_id=hashlib.sha256(fact_key.encode()).hexdigest()[:24],
                    company_cik=company.cik,
                    concept="liabilities",
                    reported_concept=f"derived liabilities ({formula})",
                    value=float(value),
                    unit=preferred_unit,
                    fiscal_year=int(row["fy"]),
                    fiscal_period=str(row.get("fp", "FY")),
                    form_type=str(row.get("form", "10-K")),
                    start_date=None,
                    end_date=str(row.get("end", "")),
                    filed_at=str(row.get("filed", "")),
                    accession_number=accession,
                    source_url=source_url,
                    scope="consolidated",
                    entity=company.name,
                    market=company.market,
                    statement="balance_sheet",
                    period_start=None,
                    consolidated_scope="consolidated",
                    currency=preferred_unit.upper(),
                    unit_scale=1.0,
                    revision="original",
                    source_document="SEC CompanyFacts us-gaap:derived-liabilities",
                    raw_text=f"derived liabilities = {formula} = {value:g}; inputs: {input_text}",
                    parser_version="sec-companyfacts-v2",
                    validation_status="ready_with_warnings",
                )
            )
        return facts

    @staticmethod
    def _values_reconcile(left: float, right: float) -> bool:
        """Return whether two same-semantic XBRL values agree after rounding."""
        return abs(left - right) <= max(1.0, max(abs(left), abs(right)) * 0.01)

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
