import json
import pytest
from test_assertion_retention_312 import setup, Provider, validator
from openthesis.research_assertions import validate_with_assertions
from openthesis.research_execution.call_budget import plan_status, extend_plan
from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
from openthesis.model_scheduler import ModelRequestScheduler
from openthesis.providers import ProviderError


def test_budget_approval_resumes_only_exact_unsubmitted_gap(tmp_path):
    journal = setup(tmp_path)
    provider = Provider()
    executor = ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal)
    validate = validator(["Equipment sales are the core operating activity."])
    with journal.store.connect() as db:
        plan = json.loads(db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (journal.run_id,)).fetchone()[0])
        plan["max_calls"] = 0
        db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (json.dumps(plan), journal.run_id))
    with journal.bind("business", 1):
        first = validate_with_assertions(validate, stage_id="business", journal=journal, model_executor=executor, allow_network=True)
    assert not provider.prompts
    assert first.record["assertion_flow"]["supported"] == 0
    with journal.store.connect() as db:
        status = plan_status(db, journal.run_id)
        assert len(status["pending_budget_gaps"]) == 1
        extend_plan(db, journal.run_id, status["plan_digest"], [status["pending_budget_gaps"][0]["gap_id"]])
    journal.budget_only = True
    with journal.bind("business", 1):
        second = validate_with_assertions(validate, stage_id="business", journal=journal, model_executor=executor, allow_network=True)
        with pytest.raises(ProviderError, match="approved"):
            executor.execute(ModelStageRequest("different", "system", "new unapproved work"))
    assert len(provider.prompts) == 1
    assert second.record["assertion_flow"]["supported"] == 1
