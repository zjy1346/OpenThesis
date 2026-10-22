"""Source-version comparison, separate from source-cell admission.

Two independently admitted printed amounts can each be genuine while their
comparability remains unresolved. Neither filing age nor a small percentage
difference is a restatement explanation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any


@dataclass(frozen=True, slots=True)
class ComparisonBinding:
    """A metric-local comparison, never a completeness claim about a filing."""

    concept: str
    current_fact_id: str
    prior_fact_id: str
    relation: str
    status: str
    reason: str
    current_start: str
    current_end: str
    prior_start: str
    prior_end: str
    source: str = ""
    candidate_fact_ids: tuple[str, ...] = ()
    prior_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _date(record: Any, field: str) -> str:
    value = str(_get(record, field) or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _start(record: Any) -> str:
    return _date(record, "start_date") or _date(record, "period_start")


def _prior_year(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
        try:
            return parsed.replace(year=parsed.year - 1).isoformat()
        except ValueError:  # February 29 in a leap-year disclosure.
            return parsed.replace(year=parsed.year - 1, day=28).isoformat()
    except ValueError:
        return ""


def _duration_shape_valid(start: str, end: str, period: str) -> bool:
    """Check a disclosed interval, without manufacturing missing dates.

    Week-based calendars can vary slightly. Q3 can denote a single quarter
    or cumulative nine months, but exact start/end pairing below never mixes
    these two shapes with one another.
    """
    length = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    ranges = {
        "FY": ((350, 378),), "CY": ((350, 378),), "ANNUAL": ((350, 378),),
        "H1": ((168, 196),), "9M": ((252, 294),),
        "Q1": ((77, 105),), "Q2": ((77, 105),),
        "Q3": ((77, 105), (252, 294)), "Q4": ((77, 105),),
    }.get(period, ((1, 378),))
    return any(lower <= length <= upper for lower, upper in ranges)


def _comparison_identity(record: Any) -> tuple[Any, ...]:
    dimensions = _get(record, "dimensions", ())
    if isinstance(dimensions, Mapping):
        dimensions = sorted(dimensions.items())
    return (
        str(_get(record, "company_cik") or ""),
        str(_get(record, "concept") or ""),
        str(_get(record, "statement") or "").casefold(),
        str(_get(record, "consolidated_scope") or _get(record, "scope") or "").casefold(),
        str(_get(record, "currency") or _get(record, "unit") or "").upper(),
        str(dimensions),
    )


def comparison_binding(current: Any, records: Any, *, relation: str = "year_over_year") -> ComparisonBinding:
    """Bind exact official dates and semantic identity for one admitted value.

    Values are normalized base-currency amounts already admitted by the caller;
    different printed multipliers alone are not a currency conversion. Never
    infer a missing start date from an H1/Q label or relabel an instant as H1.
    """
    end, start = _date(current, "end_date"), _start(current)
    instant = str(_get(current, "statement") or "").casefold() == "balance_sheet"
    prior_end = _prior_year(end)
    if relation == "previous_year_end":
        prior_end = f"{int(end[:4]) - 1}-12-31" if end and instant else ""
    expected_start = "" if instant else _prior_year(start)
    common = dict(concept=str(_get(current, "concept") or ""),
        current_fact_id=str(_get(current, "fact_id") or ""), relation=relation,
        current_start=start, current_end=end)
    def result(status: str, reason: str, candidates=(), chosen=None):
        return ComparisonBinding(**common, prior_fact_id=str(_get(chosen, "fact_id") or ""),
            status=status, reason=reason, prior_start=_start(chosen) if chosen else expected_start,
            prior_end=_date(chosen, "end_date") if chosen else prior_end,
            source=("same_filing_comparator" if chosen and _get(chosen, "accession_number") ==
                    _get(current, "accession_number") else "independent_historical" if chosen else ""),
            candidate_fact_ids=tuple(str(_get(item, "fact_id") or "") for item in candidates),
            prior_value=float(_get(chosen, "value")) if chosen else None)
    if not end or (not instant and (not start or start > end)) or not prior_end:
        return result("unavailable", "period_identity_unverified")
    if not instant and not _duration_shape_valid(start, end,
            str(_get(current, "fiscal_period") or "").upper()):
        return result("unavailable", "duration_shape_unverified")
    identity = _comparison_identity(current)
    if not identity[3] or not identity[4]:
        return result("unavailable", "scope_or_currency_unverified")
    candidates = []
    for prior in records:
        if _comparison_identity(prior) != identity:
            continue
        if _date(prior, "end_date") != prior_end:
            continue
        if not instant and (_start(prior) != expected_start or
                str(_get(prior, "fiscal_period") or "").upper() !=
                str(_get(current, "fiscal_period") or "").upper()):
            continue
        if _get(prior, "value") is None:
            continue
        # The compiler's hidden lane also contains independently filed prior
        # periods. Keep every captured version for reconciliation rather than
        # equating the display role with "same-file comparative column".
        candidates.append(prior)
    if not candidates:
        return result("unavailable", "comparable_fact_unavailable")
    same_file = [item for item in candidates
                 if _get(item, "accession_number") == _get(current, "accession_number")]
    chosen = max(same_file or candidates, key=lambda item:
        (str(_get(item, "filed_at") or ""), str(_get(item, "fact_id") or "")))
    if any(not comparative_amounts_agree(chosen, item) for item in candidates):
        return result("conflict", "unresolved_restatement", candidates)
    return result("available", "", candidates, chosen)


def build_comparison_bindings(facts: Any) -> tuple[ComparisonBinding, ...]:
    records = tuple(facts)
    bindings = []
    for fact in records:
        if str(_get(fact, "usage_status") or "").casefold() == "comparator":
            continue
        bindings.append(comparison_binding(fact, records))
        if str(_get(fact, "statement") or "").casefold() == "balance_sheet":
            bindings.append(comparison_binding(fact, records, relation="previous_year_end"))
    return tuple(bindings)


def _get(record: Any, field: str, default: Any = None) -> Any:
    return record.get(field, default) if isinstance(record, Mapping) else getattr(record, field, default)


def _printed_amount(record: Any) -> tuple[Decimal, Decimal] | None:
    lineage = _get(record, "normalization_lineage")
    if (not lineage or _get(record, "source_page") is None
            or not _get(record, "source_bbox") or not _get(record, "raw_text")
            or _get(record, "unit_provenance", "unknown") == "unknown"
            or _get(lineage, "quantity_kind") != "money"
            or _get(lineage, "inputs", ())
            or not _get(lineage, "unit_evidence_ids", ())
            or _get(lineage, "value_transform") != "identity"):
        return None
    raw = str(_get(lineage, "raw_value", "")).strip().replace(",", "").replace("−", "-")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1].strip()
    try:
        value, scale = Decimal(raw), Decimal(str(_get(lineage, "scale", "")))
        amount = Decimal(str(_get(lineage, "normalized_value", "")))
        if not all(item.is_finite() for item in (value, scale, amount)) or scale <= 0:
            return None
        with localcontext() as context:
            context.prec = max(50, len(raw) + len(str(scale)) + len(str(amount)) + 8)
            if value * scale != amount:
                return None
            quantum = Decimal(1).scaleb(value.as_tuple().exponent) * scale
        return amount, quantum
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


def comparative_amounts_agree(left: Any, right: Any) -> bool:
    """Exact agreement or proven printed quantization; never arbitrary epsilon.

    Caller supplies independently admitted facts of the same period/concept.
    Scope, currency and dates, when present, must not conflict. Equal-precision
    differing cells stay separate even if their difference is only one cent.
    """
    instant = (str(_get(left, "statement") or "").casefold() == "balance_sheet"
               and str(_get(right, "statement") or "").casefold() == "balance_sheet")
    period_fields = () if instant else ("start_date", "period_start", "fiscal_period")
    for field in ("concept", "end_date", "currency", "statement", *period_fields):
        a, b = _get(left, field), _get(right, field)
        if a and b and str(a).casefold() != str(b).casefold():
            return False
    a_scope = _get(left, "consolidated_scope") or _get(left, "scope")
    b_scope = _get(right, "consolidated_scope") or _get(right, "scope")
    if a_scope and b_scope and str(a_scope).casefold() != str(b_scope).casefold():
        return False
    try:
        a, b = Decimal(str(_get(left, "value"))), Decimal(str(_get(right, "value")))
        if not a.is_finite() or not b.is_finite():
            return False
        if a == b:
            return True
    except (InvalidOperation, ValueError):
        return False
    displayed_a, displayed_b = _printed_amount(left), _printed_amount(right)
    if displayed_a is None or displayed_b is None:
        return False
    a, aq = displayed_a
    b, bq = displayed_b
    if aq == bq:
        return False
    with localcontext() as context:
        context.prec = max(50, len(str(a)) + len(str(b)) + len(str(aq)) + len(str(bq)) + 8)
        return abs(a - b) <= max(aq, bq) / 2
