import threading
import time
import ssl
from unittest.mock import patch
from urllib.error import HTTPError
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from openthesis.domain import Company
from openthesis.market_snapshot import (
    EcbFxAdapter,
    ConfiguredQuoteAdapter,
    EastmoneyPublicQuoteAdapter,
    MemorySnapshotCache,
    MarketSnapshotModule,
    NasdaqPublicQuoteAdapter,
    TencentPublicQuoteAdapter,
    QuoteSnapshot,
    ReverseDcfPolicy,
    SnapshotStatus,
    SymbolMapping,
    StorageSnapshotCache,
    map_symbol,
)
import openthesis.market_snapshot as market_snapshot
from openthesis.financials import reverse_dcf_analysis
from openthesis.report_projection import normalize_report_sections, project_report_value


def company(ticker: str, market: str, listing: str, reporting: str = "") -> Company:
    return Company(cik=ticker, ticker=ticker, name="Fixture", market=market, exchange="", listing_currency=listing, reporting_currency=reporting or listing)


def test_symbol_mapping_covers_a_h_and_us() -> None:
    assert map_symbol(company("600519.SH", "CN_A", "CNY"), "eastmoney").provider_symbol == "1.600519"
    assert map_symbol(company("00700.HK", "HK", "HKD"), "eastmoney").provider_symbol == "116.00700"
    assert map_symbol(company("AAPL", "US", "USD"), "yahoo").provider_symbol == "AAPL"
    assert map_symbol(company("AAPL", "US", "USD"), "eastmoney").provider_symbol == "105.AAPL"
    assert map_symbol(Company(cik="IBM", ticker="IBM", name="Fixture", market="US", exchange="NYSE", listing_currency="USD"), "eastmoney").provider_symbol == "106.IBM"
    assert map_symbol(Company(cik="AMC", ticker="AMC", name="Fixture", market="US", exchange="AMEX", listing_currency="USD"), "eastmoney").provider_symbol == "107.AMC"
    bse = Company(cik="BJ", ticker="832982.BJ", name="Fixture", market="CN_A", exchange="BSE", listing_currency="CNY")
    try:
        map_symbol(bse, "yahoo")
    except ValueError as exc:
        assert str(exc) == "QUOTE_EXCHANGE_UNSUPPORTED_BSE_YAHOO"
    else:
        raise AssertionError("Yahoo must not map BSE to .SZ")
    assert map_symbol(bse, "configured").provider_symbol == "832982.BJ"


def test_eastmoney_precision_is_contract_driven_for_a_h_us() -> None:
    class Transport:
        def get_json(self, url, *, timeout):
            return {"data": {"f43": 12345, "f59": 2, "f116": 1000000, "f86": "20260907", "currency": "USD"}}
    adapter = EastmoneyPublicQuoteAdapter(Transport())
    quote = adapter.quote(company("AAPL", "US", "USD"), map_symbol(company("AAPL", "US", "USD"), "eastmoney"), policy=ReverseDcfPolicy())
    assert quote.price == 123.45


def test_future_and_old_fetched_quotes_are_stale_and_not_valued() -> None:
    for as_of in ("2026-09-09", "2026-08-01"):
        module = MarketSnapshotModule(
            adapters=(FixtureAdapter("eastmoney-public", price=10, cap=1_000_000, as_of=as_of),),
            clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc),
        )
        result = module.capture(company("AAPL", "US", "USD"))
        assert result.status is SnapshotStatus.STALE
        assert not result.allows_valuation


