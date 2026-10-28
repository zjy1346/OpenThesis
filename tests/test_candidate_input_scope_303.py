"""Local recovery is not permission to skip new sources or producer prompts."""
from dataclasses import replace

import pytest

from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec


@pytest.mark.parametrize("change", ["source", "prompt", "dependency"])
def test_automatic_resume_cannot_reproject_an_obsolete_generation(change):
    specs = (StageSpec("root", "1", "read", "check", output_contract="analysis"),
             StageSpec("consumer", "1", "read", "check", ("root",), output_contract="analysis"))
    registry = StageRegistry(specs)
    calls = []
    def produce(spec, inputs, deps):
        calls.append(spec.stage_id)
        return {"body": inputs["source"], "dependencies": deps}
    executor = StageExecutor({"read": produce}, {"check": lambda _s, body, _i:
        StageValidation(True, body, "completed_verified")})
    first = executor.execute(registry, {"source": "original-a"}, execution_fingerprint="configured-model")
    calls.clear()
    changed = {"source": "original-b" if change == "source" else "original-a"}
    if change == "prompt":
        registry = StageRegistry((replace(specs[0], prompt_sha256="a" * 64), specs[1]))
    result = executor.execute(registry, changed, previous=first, revalidation_candidates=first,
        execution_fingerprint="configured-model", invalidated=("root",) if change == "dependency" else (),
        propagate_invalidation=False)
    assert calls == (["root", "consumer"] if change != "dependency" else ["root"])
    assert result["root"].candidate["body"] == changed["source"]


def test_unchanged_received_body_and_explicit_local_projection_do_not_generate():
    registry = StageRegistry((StageSpec("root", "1", "read", "check", output_contract="analysis"),))
    calls = []
    executor = StageExecutor({"read": lambda _s, inputs, _d: calls.append(inputs) or {"body": inputs["source"]}},
        {"check": lambda _s, body, _i: StageValidation(True, body, "completed_verified")})
    first = executor.execute(registry, {"source": "original-a"})
    executor.execute(registry, {"source": "original-a"}, revalidation_candidates=first)
    executor.execute(registry, {"source": "original-b"}, revalidation_candidates=first, local_only=True)
    assert len(calls) == 1
