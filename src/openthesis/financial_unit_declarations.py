"""I/O-free literal unit evidence, independent of candidate metadata.

Missing or conflicting declarations stay unknown. No issuer-country or
amount-magnitude inference is used to fill a currency or multiplier.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata

from .text_normalization import canonical_search_text


@dataclass(frozen=True, slots=True)
class LiteralMoneyUnit:
    currencies: frozenset[str]
    scales: frozenset[str]
    scale_explicit: bool = True

    def proves(self, currency: str, scale: str) -> bool:
        from decimal import Decimal
        return self.currencies == frozenset({currency}) and self.scales == frozenset({str(Decimal(scale).normalize())})


_SCALE_LITERALS = {
    "billion": "1000000000", "十亿元": "1000000000", "亿元": "100000000",
    "million": "1000000", "百万元": "1000000", "万元": "10000", "万人民币": "10000",
    "thousand": "1000", "千元": "1000", "千人民币": "1000", "'000": "1000",
}
_SCALE_PATTERN = re.compile("|".join(re.escape(key) for key in sorted(_SCALE_LITERALS, key=len, reverse=True)))


@lru_cache(maxsize=2048)
def literal_money_unit(excerpt: str) -> LiteralMoneyUnit:
    from decimal import Decimal
    text = unicodedata.normalize("NFKC", str(excerpt)).casefold().translate(
        str.maketrans({"’": "'", "‘": "'", "′": "'", "＇": "'", "ʼ": "'"})
    )
    # Canonicalize Chinese variants without dropping the apostrophe in '000.
    normalized = canonical_search_text(text)
    compact = re.sub(r"\s+", "", text)
    currencies = frozenset(code for code, markers in (
        ("CNY", ("人民币", "rmb", "cny", "renminbi")),
        ("USD", ("美元", "usd", "us$", "usdollars", "unitedstatesdollars")),
        ("HKD", ("港元", "港币", "hkd", "hk$", "hongkongdollars")),
    ) if any((marker in normalized if any(ord(char) > 127 for char in marker) else marker in compact)
             for marker in markers))
    # Match longer literals first; 百万元 is not a second 万元 declaration.
    scales = {_SCALE_LITERALS[match.group()] for match in _SCALE_PATTERN.finditer(normalized)}
    if re.search(r"'\s*000", compact):
        scales.add("1000")
    if not scales and re.search(
        r"(?:单位|unit)[:：]?(?:人民币|rmb|cny|美元|usd|港元|hkd)?(?:元|yuan)"
        r"|人民币元|inyuan|indollars", normalized,
    ):
        scales.add("1")
    explicit = bool(scales)
    if not scales and currencies:
        # A currency-only formal header denotes base currency amounts. It is
        # supplied by a bound unit EvidenceRef, not arbitrary document prose.
        scales.add("1")
    return LiteralMoneyUnit(currencies, frozenset(str(Decimal(value).normalize()) for value in scales), explicit)


def combined_money_unit(excerpts) -> LiteralMoneyUnit:
    """Compose bound currency and display-unit observations, not amounts.

    A currency-only cell does not contradict a separate explicit multiplier.
    Two explicit multipliers or currencies remain conflicting. The caller
    independently proves every physical reference before this composition.
    """
    units = tuple(literal_money_unit(excerpt) for excerpt in excerpts)
    explicit = tuple(unit for unit in units if unit.scale_explicit)
    scale_sources = explicit or units
    return LiteralMoneyUnit(
        frozenset(value for unit in units for value in unit.currencies),
        frozenset(value for unit in scale_sources for value in unit.scales),
        bool(explicit),
    )


def physical_unit_context(fact, evidence_by_id) -> tuple[object, ...]:
    """A header's physical identity, not its per-fact generated evidence ID."""
    lineage = fact.normalization_lineage
    refs = tuple(evidence_by_id.get(key) for key in lineage.unit_evidence_ids) if lineage else ()
    if not refs or any(ref is None for ref in refs):
        return ()
    return tuple(sorted({(ref.document_id, ref.content_hash, ref.locator, ref.excerpt, ref.bbox)
                         for ref in refs}, key=repr))
