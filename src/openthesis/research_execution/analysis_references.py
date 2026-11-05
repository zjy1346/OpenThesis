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
from ..research_materials import intern_request_values


SECTIONS = frozenset({"executive_summary", "business_model", "financial_quality", "balance_sheet",
    "competitive_position", "growth_opportunities", "counterarguments", "scenarios", "implied_expectations",
    "thesis", "invalidation_conditions", "leading_indicators", "unresolved_questions",
    "management_and_capital_allocation", "existing_growth_engines", "industry_changes_and_value_allocation",
    "source_risk_review", "valuation_and_sensitivity", "evidence_and_methodology"})


def section_dependencies(prior, fields):
    """Close a named section over explicit findings and cross-section risks.

    Only unrelated *registered section* objects can be delegated. Generic or
    unregistered professional analysis remains supplied, since its dependency
    boundary is unknown. No string/record budget or ranking selects evidence.
    """
    dependencies = {
        "financial_quality": {"financial_quality", "balance_sheet", "management_and_capital_allocation"},
        "balance_sheet": {"balance_sheet", "financial_quality"},
        "business_model": {"business_model", "competitive_position", "existing_growth_engines"},
        "competitive_position": {"business_model", "competitive_position", "industry_changes_and_value_allocation"},
        "growth_opportunities": {"growth_opportunities", "existing_growth_engines", "industry_changes_and_value_allocation", "financial_quality"},
        "scenarios": {"scenarios", "financial_quality", "balance_sheet", "growth_opportunities", "implied_expectations"},
    }
    if any(field not in dependencies for field in fields):
        return deepcopy(prior)  # Unknown dependencies cannot authorize cuts.
    wanted = {"counterarguments", "source_risk_review", "unresolved_questions", "invalidation_conditions"}
    wanted.update(*(dependencies[field] for field in fields))
    memo = {}
    def visit(value):
        if not isinstance(value, (dict, list)):
            return value
        if id(value) in memo:
            return memo[id(value)]
        result = {} if isinstance(value, dict) else []
        memo[id(value)] = result
        if isinstance(value, dict):
            result.update({key: visit(child) for key, child in value.items() if key not in SECTIONS or key in wanted})
        else:
            result.extend(visit(child) for child in value)
        return result
    return visit(prior)


def _source(prior):
    from .reference_aliases import _expand_request_paths
    # Bind the existing request-local protocol before walking it. Expansion
    # retains shared object identity, so this cannot recreate an exponential
    # tree merely to build an index over it.
    return _expand_request_paths({"prior_artifacts": intern_request_values(
        dependency_findings(prior), path=("prior_artifacts",))})["prior_artifacts"]


def reference_registry(prior):
    source = _source(prior)
    registry = {}
    visited = set()
    def visit(value, path):
        if isinstance(value, (dict, list)):
            if id(value) in visited:
                return
            visited.add(id(value))
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ROOT_METADATA_FIELDS or key.startswith("_") or key == "claims":
                    continue
                target = [*path, key]
                if child not in (None, "", [], {}) and (key in SECTIONS or key in {"analysis", "summary"}
                        or key.endswith(("_analysis", "_outlook", "_discussion"))):
                    identity = "analysis:" + digest(intern_request_values(child))
                    entry = registry.setdefault(identity, {"path": target,
                        "section": key if key in SECTIONS else None, "paths": [], "sections": []})
                    entry["paths"].append(target)
                    if key in SECTIONS and key not in entry["sections"]:
                        entry["sections"].append(key)
                else:
                    visit(child, target)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, [*path, index])
    visit(source, [])
    return dict(sorted(registry.items()))


def resolve_references(candidate: dict[str, Any], prior, *, assemble_existing=False) -> dict[str, Any]:
    result = deepcopy(candidate)
    refs = result.pop("analysis_refs", None)
    if refs is None and not assemble_existing:
        return result
    registry = reference_registry(prior)
    source = _source(prior)
    from .reference_aliases import RequestPathResolver
    lookup = RequestPathResolver(source)
    refs = list(refs) if isinstance(refs, list) else [] if refs is None else [refs]
    if assemble_existing:
        # New investment judgment must still be produced. Only complete,
        # explicitly section-named upstream work can fill unchanged sections;
        # multiple different bodies remain visible, not arbitrarily selected.
        new_judgment = {"executive_summary", "thesis", "invalidation_conditions"}
        refs.extend({"analysis_id": key, "section": section} for key, entry in registry.items()
            for section in entry["sections"] if section not in new_judgment
            and result.get(section) in (None, "", [], {}))
    accepted, pending = [], []
    seen = set()
    for item in refs:
        entry = registry.get(item.get("analysis_id")) if isinstance(item, dict) else None
        section = item.get("section") if isinstance(item, dict) else None
        if entry is None or section not in SECTIONS or (entry["sections"] and section not in entry["sections"]):
            pending.append({"code": "ANALYSIS_REFERENCE_UNRESOLVED", "index": len(accepted) + len(pending)})
            continue
        value = lookup.target(entry["path"])
        identity = (section, digest(intern_request_values(value)))
        if identity in seen:
            continue
        seen.add(identity)
        if section not in result:
            result[section] = deepcopy(value)
        elif result[section] != value:
            result[section] = [result[section], deepcopy(value)]
        accepted.append({"analysis_id": item["analysis_id"], "section": section,
            "body_sha256": digest(intern_request_values(value))})
    result["_analysis_reference_resolution"] = {"accepted": accepted, "pending": pending,
        "validation": "ordinary_source_and_numeric_checks_required"}
    return result
