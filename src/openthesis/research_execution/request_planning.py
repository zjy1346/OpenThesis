"""Forecast complete role requests without making forecasts execution quotas."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .input_budget import input_measurement
from .model import provider_context_capability, provider_declares_context_capability


@dataclass(frozen=True, slots=True)
class RoleRequestMaterials:
    agent_id: str
    context_json: str
    prior_artifacts: dict[str, Any]
    role_id: str | None = None
    output_schema: str | None = None


def plan_role_requests(workflow, registry, inputs, saved=None, *, reused_stage_ids=(),
                       stage_inputs=None, request_materials=None):
    # This uses the same request builder as dispatch. Source text remains
    # complete; local estimates never masquerade as provider billed tokens.
    from ..research import _agent_prompt_bundle, _agent_role, _deduplicated_stage_context
    from ..research_assertions import _verification_prompt, _MODEL_PROTOCOL, source_identity, source_text
    from .source_compiler import packet_materials, compact_record_table
    from .executor import dependency_findings
    rows = []
    saved = saved or {}
    packets = {}
    levels = {}
    verification_groups = {}
    for spec in registry.stages:
        levels[spec.stage_id] = max((levels[key] + 1 for key in spec.depends_on), default=0)
    for spec in registry.stages:
        frozen_inputs = (stage_inputs or {}).get(spec.stage_id, inputs)
        dependencies = {key: dependency_findings(saved[key].validation.trusted_output)
                        for key in spec.depends_on if key in saved}
        if spec.optional_dependencies:
            dependencies["_dependency_gaps"] = [{"stage_id": key,
                "state": saved[key].validation.state, "issue_codes": list(saved[key].validation.issues)}
                for key in spec.optional_dependencies if key in saved and not saved[key].validation.passed]
        packets[spec.stage_id] = (request_materials(spec, frozen_inputs, dependencies) if request_materials else
            RoleRequestMaterials(spec.stage_id, frozen_inputs["context_json"], dependencies,
                                 spec.role, spec.output_contract or None))
    planned_budget = getattr(workflow, "_stage_planning_output_budget", None)
    output_limits = {key: planned_budget(packet.agent_id, key) if callable(planned_budget)
        else workflow._stage_output_budget(packet.agent_id) for key, packet in packets.items()}
    def visible_allowance(stage_id):
        packet = packets[stage_id]
        complex_stage = stage_id not in {"business-analyst", "source-industry-analysis", "source-risk-analysis"} and any(
            key in packet.agent_id for key in ("growth", "skeptic", "forecast", "counter", "synthes"))
        return min(output_limits[stage_id], 8192 if complex_stage else 4096)
    for spec in registry.stages:
        if spec.stage_id in reused_stage_ids:
            continue
        packet = packets[spec.stage_id]
        context_json, _ = _deduplicated_stage_context(packet.context_json, packet.agent_id)
        system, user = _agent_prompt_bundle(packet.agent_id, workflow.pack.prompt(spec.prompt_path),
            workflow.report_language, context_json, packet.prior_artifacts,
            role_id=packet.role_id, output_schema=packet.output_schema)
        provider = workflow._stage_budget_provider(packet.agent_id)
        measured = input_measurement(provider, system, user)
        # Future dependency bodies do not exist yet. Explicit output allowances
        # are a planning estimate, not a copy of the full source graph N times.
        dependency_estimate = sum(visible_allowance(key) for key in spec.depends_on if key not in saved)
        rows.append({"stage_id": spec.stage_id, "purpose": "generation", "input_estimate": measured["tokens"] + dependency_estimate,
            "compiled_input_estimate": measured["tokens"], "input_counting_method": measured["method"],
            "input_bytes": measured["input_bytes"], "pending_dependency_output_estimate": dependency_estimate,
            "output_limit": output_limits[spec.stage_id]})
        material = packet_materials(json.loads(context_json), _agent_role(packet.agent_id, packet.role_id))
        identities = {}
        all_identities = {}
        for records in material.get("evidence_partitions", {}).values():
            for record in records:
                if not isinstance(record, dict):
                    continue
                if "accepted_fact_ref" in record:
                    fact = next((fact for fact in material.get("accepted_facts", ()) if fact.get("fact_id") == record["accepted_fact_ref"]), {})
                    record = {**fact, **record}
                if source_text(record):
                    identity = source_identity(record)
                    all_identities[identity["source_block_id"]] = identity
                    # Planning semantic verdicts over every deterministic unit
                    # proof costs a second whole filing. Actual validation still
                    # sends EVERY cited premise of a new inference; no query is
                    # filtered here, and numeric trust stays with the compiler.
                    evidence_id = str(record.get("evidence_id", ""))
                    if record.get("kind") != "financial_fact" and not evidence_id.startswith(("fact:", "unit:", "statement:")):
                        identities[identity["source_block_id"]] = identity
        source_sizes = sorted((input_measurement(provider, "", json.dumps(value, ensure_ascii=False, separators=(",", ":")))["tokens"]
            for value in all_identities.values()), reverse=True)
        from .reference_aliases import reference_aliases, encode_references
        aliases = reference_aliases(context_json)
        fact_boundary = input_measurement(provider, "", json.dumps(encode_references(
            compact_record_table(material.get("accepted_facts", [])), aliases), ensure_ascii=False, separators=(",", ":")))["tokens"]
        # Forecast a local 4K patch (four assertions, two complete sources each),
        # not two duplicates of the largest generation. This selects NOTHING
        # for dispatch: actual repair materials are source-bound and measured
        # atomically; a larger necessary repair appends its actual work item.
        rows[-1]["repair_input_estimate"] = min(measured["tokens"], 4096 + fact_boundary + sum(source_sizes[:8]))
        rows[-1]["repair_input_basis"] = "four_assertion_patch_with_complete_source_units_estimate_not_supplier_usage_or_dispatch_filter"
        # Reasoning allowance does not manufacture additional visible claims.
        # This remains an explicit estimate until the actual candidate exists.
        complex_stage = spec.stage_id not in {"business-analyst", "source-industry-analysis", "source-risk-analysis"} and any(
            key in packet.agent_id for key in ("growth", "skeptic", "forecast", "counter", "synthes"))
        visible_target = min(output_limits[spec.stage_id], 8192 if complex_stage else 4096)
        expected_assertions = max(1, math.ceil(visible_target / 1024))
        key = (levels[spec.stage_id], getattr(getattr(provider, "config", None), "public_id", "workflow-model"))
        group = verification_groups.setdefault(key, {"stage_id": spec.stage_id, "provider": provider,
            "identities": {}, "assertions": 0, "stages": []})
        group["identities"].update(identities)
        group["assertions"] += expected_assertions
        group["stages"].append(spec.stage_id)
    for (wave, _route), group in sorted(verification_groups.items()):
        provider = group["provider"]
        # The future assertions do not exist at compilation time. Forecast
        # two COMPLETE source units per planned assertion, bounded by the
        # actual pool, instead of pretending every later inference will cite
        # every filing. Choose the largest units for a conservative forecast.
        # This is NOT the runtime query or a relevance/Top-K source filter:
        # verification dispatch still receives every actually cited source,
        # measures its complete request and shares the same atomic envelope.
        expected_assertions = group["assertions"]
        available_sources = list(group["identities"].values())
        forecast_sources = sorted(available_sources, key=lambda value: len(json.dumps(
            value, ensure_ascii=False, separators=(",", ":")).encode()), reverse=True)[:expected_assertions * 2]
        source_query = [{"assertion_id": "planning-only", "sources": forecast_sources}]
        verification = input_measurement(provider, _MODEL_PROTOCOL, _verification_prompt(source_query))
        capability = provider_context_capability(provider)
        batch_estimate = max(1, math.ceil(verification["tokens"] / max(1, capability.available_input_tokens * .85))) if provider_declares_context_capability(provider) else 1
        batch_estimate = max(batch_estimate, math.ceil((1024 + expected_assertions * 384) / 32_000))
        for index in range(batch_estimate):
            rows.append({"stage_id": group["stage_id"], "purpose": "semantic_verification", "wave_index": wave,
                "wave_stages": sorted(group["stages"]), "source_groups": len(group["identities"]),
                "source_unit_estimate": len(forecast_sources),
                "source_counting": "largest_complete_units_two_per_future_assertion_forecast_not_dispatch_filter",
                "batch_estimate_index": index, "input_estimate": math.ceil(verification["tokens"] / batch_estimate) + 1024,
                "input_counting_method": verification["method"], "output_limit": min(32_000, 1024 + math.ceil(expected_assertions / batch_estimate) * 384),
                "assertion_counting": "output_allowance_estimate_until_candidate_exists"})
    return rows
