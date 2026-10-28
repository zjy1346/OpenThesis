"""Request-local short names for source references; aliases never attest facts.

The codec only changes typed identifier fields in a JSON request. It does not
change source text, financial values, verification state, or the host's trust
boundary. An alias is a transport name, not evidence or financial admission.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import hashlib
from collections.abc import Mapping
from typing import Any


_EVIDENCE_FIELDS = frozenset({
    "evidence_id", "evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids",
    "source_evidence_ids", "input_evidence_ids", "classification_evidence_ids",
    "evidence_ref", "required_evidence_ids", "source_id", "source_ids", "analysis_source_id",
})
_FACT_FIELDS = frozenset({"fact_id", "fact_ids", "input_fact_ids", "accepted_fact_ref"})
_REFERENCE_FIELDS = _EVIDENCE_FIELDS | _FACT_FIELDS
_OPAQUE_FIELDS = frozenset({"text", "raw_text", "excerpt", "quote", "body", "analysis", "summary",
                            "expression", "formula", "source_text", "source_excerpt"})
_ALIAS_PATTERN = re.compile(r"^@([EF])(\d+)$")
_MAX_PATH_DEPTH = 32
_HOST_BINDINGS = "host_source_references"


def bind_response_record(record, receipt, bindings, input_digest):
    """Bind host aliases to an already safety-normalized, received record.

    Never wrap raw provider content ahead of the response safety Adapter.
    This control receipt is not model content and is never source evidence.
    """
    from .responses import ModelResponse, restore_response, response_record
    if not bindings and _HOST_BINDINGS not in receipt:
        return record, receipt
    safe = deepcopy(receipt)
    safe.pop(_HOST_BINDINGS, None)
    if bindings:
        pairs = sorted(bindings)
        encoded = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
        safe[_HOST_BINDINGS] = {"schema": "typed-source-bindings-v1", "bindings": pairs,
            "bindings_digest": hashlib.sha256(encoded.encode()).hexdigest(), "request_input_digest": input_digest}
    response = restore_response(record)
    return response_record(ModelResponse(response, safe,
        response.diagnostics if isinstance(response, ModelResponse) else []))


def restore_saved_references(response, input_digest):
    """Restore exact host-owned bindings at EVERY durable candidate read seam."""
    from .responses import ModelResponse
    if not isinstance(response, ModelResponse) or _HOST_BINDINGS not in response.receipt:
        return response
    control = response.receipt[_HOST_BINDINGS]
    if (not isinstance(control, dict) or set(control) != {"schema", "bindings", "bindings_digest", "request_input_digest"}
            or control.get("schema") != "typed-source-bindings-v1"
            or control.get("request_input_digest") != input_digest):
        raise ValueError("MODEL_SAVED_SOURCE_BINDINGS_INVALID")
    pairs = control.get("bindings")
    if (not isinstance(pairs, list) or any(not isinstance(pair, (list, tuple)) or len(pair) != 2
            or not isinstance(pair[0], str) or not _ALIAS_PATTERN.fullmatch(pair[0])
            or not isinstance(pair[1], str) or not pair[1] for pair in pairs)
            or len({pair[0] for pair in pairs}) != len(pairs)):
        raise ValueError("MODEL_SAVED_SOURCE_BINDINGS_INVALID")
    encoded = json.dumps(sorted(pairs), ensure_ascii=False, separators=(",", ":"))
    if control["bindings_digest"] != hashlib.sha256(encoded.encode()).hexdigest():
        raise ValueError("MODEL_SAVED_SOURCE_BINDINGS_INVALID")
    return ModelResponse(decode_references(response, dict(pairs)), response.receipt, response.diagnostics)


def reference_aliases(context_json: str) -> dict[str, str]:
    """Return sorted request aliases for original frozen evidence and fact IDs.

    The returned names only shorten typed IDs. They do not establish that a
    candidate is a verified fact or authorize use of an unsupplied source.
    """
    context = json.loads(context_json)
    if not isinstance(context, dict):
        return {}
    evidence = context.get("evidence")
    facts = context.get("facts")
    if not isinstance(evidence, list):
        evidence = []
    if not isinstance(facts, list):
        facts = []
    evidence_ids = {
        record["evidence_id"]
        for record in evidence
        if isinstance(record, dict) and isinstance(record.get("evidence_id"), str) and record["evidence_id"]
    }
    fact_ids = {
        fact["fact_id"]
        for fact in facts
        if isinstance(fact, dict) and isinstance(fact.get("fact_id"), str) and fact["fact_id"]
    }
    aliases = {f"@E{index}": original for index, original in enumerate(sorted(evidence_ids))}
    aliases.update({f"@F{index}": original for index, original in enumerate(sorted(fact_ids))})
    return aliases


def _field_alias_maps(bindings: Mapping[str, str], *, decoding: bool) -> tuple[dict[str, str], dict[str, str]]:
    evidence: dict[str, str] = {}
    facts: dict[str, str] = {}
    for alias, original in sorted(bindings.items()):
        if (not isinstance(alias, str) or not isinstance(original, str) or not original
                or not _ALIAS_PATTERN.fullmatch(alias)):
            continue
        namespace = alias[1]
        target = evidence if namespace == "E" else facts
        target[alias if decoding else original] = original if decoding else alias
    return evidence, facts


def _translate_scalar(value: Any, field: str, evidence: Mapping[str, str], facts: Mapping[str, str], *, decoding: bool) -> Any:
    if not isinstance(value, str):
        return deepcopy(value)
    if field in _EVIDENCE_FIELDS:
        aliases = evidence
    elif field in _FACT_FIELDS:
        aliases = facts
    else:
        return value
    translated = aliases.get(value, value)
    return translated


def _translate_field(value: Any, field: str, evidence: Mapping[str, str], facts: Mapping[str, str], *, decoding: bool) -> Any:
    if isinstance(value, list):
        return [(_translate_scalar(item, field, evidence, facts, decoding=decoding)
                 if isinstance(item, str) else deepcopy(item))
                for item in value]
    if isinstance(value, str):
        return _translate_scalar(value, field, evidence, facts, decoding=decoding)
    return deepcopy(value)


def _transform_table(value: dict[str, Any], evidence: Mapping[str, str], facts: Mapping[str, str], *, decoding: bool) -> dict[str, Any]:
    result = deepcopy(value)
    groups = value.get("groups")
    if not isinstance(groups, list):
        return result
    transformed_groups = []
    for group in groups:
        if not isinstance(group, dict):
            transformed_groups.append(deepcopy(group))
            continue
        updated = deepcopy(group)
        shared = group.get("shared")
        if isinstance(shared, dict):
            updated["shared"] = _transform(shared, evidence, facts, decoding=decoding)
        columns, rows = group.get("columns"), group.get("data")
        if isinstance(columns, list) and isinstance(rows, list):
            transformed_rows = []
            for row in rows:
                if not isinstance(row, list):
                    transformed_rows.append(deepcopy(row))
                    continue
                cells = []
                for index, cell in enumerate(row):
                    field = columns[index] if index < len(columns) and isinstance(columns[index], str) else ""
                    if field in _REFERENCE_FIELDS:
                        cells.append(_translate_field(cell, field, evidence, facts, decoding=decoding))
                    elif field in _OPAQUE_FIELDS:
                        cells.append(deepcopy(cell))
                    else:
                        cells.append(_transform(cell, evidence, facts, decoding=decoding))
                transformed_rows.append(cells)
            updated["data"] = transformed_rows
        transformed_groups.append(updated)
    result["groups"] = transformed_groups
    # Column names and row order encode table structure, never source IDs.
    if "columns" in value:
        result["columns"] = deepcopy(value["columns"])
    if "order" in value:
        result["order"] = deepcopy(value["order"])
    return result


def _transform(value: Any, evidence: Mapping[str, str], facts: Mapping[str, str], *, decoding: bool) -> Any:
    if isinstance(value, dict):
        if value.get("record_table") == "columns-data-v1" and {"groups", "order"} <= set(value):
            return _transform_table(value, evidence, facts, decoding=decoding)
        if len(value) == 1 and next(iter(value), None) in {"request_path_ref", "material_ref", "finding_ref"}:
            return deepcopy(value)
        result = {}
        for key, child in value.items():
            if key == "evidence" and isinstance(child, list) and all(isinstance(item, str) for item in child):
                # prompt_materials' documented citation index, not the
                # original context's array of complete evidence objects.
                result[key] = _translate_field(child, "evidence_ids", evidence, facts, decoding=decoding)
            elif key in _REFERENCE_FIELDS:
                result[key] = _translate_field(child, key, evidence, facts, decoding=decoding)
            elif key in _OPAQUE_FIELDS:
                result[key] = deepcopy(child)
            else:
                result[key] = _transform(child, evidence, facts, decoding=decoding)
        return result
    if isinstance(value, list):
        return [_transform(child, evidence, facts, decoding=decoding) for child in value]
    return deepcopy(value)


def encode_references(value: Any, bindings: Mapping[str, str]) -> Any:
    """Deep-copy JSON and alias only known values in typed reference fields.

    Long source strings and other content are opaque. Aliasing does not prove
    source availability, verification, or permission to use a reference.
    """
    evidence, facts = _field_alias_maps(bindings, decoding=False)
    return _transform(value, evidence, facts, decoding=False)


def decode_references(value: Any, bindings: Mapping[str, str]) -> Any:
    """Restore known typed aliases; unknown names remain for normal rejection.

    Decoding restores identifiers only and does not grant evidence or fact
    trust. Callers must separately validate against current-request sources.
    """
    evidence, facts = _field_alias_maps(bindings, decoding=True)
    return _transform(value, evidence, facts, decoding=True)


def _expand_request_paths(value: Any) -> Any:
    """Expand same-request path aliases or reject cycles/unresolvable paths."""
    from .source_compiler import expand_record_tables

    root = expand_record_tables(value)

    def resolve_path(path: Any) -> Any:
        if not isinstance(path, list):
            raise ValueError("MODEL_SOURCE_REFERENCE_PATH_INVALID")
        target = root
        try:
            for component in path:
                target = target[component]
        except (IndexError, KeyError, TypeError):
            raise ValueError("MODEL_SOURCE_REFERENCE_PATH_INVALID") from None
        return target

    def walk(node: Any, path_stack: tuple[tuple[Any, ...], ...] = ()) -> Any:
        if isinstance(node, dict):
            if set(node) == {"request_path_ref"}:
                path = node["request_path_ref"]
                if (not isinstance(path, list)
                        or any(not isinstance(component, (str, int)) or isinstance(component, bool)
                               for component in path)):
                    raise ValueError("MODEL_SOURCE_REFERENCE_PATH_INVALID")
                identity = tuple(path)
                if identity in path_stack or len(path_stack) >= _MAX_PATH_DEPTH:
                    raise ValueError("MODEL_SOURCE_REFERENCE_PATH_INVALID")
                return walk(resolve_path(path), (*path_stack, identity))
            return {key: walk(child, path_stack) for key, child in node.items()}
        if isinstance(node, list):
            return [walk(child, path_stack) for child in node]
        return deepcopy(node)

    return walk(root)


def _physical_body(value: Any, material_registry: Mapping[str, Any]) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict) and set(value) == {"material_ref"}:
        reference = value.get("material_ref")
        entry = material_registry.get(reference) if isinstance(reference, str) else None
        return isinstance(entry, dict) and isinstance(entry.get("text"), str) and bool(entry["text"].strip())
    return False


def supplied_bindings(encoded_prompt: str | dict[str, Any], all_bindings: Mapping[str, str]) -> dict[str, str]:
    """Select aliases backed by complete source/fact data in this request only.

    A source ID needs a physical evidence record with source text or a
    same-request reference to a complete accepted fact. A fact ID needs its
    own non-null value and concept. Reader assignments, IDs in claims, and
    aliases alone grant nothing; this function does not change trust status.
    """
    if isinstance(encoded_prompt, str):
        root = json.loads(encoded_prompt)
    elif isinstance(encoded_prompt, dict):
        root = deepcopy(encoded_prompt)
    else:
        raise ValueError("MODEL_SOURCE_REFERENCE_PROMPT_INVALID")
    if not isinstance(root, dict):
        raise ValueError("MODEL_SOURCE_REFERENCE_PROMPT_INVALID")
    request = _expand_request_paths(root)
    if not isinstance(request, dict):
        raise ValueError("MODEL_SOURCE_REFERENCE_PROMPT_INVALID")

    evidence_bindings, fact_bindings = _field_alias_maps(all_bindings, decoding=True)
    material_registry = request.get("material_registry", {})
    if not isinstance(material_registry, dict):
        material_registry = {}
    complete_facts: set[str] = set()
    complete_evidence: set[str] = set()

    def aliases(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        return [item for item in values if isinstance(item, str)]

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            evidence_ids = aliases(node.get("evidence_id"))
            accepted_refs = aliases(node.get("accepted_fact_ref"))
            has_text = any(_physical_body(node.get(key), material_registry)
                           for key in ("raw_text", "excerpt", "text"))
            has_complete_fact_ref = any(alias in complete_facts for alias in accepted_refs)
            if has_text or has_complete_fact_ref:
                complete_evidence.update(alias for alias in evidence_ids if alias in evidence_bindings)

            for child in node.values():
                collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    # First gather all complete facts so an evidence record can safely refer
    # forward to one elsewhere in this same request.
    def collect_facts(node: Any) -> None:
        if isinstance(node, dict):
            fact_ids = aliases(node.get("fact_id"))
            if (node.get("value") is not None and isinstance(node.get("concept"), str)
                    and bool(node["concept"].strip())):
                complete_facts.update(alias for alias in fact_ids if alias in fact_bindings)
            for child in node.values():
                collect_facts(child)
        elif isinstance(node, list):
            for child in node:
                collect_facts(child)

    # Only host source lanes establish physical availability. A copied prior
    # assertion can contain `evidence_id` and `text` too; that is analysis,
    # not another original source. Never let it authorize its own citation.
    context = request.get("research_context", {})
    core = request.get("common_core", {})
    context = context if isinstance(context, dict) else {}
    core = core if isinstance(core, dict) else {}
    source_lanes = [core.get("accepted_facts"), context.get("accepted_facts"), context.get("facts"),
        context.get("evidence_partitions"), context.get("evidence"), request.get("late_evidence")]
    for lane in source_lanes:
        collect_facts(lane)
    for lane in source_lanes:
        collect(lane)
    supplied = {alias: all_bindings[alias] for alias in sorted(complete_evidence | complete_facts)
                if alias in all_bindings}
    return supplied
