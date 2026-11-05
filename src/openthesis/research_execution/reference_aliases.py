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


def reference_aliases(context_json: str, *, stable=None) -> dict[str, str]:
    """Return sorted request aliases for original frozen evidence and fact IDs.

    The returned names only shorten typed IDs. They do not establish that a
    candidate is a verified fact or authorize use of an unsupplied source.
    """
    from .source_compiler import FrozenSourceContext, read_source_context
    context = read_source_context(context_json)
    if isinstance(context_json, FrozenSourceContext):
        return dict(context_json.once(("reference-aliases", stable), lambda: _aliases(context, stable)))
    return _aliases(context, stable)


def _aliases(context, stable):
    if not isinstance(context, dict):
        return {}
    if stable is None:
        stable = bool(context.get("source_reader_stages"))
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
    if stable:
        # An unrelated new source cannot renumber a fixed reading group's
        # aliases and destroy its prefix. This is an identity codec, not trust.
        from ..research_materials import TOPIC_PARTITION, usable_disclosure
        groups = {name: index for index, name in enumerate(("quantitative_facts", "business_disclosures",
            "mda_disclosures", "risk_disclosures", "audit_disclosures", "other_disclosures"), 1)}
        grouped = {}
        for record in evidence:
            original = record.get("evidence_id") if isinstance(record, dict) else None
            if original not in evidence_ids:
                continue
            topic = str(record.get("topic") or str(record.get("title", "")).split(" · ")[-1])
            bucket = groups[TOPIC_PARTITION.get(topic, "other_disclosures")] if usable_disclosure(record) else 1
            grouped[original] = min(grouped.get(original, bucket), bucket)
        aliases = {}
        for bucket in groups.values():
            for index, original in enumerate(sorted(key for key in evidence_ids if grouped.get(key) == bucket)):
                digits = str(index)
                # Group + length + ordinal is injective, compact and stable
                # when another group changes. Long ordinals use a distinct
                # escape, not an overflowing fixed-width counter.
                suffix = f"{len(digits)}{digits}" if len(digits) < 10 else f"0{len(digits)}0{digits}"
                aliases[f"@E{bucket}{suffix}"] = original
        aliases.update({f"@F{index}": original for index, original in enumerate(sorted(fact_ids))})
        return aliases
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


class RequestReferenceError(ValueError):
    """A local codec failure, never a supplier rejection or a fact verdict."""

    code = "MODEL_SOURCE_REFERENCE_PATH_INVALID"

    def __init__(self, kind, path=(), *, at=()):
        super().__init__(self.code)
        self.kind = kind
        self.path = list(path) if isinstance(path, (list, tuple)) else []
        self.at = list(at)
        self.reference_location = None


