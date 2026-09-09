"""Auditable market quotes used by the reverse DCF boundary.

Provider-specific response shapes deliberately stop at this module.  The
research workflow receives only the small, normalised snapshot below; raw
provider payloads are never persisted or sent to a model.
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError

try:
    import truststore
except ImportError:  # pragma: no cover - exercised only before install
    truststore = None  # type: ignore[assignment]

from .domain import Company
from .markets import Exchange, Market, normalize_market


class SnapshotStatus(StrEnum):
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    MANUAL = "MANUAL"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class ReverseDcfPolicy:
    """Versioned model/data freshness policy, with zero-input defaults."""

    version: str = "reverse-dcf-policy-v1"
    horizon_years: int = 5
    discount_rate: float = 0.10
    terminal_growth: float = 0.03
    max_quote_age_days: int = 7
    request_timeout_seconds: float = 4.0
    max_retries: int = 1
    conflict_tolerance: float = 0.05
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.horizon_years < 1 or self.discount_rate <= self.terminal_growth:
            raise ValueError("invalid reverse DCF policy")
        if self.max_quote_age_days < 0 or self.max_retries < 0 or self.circuit_breaker_failures < 1:
            raise ValueError("invalid market freshness policy")


@dataclass(frozen=True, slots=True)
class SymbolMapping:
    provider_symbol: str
    exchange: str
    market: str


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    provider: str
    symbol: str
    exchange: str
    price: float | None
    market_cap: float | None
    currency: str
    as_of: str
    retrieved_at: str
    market_cap_scope: str = "security"
    source_id: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FxSnapshot:
    from_currency: str
    to_currency: str
    rate: float
    as_of: str
    source: str


@dataclass(frozen=True, slots=True)
class MarketSnapshotOutcome:
    status: SnapshotStatus
    issuer_id: str
    security_id: str
    symbol: str = ""
    exchange: str = ""
    price: float | None = None
    market_cap: float | None = None
    equity_market_value: float | None = None
    currency: str = ""
    reporting_currency: str = ""
    as_of: str = ""
    provider: str = ""
    source_id: str = ""
    retrieved_at: str = ""
    fx_rate: float | None = None
    fx_as_of: str = ""
    fx_source: str = ""
    cache: str = "miss"
    error_code: str = ""
    warnings: tuple[str, ...] = ()
    policy_version: str = "reverse-dcf-policy-v1"
    quote_currency: str = ""
    valuation_currency: str = ""

    @property
    def allows_valuation(self) -> bool:
        return self.status in {SnapshotStatus.VERIFIED, SnapshotStatus.MANUAL} and self.equity_market_value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "source": self.provider,
            "issuer_id": self.issuer_id,
            "security_id": self.security_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "price": self.price,
            "market_cap": self.market_cap,
            "equity_market_value": self.equity_market_value,
            # ``currency`` remains the backwards-compatible valuation/report
            # currency; raw market_cap is explicitly paired with
            # ``quote_currency`` below.
            "currency": self.valuation_currency or self.reporting_currency or self.currency,
            "quote_currency": self.quote_currency or self.currency,
            "reporting_currency": self.reporting_currency or self.valuation_currency,
            "valuation_currency": self.valuation_currency or self.reporting_currency or self.currency,
            "as_of": self.as_of,
            "provider": self.provider,
            "source_id": self.source_id,
            "retrieved_at": self.retrieved_at,
            "fx_rate": self.fx_rate,
            "fx_as_of": self.fx_as_of,
            "fx_source": self.fx_source,
            "cache": self.cache,
            "error_code": self.error_code,
            "warnings": list(self.warnings),
            "policy_version": self.policy_version,
        }


class QuoteAdapter(Protocol):
    name: str

    def quote(self, company: Company, mapping: SymbolMapping, *, policy: ReverseDcfPolicy) -> QuoteSnapshot: ...


class FxAdapter(Protocol):
    def rate(self, from_currency: str, to_currency: str, as_of: str) -> FxSnapshot | None: ...


class JsonTransport(Protocol):
    def get_json(self, url: str, *, timeout: float) -> Any: ...


class TextTransport(Protocol):
    def get_text(self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None) -> str: ...


class SnapshotCache(Protocol):
    """Normalized snapshot cache port; implementations must be atomic."""

    def get(self, key: str) -> QuoteSnapshot | None: ...

    def set(self, key: str, snapshot: QuoteSnapshot) -> None: ...


class MemorySnapshotCache:
    """Thread-safe cache useful for desktop storage adapters and fixtures."""

    def __init__(self) -> None:
        self._values: dict[str, QuoteSnapshot] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> QuoteSnapshot | None:
        with self._lock:
            return self._values.get(key)

    def set(self, key: str, snapshot: QuoteSnapshot) -> None:
        with self._lock:
            self._values[key] = snapshot


class StorageSnapshotCache:
    """Adapter for ``Storage``'s transactional normalized-snapshot table."""

    def __init__(self, storage: Any):
        self.storage = storage

    def get(self, key: str) -> QuoteSnapshot | None:
        value = self.storage.get_market_snapshot(key)
        if not isinstance(value, dict):
            return None
        try:
            return QuoteSnapshot(
                provider=str(value["provider"]), symbol=str(value["symbol"]),
                exchange=str(value["exchange"]), price=value.get("price"),
                market_cap=value.get("market_cap"), currency=str(value["currency"]),
                as_of=str(value["as_of"]), retrieved_at=str(value["retrieved_at"]),
                market_cap_scope=str(value.get("market_cap_scope", "security")),
                source_id=str(value.get("source_id", "")),
                warnings=tuple(value.get("warnings", ())),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def set(self, key: str, snapshot: QuoteSnapshot) -> None:
        self.storage.save_market_snapshot(key, {
            "provider": snapshot.provider, "symbol": snapshot.symbol,
            "exchange": snapshot.exchange, "price": snapshot.price,
            "market_cap": snapshot.market_cap, "currency": snapshot.currency,
            "as_of": snapshot.as_of, "retrieved_at": snapshot.retrieved_at,
            "market_cap_scope": snapshot.market_cap_scope,
            "source_id": snapshot.source_id, "warnings": list(snapshot.warnings),
        }, snapshot.retrieved_at)


_VERIFIED_CONTEXT_LOCK = threading.Lock()
_VERIFIED_CONTEXT: ssl.SSLContext | None = None


def _verified_ssl_context() -> ssl.SSLContext:
    """Return one process-wide, hostname-checked HTTPS context."""
    global _VERIFIED_CONTEXT
    if _VERIFIED_CONTEXT is not None:
        return _VERIFIED_CONTEXT
    with _VERIFIED_CONTEXT_LOCK:
        if _VERIFIED_CONTEXT is None:
            if truststore is not None:
                context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            else:
                context = ssl.create_default_context()
                # Older environments without PyPA truststore do not always
                # expose the OS root store through OpenSSL. Importing trusted
                # roots is best effort; the context remains secure.
                try:
                    enum_certificates = getattr(ssl, "enum_certificates")
                    for store in ("ROOT", "CA"):
                        for cert, _encoding, trust in enum_certificates(store):
                            if trust is False:
                                continue
                            try:
                                context.load_verify_locations(cadata=cert)
                            except (TypeError, ValueError, ssl.SSLError):
                                continue
                except (AttributeError, OSError):
                    pass
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
            _VERIFIED_CONTEXT = context
    return _VERIFIED_CONTEXT


class StdlibJsonTransport:
    def get_json(self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "OpenThesis/2.5.0",
        }
        request_headers.update(dict(headers or {}))
        request = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(
            request, timeout=timeout, context=_verified_ssl_context()
        ) as response:
            return json.loads(response.read(2_000_000).decode("utf-8"))


class StdlibTextTransport:
    """Bounded HTTPS text transport for public, non-JSON quote endpoints."""

    _DEFAULT_HEADERS = {
        "Accept": "text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Referer": "https://gu.qq.com/",
        "User-Agent": "Mozilla/5.0 OpenThesis/2.5.0",
    }

    def get_text(self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None) -> str:
        if not str(url).lower().startswith("https://"):
            raise ValueError("QUOTE_INSECURE_TRANSPORT")
        request_headers = dict(self._DEFAULT_HEADERS)
        request_headers.update(dict(headers or {}))
        request = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(
            request, timeout=timeout, context=_verified_ssl_context()
        ) as response:
            return response.read(2_000_000).decode("gb18030")


def map_symbol(company: Company, provider: str = "") -> SymbolMapping:
    provider = str(provider or "").casefold()
    market = normalize_market(company.market)
    exchange = str(company.exchange or "").upper()
    exchange = {"SH": "SSE", "SHSE": "SSE", "SZ": "SZSE", "SZSE": "SZSE", "NASDAQGS": "NASDAQ", "NASDAQCM": "NASDAQ"}.get(exchange, exchange)
    ticker = str(company.ticker or "").strip().upper()
    if market == Market.CN_A:
        code = ticker.split(".", 1)[0].zfill(6)
        ticker_exchange = ticker.rsplit(".", 1)[1] if "." in ticker else ""
        ticker_exchange = {"SH": "SSE", "SHSE": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(ticker_exchange, ticker_exchange)
        exchange = exchange or ticker_exchange or ("SSE" if code.startswith(("5", "6", "9")) else "SZSE")
        if exchange == "BSE" and provider.startswith("yahoo"):
            raise ValueError("QUOTE_EXCHANGE_UNSUPPORTED_BSE_YAHOO")
        eastmoney_market = {"SSE": "1", "SZSE": "0", "BSE": "0"}.get(exchange, "0")
        if provider.startswith("eastmoney"):
            symbol = f"{eastmoney_market}.{code}"
        elif provider.startswith("tencent"):
            tencent_prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange)
            if tencent_prefix is None:
                raise ValueError("QUOTE_EXCHANGE_UNSUPPORTED")
            symbol = f"{tencent_prefix}{code}"
        elif provider.startswith("configured"):
            symbol = ticker
        else:
            symbol = f"{code}.{('SS' if exchange == 'SSE' else 'SZ')}"
        return SymbolMapping(symbol, exchange, market.value)
    if market == Market.HK:
        code = ticker.split(".", 1)[0].zfill(5)
        if provider.startswith("tencent"):
            symbol = f"hk{code}"
        else:
            symbol = f"116.{code}" if provider.startswith("eastmoney") else f"{code}.HK"
        return SymbolMapping(symbol, exchange or "HKEX", market.value)
    exchange = exchange or "NASDAQ"
    if provider.startswith("eastmoney"):
        # Eastmoney requires a market-qualified ``secid``.  A bare US ticker
        # is ambiguous (and is interpreted as a mainland security).
        eastmoney_market = {"NASDAQ": "105", "NYSE": "106", "AMEX": "107"}.get(exchange)
        if eastmoney_market is None:
            raise ValueError("QUOTE_EXCHANGE_UNSUPPORTED")
        return SymbolMapping(f"{eastmoney_market}.{ticker.split('.', 1)[0]}", exchange, market.value)
    return SymbolMapping(ticker.split(".", 1)[0], exchange, market.value)


class EastmoneyPublicQuoteAdapter:
    name = "eastmoney-public"
    _URL = "https://push2.eastmoney.com/api/qt/stock/get"

    def __init__(self, transport: JsonTransport | Callable[..., Any] | None = None):
        self.transport = transport or StdlibJsonTransport()

    def quote(self, company: Company, mapping: SymbolMapping, *, policy: ReverseDcfPolicy) -> QuoteSnapshot:
        params = urllib.parse.urlencode({"secid": mapping.provider_symbol, "fields": "f43,f57,f58,f59,f86,f116,f118"})
        payload = self._get(self._URL + "?" + params, policy)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("QUOTE_SCHEMA_INVALID")
        # f59 is the provider's decimal precision.  Older fixtures may omit
        # it only when f43 is already a decimal string; never assume /1000.
        raw_price = data.get("f43")
        precision = _integer(data.get("f59"))
        price = _eastmoney_price(raw_price, precision)
        market_cap = _number(data.get("f116")) or _number(data.get("f118"))
        as_of = _date_text(data.get("f86") or data.get("f124") or data.get("f85"))
        currency = _currency(data.get("currency"), company)
        if price is None or not as_of or not currency:
            raise ValueError("QUOTE_FIELDS_MISSING")
        return QuoteSnapshot(self.name, mapping.provider_symbol, mapping.exchange, price, market_cap, currency, as_of, _now(), source_id=self._URL)

    def _get(self, url: str, policy: ReverseDcfPolicy) -> Any:
        for attempt in range(policy.max_retries + 1):
            try:
                method = getattr(self.transport, "get_json", None)
                return method(url, timeout=policy.request_timeout_seconds) if method else self.transport(url, timeout=policy.request_timeout_seconds)
            except Exception:
                if attempt >= policy.max_retries:
                    raise
        raise ValueError("QUOTE_UNAVAILABLE")


class YahooChartQuoteAdapter:
    name = "yahoo-chart"
    _URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def __init__(self, transport: JsonTransport | Callable[..., Any] | None = None):
        self.transport = transport or StdlibJsonTransport()

    def quote(self, company: Company, mapping: SymbolMapping, *, policy: ReverseDcfPolicy) -> QuoteSnapshot:
        url = self._URL + urllib.parse.quote(mapping.provider_symbol, safe="") + "?range=5d&interval=1d"
        payload = self._get(url, policy)
        chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
        result = chart.get("result") if isinstance(chart, dict) else None
        result = result[0] if isinstance(result, list) and result else None
        meta = result.get("meta") if isinstance(result, dict) else None
        if not isinstance(meta, dict):
            raise ValueError("QUOTE_SCHEMA_INVALID")
        price = _number(meta.get("regularMarketPrice"))
        timestamp = meta.get("regularMarketTime")
        as_of = _date_text(timestamp)
        currency = _currency(meta.get("currency"), company)
        if price is None or not as_of or not currency:
            raise ValueError("QUOTE_FIELDS_MISSING")
        market_cap = _number(meta.get("marketCap"))
        market_cap_scope = str(meta.get("marketCapScope") or "security").casefold()
        # A price x shares fallback cannot safely cover ADRs, dual listings,
        # or multiple classes.  Provider supplied market cap is required.
        return QuoteSnapshot(self.name, mapping.provider_symbol, str(meta.get("exchangeName") or mapping.exchange), price, market_cap, currency, as_of, _now(), market_cap_scope=market_cap_scope, source_id=self._URL)

    def _get(self, url: str, policy: ReverseDcfPolicy) -> Any:
        for attempt in range(policy.max_retries + 1):
            try:
                method = getattr(self.transport, "get_json", None)
                return method(url, timeout=policy.request_timeout_seconds) if method else self.transport(url, timeout=policy.request_timeout_seconds)
            except Exception:
                if attempt >= policy.max_retries:
                    raise
        raise ValueError("QUOTE_UNAVAILABLE")


class NasdaqPublicQuoteAdapter:
    """Nasdaq public quote adapter with issuer-scoped market cap."""

    name = "nasdaq-public"
    _BASE = "https://api.nasdaq.com/api/quote/"
    _HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
        "User-Agent": "Mozilla/5.0 OpenThesis/2.5.0",
    }

    def __init__(self, transport: JsonTransport | Callable[..., Any] | None = None):
        self.transport = transport or StdlibJsonTransport()

    def quote(self, company: Company, mapping: SymbolMapping, *, policy: ReverseDcfPolicy) -> QuoteSnapshot:
        if normalize_market(company.market) is not Market.US:
            raise ValueError("QUOTE_MARKET_UNSUPPORTED")
        symbol = urllib.parse.quote(mapping.provider_symbol, safe="")
        info_url = f"{self._BASE}{symbol}/info?assetclass=stocks"
        summary_url = f"{self._BASE}{symbol}/summary?assetclass=stocks"
        info = self._get(info_url, policy)
        summary = self._get(summary_url, policy)
        primary = _nested_dict(info, "data", "primaryData")
        summary_data = _nested_dict(summary, "data", "summaryData")
        price = _nasdaq_number(primary.get("lastSalePrice"))
        market_cap_value = summary_data.get("MarketCap")
        market_cap = _nasdaq_number(
            market_cap_value.get("value") if isinstance(market_cap_value, dict) else market_cap_value
        )
        as_of = _nasdaq_date(primary.get("lastTradeTimestamp"))
        if price is None or market_cap is None or not as_of:
            raise ValueError("QUOTE_FIELDS_MISSING")
        return QuoteSnapshot(
            self.name,
            mapping.provider_symbol,
            str(primary.get("exchange") or mapping.exchange),
            price,
            market_cap,
            "USD",
            as_of,
            _now(),
            market_cap_scope="issuer",
            source_id=summary_url,
        )

    def _get(self, url: str, policy: ReverseDcfPolicy) -> Any:
        attempts = max(0, int(policy.max_retries))
        for attempt in range(attempts + 1):
            try:
                method = getattr(self.transport, "get_json", None)
                if method is None:
                    return self.transport(
                        url,
                        timeout=policy.request_timeout_seconds,
                        headers=self._HEADERS,
                    )
                try:
                    return method(
                        url,
                        timeout=policy.request_timeout_seconds,
                        headers=self._HEADERS,
                    )
                except TypeError as exc:
                    # Preserve compatibility with injected legacy transports
                    # while production transports receive browser headers.
                    if "headers" not in str(exc):
                        raise
                    return method(url, timeout=policy.request_timeout_seconds)
            except HTTPError as exc:
                if exc.code not in {408, 425, 429} and not 500 <= exc.code < 600:
                    raise
                if attempt >= attempts:
                    raise
            except (ConnectionError, TimeoutError, OSError):
                if attempt >= attempts:
                    raise
        raise RuntimeError("unreachable quote retry state")


