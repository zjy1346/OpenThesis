"""OT stage IDs stay distinct from the semantic roles used for research policy."""
from __future__ import annotations

import json
import hashlib

import pytest

from openthesis.research import (
    SynthesisContextLimitError,
    _agent_input_size,
    _agent_prompt_bundle,
    _stage_input_digest,
)
from openthesis.resilience.contracts import canonical_json
from openthesis.research_execution.registry import StageRegistry, StageSpec
from test_research_workflow import _valid_research_synthesis_output
from test_synthesis_capacity_preflight import (
    RecordingCoverageProvider,
    _coverage_checkpoints,
    _scenario as _coverage_scenario,
)
from test_synthesis_field_recovery import _scenario


_SYNTHESIS_PROMPT = "prompts/research-synthesizer.md"


def _material_adequacy(payload):
    reference = payload["research_context"]["material_adequacy"]["request_path_ref"]
    value = payload
    for component in reference:
        value = value[component]
    return value


def _prepare_custom_stage(journal, stage_id: str, role_id: str) -> int:
    registry = StageRegistry((StageSpec(
        stage_id,
        "ot-stage-v1",
        "model-analysis",
        "research-evidence-v1",
        input_contracts=("research-context-v1",),
        output_contract="ot-declared-output-schema-v1",
        role=role_id,
        prompt_path=_SYNTHESIS_PROMPT,
    ),))
    return journal.prepare(registry, "frozen-ot-material")[stage_id]


def _run_adaptive(context, workflow, journal, *, stage_id: str, role_id: str,
                  prior_artifacts: dict | None = None):
    generation = _prepare_custom_stage(journal, stage_id, role_id)
    with journal.bind(stage_id, generation), workflow._bind_request_journal(journal):
        return workflow._run_agent_adaptive(
            stage_id,
            _SYNTHESIS_PROMPT,
            context.compact_json(),
            prior_artifacts or {},
            role_id=role_id,
        )


def test_custom_ot_synthesis_node_repairs_with_role_contract_and_keeps_stage_identity(tmp_path):
    original = {
        **_valid_research_synthesis_output(),
        "structured_output_valid": True,
        "extension": {"risk_text": "Preserve this caller extension."},
    }
    del original["financial_quality"]
    patch = {
        "structured_output_valid": True,
        "section_patches": {"financial_quality": "Synthetic financial quality."},
    }
    store, context, provider, workflow, journal, _generation = _scenario(
        tmp_path, [original, patch]
    )
    stage_id = "ot-custom-synthesis-node"

    result = _run_adaptive(
        context, workflow, journal, stage_id=stage_id,
        role_id="research-synthesizer",
    )

    assert len(provider.wires) == 2
    assert {
        key: value for key, value in result.items()
        if key not in {"_stage_material_manifest", "_material_adequacy"}
    } == {
        **original,
        "financial_quality": patch["section_patches"]["financial_quality"],
    }
    assert result["claims"] == original["claims"]
    assert result["extension"] == original["extension"]
    assert result["_stage_material_manifest"]["stage"] == stage_id
    with store.connect() as db:
        requests = db.execute(
            "SELECT stage_id FROM model_request_journal WHERE run_id=?", ("journal-run",)
        ).fetchall()
    assert requests and {row["stage_id"] for row in requests} == {stage_id}


def test_missing_business_disclosure_does_not_block_synthesis_with_other_material(tmp_path):
    store, context, provider, workflow, journal, _generation = _scenario(
        tmp_path, [_valid_research_synthesis_output()] * 6,
    )
    context.evidence = [
        item for item in context.evidence
        if item.get("topic") != "business" and item.get("evidence_id") != "filing:business"
    ]

    result = _run_adaptive(
        context, workflow, journal,
        stage_id="ot-synthesis-without-business-material",
        role_id="research-synthesizer",
    )

    assert provider.wires
    gate = result["_material_adequacy"]
    assert gate["status"] == "ready"
    assert "business_disclosures" not in gate["missing"]


def test_synthesis_named_node_with_business_role_does_not_require_synthesis_fields(tmp_path):
    ordinary = {
        "structured_output_valid": True,
        "summary": "The business sells equipment to industrial customers.",
        "claims": [{"text": "Customers use the company's equipment."}],
        "extension": {"source_note": "caller-owned field"},
    }
    _store, context, provider, workflow, journal, _generation = _scenario(tmp_path, [ordinary])

    result = _run_adaptive(
        context, workflow, journal,
        stage_id="research-synthesizer",
        role_id="business-analyst",
    )

    assert len(provider.wires) == 1
    assert result["summary"] == ordinary["summary"]
    assert result["claims"] == ordinary["claims"]
    assert result["extension"] == ordinary["extension"]


