from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from openthesis.domain import Company
from openthesis.market_snapshot import (
    MemorySnapshotCache,
    MarketSnapshotModule,
    QuoteSnapshot,
    ReverseDcfPolicy,
    SnapshotCacheEnvelope,
    SnapshotStatus,
    StorageSnapshotCache,
)


def _company(
    ticker: str = "AAPL",
    *,
    market: str = "US",
    listing_currency: str = "USD",
    reporting_currency: str = "USD",
    issuer_id: str = "issuer:AAPL",
    security_id: str = "security:AAPL",
) -> Company:
    return Company(
        cik=ticker,
        ticker=ticker,
        name="Fixture",
        market=market,
        exchange="NASDAQ",
        issuer_id=issuer_id,
        security_id=security_id,
        listing_currency=listing_currency,
        reporting_currency=reporting_currency,
    )


class _CountingAdapter:
    name = "fixture-public"

    def __init__(self, snapshots):
        self.snapshots = tuple(snapshots)
        self.calls = 0

    def quote(self, company, mapping, *, policy):
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[index]


def _quote(provider: str, *, price: float, cap: float, currency: str = "USD") -> QuoteSnapshot:
    return QuoteSnapshot(
        provider,
        "AAPL",
        "NASDAQ",
        price,
        cap,
        currency,
        "2026-09-07",
        "2026-09-08T00:00:00+00:00",
        source_id=f"fixture:{provider}",
    )


def _clock() -> datetime:
    return datetime(2026, 9, 8, tzinfo=timezone.utc)


def test_conflict_cache_is_terminal_and_second_capture_does_not_query_sources():
    first = _CountingAdapter((_quote("one", price=100, cap=1_000_000_000),))
    second = _CountingAdapter((_quote("two", price=120, cap=1_000_000_000),))
    module = MarketSnapshotModule(adapters=(first, second), clock=_clock)
    company = _company()

    one = module.capture(company)
    two = module.capture(company)

    assert one.status is SnapshotStatus.CONFLICT
    assert two.status is SnapshotStatus.CONFLICT
    assert two.cache == "fresh"
    assert first.calls == 1
    assert second.calls == 1
    assert not two.allows_valuation
    envelope = module._cache_port.get(module._cache_key(company, ReverseDcfPolicy()))
    assert isinstance(envelope, SnapshotCacheEnvelope)
    assert envelope.status is SnapshotStatus.CONFLICT
    assert len(envelope.source_candidates) == 2
    assert envelope.validation_version
    assert envelope.policy_fingerprint
    assert envelope.identity["issuer_id"] == "ISSUER:AAPL"


def test_policy_values_are_part_of_cache_identity_not_only_policy_version():
    adapter = _CountingAdapter((_quote("one", price=100, cap=1_000_000_000),))
    module = MarketSnapshotModule(adapters=(adapter,), clock=_clock)
    company = _company()
    first_policy = ReverseDcfPolicy(conflict_tolerance=0.05)
    changed_policy = ReverseDcfPolicy(conflict_tolerance=0.20)

    assert module.capture(company, first_policy).status is SnapshotStatus.VERIFIED
    second = module.capture(company, changed_policy)

    assert second.cache == "miss"
    assert adapter.calls == 2


def test_stale_cache_hit_cannot_be_promoted_without_new_cache_identity():
    stale = _CountingAdapter((_quote("stale", price=100, cap=1_000_000_000),))
    stale.snapshots = (
        QuoteSnapshot(
            "stale",
            "AAPL",
            "NASDAQ",
            100,
            1_000_000_000,
            "USD",
            "2026-08-01",
            "2026-09-08T00:00:00+00:00",
            source_id="fixture:stale",
        ),
    )
    module = MarketSnapshotModule(adapters=(stale,), clock=_clock)
    company = _company()
    assert module.capture(company).status is SnapshotStatus.STALE

    fresh = _CountingAdapter((_quote("fresh", price=100, cap=1_000_000_000),))
    restarted = MarketSnapshotModule(adapters=(fresh,), cache=module._cache_port, clock=_clock)
    result = restarted.capture(company)
    assert result.status is SnapshotStatus.STALE
    assert result.cache == "stale"
    assert fresh.calls == 0


def test_legacy_quote_only_cache_is_revalidated_and_never_valued_without_evidence():
    cache = MemorySnapshotCache()
    module = MarketSnapshotModule(adapters=(), cache=cache, clock=_clock)
    company = _company()
    cache.set(
        module._cache_key(company, ReverseDcfPolicy()),
        _quote("legacy", price=100, cap=1_000_000_000),
    )

    result = module.capture(company)

    assert result.status in {SnapshotStatus.UNAVAILABLE, SnapshotStatus.STALE}
    assert not result.allows_valuation


def test_storage_restart_preserves_conflict_envelope_and_identity_currency_boundary():
    with TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
        from openthesis.storage import Storage

        storage = Storage(Path(directory))
        first = _CountingAdapter((_quote("one", price=100, cap=1_000_000_000),))
        second = _CountingAdapter((_quote("two", price=120, cap=1_000_000_000),))
        company = _company()
        module = MarketSnapshotModule(
            adapters=(first, second),
            cache=StorageSnapshotCache(storage),
            clock=_clock,
        )
        assert module.capture(company).status is SnapshotStatus.CONFLICT

        restarted_first = _CountingAdapter((_quote("new-one", price=1, cap=1),))
        restarted_second = _CountingAdapter((_quote("new-two", price=2, cap=1),))
        # Reopen the actual SQLite-backed cache, not merely a second module.
        restarted_storage = Storage(Path(directory))
        restarted = MarketSnapshotModule(
            adapters=(restarted_first, restarted_second),
            cache=StorageSnapshotCache(restarted_storage),
            clock=_clock,
        )
        cached = restarted.capture(company)
        assert cached.status is SnapshotStatus.CONFLICT
        assert cached.cache == "fresh"
        assert restarted_first.calls == restarted_second.calls == 0
        persisted = restarted_storage.get_market_snapshot(restarted._cache_key(company, ReverseDcfPolicy()))
        assert persisted is not None
        assert persisted["schema_version"] == "market-snapshot-cache-v3"
        assert len(persisted["source_candidates"]) == 2
        assert persisted["status"] == SnapshotStatus.CONFLICT.value
        assert persisted["identity"]["reporting_currency"] == "USD"

        changed_identity = _company(issuer_id="issuer:OTHER")
        changed = restarted.capture(changed_identity)
        assert changed.cache == "miss"
        assert restarted_first.calls == restarted_second.calls == 1

        changed_currency = _company(listing_currency="EUR", reporting_currency="EUR")
        changed_currency_result = restarted.capture(changed_currency)
        assert changed_currency_result.cache == "miss"
        assert restarted_first.calls == restarted_second.calls == 2


def test_manual_capture_remains_manual_and_does_not_write_verified_cache():
    cache = MemorySnapshotCache()
    module = MarketSnapshotModule(adapters=(), cache=cache, clock=_clock)
    company = _company()
    policy = ReverseDcfPolicy()

    result = module.capture(
        company,
        policy,
        manual_override={"price": 100, "market_cap": 1_000_000_000, "currency": "USD", "as_of": "2026-09-07"},
    )

    assert result.status is SnapshotStatus.MANUAL
    assert result.allows_valuation
    assert cache.get(module._cache_key(company, policy)) is None