class TencentPublicQuoteAdapter:
    """Tencent public quote adapter for mainland and Hong Kong securities.

    The endpoint returns a GBK-compatible tilde-delimited record rather than
    JSON.  Only the normalised quote crosses this module boundary; the raw
    response is never persisted or passed to research agents.
    """

    name = "tencent-public"
    _BASE = "https://qt.gtimg.cn/q="
    _HEADERS = {
        "Accept": "text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Referer": "https://gu.qq.com/",
        "User-Agent": "Mozilla/5.0 OpenThesis/2.5.0",
    }

    def __init__(self, transport: TextTransport | Callable[..., Any] | None = None):
        self.transport = transport or StdlibTextTransport()

    def quote(self, company: Company, mapping: SymbolMapping, *, policy: ReverseDcfPolicy) -> QuoteSnapshot:
        market = normalize_market(company.market)
        if market not in {Market.CN_A, Market.HK}:
            raise ValueError("QUOTE_MARKET_UNSUPPORTED")
        symbol = mapping.provider_symbol
        if market is Market.CN_A and not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
            raise ValueError("QUOTE_SCHEMA_INVALID")
        if market is Market.HK and not re.fullmatch(r"hk\d{5}", symbol):
            raise ValueError("QUOTE_SCHEMA_INVALID")
        text = self._get(self._BASE + urllib.parse.quote(symbol, safe=""), policy)
        fields = _tencent_fields(text, symbol)
        expected_code = symbol[2:]
        if fields[2].strip() != expected_code:
            raise ValueError("QUOTE_SCHEMA_INVALID")
        cap_index = 45 if market is Market.CN_A else 44
        currency_index = 82 if market is Market.CN_A else 75
        if len(fields) <= currency_index:
            raise ValueError("QUOTE_SCHEMA_INVALID")
        price = _positive_number(fields[3])
        raw_cap = _positive_number(fields[cap_index])
        currency = fields[currency_index].strip().upper()
        if price is None or raw_cap is None or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("QUOTE_FIELDS_MISSING")
        as_of = _tencent_date(fields[30], market is Market.HK)
        if not as_of:
            raise ValueError("QUOTE_FIELDS_MISSING")
        return QuoteSnapshot(
            self.name,
            symbol,
            mapping.exchange,
            price,
            raw_cap * 100_000_000,
            currency,
            as_of,
            _now(),
            market_cap_scope="security",
            source_id=self._BASE + symbol,
        )

    def _get(self, url: str, policy: ReverseDcfPolicy) -> str:
        attempts = max(0, int(policy.max_retries))
        for attempt in range(attempts + 1):
            try:
                method = getattr(self.transport, "get_text", None)
                if method is None:
                    return self.transport(url, timeout=policy.request_timeout_seconds, headers=self._HEADERS)
                try:
                    return method(url, timeout=policy.request_timeout_seconds, headers=self._HEADERS)
                except TypeError as exc:
                    if "headers" not in str(exc):
                        raise
                    return method(url, timeout=policy.request_timeout_seconds)
            except HTTPError as exc:
                if exc.code not in {408, 425, 429} and not 500 <= exc.code < 600:
                    raise
                if attempt >= attempts:
                    raise
            except (ConnectionError, TimeoutError, OSError):
                if attempt >= attempts:
                    raise
        raise RuntimeError("unreachable quote retry state")