class RequestPathResolver:
    """One addressing implementation for bindings, repairs and prior findings.

    Path memo caches completed lookups; active paths and active containers
    identify actual cycles. The graph memo preserves shared acyclic objects.
    The existing reference-hop protection also applies to cached lookups.
    """

    def __init__(self, value):
        from .source_compiler import expand_record_tables
        self.root = expand_record_tables(value)
        self.paths = {}
        self.active_paths = set()
        self.memo = {}
        self.active_nodes = set()

    def _dereference(self, node):
        if isinstance(node, dict) and set(node) == {"request_path_ref"}:
            target, hops = self._locate(node["request_path_ref"])
            if hops + 1 > _MAX_PATH_DEPTH:
                raise RequestReferenceError("reference_hop_limit", node["request_path_ref"])
            return target, hops + 1
        return node, 0

    def _locate(self, path):
        if not isinstance(path, list) or any(type(part) not in (str, int) for part in path):
            raise RequestReferenceError("invalid_path_type", path)
        identity = tuple(path)
        if identity in self.active_paths:
            raise RequestReferenceError("reference_cycle", path)
        if identity in self.paths:
            return self.paths[identity]
        self.active_paths.add(identity)
        try:
            target, hops = self.root, 0
            for index, component in enumerate(path):
                target, used = self._dereference(target)
                hops += used
                if hops > _MAX_PATH_DEPTH:
                    raise RequestReferenceError("reference_hop_limit", path, at=path[:index])
                if isinstance(target, dict):
                    if type(component) is not str or component not in target:
                        raise RequestReferenceError("missing_object_key", path, at=path[:index + 1])
                elif isinstance(target, list):
                    if type(component) is not int or component < 0 or component >= len(target):
                        raise RequestReferenceError("invalid_array_index", path, at=path[:index + 1])
                else:
                    raise RequestReferenceError("non_container", path, at=path[:index])
                target = target[component]
            target, used = self._dereference(target)
            hops += used
            if hops > _MAX_PATH_DEPTH:
                raise RequestReferenceError("reference_hop_limit", path)
            self.paths[identity] = (target, hops)
            return target, hops
        finally:
            self.active_paths.remove(identity)

    def target(self, path):
        return self._walk(self._locate(path)[0], path)

    def _walk(self, node, path=()):
        if not isinstance(node, (dict, list)):
            return node
        marker = id(node)
        if marker in self.active_nodes:
            raise RequestReferenceError("container_reference_cycle", path)
        if marker in self.memo:
            return self.memo[marker]
        self.active_nodes.add(marker)
        try:
            if isinstance(node, dict) and set(node) == {"request_path_ref"}:
                target, _hops = self._dereference(node)
                result = self._walk(target, node["request_path_ref"])
            elif isinstance(node, dict):
                result = {key: self._walk(child, (*path, key)) for key, child in node.items()}
            else:
                result = [self._walk(child, (*path, index)) for index, child in enumerate(node)]
            self.memo[marker] = result
            return result
        except RequestReferenceError as error:
            if error.reference_location is None:
                error.reference_location = list(path)
            raise
        finally:
            self.active_nodes.remove(marker)

    def expand(self):
        return self._walk(self.root)


def _expand_request_paths(value: Any) -> Any:
    """Validate/expand locally without serializing the expanded graph to wire."""
    try:
        return RequestPathResolver(value).expand()
    except RecursionError:
        raise RequestReferenceError("structural_resource_limit") from None


def resolve_request_path(value: Any, path: list) -> Any:
    """Locate a current-request value with the same table/reference semantics."""
    try:
        return RequestPathResolver(value).target(path)
    except RecursionError:
        raise RequestReferenceError("structural_resource_limit", path) from None


def relocate_request_paths(value, relocations):
    """Rewrite the longest moved prefix, including paths outside source lanes.

    Relocations are simultaneous, not repeatedly applied substitutions. Only
    typed path objects change; source text, record values and order do not.
    """
    moves = sorted(relocations, key=lambda pair: len(pair[0]), reverse=True)
    memo, active = {}, set()
    def visit(node):
        if not isinstance(node, (dict, list)):
            return node
        marker = id(node)
        if marker in active:
            raise RequestReferenceError("container_cycle")
        if marker in memo:
            return memo[marker]
        active.add(marker)
        try:
            if isinstance(node, dict) and set(node) == {"request_path_ref"}:
                path = node["request_path_ref"]
                if not isinstance(path, list) or any(type(part) not in (str, int) for part in path):
                    raise RequestReferenceError("invalid_path_type", path)
                replacement = path
                for old, new in moves:
                    if tuple(path[:len(old)]) == tuple(old):
                        replacement = [*new, *path[len(old):]]
                        break
                result = {"request_path_ref": list(replacement)}
            elif isinstance(node, dict):
                result = {key: visit(child) for key, child in node.items()}
            else:
                result = [visit(child) for child in node]
            memo[marker] = result
            return result
        finally:
            active.remove(marker)
    return visit(value)


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

    collected = set()
    def collect(node: Any) -> None:
        if isinstance(node, (dict, list)):
            if id(node) in collected:
                return
            collected.add(id(node))
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
    collected_facts = set()
    def collect_facts(node: Any) -> None:
        if isinstance(node, (dict, list)):
            if id(node) in collected_facts:
                return
            collected_facts.add(id(node))
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
