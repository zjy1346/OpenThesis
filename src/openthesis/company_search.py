"""Official resolution: persisted provenance, not code-shaped fake issuers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from threading import RLock
from typing import Any, Callable
import uuid

from .domain import Company, utc_now_iso
from .markets import Market, build_company, normalize_market, normalize_symbol
from .official_network import OfficialNetworkError
from .text_normalization import canonical_search_text

SEARCH_CONTRACT = "openthesis.company-search.v2"
DIRECTORY_SOURCES = {
    "CN_A": "https://www.cninfo.com.cn/new/information/topSearch/query",
    "HK": "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_c.json",
    "US": "https://www.sec.gov/files/company_tickers_exchange.json",
}


@dataclass(frozen=True, slots=True)
class CompanySearchResult:
    request_id: str
    query: str
    market: str
    state: str
    companies: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, str], ...] = ()

    def to_dict(self):
        return {"contract": SEARCH_CONTRACT, "request_id": self.request_id,
                "query": self.query, "market": self.market, "state": self.state,
                "companies": list(self.companies), "diagnostics": list(self.diagnostics)}


class OfficialCompanySearch:
    def __init__(self, storage, *, decorate: Callable[[Company], dict] | None = None):
        self.storage = storage
        self.decorate = decorate or (lambda company: company.to_dict())
        self._directory_lock = RLock()

    def search(self, query: str, market: str | Market, fetch: Callable[[], list[Company]], *,
               limit: int = 15, request_id: str = "") -> CompanySearchResult:
        query = query.strip()
        selected = normalize_market(market).value
        if not query or len(query) > 256:
            raise ValueError("company query is invalid")
        identifier = request_id or uuid.uuid4().hex
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
            raise ValueError("company search request_id is invalid")
        bounded = max(1, min(50, int(limit)))
        try:
            companies = fetch()
            if not isinstance(companies, (list, tuple)) or any(
                    not isinstance(company, Company) or company.market != selected
                    or not company.name.strip() or company.name in {company.ticker, company.ticker.split(".", 1)[0]}
                    for company in companies):
                raise OfficialNetworkError("official directory identity is invalid", code="SOURCE_IDENTITY_INVALID")
        except (OfficialNetworkError, OSError, ValueError, TypeError) as error:
            code = getattr(error, "code", "SOURCE_NETWORK_UNAVAILABLE")
            if code not in {"SOURCE_DNS_POLICY_BLOCKED", "SOURCE_RATE_LIMITED", "SOURCE_IDENTITY_INVALID"}:
                code = "SOURCE_NETWORK_UNAVAILABLE"
            cached = self._cached(selected, query, bounded)
            diagnostic = ({"code": code, "diagnostic_id": identifier},)
            return CompanySearchResult(identifier, query, selected, "degraded" if cached else "unavailable",
                tuple(cached or self._unresolved(query, selected)), diagnostic)
        now = utc_now_iso()
        if companies:
            # Concurrent searches in this application must not lose another
            # query's verified identities during a read/merge/write.
            with self._directory_lock:
                entries = self._directory(selected)
                for company in companies:
                    entries[company.ticker] = {"company": company.to_dict(), "verified_at": now}
                self.storage.set_setting(self._key(selected), json.dumps({
                    "contract": SEARCH_CONTRACT, "market": selected,
                    "source": DIRECTORY_SOURCES[selected], "entries": entries,
                }, ensure_ascii=False, sort_keys=True, allow_nan=False))
        candidates = tuple(self._candidate(company, "official_verified", now) for company in companies[:bounded])
        return CompanySearchResult(identifier, query, selected, "ready" if candidates else "empty", candidates)

    @staticmethod
    def _key(market):
        return "official-company-directory-v2:" + market

    def _directory(self, market):
        try:
            payload = json.loads(self.storage.get_setting(self._key(market), "{}"))
            if (payload.get("contract") != SEARCH_CONTRACT or payload.get("market") != market
                    or payload.get("source") != DIRECTORY_SOURCES[market]
                    or not isinstance(payload.get("entries"), dict)):
                return {}
            return payload["entries"]
        except (ValueError, TypeError, AttributeError):
            return {}

    def _cached(self, market, query, limit):
        needle = canonical_search_text(query)
        matches = []
        for ticker, entry in self._directory(market).items():
            try:
                company = Company(**{**entry["company"],
                    "share_classes": tuple(entry["company"].get("share_classes", ()))})
                normalized, _, selected = normalize_symbol(company.ticker, market)
                verified_at = entry["verified_at"]
                if (company.ticker != ticker or normalized != ticker or company.market != selected.value
                        or not company.issuer_id or not isinstance(verified_at, str) or not verified_at
                        or not company.name or company.name in {ticker, ticker.split(".", 1)[0]}):
                    continue
                timestamp = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    continue
                if needle in canonical_search_text(company.name + " " + ticker):
                    matches.append(self._candidate(company, "cached_verified", verified_at))
            except (ValueError, TypeError, KeyError):
                continue
        return matches[:limit]

    def _candidate(self, company, state, verified_at):
        return {**self.decorate(company), "identity_state": state,
                "identity_source": DIRECTORY_SOURCES[company.market], "identity_verified_at": verified_at}

    @staticmethod
    def _unresolved(query, market):
        if market == "US":
            return []
        try:
            company = build_company(query, query, market=market)
        except ValueError:
            return []
        return [{**company.to_dict(), "identity_state": "unresolved_code", "identity_source": "",
                 "identity_verified_at": ""}]

    def require_identity(self, requested: Company, fetch: Callable[[], list[Company]]) -> Company:
        result = self.search(requested.ticker, requested.market, fetch)
        verified = [item for item in result.companies
                    if item["identity_state"] in {"official_verified", "cached_verified"}
                    and item["ticker"] == requested.ticker and item["cik"] == requested.cik]
        if len(verified) != 1:
            code = (result.diagnostics[0]["code"] if result.diagnostics else "SOURCE_IDENTITY_INVALID")
            raise OfficialNetworkError("issuer identity must be verified before research", code=code)
        fields = {key: verified[0][key] for key in Company.__dataclass_fields__}
        fields["share_classes"] = tuple(fields["share_classes"])
        return Company(**fields)