class ConfiguredQuoteAdapter:
    """Optional user-supplied JSON quote source.

    The credential is used only to construct the request and is deliberately
    absent from ``QuoteSnapshot`` and all error text.  Deployments may wrap
    this adapter with their secure settings provider; no key is required by
    the default adapter chain.
    """

    name = "configured"

    def __init__(self, endpoint: str, *, api_key: str = "", transport: JsonTransport | Callable[..., Any] | None = None):
        self.endpoint = endpoint.strip()
        self._api_key = api_key
        self.transport = transport or StdlibJsonTransport()

    def quote(self, company: Company, mapping: SymbolMapping, *, policy: ReverseDcfPolicy) -> QuoteSnapshot:
        if not self.endpoint.startswith("https://"):
            raise ValueError("CONFIGURED_QUOTE_ENDPOINT_INVALID")
        query = {"symbol": mapping.provider_symbol}
        if self._api_key:
            query["apikey"] = self._api_key
        url = self.endpoint + ("&" if "?" in self.endpoint else "?") + urllib.parse.urlencode(query)
        method = getattr(self.transport, "get_json", None)
        payload = method(url, timeout=policy.request_timeout_seconds) if method else self.transport(url, timeout=policy.request_timeout_seconds)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            raise ValueError("QUOTE_SCHEMA_INVALID")
        price = _number(data.get("price") or data.get("regularMarketPrice"))
        cap = _number(data.get("market_cap") or data.get("marketCap"))
        as_of = _date_text(data.get("as_of") or data.get("asOf") or data.get("timestamp"))
        currency = _currency(data.get("currency"), company)
        if price is None or not as_of:
            raise ValueError("QUOTE_FIELDS_MISSING")
        return QuoteSnapshot(self.name, mapping.provider_symbol, mapping.exchange, price, cap, currency, as_of, _now(), source_id=self.endpoint)


