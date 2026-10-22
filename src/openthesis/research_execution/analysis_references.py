"""Host copies of named prior findings; IDs never convey factual trust.

The registry contains paths, not duplicated bodies. Resolution is against the
current frozen dependencies only and runs before ordinary business validation.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..resilience.contracts import digest
from ..research_response import ROOT_METADATA_FIELDS
from .executor import dependency_findings


SECTIONS = frozenset({"executive_summary", "business_model", "financial_quality", "balance_sheet",
    "competitive_position", "growth_opportunities", "counterarguments", "scenarios", "implied_expectations",
    "thesis", "invalidation_conditions", "leading_indicators", "unresolved_questions",
    "management_and_capital_allocation", "existing_growth_engines", "industry_changes_and_value_allocation",
    "source_risk_review", "valuation_and_sensitivity", "evidence_and_methodology"})


def reference_registry(prior):
    source = dependency_findings(prior)
    registry = {}
    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ROOT_METADATA_FIELDS or key.startswith("_") or key == "claims":
                    continue
                target = [*path, key]
                if child not in (None, "", [], {}) and (key in SECTIONS or key in {"analysis", "summary"}
                        or key.endswith(("_analysis", "_outlook", "_discussion"))):
                    identity = "analysis:" + digest({"path": target, "content": child})
                    registry[identity] = {"path": target, "section": key if key in SECTIONS else None}
                else:
                    visit(child, target)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, [*path, index])
    visit(source, [])
    return dict(sorted(registry.items()))


def resolve_references(candidate: dict[str, Any], prior) -> dict[str, Any]:
    result = deepcopy(candidate)
    refs = result.pop("analysis_refs", None)
    if refs is None:
        return result
    registry = reference_registry(prior)
    source = dependency_findings(prior)
    accepted, pending = [], []
    seen = set()
    for item in refs if isinstance(refs, list) else [refs]:
        entry = registry.get(item.get("analysis_id")) if isinstance(item, dict) else None
        section = item.get("section") if isinstance(item, dict) else None
        if entry is None or section not in SECTIONS or entry["section"] not in {None, section}:
            pending.append({"code": "ANALYSIS_REFERENCE_UNRESOLVED", "index": len(accepted) + len(pending)})
            continue
        value = source
        for key in entry["path"]:
            value = value[key]
        identity = (section, digest(value))
        if identity in seen:
            continue
        seen.add(identity)
        if section not in result:
            result[section] = deepcopy(value)
        elif result[section] != value:
            result[section] = [result[section], deepcopy(value)]
        accepted.append({"analysis_id": item["analysis_id"], "section": section, "body_sha256": digest(value)})
    result["_analysis_reference_resolution"] = {"accepted": accepted, "pending": pending,
        "validation": "ordinary_source_and_numeric_checks_required"}
    return result
