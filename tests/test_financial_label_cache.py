"""Bounded lookup optimization must preserve the full compatibility identity."""
from openthesis.financial_compatibility import FinancialRulesSnapshot
from openthesis.financial_ingestion import _exact_label_keys, _label_compact, _known_label, _completes_known_label


def test_label_cache_never_reuses_aliases_from_same_metadata_different_rules():
    first = FinancialRulesSnapshot(
        pack_id="fixture", version="1.0.0", payload_sha256="fixture-digest",
        taxonomy_aliases=(("revenue", ("Alpha top line",)),),
    )
    second = FinancialRulesSnapshot(
        pack_id="fixture", version="1.0.0", payload_sha256="fixture-digest",
        taxonomy_aliases=(("revenue", ("Beta top line",)),),
    )
    assert "alphatopline" in _exact_label_keys(first)
    assert "betatopline" not in _exact_label_keys(first)
    assert "betatopline" in _exact_label_keys(second)
    assert "alphatopline" not in _exact_label_keys(second)
    assert "alphatopline" not in _exact_label_keys(None)
    assert _known_label("Alpha top line", first)
    assert not _known_label("Alpha top line", second)
    assert _known_label("Beta top line", second)
    assert not _known_label("Beta top line", first)


def test_label_cache_retains_typography_semantics_and_bounded_immutable_keys():
    assert _label_compact("Total shareholders’ equity") == _label_compact("total shareholders' equity")
    assert isinstance(_exact_label_keys(None), frozenset)
    assert _exact_label_keys.cache_info().maxsize == 32
    assert _label_compact.cache_info().maxsize == 8192
    assert _known_label.cache_info().maxsize == 8192
    assert _completes_known_label.cache_info().maxsize == 8192
