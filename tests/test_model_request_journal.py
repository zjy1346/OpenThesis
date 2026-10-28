from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun, RunStatus
from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageRequest
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.research_execution.durable import DurableStageVerifier
from openthesis.research_execution.executor import StageResult, StageValidation
from openthesis.resilience.contracts import digest
from openthesis.storage import Storage


def _registry(*stages: tuple[str, tuple[str, ...]]) -> StageRegistry:
    return StageRegistry(tuple(
        StageSpec(stage_id, "v1", "handler", "validator", dependencies,
                  output_contract=f"{stage_id}-output")
        for stage_id, dependencies in stages
    ))


def _store(tmp_path) -> Storage:
    storage = Storage(tmp_path / "journal-store")
    storage.save_company(DEMO_COMPANY)
    storage.save_run(ResearchRun(
        run_id="journal-run", company=DEMO_COMPANY, workflow_id="workflow",
        research_pack_id="pack", research_pack_version="1", provider_id="provider",
        model_id="model", data_as_of="2026-10-04",
    ))
    return storage


def _journal(storage: Storage, *, config: str = "config-v1", cancelled=lambda: False,
             clock=lambda: 1_000.0) -> ModelRequestJournal:
    return ModelRequestJournal(storage, "journal-run", config, timeout_seconds=2,
                               cancel_check=cancelled, clock=clock)


def _request(stage_id: str = "analysis", prompt: str = "analyze this evidence") -> ModelStageRequest:
    return ModelStageRequest(stage_id, "system instructions", prompt)


def _scope(journal: ModelRequestJournal, stage_id: str, generation: int) -> tuple[str, int]:
    with journal.bind(stage_id, generation):
        scope = journal.capture_scope()
    assert scope == (stage_id, generation)
    return scope


def _row(storage: Storage, request: ModelStageRequest):
    with storage.connect() as db:
        return db.execute(
            "SELECT * FROM model_request_journal WHERE run_id=? AND stage_id=? AND input_digest=?",
            ("journal-run", request.stage_id, request.input_digest),
        ).fetchone()


def test_running_intent_is_committed_before_provider_call_and_candidate_is_reused(tmp_path):
    storage = _store(tmp_path)
    registry = _registry(("analysis", ()))
    journal = _journal(storage)
    generation = journal.prepare(registry, "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)
    calls = 0

    def invoke():
        nonlocal calls
        calls += 1
        row = _row(storage, request)
        assert row is not None
        assert row["state"] == "running"
        assert row["candidate_json"] is None
        return {"finding": "supported", "usage": {"prompt_tokens": 23}}

    first = journal.execute(request, invoke, scope)
    second = journal.execute(request, lambda: pytest.fail("provider must not be called twice"), scope)

    assert first == second == {"finding": "supported", "usage": {"prompt_tokens": 23}}
    assert calls == 1
    assert _row(storage, request)["state"] == "candidate"


def test_unknown_timeout_is_not_automatically_retried(tmp_path):
    storage = _store(tmp_path)
    journal = _journal(storage)
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)
    calls = 0

    def timeout():
        nonlocal calls
        calls += 1
        raise TimeoutError("response status unknown")

    with pytest.raises(TimeoutError):
        journal.execute(request, timeout, scope)
    assert _row(storage, request)["state"] == "unknown"
    with pytest.raises(ProviderError) as caught:
        journal.execute(request, timeout, scope)
    assert caught.value.code == "MODEL_SUBMISSION_UNKNOWN"
    assert caught.value.retryable is False
    assert calls == 1


def test_confirmed_rate_limit_is_recorded_and_not_silently_resubmitted(tmp_path):
    storage = _store(tmp_path)
    journal = _journal(storage)
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)
    calls = 0

    def reject_429():
        nonlocal calls
        calls += 1
        raise ProviderError("rate limited", code="MODEL_RATE_LIMITED", retryable=True)

    with pytest.raises(ProviderError) as first:
        journal.execute(request, reject_429, scope)
    assert first.value.code == "MODEL_RATE_LIMITED"
    assert _row(storage, request)["state"] == "rejected"
    with pytest.raises(ProviderError) as second:
        journal.execute(request, reject_429, scope)
    assert second.value.code == "MODEL_RATE_LIMITED"
    assert second.value.retryable is False
    assert calls == 1