class EcbFxAdapter:
    """ECB reference rates, expressed as target-currency units per EUR.

    The official SDMX key is ``D.<currency>.EUR.SP00.A``.  A cross-rate is
    therefore ``dst_per_eur / src_per_eur``; querying the two legs separately
    also makes EUR conversions and fixture validation explicit.
    """

    name = "ecb-reference"
    _URL = "https://data-api.ecb.europa.eu/service/data/EXR/D."

    def __init__(self, transport: JsonTransport | Callable[..., Any] | None = None, *, timeout_seconds: float = 4.0):
        self.transport = transport or StdlibJsonTransport()
        self.timeout_seconds = max(0.5, min(10.0, float(timeout_seconds)))

    def rate(self, from_currency: str, to_currency: str, as_of: str) -> FxSnapshot | None:
        src, dst = from_currency.upper(), to_currency.upper()
        if src == dst:
            return FxSnapshot(src, dst, 1.0, as_of, self.name)
        requested = date.fromisoformat(as_of)
        start = requested - timedelta(days=7)
        src_rate, src_day, src_direct = self._leg(src, start, requested)
        if src_direct is not None:
            return FxSnapshot(src, dst, src_direct, as_of, self.name)
        dst_rate, dst_day, dst_direct = self._leg(dst, start, requested)
        if src_rate is None or dst_rate is None:
            return None
        effective = min(day for day in (src_day, dst_day) if day is not None)
        return FxSnapshot(src, dst, dst_rate / src_rate, effective.isoformat(), self.name)

    def _leg(self, currency: str, start: date, end: date) -> tuple[float | None, date | None, float | None]:
        if currency == "EUR":
            return 1.0, end if end.weekday() < 5 else end - timedelta(days=end.weekday() - 4), None
        key = f"{currency}.EUR.SP00.A"
        query = urllib.parse.urlencode({"startPeriod": start.isoformat(), "endPeriod": end.isoformat(), "format": "jsondata"})
        url = self._URL + key + "?" + query
        try:
            payload = _transport_payload(self.transport, url, timeout=self.timeout_seconds)
        except Exception:
            return None, None, None
        direct = _ecb_observation(payload)
        if isinstance(payload, dict) and any(key in payload for key in ("rate", "value")):
            return None, None, direct
        fallback_day = end
        while fallback_day.weekday() >= 5:
            fallback_day -= timedelta(days=1)
        value, observed = _ecb_observation_with_date(payload, fallback_day)
        return value, observed, None


