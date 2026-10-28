"""Offline gateway-response -> stage-checkpoint contract tests.

These tests exercise the real gateway decoder, the public stage executor
checkpoint seam, and SQLite persistence/reopen.  They prove transport
retention only; the fixture claims are not presented as trusted research.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from openthesis.domain import Company, ResearchArtifact, ResearchRun
from openthesis.research_execution.checkpoints import (
    candidate_checkpoint,
    checkpoint_artifact_id,
    completed_checkpoint,
    read_checkpoints,
)
from openthesis.research_execution.executor import StageExecutor, StageResult, StageValidation
from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.research_execution.responses import (
    gateway_received_response,
    response_record,
    restore_response,
)
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.report_html import render_research_html
from openthesis.reporting import render_research_run
from openthesis.resilience.contracts import digest
from openthesis.storage import Storage


RUN_ID = "gateway-checkpoint-contract-run"
EXECUTION_FINGERPRINT = "gateway-checkpoint-contract-v1"


def _spec() -> StageSpec:
    return StageSpec(
        "gateway-analysis",
        "gateway-checkpoint-v1",
        "produce-gateway-response",
        "validate-gateway-response",
        output_contract="gateway-response-v1",
    )


def _storage(path: Path) -> Storage:
    store = Storage(path)
    company = Company("0000000314", "GWTEST", "Gateway checkpoint fixture")
    store.save_company(company)
    store.save_run(ResearchRun(
        run_id=RUN_ID,
        company=company,
        workflow_id="gateway-checkpoint-workflow",
        research_pack_id="offline-pack",
        research_pack_version="1",
        provider_id="offline-gateway",
        model_id="offline-model",
        data_as_of="2026-10-12",
    ))
    return store


def _gateway_record() -> tuple[dict, dict]:
    return response_record(_gateway_response())


def _gateway_response():
    wire = json.dumps({
        "claims": [{
            "claim_id": "revenue-growth",
            "text": "revenue grew",
            "evidence_ids": ["filing:fixture:page:7"],
        }],
        "references": ["filing:fixture:page:7"],
        "unknown_business_extension": {
            "nested": [{"label": "retain-me"}, {"metrics": {"input_tokens": 17}}],
        },
    }, ensure_ascii=False)
    return gateway_received_response(
        wire,
        {
            "configured_model_id": "offline-model",
            "connection_id": "offline-connection",
            "provider_id": "offline-gateway",
            "billing_class": "research",
            "business_token_economics": {"input_tokens": 17, "output_tokens": 9},
        },
        json.loads,
        expected_model_id="offline-model",
        expected_connection_id="offline-connection",
    )


class _ImmediateScheduler:
    @contextmanager
    def slot(self, cancel_check):
        if cancel_check():
            raise RuntimeError("cancelled")
        yield

    def mark_rate_limited(self, _delay):
        return None

    def mark_success(self):
        return None


class _FormalGatewayProvider:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def generate(self, _system_prompt, _user_prompt, *, json_mode=True):
        assert json_mode is True
        self.calls += 1
        return self.response


def _save_checkpoint(store: Storage, payload: dict, artifact_type: str) -> None:
    store.save_artifact(ResearchArtifact(
        checkpoint_artifact_id(RUN_ID, payload),
        RUN_ID,
        artifact_type,
        "gateway-analysis",
        payload,
        model_id="offline-model",
        agent_id="offline-gateway-fixture",
    ))


def test_gateway_response_survives_stage_checkpoint_and_storage_restart(tmp_path: Path) -> None:
    store = _storage(tmp_path / "storage")
    spec = _spec()
    response = _gateway_response()
    record, receipt = response_record(response)
    producer_calls = 0

    def produce(_spec, _inputs, _dependencies):
        nonlocal producer_calls
        producer_calls += 1
        # The executor sees the actual dict-compatible ModelResponse.  It is
        # not pre-converted into the durable envelope here.
        return response

    def validate(_spec, candidate, _inputs):
        # The graph validator consumes the detached plain JSON projection;
        # the producer above still returned the actual ModelResponse object.
        assert type(candidate) is dict
        assert "schema" not in candidate and "content" not in candidate
        assert type(candidate["claims"]) is list
        assert type(candidate["claims"][0]["evidence_ids"]) is list
        assert candidate["claims"][0]["evidence_ids"] == ["filing:fixture:page:7"]
        return StageValidation(
            True,
            {"transport_preserved": True},
            "completed_verified",
            record={"transport_only": True},
        )

    def save_candidate(specification, stage_input, candidate):
        _save_checkpoint(
            store,
            candidate_checkpoint(
                specification,
                stage_input,
                candidate,
                execution_fingerprint=EXECUTION_FINGERPRINT,
            ),
            "stage-candidate",
        )

    def save_result(result: StageResult):
        _save_checkpoint(store, completed_checkpoint(result), "stage-checkpoint")

    results = StageExecutor(
        {"produce-gateway-response": produce},
        {"validate-gateway-response": validate},
        checkpoint=save_result,
        candidate_checkpoint=save_candidate,
    ).execute(
        StageRegistry((spec,)),
        {"source": "frozen-gateway-fixture"},
        execution_fingerprint=EXECUTION_FINGERPRINT,
    )
    assert producer_calls == 1
    assert results[spec.stage_id].validation.passed is True

    reopened = Storage(tmp_path / "storage")
    artifacts = reopened.get_artifacts(RUN_ID)
    read = read_checkpoints(artifacts, StageRegistry((spec,)))
    assert read.issues == ()
    saved = read.results[spec.stage_id].candidate
    # Stage checkpoints intentionally persist ordinary JSON business content;
    # host receipt/diagnostics are tested through the journal below.
    assert type(saved) is dict
    assert saved["claims"] == record["content"]["claims"]
    assert type(saved["unknown_business_extension"]) is dict
    assert type(saved["unknown_business_extension"]["nested"]) is list
    assert saved["unknown_business_extension"]["nested"][1]["metrics"] == {
        "input_tokens": 17
    }
    assert receipt["extensions"]["business_token_economics"] == {
        "input_tokens": 17, "output_tokens": 9}


def test_formal_model_stage_journal_feeds_plain_stage_checkpoint_and_report_restart(
    tmp_path: Path,
) -> None:
    """Use the real provider/journal seam before the graph checkpoint seam."""
    store = _storage(tmp_path / "storage")
    spec = _spec()
    registry = StageRegistry((spec,))
    response = _gateway_response()
    provider = _FormalGatewayProvider(response)
    journal = ModelRequestJournal(
        store,
        RUN_ID,
        "config-v1",
        timeout_seconds=2,
        cancel_check=lambda: False,
    )
    generation = journal.prepare(registry, digest({"source": "filing:fixture:page:7"}))[spec.stage_id]
    request = ModelStageRequest(spec.stage_id, "system prompt", "all source evidence")
    with journal.bind(spec.stage_id, generation):
        generated = ModelStageExecutor(
            provider, _ImmediateScheduler(), request_journal=journal,
        ).execute(request)

    assert provider.calls == 1
    assert type(generated) is dict
    assert "schema" not in generated and "content" not in generated
    assert type(generated["claims"]) is list
    assert type(generated["unknown_business_extension"]) is dict
    with store.connect() as db:
        row = db.execute(
            "SELECT candidate_json,response_received,receipt_json,diagnostic_trace_id "
            "FROM model_request_journal WHERE run_id=?",
            (RUN_ID,),
        ).fetchone()
    assert row["response_received"] == 1
    assert row["diagnostic_trace_id"]
    journal_record = json.loads(row["candidate_json"])
    assert journal_record["schema"] == "openthesis.model-response.v1"
    assert journal_record["content"]["claims"] == generated["claims"]
    receipt = json.loads(row["receipt_json"])
    assert receipt["extensions"]["business_token_economics"] == {
        "input_tokens": 17, "output_tokens": 9}

    # A new journal instance recovers the received host response without a
    # second provider call.  The stage graph consumes ordinary JSON only.
    reopened = Storage(tmp_path / "storage")
    recovered_journal = ModelRequestJournal(
        reopened,
        RUN_ID,
        "config-v1",
        timeout_seconds=2,
        cancel_check=lambda: False,
    )
    recovered = recovered_journal.saved_candidate(request, spec.stage_id)
    assert recovered is not None
    assert recovered["claims"] == generated["claims"]
    assert recovered.receipt["response_received"] is True
    assert recovered.diagnostics == response.diagnostics

    def validate(_spec, candidate, _inputs):
        assert type(candidate) is dict
        assert "schema" not in candidate and "content" not in candidate
        assert type(candidate["claims"]) is list
        assert candidate["claims"][0]["evidence_ids"] == ["filing:fixture:page:7"]
        return StageValidation(
            False,
            {},
            "completed_partial",
            record={"evidence_gate": "offline-source-fixture"},
        )

    def save_candidate(specification, stage_input, candidate):
        _save_checkpoint(
            reopened,
            candidate_checkpoint(
                specification,
                stage_input,
                candidate,
                execution_fingerprint=EXECUTION_FINGERPRINT,
            ),
            "stage-candidate",
        )

    def save_result(result: StageResult):
        _save_checkpoint(reopened, completed_checkpoint(result), "stage-checkpoint")

    graph_result = StageExecutor(
        {"produce-gateway-response": lambda *_args: generated},
        {"validate-gateway-response": validate},
        checkpoint=save_result,
        candidate_checkpoint=save_candidate,
    ).execute(
        registry,
        {"source": "filing:fixture:page:7"},
        execution_fingerprint=EXECUTION_FINGERPRINT,
    )
    assert graph_result[spec.stage_id].validation.state == "completed_partial"

    # The report fixture is deliberately partial: this checks visible body and
    # source-id retention, not factual admission or substantive verification.
    report_content = {
        "mode": "synthesis-incomplete",
        "report": {"business_model": {
            "analysis": generated["claims"][0]["text"],
            "evidence_ids": ["filing:fixture:page:7"],
        }},
        "verification": {"section_validation": {"nodes": [{
            "role": "source_quote",
            "text": generated["claims"][0]["text"],
            "source_ids": ["filing:fixture:page:7"],
        }]}},
    }
    reopened.save_artifact(ResearchArtifact(
        f"{RUN_ID}:report-fixture",
        RUN_ID,
        "research-report",
        "Gateway checkpoint fixture report",
        report_content,
    ))
    final = Storage(tmp_path / "storage")
    final_read = read_checkpoints(final.get_artifacts(RUN_ID), registry)
    assert final_read.issues == ()
    assert final_read.results[spec.stage_id].candidate["claims"] == generated["claims"]
    artifacts = final.get_artifacts(RUN_ID)
    markdown = render_research_run(RUN_ID, artifacts, "en", company_name="Gateway fixture")
    html = render_research_html(RUN_ID, artifacts, "en", company_name="Gateway fixture")
    assert "revenue grew" in markdown and "revenue grew" in html


def test_plain_dict_and_formal_response_envelope_have_distinct_compatible_restore() -> None:
    plain = {
        "claims": [{"claim_id": "plain", "evidence_ids": ["source:1"]}],
        "unknown_extension": {"nested": [1, {"safe": True}]},
    }
    plain_record, plain_receipt = response_record(plain)
    assert plain_record == plain
    assert plain_receipt["schema"] == "openthesis.model-response.v1"
    assert restore_response(plain_record) == plain

    formal, _receipt = _gateway_record()
    restored = restore_response(formal)
    assert restored["claims"][0]["claim_id"] == "revenue-growth"
    assert restored.receipt["response_received"] is True


def test_journal_received_body_survives_local_stage_checkpoint_failure_without_provider_retry(
    tmp_path: Path,
) -> None:
    store = _storage(tmp_path / "storage")
    spec = _spec()
    registry = StageRegistry((spec,))
    response = _gateway_response()
    provider = _FormalGatewayProvider(response)
    journal = ModelRequestJournal(
        store, RUN_ID, "config-v1", timeout_seconds=2, cancel_check=lambda: False,
    )
    generation = journal.prepare(registry, digest({"source": "filing:fixture:page:7"}))[spec.stage_id]
    request = ModelStageRequest(spec.stage_id, "system prompt", "all source evidence")
    with journal.bind(spec.stage_id, generation):
        generated = ModelStageExecutor(
            provider, _ImmediateScheduler(), request_journal=journal,
        ).execute(request)

    def save_candidate(specification, stage_input, candidate):
        _save_checkpoint(
            store,
            candidate_checkpoint(
                specification,
                stage_input,
                candidate,
                execution_fingerprint=EXECUTION_FINGERPRINT,
            ),
            "stage-candidate",
        )

    with pytest.raises(RuntimeError, match="LOCAL_CHECKPOINT_FAILED"):
        StageExecutor(
            {"produce-gateway-response": lambda *_args: generated},
            {"validate-gateway-response": lambda *_args: StageValidation(
                False, {}, "completed_partial", record={"fixture": "evidence"})},
            checkpoint=lambda _result: (_ for _ in ()).throw(
                RuntimeError("LOCAL_CHECKPOINT_FAILED")),
            candidate_checkpoint=save_candidate,
        ).execute(
            registry,
            {"source": "filing:fixture:page:7"},
            execution_fingerprint=EXECUTION_FINGERPRINT,
        )
    assert provider.calls == 1

    reopened = Storage(tmp_path / "storage")
    recovered_journal = ModelRequestJournal(
        reopened, RUN_ID, "config-v1", timeout_seconds=2, cancel_check=lambda: False,
    )
    recovered = recovered_journal.saved_candidate(request, spec.stage_id)
    assert recovered is not None
    assert recovered["claims"] == generated["claims"]
    assert recovered.receipt["response_received"] is True
    recovered_plain = dict(recovered)
    saved_result = StageResult(
        spec.stage_id,
        spec.identity,
        digest({"source": "filing:fixture:page:7"}),
        recovered_plain,
        StageValidation(False, {}, "needs_action", issues=("LOCAL_CHECKPOINT_FAILED",)),
        digest(recovered_plain),
        execution_fingerprint=EXECUTION_FINGERPRINT,
        has_candidate=True,
    )

    def validate_local(_spec, candidate, _inputs):
        assert type(candidate) is dict
        assert candidate["claims"] == generated["claims"]
        return StageValidation(False, {}, "completed_partial", record={"fixture": "evidence"})

    resumed = StageExecutor(
        {"produce-gateway-response": lambda *_args: pytest.fail("provider/producer retried")},
        {"validate-gateway-response": validate_local},
        checkpoint=lambda result: _save_checkpoint(
            reopened, completed_checkpoint(result), "stage-checkpoint"),
    ).execute(
        registry,
        {"source": "filing:fixture:page:7"},
        local_only=True,
        revalidation_candidates={spec.stage_id: saved_result},
        execution_fingerprint=EXECUTION_FINGERPRINT,
    )
    assert resumed[spec.stage_id].validation.state == "completed_partial"
    assert provider.calls == 1


def test_received_candidate_reopens_locally_after_completion_checkpoint_failure(
    tmp_path: Path,
) -> None:
    store = _storage(tmp_path / "storage")
    spec = _spec()
    record, _receipt = _gateway_record()
    producer_calls = 0
    checkpoint_calls = 0

    def produce(_spec, _inputs, _dependencies):
        nonlocal producer_calls
        producer_calls += 1
        return record

    def validate(_spec, candidate, _inputs):
        assert candidate == record
        return StageValidation(True, {"transport_preserved": True}, "completed_verified")

    def save_candidate(specification, stage_input, candidate):
        _save_checkpoint(
            store,
            candidate_checkpoint(
                specification,
                stage_input,
                candidate,
                execution_fingerprint=EXECUTION_FINGERPRINT,
            ),
            "stage-candidate",
        )

    def fail_once(result: StageResult):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if checkpoint_calls == 1:
            raise RuntimeError("LOCAL_CHECKPOINT_FAILED")
        _save_checkpoint(store, completed_checkpoint(result), "stage-checkpoint")

    with pytest.raises(RuntimeError, match="LOCAL_CHECKPOINT_FAILED"):
        StageExecutor(
            {"produce-gateway-response": produce},
            {"validate-gateway-response": validate},
            checkpoint=fail_once,
            candidate_checkpoint=save_candidate,
        ).execute(
            StageRegistry((spec,)),
            {"source": "frozen-gateway-fixture"},
            execution_fingerprint=EXECUTION_FINGERPRINT,
        )
    assert producer_calls == 1

    reopened = Storage(tmp_path / "storage")
    read = read_checkpoints(reopened.get_artifacts(RUN_ID), StageRegistry((spec,)))
    assert read.issues == ()
    assert spec.stage_id in read.revalidation_candidates

    resumed = StageExecutor(
        {"produce-gateway-response": produce},
        {"validate-gateway-response": validate},
        checkpoint=fail_once,
    ).execute(
        StageRegistry((spec,)),
        {"source": "frozen-gateway-fixture"},
        local_only=True,
        revalidation_candidates=read.revalidation_candidates,
        execution_fingerprint=EXECUTION_FINGERPRINT,
    )
    assert resumed[spec.stage_id].validation.passed is True
    assert producer_calls == 1

