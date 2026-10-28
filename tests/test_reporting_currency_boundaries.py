from openthesis.market_snapshot import MarketSnapshotModule, SnapshotStatus
from openthesis.research import _valuation_money
from test_market_snapshot_cache_trust import _company, _quote, _clock, _CountingAdapter


def test_unknown_reporting_currency_does_not_inherit_trade_currency_or_call_fx():
    class NoFx:
        def rate(self, *_):
            raise AssertionError("no FX without known reporting currency")
    adapter = _CountingAdapter((_quote("one", price=100, cap=1000, currency="HKD"),))
    module = MarketSnapshotModule(adapters=(adapter,), fx_adapter=NoFx(), clock=_clock)
    company = _company(listing_currency="HKD", reporting_currency="")
    for _ in range(2):
        result = module.capture(company)
        assert result.reporting_currency == ""
        assert result.quote_currency == "HKD"
        assert result.status is SnapshotStatus.UNRESOLVED
        assert result.error_code == "REPORTING_CURRENCY_UNCONFIRMED"
        assert not result.allows_valuation and result.price == 100


def test_unit_and_currency_ambiguity_remain_local_valuation_gaps():
    for value in ({"value": 100}, {"value": 100, "currency": "CNY"},
                  {"value": 100, "currency": "CNY", "unit_scale": 1},
                  {"value": 100, "currency": "CNY", "unit_scale": 1000, "unit_provenance": "normalized"},
                  {"value": float("inf"), "currency": "CNY", "unit_scale": 1, "unit_provenance": "normalized"}):
        assert _valuation_money(value, currency="CNY") is None
    item = _valuation_money({"value": 100, "currency": "CNY", "unit_scale": 1000, "unit_provenance": "declared"}, currency="HKD")
    assert item.normalized_value == 100000 and item.currency == "CNY"
