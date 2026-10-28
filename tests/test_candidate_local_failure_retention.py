import pytest

from openthesis.research_execution.checkpoints import completed_checkpoint, read_checkpoints
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec


@pytest.mark.parametrize("lane", ["preparation", "durable_verification"])
def test_received_body_survives_local_failure_and_restart_without_generation(lane):
    registry = StageRegistry([StageSpec("base", "1", "generate", "verify", output_contract="body")])
    snapshots, calls = [], []
    candidate = {"body": "Complete paid candidate remains intact."}
    def fail(*_args):
        raise ValueError("fixture-only-local-failure")
    first = StageExecutor({"generate": lambda *_: calls.append(1) or candidate},
        {"verify": lambda *_: StageValidation(True, candidate, "completed_verified")},
        checkpoint=lambda result: snapshots.append({"artifact_type": "stage-checkpoint", "content": completed_checkpoint(result)}),
        candidate_preparer=fail if lane == "preparation" else None,
        candidate_verifier=fail if lane == "durable_verification" else None).execute(registry, {})["base"]
    assert first.has_candidate and first.candidate == candidate and first.validation.state == "needs_action"
    assert not first.validation.trusted_output and calls == [1]
    read = read_checkpoints(snapshots, registry)
    restored = StageExecutor({"generate": fail},
        {"verify": lambda *_: StageValidation(True, candidate, "completed_verified")}).execute(
            registry, {}, local_only=True, previous=read.results, revalidation_candidates=read.revalidation_candidates)["base"]
    assert restored.validation.passed and restored.validation.trusted_output == candidate
    assert calls == [1]