def test_two_threads_with_same_request_cannot_submit_provider_twice(tmp_path):
    storage = _store(tmp_path)
    journal = _journal(storage)
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)
    start = threading.Barrier(3)
    provider_entered = threading.Event()
    duplicate_refused = threading.Event()
    release_provider = threading.Event()
    calls = 0
    successes, failures = [], []

    def invoke():
        nonlocal calls
        calls += 1
        provider_entered.set()
        assert release_provider.wait(5)
        return {"finding": "single submission"}

    def worker():
        start.wait(timeout=5)
        try:
            successes.append(journal.execute(request, invoke, scope))
        except ProviderError as exc:
            failures.append(exc.code)
            if exc.code == "MODEL_REQUEST_IN_PROGRESS":
                duplicate_refused.set()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    try:
        assert provider_entered.wait(5)
        assert duplicate_refused.wait(5)
    finally:
        release_provider.set()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1
    assert successes == [{"finding": "single submission"}]
    assert failures == ["MODEL_REQUEST_IN_PROGRESS"]


def test_replaced_generation_keeps_late_candidate_but_never_returns_it_as_current(tmp_path):
    storage = _store(tmp_path)
    registry = _registry(("analysis", ()), ("summary", ("analysis",)))
    journal = _journal(storage)
    old_generations = journal.prepare(registry, "material-v1")
    request = _request()
    scope = _scope(journal, "analysis", old_generations["analysis"])
    provider_entered, release_provider = threading.Event(), threading.Event()
    failures, results = [], []

    def invoke():
        provider_entered.set()
        assert release_provider.wait(5)
        return {"stale": "candidate"}

    def submit():
        try:
            results.append(journal.execute(request, invoke, scope))
        except ProviderError as exc:
            failures.append(exc.code)

    thread = threading.Thread(target=submit)
    thread.start()
    try:
        assert provider_entered.wait(5)
        next_generations = journal.prepare(registry, "material-v1", invalidated=("analysis",))
        assert next_generations["analysis"] == old_generations["analysis"] + 1
        assert next_generations["summary"] == old_generations["summary"] + 1
    finally:
        release_provider.set()
    thread.join(timeout=5)

    saved = _row(storage, request)
    assert not thread.is_alive()
    assert results == []
    assert failures == ["MODEL_REQUEST_GENERATION_STALE"]
    assert saved["state"] == "late_candidate"
    assert json.loads(saved["candidate_json"]) == {"stale": "candidate"}


def test_expired_running_lease_after_interruption_is_unknown_not_a_retry(tmp_path):
    storage = _store(tmp_path)
    now = [1_000.0]
    journal = _journal(storage, clock=lambda: now[0])
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)
    calls = 0

    def interrupted():
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        journal.execute(request, interrupted, scope)
    # Model a process death after the committed intent but before recording an
    # outcome: the stale lease is not permission to submit the paid request again.
    with storage.connect() as db:
        db.execute("UPDATE model_request_journal SET state='running', lease_until=? WHERE request_id=?",
                   (now[0] - 1, _row(storage, request)["request_id"]))
    now[0] += 10
    with pytest.raises(ProviderError) as caught:
        journal.execute(request, interrupted, scope)
    assert caught.value.code == "MODEL_SUBMISSION_UNKNOWN"
    assert calls == 1


def test_prepare_invalidation_changes_target_and_descendants_and_identity_changes_all(tmp_path):
    storage = _store(tmp_path)
    registry = _registry(
        ("root", ()), ("target", ("root",)), ("child", ("target",)), ("independent", ()),
    )
    journal = _journal(storage, config="config-v1")
    initial = journal.prepare(registry, "material-v1")
    assert set(initial.values()) == {1}

    invalidated = journal.prepare(registry, "material-v1", invalidated=("target",))
    assert invalidated == {"root": 1, "target": 2, "child": 2, "independent": 1}

    new_material = journal.prepare(registry, "material-v2")
    assert new_material == {"root": 2, "target": 3, "child": 3, "independent": 2}

    new_configuration = _journal(storage, config="config-v2").prepare(registry, "material-v2")
    assert new_configuration == {"root": 3, "target": 4, "child": 4, "independent": 3}


def test_candidate_tampering_is_rejected_without_provider_call(tmp_path):
    storage = _store(tmp_path)
    journal = _journal(storage)
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)
    assert journal.execute(request, lambda: {"finding": "original"}, scope) == {"finding": "original"}
    with storage.connect() as db:
        db.execute("UPDATE model_request_journal SET candidate_json=? WHERE request_id=?",
                   ('{"finding":"tampered"}', _row(storage, request)["request_id"]))

    with pytest.raises(ProviderError) as caught:
        journal.execute(request, lambda: pytest.fail("tampering must not trigger a provider call"), scope)
    assert caught.value.code == "MODEL_REQUEST_CANDIDATE_CORRUPT"


def test_cancellation_before_submission_does_not_call_provider_or_create_intent(tmp_path):
    storage = _store(tmp_path)
    journal = _journal(storage, cancelled=lambda: True)
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)

    with pytest.raises(ProviderError) as caught:
        journal.execute(request, lambda: pytest.fail("cancelled request must not be submitted"), scope)
    assert caught.value.code == "MODEL_CANCELLED"
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_request_journal").fetchone()[0] == 0


