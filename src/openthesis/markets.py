from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .domain import Company


class Market(StrEnum):
    US = "US"
    CN_A = "CN_A"
    HK = "HK"


class Exchange(StrEnum):
    NASDAQ = "NASDAQ"
    NYSE = "NYSE"
    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"
    HKEX = "HKEX"


class AccountingStandard(StrEnum):
    US_GAAP = "US_GAAP"
    IFRS = "IFRS"
    CAS = "CAS"
    HKFRS = "HKFRS"
    UNKNOWN = "UNKNOWN"


class IndustrySupport(StrEnum):
    STANDARD = "standard"
    FINANCIAL_BETA = "financial_beta"


@dataclass(frozen=True, slots=True)
class MarketProfile:
    market: Market
    label_zh: str
    label_en: str
    exchanges: tuple[Exchange, ...]
    default_currency: str
    default_accounting_standard: AccountingStandard
    requires_sec_identity: bool
    disclosure_home: str


MARKET_PROFILES = {
    Market.US: MarketProfile(
        market=Market.US,
        label_zh="美股",
        label_en="US equities",
        exchanges=(Exchange.NASDAQ, Exchange.NYSE),
        default_currency="USD",
        default_accounting_standard=AccountingStandard.US_GAAP,
        requires_sec_identity=True,
        disclosure_home="https://www.sec.gov/edgar/search/",
    ),
    Market.CN_A: MarketProfile(
        market=Market.CN_A,
        label_zh="A 股",
        label_en="China A-shares",
        exchanges=(Exchange.SSE, Exchange.SZSE, Exchange.BSE),
        default_currency="CNY",
        default_accounting_standard=AccountingStandard.CAS,
        requires_sec_identity=False,
        disclosure_home="https://www.cninfo.com.cn/new/index",
    ),
    Market.HK: MarketProfile(
        market=Market.HK,
        label_zh="港股",
        label_en="Hong Kong equities",
        exchanges=(Exchange.HKEX,),
        default_currency="HKD",
        default_accounting_standard=AccountingStandard.UNKNOWN,
        requires_sec_identity=False,
        disclosure_home="https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
    ),
}


_FINANCIAL_KEYWORDS = (
    "银行",
    "保险",
    "证券",
    "信托",
    "bank",
    "insurance",
    "securities",
    "brokerage",
)


