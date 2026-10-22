"""I/O-free rules for a returned but unusable structured model response.

This is protocol repair, not research acceptance. It never selects a numeric
value, rewrites an ambiguous JSON object, summarizes materials, or overrides a
provider refusal. A repaired object still goes through the stage's validator.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import re
import math
from typing import Any


_REPAIRABLE_ERRORS = frozenset({"invalid_json", "invalid_shape", "empty_content"})
_REFUSAL_REASONS = frozenset({"refusal", "content_filter", "safety", "blocked", "recitation"})
OUTPUT_REPAIR_VERSION = "model-output-repair-v1"
FIELD_REPAIR_VERSION = "model-field-repair-v1"


def recover_closed_json(text: str) -> tuple[dict[str, Any] | None, tuple[dict[str, str], ...]]:
    """Recover complete values only; never add quotes, digits or defaults.

    Container prefixes may contain closed children. A numeric token at EOF is
    not closed (123 might have been 123000); an unfinished string contributes
    nothing. Duplicate targets are removed rather than choosing a winner.
    This is candidate preservation, not a trust/completeness decision.
    """
    issues: list[dict[str, str]] = []
    if not isinstance(text, str) or len(text.encode("utf-8")) > 16 * 1024 * 1024:
        return None, ({"path": "$", "code": "MODEL_OUTPUT_RESOURCE_LIMIT"},)

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non_finite_json_number")
        return parsed

    def constant(_value):
        raise ValueError("non_finite_json_number")

    decoder = json.JSONDecoder(parse_float=number, parse_constant=constant)

    def skip(position):
        while position < len(text) and text[position].isspace():
            position += 1
        return position

    def value(position, path, depth):
        position = skip(position)
        if depth > 64 or position >= len(text):
            raise ValueError("MODEL_OUTPUT_UNCLOSED_VALUE")
        if text[position] not in "[{":
            parsed, end = decoder.raw_decode(text, position)
            boundary = skip(end)
            if not isinstance(parsed, str) and (boundary >= len(text) or text[boundary] not in ",]}"):
                raise ValueError("MODEL_OUTPUT_UNCLOSED_VALUE")
            return parsed, end, True
        is_object = text[position] == "{"
        result = {} if is_object else []
        seen = set()
        position = skip(position + 1)
        close = "}" if is_object else "]"
        while position < len(text):
            if text[position] == close:
                return result, position + 1, True
            try:
                key = len(result)
                if is_object:
                    key, end = decoder.raw_decode(text, position)
                    if not isinstance(key, str) or text[skip(end):skip(end) + 1] != ":":
                        raise ValueError("MODEL_OUTPUT_INVALID_KEY")
                    position = skip(end) + 1
                child_path = path + ("." + key if is_object else "[" + str(key) + "]")
                child, end, closed = value(position, child_path, depth + 1)
                if is_object:
                    if key in seen:
                        result.pop(key, None)
                        issues.append({"path": child_path, "code": "MODEL_OUTPUT_DUPLICATE_TARGET"})
                    else:
                        seen.add(key)
                        if closed or isinstance(child, (dict, list)) and child:
                            result[key] = child
                elif closed or isinstance(child, (dict, list)) and child:
                    result.append(child)
                position = skip(end)
                if not closed:
                    break
                if position < len(text) and text[position] == close:
                    return result, position + 1, True
                if position >= len(text) or text[position] != ",":
                    break
                position = skip(position + 1)
            except (ValueError, TypeError, RecursionError):
                break
        issues.append({"path": path, "code": "MODEL_OUTPUT_CONTAINER_INCOMPLETE"})
        return result, position, False

    try:
        result, _end, _closed = value(0, "$", 0)
        return (result if isinstance(result, dict) and result else None), tuple(issues)
    except (ValueError, TypeError, RecursionError):
        return None, ({"path": "$", "code": "MODEL_OUTPUT_NO_CLOSED_NODE"},)


def normalize_stage_output(candidate: Any, contract: "ModelOutputContract | None") -> Any:
    """Lossless name/one-wrapper mapping; ambiguous targets remain candidates."""
    if not isinstance(candidate, dict):
        return candidate
    # Some received responses contain a stray punctuation-only JSON key such
    # as ': {' around an otherwise complete domain object. Unwrap only ONE
    # such container, never an unknown named extension or competing siblings.
    # Host transport controls on the outer object cannot be overwritten.
    business = [key for key in candidate if isinstance(key, str) and not key.startswith('_')
                and key not in {'structured_output_valid', 'raw_response'}]
    if len(business) == 1:
        wrapper = business[0]
        inner = candidate[wrapper]
        if (re.fullmatch(r'[\s:{}\[\]"\',;]+', wrapper)
                and isinstance(inner, dict) and any(key in inner for key in
                    {'claims', 'analysis', 'summary', 'financial_quality', 'balance_sheet', 'business_model'})):
            candidate = {**deepcopy(inner), **{key: deepcopy(value) for key, value in candidate.items() if key != wrapper}}
            candidate['_envelope_normalization'] = {'version': 'punctuation-envelope-v1',
                'from': wrapper, 'original_body': deepcopy(inner)}
    if contract is None:
        return candidate
    result = deepcopy(candidate)
    normalize = lambda key: re.sub(r"[^a-z0-9]", "", key.casefold())
    targets = {normalize(field.name): field.name for field in contract.fields}
    values: dict[str, list[tuple[str, Any]]] = {}
    for key, item in candidate.items():
        target = targets.get(normalize(key))
        if target:
            values.setdefault(target, []).append((key, item))
        elif key.casefold() in {"data", "result", "report", "output", "analysis"} and isinstance(item, dict):
            for child_key, child in item.items():
                target = targets.get(normalize(child_key))
                if target:
                    values.setdefault(target, []).append((key + "." + child_key, child))
    changes, issues = [], []
    for target, choices in values.items():
        # Original alias bodies stay auditable, but conflicting alternatives
        # must not leak into the extensible-field publication projection.
        for path, _value in choices:
            if "." not in path and path != target:
                result.pop(path, None)
            elif "." in path:
                wrapper, child = path.split(".", 1)
                result[wrapper].pop(child, None)
        first = choices[0][1]
        if any(value != first for _path, value in choices[1:]):
            result.pop(target, None)
            issues.append({"path": target, "code": "MODEL_OUTPUT_ALIAS_CONFLICT"})
            continue
        result[target] = deepcopy(first)
        changes.extend({"from": path, "to": target} for path, _value in choices if path != target)
    if changes or issues:
        result["_output_normalization"] = {"version": "model-output-normalization-v1",
            "changes": changes, "issues": issues,
            "original_alias_candidates": {target: [{"path": path, "value": deepcopy(value)}
                for path, value in choices] for target, choices in values.items()}}
    return result


@dataclass(frozen=True, slots=True)
class OutputField:
    """A known top-level wire field, not a research-completeness assertion."""

    name: str
    types: tuple[str, ...] = ("string", "object", "array")

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name)
                or self.name in {"structured_output_valid", "raw_response", "prompt"}
                or not isinstance(self.types, tuple) or not self.types
                or any(not isinstance(kind, str) for kind in self.types)
                or len(set(self.types)) != len(self.types)
                or any(kind not in {"string", "object", "array"} for kind in self.types)):
            raise ValueError("MODEL_OUTPUT_FIELD_INVALID")

    def accepts(self, value: Any) -> bool:
        return (("string" in self.types and isinstance(value, str) and bool(value.strip()))
                or ("object" in self.types and isinstance(value, dict))
                or ("array" in self.types and isinstance(value, list)))


@dataclass(frozen=True, slots=True)
class ModelOutputContract:
    """Versioned, immutable shape rules owned by the requesting stage.

    Empty containers can be honest missing-information outputs. They remain
    subject to the independent substantive/coverage validator, not filled here.
    Unknown extension fields are retained unchanged.
    """

    version: str
    fields: tuple[OutputField, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.version, str) or not self.version.strip()
                or not isinstance(self.fields, tuple) or not self.fields
                or any(not isinstance(field, OutputField) for field in self.fields)
                or len({field.name for field in self.fields}) != len(self.fields)):
            raise ValueError("MODEL_OUTPUT_CONTRACT_INVALID")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def invalid_fields(self, candidate: Any) -> tuple[str, ...]:
        if not isinstance(candidate, dict):
            return tuple(field.name for field in self.fields)
        return tuple(field.name for field in self.fields
                     if field.name not in candidate or not field.accepts(candidate[field.name]))


@dataclass(frozen=True, slots=True)
class TargetRepairResult:
    """Detached outcome of applying independently valid frozen targets.

    ``candidate`` is still untrusted model output.  ``progress`` only means
    that one or more local target checks succeeded; the normal stage validator
    remains the authority for business acceptance.
    """

    candidate: dict[str, Any]
    accepted_patches: tuple[str, ...] = ()
    unresolved_targets: tuple[str, ...] = ()
    ignored_extensions: tuple[str, ...] = ()
    issues: tuple[dict[str, str], ...] = ()
    progress: bool = False


def merge_field_targets(original: dict[str, Any], repair: Any,
                        contract: ModelOutputContract,
                        fields: tuple[str, ...]) -> TargetRepairResult:
    """Apply valid top-level targets without making healthy siblings atomic."""
    baseline = deepcopy(original) if isinstance(original, dict) else {}
    targets = tuple(fields) if isinstance(fields, tuple) else ()
    issues: list[dict[str, str]] = []
    unresolved: list[str] = []
    accepted: list[str] = []
    ignored: list[str] = []

    if not isinstance(original, dict) or repairable_fields(original, contract) != targets:
        unresolved.extend(targets)
        issues.append({"path": "/", "code": "MODEL_FIELD_REPAIR_TARGET_STALE"})
        return TargetRepairResult(baseline, (), tuple(unresolved), (), tuple(issues), False)

    if (not isinstance(repair, dict) or repair.get("structured_output_valid") is not True
            or response_refused(repair) or repair.get("_response_error") not in (None, "")):
        unresolved.extend(targets)
        issues.append({"path": "/", "code": "MODEL_FIELD_REPAIR_RESPONSE_INVALID"})
        return TargetRepairResult(baseline, (), tuple(unresolved), (), tuple(issues), False)

    control = {"structured_output_valid", "_response_meta", "_response_error",
               "raw_response", "section_patches"}
    ignored.extend(sorted(str(key) for key in repair if key not in control))
    patches = repair.get("section_patches")
    if not isinstance(patches, dict):
        unresolved.extend(targets)
        issues.append({"path": "section_patches", "code": "MODEL_FIELD_REPAIR_TARGETS_MISSING"})
        return TargetRepairResult(baseline, (), tuple(unresolved), tuple(ignored), tuple(issues), False)

    ignored.extend(sorted(f"section_patches.{key}" for key in patches if key not in targets))
    field_by_name = {field.name: field for field in contract.fields}
    merged = deepcopy(baseline)
    for name in targets:
        if name not in patches:
            unresolved.append(name)
            issues.append({"path": name, "code": "MODEL_FIELD_REPAIR_TARGET_MISSING"})
            continue
        field = field_by_name.get(name)
        if field is None or not field.accepts(patches[name]):
            unresolved.append(name)
            issues.append({"path": name, "code": "MODEL_FIELD_REPAIR_TARGET_INVALID"})
            continue
        merged[name] = deepcopy(patches[name])
        accepted.append(name)
    return TargetRepairResult(
        merged, tuple(accepted), tuple(unresolved), tuple(ignored), tuple(issues), bool(accepted)
    )


def repairable_fields(candidate: Any, contract: ModelOutputContract | None) -> tuple[str, ...]:
    if (contract is None or not isinstance(candidate, dict) or response_refused(candidate)
            or candidate.get("structured_output_valid") is not True
            or candidate.get("_response_error") not in (None, "")):
        return ()
    if isinstance(candidate.get("analysis_refs"), list):
        # Named findings are resolved by the host before the business gate.
        # Missing raw section keys here do not justify a paid regeneration.
        # Invalid IDs remain pending at that gate, never verified by this flag.
        return ()
    meta = candidate.get("_response_meta")
    if isinstance(meta, dict):
        reason = meta.get("finish_reason")
        if isinstance(reason, str) and reason.strip().casefold() in {"length", "max_tokens", "max_output_tokens"}:
            return ()
    return contract.invalid_fields(candidate)


def field_repair_prompt(user_prompt: str, contract: ModelOutputContract,
                        fields: tuple[str, ...]) -> str:
    rules = [asdict(field) for field in contract.fields if field.name in fields]
    from .repair_context import repair_context
    targeted = json.dumps(repair_context(user_prompt, fields), ensure_ascii=False, separators=(",", ":"))
    return targeted + (
        "\n\n[OpenThesis model-field-repair-v1]\n"
        "The previous response omitted or mistyped only the following top-level fields. "
        "For this repair response return ONLY {\"section_patches\": {...}}, with exactly "
        "these field names and their specified JSON types: "
        + json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + ". All original task, source evidence, language and safety requirements still apply. "
        "Do not regenerate other sections, invent unavailable information, add evidence IDs, "
        "or shorten the supplied original materials. A missing fact remains unknown. "
        "This is output-shape correction, not authorization to bypass provider refusal."
    )


def merge_field_repair(original: dict[str, Any], repair: Any,
                       contract: ModelOutputContract, fields: tuple[str, ...]) -> dict[str, Any] | None:
    """Compatibility wrapper: return a candidate only when any target progressed."""
    result = merge_field_targets(original, repair, contract, fields)
    return result.candidate if result.progress else None


def response_refused(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    meta = candidate.get("_response_meta")
    if not isinstance(meta, dict):
        return False
    reason = meta.get("finish_reason")
    return (isinstance(reason, str) and reason.casefold() in _REFUSAL_REASONS
            or bool(meta.get("refusal")))


def repairable_output(candidate: Any) -> bool:
    error = candidate.get("_response_error") if isinstance(candidate, dict) else None
    return (isinstance(candidate, dict) and candidate.get("structured_output_valid") is False
            and isinstance(error, str) and error in _REPAIRABLE_ERRORS
            and not response_refused(candidate))


def repaired_output_valid(candidate: Any) -> bool:
    return (isinstance(candidate, dict) and candidate.get("structured_output_valid") is True
            and candidate.get("_response_error") in (None, "")
            and not response_refused(candidate))


def output_repair_prompt(user_prompt: str) -> str:
    """Keep the original request intact; do not resend untrusted output as data."""
    return user_prompt + (
        "\n\n[OpenThesis model-output-repair-v1]\n"
        "The previous response could not be used as a complete JSON object. "
        "Return one complete JSON object following ALL original task, schema, "
        "language and evidence requirements above. Do not omit requested fields, "
        "source evidence, material risks or analysis to make the output shorter. "
        "Do not invent missing data, references or facts. Use valid JSON syntax, "
        "unique object keys and finite JSON numbers, without markdown fences or "
        "commentary outside the object. This instruction only corrects the output "
        "protocol; it does not override provider safety or refusal rules."
    )
