from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from openthesis.research_execution.executor import StageExecutor, StageExecutionCancelled, StageResult, StageValidation
from openthesis.resilience.contracts import digest
from openthesis.research_execution.registry import StageRegistry, StageSpec


def graph():
    return StageRegistry((StageSpec("a", "1", "model", "verify", output_contract="analysis"),
                          StageSpec("b", "1", "model", "verify", output_contract="analysis"),
                          StageSpec("final", "1", "model", "verify", ("a", "b"), output_contract="report")))


def verified(_spec, candidate, _inputs):
    return StageValidation(True, candidate, "completed_verified")


def test_start_resume_and_invalidated_descendants_use_identical_executor():
    calls, committed = [], []
    def produce(spec, inputs, dependencies):
        calls.append(spec.stage_id)
        return {"stage": spec.stage_id, "deps": dependencies, "source": inputs["source"]}
    executor = StageExecutor({"model": produce}, {"verify": verified}, checkpoint=committed.append)
    first = executor.execute(graph(), {"source": "frozen"}, max_workers=2)
    assert set(calls) == {"a", "b", "final"}
    calls.clear()
    replay = executor.execute(graph(), {"source": "frozen"}, previous=first)
    assert calls == []
    assert replay["final"].candidate == first["final"].candidate
    executor.execute(graph(), {"source": "frozen"}, previous=first, invalidated=("a",))
    assert calls == ["a", "final"]


def test_failed_sibling_is_preserved_and_downstream_never_runs_with_missing_dependency():
    calls = []
    def produce(spec, _inputs, _dependencies):
        calls.append(spec.stage_id)
        if spec.stage_id == "a":
            raise RuntimeError("provider secret must not be logged")
        return {"value": "safe"}
    result = StageExecutor({"model": produce}, {"verify": verified}).execute(graph(), {}, max_workers=2)
    assert set(calls) == {"a", "b"}
    assert result["b"].validation.passed
    assert result["a"].error_code == "STAGE_INTERNAL_RuntimeError"
    assert result["final"].error_code == "STAGE_DEPENDENCY_UNAVAILABLE"


def test_validator_failure_cannot_publish_candidate_or_reuse_false_success():
    def reject(_spec, _candidate, _inputs):
        return StageValidation(False, state="failed_verification", issues=("missing_evidence",))
    result = StageExecutor({"model": lambda *_args: {"unproven": 42}}, {"verify": reject}).execute(graph(), {})
    assert not result["a"].usable
    assert result["a"].validation.trusted_output == {}
    assert result["final"].candidate == {}


def test_changed_input_or_prompt_cannot_reuse_checkpoint():
    calls = []
    executor = StageExecutor({"model": lambda spec, *_args: calls.append(spec.stage_id) or {"value": 1}}, {"verify": verified})
    first = executor.execute(graph(), {"source": 1})
    calls.clear()
    executor.execute(graph(), {"source": 2}, previous=first)
    assert calls == ["a", "b", "final"]


def test_execution_configuration_change_keeps_proven_siblings_but_audits_new_attempt():
    calls = []
    executor = StageExecutor({"model": lambda spec, *_args: calls.append(spec.stage_id) or {"value": 1}}, {"verify": verified})
    first = executor.execute(graph(), {"source": "original"}, execution_fingerprint="old-model-config")
    calls.clear()
    continued = executor.execute(graph(), {"source": "original"}, previous=first,
                                 invalidated=("a",), execution_fingerprint="new-model-config")
    assert calls == ["a", "final"]
    assert continued["b"].execution_fingerprint == "old-model-config"
    assert continued["a"].execution_fingerprint == "new-model-config"
    assert continued["final"].execution_fingerprint == "new-model-config"


def test_pending_candidate_after_crash_is_verified_without_a_second_producer_call():
    calls = []
    executor = StageExecutor({"model": lambda spec, *_args: calls.append(spec.stage_id) or {"value": 1}}, {"verify": verified})
    first = executor.execute(graph(), {"source": "original"})
    candidate = first["a"]
    pending = StageResult(candidate.stage_id, candidate.stage_identity, candidate.input_digest,
                          candidate.candidate, StageValidation(False, state="needs_action"),
                          candidate.candidate_digest, has_candidate=True)
    calls.clear()
    replay = executor.execute(graph(), {"source": "original"}, previous={**first, "a": pending})
    assert calls == []
    assert replay["a"].validation.passed


