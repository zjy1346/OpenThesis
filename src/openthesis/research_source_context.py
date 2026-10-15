"""Pure, typed material-context projections for research stages.

The financial compiler remains the only admission gate.  This module only
projects already supplied facts, source evidence, and repair observations into
separate JSON channels; it never promotes a candidate or recalculates a fact.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any
import re

from .research_assertions import source_identity
from .resilience.contracts import digest


SOURCE_CONTEXT_VERSION = "research-source-context-v2-question-repair"
_REPAIR_USE_CLASSES = ["repair_mapping", "model_analysis_labelled"]


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _as_dict(item: Any) -> dict[str, Any] | None:
    if isinstance(item, Mapping):
        return deepcopy(dict(item))
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return deepcopy(dict(value))
    return None


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _target_matches(observation: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    """Match only explicit stable target identities; never infer by value."""

    keys = ("concept", "fact_id", "evidence_id", "company_cik", "accession_number",
            "period_end", "fiscal_period", "scope", "source_hash")
    supplied = [key for key in keys if target.get(key) not in (None, "")]
    return bool(supplied) and all(observation.get(key) == target.get(key) for key in supplied)


def _repair_catalog_item(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Return metadata useful for retrieval without exposing candidate prose."""

    return {
        key: deepcopy(observation.get(key))
        for key in (
            "fact_id", "evidence_id", "concept", "accession_number", "period_end",
            "fiscal_period", "fiscal_year", "company_cik", "source_document",
            "source_page", "source_bbox", "source_hash", "issue_codes",
        )
        if key in observation
    }


def _normalise_repair_observation(item: Any) -> dict[str, Any] | None:
    value = _as_dict(item)
    if value is None:
        return None
    # A repair observation is not an admission result, even if an old caller
    # supplied an optimistic status.  Keep the source/candidate values intact,
    # but make the use boundary explicit and fail closed.
    value["allowed_uses"] = list(_REPAIR_USE_CLASSES)
    value["financial_admission"] = False
    value["verification_state"] = "unresolved"
    return value


