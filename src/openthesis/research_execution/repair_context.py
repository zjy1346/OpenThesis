"""Dependency-complete reading view for a named repair, not a prompt append."""
from __future__ import annotations

from copy import deepcopy
import json


_PARTITIONS = {
    "business_model": {"business_disclosures", "mda_disclosures"},
    "competitive_position": {"business_disclosures", "mda_disclosures"},
    "growth_opportunities": {"quantitative_facts", "business_disclosures", "mda_disclosures"},
    "opportunities": {"quantitative_facts", "business_disclosures", "mda_disclosures"},
    "financial_quality": {"quantitative_facts", "audit_disclosures", "mda_disclosures"},
    "balance_sheet": {"quantitative_facts", "audit_disclosures"},
    "scenarios": {"quantitative_facts", "business_disclosures", "mda_disclosures"},
}


def repair_context(prompt, fields):
    try:
        original = json.loads(prompt)
    except (TypeError, ValueError):
        # Unstructured legacy tasks have no proven dependency boundary. Do
        # not pretend the instruction-only subset contains its source records.
        raise ValueError("MODEL_REPAIR_DEPENDENCY_BOUNDARY_UNKNOWN")
    if not isinstance(original, dict):
        raise ValueError("MODEL_REPAIR_DEPENDENCY_BOUNDARY_UNKNOWN")
    context = deepcopy(original.get("research_context", {}))
    selected = {"risk_disclosures", "other_disclosures"}
    known = all(field in _PARTITIONS for field in fields)
    if known:
        selected.update(*( _PARTITIONS[field] for field in fields))
        partitions = context.get("evidence_partitions", {})
        context["evidence_partitions"] = {key: value for key, value in partitions.items() if key in selected}
        ids = {item.get("evidence_id") for items in context["evidence_partitions"].values()
               for item in items if isinstance(item, dict)}
        context["evidence"] = [item for item in context.get("evidence", [])
                               if isinstance(item, str) and item in ids]
        context["source_blocks"] = [item for item in context.get("source_blocks", [])
                                    if item.get("evidence_ref") in ids]
        # Accepted facts are the compact numeric/period/unit boundary, not
        # parser audit or an ancestor report. Even a business/risk patch may
        # depend on them; retaining them avoids an unprovable dependency cut.
    result = {key: deepcopy(original[key]) for key in (
        "output_language", "output_language_instruction", "claim_contract", "request_reference_contract") if key in original}
    result.update({"research_context": context, "target_fields": list(fields),
        "dependency_scope": "named_fields_and_risk_counterevidence" if known else "expanded_role_material_unknown_field_dependencies",
        "acceptance": "Return only target patches; preserve source identity, period, units and opposing evidence. Unknown facts stay unknown."})
    if not known or any(field in {"scenarios", "growth_opportunities", "opportunities"} for field in fields):
        result["prior_artifacts"] = deepcopy(original.get("prior_artifacts", {}))
        result["dependency_scope"] += ":cross_stage_findings_retained"
    # Include only reachable interned text. No dangling references or implicit
    # lookup into the preceding request's prompt/cache are permitted.
    registry = original.get("material_registry", {})
    references = set()
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("material_ref"), str):
                references.add(value["material_ref"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    if references - set(registry):
        raise ValueError("MODEL_REPAIR_SOURCE_REFERENCE_UNRESOLVED")
    result["material_registry"] = {key: deepcopy(registry[key]) for key in sorted(references)}
    def resolve_path(path):
        value = result
        try:
            for component in path:
                value = value[component]
        except (KeyError, IndexError, TypeError):
            raise ValueError("MODEL_REPAIR_SOURCE_REFERENCE_UNRESOLVED") from None
        return value
    def check(value):
        if isinstance(value, dict):
            if "finding_ref" in value:
                finding = result.get("prior_artifacts", {}).get("findings_registry", {}).get(value["finding_ref"])
                if not isinstance(finding, dict) or not isinstance(finding.get("text"), str):
                    raise ValueError("MODEL_REPAIR_SOURCE_REFERENCE_UNRESOLVED")
            if "request_path_ref" in value:
                if not isinstance(value["request_path_ref"], list):
                    raise ValueError("MODEL_REPAIR_SOURCE_REFERENCE_UNRESOLVED")
                resolve_path(value["request_path_ref"])
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(result)
    return result
