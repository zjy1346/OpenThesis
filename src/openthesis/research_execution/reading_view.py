"""Research reading packets, separate from the full host evidence ledger.

Only explicitly named local parser/bookkeeping metadata is removed. Values,
units, scopes, source bytes, headers, revisions and opposing evidence are not
summarized. Every omitted topical record has a named reader, not a Top-K cut.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


_HOST_AUDIT_FIELDS = frozenset({"normalization_lineage", "generation_id", "parser_version",
    "recognition_backend", "ingestion_backend", "classifier_version", "classification_rule_id",
    "cache_key", "cache_hit", "worker_id", "checkpoint_id", "processing_elapsed_seconds"})
_TOPICS = {
    "business-analyst": {"business_disclosures", "mda_disclosures", "risk_disclosures", "other_disclosures"},
    "accounting-risk-analyst": {"quantitative_facts", "business_disclosures", "mda_disclosures", "audit_disclosures", "risk_disclosures", "other_disclosures"},
    "growth-opportunity-analyst": {"business_disclosures", "mda_disclosures", "risk_disclosures", "other_disclosures"},
}
_READERS = {"quantitative_facts": "financial-analyst", "business_disclosures": "business-analyst",
            "mda_disclosures": "growth-opportunity-analyst", "audit_disclosures": "accounting-risk-analyst",
            "risk_disclosures": "source-risk-analysis", "other_disclosures": "business-analyst/source-risk-analysis"}


def reading_manifest(routed, *, readers=(), stages=()):
    """Frozen source responsibility, not a model's assertion that it has read.

    Unknown/custom graphs keep an actual available reader. Multi-topic sources
    retain every owner and every original attribution; nothing is ranked away.
    """
    available = set(stages or readers)
    fallback = next((name for name in ("business-analyst", "financial-analyst", "accounting-risk-analyst")
                     if name in available), next(iter(sorted(available)), None))
    preferred = {"quantitative_facts": ("financial-analyst", "accounting-risk-analyst"),
        "business_disclosures": ("business-analyst",),
        "mda_disclosures": ("source-industry-analysis", "growth-opportunity-analyst", "business-analyst"),
        "risk_disclosures": ("source-risk-analysis", "skeptical-analyst", "accounting-risk-analyst"),
        "audit_disclosures": ("accounting-risk-analyst", "source-risk-analysis"),
        "other_disclosures": ("business-analyst",)}
    rows = []
    from ..research_materials import TOPIC_PARTITION, usable_disclosure
    for topic, records in routed.get("evidence_partitions", {}).items():
        for record in records:
            if not isinstance(record, dict):
                continue
            identity = record.get("evidence_id")
            if not identity:
                continue
            explicit = str(record.get("topic") or "")
            structured_statement = None
            if str(identity).startswith("table-structure:"):
                try:
                    native = json.loads(record.get("raw_text") or record.get("excerpt") or "")
                    if isinstance(native, dict) and native.get("contract") == "native-table-v1":
                        structured_statement = native.get("statement")
                except (ValueError, TypeError):
                    pass  # Unrecognized originals remain complete, not lost.
            financial = (explicit in {"financial_notes", "notes_to_financial_statements", "financial_statements"}
                or record.get("kind") == "unadmitted_source_row"
                or str(identity).startswith(("unit:", "statement:", "table:", "table-structure:"))
                or not usable_disclosure(record))
            topics = set(record.get("topics", ())) if isinstance(record.get("topics"), list) else set()
            topics = {TOPIC_PARTITION.get(name, name) for name in topics}
            topics.add("quantitative_facts" if financial else topic)
            owners = []
            for name in sorted(topics):
                choices = [owner for owner in preferred.get(name, ()) if owner in available]
                # Audit can be financial and risk evidence; keep both readers.
                owners.extend(choices if name == "audit_disclosures" else choices[:1])
            # Complementary financial readers, never a new fact-admission rule.
            # Finance reads income/cash-flow tables; accounting reads complete
            # balance-sheet tables, notes, and provenance originals. Both keep
            # admitted values. A source tag assigns work, not numeric trust.
            independent_source = str(identity).startswith("filing:") or record.get("kind") == "filing_text"
            auxiliary_financial = (not independent_source and not str(identity).startswith(
                ("fact:", "unit:", "statement:", "table:", "table-structure:"))
                and record.get("kind") != "unadmitted_source_row")
            if "accounting-risk-analyst" in available and (structured_statement == "balance_sheet" or auxiliary_financial):
                owners = ["accounting-risk-analyst"]
            owners = list(dict.fromkeys(owners or ([fallback] if fallback else [])))
            rows.append({"evidence_id": identity, "document_id": record.get("document_id"),
                "period": record.get("end_date") or record.get("period") or record.get("fiscal_year"),
                "page": record.get("source_page", record.get("page")), "topics": sorted(topics),
                "body_sha256": hashlib.sha256(json.dumps(_reading_record(record), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "readers": owners, "state": "assigned" if owners else "unassigned"})
    return {"schema": "frozen-source-reading-manifest-v2", "sources": rows,
        "unassigned_evidence_ids": sorted({row["evidence_id"] for row in rows if not row["readers"]}),
        "completion_policy": "physical_input_and_stage_validation; model_read_claim_is_not_proof"}


def assigned_reading_view(routed, role, *, lane, readers=(), stages=(), required_evidence_ids=()):
    manifest = reading_manifest(routed, readers=readers, stages=stages)
    owned = {row["evidence_id"] for row in manifest["sources"] if lane in row["readers"]}
    retained = owned | set(required_evidence_ids)
    result = deepcopy(routed)
    result["evidence_partitions"] = {topic: [_reading_record(row) for row in rows
        if isinstance(row, dict) and row.get("evidence_id") in retained]
        for topic, rows in routed.get("evidence_partitions", {}).items()}
    result["evidence"] = [_reading_record(row) for row in routed.get("evidence", ())
        if isinstance(row, dict) and row.get("evidence_id") in retained]
    result["source_blocks"] = [_reading_record(row) for row in routed.get("source_blocks", ())
        if not isinstance(row, dict) or row.get("evidence_id", row.get("evidence_ref")) in retained]
    result["accepted_facts"] = [_reading_record(row) for row in result.get("accepted_facts", ())]
    result["reading_packet"] = {"version": "assigned-original-readers-v2", "reader": role, "lane": lane,
        "assigned_evidence_ids": sorted(owned), "physically_included_evidence_ids": sorted(retained),
        "delegated_sources": [{"evidence_id": row["evidence_id"], "readers": row["readers"]}
            for row in manifest["sources"] if row["evidence_id"] not in retained],
        "unassigned_evidence_ids": manifest["unassigned_evidence_ids"],
        "completion_policy": manifest["completion_policy"]}
    return result


def _financial_or_source_record(record):
    return isinstance(record, dict) and (record.get("fact_id") or record.get("evidence_id")
        or record.get("kind") in {"financial_fact", "filing_text"})


def _reading_record(record):
    if not _financial_or_source_record(record):
        return deepcopy(record)
    # No recursive stripping of arbitrary professional analysis or of source
    # text. The complete normalization chain stays in the host ledger.
    return {key: deepcopy(value) for key, value in record.items() if key not in _HOST_AUDIT_FIELDS}


def model_reading_view(routed, role, *, required_evidence_ids=()):
    result = deepcopy(routed)
    selected = _TOPICS.get(role)
    if selected is not None and not result.get('accepted_facts'):
        # A source-only caller has no admitted fact channel. Its quantitative
        # source rows must not disappear with a topical assignment, and must
        # not be silently promoted to accepted facts either.
        selected = {*selected, 'quantitative_facts'}
    required = set(required_evidence_ids)
    admitted = {str(item.get('fact_id')): item for item in result.get('accepted_facts', ())
                if isinstance(item, dict) and item.get('fact_id')}
    partitions = {}
    assignments = []
    included = set()
    actual_roles = set(routed.get("source_reader_roles", ()))
    for partition, records in routed.get("evidence_partitions", {}).items():
        retained = []
        for record in records:
            identity = record.get("evidence_id") if isinstance(record, dict) else None
            fact = admitted.get(str(identity).removeprefix('fact:'))
            represented = bool(fact) and all(
                key in fact and str(fact[key]) == str(value)
                for key, value in record.items() if key not in _HOST_AUDIT_FIELDS | {'kind', 'evidence_id'})
            readers = set(_READERS.get(partition, "").split("/"))
            if "source-risk-analysis" in readers:
                readers.add("skeptical-analyst")
            unowned = bool(actual_roles) and not readers.intersection(actual_roles)
            if (selected is None or partition in selected or partition not in _READERS or unowned or identity in required
                    or partition == 'quantitative_facts' and not represented):
                retained.append(_reading_record(record))
                included.add(identity)
            else:
                assignments.append({"evidence_id": identity, "reader": _READERS[partition], "topic": partition})
        partitions[partition] = retained
    result["evidence_partitions"] = partitions
    result["evidence"] = [_reading_record(item) for item in result.get("evidence", [])
                          if isinstance(item, dict) and item.get("evidence_id") in included]
    result["source_blocks"] = [_reading_record(item) for item in result.get("source_blocks", [])
                               if not isinstance(item, dict) or item.get("evidence_id") in included]
    result["accepted_facts"] = [_reading_record(item) for item in result.get("accepted_facts", [])]
    result["reading_packet"] = {"version": "role-reading-view-v1", "reader": role,
        "host_audit_fields": sorted(_HOST_AUDIT_FIELDS), "delegated_sources": assignments,
        "unknown_topic_reader": role,
        "coverage": "Not a full-document claim. All unknown-topic sources stay available; all cited dependency sources are included. Other topical records have explicit reading assignments in the same frozen ledger."}
    return result
