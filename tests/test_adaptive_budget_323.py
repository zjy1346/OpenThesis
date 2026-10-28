"""Default forecasts are not quotas; explicit caps and work identity remain real."""
import pytest
import json
import time
from concurrent.futures import ThreadPoolExecutor

from openthesis.providers import ProviderError
from openthesis.research_execution.call_budget import prepare_plan, plan_status, reserve, user_envelope, extend_plan, LEGACY_STANDARD_ENVELOPE, record_gap, settle
from test_assertion_retention_312 import setup


def plan(journal, envelope=None):
    with journal.store.connect() as db:
        db.execute("DELETE FROM model_run_call_plans WHERE run_id=?", (journal.run_id,))
        prepare_plan(db, journal.run_id, ["business"], request_plan=[{
            "stage_id": "business", "purpose": "generation", "input_estimate": 2_000_000,
            "output_limit": 131_072}], envelope=envelope)


def call(journal, number, purpose="generation", work=None):
    with journal.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, {"request_id": f"r:{number}", "attempt_id": f"a:{number}",
            "stage_id": "business", "generation": 1, "work_item": work or {
                "obligation": purpose, "targets": [f"block:{number}"], "source_version": "source:1",
                "completed_digest": "unchanged", "strategy": "source-bound-v1", "trigger": "missing_block"}},
            purpose=purpose, input_estimate=100_000, output_limit=10_000, elapsed_seconds=1800)


def test_default_large_plan_and_seventeenth_necessary_request_continue(tmp_path):
    journal = setup(tmp_path)
    plan(journal)
    for number in range(18):
        call(journal, number)
    status = journal.store.model_call_plan(journal.run_id)
    assert status["policy"] == "adaptive"
    assert status["consumption"]["calls"] == 18
    assert status["remaining_calls"] is None
    assert status["user_hard_limits"]["max_total_tokens"] is None


def test_different_repairs_and_extra_verification_are_not_count_quotas(tmp_path):
    journal = setup(tmp_path)
    plan(journal)
    for number in range(5):
        call(journal, number, "claim_repair")
    for number in range(5, 10):
        call(journal, number, "semantic_verification")


def test_same_semantic_work_cannot_be_recharged_by_changing_physical_id(tmp_path):
    journal = setup(tmp_path)
    plan(journal)
    work = {"obligation": "claim_repair", "targets": ["financial_quality.claims[0]"],
        "source_version": "source:1", "completed_digest": "candidate:1", "strategy": "source-bound-v1"}
    call(journal, 1, "claim_repair", work)
    with pytest.raises(ProviderError, match="work") as caught:
        call(journal, 2, "claim_repair", {**work, "trigger": "new wording"})
    assert caught.value.code == "MODEL_WORK_ITEM_NO_PROGRESS"
    assert journal.store.model_call_plan(journal.run_id)["consumption"]["calls"] == 1


def test_user_explicit_call_cap_still_stops_before_send(tmp_path):
    journal = setup(tmp_path)
    plan(journal, {"policy": "adaptive", "user_hard_limits": {"max_calls": 1}})
    call(journal, 1)
    with pytest.raises(ProviderError) as caught:
        call(journal, 2)
    assert caught.value.code == "MODEL_RUN_CALL_BUDGET_EXHAUSTED"


def test_null_roundtrip_never_restores_old_default_caps():
    value = user_envelope({"policy": "adaptive", "user_hard_limits": {"max_calls": None}})
    assert user_envelope(value) == value
    assert value["max_calls"] is None
    assert value["max_automatic_seconds"] is None


