"""Repeated financial row classification must not redo Unicode/OpenCC work."""
from openthesis import financial_taxonomy as taxonomy
from openthesis.financial_ingestion import _labels_for_rules, _label_compact


def test_repeated_row_classification_bounds_expensive_normalization(monkeypatch):
    cache = getattr(taxonomy, '_cached_canonical_financial_label', None)
    if cache is not None:
        cache.cache_clear()
    _label_compact.cache_clear()
    original = taxonomy.canonical_search_text
    calls = []
    def measured(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(taxonomy, 'canonical_search_text', measured)
    aliases = [alias for values in _labels_for_rules(None).values() for alias in values]
    for _ in range(30):
        assert not any(taxonomy.label_matches('未登记的往来明细326', alias) for alias in aliases)
    assert len(calls) < 2_000, f'{len(calls)} expensive conversions for 30 identical rows'


def test_normalization_keeps_original_semantics_for_aliases_and_unhashable_values():
    values = ('營業收入', '營業收入（人民幣千元）', 'Operating Cash Flow', None, 0, False, [], {'text':'收益'})
    for value in values:
        assert taxonomy.normalize_financial_label(value) == taxonomy.canonical_search_text(value)
