from copy import deepcopy

from openthesis.research_execution.checkpoints import candidate_checkpoint, completed_checkpoint, read_checkpoints
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec


def registry():
    return StageRegistry((StageSpec("analysis", "1", "produce", "verify", output_contract="analysis"),))


def artifact(payload, identity="artifact-1", kind="stage-checkpoint"):
    return {"artifact_id": identity, "artifact_type": kind, "content": payload}


def test_persisted_candidate_is_reverified_without_another_call_after_restart():
    journal, calls = [], []
    first = StageExecutor({"produce": lambda *_args: calls.append(True) or {"claim": "source"}},
                          {"verify": lambda _spec, output, _inputs: StageValidation(True, output, "completed_verified")},
                          candidate_checkpoint=lambda spec, key, output: journal.append(
                              artifact(candidate_checkpoint(spec, key, output), kind="stage-candidate")))
    first.execute(registry(), {"source": "frozen"})
    saved = read_checkpoints(journal, registry())
    assert saved.issues == ()
    assert not saved.results["analysis"].usable
    resumed = first.execute(registry(), {"source": "frozen"}, previous=saved.results)
    assert calls == [True]
    assert resumed["analysis"].validation.passed


def test_completed_checkpoint_round_trip_is_detached_and_preserves_identity():
    result = StageExecutor({"produce": lambda *_args: {"nested": {"value": "source"}}},
                           {"verify": lambda _spec, output, _inputs: StageValidation(True, output, "completed_verified")}
                           ).execute(registry(), {})["analysis"]
    payload = completed_checkpoint(result)
    saved = read_checkpoints([artifact(payload)], registry()).results["analysis"]
    payload["result"]["candidate"]["nested"]["value"] = "changed"
    assert saved.candidate["nested"]["value"] == "source"
    assert saved.input_digest == result.input_digest


def test_bad_and_legacy_checkpoint_metadata_is_not_promoted_to_reusable_success():
    result = StageExecutor({"produce": lambda *_args: {"value": "source"}},
                           {"verify": lambda _spec, output, _inputs: StageValidation(True, output, "completed_verified")}
                           ).execute(registry(), {})["analysis"]
    payload = completed_checkpoint(result)
    changed = deepcopy(payload)
    changed["result"]["candidate"]["value"] = "tampered"
    coercion = deepcopy(payload)
    coercion["result"]["has_candidate"] = "true"
    saved = read_checkpoints([artifact(changed, "changed"), artifact(coercion, "coerced"),
                              artifact({"stage_id": "analysis", "candidate": {"value": "old"}}, "legacy")], registry())
    assert saved.results == {}
    assert len(saved.issues) == 3


def test_latest_invalid_candidate_blocks_silent_fallback_to_older_success():
    result = StageExecutor({"produce": lambda *_args: {"value": "old-source"}},
                           {"verify": lambda _spec, output, _inputs: StageValidation(True, output, "completed_verified")}
                           ).execute(registry(), {})["analysis"]
    prior = completed_checkpoint(result)
    latest = deepcopy(prior)
    latest["result"]["candidate"]["value"] = "changed-but-not-rehashed"
    saved = read_checkpoints([artifact(prior, "old-success"),
                              artifact(latest, "latest-candidate", kind="stage-candidate")], registry())
    assert "analysis" not in saved.results
    assert saved.issues == ({"artifact_id": "latest-candidate",
                             "code": "STAGE_CHECKPOINT_CANDIDATE_CHANGED"},)


def test_healthy_new_checkpoint_supersedes_an_earlier_invalid_attempt():
    result = StageExecutor({"produce": lambda *_args: {"value": "source"}},
                           {"verify": lambda _spec, output, _inputs: StageValidation(True, output, "completed_verified")}
                           ).execute(registry(), {})["analysis"]
    valid = completed_checkpoint(result)
    saved = read_checkpoints([artifact({"stage_id": "analysis", "candidate": {}}, "legacy"),
                              artifact(valid, "new-success")], registry())
    assert saved.results["analysis"].usable
    assert len(saved.issues) == 1
