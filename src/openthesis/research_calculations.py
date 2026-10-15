"""Explicit, source-bound financial calculation proofs, never prose inference.

Inputs are canonical amounts (already normalized by FinancialPipeline). A
formula names a fixed accounting operation and its exact roles; arbitrary
expressions, inline amounts, unknown sources and mixed identities are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

from .financial_contracts import exact_decimal_arithmetic


@dataclass(frozen=True, slots=True)
class Formula:
    roles: tuple[tuple[str, str], ...]
    operation: str
    unit: str = "ratio"


FORMULAS = {
    "revenue_growth": Formula((("current", "revenue"), ("previous", "revenue")), "growth"),
    "net_income_growth": Formula((("current", "net_income"), ("previous", "net_income")), "growth"),
    "operating_cash_flow_growth": Formula((("current", "operating_cash_flow"), ("previous", "operating_cash_flow")), "growth"),
    "operating_margin": Formula((("numerator", "operating_income"), ("denominator", "revenue")), "ratio"),
    "net_margin": Formula((("numerator", "net_income"), ("denominator", "revenue")), "ratio"),
    "gross_margin": Formula((("numerator", "gross_profit"), ("denominator", "revenue")), "ratio"),
    "debt_ratio": Formula((("numerator", "liabilities"), ("denominator", "assets")), "ratio"),
    "cash_conversion": Formula((("numerator", "operating_cash_flow"), ("denominator", "net_income")), "ratio"),
    "free_cash_flow": Formula((("operating_cash_flow", "operating_cash_flow"), ("capital_expenditure", "capital_expenditure")), "subtract", "currency"),
}


@dataclass(frozen=True, slots=True)
class CalculationProof:
    issue_code: str = ""
    record: dict[str, Any] | None = None


def formula_contract() -> dict[str, Any]:
    return {name: {"inputs": dict(formula.roles), "unit": formula.unit}
            for name, formula in FORMULAS.items()}


def _number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean amount")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite amount")
    return number


def _same_period(left: Mapping, right: Mapping) -> bool:
    return all(str(left.get(key) or "") == str(right.get(key) or "")
               for key in ("start_date", "end_date", "fiscal_year", "fiscal_period"))


def _prior_year(left: Mapping, right: Mapping) -> bool:
    try:
        if int(left["fiscal_year"]) != int(right["fiscal_year"]) + 1:
            return False
        if left.get("fiscal_period") != right.get("fiscal_period"):
            return False
        for key in ("start_date", "end_date"):
            if not left.get(key) and not right.get(key) and key == "start_date":
                continue
            current, previous = date.fromisoformat(left[key]), date.fromisoformat(right[key])
            # Feb 29 and Feb 28 are the same annual boundary, not a duration
            # shortcut allowing arbitrary nearby dates or skipped fiscal years.
            if current.year != previous.year + 1 or current.month != previous.month:
                return False
            if current.day != previous.day and not (current.month == 2 and {current.day, previous.day} == {28, 29}):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def prove_calculation(claim: dict, records: list[dict]) -> CalculationProof:
    formula_name = claim.get("formula")
    formula = FORMULAS.get(formula_name) if isinstance(formula_name, str) else None
    if formula is None or claim.get("concept") != formula_name:
        return CalculationProof("CLAIM_FORMULA_UNSUPPORTED")
    bindings = claim.get("inputs")
    if not isinstance(bindings, dict) or set(bindings) != {role for role, _ in formula.roles}:
        return CalculationProof("CLAIM_CALCULATION_INPUTS_INVALID")
    by_id = {record.get("evidence_id"): record for record in records}
    selected = []
    for role, concept in formula.roles:
        evidence_id = bindings[role]
        if not isinstance(evidence_id, str) or evidence_id not in claim.get("evidence_ids", ()):
            return CalculationProof("CLAIM_CALCULATION_INPUT_REFERENCE_INVALID")
        record = by_id.get(evidence_id)
        if (not isinstance(record, dict) or record.get("kind") != "financial_fact"
                or record.get("concept") != concept
                or record.get("usage_status") in {"rejected", "quarantined", "unverified"}):
            return CalculationProof("CLAIM_CALCULATION_INPUT_UNVERIFIED")
        selected.append(record)
    first = selected[0]
    # Equality alone is insufficient when an identity was omitted on both
    # sides: issuer, scope and currency must be known canonical identities.
    for key in ("company_cik", "scope"):
        if not first.get(key) or any(record.get(key) != first[key] for record in selected):
            return CalculationProof("CLAIM_CALCULATION_IDENTITY_CONFLICT")
    if str(first["scope"]).casefold() in {"unknown", "ambiguous", "unspecified"}:
        return CalculationProof("CLAIM_CALCULATION_IDENTITY_CONFLICT")
    for record in selected:
        try:
            date.fromisoformat(record["end_date"])
        except (KeyError, TypeError, ValueError):
            return CalculationProof("CLAIM_CALCULATION_PERIOD_CONFLICT")
    currency = str(first.get("currency") or first.get("unit") or "")
    if len(currency) != 3 or not currency.isupper() or any(
            str(record.get("currency") or record.get("unit") or "") != currency for record in selected):
        return CalculationProof("CLAIM_CALCULATION_UNIT_CONFLICT")
    for key in ("entity", "market", "dimensions", "source_generation"):
        if any(record.get(key) != first.get(key) for record in selected):
            return CalculationProof("CLAIM_CALCULATION_IDENTITY_CONFLICT")
    if (not _prior_year(*selected) if formula.operation == "growth"
            else not all(_same_period(first, record) for record in selected)):
        return CalculationProof("CLAIM_CALCULATION_PERIOD_CONFLICT")
    for key in ("company_cik", "entity", "market", "scope", "dimensions", "start_date", "end_date", "fiscal_year", "fiscal_period"):
        if claim.get(key) not in (None, "") and claim[key] != first.get(key):
            return CalculationProof("CLAIM_CALCULATION_IDENTITY_CONFLICT" if key not in {
                "start_date", "end_date", "fiscal_year", "fiscal_period"} else "CLAIM_CALCULATION_PERIOD_CONFLICT")
    unit = currency if formula.unit == "currency" else "ratio"
    if claim.get("unit") != unit or claim.get("currency") not in (None, "", currency if formula.unit == "currency" else "ratio"):
        return CalculationProof("CLAIM_CALCULATION_UNIT_CONFLICT")
    if claim.get("unit_scale") not in (None, 1, 1.0):
        return CalculationProof("CLAIM_CALCULATION_UNIT_CONFLICT")
    try:
        left, right = (_number(record["value"]) for record in selected)
        with localcontext() as context:
            context.prec = 50
            if formula.operation in {"ratio", "growth"} and (right <= 0 or formula.operation == "growth" and left < 0):
                return CalculationProof("CLAIM_CALCULATION_DOMAIN_INVALID")
            if formula.operation == "subtract" and right < 0:
                return CalculationProof("CLAIM_CALCULATION_DOMAIN_INVALID")
            result = exact_decimal_arithmetic((left, right), "subtract") if formula.operation == "subtract" else left / right
            if formula.operation == "growth":
                result -= 1
            # The financial engine's public JSON scalar is binary64. Compare
            # its exact serialization too, not a global relative tolerance;
            # otherwise repeating ratios could never be expressed by JSON.
            canonical_result = _number(float(result))
            if _number(claim.get("value")) not in (result, canonical_result):
                return CalculationProof("CLAIM_CALCULATION_VALUE_CONFLICT")
    except (KeyError, InvalidOperation, ValueError, TypeError, ArithmeticError):
        return CalculationProof("CLAIM_CALCULATION_VALUE_INVALID")
    return CalculationProof(record={**first, "concept": formula_name, "unit": unit,
        "currency": unit, "unit_scale": 1, "value": str(canonical_result), "raw_text": "",
        "evidence_id": "calculation:" + formula_name, "input_evidence_ids": tuple(bindings.values())})
