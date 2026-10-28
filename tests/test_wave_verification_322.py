from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec


def test_ready_wave_is_checkpointed_before_one_shared_preparation_without_blocking_workers():
    saved, prepared, generated = [], [], []
    stages = [StageSpec(key, "v1", "model", "verify", output_contract="body-v1") for key in ("a", "b", "c")]
    stages.append(StageSpec("final", "v1", "model", "verify", ("a", "b", "c"), output_contract="body-v1"))
    def produce(spec, _inputs, _deps):
        generated.append(spec.stage_id)
        return {"body": spec.stage_id}
    def prepare(items):
        ids = [spec.stage_id for spec, _candidate, _inputs in items]
        assert all(key in saved for key in ids)
        prepared.append(ids)
    def validate(_spec, candidate, _inputs):
        return StageValidation(True, candidate, "completed_verified")
    executor = StageExecutor({"model": produce}, {"verify": validate},
        candidate_checkpoint=lambda spec, *_args: saved.append(spec.stage_id), candidate_wave_preparer=prepare)
    result = executor.execute(StageRegistry(stages), {}, max_workers=2)
    assert prepared == [["a", "b", "c"], ["final"]]
    assert sorted(generated) == ["a", "b", "c", "final"]
    assert all(item.validation.passed for item in result.values())


def test_wave_preparation_failure_retains_all_received_bodies_without_new_generation():
    generated = []
    def fail(_items):
        raise RuntimeError("private provider error")
    stages = StageRegistry([StageSpec(key, "v1", "model", "verify", output_contract="body-v1") for key in ("a", "b")])
    executor = StageExecutor({"model": lambda spec, *_: generated.append(spec.stage_id) or {"body": spec.stage_id}},
        {"verify": lambda *_: StageValidation(False, state="needs_action")}, candidate_wave_preparer=fail)
    result = executor.execute(stages, {}, max_workers=2)
    assert sorted(generated) == ["a", "b"]
    assert all(item.has_candidate and item.candidate == {"body": key} for key, item in result.items())
    assert all(item.error_code == "STAGE_WAVE_PREPARATION_FAILED" for item in result.values())
