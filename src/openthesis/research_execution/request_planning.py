"""Compile role requests before allocating a finite, versioned run envelope."""
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
    from ..research_materials import prompt_materials
    from .executor import dependency_findings
    rows = []
    saved = saved or {}
    packets = {}
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
    output_limits = {key: workflow._stage_output_budget(packet.agent_id) for key, packet in packets.items()}
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
        dependency_estimate = sum(output_limits[key] for key in spec.depends_on if key not in saved)
        rows.append({"stage_id": spec.stage_id, "purpose": "generation", "input_estimate": measured["tokens"] + dependency_estimate,
            "compiled_input_estimate": measured["tokens"], "input_counting_method": measured["method"],
            "input_bytes": measured["input_bytes"], "pending_dependency_output_estimate": dependency_estimate,
            "output_limit": output_limits[spec.stage_id]})
        material = prompt_materials(json.loads(context_json), _agent_role(packet.agent_id, packet.role_id))
        identities = {}
        for records in material.get("evidence_partitions", {}).values():
            for record in records:
                if not isinstance(record, dict):
                    continue
                if "accepted_fact_ref" in record:
                    fact = next((fact for fact in material.get("accepted_facts", ()) if fact.get("fact_id") == record["accepted_fact_ref"]), {})
                    record = {**fact, **record}
                if source_text(record):
                    identity = source_identity(record)
                    identities[identity["source_block_id"]] = identity
        source_query = [{"assertion_id": "planning-only", "sources": list(identities.values())}]
        verification = input_measurement(provider, _MODEL_PROTOCOL, _verification_prompt(source_query))
        capability = provider_context_capability(provider)
        batch_estimate = max(1, math.ceil(verification["tokens"] / max(1, capability.available_input_tokens * .85))) if provider_declares_context_capability(provider) else 1
        expected_assertions = max(1, math.ceil(output_limits[spec.stage_id] / 512))
        batch_estimate = max(batch_estimate, math.ceil((1024 + expected_assertions * 384) / 32_000))
        for index in range(batch_estimate):
            rows.append({"stage_id": spec.stage_id, "purpose": "semantic_verification", "source_groups": len(identities),
                "batch_estimate_index": index, "input_estimate": math.ceil(verification["tokens"] / batch_estimate) + 1024,
                "input_counting_method": verification["method"], "output_limit": min(32_000, 1024 + math.ceil(expected_assertions / batch_estimate) * 384),
                "assertion_counting": "output_allowance_estimate_until_candidate_exists"})
    return rows