def test_snapshot_cache_is_shared_between_module_instances() -> None:
    cache = MemorySnapshotCache()
    first = FixtureAdapter("eastmoney-public", price=10, cap=1_000_000)
    second = FixtureAdapter("eastmoney-public", price=99, cap=9_000_000)
    one = MarketSnapshotModule(adapters=(first,), cache=cache, clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
    two = MarketSnapshotModule(adapters=(second,), cache=cache, clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert one.capture(company("AAPL", "US", "USD")).price == 10
    assert two.capture(company("AAPL", "US", "USD")).price == 10


def test_cached_security_scope_for_multi_listing_is_not_valuation_safe() -> None:
    cache = MemorySnapshotCache()
    dual = Company(cik="CATL", ticker="300750.SZ", name="CATL", market="CN_A", exchange="SZSE", listing_currency="CNY", reporting_currency="CNY", issuer_id="CN:CATL")
    cache.set("market-snapshot-v2|CATL|reverse-dcf-policy-v1", QuoteSnapshot("cached", "300750.SZ", "SZSE", 200, 1_000_000_000, "CNY", "2026-09-07", "2026-09-08T00:00:00+00:00"))
    result = MarketSnapshotModule(adapters=(), cache=cache, clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc)).capture(dual)
    assert result.status is SnapshotStatus.UNAVAILABLE
    assert result.error_code == "QUOTE_UNAVAILABLE"


def test_quote_currency_mismatch_is_conflict_even_with_market_cap() -> None:
    result = MarketSnapshotModule(adapters=(FixtureAdapter("eastmoney-public", price=10, cap=1_000_000, currency="CNY"),), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc)).capture(company("AAPL", "US", "USD"))
    assert result.status is SnapshotStatus.CONFLICT
    assert result.error_code == ""
    assert not result.allows_valuation


