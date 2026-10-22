from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.call_budget import reserve, settle
from test_assertion_retention_312 import setup


def context(index, stage="business"):
    return {"request_id": f"request:{index}", "attempt_id": f"attempt:{index}",
            "stage_id": stage, "generation": 1}


def test_repair_purposes_share_atomic_one_per_stage_and_two_per_run(tmp_path):
    journal = setup(tmp_path)

    def attempt(index):
        try:
            with journal.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                reserve(db, journal.run_id, context(index), purpose=("field_repair" if index % 2 else "claim_repair"),
                        input_estimate=100, output_limit=200)
            return True
        except ProviderError as error:
            assert error.code == "MODEL_RUN_REPAIR_BUDGET_EXHAUSTED"
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt, range(2))) == 1
    with journal.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(3, "synthesis"), purpose="section_delivery",
                input_estimate=100, output_limit=200)
    with journal.store.connect() as db, pytest.raises(ProviderError, match="repair budget"):
        db.execute("BEGIN IMMEDIATE")
        reserve(db, journal.run_id, context(4, "another-purpose"), purpose="output_repair",
                input_estimate=100, output_limit=200)


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