@dataclass
class _Flight:
    event: threading.Event
    result: MarketSnapshotOutcome | None = None


class MarketSnapshotModule:
    def __init__(self, *, adapters: tuple[QuoteAdapter, ...] | None = None, fx_adapter: FxAdapter | None = None,
                 clock: Callable[[], datetime] | None = None, cache: SnapshotCache | None = None):
        self.adapters = (
            EastmoneyPublicQuoteAdapter(),
            TencentPublicQuoteAdapter(),
            NasdaqPublicQuoteAdapter(),
            YahooChartQuoteAdapter(),
        ) if adapters is None else adapters
        self.fx_adapter = fx_adapter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache_port = cache or MemorySnapshotCache()
        self._inflight: dict[str, _Flight] = {}
        self._flight_lock = threading.RLock()
        self._adapter_failures: dict[str, int] = {}
        self._adapter_open_until: dict[str, float] = {}

    def capture(self, company: Company, policy: ReverseDcfPolicy | None = None, manual_override: dict[str, Any] | None = None) -> MarketSnapshotOutcome:
        policy = policy or ReverseDcfPolicy()
        key = self._cache_key(company, policy)
        if manual_override is not None:
            quote = _manual_quote(company, manual_override, self._clock())
            return self._outcome(company, quote, policy, status=SnapshotStatus.MANUAL)
        cached = self._cache_port.get(key)
        if cached is not None and _is_fresh(cached.as_of, self._clock().date(), policy.max_quote_age_days):
            try:
                _validate_quote(cached, company)
                if cached.market_cap is not None and _requires_issuer_scope(company) and cached.market_cap_scope not in {"issuer", "consolidated"}:
                    raise ValueError("MARKET_CAP_SCOPE_INCOMPLETE")
                if _quote_currency_conflicts(cached, company):
                    return self._outcome(company, cached, policy, status=SnapshotStatus.CONFLICT, warnings=("QUOTE_CURRENCY_MISMATCH",), cache="fresh")
            except ValueError:
                cached = None
            else:
                return self._outcome(company, cached, policy, cache="fresh")
        with self._flight_lock:
            flight = self._inflight.get(key)
            owner = flight is None
            if owner:
                flight = _Flight(threading.Event())
                self._inflight[key] = flight
        if not owner:
            # Waiters reuse the owner's normalized outcome.  A bounded wait
            # prevents an adapter hang from freezing a research request.
            if flight.event.wait(policy.request_timeout_seconds * (policy.max_retries + 2)) and flight.result is not None:
                return flight.result
            return self._unavailable(company, policy, "QUOTE_SINGLE_FLIGHT_TIMEOUT")
        try:
            try:
                result = self._capture_network(company, policy, cached)
            except Exception:
                result = self._unavailable(company, policy, "QUOTE_UNAVAILABLE")
            flight.result = result
            return result
        finally:
            with self._flight_lock:
                self._inflight.pop(key, None)
                flight.event.set()

    def _cache_key(self, company: Company, policy: ReverseDcfPolicy) -> str:
        identity = str(company.security_id or company.issuer_id or company.ticker).strip().upper()
        return f"market-snapshot-v2|{identity}|{policy.version}"

    def _capture_network(self, company: Company, policy: ReverseDcfPolicy, cached: QuoteSnapshot | None) -> MarketSnapshotOutcome:
        quotes: list[QuoteSnapshot] = []
        errors: list[str] = []
        currency_conflict = False
        executor = ThreadPoolExecutor(max_workers=max(1, len(self.adapters)), thread_name_prefix="market-quote")
        futures = [executor.submit(self._query_adapter, adapter, company, policy) for adapter in self.adapters]
        try:
            done, _ = wait(futures, timeout=max(0.1, policy.request_timeout_seconds * (policy.max_retries + 1)))
            for future in futures:
                if future not in done:
                    quote, error = None, "QUOTE_TIMEOUT"
                else:
                    quote, error = future.result()
                if quote is not None:
                    if _quote_currency_conflicts(quote, company):
                        currency_conflict = True
                    if quote.market_cap is not None and _requires_issuer_scope(company) and quote.market_cap_scope not in {"issuer", "consolidated"}:
                        quote = replace(quote, market_cap=None, warnings=(*quote.warnings, "MARKET_CAP_SCOPE_INCOMPLETE"))
                    quotes.append(quote)
                if error:
                    errors.append(error)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if not quotes:
            if cached is not None:
                return self._outcome(company, cached, policy, status=SnapshotStatus.STALE, cache="stale", warnings=tuple(errors))
            return self._unavailable(company, policy, "QUOTE_UNAVAILABLE", tuple(errors))
        if not any(q.market_cap is not None for q in quotes):
            if currency_conflict:
                return self._outcome(company, quotes[0], policy, status=SnapshotStatus.CONFLICT, warnings=tuple((*errors, "QUOTE_CURRENCY_MISMATCH")))
            if cached is not None:
                return self._outcome(company, cached, policy, status=SnapshotStatus.STALE, cache="stale", warnings=tuple((*errors, "MARKET_CAP_SCOPE_INCOMPLETE")))
            return self._unavailable(company, policy, "MARKET_CAP_SCOPE_INCOMPLETE", tuple((*errors, "MARKET_CAP_SCOPE_INCOMPLETE")))
        status = SnapshotStatus.VERIFIED
        warnings: list[str] = list(errors)
        if len(quotes) > 1 and _conflicts(quotes, policy.conflict_tolerance):
            status = SnapshotStatus.CONFLICT
            warnings.append("QUOTE_SOURCES_CONFLICT")
        if currency_conflict:
            status = SnapshotStatus.CONFLICT
            warnings.append("QUOTE_CURRENCY_MISMATCH")
        chosen = next((q for q in quotes if q.market_cap is not None), quotes[0])
        if not _is_fresh(chosen.as_of, self._clock().date(), policy.max_quote_age_days):
            status = SnapshotStatus.STALE
            warnings.append("QUOTE_DATE_OUTSIDE_POLICY")
        self._cache_port.set(self._cache_key(company, policy), chosen)
        return self._outcome(company, chosen, policy, status=status, warnings=tuple(warnings))

    def _query_adapter(self, adapter: QuoteAdapter, company: Company, policy: ReverseDcfPolicy) -> tuple[QuoteSnapshot | None, str]:
        name = str(getattr(adapter, "name", "unknown"))
        breaker_key = self._breaker_key(name, company)
        if self._breaker_open(breaker_key):
            return None, "QUOTE_PROVIDER_CIRCUIT_OPEN"
        try:
            provider_name = name.split("-")[0]
            quote = adapter.quote(company, map_symbol(company, provider_name), policy=policy)
            _validate_quote(quote, company)
            self._record_adapter_success(breaker_key)
            return quote, ""
        except Exception as exc:
            code = str(exc).strip()
            if code == "QUOTE_MARKET_UNSUPPORTED":
                return None, ""
            self._record_adapter_failure(breaker_key, policy)
            return None, code if code.startswith(("QUOTE_", "MARKET_CAP_", "CONFIGURED_")) else "QUOTE_UNAVAILABLE"

    @staticmethod
    def _breaker_key(name: str, company: Company) -> str:
        return f"{name}:{normalize_market(company.market).value}"

    def _breaker_open(self, name: str) -> bool:
        with self._flight_lock:
            return time.monotonic() < self._adapter_open_until.get(name, 0.0)

    def _record_adapter_success(self, name: str) -> None:
        with self._flight_lock:
            self._adapter_failures.pop(name, None)
            self._adapter_open_until.pop(name, None)

    def _record_adapter_failure(self, name: str, policy: ReverseDcfPolicy) -> None:
        with self._flight_lock:
            count = self._adapter_failures.get(name, 0) + 1
            self._adapter_failures[name] = count
            if count >= policy.circuit_breaker_failures:
                self._adapter_open_until[name] = time.monotonic() + policy.circuit_breaker_cooldown_seconds

    def _outcome(self, company: Company, quote: QuoteSnapshot, policy: ReverseDcfPolicy, *, status: SnapshotStatus = SnapshotStatus.VERIFIED, cache: str = "miss", warnings: tuple[str, ...] = ()) -> MarketSnapshotOutcome:
        report_currency = str(company.reporting_currency or quote.currency).upper()
        value = quote.market_cap
        fx_rate = None
        fx_as_of = ""
        fx_source = ""
        all_warnings = tuple(dict.fromkeys((*quote.warnings, *warnings)))
        if quote.currency != report_currency:
            if status is SnapshotStatus.CONFLICT:
                return MarketSnapshotOutcome(status=SnapshotStatus.CONFLICT, issuer_id=company.issuer_id, security_id=company.security_id,
                    symbol=quote.symbol, exchange=quote.exchange, price=quote.price, market_cap=value,
                    currency=report_currency, reporting_currency=report_currency, as_of=quote.as_of,
                    provider=quote.provider, source_id=quote.source_id, retrieved_at=quote.retrieved_at,
                    warnings=all_warnings, policy_version=policy.version,
                    quote_currency=quote.currency, valuation_currency=report_currency)
            fx = self.fx_adapter.rate(quote.currency, report_currency, quote.as_of) if self.fx_adapter else None
            if fx is None or value is None:
                return MarketSnapshotOutcome(status=SnapshotStatus.UNAVAILABLE, issuer_id=company.issuer_id, security_id=company.security_id,
                    symbol=quote.symbol, exchange=quote.exchange, price=quote.price, market_cap=value,
                    currency=quote.currency, reporting_currency=report_currency, as_of=quote.as_of,
                    provider=quote.provider, source_id=quote.source_id, retrieved_at=quote.retrieved_at,
                    error_code="FX_UNAVAILABLE" if fx is None else "MARKET_CAP_MISSING", warnings=all_warnings,
                    policy_version=policy.version, quote_currency=quote.currency, valuation_currency=report_currency)
            value *= fx.rate
            fx_rate, fx_as_of, fx_source = fx.rate, fx.as_of, fx.source
        return MarketSnapshotOutcome(status=status, issuer_id=company.issuer_id, security_id=company.security_id,
            symbol=quote.symbol, exchange=quote.exchange, price=quote.price, market_cap=quote.market_cap,
            equity_market_value=value, currency=report_currency, reporting_currency=report_currency,
            as_of=quote.as_of, provider=quote.provider, source_id=quote.source_id,
            retrieved_at=quote.retrieved_at, fx_rate=fx_rate, fx_as_of=fx_as_of, fx_source=fx_source,
            cache=cache, warnings=all_warnings, policy_version=policy.version,
            quote_currency=quote.currency, valuation_currency=report_currency)

    def _unavailable(self, company: Company, policy: ReverseDcfPolicy, code: str, warnings: tuple[str, ...] = ()) -> MarketSnapshotOutcome:
        return MarketSnapshotOutcome(status=SnapshotStatus.UNAVAILABLE, issuer_id=company.issuer_id, security_id=company.security_id,
            currency=company.listing_currency, reporting_currency=company.reporting_currency,
            quote_currency=company.listing_currency, valuation_currency=company.reporting_currency,
            error_code=code, warnings=warnings, policy_version=policy.version)


