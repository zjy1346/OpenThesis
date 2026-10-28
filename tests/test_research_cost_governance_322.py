"""Real plan/prompt boundaries; no supplier calls or price assumptions."""
import json
from os.path import commonprefix
import pytest

from openthesis.research import _agent_prompt_bundle
from openthesis.research_execution.call_budget import prepare_plan, plan_status, reserve
from test_assertion_retention_312 import setup


def test_large_compiled_workload_remains_adaptive_and_retains_its_forecast(tmp_path):
    journal = setup(tmp_path)
    with journal.store.connect() as db:
        db.execute("DELETE FROM model_run_call_plans WHERE run_id=?", (journal.run_id,))
        prepare_plan(db, journal.run_id, ["business"], request_plan=[{
            "stage_id": "business", "purpose": "generation",
            "input_estimate": 2_000_000, "output_limit": 131_072,
        }])
        plan = plan_status(db, journal.run_id)
    assert plan["max_input_estimate"] is None
    assert plan["max_output_tokens"] is None
    assert plan["max_calls"] is None
    assert plan["user_hard_limits"]["max_calls"] is None
    assert plan["user_hard_limits"]["max_input_tokens"] is None
    assert plan["workload_fits"] is True
    assert plan["initial_forecast"]["max_calls"] == 3
    assert plan["initial_forecast"]["max_input_estimate"] == 4_000_000
    assert plan["initial_forecast"]["max_output_tokens"] == 135_168

    context = {"request_id": "large-workload", "attempt_id": "large-workload-1",
        "stage_id": "business", "generation": 1}
    with journal.store.connect() as db:
        reserve(db, journal.run_id, context, purpose="generation",
            input_estimate=2_000_000, output_limit=131_072)
    continued = journal.store.model_call_plan(journal.run_id)
    assert continued["consumption"]["calls"] == 1
    assert continued["forecast_exceeded"] == []
    assert continued["initial_forecast"]["max_input_estimate"] == 4_000_000


@pytest.mark.parametrize("audit_variant", range(8))
def test_dependency_source_supplement_does_not_reorder_the_shared_original_prefix(audit_variant):
    context = {"company": {"cik": "stable-322"}, "evidence": [
        {"evidence_id": "filing:business322", "topic": "business", "kind": "filing_text",
         "document_id": "official-b", "source_url": "https://official.example/business.pdf",
         "raw_text": "Complete business disclosure; retain risk qualifiers. " * 500},
        {"evidence_id": "filing:audit322", "topic": "audit", "kind": "filing_text",
         "document_id": "official-a", "source_url": "https://official.example/audit.pdf",
         "raw_text": f"Audit variant {audit_variant}. " + "Full audit disclosure and exceptions. " * 300},
    ]}
    kwargs = dict(agent_id="business-analyst", role_prompt="Review the original sources.",
                  report_language="en", context_json=json.dumps(context))
    _, before = _agent_prompt_bundle(**kwargs, prior_artifacts={})
    _, after = _agent_prompt_bundle(**kwargs, prior_artifacts={"audit_dependency": {
        "analysis": "A new audit question.", "evidence_ids": ["filing:audit322"]}})
    # Necessary late evidence must be supplied, but cannot move ahead of the
    # already supplied 26KB business source and destroy its reusable prefix.
    assert len(commonprefix([before, after]).encode("utf-8")) > 20_000
    assert "Full audit disclosure and exceptions." in after
