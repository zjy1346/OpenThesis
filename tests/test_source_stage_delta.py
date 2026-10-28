"""Source-ready stages and financial deltas share one executor, not two runs."""
from openthesis.resilience.contracts import digest
from openthesis.research_execution.executor import StageExecutor, StageValidation, stage_input_digest
from openthesis.research_execution.registry import StageRegistry, StageSpec


def test_scoped_material_is_actual_input_and_financial_delta_does_not_repeat_business():
    registry = StageRegistry((StageSpec("business", "1", "model", "verify", output_contract="analysis"),
                              StageSpec("financial", "1", "model", "verify", output_contract="analysis"),
                              StageSpec("final", "1", "model", "verify", ("business", "financial"), output_contract="report")))
    calls, validations = [], []

    def produce(spec, inputs, dependencies):
        calls.append((spec.stage_id, inputs))
        return {"source": inputs["source"], "dependencies": sorted(dependencies)}

    def verify(spec, value, inputs):
        validations.append((spec.stage_id, inputs))
        return StageValidation(True, value, "completed_verified")

    executor = StageExecutor({"model": produce}, {"verify": verify})
    source = {"source": "official-original-pages"}
    first = executor.execute(registry, {}, stage_inputs={"business": source}, targets=("business",))
    assert [stage for stage, _ in calls] == ["business"]
    assert validations == calls
    calls.clear()
    full = {"source": "official-original-pages-and-admitted-financials"}
    result = executor.execute(registry, full, stage_inputs={"business": source}, previous=first)
    assert [stage for stage, _ in calls] == ["financial", "final"]
    assert result["business"].candidate_digest == first["business"].candidate_digest
    assert result["business"].input_digest == stage_input_digest(registry.get("business"), digest(source), "", {})
    assert result["financial"].input_digest == stage_input_digest(registry.get("financial"), digest(full), "", {})
    assert result["final"].validation.passed


def test_changed_sources_invalidate_business_even_with_same_financial_delta():
    registry = StageRegistry((StageSpec("business", "1", "model", "verify", output_contract="analysis"),))
    calls = []
    executor = StageExecutor({"model": lambda spec, inputs, deps: calls.append(inputs) or {"text": inputs["source"]}},
        {"verify": lambda spec, value, inputs: StageValidation(True, value, "completed_verified")})
    first = executor.execute(registry, {}, stage_inputs={"business": {"source": "original-a"}})
    executor.execute(registry, {}, stage_inputs={"business": {"source": "original-b"}}, previous=first)
    assert len(calls) == 2

