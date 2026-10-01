from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .domain import FilingDocument


@dataclass(frozen=True, slots=True)
class DisclosureCoverage:
    requested_count: int
    report_types: tuple[str, ...]
    authority: str
    found_periods: tuple[str, ...]
    missing_periods: tuple[str, ...]
    exhausted: bool
    failures: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return (
            len(self.found_periods) >= self.requested_count
            and not self.missing_periods
            and not self.failures
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_count": self.requested_count,
            "report_types": list(self.report_types),
            "authority": self.authority,
            "found_periods": list(self.found_periods),
            "missing_periods": list(self.missing_periods),
            "exhausted": self.exhausted,
            "failures": list(self.failures),
            "complete": self.complete,
        }


class DisclosureCoveragePlanner:
    """Turn discovery length into an explicit, auditable coverage contract."""

    _ANNUAL_FORMS = frozenset({"10-K", "20-F", "40-F", "ANNUAL_REPORT"})

    def evaluate(
        self,
        filings: Iterable[FilingDocument],
        *,
        requested_count: int,
        authority: str,
        failures: Iterable[str] = (),
    ) -> DisclosureCoverage:
        requested = max(1, int(requested_count))
        periods = sorted(
            {
                str(item.period_end)[:4]
                for item in filings
                if item.form_type in self._ANNUAL_FORMS and str(item.period_end)[:4].isdigit()
            },
            reverse=True,
        )
        if periods:
            latest = int(periods[0])
            expected = tuple(str(latest - offset) for offset in range(requested))
        else:
            expected = ()
        found = tuple(period for period in expected if period in periods)
        missing = tuple(period for period in expected if period not in periods)
        errors = tuple(dict.fromkeys(str(item) for item in failures if str(item)))
        return DisclosureCoverage(
            requested, tuple(sorted(self._ANNUAL_FORMS)), authority,
            found, missing, len(periods) < requested, errors,
        )