def test_custom_synthesis_prompt_uses_role_for_coverage_but_keeps_node_as_agent(tmp_path):
    _store, context, _provider, workflow, _journal_value, _generation = _scenario(tmp_path, [])
    stage_id = "ot-custom-synthesis-prompt-node"
    role_id = "research-synthesizer"
    context_json = context.compact_json()
    role_prompt = workflow.pack.prompt(_SYNTHESIS_PROMPT)

    _system_prompt, user_prompt = _agent_prompt_bundle(
        stage_id,
        role_prompt,
        workflow.report_language,
        context_json,
        {},
        role_id=role_id,
    )
    payload = json.loads(user_prompt)

    assert payload["agent"] == stage_id
    assert payload["agent_role"] == role_id
    assert "research_coverage_contract" in payload
    assert _material_adequacy(payload)["agent_id"] == role_id


def test_custom_synthesis_input_size_counts_the_exact_utf8_prompt_bundle(tmp_path):
    _store, context, _provider, workflow, _journal_value, _generation = _scenario(tmp_path, [])
    stage_id = "ot-custom-synthesis-size-node"
    role_id = "research-synthesizer"
    context_json = context.compact_json()
    role_prompt = workflow.pack.prompt(_SYNTHESIS_PROMPT)
    system_prompt, user_prompt = _agent_prompt_bundle(
        stage_id,
        role_prompt,
        workflow.report_language,
        context_json,
        {"prior": "kept"},
        role_id=role_id,
    )

    actual_size = _agent_input_size(
        stage_id,
        role_prompt,
        workflow.report_language,
        context_json,
        {"prior": "kept"},
        role_id=role_id,
    )

    assert actual_size == len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))


def test_stage_input_digest_preserves_legacy_identity_and_separates_declared_role_and_schema():
    context_json = '{"company":{"name":"Example"},"evidence":[{"evidence_id":"fact:1"}]}'
    prior = {"summary": "Keep the complete prior artifact."}
    legacy_payload = {"context": json.loads(context_json), "prior_artifacts": prior}
    legacy_expected = hashlib.sha256(canonical_json(legacy_payload).encode("utf-8")).hexdigest()

    default_digest = _stage_input_digest(context_json, prior)
    synthesis_digest = _stage_input_digest(
        context_json,
        prior,
        role_id="research-synthesizer",
        output_schema="ot://openthesis/agent-artifact/1",
    )
    business_digest = _stage_input_digest(
        context_json,
        prior,
        role_id="business-analyst",
        output_schema="ot://openthesis/agent-artifact/1",
    )
    synthesis_other_schema_digest = _stage_input_digest(
        context_json,
        prior,
        role_id="research-synthesizer",
        output_schema="ot://openthesis/diagnostic/1",
    )

    assert default_digest == legacy_expected
    assert len({default_digest, synthesis_digest, business_digest,
                synthesis_other_schema_digest}) == 4


def test_ot_coverage_keeps_declared_node_role_schema_and_checkpoint_identity(tmp_path):
    stage_id = "custom-coverage"
    role_id = "research-synthesizer"
    output_schema = "ot://openthesis/agent-artifact/1"
    provider = RecordingCoverageProvider(max_input_bytes=None)
    storage, run, workflow, context, prior = _coverage_scenario(tmp_path, provider)
    context_json = context.compact_json()
    max_input_bytes = _agent_input_size(
        stage_id,
        workflow.pack.prompt(_SYNTHESIS_PROMPT),
        workflow.report_language,
        context_json,
        prior,
        role_id=role_id,
        output_schema=output_schema,
    ) - 1
    provider.max_input_bytes = max_input_bytes

    try:
        workflow._run_agent_adaptive(
            stage_id,
            _SYNTHESIS_PROMPT,
            context_json,
            prior,
            run=run,
            role_id=role_id,
            output_schema=output_schema,
        )
    except SynthesisContextLimitError as error:
        assert error.available_bytes == max_input_bytes
        assert error.required_bytes > max_input_bytes
    assert provider.requests
    assert all(item["bytes"] <= max_input_bytes for item in provider.requests)
    coverage_requests = [
        wire for wire in provider.requests if wire["payload"].get("target_agent")
    ]
    assert coverage_requests
    for wire in coverage_requests:
        payload = wire["payload"]
        assert payload["target_agent"] == stage_id
        assert payload["target_role"] == role_id
        assert payload["output_schema"] == output_schema

    checkpoints = _coverage_checkpoints(storage, run)
    assert checkpoints
    assert all(item["content"]["agent_id"] == stage_id for item in checkpoints)
