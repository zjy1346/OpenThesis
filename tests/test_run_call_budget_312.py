from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.call_budget import reserve, settle
from test_assertion_retention_312 import setup


def context(index, stage="business", *, generation=1, work=None):
    return {"request_id": f"request:{index}", "attempt_id": f"attempt:{index}",
            "stage_id": stage, "generation": generation, "work_item": work or {
                "obligation": "test-work", "targets": [f"target:{index}"],
                "source_version": "source:1", "completed_digest": "candidate:1",
                "strategy": "source-bound-v1", "trigger": "missing-target"}}


def _work_item(target, *, obligation="repair"):
    return {"obligation": obligation, "targets": [target], "source_version": "source:1",
            "completed_digest": "candidate:1", "strategy": "source-bound-v1", "trigger": "missing-target"}


def test_repair_work_item_identity_allows_new_gap_and_blocks_duplicate(tmp_path):
    journal = setup(tmp_path)
    same_gap = _work_item("claims[0]")

    def attempt(index):
        try:
            with journal.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                reserve(db, journal.run_id, context(index, work=same_gap), purpose="claim_repair",
                        input_estimate=100, output_limit=200)
            return True
        except ProviderError as error:
            assert error.code == "MODEL_WORK_ITEM_NO_PROGRESS"
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt, range(2))) == 1
    with journal.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(3, work=_work_item("claims[1]")), purpose="claim_repair",
                input_estimate=100, output_limit=200)
    with journal.store.connect() as db, pytest.raises(ProviderError) as caught:
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(4, generation=2, work={**same_gap, "trigger": "retry wording changed"}),
                purpose="claim_repair",
                input_estimate=100, output_limit=200)
    assert caught.value.code == "MODEL_WORK_ITEM_NO_PROGRESS"


def test_generation_work_item_identity_allows_new_target_and_blocks_same_target(tmp_path):
    journal = setup(tmp_path)
    first_work = _work_item("sections.business", obligation="generation")
    next_work = _work_item("sections.risk", obligation="generation")
    with journal.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(1, generation=1, work=first_work), purpose="generation",
                input_estimate=100, output_limit=200)
        db.commit()
    with journal.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(2, generation=1, work=next_work), purpose="generation",
                input_estimate=100, output_limit=200)
        db.commit()
    with journal.store.connect() as db, pytest.raises(ProviderError) as caught:
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(3, generation=2, work={**first_work, "trigger": "retry wording changed"}),
                purpose="generation", input_estimate=100, output_limit=200)
    assert caught.value.code == "MODEL_WORK_ITEM_NO_PROGRESS"
    assert journal.store.model_call_plan(journal.run_id)["consumption"]["calls"] == 2


def test_unknown_usage_keeps_tokens_reserved_across_restart(tmp_path):
    journal = setup(tmp_path)
    with journal.store.connect() as db:
        row = db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (journal.run_id,)).fetchone()
        plan = {**json.loads(row[0]), "max_input_estimate": 100}
        db.execute("UPDATE model_run_call_plans SET plan_json=?", (json.dumps(plan),))
        reserve(db, journal.run_id, context(1), purpose="generation", input_estimate=100, output_limit=200)
        settle(db, context(1), {"input_tokens": None, "output_tokens": None}, "remote_unknown")
    restored = setup(tmp_path)
    with restored.store.connect() as db, pytest.raises(ProviderError, match="token budget"):
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(2, "synthesis"), purpose="generation", input_estimate=1, output_limit=200)
