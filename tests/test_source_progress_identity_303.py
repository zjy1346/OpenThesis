"""Received output, completed stage and complete research are different states."""
from types import SimpleNamespace

from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow
from openthesis.service import _ResearchJob


def test_bound_source_risk_progress_cannot_mark_the_final_counter_stage_done():
    observed = []
    workflow = ResearchWorkflow(SimpleNamespace(), builtin_pack(), object(), ModelConfig("fixture"),
        agent_progress=lambda agent, state: observed.append((agent, state)))
    journal = SimpleNamespace(capture_scope=lambda: ("source-risk-analysis", 1))
    with workflow._bind_request_journal(journal):
        workflow._set_agent_state("skeptical-analyst", "running")
        workflow._set_agent_state("skeptical-analyst", "completed")
    assert observed == [("source-risk-analysis", "running"), ("source-risk-analysis", "validating")]
    workflow._set_agent_state("skeptical-analyst", "completed_verified")
    assert observed[-1] == ("skeptical-analyst", "completed_verified")


def test_stage_finish_count_does_not_claim_complete_research():
    job = _ResearchJob("fixture", agent_states={
        "business-analyst": "completed_verified", "source-industry-analysis": "completed_partial",
        "source-risk-analysis": "reused_verified", "skeptical-analyst": "validating", "other": "needs_action"})
    result = job.snapshot()
    assert result["completed_agents"] == 3 and result["total_agents"] == 5
    assert result["research_complete"] is False


def test_custom_ot_stage_progress_does_not_lookup_the_official_graph():
    observed = []
    workflow = ResearchWorkflow(SimpleNamespace(), builtin_pack(), object(), ModelConfig("fixture"),
        agent_progress=lambda agent, state: observed.append((agent, state)))
    with workflow._bind_request_journal(SimpleNamespace(capture_scope=lambda: ("custom-read", 1))):
        workflow._set_agent_state("custom-agent", "completed")
    assert observed == [("custom-agent", "validating")]
