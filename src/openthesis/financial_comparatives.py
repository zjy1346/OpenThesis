"""Source-version comparison, separate from source-cell admission.

Two independently admitted printed amounts can each be genuine while their
comparability remains unresolved. Neither filing age nor a small percentage
difference is a restatement explanation.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any


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
    for field in ("concept", "end_date", "fiscal_period", "currency", "statement"):
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
