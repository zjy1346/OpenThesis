"""Pure projection of admitted financial facts into period-aware profiles.

This module intentionally depends on domain and contract types only.  It is
safe for the admission boundary to use without importing the PDF parser.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from .domain import EvidenceRef, FilingDocument, FinancialFact
from .financial_contracts import FinancialGroupValidation, ValidationStatus


@dataclass(frozen=True, slots=True)
class FinancialProfile:
    """Validated financial view consumed by deterministic metrics and research."""

    facts: tuple[FinancialFact, ...]
    fact_dicts: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    interim_metrics: tuple[dict[str, Any], ...]
    validation_groups: tuple[FinancialGroupValidation, ...]
    rejected_periods: tuple[dict[str, Any], ...]
    reporting_currency: str
    status: ValidationStatus
    period_continuity: tuple[dict[str, Any], ...] = ()
    period_coverage: dict[str, Any] = field(default_factory=dict)
    # Independently validated dimension/share disclosures are model evidence,
    # not interchangeable with the consolidated financial metric matrix.
    disclosure_facts: tuple[FinancialFact, ...] = ()
    # Source-current parser observations remain separate from accepted facts;
    # they are available for explicit repair mapping only.
    parsing_observations: tuple[dict[str, Any], ...] = ()
    repair_observations: tuple[dict[str, Any], ...] = ()
    source_evidence: tuple[EvidenceRef, ...] = ()


def _validation_group_role(group: Any) -> str:
    """Return the explicit period role, retaining old-payload compatibility."""

    explicit = str(getattr(group, "role", "") or "").strip().casefold()
    if explicit in {"target", "comparator", "audit"}:
        return explicit
    validation = getattr(group, "validation", None)
    facts = tuple(getattr(validation, "accepted", ())) + tuple(
        getattr(validation, "quarantined", ())
    )
    if facts and all(
        str(getattr(fact, "usage_status", "") or "").casefold() == "comparator"
        for fact in facts
    ):
        return "comparator"
    return "target"


def _fact_period_identity(fact: FinancialFact) -> tuple[str, str, str, str, str]:
    return (
        str(fact.accession_number),
        str(fact.end_date)[:10],
        str(fact.fiscal_period).upper(),
        str(fact.consolidated_scope or fact.scope or "consolidated").casefold(),
        str(fact.currency or fact.unit or "").upper(),
    )


def _annual_period_end(period_end: Any, fiscal_period: Any) -> tuple[int, str] | None:
    """Return the year and exact ISO end date for a formal annual period."""

    if str(fiscal_period or "").strip().upper() not in {"", "FY", "CY", "ANNUAL"}:
        return None
    end = str(period_end or "")[:10]
    try:
        parsed = date.fromisoformat(end)
    except ValueError:
        return None
    if parsed.isoformat() != end:
        return None
    return parsed.year, end


def build_financial_profile(
    facts: Sequence[FinancialFact],
    validation_groups: Sequence[FinancialGroupValidation] = (),
    reporting_currency: str = "",
    *,
    selected_filings: Sequence[FilingDocument] = (),
    manifests: Sequence[Any] = (),
    requested_annual_count: int | None = None,
    research_as_of: str | None = None,
    parsing_observations: Sequence[dict[str, Any]] = (),
) -> FinancialProfile:
    """Build one trusted, period-aware profile without reimplementing gate logic.

    ``selected_filings``/``manifests`` make an absent fact group observable.  A
    selected annual report with no accepted facts is represented as ``no_facts``
    rather than silently disappearing from the continuity view.
    """

    from .financials import calculate_interim_metrics, calculate_metrics

    groups = tuple(validation_groups)
    profile_groups = tuple(
        group for group in groups
        if _validation_group_role(group) == "target"
        and str(group.identity[3] or "consolidated").casefold() == "consolidated"
        and (
            not reporting_currency
            or not group.identity[4]
            or str(group.identity[4]).upper() == str(reporting_currency).upper()
        )
    )
    rejected_identities = {
        (
            str(group.identity[0]), str(group.identity[1])[:10],
            str(group.identity[2]).upper(), str(group.identity[3]).casefold(),
            str(group.identity[4]).upper(),
        )
        for group in profile_groups
        if group.validation.status is ValidationStatus.REJECTED
    }
    # Group capability/coverage cannot revoke an independently admitted sibling
    # cell. Legacy rejected groups without accepted-cell proof stay excluded.
    independently_accepted = {
        fact.fact_id for group in profile_groups for fact in group.validation.accepted
        if fact.validation_status != ValidationStatus.REJECTED.value
    }
    accepted = tuple(
        fact for fact in facts
        if fact.validation_status != ValidationStatus.REJECTED.value
        and (_fact_period_identity(fact) not in rejected_identities
             or fact.fact_id in independently_accepted)
    )
    fact_dicts = tuple(fact.to_dict() for fact in accepted)

    period_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for filing in selected_filings:
        key = (
            filing.accession_number,
            str(filing.period_end)[:10],
            str(filing.fiscal_period or "FY").upper(),
        )
        period_rows[key] = {
            "accession_number": filing.accession_number,
            "period_end": filing.period_end,
            "fiscal_period": filing.fiscal_period,
            "form_type": filing.form_type,
            "scope": "",
            "currency": "",
            "status": "no_facts",
            "issues": (),
        }
    for manifest in manifests:
        key = (
            manifest.accession_number,
            str(manifest.period_end)[:10],
            str(manifest.fiscal_period or "FY").upper(),
        )
        row = period_rows.setdefault(key, {
            "accession_number": manifest.accession_number,
            "period_end": manifest.period_end,
            "fiscal_period": manifest.fiscal_period,
            "form_type": manifest.form_type,
            "scope": "",
            "currency": "",
            "status": "no_facts",
            "issues": (),
        })
        row.update(
            period_end=manifest.period_end,
            fiscal_period=manifest.fiscal_period,
            form_type=manifest.form_type,
        )
    for group in profile_groups:
        accession, period_end, fiscal_period, scope, currency = group.identity
        key = (accession, str(period_end)[:10], str(fiscal_period).upper())
        row = period_rows.setdefault(key, {
            "accession_number": accession,
            "period_end": period_end,
            "fiscal_period": fiscal_period,
            "form_type": "",
            "scope": scope,
            "currency": currency,
            "status": "no_facts",
            "issues": (),
        })
        row.update(
            period_end=period_end,
            fiscal_period=fiscal_period,
            scope=scope,
            currency=currency,
            status=(
                "rejected"
                if group.validation.status is ValidationStatus.REJECTED
                else "accepted"
            ),
            issues=tuple(group.validation.issues),
        )
    accepted_periods = {
        (fact.accession_number, str(fact.end_date)[:10], str(fact.fiscal_period).upper())
        for fact in accepted
        if str(getattr(fact, "usage_status", "") or "").casefold() != "comparator"
    }
    for key, row in period_rows.items():
        if key in accepted_periods:
            row["status"] = "partial" if row["status"] == "rejected" else "accepted"
    period_continuity = tuple(
        sorted(
            period_rows.values(),
            key=lambda item: (
                str(item.get("period_end", "")),
                str(item.get("accession_number", "")),
            ),
            reverse=True,
        )
    )
    rejected_periods = tuple(
        {key: value for key, value in row.items() if key != "accession_number"}
        for row in period_continuity
        if row["status"] == "rejected"
    )
    annual_rows = [
        row for row in period_continuity
        if str(row.get("fiscal_period", "")).upper() in {"", "FY", "CY", "ANNUAL"}
    ]
    annual_periods = [
        (row, identity)
        for row in annual_rows
        if (identity := _annual_period_end(
            row.get("period_end"), row.get("fiscal_period")
        )) is not None
    ]
    discovered_annual_years = sorted({identity[0] for _, identity in annual_periods})
    # Parsing output is not an admission decision. Unknown currency/parent
    # scope/rejected groups are still observed source periods, and an archive
    # may retain them even when no fact from that accession is active.
    parsed_periods = {
        (str(group.identity[0]), str(group.identity[1])[:10], str(group.identity[2]).upper())
        for group in groups
        if group.validation.accepted or group.validation.quarantined
    }
    selected_keys = set(period_rows)
    parsed_periods.update((str(item.get("accession_number", "")), str(item.get("period_end", ""))[:10],
        str(item.get("fiscal_period", "")).upper()) for item in parsing_observations
        if isinstance(item, dict) and (str(item.get("accession_number", "")),
            str(item.get("period_end", ""))[:10], str(item.get("fiscal_period", "")).upper()) in selected_keys)
    parsed_annual_years = sorted({
        identity[0]
        for _accession, end, period in parsed_periods
        if (identity := _annual_period_end(end, period)) is not None
    })
    admitted_annual_years = sorted({
        identity[0]
        for row, identity in annual_periods
        if row.get("status") in {"accepted", "partial"}
    })
    # Keep this compatibility name tied to admitted facts, not mere discovery.
    annual_years = admitted_annual_years
    all_comparator_years = sorted({
        int(fact.fiscal_year) for fact in facts
        if str(getattr(fact, "usage_status", "")).casefold() == "comparator"
    })
    interim_periods = sorted({
        f"{fact.fiscal_year} {fact.fiscal_period}"
        for fact in accepted
        if str(fact.fiscal_period).upper() not in {"", "FY", "CY", "ANNUAL"}
    })
    requested_annual = [
        filing for filing in selected_filings
        if str(filing.fiscal_period or "").upper() in {"", "FY", "CY", "ANNUAL"}
    ]
    display_limit = (
        max(1, int(requested_annual_count))
        if requested_annual_count is not None and int(requested_annual_count) > 0
        else 5
    )
    displayed_years = tuple(admitted_annual_years[-display_limit:])
    latest_discovered_year = (
        discovered_annual_years[-1] if discovered_annual_years else None
    )
    expected_years = (
        tuple(range(latest_discovered_year - display_limit + 1, latest_discovered_year + 1))
        if latest_discovered_year is not None else ()
    )
    annual_end_by_year: dict[int, str] = {}
    for _, identity in annual_periods:
        # period_continuity is newest-first, so the first observed end date for
        # a year is the best available official filing identity for that year.
        annual_end_by_year.setdefault(*identity)
    latest_period_end = (
        annual_end_by_year.get(latest_discovered_year)
        if latest_discovered_year is not None else None
    )
    if latest_period_end and expected_years:
        requested_range = (
            f"{expected_years[0]}{latest_period_end[4:10]}", latest_period_end,
        )
    else:
        requested_ends = [annual_end_by_year[year] for year in discovered_annual_years]
        requested_range = (
            (requested_ends[0], requested_ends[-1]) if requested_ends else None
        )
    hidden_comparator_years = sorted(
        year for year in all_comparator_years if year not in displayed_years
    )
    research_dates = [
        str(fact.filed_at) for fact in accepted if str(fact.filed_at)
    ] + [str(filing.filed_at) for filing in selected_filings if str(filing.filed_at)]
    rejected_status_by_year = {
        int(str(row.get("period_end", ""))[:4]): str(row.get("status", ""))
        for row in annual_rows
        if str(row.get("period_end", ""))[:4].isdigit()
    }
    missing_or_rejected = tuple(
        {
            "year": year,
            "status": (
                "rejected" if rejected_status_by_year.get(year) == "rejected"
                else "parsed_unadmitted" if year in parsed_annual_years
                else "missing"
            ),
            "reason_code": (
                "OFFICIAL_ANNUAL_REJECTED"
                if rejected_status_by_year.get(year) == "rejected"
                else "OFFICIAL_ANNUAL_NOT_ADMITTED" if year in parsed_annual_years
                else "OFFICIAL_ANNUAL_UNAVAILABLE"
            ),
        }
        for year in expected_years
        if year not in annual_years or rejected_status_by_year.get(year) == "rejected"
    )
    period_coverage = {
        "requested_annual_count": (
            int(requested_annual_count)
            if requested_annual_count is not None
            else len(requested_annual) if selected_filings else None
        ),
        "requested_annual_range": requested_range,
        "required_annual_periods": tuple(
            {"year": year, "period": "FY", "period_end": annual_end_by_year.get(
                year, f"{year}{latest_period_end[4:10]}" if latest_period_end else "")}
            for year in expected_years
        ),
        "discovered_annual_years": tuple(discovered_annual_years),
        "parsed_annual_years": tuple(parsed_annual_years),
        "admitted_annual_years": tuple(admitted_annual_years),
        "available_annual_years": tuple(annual_years),
        "displayed_annual_years": displayed_years,
        "hidden_comparator_years": tuple(hidden_comparator_years),
        "latest_official_fy": (
            discovered_annual_years[-1] if discovered_annual_years else None
        ),
        "latest_admitted_fy": (
            admitted_annual_years[-1] if admitted_annual_years else None
        ),
        "interim_periods": tuple(interim_periods),
        "missing_or_rejected_years": missing_or_rejected,
        "missing_reason_code": (
            "OFFICIAL_ANNUAL_REJECTED"
            if any(
                item["reason_code"] == "OFFICIAL_ANNUAL_REJECTED"
                for item in missing_or_rejected
            )
            else "OFFICIAL_ANNUAL_NOT_ADMITTED" if any(
                item["reason_code"] == "OFFICIAL_ANNUAL_NOT_ADMITTED" for item in missing_or_rejected)
            else "OFFICIAL_ANNUAL_UNAVAILABLE" if missing_or_rejected else None
        ),
        "research_as_of": research_as_of or (max(research_dates) if research_dates else None),
    }
    statuses = {group.validation.status for group in groups}
    has_missing_periods = any(item["status"] == "no_facts" for item in period_continuity)
    status = (
        ValidationStatus.REJECTED if not accepted
        else ValidationStatus.READY_WITH_WARNINGS
        if (
            ValidationStatus.REJECTED in statuses
            or ValidationStatus.READY_WITH_WARNINGS in statuses
            or has_missing_periods
        )
        else ValidationStatus.VERIFIED
    )
    return FinancialProfile(
        accepted,
        fact_dicts,
        tuple(calculate_metrics(list(fact_dicts))),
        tuple(calculate_interim_metrics(list(fact_dicts))),
        groups,
        rejected_periods,
        str(reporting_currency or (accepted[0].currency if accepted else "")),
        status,
        period_continuity,
        period_coverage,
        parsing_observations=tuple(
            deepcopy(item) for item in parsing_observations
            if isinstance(item, dict)
        ),
    )