def _manual_quote(company: Company, value: dict[str, Any], now: datetime) -> QuoteSnapshot:
    price = _number(value.get("price"))
    cap = _number(value.get("market_cap"))
    if cap is None:
        cap = _number(value.get("market_cap_billions"))
        if cap is not None:
            cap *= 1_000_000_000
    currency = _currency(value.get("currency"), company)
    as_of = str(value.get("as_of") or "")
    date.fromisoformat(as_of)
    if price is None and cap is None:
        raise ValueError("manual market values require price or market cap")
    return QuoteSnapshot("manual", company.ticker, company.exchange, price, cap, currency, as_of, now.isoformat(), source_id="user-input", market_cap_scope="manual")


def _validate_quote(quote: QuoteSnapshot, company: Company) -> None:
    if quote.price is not None and quote.price < 0 or quote.market_cap is not None and quote.market_cap <= 0:
        raise ValueError("QUOTE_VALUE_INVALID")
    if len(quote.currency) != 3 or not quote.currency.isalpha():
        raise ValueError("QUOTE_CURRENCY_INVALID")
    date.fromisoformat(quote.as_of)


def _requires_issuer_scope(company: Company) -> bool:
    classes = tuple(str(item).upper() for item in (getattr(company, "share_classes", ()) or ()))
    if len(classes) > 1 or any(item in {"A", "H", "ADR", "ORDINARY", "PREFERRED"} for item in classes):
        return True
    identity = " ".join(str(getattr(company, key, "")) for key in ("issuer_id", "security_id", "ticker")).upper()
    if str(getattr(company, "issuer_id", "")).upper() in MULTI_LISTING_ISSUER_REGISTRY:
        return True
    return "A/H" in identity or "ADR" in identity or "DUAL" in identity


