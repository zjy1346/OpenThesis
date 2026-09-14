from __future__ import annotations

from functools import lru_cache
import re
import unicodedata

from opencc import OpenCC


@lru_cache(maxsize=1)
def _traditional_to_simplified() -> OpenCC:
    return OpenCC("t2s")


def normalize_company_query(value: object) -> str:
    """Normalize user-entered issuer text without changing stored legal names."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    # Risk-warning stars are presentation markers rather than issuer identity.
    # CNINFO accepts the stable ST prefix, while the original official name is
    # preserved from the catalogue response.
    normalized = re.sub(r"^\*+(?=ST)", "", normalized, flags=re.IGNORECASE)
    return normalized


def canonical_search_text(value: object) -> str:
    """Return a punctuation-insensitive Simplified-Chinese search key."""
    normalized = normalize_company_query(value).casefold()
    simplified = _traditional_to_simplified().convert(normalized)
    return "".join(character for character in simplified if character.isalnum())
