"""Coverage checkpoints must be reused only for the model source that made them."""
from __future__ import annotations

from dataclasses import replace

import pytest

from openthesis.research import SynthesisContextLimitError
from openthesis.research_coverage import OBLIGATIONS
from test_synthesis_capacity_preflight import (
    RecordingCoverageProvider,
    _coverage_checkpoints,
    _scenario,
)


_SYNTHESIS_STAGE = "research-synthesizer"
_SYNTHESIS_PROMPT = "prompts/research-synthesizer.md"
_MAX_INPUT_BYTES = 7_424


def _capacity_failure(workflow, context, prior, run):
    with pytest.raises(SynthesisContextLimitError) as caught:
        workflow._run_agent_adaptive(
            _SYNTHESIS_STAGE,
            _SYNTHESIS_PROMPT,
            context.compact_json(),
            prior,
            run=run,
        )
    error = caught.value
    assert error.available_bytes == _MAX_INPUT_BYTES
    assert error.sections == [{
        "name": f"{_SYNTHESIS_STAGE}:minimum-coverage-merge",
        "bytes": error.required_bytes,
    }]
    assert error.required_bytes > _MAX_INPUT_BYTES
    return error


@pytest.mark.parametrize(
    ("configuration_change", "expected_model_id"),
    [
        ({"configuration_version": 2}, "gateway:test.capacity-preflight@2"),
        ({"configured_model_id": "test.other-model"}, "gateway:test.other-model@1"),
    ],
    ids=("configuration-version", "configured-model-id"),
)
def test_coverage_checkpoints_are_reused_only_for_same_model_source(
    tmp_path, configuration_change, expected_model_id
):
    provider = RecordingCoverageProvider(max_input_bytes=_MAX_INPUT_BYTES)
    storage, run, workflow, context, prior = _scenario(tmp_path, provider)
    initial_model_id = workflow.model_config.public_id

    first_error = _capacity_failure(workflow, context, prior, run)
    first_requests = list(provider.requests)
    assert first_requests
    first_partition_ids = [
        item["payload"]["partition"]["sha256"] for item in first_requests
    ]
    original_checkpoints = _coverage_checkpoints(storage, run)
    assert original_checkpoints
    original_ids = {item["artifact_id"] for item in original_checkpoints}
    original_material_ids = {
        item["content"]["input_sha256"] for item in original_checkpoints
    }
    assert {item["model_id"] for item in original_checkpoints} == {initial_model_id}
    original_snapshots = {
        item["artifact_id"]: {
            "content": item["content"],
            "model_id": item["model_id"],
            "agent_id": item["agent_id"],
            "created_at": item["created_at"],
        }
        for item in original_checkpoints
    }

    repeated_error = _capacity_failure(workflow, context, prior, run)
    assert repeated_error.required_bytes == first_error.required_bytes
    assert provider.requests == first_requests
    assert {item["artifact_id"] for item in _coverage_checkpoints(storage, run)} == original_ids

    workflow.model_config = replace(workflow.model_config, **configuration_change)
    assert workflow.model_config.public_id == expected_model_id
    assert workflow.model_config.public_id != initial_model_id
    changed_error = _capacity_failure(workflow, context, prior, run)
    assert changed_error.required_bytes == first_error.required_bytes

    new_requests = provider.requests[len(first_requests):]
    assert len(new_requests) == len(first_requests)
    assert [item["payload"]["partition"]["sha256"] for item in new_requests] == first_partition_ids

    checkpoints_after_change = _coverage_checkpoints(storage, run)
    checkpoints_by_id = {item["artifact_id"]: item for item in checkpoints_after_change}
    assert original_ids.issubset(checkpoints_by_id)
    for artifact_id, snapshot in original_snapshots.items():
        current = checkpoints_by_id[artifact_id]
        assert current["content"] == snapshot["content"]
        assert current["model_id"] == snapshot["model_id"]
        assert current["agent_id"] == snapshot["agent_id"]
        assert current["created_at"] == snapshot["created_at"]

    new_checkpoints = [
        item for item in checkpoints_after_change if item["artifact_id"] not in original_ids
    ]
    assert len(new_checkpoints) == len(first_requests)
    assert {item["model_id"] for item in new_checkpoints} == {expected_model_id}
    assert {
        item["content"]["input_sha256"] for item in new_checkpoints
    } == original_material_ids