def source_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build separate source, fact, repair, and issue channels.

    ``facts`` is the only accepted-fact input.  Evidence records are converted
    to host-bound source blocks, but are never copied into ``accepted_facts``.
    Repair observations remain separate and are not selected for model display
    until their caller supplies an explicit repair target.
    """

    payload = context if isinstance(context, Mapping) else {}
    facts = [deepcopy(item) for item in _sequence(payload.get("facts"))]
    blocks: list[dict[str, Any]] = []
    for item in _sequence(payload.get("evidence")):
        record = _as_dict(item)
        if record is None:
            continue
        try:
            identity = source_identity(record)
        except (TypeError, ValueError, KeyError):
            continue
        block = dict(identity)
        for key in (
            "bbox", "source_document", "accession_number", "concept", "currency",
            "unit", "unit_scale", "unit_provenance", "start_date", "filed_at",
        ):
            if key in record:
                block[key] = deepcopy(record[key])
        blocks.append(block)

    observations = [
        observation for item in _sequence(payload.get("repair_observations"))
        if (observation := _normalise_repair_observation(item)) is not None
    ]
    issues = [deepcopy(item) for item in _sequence(payload.get("issues"))]
    targets = [
        dict(item) for item in _sequence(payload.get("repair_targets"))
        if isinstance(item, Mapping)
    ]
    selected = [
        deepcopy(observation)
        for observation in observations
        if targets and any(_target_matches(observation, target) for target in targets)
    ]
    return {
        "contract_version": SOURCE_CONTEXT_VERSION,
        "accepted_facts": facts,
        "source_blocks": blocks,
        "repair_observations": observations,
        "issues": issues,
        "repair_catalog": [_repair_catalog_item(item) for item in observations],
        "repair_retrieval": {
            "required": bool(observations) and not bool(targets),
            "targets": deepcopy(targets),
            "selected_count": len(selected),
        },
        "targeted_repair_observations": selected,
    }


def repair_observations_from_canonical(canonical: Any) -> list[dict[str, Any]]:
    """Project non-research candidates and problematic validation records.

    No values are normalized here.  A missing source binding remains explicitly
    ``unknown`` and is carried as an observation for repair/audit only.
    """

    research = _sequence(_value(canonical, "research_facts", ()))
    research_ids = {
        str(_value(item, "fact_id", "")) for item in research if _value(item, "fact_id", "")
    }
    candidates = _sequence(_value(canonical, "resolved_facts", ()))
    candidates.extend(_sequence(_value(canonical, "quarantined_facts", ())))
    coverage = _value(canonical, "coverage", {})
    if not isinstance(coverage, Mapping):
        coverage = {}
    raw_records = coverage.get("fact_validation_records", {})
    records = raw_records if isinstance(raw_records, Mapping) else {}
    evidence_by_id = {str(_value(ref, "evidence_id", "")): ref
                      for ref in _sequence(_value(canonical, "evidence", ()))}
    filings = _sequence(_value(canonical, "filings", ()))

    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        fact = _as_dict(candidate)
        if fact is None:
            continue
        fact_id = str(fact.get("fact_id", ""))
        if not fact_id or fact_id in seen:
            continue
        seen.add(fact_id)
        raw_record = records.get(fact_id, {})
        record = deepcopy(dict(raw_record)) if isinstance(raw_record, Mapping) else {}
        components = record.get("components", {})
        amount = components.get("amount", {}) if isinstance(components, Mapping) else {}
        row_ids = amount.get("evidence_ids", ()) if isinstance(amount, Mapping) else ()
        original_bindings = []
        for filing in filings:
            if (_value(filing, "accession_number") != fact.get("accession_number")
                    or _value(filing, "company_cik") != fact.get("company_cik")):
                continue
            for row_id in row_ids:
                ref = evidence_by_id.get(str(row_id))
                if (ref is not None and _value(ref, "document_id") == _value(filing, "document_id")
                        and _value(ref, "content_hash") and _value(ref, "content_hash") == _value(filing, "content_hash")
                        and _value(ref, "excerpt") == fact.get("raw_text")):
                    original_bindings.append({"evidence_id": str(row_id),
                        "document_id": _value(filing, "document_id"),
                        "document_sha256": _value(ref, "content_hash"),
                        "page": fact.get("source_page"), "bbox": deepcopy(fact.get("source_bbox"))})
        bound_hashes = {item["document_sha256"] for item in original_bindings}
        source_hash = next(iter(bound_hashes)) if len(bound_hashes) == 1 else ""
        issue_codes = record.get("issues", ())
        if isinstance(issue_codes, str):
            issue_codes = [issue_codes] if issue_codes else []
        elif not isinstance(issue_codes, (list, tuple)):
            issue_codes = []
        has_issue = bool(issue_codes) or record.get("accepted") is False
        if fact_id in research_ids and not has_issue:
            continue
        lineage = fact.get("normalization_lineage")
        if isinstance(lineage, Mapping):
            lineage = deepcopy(dict(lineage))
            raw_value = lineage.get("raw_value", "")
        else:
            raw_value = fact.get("raw_value", "")
        observation = {
            "fact_id": fact_id,
            "evidence_id": f"fact:{fact_id}",
            "concept": fact.get("concept", ""),
            "reported_concept": fact.get("reported_concept", ""),
            "company_cik": fact.get("company_cik", ""),
            "entity": fact.get("entity", ""),
            "accession_number": fact.get("accession_number", ""),
            "period_end": fact.get("end_date", ""),
            "period_start": fact.get("start_date", ""),
            "fiscal_year": fact.get("fiscal_year"),
            "fiscal_period": fact.get("fiscal_period", ""),
            "scope": fact.get("scope", ""),
            "currency": fact.get("currency", ""),
            "unit": fact.get("unit", ""),
            "unit_provenance": fact.get("unit_provenance", "unknown"),
            "source_document": fact.get("source_document", ""),
            "source_url": fact.get("source_url", ""),
            "source_page": fact.get("source_page"),
            "source_bbox": deepcopy(fact.get("source_bbox")),
            "raw_text": fact.get("raw_text", ""),
            "raw_value": raw_value,
            "candidate_value": record.get("candidate_value", record.get("value", fact.get("value"))),
            "normalization_lineage": lineage,
            "source_hash": source_hash,
            "source_bindings": original_bindings,
            "issue_codes": [str(item) for item in issue_codes],
            "validation_record": record,
            "source_binding": "bound" if all(
                fact.get(key) for key in ("company_cik", "accession_number", "end_date", "source_document")
            ) and bool(source_hash) else "unknown",
            "allowed_uses": list(_REPAIR_USE_CLASSES),
            "financial_admission": False,
            "verification_state": "unresolved",
        }
        projected.append(observation)
    return projected


def route_repair_observations(
    observations: Sequence[Mapping[str, Any]], targets: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return target-matched body and metadata-only catalog."""

    if not isinstance(observations, (list, tuple)):
        observations = ()
    normalized = [
        item for raw in observations
        if (item := _normalise_repair_observation(raw)) is not None
    ]
    target_list = [item for item in targets if isinstance(item, Mapping)]
    selected = [
        deepcopy(item) for item in normalized
        if target_list and any(_target_matches(item, target) for target in target_list)
    ]
    return selected, [_repair_catalog_item(item) for item in normalized]


def observation_source_record(observation: Mapping[str, Any]) -> dict[str, Any] | None:
    """Original row for labelled interpretation, never an admitted amount."""
    bindings = observation.get('source_bindings')
    if not isinstance(bindings, (list, tuple)) or not observation.get('raw_text'):
        return None
    expected_hash = observation.get('source_hash')
    sources = [item for item in bindings if isinstance(item, Mapping)
        and item.get('document_id') and item.get('document_sha256') == expected_hash
        and item.get('page') == observation.get('source_page')]
    identities = {(item.get('document_id'), item.get('document_sha256'), item.get('page')) for item in sources}
    page = observation.get('source_page')
    if (len(identities) != 1 or not isinstance(expected_hash, str)
            or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_hash)
            or not isinstance(page, int) or isinstance(page, bool) or page < 1
            or not observation.get('source_url')):
        return None
    source = sources[0]
    record = {'kind': 'unadmitted_source_row', 'document_id': source['document_id'],
        'document_sha256': expected_hash, 'page': source['page'], 'bbox': deepcopy(source.get('bbox')),
        'source_url': observation.get('source_url'), 'company_cik': observation.get('company_cik'),
        'end_date': observation.get('period_end'), 'fiscal_period': observation.get('fiscal_period'),
        'scope': observation.get('scope'), 'raw_text': observation['raw_text'],
        'allowed_uses': list(_REPAIR_USE_CLASSES), 'financial_admission': False,
        'issue_codes': deepcopy(observation.get('issue_codes', ())), 'candidate_fact_id': observation.get('fact_id')}
    record['evidence_id'] = 'observation:' + digest(record)
    return record
