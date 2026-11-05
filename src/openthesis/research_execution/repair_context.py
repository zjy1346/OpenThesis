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
    from .reference_aliases import RequestReferenceError
    from .request_planning import RequestCompilationError
    from .request_diagnostics import compilation_diagnostic
    try:
        return _repair_context(prompt, fields)
    except RequestReferenceError as error:
        raise RequestCompilationError(compilation_diagnostic(error, "local-section-repair", prompt,
            {"target_fields": list(fields)})) from error


def _repair_context(prompt, fields):
    try:
        original = json.loads(prompt)
    except (TypeError, ValueError):
        # Unstructured legacy tasks have no proven dependency boundary. Do
        # not pretend the instruction-only subset contains its source records.
        raise ValueError("MODEL_REPAIR_DEPENDENCY_BOUNDARY_UNKNOWN")
    if not isinstance(original, dict):
        raise ValueError("MODEL_REPAIR_DEPENDENCY_BOUNDARY_UNKNOWN")
    # Repairs are independent requests. Expand path aliases against this full
    # frozen input *before* selecting partitions, so a removed sibling cannot
    # strand references into a previous request or the relocated common core.
    from .reference_aliases import _expand_request_paths
    original = _expand_request_paths(original)
    context = deepcopy(original.get("research_context", {}))
    selected = {"risk_disclosures", "audit_disclosures", "other_disclosures"}
    known = all(field in _PARTITIONS for field in fields)
    if known:
        selected.update(*( _PARTITIONS[field] for field in fields))
        partitions = context.get("evidence_partitions", {})
        context["evidence_partitions"] = {key: value for key, value in partitions.items() if key in selected}
        ids = {item.get("evidence_id") for items in context["evidence_partitions"].values()
               for item in items if isinstance(item, dict)}
        context["evidence"] = [item for item in context.get("evidence", [])
                               if isinstance(item, str) and item in ids]
        if isinstance(context.get("source_blocks"), list):
            context["source_blocks"] = [item for item in context["source_blocks"] if item.get("evidence_ref") in ids]
        # Accepted facts are the compact numeric/period/unit boundary, not
        # parser audit or an ancestor report. Even a business/risk patch may
        # depend on them; retaining them avoids an unprovable dependency cut.
    result = {key: deepcopy(original[key]) for key in (
        "output_language", "output_language_instruction", "claim_contract", "request_reference_contract", "late_evidence") if key in original}
    result.update({"research_context": context, "target_fields": list(fields),
        "dependency_scope": "named_fields_and_risk_counterevidence" if known else "expanded_role_material_unknown_field_dependencies",
        "acceptance": "Return only target patches; preserve source identity, period, units and opposing evidence. Unknown facts stay unknown."})
    if not known or any(field in {"scenarios", "growth_opportunities", "opportunities"} for field in fields) or original.get("sectional_delivery"):
        from .analysis_references import section_dependencies
        result["prior_artifacts"] = section_dependencies(original.get("prior_artifacts", {}), fields)
        result["dependency_scope"] += ":cross_stage_findings_retained"
    # Include only reachable interned text. No dangling references or implicit
    # lookup into the preceding request's prompt/cache are permitted.
    registry = original.get("material_registry", {})
    references = set()
    visited = set()
    def visit(value):
        if isinstance(value, (dict, list)):
            if id(value) in visited:
                return
            visited.add(id(value))
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
    checked = set()
    def check(value):
        if isinstance(value, (dict, list)):
            if id(value) in checked:
                return
            checked.add(id(value))
        if isinstance(value, dict):
            if "finding_ref" in value:
                finding = result.get("prior_artifacts", {}).get("findings_registry", {}).get(value["finding_ref"])
                if not isinstance(finding, dict) or not isinstance(finding.get("text"), str):
                    raise ValueError("MODEL_REPAIR_SOURCE_REFERENCE_UNRESOLVED")
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(result)
    # Expanding paths was necessary for safe selection, not permission to send
    # another uncompressed copy of every admitted row in a tiny field patch.
    # The same lossless table contract applies to these independent requests.
    from .source_compiler import compact_record_table
    for key in ("accepted_facts", "repair_observations", "repair_catalog"):
        if key in context:
            context[key] = compact_record_table(context[key])
    for key, records in context.get("evidence_partitions", {}).items():
        context["evidence_partitions"][key] = compact_record_table(records)
    if "record_table_contract" in original:
        result["record_table_contract"] = original["record_table_contract"]
    from ..research_materials import intern_request_values
    result = intern_request_values(result)
    # Validate the NEW request layout with the shared addressing semantics;
    # send its compact representation, not this local expanded audit graph.
    _expand_request_paths(result)
    return result