def test_coverage_checkpoints_bind_the_exact_synthesis_prompt(tmp_path, monkeypatch):
    provider = RecordingCoverageProvider(max_input_bytes=_MAX_INPUT_BYTES)
    storage, run, workflow, context, prior = _scenario(tmp_path, provider)
    prompt_type = type(workflow.pack)
    original_prompt_method = prompt_type.prompt
    original_prompt = workflow.pack.prompt(_SYNTHESIS_PROMPT)
    marker = "executive_summary"
    assert marker in original_prompt
    changed_prompt = original_prompt.replace(marker, "Executive_summary", 1)
    assert changed_prompt != original_prompt
    assert len(changed_prompt.encode("utf-8")) == len(original_prompt.encode("utf-8"))

    first_error = _capacity_failure(workflow, context, prior, run)
    first_requests = list(provider.requests)
    assert len(first_requests) == len(OBLIGATIONS)
    first_partition_ids = [
        item["payload"]["partition"]["sha256"] for item in first_requests
    ]
    original_checkpoints = _coverage_checkpoints(storage, run)
    assert len(original_checkpoints) == len(OBLIGATIONS)
    original_ids = {item["artifact_id"] for item in original_checkpoints}
    original_hashes = {
        item["content"]["coverage_request_sha256"] for item in original_checkpoints
    }
    assert original_hashes and all(original_hashes)
    original_snapshots = {
        item["artifact_id"]: {
            "content": item["content"],
            "model_id": item["model_id"],
            "agent_id": item["agent_id"],
            "created_at": item["created_at"],
        }
        for item in original_checkpoints
    }

    repeated_error = _capacity_failure(workflow, context, prior, run)
    assert repeated_error.required_bytes == first_error.required_bytes
    assert provider.requests == first_requests

    def prompt_with_changed_case(pack, relative_path):
        prompt = original_prompt_method(pack, relative_path)
        if relative_path == _SYNTHESIS_PROMPT:
            return prompt.replace(marker, "Executive_summary", 1)
        return prompt

    monkeypatch.setattr(prompt_type, "prompt", prompt_with_changed_case)
    actual_changed_prompt = workflow.pack.prompt(_SYNTHESIS_PROMPT)
    assert actual_changed_prompt == changed_prompt
    assert actual_changed_prompt != original_prompt
    assert len(actual_changed_prompt.encode("utf-8")) == len(original_prompt.encode("utf-8"))

    changed_error = _capacity_failure(workflow, context, prior, run)
    assert changed_error.required_bytes == first_error.required_bytes
    new_requests = provider.requests[len(first_requests):]
    assert len(new_requests) == len(first_requests)
    assert [item["payload"]["partition"]["sha256"] for item in new_requests] == first_partition_ids

    checkpoints_after_change = _coverage_checkpoints(storage, run)
    checkpoints_by_id = {item["artifact_id"]: item for item in checkpoints_after_change}
    assert original_ids.issubset(checkpoints_by_id)
    for artifact_id, snapshot in original_snapshots.items():
        current = checkpoints_by_id[artifact_id]
        assert current["content"] == snapshot["content"]
        assert current["model_id"] == snapshot["model_id"]
        assert current["agent_id"] == snapshot["agent_id"]
        assert current["created_at"] == snapshot["created_at"]

    new_checkpoints = [
        item for item in checkpoints_after_change if item["artifact_id"] not in original_ids
    ]
    assert len(new_checkpoints) == len(first_requests)
    new_hashes = {
        item["content"]["coverage_request_sha256"] for item in new_checkpoints
    }
    assert new_hashes and all(new_hashes)
    assert original_hashes.isdisjoint(new_hashes)


