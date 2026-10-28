"""Bounded local compilation of frozen source prefixes, never a provider cache."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading

TEMPLATE_VERSION = "frozen-source-prefix-322-v2"
_MAX_BYTES = 16 * 1024 * 1024
_MAX_ENTRIES = 16
_lock = threading.RLock()
_entries: OrderedDict[str, tuple[str, int]] = OrderedDict()
_bytes = 0


def compact_record_table(records):
    """Lossless columns/data presentation, not a summary or source filter.

    Equal key sets have explicit column headers; equal cells are inherited
    from a shared row header. Original order and absent-vs-null are retained.
    Only use the representation when its complete JSON is actually smaller.
    """
    if not isinstance(records, list) or len(records) < 8 or not all(isinstance(row, dict) for row in records):
        return records
    grouped = OrderedDict()
    order = []
    for record in records:
        keys = tuple(sorted(record))
        # A filing's shared table headers/units/provenance should appear once
        # for that filing, not fail to deduplicate just because another filing
        # has a different document hash. This never merges document identities.
        document = record.get("document_id") or record.get("accession_number") or ""
        boundary = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        group = grouped.setdefault((keys, boundary), {"index": len(grouped), "records": []})
        order.append([group["index"], len(group["records"])])
        group["records"].append(record)
    groups = []
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for (keys, _document), group in grouped.items():
        rows = group["records"]
        shared = {key: rows[0][key] for key in keys if all(encode(row[key]) == encode(rows[0][key]) for row in rows[1:])}
        columns = [key for key in keys if key not in shared]
        groups.append({"shared": shared, "columns": columns, "data": [[row[key] for key in columns] for row in rows]})
    table = {"record_table": "columns-data-v1", "groups": groups, "order": order}
    return table if len(encode(table).encode()) < len(encode(records).encode()) * .9 else records


def expand_record_tables(value):
    """Audit/replay inverse; no model output receives trust from this codec."""
    if isinstance(value, dict):
        if value.get("record_table") == "columns-data-v1" and set(value) == {"record_table", "groups", "order"}:
            groups = [[{**group["shared"], **dict(zip(group["columns"], row, strict=True))} for row in group["data"]]
                      for group in value["groups"]]
            return [expand_record_tables(groups[group][index]) for group, index in value["order"]]
        return {key: expand_record_tables(child) for key, child in value.items()}
    if isinstance(value, list):
        return [expand_record_tables(child) for child in value]
    return value


def compact_record_tree(value):
    """Apply the same reversible table codec to nested original row arrays.

    Request paths refer to the expanded JSON tree. Table representation never
    changes that tree, nor groups together different document identities.
    """
    if isinstance(value, dict):
        if value.get("record_table") == "columns-data-v1":
            return value
        return {key: compact_record_tree(child) for key, child in value.items()}
    if isinstance(value, list):
        return compact_record_table([compact_record_tree(child) for child in value])
    return value

# Fixed readers share complete packets. This changes dispatch presentation,
# never the host ledger or a validator's access to originals.
_PACKET_READERS = {
    "financial-analyst": ("financial-accounting", "financial-analyst"),
    "accounting-risk-analyst": ("financial-accounting", "financial-analyst"),
    "business-analyst": ("business-growth", "business-analyst"),
    "growth-opportunity-analyst": ("business-growth", "business-analyst"),
    "growth-opportunity-analyst-repair": ("business-growth", "business-analyst"),
}


def packet_dispatch_group(spec):
    role = spec.role or spec.stage_id
    return _PACKET_READERS.get(role, ("independent-review", "research-synthesizer"))[0]


def packet_materials(context, role):
    """A complete named reading assignment, never an unowned Top-K cut.

    Delegation is allowed only when the host's frozen graph contains an
    actual financial reader and admitted facts remain in the common core.
    Custom single-reader workflows and source-only cases retain their full
    numeric source channel. Cited dependency sources are supplied separately.
    """
    from ..research_materials import prompt_materials
    _packet, reader = _PACKET_READERS.get(role, ("independent-review", "research-synthesizer"))
    materials = prompt_materials(context, reader)
    roles = set(context.get("source_reader_roles", ()))
    owner = next((key for key in ("financial-analyst", "accounting-risk-analyst") if key in roles), None)
    if not owner or not materials.get("accepted_facts") or reader in {"financial-analyst", "accounting-risk-analyst"}:
        return materials
    delegated = set()
    for topic, records in materials.get("evidence_partitions", {}).items():
        retained = []
        for record in records:
            identity = str(record.get("evidence_id", ""))
            # These are the compiler's explicit financial proof namespaces
            # (domain.py/financial_statement_identity), not keyword filtering
            # of narrative disclosures. Unknown prose stays with this reader.
            financial_proof = record.get("kind") == "unadmitted_source_row" or (record.get("kind") not in {"filing_text", "disclosure"}
                and identity.startswith(("unit:", "statement:")) and record.get("document_id"))
            if topic == "quantitative_facts" or financial_proof:
                delegated.add(identity)
            else:
                retained.append(record)
        materials["evidence_partitions"][topic] = retained
    if delegated:
        materials["evidence"] = [key for key in materials.get("evidence", ()) if key not in delegated]
        if isinstance(materials.get("source_blocks"), list):
            materials["source_blocks"] = [item for item in materials["source_blocks"] if item.get("evidence_ref") not in delegated]
        materials["reading_packet"]["delegated_sources"].extend({"evidence_id": key, "reader": owner,
            "topic": "quantitative_facts", "scope": "complete_raw_financial_source; admitted_values_remain_in_common_core"} for key in sorted(delegated) if key)
    return materials


def compiled_source_package(context_json: str, role: str, required_evidence_ids=()):
    """Frozen packet plus late source supplements; no paid cache prewarm.

    Role instructions and dependency citations never insert new hashes ahead
    of the original packet. Supplemental records are supplied physically in
    this request, not assumed to exist in a provider's remote context.
    """
    from ..research_materials import prompt_materials, route_materials, intern_request_values
    packet, reader = _PACKET_READERS.get(role, ("independent-review", "research-synthesizer"))
    materials, registry = compiled_role_sources(context_json, reader)
    existing = {record.get("evidence_id") for records in materials.get("evidence_partitions", {}).values()
                for record in records if isinstance(record, dict)}
    required = set(required_evidence_ids) - existing
    supplements = []
    if required:
        complete = prompt_materials(json.loads(context_json), role, required_evidence_ids=required)
        supplements = [record for records in complete.get("evidence_partitions", {}).values()
                       for record in records if isinstance(record, dict)
                       and record.get("evidence_id") in required]
    core = {}
    relocated = {key for key in ("accepted_facts", "financial_summary") if key in materials}
    def rebind(value):
        if isinstance(value, dict):
            if set(value) == {"request_path_ref"}:
                path = value["request_path_ref"]
                if len(path) >= 2 and path[0] == "research_context" and path[1] in relocated:
                    return {"request_path_ref": ["common_core", *path[1:]]}
            return {key: rebind(child) for key, child in value.items()}
        if isinstance(value, list):
            return [rebind(child) for child in value]
        return value
    materials = rebind(materials)
    # All admitted values retain full source identities/units. Replace only
    # byte-identical duplicate objects with the already supported path alias.
    for key in ("company", "accepted_facts", "financial_summary"):
        if key in materials:
            core[key] = materials[key]
            if key in relocated:
                materials[key] = {"request_path_ref": ["common_core", key]}
    adequacy = materials.pop("material_adequacy", None)
    reading = materials.pop("reading_packet", None)
    if role != reader:
        adequacy = route_materials(json.loads(context_json), role).get("material_adequacy")
        if isinstance(reading, dict):
            reading = {**reading, "reader": role}
    if isinstance(reading, dict) and "delegated_sources" in reading:
        reading["delegated_sources"] = compact_record_table(reading["delegated_sources"])
    shared = intern_request_values({"common_core": core, "research_context": materials})
    core, materials = shared["common_core"], shared["research_context"]
    core, materials = compact_record_tree(core), compact_record_tree(materials)
    packet_digest = hashlib.sha256(json.dumps([core, materials, registry], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    # Preserve the legacy packet envelope; these are fixed for this packet's
    # reader, while the current role and dynamic instructions live in tail.
    materials["material_adequacy"] = {"request_path_ref": ["reading_assignment", "material_adequacy"]}
    materials["reading_packet"] = {"request_path_ref": ["reading_assignment", "reading_packet"]}
    return materials, registry, {"packet_id": packet, "content_digest": packet_digest,
        "version": TEMPLATE_VERSION}, core, supplements, {"role": role,
            "material_adequacy": adequacy, "reading_packet": reading}


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
    if required:
        materials = prompt_materials(json.loads(context_json), role, required_evidence_ids=required)
    else:
        materials = packet_materials(json.loads(context_json), role)
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
