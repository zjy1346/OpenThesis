"""Durable generation bodies are distinct from auxiliary model judgments."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from openthesis.research_execution.production import _local_journal_candidates
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.model import ModelStageRequest
from openthesis.research_content_flow import stage_flow, report_flow
from openthesis.research import _supported_synthesis_sections
from openthesis.research_assertions import AssertionSession, assertion_session, validate_response
from openthesis.research_section_validation import _labelled_nonfactual_text
from test_model_request_journal import _store, _journal, _registry
from test_source_bound_assertions import record, review


def test_restart_selects_generation_not_more_recent_semantic_response(tmp_path):
    store = _store(tmp_path)
    clock = [1000.0]
    journal = _journal(store, clock=lambda: clock[0])
    registry = _registry(("analysis", ()))
    generation = journal.prepare(registry, "sources")["analysis"]
    request = ModelStageRequest("analysis", "system", "research")
    body = {"business_model": "Complete research candidate"}
    journal.execute(request, lambda: body, ("analysis", generation))
    clock[0] += 1
    journal.execute(replace(request, user_prompt="verify", purpose="semantic_verification"),
        lambda: {"results": []}, ("analysis", generation))
    restored = _journal(store)
    assert restored.saved_generation_candidates()["analysis"]["candidate"] == body
    candidates = _local_journal_candidates(restored, registry, {})
    assert not candidates["analysis"].validation.trusted_output
    local = StageExecutor({"handler": lambda *_: pytest.fail("no wire")},
        {"validator": lambda _s, candidate, _i: StageValidation(True, candidate, "completed_verified")})
    result = local.execute(registry, {}, local_only=True, revalidation_candidates=candidates)["analysis"]
    assert result.candidate == body and result.validation.trusted_output == body
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_request_journal").fetchone()[0] == 2


def test_legacy_purpose_and_corrupt_latest_never_guess_an_older_body(tmp_path):
    store = _store(tmp_path)
    clock = [1000.0]
    journal = _journal(store, clock=lambda: clock[0])
    generation = journal.prepare(_registry(("analysis", ())), "sources")["analysis"]
    first = ModelStageRequest("analysis", "system", "first")
    journal.execute(first, lambda: {"body": "old"}, ("analysis", generation))
    clock[0] += 1
    second = replace(first, user_prompt="latest")
    journal.execute(second, lambda: {"body": "latest"}, ("analysis", generation))
    with store.connect() as db:
        db.execute("UPDATE model_request_journal SET candidate_json='broken' WHERE input_digest=?", (second.input_digest,))
    restored = _journal(store)
    assert restored.saved_generation_candidates() == {}
    assert restored.candidate_read_issues[0]["code"] == "MODEL_REQUEST_CANDIDATE_CORRUPT"
    with store.connect() as db:
        db.execute("UPDATE model_request_journal SET purpose='' ")
    assert _journal(store).saved_generation_candidates() == {}


def test_observability_counts_received_body_and_path_reductions_not_complete_score(tmp_path):
    registry = _registry(("analysis", ()))
    body = {"business_model": {"text": "Complete research", "evidence_ids": ["filing:one"]},
        "extra_business_field": "Retained extension"}
    result = StageExecutor({"handler": lambda *_: body},
        {"validator": lambda *_: StageValidation(False, state="needs_action",
            record={"proof": {"schema": "evidence-bound-sections-v8-assertions", "nodes": [],
                "content_flow": {"reductions": [{"code": "pending", "path": "business_model.text"}]}}})}).execute(registry, {})["analysis"]
    flow = stage_flow(result, [{"evidence_id": "filing:one"}], generation=1,
        request_rows=[{"response_received": 1, "state": "candidate"}])
    assert flow["received_responses"] == 1 and flow["candidate_available"]
    assert flow["parsed_leaf_count"] == 2 and flow["accepted_nodes"] == 0
    assert {"code": "pending", "path": "business_model.text"} in flow["reductions"]
    assert {"code": "STAGE_CANDIDATE_REJECTED", "path": "stage:analysis"} in flow["reductions"]
    final = report_flow([{"artifact_type": "stage-content-flow", "content": flow}],
        [{"content_semantics": [{"role": "notice", "validation_state": "verified"},
            {"role": "verified_analysis", "validation_state": "verified"}]}])
    assert final["presented_semantic_nodes"] == 1 and "completeness" not in final


def test_scalar_summary_dependency_is_collected_in_one_batch_without_raw_trust():
    source = record("The company manufactures industrial refrigeration equipment.")
    text = "The company's core activity is industrial cooling equipment manufacturing."
    candidate = {"executive_summary": text, "claims": [{"claim_id": "claim:core", "text": text,
        "kind": "inference", "evidence_ids": [source["evidence_id"]]}]}
    def validate():
        return _supported_synthesis_sections(candidate, {source["evidence_id"]}, {source["evidence_id"]: source}, "en")
    pending = AssertionSession()
    with assertion_session(pending):
        before, _ = validate()
    assert "executive_summary" not in before
    queries = list(pending.queries.values())
    replies = {"structured_output_valid": True, "results": [review(query) for query in queries]}
    with assertion_session(AssertionSession(validate_response(replies, queries, "offline-verifier"))):
        accepted, audit = validate()
    assert accepted["executive_summary"] == text
    assert any(node["source_path"] == "executive_summary" for node in audit["nodes"])


@pytest.mark.parametrize("text,kind,expected", [
    ("Compare reported consolidated results across annual periods.", "methodology", True),
    ("What were the drivers of reported earnings?", "question", True),
    ("Compare earnings. Revenue was higher.", "methodology", False),
    ("What drives earnings? Revenue was 300.", "question", False),
    ("The company was profitable; how can we compare results?", "question", False),
])
def test_research_intent_is_not_confused_with_historical_facts(text, kind, expected):
    assert _labelled_nonfactual_text(text, kind) is expected
