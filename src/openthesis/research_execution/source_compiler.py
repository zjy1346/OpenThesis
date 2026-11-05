"""Bounded local compilation of frozen source prefixes, never a provider cache."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading

TEMPLATE_VERSION = "frozen-source-prefix-332-v1"
_MAX_BYTES = 16 * 1024 * 1024
_MAX_ENTRIES = 16
_lock = threading.RLock()
_entries: OrderedDict[str, tuple[str, int]] = OrderedDict()
_bytes = 0


class FrozenSourceContext(str):
    """One graph-owned canonical text and its read-only local material view.

    Correction bodies live in the original observation registry (persisted in
    the deterministic financial snapshot). The main planning string contains
    only their content-bound index. Role routing hydrates matching originals
    before building a wire; a local locator never becomes a model citation.
    """

    def __new__(cls, snapshot, *, encoded=None):
        from ..research_source_context import _repair_catalog_item
        observations = snapshot.get("repair_observations", ())
        index = None
        if encoded is None:
            if isinstance(observations, list) and observations and all(isinstance(row, dict) for row in observations):
                index = [{**_repair_catalog_item(row), "repair_record_sha256": hashlib.sha256(json.dumps(
                    row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
                    for row in observations]
            encoded = json.dumps(snapshot if index is None else {**snapshot, "repair_observations": index},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        value = super().__new__(cls, encoded)
        value.snapshot = snapshot
        value.repair_index = index
        value._derived = {}
        value._view_lock = threading.RLock()
        return value

    def __deepcopy__(self, memo):
        return self

    def once(self, key, compute):
        with self._view_lock:
            if key not in self._derived:
                self._derived[key] = compute()
            return self._derived[key]

    @property
    def sha256(self):
        return self.once("canonical-sha256", lambda: hashlib.sha256(self.encode()).hexdigest())

    def legacy_text(self):
        if self.repair_index is None:
            return self
        return json.dumps(self.snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_source_context(context_json):
    """Borrow for reading only; callers changing a tree must copy first."""
    return context_json.snapshot if isinstance(context_json, FrozenSourceContext) else json.loads(context_json)

# One simultaneous relocation map for every incoming typed request path.
# Apply it to the assembled request too: dependencies and late evidence can
# reference material that was interned before it moved to the dynamic tail.
_CORE_FIELDS = ("accepted_facts", "financial_summary")
_STATE_FIELDS = ("valuation", "market_snapshot", "research_as_of", "valuation_gaps",
    "repair_observations", "repair_catalog", "repair_retrieval", "targeted_repair_observations", "issues")
REQUEST_RELOCATIONS = tuple(
    [(('research_context', key), ('common_core', key)) for key in _CORE_FIELDS]
    + [(('research_context', key), ('reading_assignment', 'current_research_state', key)) for key in _STATE_FIELDS]
    + [(('research_context', key), ('reading_assignment', key)) for key in ('material_adequacy', 'reading_packet')]
)


def relocate_source_paths(value):
    from .reference_aliases import relocate_request_paths
    # A sectional repair has its own self-contained layout. Rewrite only
    # moves actually present in this request, never a globally assumed map.
    context = value.get("research_context", {}) if isinstance(value, dict) else {}
    moves = []
    for old, new in REQUEST_RELOCATIONS:
        target = value
        for component in new:
            if not isinstance(target, dict) or component not in target:
                break
            target = target[component]
        else:
            source = context.get(old[1]) if isinstance(context, dict) else None
            if not isinstance(context, dict) or old[1] not in context or (isinstance(source, dict) and set(source) == {"request_path_ref"}
                    and source["request_path_ref"] == list(new)):
                moves.append((old, new))
    return relocate_request_paths(value, moves)


def _source_key(context_json, role, required=(), reading_lane=None):
    if isinstance(context_json, FrozenSourceContext):
        context_json = "sha256:" + context_json.sha256
    return hashlib.sha256((TEMPLATE_VERSION + "\0" + role + "\0" + str(reading_lane) + "\0" + context_json
        + "\0" + json.dumps(tuple(sorted(set(required))), ensure_ascii=False)).encode()).hexdigest()


def source_packet_cache_token(context_json, role, reading_lane=None):
    context = read_source_context(context_json)
    reader = role if context.get("source_reader_stages") else _PACKET_READERS.get(role, ("", "research-synthesizer"))[1]
    if context.get("source_reader_roles") and reader not in context["source_reader_roles"]:
        reader = role
    key = _source_key(context_json, reader, reading_lane=reading_lane)
    with _lock:
        current = _entries.get(key)
        return (key, hashlib.sha256(current[0].encode()).hexdigest()) if current else None


def invalidate_source_packet(token):
    """Evict just a pre-existing failed packet; never clear other companies."""
    if token is None:
        return False
    key, expected = token
    with _lock:
        current = _entries.get(key)
        if current is None or hashlib.sha256(current[0].encode()).hexdigest() != expected:
            return False
        global _bytes
        _bytes -= current[1]
        del _entries[key]
        return True


def source_evidence_closure(context, evidence_ids):
    """Exact linked headers/units/footnotes and same-page attribution closure."""
    originals, pages = {}, {}
    for row in context.get("evidence", ()):
        if not isinstance(row, dict) or not row.get("evidence_id"):
            continue
        originals.setdefault(row["evidence_id"], []).append(row)
        page = row.get("source_page", row.get("page"))
        if row.get("document_id") and page is not None:
            pages.setdefault((row["document_id"], page), set()).add(row["evidence_id"])
    closure, pending = set(evidence_ids), list(evidence_ids)
    while pending:
        for row in originals.get(pending.pop(), ()):
            linked = set(pages.get((row.get("document_id"), row.get("source_page", row.get("page"))), ()))
            for field in ("classification_evidence_ids", "unit_evidence_ids", "header_evidence_ids",
                          "footnote_evidence_ids", "continuation_evidence_ids", "related_evidence_ids"):
                if isinstance(row.get(field), list):
                    linked.update(identity for identity in row[field] if isinstance(identity, str))
            for identity in linked - closure:
                closure.add(identity)
                pending.append(identity)
    return closure


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
    from .reference_aliases import RequestReferenceError
    memo, active, generated = {}, set(), []
    def visit(node, path=()):
        if not isinstance(node, (dict, list)):
            return node
        marker = id(node)
        if marker in active:
            raise RequestReferenceError("container_cycle", path)
        if marker in memo:
            return memo[marker]
        active.add(marker)
        try:
            if isinstance(node, dict) and node.get("record_table") == "columns-data-v1" and set(node) == {"record_table", "groups", "order"}:
                if not isinstance(node["groups"], list) or not isinstance(node["order"], list):
                    raise RequestReferenceError("invalid_record_table", path)
                groups = []
                for group in node["groups"]:
                    if (not isinstance(group, dict) or not isinstance(group.get("shared"), dict)
                            or not isinstance(group.get("columns"), list) or not isinstance(group.get("data"), list)
                            or any(type(key) is not str for key in group["columns"])
                            or len(set(group["columns"])) != len(group["columns"])
                            or set(group["columns"]) & set(group["shared"])):
                        raise RequestReferenceError("invalid_record_table", path)
                    records = []
                    for row in group["data"]:
                        if not isinstance(row, list) or len(row) != len(group["columns"]):
                            raise RequestReferenceError("invalid_record_table", path)
                        record = {**group["shared"], **dict(zip(group["columns"], row, strict=True))}
                        # Keep temporary row identities alive for the graph memo.
                        generated.append(record)
                        records.append(record)
                    groups.append(records)
                result = []
                for position, pair in enumerate(node["order"]):
                    if (not isinstance(pair, list) or len(pair) != 2 or any(type(index) is not int or index < 0 for index in pair)
                            or pair[0] >= len(groups) or pair[1] >= len(groups[pair[0]])):
                        raise RequestReferenceError("invalid_record_table_index", path)
                    result.append(visit(groups[pair[0]][pair[1]], (*path, position)))
            elif isinstance(node, dict):
                result = {key: visit(child, (*path, key)) for key, child in node.items()}
            else:
                result = [visit(child, (*path, index)) for index, child in enumerate(node)]
            memo[marker] = result
            return result
        finally:
            active.remove(marker)
    return visit(value)


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


def packet_materials(context, role, *, reading_lane=None):
    """A complete named reading assignment, never an unowned Top-K cut.

    Delegation is allowed only when the host's frozen graph contains an
    actual financial reader and admitted facts remain in the common core.
    Custom single-reader workflows and source-only cases retain their full
    numeric source channel. Cited dependency sources are supplied separately.
    """
    from ..research_materials import prompt_materials
    if context.get("source_reader_stages"):
        from ..research_materials import route_materials
        from .reading_view import assigned_reading_view
        # First readers receive their complete assigned originals. Subsequent
        # reviewers receive dependency-cited originals as a physical tail below.
        lane = reading_lane or (role if role in {"financial-analyst", "business-analyst", "accounting-risk-analyst"}
                                else "dependency-review")
        routed = assigned_reading_view(route_materials(context, role), role, lane=lane,
            readers=context.get("source_reader_roles", ()), stages=context["source_reader_stages"])
        # Reuse the established source/fact presentation, not another engine.
        return prompt_materials(context, role, routed_override=routed)
    _packet, reader = _PACKET_READERS.get(role, ("independent-review", "research-synthesizer"))
    if context.get("source_reader_roles") and reader not in context["source_reader_roles"]:
        reader = role
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


def frozen_packet_materials(context_json, role, *, reading_lane=None):
    """The planner and compiler share the same role projection."""
    def build():
        return packet_materials(read_source_context(context_json), role, reading_lane=reading_lane)
    if isinstance(context_json, FrozenSourceContext):
        return context_json.once(("role-source-materials", role, reading_lane), build)
    return build()


def compiled_source_package(context_json: str, role: str, required_evidence_ids=(), *, reading_lane=None):
    """Frozen packet plus late source supplements; no paid cache prewarm.

    Role instructions and dependency citations never insert new hashes ahead
    of the original packet. Supplemental records are supplied physically in
    this request, not assumed to exist in a provider's remote context.
    """
    from ..research_materials import prompt_materials, route_materials, intern_request_values
    context = read_source_context(context_json)
    assigned = bool(context.get("source_reader_stages"))
    if assigned:
        reader = role
        packet = reading_lane or (role if role in {"financial-analyst", "business-analyst", "accounting-risk-analyst"} else "dependency-review")
    else:
        packet, reader = _PACKET_READERS.get(role, ("independent-review", "research-synthesizer"))
        if context.get("source_reader_roles") and reader not in context["source_reader_roles"]:
            reader = role
    materials, registry = compiled_role_sources(context_json, reader, reading_lane=reading_lane)
    existing = {record.get("evidence_id") for records in materials.get("evidence_partitions", {}).values()
                for record in records if isinstance(record, dict)}
    required = set(required_evidence_ids) - existing
    if assigned:
        # Counterevidence is a mandatory dependency, not a popularity filter.
        # Include complete risk/audit originals even if a producer failed to
        # cite them; same-page qualifiers are bound below, not guessed.
        if packet == "dependency-review":
            routed = route_materials(context, role)
            required.update(row.get("evidence_id") for topic in ("risk_disclosures", "audit_disclosures")
                            for row in routed["evidence_partitions"].get(topic, ()) if row.get("evidence_id"))
        required = source_evidence_closure(context, required)
        required.difference_update(existing)
    supplements = []
    if required:
        complete = prompt_materials(context, role, required_evidence_ids=required)
        supplements = [record for records in complete.get("evidence_partitions", {}).values()
                       for record in records if isinstance(record, dict)
                       and record.get("evidence_id") in required]
    core = {}
    relocated = {key for key in _CORE_FIELDS if key in materials}
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
        adequacy = route_materials(context, role).get("material_adequacy")
        if isinstance(reading, dict):
            reading = {**reading, "reader": role}
    if assigned and isinstance(reading, dict):
        original_ids = {row.get("evidence_id") for row in context.get("evidence", ())}
        reading["unresolved_required_evidence_ids"] = sorted(set(required_evidence_ids) - original_ids)
        # Full responsibility is persisted by the host, not repeated in every
        # unrelated reader's prompt. Adding an audit source must not force the
        # business reader to regenerate identical work.
        reading["delegated_sources"] = {"host_manifest": "frozen-source-reading-manifest-v2",
            "policy": "Other originals have actual readers. Request concrete evidence_ids and fields via material_requests when this question needs more original evidence."}
        if isinstance(adequacy, dict):
            adequacy["counts"] = {key: len(rows) for key, rows in materials.get("evidence_partitions", {}).items()
                                  if isinstance(rows, list) and rows}
    # Prices/as-of state and targeted repair work are required NOW, but are
    # not part of a frozen original. Keep their complete values in the tail;
    # fixed paths preserve the established source-context interface.
    current_state = {}
    for key in _STATE_FIELDS:
        if key in materials:
            current_state[key] = materials[key]
            materials[key] = {"request_path_ref": ["reading_assignment", "current_research_state", key]}
    if isinstance(reading, dict) and "delegated_sources" in reading:
        reading["delegated_sources"] = compact_record_table(reading["delegated_sources"])
    moved = relocate_source_paths({"common_core": core, "research_context": materials,
        "late_evidence": supplements, "reading_assignment": {"role": role,
            "material_adequacy": adequacy, "reading_packet": reading, "current_research_state": current_state}})
    supplements, assignment = moved["late_evidence"], moved["reading_assignment"]
    shared = intern_request_values({"common_core": moved["common_core"], "research_context": moved["research_context"]})
    core, materials = shared["common_core"], shared["research_context"]
    core, materials = compact_record_tree(core), compact_record_tree(materials)
    packet_digest = hashlib.sha256(json.dumps([core, materials, registry], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    # Preserve the legacy packet envelope; these are fixed for this packet's
    # reader, while the current role and dynamic instructions live in tail.
    materials["material_adequacy"] = {"request_path_ref": ["reading_assignment", "material_adequacy"]}
    materials["reading_packet"] = {"request_path_ref": ["reading_assignment", "reading_packet"]}
    return materials, registry, {"packet_id": packet, "content_digest": packet_digest,
        "version": TEMPLATE_VERSION}, core, supplements, assignment


def compiled_role_sources(context_json: str, role: str, required_evidence_ids=(), *, reading_lane=None):
    from ..research_materials import prompt_materials, canonical_request_materials
    required = tuple(sorted(set(required_evidence_ids)))
    key = _source_key(context_json, role, required, reading_lane)
    with _lock:
        cached = _entries.get(key)
        if cached is not None:
            _entries.move_to_end(key)
            data = json.loads(cached[0])
            return data[0], data[1]
    if required:
        materials = prompt_materials(read_source_context(context_json), role, required_evidence_ids=required)
    else:
        materials = frozen_packet_materials(context_json, role, reading_lane=reading_lane)
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
