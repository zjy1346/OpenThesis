from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

from test_assertion_retention_312 import Provider, setup, validator
from openthesis.model_scheduler import ModelRequestScheduler
from openthesis.research_assertions import validate_with_assertions
from openthesis.research_execution.model import ModelStageExecutor
from openthesis.research_execution.requests import ModelRequestJournal


def test_two_stage_consumers_send_identical_assertion_only_once(tmp_path):
    first = setup(tmp_path)
    second = ModelRequestJournal(first.store, first.run_id, "configured:test", timeout_seconds=2, cancel_check=lambda: False)
    barrier = threading.Barrier(2)

    class SlowProvider(Provider):
        def generate(self, system, user, **kwargs):
            time.sleep(.15)
            return super().generate(system, user, **kwargs)

    provider = SlowProvider()
    def run(journal, stage):
        barrier.wait(timeout=3)
        with journal.bind(stage, 1):
            return validate_with_assertions(validator(["Equipment sales are the core operating activity."]),
                stage_id=stage, journal=journal,
                model_executor=ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal), allow_network=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        left = executor.submit(run, first, "business")
        right = executor.submit(run, second, "synthesis")
        results = [left.result(timeout=10), right.result(timeout=10)]
    assert len(provider.prompts) == 1, "cross-stage proof cache alone cannot prevent concurrent paid duplicate verification"
    assert all(result.record["assertion_flow"]["supported"] == 1 for result in results)


def test_related_large_sources_pack_to_real_capacity_without_clipping():
    from openthesis.research_assertions import _verification_batches
    source = {"source_block_id": "source:large", "raw_text": "Full context and counterevidence. " * 2400}
    queries = [{"assertion_id": f"a{index}", "text": f"Premise {index}", "sources": [source]} for index in range(12)]
    batches = list(_verification_batches(queries, Provider()))
    assert len(batches) == 1, "fixed 64k creates repeated source uploads on large-capacity models"
    assert batches[0][0]["sources"][0]["raw_text"] == source["raw_text"]


def test_received_verdict_recovers_locally_after_proof_commit_failure(tmp_path):
    journal = setup(tmp_path)
    provider = Provider()
    validate = validator(["Equipment sales are the core operating activity."])
    executor = ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal)
    with journal.bind("business", 1), patch("openthesis.research_execution.assertion_cache.save_proofs", side_effect=RuntimeError("commit interrupted")):
        validate_with_assertions(validate, stage_id="business", journal=journal, model_executor=executor, allow_network=True)
    reopened = ModelRequestJournal(journal.store, journal.run_id, "configured:test", timeout_seconds=.1, cancel_check=lambda: False)
    with reopened.bind("synthesis", 1):
        result = validate_with_assertions(validate, stage_id="synthesis", journal=reopened,
            model_executor=ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=reopened), allow_network=True)
    assert len(provider.prompts) == 1, "a received answer must never be paid for twice"
    assert result.record["assertion_flow"]["supported"] == 1, "local persistence failure must not orphan a received proof"


def test_dispatch_parse_error_cannot_release_unknown_assertion_owner(tmp_path):
    journal = setup(tmp_path)
    provider = Provider()
    executor = ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal)
    validate = validator(["Equipment sales are the core operating activity."])
    with patch.object(executor, "execute", side_effect=ValueError("transport decoding interrupted")) as dispatch, \
         patch.object(journal, "request_observation", return_value={"submission_state": "remote_unknown"}):
        with journal.bind("business", 1):
            first = validate_with_assertions(validate, stage_id="business", journal=journal,
                model_executor=executor, allow_network=True)
        with journal.bind("synthesis", 1):
            second = validate_with_assertions(validate, stage_id="synthesis", journal=journal,
                model_executor=executor, allow_network=True)
    assert dispatch.call_count == 1, "a parser exception is not evidence that the remote request was never sent"
    assert first.record["source_flow"]["submission_state"] == "remote_unknown"
    assert second.record["source_flow"]["unknown_submission_assertion_ids"]
    assert second.record["assertion_flow"]["supported"] == 0


def test_assertion_sender_election_is_atomic_across_processes_and_survives_their_exit(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from types import SimpleNamespace
    from openthesis.research_assertions import VERSION
    from openthesis.research_execution.assertion_cache import acquire_assertions
    journal = setup(tmp_path)
    script = """
import json,sys
from pathlib import Path
from types import SimpleNamespace
from openthesis.storage import Storage
from openthesis.research_assertions import VERSION
from openthesis.research_execution.assertion_cache import acquire_assertions
journal=SimpleNamespace(store=Storage(Path(sys.argv[1])),run_id=sys.argv[2])
print('ready',flush=True)
sys.stdin.readline()
owner,owned,waiting=acquire_assertions(journal,[{'assertion_id':'same-source-premise'}],VERSION)
print(json.dumps({'owned':len(owned),'waiting':len(waiting)}),flush=True)
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, "-c", script, str(tmp_path), journal.run_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=environment))
        for child in children:
            child.stdin.write("start\n")
            child.stdin.flush()
        rows = []
        for child in children:
            output, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            lines = output.splitlines()
            assert lines[0] == "ready"
            rows.append(json.loads(lines[1]))
        assert sum(row["owned"] for row in rows) == 1
        assert sum(row["waiting"] for row in rows) == 1
        _, owned, waiting = acquire_assertions(SimpleNamespace(store=journal.store, run_id=journal.run_id),
            [{"assertion_id": "same-source-premise"}], VERSION)
        assert not owned and len(waiting) == 1, "process exit is not proof of non-submission"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