@pytest.mark.parametrize("known_default", [True, False])
@pytest.mark.parametrize("choice", ["keep", "remove"])
def test_explicit_legacy_migration_preserves_history_and_source_heads(tmp_path, known_default, choice):
    journal = setup(tmp_path)
    plan(journal)
    call(journal, 1)
    with journal.store.connect() as db:
        old = json.loads(db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (journal.run_id,)).fetchone()[0])
        old.update(schema="research-call-plan-v3-user-envelope", user_envelope={**LEGACY_STANDARD_ENVELOPE},
            max_calls=16, max_input_estimate=1_500_000, max_output_tokens=100_000, workload_fits=False)
        for key in ("policy", "limit_source", "user_hard_limits"):
            old.pop(key, None)
        if not known_default:
            old["user_envelope"]["max_calls"] = 18
        db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (json.dumps(old), journal.run_id))
        status = plan_status(db, journal.run_id)
        heads = [tuple(row) for row in db.execute("SELECT * FROM model_stage_request_heads")]
        assert status["migration_choice_required"] is not known_default
    with journal.store.connect() as db:
        migrated = extend_plan(db, journal.run_id, status["plan_digest"], [], choice)
        assert [tuple(row) for row in db.execute("SELECT * FROM model_stage_request_heads")] == heads
    assert migrated["consumption"] == status["consumption"]
    assert migrated["policy"] == "adaptive"
    assert migrated["max_calls"] == (None if choice == "remove" else 16 if known_default else 18)
    assert migrated["limit_source"] == ("unset" if choice == "remove" else "user_explicit")


def test_atomic_semantic_dedup_concurrent_new_physical_attempts(tmp_path):
    journal = setup(tmp_path)
    plan(journal)
    work = {"obligation": "generation", "targets": ["same-block"], "source_version": "frozen",
        "completed_digest": "before", "strategy": "source-bound-v1"}
    def attempt(number):
        try:
            call(journal, number, work=work)
            return "reserved"
        except ProviderError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(attempt, (1, 2)))
    assert sorted(outcomes) == ["MODEL_WORK_ITEM_NO_PROGRESS", "reserved"]
    assert journal.store.model_call_plan(journal.run_id)["consumption"]["calls"] == 1


def test_adaptive_restart_after_twenty_minutes_does_not_create_run_deadline(tmp_path):
    from openthesis.research_execution.registry import StageRegistry, StageSpec
    journal = setup(tmp_path)
    with journal.store.connect() as db:
        saved = json.loads(db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (journal.run_id,)).fetchone()[0])
        saved["started_at"] = time.time() - 2000
        db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (json.dumps(saved), journal.run_id))
    registry = StageRegistry([StageSpec(stage, "1", "analysis", "verify", output_contract="body") for stage in ("business", "synthesis")])
    journal.prepare(registry, "frozen")
    journal.ensure_automatic_time_available()
    assert journal._automatic_deadline is None


@pytest.mark.parametrize("known_cache", [True, False])
def test_explicit_fee_cap_uses_receipted_cache_savings_not_guesses(tmp_path, known_cache):
    journal = setup(tmp_path)
    quote = {"input_per_million": 1., "cached_input_per_million": .1, "output_per_million": 0.,
        "currency": "CNY", "source": "route-attested-fixture", "route_identity": "fixture-route"}
    plan(journal, {"user_hard_limits": {"max_cost": 1.5}, "price_quote": quote})
    first = {"request_id": "fee:first", "attempt_id": "fee:attempt", "stage_id": "business", "generation": 1}
    second = {**first, "request_id": "fee:second", "attempt_id": "fee:next"}
    with journal.store.connect() as db:
        reserve(db, journal.run_id, first, purpose="generation", input_estimate=1_000_000, output_limit=1, route_identity="fixture-route")
        if known_cache:
            usage = {"input_tokens": 1_000_000, "output_tokens": 0, "reasoning_tokens": 0, "cache_read_input_tokens": 900_000}
            settle(db, first, usage, "response_received", reasoning_in_output=True)
            db.execute("INSERT INTO model_wire_diagnostics VALUES(?,?,?,?,?,?,?)", (first["request_id"], first["attempt_id"],
                journal.run_id, "business", "generation", json.dumps({"usage": usage}), time.time()))
        if known_cache:
            reserve(db, journal.run_id, second, purpose="generation", input_estimate=1_000_000, output_limit=1, route_identity="fixture-route")
        else:
            with pytest.raises(ProviderError) as blocked:
                reserve(db, journal.run_id, second, purpose="generation", input_estimate=1_000_000, output_limit=1, route_identity="fixture-route")
            assert blocked.value.code == "MODEL_RUN_COST_BUDGET_EXHAUSTED"