def normalize_market(value: str | Market | None) -> Market:
    if isinstance(value, Market):
        return value
    normalized = str(value or "US").strip().upper().replace("-", "_")
    aliases = {
        "A": Market.CN_A,
        "A_SHARE": Market.CN_A,
        "A_SHARES": Market.CN_A,
        "CN": Market.CN_A,
        "CHINA": Market.CN_A,
        "HKEX": Market.HK,
        "HONG_KONG": Market.HK,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return Market(normalized)
    except ValueError as exc:
        raise ValueError("unsupported market") from exc


def market_profile(value: str | Market) -> MarketProfile:
    return MARKET_PROFILES[normalize_market(value)]


def industry_support(name: str = "", industry: str = "") -> IndustrySupport:
    searchable = f"{name} {industry}".casefold()
    if any(keyword.casefold() in searchable for keyword in _FINANCIAL_KEYWORDS):
        return IndustrySupport.FINANCIAL_BETA
    return IndustrySupport.STANDARD


def normalize_symbol(symbol: str, market: str | Market | None = None) -> tuple[str, Exchange, Market]:
    raw = symbol.strip().upper().replace(" ", "")
    if not raw:
        raise ValueError("security symbol is required")

    suffixes = {
        ".SH": (Exchange.SSE, Market.CN_A),
        ".SS": (Exchange.SSE, Market.CN_A),
        ".SZ": (Exchange.SZSE, Market.CN_A),
        ".BJ": (Exchange.BSE, Market.CN_A),
        ".HK": (Exchange.HKEX, Market.HK),
    }
    for suffix, (exchange, resolved_market) in suffixes.items():
        if raw.endswith(suffix):
            code = raw[: -len(suffix)]
            return _format_symbol(code, exchange), exchange, resolved_market

    requested = normalize_market(market) if market else None
    if requested == Market.HK or (requested is None and re.fullmatch(r"\d{4,5}", raw)):
        return _format_symbol(raw, Exchange.HKEX), Exchange.HKEX, Market.HK
    if requested == Market.CN_A or (requested is None and re.fullmatch(r"\d{6}", raw)):
        exchange = infer_a_share_exchange(raw)
        return _format_symbol(raw, exchange), exchange, Market.CN_A
    if requested in {None, Market.US} and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", raw):
        return raw, Exchange.NASDAQ, Market.US
    raise ValueError("security symbol does not match the selected market")


def infer_a_share_exchange(code: str) -> Exchange:
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("A-share symbols must contain six digits")
    if code.startswith(("43", "83", "87", "88", "92")):
        return Exchange.BSE
    if code.startswith(("0", "2", "3")):
        return Exchange.SZSE
    if code.startswith(("5", "6", "9")):
        return Exchange.SSE
    raise ValueError("unable to infer the A-share exchange; use .SH, .SZ, or .BJ")


def build_company(
    symbol: str,
    name: str,
    *,
    market: str | Market | None = None,
    issuer_id: str = "",
    industry: str = "",
    accounting_standard: str = "",
    reporting_currency: str = "",
) -> Company:
    normalized_symbol, exchange, resolved_market = normalize_symbol(symbol, market)
    profile = MARKET_PROFILES[resolved_market]
    security_id = f"{resolved_market.value}:{exchange.value}:{normalized_symbol}"
    standard = accounting_standard.strip().upper() or profile.default_accounting_standard.value
    support = industry_support(name, industry)
    return Company(
        cik=security_id,
        ticker=normalized_symbol,
        name=name.strip() or normalized_symbol,
        exchange=exchange.value,
        issuer_id=issuer_id.strip() or security_id,
        market=resolved_market.value,
        security_id=security_id,
        listing_currency=profile.default_currency,
        reporting_currency=reporting_currency.strip().upper() or profile.default_currency,
        accounting_standard=standard,
        industry=industry.strip(),
        industry_support=support.value,
        source_url=_source_url(normalized_symbol, exchange),
    )


def search_companies(
    query: str,
    *,
    market: str | Market | None = None,
    companies: Iterable[Company] | None = None,
    limit: int = 15,
) -> list[Company]:
    normalized = query.strip()
    if not normalized:
        raise ValueError("company query is required")
    requested_market = normalize_market(market) if market else None
    pool = tuple(companies or COMMON_MARKET_COMPANIES)
    needle = normalized.casefold()
    matches = [
        company
        for company in pool
        if (requested_market is None or company.market == requested_market.value)
        and (
            needle in company.name.casefold()
            or needle in company.ticker.casefold()
            or needle in company.ticker.replace(".", "").casefold()
        )
    ]
    if matches:
        return matches[: max(1, limit)]
    if requested_market in {Market.CN_A, Market.HK} or re.search(r"\d", normalized):
        try:
            return [build_company(normalized, normalized, market=requested_market)][:limit]
        except ValueError:
            return []
    return []


def _format_symbol(code: str, exchange: Exchange) -> str:
    if exchange == Exchange.HKEX:
        if not code.isdigit() or len(code) > 5:
            raise ValueError("Hong Kong stock codes must contain at most five digits")
        return f"{int(code):05d}.HK"
    if exchange in {Exchange.SSE, Exchange.SZSE, Exchange.BSE}:
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("A-share symbols must contain six digits")
        suffix = {Exchange.SSE: "SH", Exchange.SZSE: "SZ", Exchange.BSE: "BJ"}[exchange]
        return f"{code}.{suffix}"
    return code


def _source_url(symbol: str, exchange: Exchange) -> str:
    code = symbol.split(".", 1)[0]
    if exchange == Exchange.SSE:
        return "https://www.sse.com.cn/assortment/stock/list/info/announcement/"
    if exchange == Exchange.SZSE:
        return "https://www.szse.cn/disclosure/notice/company/index.html"
    if exchange == Exchange.BSE:
        return "https://www.bse.cn/disclosure/announcement.html"
    if exchange == Exchange.HKEX:
        return f"https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh&market=SEHK&searchType=1&stockCode={code}"
    return "https://www.sec.gov/edgar/search/"


def _known(
    symbol: str,
    name: str,
    *,
    issuer_id: str,
    industry: str = "",
    accounting_standard: str = "",
    reporting_currency: str = "",
) -> Company:
    return build_company(
        symbol,
        name,
        issuer_id=issuer_id,
        industry=industry,
        accounting_standard=accounting_standard,
        reporting_currency=reporting_currency,
    )


COMMON_MARKET_COMPANIES = (
    _known("00005.HK", "HSBC Holdings", issuer_id="HK:HSBC", industry="bank", accounting_standard="IFRS", reporting_currency="USD"),
    _known("600519.SH", "贵州茅台", issuer_id="CN:KWEICHOW-MOUTAI", industry="食品饮料"),
    _known("000858.SZ", "五粮液", issuer_id="CN:WULIANGYE", industry="食品饮料"),
    _known("300750.SZ", "宁德时代", issuer_id="CN:CATL", industry="电力设备"),
    _known("688981.SH", "中芯国际", issuer_id="CN:SMIC", industry="半导体"),
    _known("832982.BJ", "锦波生物", issuer_id="CN:JPBIO", industry="生物科技"),
    _known("600036.SH", "招商银行", issuer_id="CN:CMB", industry="银行"),
    _known("00700.HK", "腾讯控股", issuer_id="HK:TENCENT", industry="互联网", accounting_standard="IFRS", reporting_currency="CNY"),
    _known("09988.HK", "阿里巴巴-W", issuer_id="HK:ALIBABA", industry="互联网", accounting_standard="US_GAAP", reporting_currency="CNY"),
    _known("03690.HK", "美团-W", issuer_id="HK:MEITUAN", industry="互联网", accounting_standard="IFRS", reporting_currency="CNY"),
    _known("03750.HK", "宁德时代", issuer_id="CN:CATL", industry="电力设备", accounting_standard="CAS", reporting_currency="CNY"),
    _known("03968.HK", "招商银行", issuer_id="CN:CMB", industry="银行", accounting_standard="CAS", reporting_currency="CNY"),
)