@pytest.mark.parametrize("encoded", ('{broken', 'null', '{"finding":NaN}',
                                      '{"finding":"original","finding":"original"}'))
def test_corrupt_cached_json_is_a_typed_integrity_error_not_a_new_call(tmp_path, encoded):
    storage = _store(tmp_path)
    journal = _journal(storage)
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)
    journal.execute(request, lambda: {"finding": "original"}, scope)
    with storage.connect() as db:
        db.execute("UPDATE model_request_journal SET candidate_json=?", (encoded,))
    with pytest.raises(ProviderError) as caught:
        journal.execute(request, lambda: pytest.fail("corrupt cache must not submit"), scope)
    assert caught.value.code == "MODEL_REQUEST_CANDIDATE_CORRUPT"


def test_response_after_request_lease_is_saved_and_recovered_only_by_new_local_owner(tmp_path):
    storage = _store(tmp_path)
    now = [1000.0]
    journal = _journal(storage, clock=lambda: now[0])
    generation = journal.prepare(_registry(("analysis", ())), "material-v1")["analysis"]
    request, scope = _request(), _scope(journal, "analysis", generation)

    def delayed_response():
        now[0] += 18  # configured timeout 2s plus the 15s persistence grace
        return {"finding": "late"}

    with pytest.raises(ProviderError) as caught:
        journal.execute(request, delayed_response, scope)
    assert caught.value.code == "MODEL_REQUEST_EXPIRED"
    saved = _row(storage, request)
    assert saved["state"] == "late_candidate"
    assert json.loads(saved["candidate_json"]) == {"finding": "late"}
    assert saved["response_received"] == 1
    restored = journal.execute(request, lambda: pytest.fail("late request must not resubmit"), scope)
    assert restored == {"finding": "late"}
    recovered = _row(storage, request)
    assert recovered["state"] == "candidate"
    assert recovered["owner_id"] != saved["owner_id"]
    assert recovered["diagnostic_code"] == "MODEL_LATE_CANDIDATE_LOCAL_RECOVERED"


@pytest.mark.parametrize("timeout", (0, -1, float("nan"), float("inf"), True, "2"))
def test_invalid_timeout_cannot_create_an_unbounded_request_lease(tmp_path, timeout):
    storage = _store(tmp_path)
    with pytest.raises(ValueError, match="MODEL_REQUEST_JOURNAL_CONFIGURATION_INVALID"):
        ModelRequestJournal(storage, "journal-run", "config-v1", timeout_seconds=timeout,
                            cancel_check=lambda: False)


def test_delete_finished_run_removes_only_its_request_and_recovery_tree(tmp_path):
    storage = _store(tmp_path)
    registry = _registry(("analysis", ()))
    journal = _journal(storage)
    generation = journal.prepare(registry, "material-v1")["analysis"]
    journal.execute(_request(), lambda: {"finding": "original"}, _scope(journal, "analysis", generation))
    spec = registry.stages[0]
    candidate = {"finding": "original"}

    def validate(value):
        return StageResult(spec.stage_id, spec.identity, "material-v1", value,
                           StageValidation(True, value, state="completed_verified"), digest(value),
                           execution_fingerprint="config-v1", has_candidate=True)

    result = DurableStageVerifier(storage, "journal-run", request_generations={"analysis": generation})(
        spec, "material-v1", candidate, "config-v1", validate)
    assert result.validation.passed
    completed = ResearchRun(run_id="journal-run", company=DEMO_COMPANY, workflow_id="workflow",
                            research_pack_id="pack", research_pack_version="1", provider_id="provider",
                            model_id="model", data_as_of="2026-10-04", status=RunStatus.COMPLETED)
    storage.save_run(completed)
    storage.save_run(replace(completed, run_id="unrelated-run"))
    other_journal = ModelRequestJournal(storage, "unrelated-run", "config-v1", timeout_seconds=2,
                                       cancel_check=lambda: False)
    other_generation = other_journal.prepare(registry, "material-v1")["analysis"]
    other_journal.execute(_request(), lambda: {"finding": "keep"},
                          _scope(other_journal, "analysis", other_generation))
    with storage.connect() as db:
        db.execute("INSERT INTO recovery_provider_circuits(provider_id,configuration_fingerprint) VALUES(?,?)",
                   ("provider", "config-v1"))
    assert storage.delete_run("journal-run")
    with storage.connect() as db:
        for table in ("recovery_incidents", "recovery_plans", "recovery_actions", "recovery_events",
                      "recovery_target_leases", "stage_verification_snapshots"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in ("model_request_journal", "model_stage_request_heads"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id='journal-run'").fetchone()[0] == 0
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id='unrelated-run'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM recovery_provider_circuits").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