def test_storage_snapshot_cache_survives_new_module_boundary() -> None:
    from openthesis.storage import Storage
    with TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
        storage = Storage(Path(directory))
        cache = StorageSnapshotCache(storage)
        first = FixtureAdapter("eastmoney-public", price=10, cap=1_000_000)
        module_one = MarketSnapshotModule(adapters=(first,), cache=cache, clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
        assert module_one.capture(company("AAPL", "US", "USD")).price == 10
        second = FixtureAdapter("eastmoney-public", price=99, cap=9_000_000)
        module_two = MarketSnapshotModule(adapters=(second,), cache=StorageSnapshotCache(storage), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
        assert module_two.capture(company("AAPL", "US", "USD")).price == 10


def test_single_flight_waiters_reuse_owner_result() -> None:
    class Slow(FixtureAdapter):
        def __init__(self):
            super().__init__("eastmoney-public", price=10, cap=1_000_000)
            self.calls = 0
        def quote(self, company, mapping, *, policy):
            self.calls += 1
            time.sleep(0.08)
            return self.snapshot
    adapter = Slow()
    module = MarketSnapshotModule(adapters=(adapter,), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
    results = []
    threads = [threading.Thread(target=lambda: results.append(module.capture(company("AAPL", "US", "USD")))) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert adapter.calls == 1
    assert len(results) == 4 and all(item.allows_valuation for item in results)


def test_quote_adapters_are_bounded_parallel_not_serial() -> None:
    class Slow(FixtureAdapter):
        def quote(self, company, mapping, *, policy):
            time.sleep(0.35)
            return self.snapshot
    started = time.monotonic()
    module = MarketSnapshotModule(adapters=(Slow("slow", price=10, cap=1_000_000), FixtureAdapter("fast", price=10, cap=1_000_000)), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
    result = module.capture(company("AAPL", "US", "USD"), ReverseDcfPolicy(request_timeout_seconds=0.1, max_retries=0))
    elapsed = time.monotonic() - started
    assert result.status is SnapshotStatus.VERIFIED
    assert elapsed < 0.28


def test_provider_failures_open_circuit_after_bounded_attempts() -> None:
    class Failing:
        name = "failing"
        def __init__(self): self.calls = 0
        def quote(self, company, mapping, *, policy):
            self.calls += 1
            raise TimeoutError("network")
    adapter = Failing()
    policy = ReverseDcfPolicy(circuit_breaker_failures=2, circuit_breaker_cooldown_seconds=120)
    module = MarketSnapshotModule(adapters=(adapter,), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert module.capture(company("AAPL", "US", "USD"), policy).status is SnapshotStatus.UNAVAILABLE
    assert module.capture(company("AAPL", "US", "USD"), policy).status is SnapshotStatus.UNAVAILABLE
    assert module.capture(company("AAPL", "US", "USD"), policy).error_code == "QUOTE_UNAVAILABLE"
    assert adapter.calls == 2


def test_market_cap_scope_does_not_derive_from_shares() -> None:
    class Transport:
        def get_json(self, url, *, timeout):
            return {"chart": {"result": [{"meta": {"regularMarketPrice": 10, "regularMarketTime": 1788739200, "currency": "USD", "sharesOutstanding": 1000}}]}}
    from openthesis.market_snapshot import YahooChartQuoteAdapter
    result = MarketSnapshotModule(adapters=(YahooChartQuoteAdapter(Transport()),), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc)).capture(company("AAPL", "US", "USD"))
    # The module returns a stable unavailable outcome rather than a price x
    # shares estimate that could span multiple security classes.
    assert result.status is SnapshotStatus.UNAVAILABLE


def test_us_secondary_source_supplies_price_and_issuer_cap_when_eastmoney_fails() -> None:
    class EastmoneyFails:
        name = "eastmoney-public"

        def quote(self, company, mapping, *, policy):
            raise ConnectionError("eastmoney unavailable")

    class SecondaryTransport:
        def get_json(self, url, *, timeout, headers):
            assert headers["Accept"] == "application/json, text/plain, */*"
            assert headers["Origin"] == "https://www.nasdaq.com"
            if "/info?" in url:
                return {"data": {"primaryData": {
                    "lastSalePrice": "$315.93",
                    "lastTradeTimestamp": "Sep 8, 2026 12:27 PM ET",
                    "exchange": "NASDAQ",
                }}}
            return {"data": {"summaryData": {
                "MarketCap": {"value": "4,610,447,403,800"},
            }}}

    secondary = NasdaqPublicQuoteAdapter(SecondaryTransport())
    result = MarketSnapshotModule(
        adapters=(EastmoneyFails(), secondary),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
    ).capture(company("AAPL", "US", "USD"))
    assert result.status is SnapshotStatus.VERIFIED
    assert result.allows_valuation
    assert result.price == 315.93
    assert result.market_cap == 4_610_447_403_800
    assert result.provider == "nasdaq-public"
    assert result.source_id.endswith("/summary?assetclass=stocks")


def test_nasdaq_public_retries_only_transient_transport_failure() -> None:
    class Transport:
        def __init__(self):
            self.calls = []
            self.failed_once = False

        def get_json(self, url, *, timeout, headers):
            self.calls.append(url)
            if not self.failed_once:
                self.failed_once = True
                raise ConnectionError("temporary")
            if "/info?" in url:
                return {"data": {"primaryData": {
                    "lastSalePrice": "$315.93",
                    "lastTradeTimestamp": "Sep 8, 2026 12:27 PM ET",
                    "exchange": "NASDAQ",
                }}}
            return {"data": {"summaryData": {"MarketCap": {"value": "4,610,447,403,800"}}}}

    transport = Transport()
    quote = NasdaqPublicQuoteAdapter(transport).quote(
        company("AAPL", "US", "USD"),
        map_symbol(company("AAPL", "US", "USD"), "nasdaq"),
        policy=ReverseDcfPolicy(max_retries=1),
    )
    assert quote.market_cap == 4_610_447_403_800
    assert len(transport.calls) == 3


def test_nasdaq_schema_error_is_not_retried() -> None:
    class Transport:
        def __init__(self):
            self.calls = 0

        def get_json(self, url, *, timeout, headers):
            self.calls += 1
            return {"data": {}}

    transport = Transport()
    try:
        NasdaqPublicQuoteAdapter(transport).quote(
            company("AAPL", "US", "USD"),
            map_symbol(company("AAPL", "US", "USD"), "nasdaq"),
            policy=ReverseDcfPolicy(max_retries=3),
        )
    except ValueError as exc:
        assert str(exc) == "QUOTE_FIELDS_MISSING"
    else:
        raise AssertionError("schema errors must fail without retry")
    assert transport.calls == 2


def test_windows_transport_uses_verified_ca_context() -> None:
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b"{}"

    def secure_urlopen(request, *, timeout, context):
        seen["context"] = context
        return Response()

    with patch.object(market_snapshot.urllib.request, "urlopen", secure_urlopen):
        payload = market_snapshot.StdlibJsonTransport().get_json(
            "https://data.example.test/quotes", timeout=1.0
        )
    assert payload == {}
    assert isinstance(seen["context"], ssl.SSLContext)
    assert seen["context"].verify_mode is ssl.CERT_REQUIRED
    assert seen["context"].check_hostname


def test_verified_ssl_context_is_reused_between_transport_requests() -> None:
    contexts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b"{}"

    def secure_urlopen(request, *, timeout, context):
        contexts.append(context)
        return Response()

    with patch.object(market_snapshot.urllib.request, "urlopen", secure_urlopen):
        transport = market_snapshot.StdlibJsonTransport()
        transport.get_json("https://data.example.test/one", timeout=1.0)
        transport.get_json("https://data.example.test/two", timeout=1.0)
    assert len(contexts) == 2
    assert contexts[0] is contexts[1]


def test_transport_prefers_truststore_context() -> None:
    contexts = []

    class Context:
        def __init__(self, protocol):
            self.protocol = protocol
            self.verify_mode = ssl.CERT_REQUIRED
            self.check_hostname = True

    class Truststore:
        SSLContext = Context

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b"{}"

    def secure_urlopen(request, *, timeout, context):
        contexts.append(context)
        return Response()

    with (
        patch.object(market_snapshot, "truststore", Truststore()),
        patch.object(market_snapshot, "_VERIFIED_CONTEXT", None),
        patch.object(market_snapshot.urllib.request, "urlopen", secure_urlopen),
    ):
        market_snapshot.StdlibJsonTransport().get_json(
            "https://data.example.test/quotes", timeout=1.0
        )
    assert isinstance(contexts[0], Context)
    assert contexts[0].protocol is ssl.PROTOCOL_TLS_CLIENT
    assert contexts[0].verify_mode is ssl.CERT_REQUIRED
    assert contexts[0].check_hostname


def test_nasdaq_does_not_retry_non_transient_http_error() -> None:
    class Transport:
        def __init__(self):
            self.calls = 0

        def get_json(self, url, *, timeout, headers):
            self.calls += 1
            raise HTTPError(url, 400, "bad request", {}, None)

    transport = Transport()
    try:
        NasdaqPublicQuoteAdapter(transport).quote(
            company("AAPL", "US", "USD"),
            map_symbol(company("AAPL", "US", "USD"), "nasdaq"),
            policy=ReverseDcfPolicy(max_retries=3),
        )
    except HTTPError:
        pass
    else:
        raise AssertionError("HTTP 400 must fail")
    assert transport.calls == 1


def _tencent_payload(symbol: str, *, hong_kong: bool = False, code: str | None = None) -> str:
    fields = [""] * (76 if hong_kong else 83)
    fields[0] = "51"
    fields[1] = "Tencent" if hong_kong else "BYD"
    fields[2] = code or symbol.removeprefix("hk").removeprefix("sh").removeprefix("sz").removeprefix("bj")
    fields[3] = "315.93" if hong_kong else "123.45"
    fields[30] = "2026/09/08 15:30:00" if hong_kong else "20260908153000"
    fields[44 if hong_kong else 45] = "9876.54" if hong_kong else "1234.56"
    fields[75 if hong_kong else 82] = "HKD" if hong_kong else "CNY"
    return f'v_{symbol}="' + "~".join(fields) + '";'


def test_tencent_public_quote_parses_cn_and_hk_protocol() -> None:
    class Transport:
        def get_text(self, url, *, timeout, headers):
            assert url.startswith("https://qt.gtimg.cn/q=")
            assert headers["Accept"]
            symbol = url.rsplit("=", 1)[1]
            return _tencent_payload(symbol, hong_kong=symbol.startswith("hk"))

    adapter = TencentPublicQuoteAdapter(Transport())
    cn = company("002594.SZ", "CN_A", "CNY")
    hk = company("00700.HK", "HK", "HKD")
    cn_quote = adapter.quote(cn, map_symbol(cn, "tencent"), policy=ReverseDcfPolicy(max_retries=0))
    hk_quote = adapter.quote(hk, map_symbol(hk, "tencent"), policy=ReverseDcfPolicy(max_retries=0))
    assert cn_quote.price == 123.45
    assert cn_quote.market_cap == 1234.56 * 1e8
    assert cn_quote.currency == "CNY"
    assert cn_quote.market_cap_scope == "security"
    assert hk_quote.price == 315.93
    assert hk_quote.market_cap == 9876.54 * 1e8
    assert hk_quote.currency == "HKD"


def test_tencent_mapping_and_unsupported_us_are_stable() -> None:
    assert map_symbol(company("002594.SZ", "CN_A", "CNY"), "tencent").provider_symbol == "sz002594"
    assert map_symbol(company("600519.SH", "CN_A", "CNY"), "tencent").provider_symbol == "sh600519"
    assert map_symbol(company("830799.BJ", "CN_A", "CNY"), "tencent").provider_symbol == "bj830799"
    assert map_symbol(company("00700.HK", "HK", "HKD"), "tencent").provider_symbol == "hk00700"

    class Transport:
        def get_text(self, url, *, timeout, headers):
            raise AssertionError("US must be rejected before transport")

    us = company("AAPL", "US", "USD")
    try:
        TencentPublicQuoteAdapter(Transport()).quote(
            us, map_symbol(us, "tencent"), policy=ReverseDcfPolicy(max_retries=0)
        )
    except ValueError as exc:
        assert str(exc) == "QUOTE_MARKET_UNSUPPORTED"
    else:
        raise AssertionError("Tencent must not serve US")


def test_tencent_malformed_code_is_schema_invalid() -> None:
    class Transport:
        def get_text(self, url, *, timeout, headers):
            return _tencent_payload("sz002594", code="002595")

    target = company("002594.SZ", "CN_A", "CNY")
    try:
        TencentPublicQuoteAdapter(Transport()).quote(
            target, map_symbol(target, "tencent"), policy=ReverseDcfPolicy(max_retries=0)
        )
    except ValueError as exc:
        assert str(exc) == "QUOTE_SCHEMA_INVALID"
    else:
        raise AssertionError("mismatched Tencent code must be rejected")


def test_cn_and_hk_use_tencent_when_eastmoney_fails() -> None:
    class EastmoneyFails:
        name = "eastmoney-public"

        def quote(self, company, mapping, *, policy):
            raise ConnectionError("eastmoney unavailable")

    class Transport:
        def get_text(self, url, *, timeout, headers):
            symbol = url.rsplit("=", 1)[1]
            return _tencent_payload(symbol, hong_kong=symbol.startswith("hk"))

    adapter = TencentPublicQuoteAdapter(Transport())
    for target in (company("002594.SZ", "CN_A", "CNY"), company("00700.HK", "HK", "HKD")):
        result = MarketSnapshotModule(
            adapters=(EastmoneyFails(), adapter),
            clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        ).capture(target)
        assert result.status is SnapshotStatus.VERIFIED
        assert result.provider == "tencent-public"
        assert result.market_cap is not None


def test_us_circuit_breaker_does_not_block_cn_a_provider() -> None:
    class MarketScoped:
        name = "eastmoney-public"

        def __init__(self):
            self.calls = []

        def quote(self, company, mapping, *, policy):
            self.calls.append(company.market)
            if company.market == "US":
                raise TimeoutError("US endpoint unavailable")
            return QuoteSnapshot(
                self.name,
                mapping.provider_symbol,
                mapping.exchange,
                100.0,
                1_000_000_000,
                "CNY",
                "2026-09-09",
                "2026-09-09T00:00:00+00:00",
            )

    adapter = MarketScoped()
    policy = ReverseDcfPolicy(circuit_breaker_failures=2, circuit_breaker_cooldown_seconds=120)
    module = MarketSnapshotModule(
        adapters=(adapter,),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert module.capture(company("AAPL", "US", "USD"), policy).status is SnapshotStatus.UNAVAILABLE
    assert module.capture(company("MSFT", "US", "USD"), policy).status is SnapshotStatus.UNAVAILABLE
    cn_result = module.capture(company("002594.SZ", "CN_A", "CNY"), policy)
    assert cn_result.status is SnapshotStatus.VERIFIED
    assert cn_result.allows_valuation
    assert adapter.calls[-1] == "CN_A"


def test_dual_class_listing_requires_issuer_scoped_market_cap() -> None:
    dual = Company(cik="CATL", ticker="300750.SZ", name="CATL", market="CN_A", exchange="SZSE", listing_currency="CNY", reporting_currency="CNY", issuer_id="CN:CATL")
    scoped = FixtureAdapter("eastmoney-public", price=200, cap=1_000_000_000, currency="CNY")
    result = MarketSnapshotModule(adapters=(scoped,), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc)).capture(dual)
    assert result.status is SnapshotStatus.UNAVAILABLE
    assert result.error_code == "MARKET_CAP_SCOPE_INCOMPLETE"
    confirmed = FixtureAdapter("eastmoney-public", price=200, cap=1_000_000_000, currency="CNY")
    confirmed.snapshot = QuoteSnapshot("eastmoney-public", "fixture", "SZSE", 200, 1_000_000_000, "CNY", "2026-09-07", "2026-09-08T00:00:00+00:00", market_cap_scope="issuer")
    assert MarketSnapshotModule(adapters=(confirmed,), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc)).capture(dual).allows_valuation


def test_alibaba_multi_listing_registry_blocks_unscoped_adr_market_cap() -> None:
    company = Company(cik="BABA", ticker="BABA", name="Alibaba", market="US", exchange="NYSE", listing_currency="USD", issuer_id="US:ALIBABA")
    result = MarketSnapshotModule(adapters=(FixtureAdapter("yahoo-chart", price=80, cap=100_000_000, currency="USD"),), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc)).capture(company)
    assert result.status is SnapshotStatus.UNAVAILABLE
    assert result.error_code == "MARKET_CAP_SCOPE_INCOMPLETE"


class FixtureAdapter:
    def __init__(self, name: str, *, price: float, cap: float | None, currency: str = "USD", as_of: str = "2026-09-07"):
        self.name = name
        self.snapshot = QuoteSnapshot(name, "fixture", "NASDAQ", price, cap, currency, as_of, "2026-09-08T00:00:00+00:00")

    def quote(self, company, mapping, *, policy):
        return self.snapshot


def test_capture_uses_verified_quote_and_fresh_cache_without_second_call() -> None:
    first = FixtureAdapter("eastmoney-public", price=100, cap=1_000_000_000)
    second = FixtureAdapter("yahoo-chart", price=100.2, cap=None)
    module = MarketSnapshotModule(adapters=(first, second), clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
    result = module.capture(company("AAPL", "US", "USD"))
    assert result.status is SnapshotStatus.VERIFIED
    assert result.equity_market_value == 1_000_000_000
    assert module.capture(company("AAPL", "US", "USD")).cache == "fresh"


def test_conflicting_sources_are_not_silently_selected() -> None:
    module = MarketSnapshotModule(adapters=(FixtureAdapter("a", price=100, cap=1e9), FixtureAdapter("b", price=120, cap=1e9)))
    result = module.capture(company("AAPL", "US", "USD"))
    assert result.status is SnapshotStatus.CONFLICT
    assert not result.allows_valuation


def test_price_only_secondary_source_participates_in_conflict_check() -> None:
    module = MarketSnapshotModule(adapters=(
        FixtureAdapter("eastmoney-public", price=100, cap=1e9),
        FixtureAdapter("yahoo-chart", price=130, cap=None),
    ))
    result = module.capture(company("AAPL", "US", "USD"))
    assert result.status is SnapshotStatus.CONFLICT
    assert not result.allows_valuation


def test_manual_override_and_ecb_fixture_are_auditable() -> None:
    module = MarketSnapshotModule(adapters=(), fx_adapter=type("Fx", (), {"rate": lambda self, a, b, d: type("R", (), {"rate": 0.13, "as_of": d, "source": "fixture-ecb"})()})())
    result = module.capture(company("00700.HK", "HK", "HKD", "CNY"), manual_override={"price": 300, "market_cap_billions": 100, "currency": "HKD", "as_of": "2026-09-07"})
    assert result.status is SnapshotStatus.MANUAL
    assert result.fx_rate == 0.13
    assert result.equity_market_value == 13_000_000_000
    assert result.fx_source == "fixture-ecb"


def test_automatic_cross_currency_quote_is_normalized_to_reporting_currency() -> None:
    class Fx:
        def rate(self, from_currency, to_currency, as_of):
            return type("R", (), {"rate": 0.13, "as_of": as_of, "source": "fixture-ecb"})()
    module = MarketSnapshotModule(
        adapters=(FixtureAdapter("eastmoney-public", price=300, cap=100_000_000_000, currency="HKD"),),
        fx_adapter=Fx(),
    )
    result = module.capture(company("00700.HK", "HK", "HKD", "CNY"))
    assert result.status is SnapshotStatus.VERIFIED
    assert result.currency == "CNY"
    assert result.quote_currency == "HKD"
    assert result.valuation_currency == "CNY"
    assert result.market_cap == 100_000_000_000
    assert result.equity_market_value == 13_000_000_000


def test_ecb_fixture_adapter_accepts_normalized_payload() -> None:
    class Transport:
        def get_json(self, url, *, timeout):
            return {"rate": 0.13}
    fx = EcbFxAdapter(Transport()).rate("HKD", "CNY", "2026-09-07")
    assert fx is not None and fx.rate == 0.13


def test_ecb_cross_rate_uses_per_eur_legs_and_previous_workday() -> None:
    class Transport:
        def get_json(self, url, *, timeout):
            assert ".EUR.SP00.A" in url
            if "USD.EUR.SP00.A" in url:
                return {"dataSets": [{"series": {"0": {"observations": {"0": [1.1]}}}}]}
            if "HKD.EUR.SP00.A" in url:
                return "CURRENCY,TIME_PERIOD,OBS_VALUE\nHKD,2026-09-04,8.8\n"
            return {}
    fx = EcbFxAdapter(Transport()).rate("USD", "HKD", "2026-09-06")
    assert fx is not None
    assert fx.rate == 8.0
    assert fx.as_of == "2026-09-04"


def test_ecb_fx_failure_is_bounded_to_two_leg_requests() -> None:
    class Transport:
        def __init__(self): self.calls = 0
        def get_json(self, url, *, timeout):
            self.calls += 1
            raise TimeoutError("offline")
    transport = Transport()
    assert EcbFxAdapter(transport, timeout_seconds=0.5).rate("USD", "CNY", "2026-09-08") is None
    assert transport.calls == 2


def test_configured_adapter_keeps_key_out_of_normalized_audit() -> None:
    class Transport:
        def get_json(self, url, *, timeout):
            assert "secret-key" in url
            return {"price": 10, "market_cap": 1000, "currency": "USD", "as_of": "2026-09-07"}
    adapter = ConfiguredQuoteAdapter("https://quotes.example.test/v1", api_key="secret-key", transport=Transport())
    quote = adapter.quote(company("AAPL", "US", "USD"), SymbolMapping("AAPL", "NASDAQ", "US"), policy=ReverseDcfPolicy())
    assert quote.source_id == "https://quotes.example.test/v1"
    assert "secret-key" not in repr(quote)


def test_reverse_dcf_is_equity_fcfe_and_rejects_lookahead_metric() -> None:
    metrics = [
        {"year": 2026, "filed_at": "2026-10-01", "free_cash_flow": 999.0},
        {"year": 2025, "filed_at": "2026-03-01", "free_cash_flow": 100.0},
    ]
    result = reverse_dcf_analysis(metrics, 1_000.0, market_as_of="2026-09-07", currency="USD")
    assert result["cash_flow_basis"].startswith("FCFE proxy")
    assert result["base_fcf_period"] == "2025"
    assert result["equity_market_value"] == 1_000.0
    assert all("enterprise_value" not in row for row in result["sensitivity"])


def test_reverse_dcf_chooses_latest_disclosed_full_fy_not_interim() -> None:
    result = reverse_dcf_analysis([
        {"year": 2026, "period": "H1", "filed_at": "2026-08-01", "free_cash_flow": 900.0},
        {"year": 2025, "period": "FY", "filed_at": "2026-03-01", "free_cash_flow": 100.0},
        {"year": 2024, "period": "FY", "filed_at": "2025-03-01", "free_cash_flow": 80.0},
    ], 1_000.0, market_as_of="2026-09-07", currency="USD")
    assert result["base_free_cash_flow"] == 100.0
    assert result["base_fcf_period"] == "2025 FY"
    assert result["base_fcf_filed_at"] == "2026-03-01"


def test_counterargument_aliases_survive_canonical_projection() -> None:
    report = normalize_report_sections({"counterarguments": {"counterarguments": ["Debt risk"], "unsupported": ["Margin assumption"], "evidence_gaps": ["Segment data"]}}, "en")
    projected = project_report_value(report["counterarguments"], include_technical=False, section="counterarguments")
    assert projected["strongest_counterarguments"] == ["Debt risk"]
    assert projected["unsupported_assumptions"] == ["Margin assumption"]
    assert projected["missing_evidence"] == ["Segment data"]