def _quote_currency_conflicts(quote: QuoteSnapshot, company: Company) -> bool:
    listing = str(getattr(company, "listing_currency", "") or "").strip().upper()
    return bool(listing and quote.currency.strip().upper() != listing)


# Metadata seam: providers may later populate this from a maintained issuer
# catalogue instead of changing the valuation gate.
MULTI_LISTING_ISSUER_REGISTRY = frozenset({"CN:CATL", "CN:CMB", "HK:ALIBABA", "CN:ALIBABA", "US:ALIBABA"})


def _conflicts(quotes: list[QuoteSnapshot], tolerance: float) -> bool:
    currencies = {q.currency for q in quotes}
    if len(currencies) > 1:
        return True
    prices = [q.price for q in quotes if q.price is not None]
    if len(prices) > 1 and min(prices) > 0 and (max(prices) - min(prices)) / min(prices) > tolerance:
        return True
    caps = [q.market_cap for q in quotes if q.market_cap is not None]
    return len(caps) > 1 and min(caps) > 0 and (max(caps) - min(caps)) / min(caps) > tolerance


def _is_fresh(as_of: str, today: date, max_age: int) -> bool:
    try:
        quote_date = date.fromisoformat(as_of)
        return quote_date <= today and today - quote_date <= timedelta(days=max_age)
    except ValueError:
        return False


def _number(value: Any, *, scale: float = 1.0) -> float | None:
    if value in (None, "", "-", "—"):
        return None
    try:
        number = float(value) / scale
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _nested_dict(value: Any, *keys: str) -> dict[str, Any]:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _nasdaq_number(value: Any) -> float | None:
    if value in (None, "", "-", "—", "N/A", "n/a"):
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)([KMBT])?", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(
        str(match.group(2) or "").upper(), 1.0
    )
    value = number * multiplier
    return value if value >= 0 else None


def _positive_number(value: Any) -> float | None:
    number = _number(str(value).strip().replace(",", ""))
    return number if number is not None and number > 0 else None


def _tencent_fields(payload: Any, symbol: str) -> list[str]:
    if isinstance(payload, bytes):
        payload = payload.decode("gb18030")
    text = str(payload or "")
    pattern = rf"\s*v_{re.escape(symbol)}=\"([^\"]*)\";?\s*"
    match = re.fullmatch(pattern, text)
    if match is None:
        raise ValueError("QUOTE_SCHEMA_INVALID")
    fields = match.group(1).split("~")
    required = 83 if symbol[:2] in {"sh", "sz", "bj"} else 76
    if len(fields) < required:
        raise ValueError("QUOTE_SCHEMA_INVALID")
    return fields


