"""Research reading packets, separate from the full host evidence ledger.

Only explicitly named local parser/bookkeeping metadata is removed. Values,
units, scopes, source bytes, headers, revisions and opposing evidence are not
summarized. Every omitted topical record has a named reader, not a Top-K cut.
"""
from __future__ import annotations

from copy import deepcopy


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
    for partition, records in routed.get("evidence_partitions", {}).items():
        retained = []
        for record in records:
            identity = record.get("evidence_id") if isinstance(record, dict) else None
            fact = admitted.get(str(identity).removeprefix('fact:'))
            represented = bool(fact) and all(
                key in fact and str(fact[key]) == str(value)
                for key, value in record.items() if key not in _HOST_AUDIT_FIELDS | {'kind', 'evidence_id'})
            if (selected is None or partition in selected or partition not in _READERS or identity in required
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
