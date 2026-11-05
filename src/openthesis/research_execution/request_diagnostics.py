"""Accounting of final message content; no prompts or guessed usage persisted.

Byte components form a disjoint ledger. Component tokens are proportional
local allocations, NEVER provider usage or independent tokenizer counts.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import re
import threading


_CONTRACT = {"claim_contract", "output_language", "output_language_instruction",
    "source_reference_contract", "record_table_contract", "request_reference_contract",
    "analysis_reference_contract", "research_coverage_contract", "required_claim_shape"}
_GROUPS = {"common_core": "common_facts", "research_context": "originals",
    "material_registry": "originals", "prior_artifacts": "upstream_findings",
    "analysis_registry": "upstream_findings", "late_evidence": "supplements",
    "current_research_state": "supplements", "reading_assignment": "supplements"}


def request_outline(payload):
    """Only top-level container shapes; never source prose or scalar values."""
    return {key: {"type": type(value).__name__,
        "items": len(value) if isinstance(value, (dict, list)) else None}
        for key, value in payload.items() if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key)}


def compilation_diagnostic(error, stage, context_json, prior_artifacts, *, rebuilt=False):
    from .source_compiler import TEMPLATE_VERSION, REQUEST_RELOCATIONS
    from ..research_materials import intern_request_values
    def path(parts):
        return [part if type(part) is int and part >= 0 else part if isinstance(part, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", part) else
                {"component_sha256": hashlib.sha256(json.dumps(part, ensure_ascii=False, default=str).encode()).hexdigest()}
                for part in parts or ()]
    try:
        prior = json.dumps(intern_request_values(prior_artifacts), ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        prior = '"prior_artifacts_not_json_compatible"'
    return {"schema": "request-compilation-diagnostic-v1", "phase": "local_pre_provider",
        "stage_id": stage if isinstance(stage, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", stage) else "unknown",
        "code": "MODEL_REQUEST_COMPILATION_FAILED", "kind": error.kind,
        "path": path(error.path), "at": path(error.at), "reference_location": path(error.reference_location),
        "relocations": [{"from": list(old), "to": list(new)} for old, new in REQUEST_RELOCATIONS],
        "compiler_version": TEMPLATE_VERSION, "source_context_sha256": hashlib.sha256(context_json.encode()).hexdigest(),
        "input_snapshot_sha256": hashlib.sha256(context_json.encode() + b"\x00" + prior.encode()).hexdigest(),
        "request_snapshot_sha256": getattr(error, "request_snapshot_sha256", None),
        "request_outline": getattr(error, "request_outline", {}),
        "local_cache_rebuilt": rebuilt, "provider_called": False, "response_received": False,
        "same_frozen_source": True}


def safe_compilation_diagnostic(value):
    """One source-free whitelist at the checkpoint and public-export seams."""
    if not isinstance(value, dict) or value.get("schema") != "request-compilation-diagnostic-v1":
        return None
    result = {"schema": value["schema"], "phase": "local_pre_provider",
        "code": "MODEL_REQUEST_COMPILATION_FAILED", "provider_called": False, "response_received": False}
    for name in ("stage_id", "kind", "compiler_version"):
        item = value.get(name)
        result[name] = item if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", item) else None
    for name in ("source_context_sha256", "input_snapshot_sha256", "request_snapshot_sha256"):
        item = value.get(name)
        result[name] = item if isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item) else None
    def path(parts):
        if not isinstance(parts, list):
            return []
        return [part for part in parts if type(part) is int and part >= 0 or isinstance(part, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", part) or isinstance(part, dict)
            and set(part) == {"component_sha256"} and isinstance(part["component_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", part["component_sha256"])]
    for name in ("path", "at", "reference_location"):
        result[name] = path(value.get(name))
    relocations = value.get("relocations")
    result["relocations"] = [{"from": path(row.get("from")), "to": path(row.get("to"))}
        for row in relocations if isinstance(row, dict)] if isinstance(relocations, list) else []
    result["request_outline"] = {key: {"type": row.get("type") if row.get("type") in
        {"dict", "list", "str", "int", "float", "bool", "NoneType"} else None,
        "items": row.get("items") if type(row.get("items")) is int and row["items"] >= 0 else None}
        for key, row in value.get("request_outline", {}).items() if isinstance(key, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key) and isinstance(row, dict)} if isinstance(value.get("request_outline"), dict) else {}
    for name in ("local_cache_rebuilt", "same_frozen_source"):
        result[name] = value.get(name) is True
    return result


def message_accounting(system, user, measurement):
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    sizes = {name: 0 for name in ("fixed_contract", "common_facts", "originals",
        "upstream_findings", "supplements", "task_control", "json_envelope")}
    sizes["fixed_contract"] = len(system.encode())
    fields = {}
    try:
        payload = json.loads(user)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        for key, value in payload.items():
            size = len(encode(value))
            fields[key] = size
            sizes["fixed_contract" if key in _CONTRACT else _GROUPS.get(key, "task_control")] += size
        sizes["json_envelope"] = len(user.encode()) - sum(fields.values())
    else:
        sizes["task_control"] = len(user.encode())
    total = len(system.encode()) + len(user.encode())
    return {"schema": "final-message-ledger-v1", "input_bytes": total,
        "component_bytes": sizes, "field_value_bytes": fields,
        "accounted_bytes": sum(sizes.values()), "input_tokens": measurement["tokens"],
        "counting_method": measurement["method"], "tokenizer": measurement["tokenizer"],
        "local_tokenizer_count": measurement["exact"],
        "component_tokens_estimate": {key: round(measurement["tokens"] * size / total, 2)
            if total else 0 for key, size in sizes.items()},
        "component_token_method": "proportional_local_allocation_not_supplier_usage",
        "wire_boundary": "final_system_user_content; provider_JSON_envelope_not_a_token_receipt"}


def capacity_accounting(provider, capability):
    config = getattr(provider, "config", None)
    configured = getattr(config, "context_window_hint", None)
    physical = capability.explicit_max_input_bytes
    declared = any(type(getattr(provider, name, None)) is int and getattr(provider, name) > 0
                   for name in ("context_window_tokens", "max_input_tokens"))
    return {"source": "user_configured_route" if configured else "provider_declared" if declared else "unknown_planning_fallback",
        "route_identity": getattr(config, "public_id", None),
        "context_tokens": capability.max_input_tokens if declared else None,
        "planning_context_tokens": capability.max_input_tokens,
        "physical_input_bytes": physical,
        "current_output_reserve": capability.reserved_output_tokens,
        "independent_repair_reserve_deducted": False,
        "available_input_tokens": capability.available_input_tokens if declared else None,
        "provider_rejection_observed": False}


class PrefixObservations:
    """Bounded, in-memory comparison of actual bytes; no persistent raw text."""
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._bytes = 0

    def observe(self, route, system, user):
        try:
            payload = json.loads(user)
        except (ValueError, TypeError):
            payload = {}
        packet = payload.get("source_package") if isinstance(payload, dict) else None
        group = packet.get("packet_id", "non-research") if isinstance(packet, dict) else "non-research"
        key = (route, group)
        wire = system.encode() + b"\x00" + user.encode()
        field_hashes = [(name, hashlib.sha256(json.dumps(value, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()) for name, value in payload.items()] if isinstance(payload, dict) else []
        with self._lock:
            previous = self._entries.pop(key, None)
            result = {"group": group, "observation": "first_read", "lcp_bytes": None,
                "first_difference_field": None, "not_provider_cache_usage": True}
            if previous:
                old, old_fields = previous
                self._bytes -= len(old)
                limit = min(len(old), len(wire))
                # Compare blocks first; scan only the first unequal block.
                cursor = 0
                while cursor + 4096 <= limit and old[cursor:cursor + 4096] == wire[cursor:cursor + 4096]:
                    cursor += 4096
                while cursor < limit and old[cursor] == wire[cursor]:
                    cursor += 1
                old_map, new_map = dict(old_fields), dict(field_hashes)
                difference = next((name for name, digest in field_hashes if old_map.get(name) != digest),
                    next((name for name in old_map if name not in new_map), None))
                result.update(observation="same_group_followup", lcp_bytes=cursor,
                    first_difference_field="system" if system.encode() != old.split(b"\x00", 1)[0] else difference)
            # Accounting only; never trim a request to fit this local cache.
            if len(wire) <= 16 * 1024 * 1024:
                self._entries[key] = (wire, field_hashes)
                self._bytes += len(wire)
                while len(self._entries) > 8 or self._bytes > 16 * 1024 * 1024:
                    _, (removed, _) = self._entries.popitem(last=False)
                    self._bytes -= len(removed)
            return result