def test_rejected_durable_candidate_does_not_trigger_an_implicit_regeneration():
    calls = []
    first = StageExecutor({"model": lambda *_args: {"value": 1}}, {"verify": verified}).execute(graph(), {})
    executor = StageExecutor({"model": lambda spec, *_args: calls.append(spec.stage_id) or {"value": 2}},
                             {"verify": lambda *_args: StageValidation(False, state="failed_verification")})
    replay = executor.execute(graph(), {}, previous=first)
    assert calls == []
    assert replay["a"].error_code == "STAGE_CANDIDATE_REJECTED"
    assert replay["final"].error_code == "STAGE_DEPENDENCY_UNAVAILABLE"


def test_cancel_after_producer_returns_never_checkpoints_late_success():
    cancelled = threading.Event()
    committed = []
    def produce(*_args):
        cancelled.set()
        return {"late": True}
    executor = StageExecutor({"model": produce}, {"verify": verified},
                             checkpoint=committed.append, cancel_check=cancelled.is_set)
    with pytest.raises(StageExecutionCancelled):
        executor.execute(graph(), {})
    assert committed == []


def test_unregistered_handler_and_inconsistent_validator_state_are_refused_before_call():
    with pytest.raises(ValueError, match="NOT_REGISTERED"):
        StageExecutor({}, {}).execute(graph(), {})
    with pytest.raises(ValueError, match="INCONSISTENT"):
        StageValidation(False, {"value": 1}, "completed_verified")


def test_verifier_exception_keeps_durable_candidate_and_healthy_sibling_for_resume():
    produced, journal = [], []
    verifier_broken = True

    def produce(spec, *_args):
        produced.append(spec.stage_id)
        return {"stage": spec.stage_id, "value": "source"}

    def verify(spec, candidate, inputs):
        if spec.stage_id == "a" and verifier_broken:
            raise RuntimeError("secret error text must not enter checkpoint")
        return verified(spec, candidate, inputs)

    executor = StageExecutor({"model": produce}, {"verify": verify}, checkpoint=journal.append)
    first = executor.execute(graph(), {}, max_workers=2)
    assert first["a"].has_candidate
    assert first["a"].candidate == {"stage": "a", "value": "source"}
    assert first["a"].error_code == "STAGE_VERIFIER_FAILED"
    assert not first["a"].usable
    assert first["b"].usable
    assert first["final"].error_code == "STAGE_DEPENDENCY_UNAVAILABLE"
    assert "secret error text" not in str(journal)

    verifier_broken = False
    produced.clear()
    replay = executor.execute(graph(), {}, previous=first)
    assert produced == ["final"]
    assert replay["a"].usable and replay["b"].usable and replay["final"].usable


def test_exception_during_revalidation_does_not_abort_the_other_stages_or_reproduce():
    produced = []
    first = StageExecutor({"model": lambda *_args: {"value": "source"}},
                          {"verify": verified}).execute(graph(), {})

    def broken(spec, candidate, inputs):
        if spec.stage_id == "a":
            raise ValueError("bad verifier")
        return verified(spec, candidate, inputs)

    replay = StageExecutor({"model": lambda spec, *_args: produced.append(spec.stage_id) or {}},
                           {"verify": broken}).execute(graph(), {}, previous=first)
    assert produced == []
    assert replay["a"].has_candidate and not replay["a"].usable
    assert replay["b"].usable
    assert replay["final"].error_code == "STAGE_DEPENDENCY_UNAVAILABLE"


def test_changed_prompt_bytes_invalidate_only_that_stage_and_descendants():
    source_graph = StageRegistry(replace(spec, prompt_path=f"{spec.stage_id}.md") for spec in graph().stages)
    prompts = {f"{spec.stage_id}.md": f"original {spec.stage_id}" for spec in source_graph.stages}
    calls = []
    executor = StageExecutor({"model": lambda spec, *_args: calls.append(spec.stage_id) or {"value": "source"}},
                             {"verify": verified})
    first_registry = source_graph.bind_prompts(prompts, shared_instructions="core instructions")
    first = executor.execute(first_registry, {})
    calls.clear()
    changed = source_graph.bind_prompts({**prompts, "a.md": "updated risk instructions"},
                                       shared_instructions="core instructions")
    replay = executor.execute(changed, {}, previous=first)
    assert calls == ["a", "final"]
    assert replay["b"].stage_identity == first["b"].stage_identity
    assert changed.get("a").identity != first_registry.get("a").identity
    assert source_graph.get("a").prompt_sha256 == ""