def _tencent_date(value: Any, hong_kong: bool) -> str:
    text = str(value or "").strip()
    pattern = "%Y/%m/%d %H:%M:%S" if hong_kong else "%Y%m%d%H%M%S"
    try:
        return datetime.strptime(text, pattern).date().isoformat()
    except ValueError:
        return ""


def _nasdaq_date(value: Any) -> str:
    text = str(value or "").strip()
    # Nasdaq uses a human-readable timestamp such as
    # ``Sep 8, 2026 12:27 PM ET``. The timezone suffix is display metadata;
    # only the calendar date is used by the freshness policy.
    text = re.sub(r"\s+(?:ET|EST|EDT)$", "", text, flags=re.IGNORECASE)
    for pattern in (
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y",
        "%b %d, %Y %I:%M %p",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return _date_text(value)


def _integer(value: Any) -> int | None:
    if value in (None, "", "-", "—"):
        return None
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _eastmoney_price(value: Any, precision: int | None) -> float | None:
    if value in (None, "", "-", "—"):
        return None
    text = str(value).strip()
    # Decimal strings are already normalized by the public API contract.
    if "." in text:
        return _number(text)
    if precision is None:
        return None
    return _number(value, scale=10 ** precision)


def _currency(value: Any, company: Company) -> str:
    text = str(value or "").strip().upper()
    return text if len(text) == 3 and text.isalpha() else str(company.listing_currency or "USD").upper()


def _date_text(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    text = str(value or "").strip()
    if text.isdigit() and len(text) == 8:
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    text = text[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _transport_payload(transport: Any, url: str, *, timeout: float) -> Any:
    method = getattr(transport, "get_json", None)
    if method:
        return method(url, timeout=timeout)
    text_method = getattr(transport, "get_text", None)
    if text_method:
        return text_method(url, timeout=timeout)
    return transport(url, timeout=timeout)


def _ecb_observation(payload: Any) -> float | None:
    """Extract the observation value from SDMX JSON or CSV only."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        lines = [line.strip() for line in payload.splitlines() if line.strip()]
        if not lines:
            return None
        headers = [part.strip().lower() for part in lines[0].split(",")]
        value_index = next((i for i, name in enumerate(headers) if name in {"obs_value", "value", "rate"}), None)
        if value_index is None:
            return None
        for line in reversed(lines[1:]):
            columns = [part.strip() for part in line.split(",")]
            if value_index < len(columns):
                value = _number(columns[value_index])
                if value is not None and value > 0:
                    return value
        return None
    if not isinstance(payload, dict):
        return None
    # Tiny fixture/normalised form.
    for key in ("rate", "value", "OBS_VALUE"):
        value = _number(payload.get(key))
        if value is not None and value > 0:
            return value
    data_sets = payload.get("dataSets")
    if isinstance(data_sets, list) and data_sets:
        dataset = data_sets[0]
        series = dataset.get("series", {}) if isinstance(dataset, dict) else {}
        if isinstance(series, dict):
            for item in series.values():
                observations = item.get("observations", {}) if isinstance(item, dict) else {}
                if isinstance(observations, dict):
                    for observation in observations.values():
                        candidate = observation[0] if isinstance(observation, list) and observation else observation
                        value = _number(candidate)
                        if value is not None and value > 0:
                            return value
    data = payload.get("data")
    if isinstance(data, list):
        for row in reversed(data):
            if isinstance(row, dict):
                value = _number(row.get("OBS_VALUE") or row.get("value"))
                if value is not None and value > 0:
                    return value
    return None


def _ecb_observation_with_date(payload: Any, fallback: date) -> tuple[float | None, date | None]:
    if isinstance(payload, str):
        lines = [line.strip() for line in payload.splitlines() if line.strip()]
        if len(lines) < 2:
            return None, None
        headers = [part.strip().lower() for part in lines[0].split(",")]
        value_index = next((i for i, name in enumerate(headers) if name in {"obs_value", "value", "rate"}), None)
        date_index = next((i for i, name in enumerate(headers) if name in {"time_period", "date", "observation_date"}), None)
        if value_index is None:
            return None, None
        for line in reversed(lines[1:]):
            columns = [part.strip() for part in line.split(",")]
            value = _number(columns[value_index]) if value_index < len(columns) else None
            if value is not None and value > 0:
                observed = _date_text(columns[date_index]) if date_index is not None and date_index < len(columns) else fallback.isoformat()
                return value, date.fromisoformat(observed) if observed else fallback
        return None, None
    if isinstance(payload, dict):
        data_sets = payload.get("dataSets")
        structure = payload.get("structure", {})
        dimensions = structure.get("dimensions", {}).get("observation", []) if isinstance(structure, dict) else []
        time_values = dimensions[0].get("values", []) if dimensions and isinstance(dimensions[0], dict) else []
        if isinstance(data_sets, list) and data_sets:
            series = data_sets[0].get("series", {}) if isinstance(data_sets[0], dict) else {}
            if isinstance(series, dict):
                for item in series.values():
                    observations = item.get("observations", {}) if isinstance(item, dict) else {}
                    if isinstance(observations, dict):
                        candidates = []
                        for index, observation in observations.items():
                            candidate = observation[0] if isinstance(observation, list) and observation else observation
                            value = _number(candidate)
                            if value is not None and value > 0:
                                observed = fallback
                                try:
                                    observed = date.fromisoformat(str(time_values[int(index)]["id"] if isinstance(time_values[int(index)], dict) else time_values[int(index)]))
                                except (IndexError, KeyError, TypeError, ValueError):
                                    pass
                                candidates.append((observed, value))
                        if candidates:
                            observed, value = max(candidates, key=lambda item: item[0])
                            return value, observed
    return _ecb_observation(payload), fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
