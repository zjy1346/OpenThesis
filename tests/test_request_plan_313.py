import json
import pytest
from test_assertion_retention_312 import setup
from openthesis.research_execution.call_budget import prepare_plan, reserve, record_gap, plan_status, extend_plan
from openthesis.providers import ProviderError


def test_role_request_plan_sums_real_requests_not_graph_bytes_times_slots(tmp_path):
    journal = setup(tmp_path)
    with journal.store.connect() as db:
        db.execute("DELETE FROM model_run_call_plans WHERE run_id=?", (journal.run_id,))
        prepare_plan(db, journal.run_id, ["business", "synthesis"], 100_000_000, request_plan=[
            {"stage_id": "business", "purpose": "generation", "input_estimate": 100, "output_limit": 500},
            {"stage_id": "synthesis", "purpose": "generation", "input_estimate": 200, "output_limit": 600},
            {"stage_id": "business", "purpose": "semantic_verification", "input_estimate": 80, "output_limit": 120}])
        plan = plan_status(db, journal.run_id)
        assert plan["schema"] == "research-call-plan-v4-adaptive"
        assert plan["policy"] == "adaptive"
        assert plan["workload_fits"] is True
        assert plan["max_calls"] is None
        assert plan["max_input_estimate"] is None
        assert plan["max_output_tokens"] is None
        assert plan["verification_batches"] == 1
        assert plan["initial_forecast"]["max_calls"] == 5
        assert plan["initial_forecast"]["max_input_estimate"] == 680
        prepare_plan(db, journal.run_id, ["business", "synthesis", "extra"], 9_000_000)
        assert plan_status(db, journal.run_id)["plan_digest"] == plan["plan_digest"], "restart cannot widen an existing run plan"


def test_budget_gap_requires_explicit_matching_user_approval_and_keeps_consumption(tmp_path):
    journal = setup(tmp_path)
    context = {"request_id": "request", "attempt_id": "first", "stage_id": "business", "generation": 1}
    with journal.store.connect() as db:
        plan = json.loads(db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (journal.run_id,)).fetchone()[0])
        plan["max_calls"] = 0
        db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (json.dumps(plan), journal.run_id))
    with pytest.raises(ProviderError):
        with journal.store.connect() as db:
            reserve(db, journal.run_id, context, purpose="semantic_verification", input_estimate=200, output_limit=300)
    with journal.store.connect() as db:
        record_gap(db, journal.run_id, context, "semantic_verification", 200, 300, "MODEL_RUN_CALL_BUDGET_EXHAUSTED")
        status = plan_status(db, journal.run_id)
    assert status["max_calls"] == 0 and len(status["pending_budget_gaps"]) == 1
    with pytest.raises(ValueError, match="PLAN_CHANGED"):
        with journal.store.connect() as db:
            extend_plan(db, journal.run_id, "0" * 64, [status["pending_budget_gaps"][0]["gap_id"]])
    with journal.store.connect() as db:
        updated = extend_plan(db, journal.run_id, status["plan_digest"], [status["pending_budget_gaps"][0]["gap_id"]])
    assert updated["max_calls"] == 1 and updated["pending_budget_gaps"] == []
    with journal.store.connect() as db:
        reserve(db, journal.run_id, context, purpose="semantic_verification", input_estimate=200, output_limit=300)
    with journal.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_call_reservations WHERE run_id=?", (journal.run_id,)).fetchone()[0] == 1


def test_planner_compiles_actual_stage_scope_assignments_and_trusted_dependencies():
    from types import SimpleNamespace
    from test_assertion_retention_312 import Provider
    from openthesis.research import _agent_prompt_bundle, _deduplicated_stage_context
    from openthesis.research_execution.executor import StageResult, StageValidation
    from openthesis.research_execution.input_budget import input_measurement
    from openthesis.research_execution.registry import StageRegistry, StageSpec
    from openthesis.research_execution.request_planning import RoleRequestMaterials, plan_role_requests
    base = StageSpec("source-business", "1", "model-analysis", "research-evidence-v1",
        role="business-analyst", prompt_path="business", output_contract="analysis")
    next_stage = StageSpec("growth", "1", "model-analysis", "growth-evidence-v1", depends_on=(base.stage_id,),
        role="growth-opportunity-analyst", prompt_path="growth", output_contract="growth")
    registry = StageRegistry([base, next_stage])
    source = {"evidence": [{"evidence_id": "filing:business", "excerpt": "Equipment sales and competitive risk."}]}
    narrow = {"context_json": json.dumps(source)}
    wide = {"context_json": json.dumps({**source, "facts": [{"fact_id": "unrelated-financial-lane"}]})}
    provider = Provider()
    workflow = SimpleNamespace(pack=SimpleNamespace(prompt=lambda path: "Instructions: " + path),
        report_language="en", _stage_output_budget=lambda agent: 1500 if agent == "business-analyst" else 2100,
        _stage_budget_provider=lambda agent: provider)
    trusted = {"summary": "Retain this verified analysis.", "raw_response": "private audit, not dispatch material"}
    saved = {base.stage_id: StageResult(base.stage_id, base.identity, "input",
        {"summary": "UNVERIFIED CANDIDATE MUST NOT ENTER PLAN"},
        StageValidation(True, trusted, "completed_verified"), "candidate")}
    seen = {}
    def materials(spec, frozen_inputs, dependencies):
        packet = RoleRequestMaterials(spec.role, frozen_inputs["context_json"],
            {"assigned_research_obligations": [{"obligation_id": "business_model"}], "dependency_outputs": dependencies})
        seen[spec.stage_id] = packet
        return packet
    plan = plan_role_requests(workflow, registry, wide, saved, stage_inputs={base.stage_id: narrow},
        request_materials=materials, reused_stage_ids=(base.stage_id,))
    packet = seen[next_stage.stage_id]
    assert packet.prior_artifacts["dependency_outputs"] == {base.stage_id: {"summary": "Retain this verified analysis."}}
    assert "facts" not in json.loads(seen[base.stage_id].context_json)
    canonical, _ = _deduplicated_stage_context(packet.context_json, packet.agent_id)
    expected = input_measurement(provider, *_agent_prompt_bundle(packet.agent_id, "Instructions: growth", "en",
        canonical, packet.prior_artifacts))
    generation = next(row for row in plan if row["purpose"] == "generation")
    assert generation["compiled_input_estimate"] == expected["tokens"]
    assert generation["input_bytes"] == expected["input_bytes"]
    assert generation["pending_dependency_output_estimate"] == 0
    assert generation["output_limit"] == 2100
