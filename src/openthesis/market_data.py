from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError

from .domain import Company, FilingDocument
from .filing_selection import select_research_filings
from .markets import (
    COMMON_MARKET_COMPANIES,
    Exchange,
    Market,
    build_company,
    normalize_market,
    search_companies,
)


class MarketDataError(RuntimeError):
    """A public-disclosure source could not return a trustworthy result."""

    def __init__(self, message: str, *, code: str = "FILING_FETCH_FAILED"):
        super().__init__(message)
        self.code = code


class HttpTransport(Protocol):
    def get_json(self, url: str) -> Any: ...

    def post_form(self, url: str, fields: dict[str, str]) -> Any: ...

    def get_text(self, url: str) -> str: ...

    def download(self, url: str, target: Path) -> Path: ...


class OfficialDisclosureHttpClient:
    _ALLOWED_HOSTS = {
        "www.cninfo.com.cn",
        "static.cninfo.com.cn",
        "www1.hkexnews.hk",
        "www.hkexnews.hk",
    }

    def __init__(self, *, timeout_seconds: int = 30, maximum_bytes: int = 50_000_000):
        self.timeout_seconds = max(5, min(120, int(timeout_seconds)))
        self.maximum_bytes = max(1_000_000, min(100_000_000, int(maximum_bytes)))

    def get_json(self, url: str) -> Any:
        return json.loads(self._request(url).decode("utf-8-sig"))

    def post_form(self, url: str, fields: dict[str, str]) -> Any:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        return json.loads(
            self._request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                },
            ).decode("utf-8-sig")
        )

    def get_text(self, url: str) -> str:
        return self._request(url).decode("utf-8", errors="replace")

    def download(self, url: str, target: Path) -> Path:
        payload = self._request(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target

    def _request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        self._validate_url(url)
        request_headers = {
            "Accept": "application/json,text/html,application/pdf;q=0.9,*/*;q=0.5",
            "User-Agent": "OpenThesis/1.2 public-disclosure research client",
            "Referer": "https://www.cninfo.com.cn/" if "cninfo" in url else "https://www1.hkexnews.hk/",
            **(headers or {}),
        }
        request = urllib.request.Request(url, data=data, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                self._validate_url(response.geturl())
                payload = response.read(self.maximum_bytes + 1)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise MarketDataError("official disclosure source is unavailable") from exc
        if len(payload) > self.maximum_bytes:
            raise MarketDataError("official disclosure document exceeds the size limit")
        return payload

    @classmethod
    def _validate_url(cls, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in cls._ALLOWED_HOSTS:
            raise MarketDataError("unsupported disclosure source URL")


@dataclass(slots=True)
class EvidenceBundle:
    company: Company
    filings: list[FilingDocument]
    facts: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    warnings: list[str]


class MarketAdapter(Protocol):
    market: Market

    def resolve(self, query: str, *, limit: int = 15) -> list[Company]: ...

    def list_financial_filings(self, company: Company, *, limit: int = 5) -> list[FilingDocument]: ...

    def download_filing(self, filing: FilingDocument, target_dir: Path) -> FilingDocument: ...


class CnInfoAdapter:
    market = Market.CN_A
    _SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
    _COLUMN = {Exchange.SSE: "sse", Exchange.SZSE: "szse", Exchange.BSE: "bj"}

    def __init__(self, transport: HttpTransport | None = None):
        self.transport = transport or OfficialDisclosureHttpClient()
        self._stock_rows: list[dict[str, str]] | None = None

    def resolve(self, query: str, *, limit: int = 15) -> list[Company]:
        normalized = query.strip()
        if not normalized:
            raise ValueError("company query is required")
        try:
            rows = self._search_rows(normalized, limit)
        except (MarketDataError, ValueError, TypeError):
            return search_companies(query, market=Market.CN_A, limit=limit)
        return [self._company(row) for row in rows[: max(1, limit)]] or search_companies(
            query, market=Market.CN_A, limit=limit
        )

    def list_financial_filings(self, company: Company, *, limit: int = 5) -> list[FilingDocument]:
        exchange = Exchange(company.exchange)
        row = next(
            (item for item in self._search_rows(company.ticker[:6], 10) if item["code"] == company.ticker[:6]),
            None,
        )
        if row is None:
            raise MarketDataError("company is not present in the official disclosure catalogue")
        query_fields = {
                "pageNum": "1",
                # The source page size is deliberately independent of the number
                # of annual years requested. Summaries and interim reports must
                # not crowd older annual reports out of discovery.
                "pageSize": "30",
                "column": self._COLUMN[exchange],
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{row['code']},{row['org_id']}",
                "searchkey": "",
                "secid": "",
                # Query full annual reports separately. A combined periodic
                # category can push older A-share annuals behind many quarterly
                # and mirrored H-share announcements.
                "category": "category_ndbg_szsh;",
                "trade": "",
                "seDate": "",
                "sortName": "time",
                "sortType": "desc",
                "isHLtitle": "true",
            }
        payload = self.transport.post_form(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            query_fields,
        )
        if not isinstance(payload, dict):
            raise MarketDataError(
                "CNInfo returned an invalid announcement response",
                code="FILING_STATUS_UNVERIFIED",
            )
        announcements = payload.get("announcements")
        total_announcements = _optional_nonnegative_int(payload.get("totalAnnouncement"))
        financial_result_empty = False
        if announcements is None:
            if total_announcements == 0:
                announcements = []
                financial_result_empty = True
            else:
                raise MarketDataError(
                    "CNInfo did not return a verifiable announcement list",
                    code="FILING_STATUS_UNVERIFIED",
                )
        if not isinstance(announcements, list):
            raise MarketDataError(
                "CNInfo returned an unsupported announcement list",
                code="FILING_STATUS_UNVERIFIED",
            )
        if not announcements:
            if total_announcements in {None, 0}:
                financial_result_empty = True
            else:
                raise MarketDataError(
                    "CNInfo returned an inconsistent announcement count",
                    code="FILING_STATUS_UNVERIFIED",
                )
        if total_announcements and total_announcements > len(announcements):
            page_count = min(10, (total_announcements + 29) // 30)
            for page_number in range(2, page_count + 1):
                page_fields = dict(query_fields)
                page_fields["pageNum"] = str(page_number)
                page_payload = self.transport.post_form(
                    "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                    page_fields,
                )
                page_rows = page_payload.get("announcements") if isinstance(page_payload, dict) else None
                if not isinstance(page_rows, list):
                    break
                announcements.extend(page_rows)
        periodic_fields = dict(query_fields)
        periodic_fields["pageNum"] = "1"
        periodic_fields["category"] = (
            "category_yjdbg_szsh;category_bndbg_szsh;category_sjdbg_szsh;"
        )
        periodic_payload = self.transport.post_form(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            periodic_fields,
        )
        periodic_rows = (
            periodic_payload.get("announcements")
            if isinstance(periodic_payload, dict)
            else None
        )
        if isinstance(periodic_rows, list):
            announcements.extend(periodic_rows)
        filings = [self._filing(company, item) for item in announcements if isinstance(item, dict)]
        usable = [item for item in filings if item is not None]
        if not any(item.form_type == "ANNUAL_REPORT" for item in usable):
            listing_payload = self.transport.post_form(
                "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                {
                    "pageNum": "1",
                    "pageSize": "30",
                    "column": self._COLUMN[exchange],
                    "tabName": "fulltext",
                    "plate": "",
                    "stock": f"{row['code']},{row['org_id']}",
                    "searchkey": "招股说明书",
                    "secid": "",
                    "category": "",
                    "trade": "",
                    "seDate": "",
                    "sortName": "time",
                    "sortType": "desc",
                    "isHLtitle": "true",
                },
            )
            if isinstance(listing_payload, dict) and isinstance(listing_payload.get("announcements"), list):
                usable.extend(
                    item
                    for item in (
                        self._filing(company, row_item)
                        for row_item in listing_payload["announcements"]
                        if isinstance(row_item, dict)
                    )
                    if item is not None
                )
        if not usable:
            if financial_result_empty:
                return []
            raise MarketDataError(
                "CNInfo announcements contain no supported financial reports",
                code="FILING_FORMAT_UNSUPPORTED",
            )
        selection = select_research_filings(usable, annual_limit=limit)
        return list(selection.documents)

    def download_filing(self, filing: FilingDocument, target_dir: Path) -> FilingDocument:
        suffix = Path(urllib.parse.urlparse(filing.source_url).path).suffix or ".pdf"
        target = target_dir / f"{filing.accession_number}{suffix.lower()}"
        self.transport.download(filing.source_url, target)
        filing.local_path = str(target)
        filing.content_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        return filing

    def _search_rows(self, query: str, limit: int) -> list[dict[str, str]]:
        payload = self.transport.post_form(
            self._SEARCH_URL,
            {"keyWord": query, "maxSecNum": str(max(1, min(50, limit)))},
        )
        rows: list[dict[str, str]] = []
        for item in _find_dict_rows(payload):
            code = _first_text(item, "code", "secCode", "stockCode")
            name = _first_text(item, "zwjc", "secName", "name", "shortName")
            org_id = _first_text(item, "orgId", "orgid", "org_id")
            if re.fullmatch(r"\d{6}", code) and name and org_id:
                try:
                    company = build_company(code, name, market=Market.CN_A)
                except ValueError:
                    continue
                rows.append(
                    {
                        "code": code,
                        "name": name,
                        "org_id": org_id,
                        "exchange": company.exchange,
                    }
                )
        return rows

    @staticmethod
    def _company(row: dict[str, str]) -> Company:
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[row["exchange"]]
        symbol = f"{row['code']}.{suffix}"
        known = next((item for item in COMMON_MARKET_COMPANIES if item.ticker == symbol), None)
        if known is not None:
            return Company(**known.to_dict())
        return build_company(
            symbol,
            row["name"],
            issuer_id=f"CNINFO:{row['org_id'] or row['code']}",
        )

    @staticmethod
    def _filing(company: Company, item: dict[str, Any]) -> FilingDocument | None:
        adjunct = str(item.get("adjunctUrl", "")).strip().lstrip("/")
        if not adjunct or not adjunct.lower().endswith((".pdf", ".html", ".htm")):
            return None
        title = re.sub(r"<[^>]+>", "", str(item.get("announcementTitle", "")))
        if "摘要" in title or "summary" in title.casefold():
            return None
        form_type, fiscal_period = _classify_report(title)
        if not form_type:
            return None
        announcement_id = str(item.get("announcementId", "")).strip() or hashlib.sha256(adjunct.encode()).hexdigest()[:20]
        filed_at = _timestamp_to_iso(item.get("announcementTime"))
        year_match = re.search(r"(20\d{2})", title)
        period_end = _report_period_end(
            int(year_match.group(1)) if year_match else int(filed_at[:4]),
            fiscal_period,
            filed_at,
        )
        return FilingDocument(
            document_id=f"cninfo:{announcement_id}",
            company_cik=company.cik,
            accession_number=announcement_id,
            form_type=form_type,
            fiscal_period=fiscal_period,
            period_end=period_end,
            filed_at=filed_at,
            primary_document=title or Path(adjunct).name,
            source_url=f"https://static.cninfo.com.cn/{adjunct}",
        )


class HkexNewsAdapter:
    market = Market.HK
    _STOCK_LIST_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_c.json"

    def __init__(self, transport: HttpTransport | None = None):
        self.transport = transport or OfficialDisclosureHttpClient()
        self._stock_rows: list[dict[str, str]] | None = None

    def resolve(self, query: str, *, limit: int = 15) -> list[Company]:
        needle = query.strip().casefold()
        if not needle:
            raise ValueError("company query is required")
        try:
            rows = self._stocks()
        except MarketDataError:
            return search_companies(query, market=Market.HK, limit=limit)
        matches = [row for row in rows if needle in row["code"].casefold() or needle in row["name"].casefold()]
        return [self._company(row) for row in matches[: max(1, limit)]] or search_companies(
            query, market=Market.HK, limit=limit
        )

    def list_financial_filings(self, company: Company, *, limit: int = 5) -> list[FilingDocument]:
        code = company.ticker[:5]
        prefix_url = "https://www1.hkexnews.hk/search/prefix.do?" + urllib.parse.urlencode(
            {
                "callback": "openthesis",
                "type": "A",
                "name": code,
                "market": "SEHK",
                "lang": "EN",
            }
        )
        prefix_text = self.transport.get_text(prefix_url)
        stock_match = re.search(r'"stockId"\s*:\s*(\d+)', prefix_text)
        if not stock_match:
            raise MarketDataError("company is not present in the HKEX disclosure catalogue")
        stock_id = stock_match.group(1)
        # The title-search servlet is the bounded, official JSON discovery
        # interface.  Query report-type filters individually so unrelated
        # earnings releases cannot crowd annual/interim reports out of the
        # source page.  The old HTML page remains a conservative fallback for
        # deployments where the servlet is unavailable.
        titles = ("Annual Report", "Interim Report", "Quarterly Report", "Half-Year Report")
        discovered: list[FilingDocument] = []
        json_successes = 0
        json_failures = 0
        explicit_empty = 0
        annual_count = 0
        for title_filter in titles:
            # Once the requested annual history is present, do not fetch more
            # annual pages; periodic filters are still queried for a current
            # year without an annual filing.
            if title_filter == "Annual Report" and annual_count >= max(1, limit):
                continue
            now = datetime.now(timezone.utc)
            from_date = f"{max(2000, now.year - 10):04d}0101"
            query = {
                "sortDir": "0",
                "sortByOptions": "DateTime",
                "category": "0",
                "market": "SEHK",
                "stockId": stock_id,
                "documentType": "-1",
                "fromDate": from_date,
                "toDate": now.strftime("%Y%m%d"),
                "title": title_filter,
                "searchType": "0",
                "t1code": "-2",
                "t2Gcode": "-2",
                "t2code": "-2",
                "rowRange": "100",
                "lang": "E",
            }
            search_url = "https://www1.hkexnews.hk/search/titleSearchServlet.do?" + urllib.parse.urlencode(query)
            try:
                payload = self.transport.get_json(search_url)
                rows = _hkex_json_rows(payload)
            except Exception:
                json_failures += 1
                continue
            if rows is None:
                json_failures += 1
                continue
            json_successes += 1
            if rows == []:
                explicit_empty += 1
                continue
            parsed = _hkex_filings_from_json(company, rows, limit=100)
            discovered.extend(parsed)
            annual_count = len({item.period_end[:4] for item in discovered if item.form_type == "ANNUAL_REPORT"})

        if json_successes:
            filings = list(select_research_filings(_dedupe_hkex_filings(discovered), annual_limit=limit).documents)
            if filings:
                return filings
            if explicit_empty == json_successes and not json_failures:
                return []
            # A valid response containing only unsupported titles is not an
            # authorization to scrape unrelated rows; report it explicitly.
            if not json_failures:
                raise MarketDataError(
                    "HKEX returned no supported financial reports",
                    code="FILING_FORMAT_UNSUPPORTED",
                )

        # Compatibility fallback for older HKEX deployments or malformed JSON.
        search_url = "https://www1.hkexnews.hk/search/titlesearch.xhtml?" + urllib.parse.urlencode(
            {"category": "0", "lang": "EN", "market": "SEHK", "stockId": stock_id}
        )
        text = self.transport.get_text(search_url)
        filings = list(select_research_filings(_hkex_filings_from_text(company, text, limit=30), annual_limit=limit).documents)
        if filings:
            return filings
        if _is_explicit_empty_hkex_result(text):
            return []
        raise MarketDataError(
            "HKEX did not return a verifiable financial-report result",
            code="FILING_STATUS_UNVERIFIED",
        )

    def download_filing(self, filing: FilingDocument, target_dir: Path) -> FilingDocument:
        target = target_dir / f"{filing.accession_number}.pdf"
        self.transport.download(filing.source_url, target)
        filing.local_path = str(target)
        filing.content_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        return filing

    def _stocks(self) -> list[dict[str, str]]:
        if self._stock_rows is not None:
            return self._stock_rows
        try:
            payload = self.transport.get_json(self._STOCK_LIST_URL)
        except Exception as exc:
            raise MarketDataError("HKEX company catalogue is unavailable") from exc
        rows: list[dict[str, str]] = []
        for item in _find_dict_rows(payload):
            code = _first_text(item, "code", "stockCode", "stock_code", "c").zfill(5)
            name = _first_text(item, "name", "stockName", "stock_name", "cName", "n")
            stock_id = _first_text(item, "stockId", "stock_id", "id", "s")
            if re.fullmatch(r"\d{5}", code) and name and stock_id:
                rows.append({"code": code, "name": name, "stock_id": stock_id})
        if not rows:
            raise MarketDataError("HKEX company catalogue returned no usable records")
        self._stock_rows = rows
        return rows

    @staticmethod
    def _company(row: dict[str, str]) -> Company:
        symbol = f"{row['code']}.HK"
        known = next((item for item in COMMON_MARKET_COMPANIES if item.ticker == symbol), None)
        if known is not None:
            return Company(**known.to_dict())
        return build_company(
            symbol,
            row["name"],
            issuer_id=f"HKEX:{row['stock_id']}",
            accounting_standard="UNKNOWN",
        )


class MarketDataModule:
    """Deep module hiding public-disclosure source differences from research callers."""

    def __init__(
        self,
        *,
        cn_adapter: MarketAdapter | None = None,
        hk_adapter: MarketAdapter | None = None,
    ):
        self._adapters = {
            Market.CN_A: cn_adapter or CnInfoAdapter(),
            Market.HK: hk_adapter or HkexNewsAdapter(),
        }

    def resolve(self, query: str, market: str | Market, *, limit: int = 15) -> list[Company]:
        normalized = normalize_market(market)
        if normalized == Market.US:
            raise ValueError("US company resolution remains owned by the SEC adapter")
        return self._adapters[normalized].resolve(query, limit=limit)

    def adapter_for(self, company: Company) -> MarketAdapter:
        market = normalize_market(company.market)
        if market == Market.US:
            raise ValueError("US filings remain owned by the SEC adapter")
        return self._adapters[market]


def _find_dict_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ("code", "stockCode", "secCode", "c")):
            rows.append(value)
        for child in value.values():
            rows.extend(_find_dict_rows(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_find_dict_rows(child))
    return rows


def _first_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value not in {None, ""}:
            return str(value).strip()
    return ""


def _timestamp_to_iso(value: Any) -> str:
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _report_period_end(year: int, fiscal_period: str, filed_at: str) -> str:
    suffix = {"FY": "12-31", "H1": "06-30", "Q1": "03-31", "Q3": "09-30"}.get(fiscal_period)
    return f"{year:04d}-{suffix}" if suffix else filed_at[:10]


def _is_explicit_empty_hkex_result(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).casefold()
    return any(
        marker in normalized
        for marker in (
            "no matching record",
            "no matching result",
            "no records found",
            "沒有符合條件的紀錄",
            "沒有符合條件的記錄",
        )
    )


def _classify_report(title: str) -> tuple[str, str]:
    lowered = title.casefold()
    if "半年度报告" in title or "中期报告" in title or "interim report" in lowered or "half-year report" in lowered:
        return "INTERIM_REPORT", "H1"
    if "招股说明书" in title or "prospectus" in lowered:
        return "PROSPECTUS", "IPO"
    if "上市公告书" in title or "listing document" in lowered:
        return "LISTING_REPORT", "IPO"
    if "一季度报告" in title or "1季度报告" in title:
        return "QUARTERLY_REPORT", "Q1"
    if "三季度报告" in title or "3季度报告" in title:
        return "QUARTERLY_REPORT", "Q3"
    if re.search(r"\b(?:first|1st) quarter(?:ly)?(?:\s+financial)?\s+report\b", lowered):
        return "QUARTERLY_REPORT", "Q1"
    if re.search(r"\b(?:third|3rd) quarter(?:ly)?(?:\s+financial)?\s+report\b", lowered):
        return "QUARTERLY_REPORT", "Q3"
    if "年度报告" in title or "年报" in title or "annual report" in lowered:
        return "ANNUAL_REPORT", "FY"
    if "季度报告" in title or "quarterly report" in lowered:
        return "QUARTERLY_REPORT", "Q"
    return "", ""


def _hkex_json_rows(payload: Any) -> list[dict[str, Any]] | None:
    """Normalize the HKEX servlet's list or nested JSON-string result.

    ``None`` denotes a malformed/unsupported response (source failure), while
    an empty list is an explicit, valid no-match response.
    """
    if not isinstance(payload, dict):
        return None
    result: Any = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return None
    if isinstance(result, dict):
        result = result.get("data", result.get("rows", result.get("result")))
    if not isinstance(result, list):
        return None
    if any(not isinstance(item, dict) for item in result):
        return None
    return list(result)


def _clean_hkex_title(value: Any) -> str:
    text = html_lib.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _hkex_file_url(value: Any) -> tuple[str, str] | None:
    raw = html_lib.unescape(str(value or "")).replace("\\/", "/").strip()
    if not raw:
        return None
    parsed = urllib.parse.urlparse(raw)
    path = parsed.path or raw
    if not re.search(r"\.pdf$", path, re.IGNORECASE) or not path.casefold().startswith("/listedco/listconews/"):
        return None
    if not path.startswith("/"):
        path = "/" + path
    host = (parsed.hostname or "www1.hkexnews.hk").lower()
    if host not in {"www1.hkexnews.hk", "www.hkexnews.hk"}:
        return None
    return path, f"https://{host}{path}"


def _hkex_date_time(value: Any, fallback_year: int | None = None) -> str:
    text = str(value or "").strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    if fallback_year:
        return f"{fallback_year:04d}-01-01T00:00:00+00:00"
    return datetime.now(timezone.utc).isoformat()


def _dedupe_hkex_filings(filings: list[FilingDocument]) -> list[FilingDocument]:
    seen: set[str] = set()
    result: list[FilingDocument] = []
    for filing in filings:
        key = filing.source_url.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(filing)
    return result


def _hkex_filings_from_json(
    company: Company,
    rows: list[dict[str, Any]],
    *,
    limit: int = 100,
) -> list[FilingDocument]:
    filings: list[FilingDocument] = []
    seen: set[str] = set()
    for row in rows:
        url_parts = _hkex_file_url(row.get("FILE_LINK") or row.get("fileLink") or row.get("FILELINK"))
        if url_parts is None:
            continue
        path, source_url = url_parts
        if source_url.casefold() in seen:
            continue
        title = _clean_hkex_title(row.get("TITLE") or row.get("title") or row.get("LONG_TEXT"))
        form_type, fiscal_period = _classify_report(title)
        if not form_type:
            continue
        seen.add(source_url.casefold())
        accession = Path(path).stem
        year_match = re.search(r"\b(20\d{2})\b", title)
        filed_at = _hkex_date_time(
            row.get("DATE_TIME") or row.get("dateTime"),
            int(year_match.group(1)) if year_match else None,
        )
        period_end = _report_period_end(
            int(year_match.group(1)) if year_match else int(filed_at[:4]),
            fiscal_period,
            filed_at,
        )
        filings.append(
            FilingDocument(
                document_id=f"hkex:{accession}",
                company_cik=company.cik,
                accession_number=accession,
                form_type=form_type,
                fiscal_period=fiscal_period,
                period_end=period_end,
                filed_at=filed_at,
                primary_document=title or accession,
                source_url=source_url,
            )
        )
        if len(filings) >= max(1, limit):
            break
    return filings


def _hkex_filings_from_text(company: Company, text: str, *, limit: int) -> list[FilingDocument]:
    decoded = html_lib.unescape(text.replace("\\/", "/").replace("\\u0026", "&"))
    urls = re.findall(
        r"(?:https://www1?\.hkexnews\.hk)?(/listedco/listconews/(?:sehk|gem)/\d{4}/\d{4}/[A-Za-z0-9_.-]+\.pdf)",
        decoded,
        flags=re.IGNORECASE,
    )
    filings: list[FilingDocument] = []
    for path in dict.fromkeys(urls):
        anchor = re.search(
            rf"<a\b[^>]*href\s*=\s*[\"'][^\"']*{re.escape(path)}[^\"']*[\"'][^>]*>(.*?)</a>",
            decoded,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if anchor is None:
            continue
        title = re.sub(r"<[^>]+>", " ", html_lib.unescape(anchor.group(1)))
        title = re.sub(r"\s+", " ", title).strip()
        form_type, fiscal_period = _classify_report(title)
        if not form_type:
            continue
        accession = Path(path).stem
        date_match = re.search(r"/(\d{4})/(\d{4})/", path)
        filed_at = (
            f"{date_match.group(1)}-{date_match.group(2)[:2]}-{date_match.group(2)[2:]}T00:00:00+00:00"
            if date_match
            else datetime.now(timezone.utc).isoformat()
        )
        year_match = re.search(r"(20\d{2})", title)
        period_end = _report_period_end(
            int(year_match.group(1)) if year_match else int(filed_at[:4]),
            fiscal_period,
            filed_at,
        )
        filings.append(
            FilingDocument(
                document_id=f"hkex:{accession}",
                company_cik=company.cik,
                accession_number=accession,
                form_type=form_type,
                fiscal_period=fiscal_period,
                period_end=period_end,
                filed_at=filed_at,
                primary_document=title[:240] or accession,
                source_url=f"https://www1.hkexnews.hk{path}",
            )
        )
        if len(filings) >= limit:
            break
    return filings
