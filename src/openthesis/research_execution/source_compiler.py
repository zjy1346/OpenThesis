"""Bounded local compilation of frozen source prefixes, never a provider cache."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading

TEMPLATE_VERSION = "frozen-source-prefix-313-v1"
_MAX_BYTES = 16 * 1024 * 1024
_MAX_ENTRIES = 16
_lock = threading.RLock()
_entries: OrderedDict[str, tuple[str, int]] = OrderedDict()
_bytes = 0


def compiled_role_sources(context_json: str, role: str, required_evidence_ids=()):
    from ..research_materials import prompt_materials, canonical_request_materials
    required = tuple(sorted(set(required_evidence_ids)))
    key = hashlib.sha256((TEMPLATE_VERSION + "\0" + role + "\0" + context_json
        + "\0" + json.dumps(required, ensure_ascii=False)).encode()).hexdigest()
    with _lock:
        cached = _entries.get(key)
        if cached is not None:
            _entries.move_to_end(key)
            data = json.loads(cached[0])
            return data[0], data[1]
    materials = prompt_materials(json.loads(context_json), role, required_evidence_ids=required)
    context, _, registry = canonical_request_materials(materials, {})
    encoded = json.dumps([context, registry], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    size = len(encoded.encode())
    if size <= _MAX_BYTES:
        global _bytes
        with _lock:
            previous = _entries.pop(key, None)
            if previous:
                _bytes -= previous[1]
            _entries[key] = (encoded, size)
            _bytes += size
            while _bytes > _MAX_BYTES or len(_entries) > _MAX_ENTRIES:
                _, (_, removed) = _entries.popitem(last=False)
                _bytes -= removed
    return context, registry
