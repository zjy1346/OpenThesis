"""Research recommendation admission is evidence-backed, separate from identity search."""
from __future__ import annotations

from datetime import date
from calendar import monthrange
from typing import Any, Iterable

from .domain import Company


def readiness(company: Company, groups: Iterable[dict[str, Any]], *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    years: set[int] = set()
    latest_period: date | None = None
    for group in groups:
        if (group.get('status') != 'VERIFIED' or group.get('fiscal_period') != 'FY'
                or group.get('consolidated_scope') != 'consolidated'
                or group.get('currency') != company.reporting_currency
                or group.get('issues')
                or not {'revenue', 'assets', 'liabilities', 'equity'}.issubset(set(group.get('covered_concepts', [])))):
            continue
        try:
            period = date.fromisoformat(str(group['period_end']))
        except (KeyError, ValueError):
            continue
        if period <= today:
            years.add(period.year)
            latest_period = max(latest_period, period) if latest_period else period
    latest = max(years, default=0)
    consecutive = 0
    while latest - consecutive in years:
        consecutive += 1
    # Policy: an annual period remains current through the next annual cycle
    # plus four publication months. Use the issuer's actual year end, not Jan 1;
    # this is a recommendation freshness rule, not a statutory deadline claim.
    fresh_until = None
    if latest_period:
        month_index = latest_period.year * 12 + latest_period.month - 1 + 16
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        fresh_until = date(year, month, min(latest_period.day, monthrange(year, month)[1]))
    eligible = consecutive >= 5 and fresh_until is not None and today <= fresh_until
    return {'policy_version': 'research-readiness-v1', 'recommended': eligible,
            'verified_annual_years': sorted(years), 'consecutive_years': consecutive,
            'fresh_until': fresh_until.isoformat() if fresh_until else None,
            'reason': 'verified_five_year_history' if eligible else 'five_year_history_not_verified'}


def primary_market_alternative(company: Company, catalogue: Iterable[Company]) -> Company | None:
    if company.market != 'HK':
        return None
    return next((item for item in catalogue if item.issuer_id == company.issuer_id
                 and item.market == 'CN_A' and item.security_id != company.security_id), None)
