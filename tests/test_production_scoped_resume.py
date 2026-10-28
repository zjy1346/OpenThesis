"""Scoped resume identities must match the same material and dependencies."""
import pytest

from openthesis.resilience.contracts import digest
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.production import stage_execution_plan
from openthesis.research_execution.registry import standard_model_registry


def _verified(spec, candidate, _inputs):
    return StageValidation(
        True,
        {"stage": spec.stage_id, "material": candidate["material"]},
        "completed_verified",
    )


def _executor(calls):
    def produce(spec, inputs, dependencies):
        calls.append(spec.stage_id)
        return {
            "stage": spec.stage_id,
            "material": inputs["material"],
            "dependency_stages": sorted(dependencies),
        }

    return StageExecutor({"model-analysis": produce}, {"research-evidence-v1": _verified,
        "growth-evidence-v1": _verified, "scenario-evidence-v1": _verified,
        "report-coverage-v1": _verified})


def _inputs(material="filing-set-a"):
    return {
        "material": material,
        "capabilities": [
            "company_identity", "quantitative_facts", "business_disclosures",
            "audit_disclosures", "mda_disclosures", "risk_disclosures",
            "sourced_disclosures",
        ],
    }


def test_final_synthesis_resume_preserves_compatible_independent_stages():
    registry = standard_model_registry()
    calls = []
    executor = _executor(calls)
    inputs = _inputs()
    saved = executor.execute(registry, inputs, execution_fingerprint="model-old")
    calls.clear()

    fingerprints, invalidated = stage_execution_plan(
        registry, saved, "model-new", ("final-synthesis",),
        scoped_resume=True, material_digest=digest(inputs),
    )
    resumed = executor.execute(
        registry, inputs, previous=saved, invalidated=invalidated,
        execution_fingerprints=fingerprints,
    )

    assert invalidated == ("final-synthesis",)
    assert calls == ["final-synthesis"]
    for stage_id, prior in saved.items():
        if stage_id == "final-synthesis":
            assert resumed[stage_id].execution_fingerprint == "model-new"
        else:
            assert resumed[stage_id].execution_fingerprint == "model-old"
            assert resumed[stage_id].candidate_digest == prior.candidate_digest


def test_growth_resume_reexecutes_its_downstream_but_not_independent_base_stages():
    registry = standard_model_registry()
    calls = []
    executor = _executor(calls)
    inputs = _inputs()
    saved = executor.execute(registry, inputs, execution_fingerprint="model-old")
    calls.clear()

    fingerprints, invalidated = stage_execution_plan(
        registry, saved, "model-current", ("growth-opportunities",),
        scoped_resume=True, material_digest=digest(inputs),
    )
    resumed = executor.execute(
        registry, inputs, previous=saved, invalidated=invalidated,
        execution_fingerprints=fingerprints,
    )

    assert invalidated == (
        "growth-opportunities", "counter-analysis", "forecast-scenarios", "final-synthesis",
    )
    assert calls == list(invalidated)
    for stage_id in ("financial-analyst", "business-analyst", "accounting-risk-analyst"):
        assert resumed[stage_id].execution_fingerprint == "model-old"
        assert resumed[stage_id].candidate_digest == saved[stage_id].candidate_digest
    for stage_id in invalidated:
        assert resumed[stage_id].execution_fingerprint == "model-current"


def test_changed_material_replans_every_stage_to_current_model_identity():
    registry = standard_model_registry()
    calls = []
    executor = _executor(calls)
    original_inputs = _inputs()
    saved = executor.execute(registry, original_inputs, execution_fingerprint="model-old")
    calls.clear()
    changed_inputs = _inputs("filing-set-b")

    fingerprints, invalidated = stage_execution_plan(
        registry, saved, "model-current", ("final-synthesis",),
        scoped_resume=True, material_digest=digest(changed_inputs),
    )
    resumed = executor.execute(
        registry, changed_inputs, previous=saved, invalidated=invalidated,
        execution_fingerprints=fingerprints,
    )

    assert set(fingerprints.values()) == {"model-current"}
    assert calls == [spec.stage_id for spec in registry.stages]
    assert all(result.execution_fingerprint == "model-current" for result in resumed.values())
    assert all(
        resumed[stage_id].input_digest != saved[stage_id].input_digest
        for stage_id in saved
    )
