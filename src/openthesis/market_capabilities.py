from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class MarketCapability:
    market: str
    official_authority: str
    discovery: bool
    structured_financials: bool
    pdf_layout: bool
    vision_fallback: bool
    accounting_standards: tuple[str, ...]
    annual_forms: tuple[str, ...]


class MarketCapabilityRegistry:
    def __init__(self, capabilities: Iterable[MarketCapability] = ()):
        self._items = {item.market.upper(): item for item in capabilities}

    def register(self, capability: MarketCapability) -> None:
        key = capability.market.upper()
        if key in self._items:
            raise ValueError(f"market capability already registered: {key}")
        self._items[key] = capability

    def require(self, market: str) -> MarketCapability:
        try:
            return self._items[market.upper()]
        except KeyError as exc:
            raise LookupError(f"market capability is not registered: {market}") from exc

    def snapshot(self) -> tuple[MarketCapability, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


DEFAULT_MARKET_CAPABILITIES = MarketCapabilityRegistry((
    MarketCapability("US", "SEC", True, True, True, True, ("US_GAAP", "IFRS"), ("10-K", "20-F", "40-F")),
    MarketCapability("HK", "HKEX", True, False, True, True, ("IFRS", "HKFRS", "CAS"), ("ANNUAL_REPORT",)),
    MarketCapability("CN_A", "SSE/SZSE/BSE", True, False, True, True, ("CAS",), ("ANNUAL_REPORT",)),
))
