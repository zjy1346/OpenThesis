"""Conservative, deterministic deduplication for already-built research claims.

This module does not decide whether a claim is true.  Numeric equivalence is
available only when the caller explicitly says the numeric claims have already
passed independent verification.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Iterable


_IGNORED_IDENTITY_FIELDS = frozenset(
    {
        "claim_id",
        "confidence",
        "display_text",
        "section",
        "section_id",
        "section_ids",
    }
)
_NORMALIZED_TEXT_FIELDS = frozenset({"text", "conclusion", "argument"})
_CLAIM_REFERENCE_FIELDS = frozenset({"input_claim_ids", "claim_ids"})
_FACT_KINDS = frozenset({"fact", "calculation"})
_NUMBER_PATTERN = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CURRENCY_PATTERN = r"(?:USD|CNY|HKD|\$|元)"


def _decimal_from_value(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                return None
            number = Decimal(repr(value))
        elif isinstance(value, str):
            number = Decimal(value.strip())
        else:
            return None
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _decimal_fingerprint(number: Decimal) -> tuple[int, tuple[int, ...], int]:
    """Return an exact coefficient/exponent form without Decimal context math."""
    sign, digits, exponent = number.as_tuple()
    digits = tuple(digits)
    if not any(digits):
        # Keep the sign of negative zero; do not silently erase a minus sign.
        return sign, (0,), 0
    digit_list = list(digits)
    while len(digit_list) > 1 and digit_list[-1] == 0:
        digit_list.pop()
        exponent += 1
    return sign, tuple(digit_list), exponent


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        sign, digits, exponent = value.as_tuple()
        return {"$decimal": [sign, list(digits), exponent]}
    if isinstance(value, float):
        if math.isfinite(value):
            return {"$float": repr(value)}
        return {"$float": "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        members = [_canonical_value(item) for item in value]
        members.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        return {"$set": members}
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _normalized_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = " ".join(normalized.split())
    # Only terminal sentence punctuation is formatting. Internal decimal
    # points, commas, signs, and all other numeric characters remain intact.
    normalized = re.sub(r"[.!?;:。！？；：]+$", "", normalized).rstrip()
    return normalized


def _evidence_set(value: Any) -> Any:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return value
    members: dict[str, Any] = {}
    for member in value:
        encoded = _canonical_json(member)
        members.setdefault(encoded, member)
    return {"$evidence_set": [_canonical_value(members[key]) for key in sorted(members)]}


def _semantic_payload(
    claim: dict[str, Any],
    *,
    include_text: bool,
    numeric_value: tuple[int, tuple[int, ...], int] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in claim.items():
        if key in _IGNORED_IDENTITY_FIELDS:
            continue
        if key == "text" and not include_text:
            continue
        if key == "evidence_ids":
            value = _evidence_set(value)
        elif key in _NORMALIZED_TEXT_FIELDS and isinstance(value, str):
            value = _normalized_text(value)
        if key == "value" and numeric_value is not None:
            value = {"$verified_decimal": [numeric_value[0], list(numeric_value[1]), numeric_value[2]]}
        payload[key] = value
    return payload


def _claim_year(claim: dict[str, Any]) -> int | None:
    candidates: list[Any] = [claim.get("fiscal_year"), claim.get("FY"), claim.get("year")]
    period = claim.get("period")
    if isinstance(period, dict):
        candidates.extend((period.get("FY"), period.get("fiscal_year"), period.get("year")))
    years: set[int] = set()
    for candidate in candidates:
        if isinstance(candidate, bool) or candidate in (None, ""):
            continue
        try:
            year = int(candidate)
        except (TypeError, ValueError, OverflowError):
            continue
        years.add(year)
    return next(iter(years)) if len(years) == 1 else None


def _currency_identity(claim: dict[str, Any]) -> str | None:
    currency = claim.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        currency = claim.get("unit")
    if not isinstance(currency, str) or not currency.strip():
        return None
    normalized = unicodedata.normalize("NFKC", currency).strip().upper()
    aliases = {"RMB": "CNY", "CNH": "CNY", "US$": "USD", "HK$": "HKD"}
    return aliases.get(normalized, normalized)


def _currency_token_identity(token: str, claim: dict[str, Any]) -> str:
    token = unicodedata.normalize("NFKC", token).upper()
    aliases = {"元": "CNY"}
    if token == "$":
        return _currency_identity(claim) or "$"
    return aliases.get(token, token)


def _direct_revenue_text_key(text: Any, claim: dict[str, Any], value: Decimal) -> tuple[Any, ...] | None:
    if not isinstance(text, str):
        return None
    if str(claim.get("concept", "")).strip().casefold() != "revenue":
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", text).split()).strip()
    normalized = re.sub(r"[.!?;:。！？；：]+$", "", normalized).rstrip()
    label = r"(?:annual\s+revenue|revenue|sales|营业收入|營業收入)"
    link = r"(?:\s+(?:(?:was|is|of)\s+)?|\s*[:：]\s*|为|為)"
    pattern = re.compile(
        rf"^(?:(?:FY\s*)?(?P<year_prefix>\d{{4}})\s+)?"
        rf"(?P<label>{label}){link}"
        rf"(?P<currency_prefix>{_CURRENCY_PATTERN})?\s*"
        rf"(?P<amount>{_NUMBER_PATTERN})"
        rf"\s*(?P<currency_suffix>{_CURRENCY_PATTERN})?"
        rf"(?:\s+(?:FY\s*)?(?P<year_suffix>\d{{4}}))?$",
        re.IGNORECASE,
    )
    match = pattern.fullmatch(normalized)
    if not match:
        return None
    prefix_currency = match.group("currency_prefix")
    suffix_currency = match.group("currency_suffix")
    if not prefix_currency and not suffix_currency:
        return None
    prefix_identity = _currency_token_identity(prefix_currency, claim) if prefix_currency else None
    suffix_identity = _currency_token_identity(suffix_currency, claim) if suffix_currency else None
    if prefix_identity and suffix_identity and prefix_identity != suffix_identity:
        return None
    text_currency = prefix_identity or suffix_identity
    claim_currency = _currency_identity(claim)
    if claim_currency and text_currency != claim_currency:
        return None

    raw_amount = match.group("amount").replace(",", "")
    try:
        text_amount = Decimal(raw_amount)
    except InvalidOperation:
        return None
    if not text_amount.is_finite() or _decimal_fingerprint(text_amount) != _decimal_fingerprint(value):
        return None

    year_prefix = match.group("year_prefix")
    year_suffix = match.group("year_suffix")
    if year_prefix and year_suffix and year_prefix != year_suffix:
        return None
    year_text = int(year_prefix or year_suffix) if (year_prefix or year_suffix) else None
    semantic_year = _claim_year(claim)
    if year_text is not None and semantic_year is not None and year_text != semantic_year:
        return None
    return text_currency, year_text if year_text is not None else semantic_year, _decimal_fingerprint(text_amount)


def _text_hash(claim: dict[str, Any]) -> str | None:
    text = claim.get("text")
    if not isinstance(text, str):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_reference_only(claim: dict[str, Any]) -> bool:
    for field in ("_verification_state", "verification_state", "claim_verification_state"):
        state = claim.get(field)
        if isinstance(state, str) and state.strip().casefold() in {"reference", "reference_only"}:
            return True
    return False


def _key_for_claim(claim: dict[str, Any], numeric_eligible: bool) -> tuple[str, str | None]:
    try:
        kind = claim.get("kind")
        if (
            numeric_eligible
            and isinstance(kind, str)
            and kind.casefold() in _FACT_KINDS
            and not _is_reference_only(claim)
        ):
            number = _decimal_from_value(claim.get("value"))
            if number is not None:
                numeric_value = _decimal_fingerprint(number)
                semantic = _canonical_json(
                    _semantic_payload(claim, include_text=False, numeric_value=numeric_value)
                )
                direct_text = _direct_revenue_text_key(claim.get("text"), claim, number)
                if direct_text is not None:
                    fingerprint_data = _canonical_json({"semantic": semantic, "revenue": direct_text})
                    fingerprint = hashlib.sha256(fingerprint_data.encode("utf-8")).hexdigest()
                    return json.dumps(["verified-direct-revenue", semantic, direct_text], ensure_ascii=False), fingerprint
                text = claim.get("text")
                if isinstance(text, str):
                    normalized = _normalized_text(text)
                    fingerprint_data = _canonical_json({"semantic": semantic, "text": normalized})
                    fingerprint = hashlib.sha256(fingerprint_data.encode("utf-8")).hexdigest()
                    return json.dumps(["verified-exact-text", semantic, normalized], ensure_ascii=False), fingerprint
        semantic = _canonical_json(_semantic_payload(claim, include_text=True))
        return semantic, None
    except (TypeError, ValueError):
        # Claims outside JSON-compatible domain are conservatively left unique.
        return f"unmergeable:{id(claim)}", None


def _section_values(claim: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for key in ("section_ids", "section_id", "section"):
        if key not in claim:
            continue
        value = claim[key]
        if isinstance(value, (list, tuple)):
            values.extend(value)
        elif isinstance(value, (set, frozenset)):
            values.extend(sorted(value, key=lambda item: _canonical_json(item)))
        elif value is not None:
            values.append(value)
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        try:
            marker = _canonical_json(value)
        except (TypeError, ValueError):
            marker = f"unmergeable:{type(value).__name__}:{id(value)}"
        if marker not in seen:
            seen.add(marker)
            result.append(deepcopy(value))
    return result


def _confidence_value(claim: dict[str, Any]) -> Decimal | None:
    if "confidence" not in claim:
        return None
    value = claim["confidence"]
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and Decimal(0) <= number <= Decimal(1) else None


def _claim_id(claim: dict[str, Any]) -> str | None:
    value = claim.get("claim_id")
    return value if isinstance(value, str) and value.strip() else None


def _flatten_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        flattened: list[str] = []
        for nested in value.values():
            flattened.extend(_flatten_ids(nested))
        return flattened
    if isinstance(value, (list, tuple, set, frozenset)):
        flattened = []
        for nested in value:
            flattened.extend(_flatten_ids(nested))
        return flattened
    return []


def _referenced_claim_ids(value: Any) -> set[str]:
    referenced: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in _CLAIM_REFERENCE_FIELDS:
                referenced.update(_flatten_ids(nested))
            else:
                referenced.update(_referenced_claim_ids(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            referenced.update(_referenced_claim_ids(nested))
    return referenced


def _group_claim_id_aliases(
    group: list[tuple[int, dict[str, Any]]],
    id_indices: dict[str, list[int]],
    referenced_ids: set[str],
) -> tuple[bool, dict[str, str]]:
    """Check whether removed IDs can be safely represented by the survivor."""
    group_indices = {index for index, _ in group}
    kept_id = _claim_id(group[0][1])
    aliases: dict[str, str] = {}
    for index, claim in group[1:]:
        removed_id = _claim_id(claim)
        if removed_id is None:
            continue
        occurrences = id_indices.get(removed_id, [])
        occurrences_stay_in_group = bool(occurrences) and all(
            occurrence in group_indices for occurrence in occurrences
        )
        if removed_id == kept_id:
            if removed_id in referenced_ids and not occurrences_stay_in_group:
                return False, {}
            continue
        kept_id_unambiguous = kept_id is not None and not any(
            occurrence not in group_indices for occurrence in id_indices.get(kept_id, [])
        )
        if occurrences_stay_in_group and kept_id_unambiguous:
            aliases[removed_id] = kept_id
        elif removed_id in referenced_ids:
            # No safe alias can preserve an existing dependency to this ID.
            return False, {}
    return True, aliases


def _merge_group(group: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    merged = deepcopy(group[0][1])
    bindings: list[Any] = []
    seen_bindings: set[str] = set()
    for _, claim in group:
        for binding in _section_values(claim):
            marker = _canonical_json(binding)
            if marker not in seen_bindings:
                seen_bindings.add(marker)
                bindings.append(binding)
    if any(key in claim for _, claim in group for key in ("section", "section_id", "section_ids")):
        merged.pop("section", None)
        merged.pop("section_id", None)
        merged["section_ids"] = bindings

    confidence_values = [_confidence_value(claim) for _, claim in group]
    if any(value is None for value in confidence_values):
        merged["confidence"] = None
    else:
        lowest = min(value for value in confidence_values if value is not None)
        first_minimum = next(
            claim["confidence"]
            for (_, claim), value in zip(group, confidence_values)
            if value == lowest
        )
        merged["confidence"] = deepcopy(first_minimum)
    return merged


def deduplicate_claims(
    claims: Iterable[dict[str, Any]],
    *,
    verified_numeric: bool = False,
    numeric_verified_indices: set[int] | None = None,
    referenced_claim_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return detached claims with only contract-safe duplicates collapsed.

    ``verified_numeric`` is a trust-boundary assertion by the caller, not a
    request for this helper to validate financial facts itself.
    """
    if not isinstance(verified_numeric, bool):
        raise TypeError("verified_numeric must be a boolean")
    items = list(claims)
    if any(not isinstance(claim, dict) for claim in items):
        raise TypeError("claims must contain dictionaries only")
    if referenced_claim_ids is not None:
        if not isinstance(referenced_claim_ids, set):
            raise TypeError("referenced_claim_ids must be a set of strings")
        for claim_id in referenced_claim_ids:
            if not isinstance(claim_id, str):
                raise TypeError("referenced_claim_ids must contain strings only")
            if not claim_id.strip():
                raise ValueError("referenced_claim_ids cannot contain empty IDs")
    if numeric_verified_indices is not None:
        if not verified_numeric:
            raise ValueError("numeric_verified_indices requires verified_numeric=True")
        if not isinstance(numeric_verified_indices, set):
            raise ValueError("numeric_verified_indices must be a set of indices")
        for index in numeric_verified_indices:
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= len(items)
            ):
                raise ValueError("numeric_verified_indices contains an invalid index")
        eligible_indices = numeric_verified_indices
    else:
        eligible_indices = set(range(len(items))) if verified_numeric else set()

    buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    fingerprints: dict[str, str | None] = {}
    id_indices: dict[str, list[int]] = {}
    referenced_ids: set[str] = set()
    if referenced_claim_ids is not None:
        referenced_ids.update(referenced_claim_ids)
    for index, claim in enumerate(items):
        key, fingerprint = _key_for_claim(claim, index in eligible_indices)
        buckets.setdefault(key, []).append((index, claim))
        fingerprints.setdefault(key, fingerprint)
        claim_id = _claim_id(claim)
        if claim_id is not None:
            id_indices.setdefault(claim_id, []).append(index)
        referenced_ids.update(_referenced_claim_ids(claim))

    ordered_groups = sorted(buckets.items(), key=lambda item: item[1][0][0])
    output_by_index: dict[int, dict[str, Any]] = {}
    merges: list[dict[str, Any]] = []
    claim_id_aliases: dict[str, str] = {}
    for key, group in ordered_groups:
        can_merge, aliases = _group_claim_id_aliases(group, id_indices, referenced_ids)
        if len(group) > 1 and can_merge:
            output_by_index[group[0][0]] = _merge_group(group)
            claim_id_aliases.update(aliases)
        else:
            for index, claim in group:
                output_by_index[index] = deepcopy(claim)
        if len(group) <= 1 or not can_merge:
            continue
        indices = [index for index, _ in group]
        merges.append(
            {
                "kept_index": indices[0],
                "removed_indices": indices[1:],
                "indices": indices,
                "text_hashes": [_text_hash(claim) for _, claim in group],
                "numeric_fingerprint": fingerprints.get(key),
            }
        )

    output = [output_by_index[index] for index in range(len(items)) if index in output_by_index]
    audit = {
        "schema_version": "research-claim-dedup-v1",
        "input_count": len(items),
        "output_count": len(output),
        "removed_count": len(items) - len(output),
        "merges": merges,
        "claim_id_aliases": claim_id_aliases,
    }
    return output, audit
