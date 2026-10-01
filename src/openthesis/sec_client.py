from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .download_safety import UnsafeDisclosurePayload, store_immutable_payload


SEC_DATA_BASE = "https://data.sec.gov"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

_ANNUAL_FORMS = frozenset({
    "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
})
_MIN_ANNUAL_DURATION_DAYS = 350
_MAX_ANNUAL_DURATION_DAYS = 380


def _normalize_sec_exchange(value: object) -> str:
    if isinstance(value, list):
        value = next((item for item in value if str(item).strip()), "")
    normalized = str(value or "").strip().upper()
    if "NASDAQ" in normalized:
        return "NASDAQ"
    if normalized in {"NYSE", "NEW YORK STOCK EXCHANGE"}:
        return "NYSE"
    if normalized in {"NYSE AMERICAN", "NYSEAMERICAN", "AMEX"}:
        return "AMEX"
    return normalized


@dataclass(frozen=True, slots=True)
class PeriodSemantics:
    """Evidence-based classification of an SEC CompanyFacts annual candidate.

    ``fp=FY`` is only a filing label. A flow is annual only when its actual
    inclusive start/end span is within the normal fiscal-year window and its
    SEC fiscal-year label falls inside that date interval. Instant facts are
    point-in-time observations and therefore must have no duration start and
    an end date in the labeled fiscal year.
    """

    kind: str
    complete: bool
    reason: str
    period_days: int | None = None

    @classmethod
    def for_row(cls, row: dict[str, Any], expected_kind: str = "auto") -> PeriodSemantics:
        kind = str(expected_kind or "auto").strip().lower()
        if kind == "auto":
            kind = "duration" if row.get("start") not in (None, "") else "instant"
        if kind not in {"duration", "instant"}:
            return cls("invalid", False, "unknown_period_kind")

        form = str(row.get("form", "")).strip().upper()
        if form not in _ANNUAL_FORMS:
            return cls("invalid", False, "non_annual_form")
        if str(row.get("fp", "")).strip().upper() != "FY":
            return cls("invalid", False, "non_fy_label")
        fiscal_year = row.get("fy")
        if isinstance(fiscal_year, bool) or not isinstance(fiscal_year, int):
            return cls("invalid", False, "missing_fiscal_year")
        if not str(row.get("accn", "")).strip():
            return cls("invalid", False, "missing_accession")
        try:
            filed = date.fromisoformat(str(row.get("filed", "")))
            end = date.fromisoformat(str(row.get("end", "")))
        except (TypeError, ValueError):
            return cls("invalid", False, "invalid_source_date")
        start_value = row.get("start")
        if kind == "instant":
            if start_value not in (None, ""):
                return cls("invalid", False, "instant_has_duration_start")
            if end.year != fiscal_year:
                return cls("invalid", False, "instant_end_not_in_fiscal_year")
            if end > filed:
                return cls("invalid", False, "period_end_after_filing")
            return cls("instant", True, "point_in_time_fy_end")

        if start_value in (None, ""):
            return cls("invalid", False, "duration_missing_start")
        try:
            start = date.fromisoformat(str(start_value))
        except (TypeError, ValueError):
            return cls("invalid", False, "invalid_start_date")
        if end < start:
            return cls("invalid", False, "period_end_before_start")
        if end > filed:
            return cls("invalid", False, "period_end_after_filing")
        period_days = (end - start).days + 1
        if period_days < _MIN_ANNUAL_DURATION_DAYS:
            return cls("short_duration", False, "duration_below_annual_window", period_days)
        if period_days > _MAX_ANNUAL_DURATION_DAYS:
            return cls("invalid", False, "duration_above_annual_window", period_days)
        if not (start.year <= fiscal_year <= end.year):
            return cls("invalid", False, "fiscal_year_outside_period")
        return cls("annual_duration", True, "complete_fiscal_year", period_days)


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
    "operating_income": (
        "ProfitLossFromOperatingActivities",
        "OperatingProfitLoss",
    ),
    "operating_cash_flow": (
        "CashFlowsFromUsedInOperatingActivities",
        "CashFlowsFromUsedInOperations",
    ),
    "capital_expenditure": (
        "PurchaseOfPropertyPlantAndEquipment",
        "PaymentsToAcquirePropertyPlantAndEquipment",
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

# Stable aliases attach to the security ticker, never to a display position.
# Exact multilingual aliases are intentionally curated; typo tolerance is
# restricted to Latin ticker/name tokens below so a fuzzy Chinese query cannot
# silently select the wrong issuer.
SEC_SECURITY_ALIASES: dict[str, tuple[str, ...]] = {
    "AAPL": ("苹果", "蘋果", "苹果公司", "蘋果公司"),
    "MSFT": ("微软", "微軟", "微软公司", "微軟公司"),
    "NVDA": ("英伟达", "英偉達", "辉达", "輝達"),
    "AMZN": ("亚马逊", "亞馬遜"),
    "GOOG": ("谷歌",),
    "GOOGL": ("谷歌",),
    "META": ("元宇宙平台", "脸书", "臉書"),
    "TSLA": ("特斯拉",),
    "BRK.B": ("伯克希尔哈撒韦", "波克夏海瑟威"),
}


def _bounded_damerau_levenshtein(left: str, right: str, limit: int) -> int:
    """Optimal-string-alignment distance with an early length bound."""

    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, 1):
        current = [row_index]
        row_min = row_index
        for column_index, right_char in enumerate(right, 1):
            value = min(
                current[column_index - 1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (left_char != right_char),
            )
            if (
                previous_previous is not None
                and row_index > 1
                and column_index > 1
                and left_char == right[column_index - 2]
                and left[row_index - 2] == right_char
            ):
                value = min(value, previous_previous[column_index - 2] + 1)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous_previous, previous = previous, current
    return previous[-1]


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


def _retry_after_seconds(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        try:
            delay = parsedate_to_datetime(text).timestamp() - time.time()
            return max(0.0, delay)
        except (TypeError, ValueError, OverflowError):
            return None


class SecClient:
    def __init__(
        self, user_agent: str, cache_dir: Path, min_interval: float = 0.12,
        *, max_response_bytes: int = 64 * 1024 * 1024, max_attempts: int = 4,
        request_deadline_seconds: float = 90.0,
    ):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC User-Agent 必须包含联系邮箱，例如 OpenThesis name@example.com")
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.max_response_bytes = max(1, int(max_response_bytes))
        self.max_attempts = max(1, int(max_attempts))
        self.request_deadline_seconds = max(1.0, float(request_deadline_seconds))
        self._last_request = 0.0
        # Discovery has historically returned only filings, so diagnostics
        # are exposed out-of-band to preserve that API for existing callers.
        self.discovery_diagnostics: tuple[str, ...] = ()

    def _request_bytes(self, url: str) -> bytes:
        deadline = time.monotonic() + self.request_deadline_seconds
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
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
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with urllib.request.urlopen(request, timeout=min(30.0, remaining)) as response:
                    declared = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
                    if declared and int(declared) > self.max_response_bytes:
                        raise SecClientError("SEC_RESPONSE_TOO_LARGE")
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = response.read(min(64 * 1024, self.max_response_bytes - total + 1))
                        if not chunk:
                            return b"".join(chunks)
                        total += len(chunk)
                        if total > self.max_response_bytes:
                            raise SecClientError("SEC_RESPONSE_TOO_LARGE")
                        chunks.append(chunk)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 503} or attempt + 1 >= self.max_attempts:
                    break
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After", ""))
                delay = retry_after if retry_after is not None else min(8.0, 0.5 * (2 ** attempt))
                delay += random.uniform(0.0, min(0.25, delay * 0.1))
                if time.monotonic() + delay >= deadline:
                    break
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    break
                delay = min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.1)
                if time.monotonic() + delay >= deadline:
                    break
                time.sleep(delay)
            finally:
                self._last_request = time.monotonic()
        raise SecClientError(f"SEC 请求失败：{url}\n{last_error or 'request deadline exceeded'}") from last_error

    def _get_json(self, url: str, cache_name: str | None = None) -> dict[str, Any]:
        cache_path = self.cache_dir / cache_name if cache_name else None
        if cache_path and cache_path.exists():
            age_seconds = time.time() - cache_path.stat().st_mtime
            if age_seconds < 24 * 60 * 60:
                return json.loads(cache_path.read_text(encoding="utf-8"))
        payload = self._request_bytes(url)
        if cache_path:
            temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(cache_path)
        return json.loads(payload.decode("utf-8"))
    def search_companies(self, query: str, limit: int = 15) -> list[Company]:
        query = query.strip().casefold().replace("/", "-").replace(".", "-")
        if not query:
            return []
        payload = self._get_json(
            SEC_TICKERS_EXCHANGE_URL,
            "company_tickers_exchange.json",
        )
        rows: list[dict[str, Any]] = []
        if isinstance(payload.get("fields"), list) and isinstance(payload.get("data"), list):
            fields = [str(field) for field in payload["fields"]]
            rows = [
                dict(zip(fields, values))
                for values in payload["data"]
                if isinstance(values, list) and len(values) == len(fields)
            ]
        else:
            rows = [item for item in payload.values() if isinstance(item, dict)]
        matches: list[tuple[int, Company]] = []
        for item in rows:
            ticker = str(item.get("ticker", ""))
            name = str(item.get("title", ""))
            if not name:
                name = str(item.get("name", ""))
            normalized_ticker = ticker.lower().replace("/", "-").replace(".", "-")
            normalized_name = name.casefold()
            aliases = tuple(
                alias.casefold()
                for alias in SEC_SECURITY_ALIASES.get(
                    ticker.upper().replace("-", ".").replace("/", "."), ()
                )
            )
            haystack = f"{normalized_ticker} {normalized_name}"
            exact_alias = query in aliases
            substring = query in haystack
            fuzzy = False
            fuzzy_distance = 99
            if query.isascii() and query.isalnum() and len(query) >= 4:
                threshold = 1 if len(query) <= 6 else 2
                tokens = [normalized_ticker, *re.findall(r"[a-z0-9]{4,}", normalized_name)]
                distances = [
                    _bounded_damerau_levenshtein(query, token, threshold)
                    for token in tokens
                    if abs(len(query) - len(token)) <= threshold
                ]
                if distances:
                    fuzzy_distance = min(distances)
                    fuzzy = fuzzy_distance <= threshold
            if not (exact_alias or substring or fuzzy):
                continue
            score = 0
            if normalized_ticker == query:
                score += 100
            if normalized_name == query:
                score += 80
            if exact_alias:
                score += 90
            if normalized_ticker.startswith(query):
                score += 40
            if normalized_name.startswith(query):
                score += 20
            if fuzzy:
                score += 15 - fuzzy_distance
            exchange = _normalize_sec_exchange(item.get("exchange"))
            matches.append(
                (
                    score,
                    Company(
                        cik=str(item.get("cik_str", item.get("cik", ""))).zfill(10),
                        ticker=ticker.upper().replace("/", ".").replace("-", "."),
                        name=name,
                        exchange=exchange,
                    ),
                )
            )
        matches.sort(key=lambda pair: (-pair[0], pair[1].ticker))
        return [company for _, company in matches[:limit]]

    def list_annual_filings(self, company: Company, limit: int = 5) -> list[FilingDocument]:
        requested_limit = max(1, int(limit))
        diagnostics: list[str] = []
        submissions = self._get_json(
            f"{SEC_DATA_BASE}/submissions/CIK{company.cik}.json",
            f"submissions-{company.cik}.json",
        )
        recent = submissions.get("filings", {}).get("recent", {})
        filings: list[FilingDocument] = []

        def append_rows(rows: Any) -> None:
            forms = rows.get("form", []) if isinstance(rows, dict) else []
            accessions = rows.get("accessionNumber", []) if isinstance(rows, dict) else []
            documents = rows.get("primaryDocument", []) if isinstance(rows, dict) else []
            report_dates = rows.get("reportDate", []) if isinstance(rows, dict) else []
            filing_dates = rows.get("filingDate", []) if isinstance(rows, dict) else []
            if not all(isinstance(item, list) for item in (forms, accessions, documents)):
                return
            row_count = min(len(forms), len(accessions), len(documents))
            cik_plain = str(int(company.cik))
            for index in range(row_count):
                form_type = str(forms[index])
                if form_type not in {"10-K", "20-F", "40-F"}:
                    continue
                accession = str(accessions[index])
                primary_document = str(documents[index])
                if not accession or not primary_document:
                    continue
                accession_plain = accession.replace("-", "")
                filings.append(
                    FilingDocument(
                        document_id=f"sec:{company.cik}:{accession}",
                        company_cik=company.cik,
                        accession_number=accession,
                        form_type=form_type,
                        fiscal_period="FY",
                        period_end=str(report_dates[index]) if index < len(report_dates) else "",
                        filed_at=str(filing_dates[index]) if index < len(filing_dates) else "",
                        primary_document=primary_document,
                        source_url=(
                            f"{SEC_ARCHIVES_BASE}/{cik_plain}/{accession_plain}/{primary_document}"
                        ),
                    )
                )

        append_rows(recent)
        files = submissions.get("filings", {}).get("files", [])
        for entry in files if isinstance(files, list) else []:
            if len(filings) >= requested_limit:
                break
            name = str(entry.get("name", "")) if isinstance(entry, dict) else ""
            # SEC's official shard names are flat CIK submissions JSON files.
            # Reject path-like values instead of allowing a server response to
            # influence local cache paths or request unrelated URLs.
            if (
                not name
                or Path(name).name != name
                or not name.startswith(f"CIK{company.cik}-submissions-")
                or not name.endswith(".json")
            ):
                diagnostics.append("historical_shard_invalid_name")
                continue
            try:
                shard = self._get_json(
                    f"{SEC_DATA_BASE}/submissions/{name}", name
                )
                shard_rows = shard.get("filings", shard)
                append_rows(shard_rows)
            except Exception as exc:
                # The return type remains list[FilingDocument] for backwards
                # compatibility. Callers can inspect the bounded diagnostic
                # and still use the recent filings that were already found.
                diagnostics.append(f"historical_shard_failed:{name}:{type(exc).__name__}")

        deduped: dict[str, FilingDocument] = {}
        for filing in filings:
            deduped.setdefault(filing.accession_number, filing)
        result = sorted(
            deduped.values(),
            key=lambda item: (
                str(item.period_end or ""),
                str(item.filed_at or ""),
                str(item.accession_number or ""),
            ),
            reverse=True,
        )
        self.discovery_diagnostics = tuple(dict.fromkeys(diagnostics))
        return result[:requested_limit]

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
        annual_period_ends: dict[int, set[str]] = {}
        for normalized, namespace, candidates in mappings:
            if self._period_kind_for_concept(normalized) != "duration":
                continue
            for reported in candidates:
                definition = namespace.get(reported)
                units = definition.get("units", {}) if isinstance(definition, dict) else {}
                if not isinstance(units, dict):
                    continue
                preferred_unit = self._preferred_unit(
                    normalized, units, getattr(company, "reporting_currency", "")
                )
                if not preferred_unit:
                    continue
                for row in units.get(preferred_unit, []):
                    if not isinstance(row, dict):
                        continue
                    if PeriodSemantics.for_row(row, "duration").complete:
                        annual_period_ends.setdefault(int(row["fy"]), set()).add(str(row["end"]))
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
                period_kind = self._period_kind_for_concept(normalized)
                for row in self._select_annual_facts(
                    units[preferred_unit],
                    allow_foreign=True,
                    period_kind=period_kind,
                    period_ends=annual_period_ends if period_kind == "instant" else None,
                ):
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
                unit_code = preferred_unit.upper() if preferred_unit else ""
                is_share_fact = normalized == "shares_outstanding"
                if is_share_fact:
                    if unit_code not in {"SHARE", "SHARES"}:
                        continue
                    currency = ""
                else:
                    # CompanyFacts values are already reported in base XBRL
                    # units.  Only ISO-like currency units may become trusted
                    # money; ratios/per-share/custom units stay out of this
                    # monetary path instead of being mislabeled.
                    if len(unit_code) != 3 or not unit_code.isalpha():
                        continue
                    currency = unit_code
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
                        unit_provenance="structured_normalized",
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
                for row in self._select_annual_facts(
                    units.get(preferred_unit, []),
                    allow_foreign=True,
                    period_kind="instant",
                    period_ends=annual_period_ends,
                ):
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

        # A separate conservative path covers issuers whose CompanyFacts
        # history consistently contains parent stockholders' equity but no
        # NCI concept at all.  A single missing NCI row is not evidence of
        # zero, so require at least two distinct FY accessions and reject the
        # path when either NCI or total-equity-with-NCI appears anywhere in
        # the issuer payload.  This is the common fully-owned-subsidiary
        # presentation used by AMZN-like filers, while mixed/NCI filers stay
        # quarantined unless an explicit complete formula is available.
        parent_balance_keys = set(parent_rows).intersection(balance_rows)
        parent_balance_years = {key[2] for key in parent_balance_keys}
        if (
            not nci_rows
            and not total_equity_rows
            and len(parent_balance_years) >= 2
        ):
            history_note = f"issuer-wide no reported NCI across {len(parent_balance_years)} FY periods"
            for key in parent_balance_keys:
                parent_item = parent_rows[key]
                balance_item = balance_rows[key]
                balance_value = float(balance_item[1]["val"])
                parent_value = float(parent_item[1]["val"])
                derived_value = balance_value - parent_value
                if derived_value < 0:
                    continue
                inputs = (
                    (balance_item[0], balance_value),
                    (parent_item[0], parent_value),
                )
                formula_candidates.setdefault(key, []).append(
                    (
                        derived_value,
                        f"{balance_item[0]} - {parent_item[0]} ({history_note})",
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
                    unit_provenance="structured_normalized",
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
    def _period_kind_for_concept(normalized: str) -> str:
        """Map standardized statement concepts to XBRL duration semantics."""

        if normalized in {
            "assets", "liabilities", "equity", "total_equity", "cash",
            "accounts_receivable", "inventory", "shares_outstanding",
        }:
            return "instant"
        return "duration"

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
    def _select_annual_facts(
        rows: list[dict[str, Any]],
        *,
        allow_foreign: bool = False,
        period_kind: str = "auto",
        period_ends: dict[int, set[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Select only source-backed, semantically complete fiscal-year facts.

        A filing's ``fp=FY`` is not sufficient for flow facts: SEC CompanyFacts
        can contain a fourth-quarter duration with that label. Duration facts
        need a real 350–380 day span. Instant facts use a separate path: they
        must have no start and their as-of date must be in the labeled FY.
        Later filed complete values (including 10-K/A restatements) take
        precedence. A conflicting duplicate from the same filing revision is
        quarantined rather than resolved by payload order.
        """

        accepted_forms = _ANNUAL_FORMS if allow_foreign else frozenset({"10-K", "10-K/A"})
        candidates: list[tuple[int, str, str, str, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("form", "")).strip().upper() not in accepted_forms:
                continue
            semantics = PeriodSemantics.for_row(row, period_kind)
            if not semantics.complete:
                continue
            fiscal_year = int(row["fy"])
            if semantics.kind == "instant" and period_ends is not None:
                known_ends = period_ends.get(fiscal_year)
                if known_ends and str(row.get("end", "")) not in known_ends:
                    continue
            filed = str(row["filed"])
            accession = str(row["accn"]).strip()
            amended = str(row.get("form", "")).strip().upper().endswith("/A")
            candidates.append((fiscal_year, filed, "1" if amended else "0", accession, row))

        by_year: dict[int, list[tuple[int, str, str, str, dict[str, Any]]]] = {}
        for candidate in candidates:
            by_year.setdefault(candidate[0], []).append(candidate)

        selected: list[dict[str, Any]] = []
        for fiscal_year, year_candidates in by_year.items():
            latest_revision = max(
                (filed, amendment, accession)
                for _, filed, amendment, accession, _ in year_candidates
            )
            top = [
                candidate
                for candidate in year_candidates
                if candidate[1:4] == latest_revision
            ]
            # Duplicate rows in CompanyFacts are common; identical duplicates
            # are harmless. Distinct start/value/end facts from the exact same
            # accession and filed revision are not safely orderable.
            identities = {
                (
                    str(candidate[4].get("start", "")),
                    str(candidate[4].get("end", "")),
                    repr(candidate[4].get("val")),
                )
                for candidate in top
            }
            if len(identities) != 1:
                continue
            selected.append(top[0][4])

        return sorted(selected, key=lambda item: int(item["fy"]), reverse=True)[:10]


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