def test_legacy_coverage_checkpoints_without_request_proof_are_recomputed(tmp_path):
    donor_provider = RecordingCoverageProvider(max_input_bytes=_MAX_INPUT_BYTES)
    donor_storage, donor_run, donor_workflow, donor_context, donor_prior = _scenario(
        tmp_path / "donor", donor_provider
    )
    _capacity_failure(donor_workflow, donor_context, donor_prior, donor_run)
    donor_checkpoints = _coverage_checkpoints(donor_storage, donor_run)
    assert len(donor_checkpoints) == len(OBLIGATIONS)
    donor_partition_ids = [
        item["payload"]["partition"]["sha256"] for item in donor_provider.requests
    ]
    donor_snapshots = {
        item["artifact_id"]: {
            "content": item["content"],
            "model_id": item["model_id"],
            "agent_id": item["agent_id"],
            "created_at": item["created_at"],
        }
        for item in donor_checkpoints
    }

    receiver_provider = RecordingCoverageProvider(max_input_bytes=_MAX_INPUT_BYTES)
    receiver_storage, receiver_run, receiver_workflow, receiver_context, receiver_prior = (
        _scenario(tmp_path / "receiver", receiver_provider)
    )
    for checkpoint in donor_checkpoints:
        legacy_content = dict(checkpoint["content"])
        legacy_content.pop("coverage_request_sha256", None)
        receiver_workflow._save(
            receiver_run,
            checkpoint["artifact_type"],
            checkpoint["title"],
            legacy_content,
            agent_id=checkpoint["agent_id"],
        )

    imported_checkpoints = _coverage_checkpoints(receiver_storage, receiver_run)
    assert len(imported_checkpoints) == len(donor_checkpoints)
    assert all(
        "coverage_request_sha256" not in item["content"]
        for item in imported_checkpoints
    )
    imported_ids = {item["artifact_id"] for item in imported_checkpoints}
    imported_snapshots = {
        item["artifact_id"]: {
            "content": item["content"],
            "model_id": item["model_id"],
            "agent_id": item["agent_id"],
            "created_at": item["created_at"],
        }
        for item in imported_checkpoints
    }

    _capacity_failure(receiver_workflow, receiver_context, receiver_prior, receiver_run)
    assert len(receiver_provider.requests) == len(OBLIGATIONS)
    assert [
        item["payload"]["partition"]["sha256"] for item in receiver_provider.requests
    ] == donor_partition_ids

    receiver_checkpoints = _coverage_checkpoints(receiver_storage, receiver_run)
    receiver_by_id = {item["artifact_id"]: item for item in receiver_checkpoints}
    assert imported_ids.issubset(receiver_by_id)
    for artifact_id, snapshot in imported_snapshots.items():
        current = receiver_by_id[artifact_id]
        assert current["content"] == snapshot["content"]
        assert current["model_id"] == snapshot["model_id"]
        assert current["agent_id"] == snapshot["agent_id"]
        assert current["created_at"] == snapshot["created_at"]
    new_checkpoints = [
        item for item in receiver_checkpoints if item["artifact_id"] not in imported_ids
    ]
    assert len(new_checkpoints) == len(OBLIGATIONS)
    assert all(item["content"].get("coverage_request_sha256") for item in new_checkpoints)

    donor_after = _coverage_checkpoints(donor_storage, donor_run)
    donor_after_by_id = {item["artifact_id"]: item for item in donor_after}
    assert set(donor_snapshots).issubset(donor_after_by_id)
    for artifact_id, snapshot in donor_snapshots.items():
        current = donor_after_by_id[artifact_id]
        assert current["content"] == snapshot["content"]
        assert current["model_id"] == snapshot["model_id"]
        assert current["agent_id"] == snapshot["agent_id"]
        assert current["created_at"] == snapshot["created_at"]
