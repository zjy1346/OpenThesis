import json

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun
from openthesis.model_scheduler import ModelRequestScheduler
from openthesis.research_assertions import classify_assertion, validate_with_assertions
from openthesis.research_execution.executor import StageValidation
from openthesis.research_execution.model import ModelStageExecutor
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.storage import Storage


SOURCE = {"evidence_id": "filing:business", "document_id": "annual:2025",
          "document_sha256": "a" * 64, "company_cik": "QA", "end_date": "2025-12-31",
          "page": 7, "raw_text": "The company manufactures and sells industrial refrigeration equipment."}


def setup(tmp_path):
    store = Storage(tmp_path)
    store.save_company(DEMO_COMPANY)
    run = ResearchRun(run_id="proof-312", company=DEMO_COMPANY, workflow_id="test",
        research_pack_id="test", research_pack_version="1", provider_id="test",
        model_id="test", data_as_of="2025-12-31")
    store.save_run(run)
    journal = ModelRequestJournal(store, run.run_id, "configured:test", timeout_seconds=2,
                                  cancel_check=lambda: False)
    registry = StageRegistry([StageSpec(s, "1", "analysis", "verify", output_contract="body")
                              for s in ("business", "synthesis")])
    journal.prepare(registry, "frozen")
    return journal


def validator(texts, source=SOURCE):
    def validate():
        states = [classify_assertion({"kind": "inference", "text": text}, [source]) for text in texts]
        return StageValidation(False, {"states": states}, "completed_partial", record={})
    return validate


class Provider:
    context_window_tokens = 1_000_000

    def __init__(self):
        self.prompts = []

    def generate(self, _system, user, **_kwargs):
        raw = json.loads(user)
        self.prompts.append(raw)
        return {"structured_output_valid": True, "results": [{
            "assertion_id": item["assertion_id"], "state": "supported",
            "source_spans": [{"source_block_id": key, "quote": raw["sources"][key]["raw_text"]}
                             for key in item["source_block_ids"]]}
            for item in raw["assertions"]]}


def test_one_source_copy_and_proof_reused_in_another_stage_after_restart(tmp_path):
    journal = setup(tmp_path)
    provider = Provider()
    texts = ["Equipment sales are the core operating activity.", "Manufacturing is an important business premise."]
    with journal.bind("business", 1):
        first = validate_with_assertions(validator(texts), stage_id="business", journal=journal,
            model_executor=ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal),
            allow_network=True)
    assert first.record["assertion_flow"]["supported"] == 2
    assert len(provider.prompts) == 1
    assert len(provider.prompts[0]["sources"]) == 1
    assert all("sources" not in item for item in provider.prompts[0]["assertions"])
    reopened = ModelRequestJournal(Storage(tmp_path), journal.run_id, "configured:test",
                                    timeout_seconds=2, cancel_check=lambda: False)
    replay = validate_with_assertions(validator(texts), stage_id="synthesis", journal=reopened)
    assert replay.record["assertion_flow"]["supported"] == 2
    assert replay.record["source_flow"]["reused_proofs"] == 2
    changed = validate_with_assertions(validator([texts[0] + " and insurance."],
        {**SOURCE, "raw_text": "The business ceased manufacturing."}), stage_id="synthesis", journal=reopened)
    assert changed.record["assertion_flow"]["supported"] == 0
    assert len(provider.prompts) == 1


def test_prepare_failure_reason_survives_local_replay(tmp_path):
    journal = setup(tmp_path)

    class BrokenExecutor:
        provider = Provider()

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("local failure")

    validate = validator(["Equipment sales are the core operating activity."])
    first = validate_with_assertions(validate, stage_id="business", journal=journal,
                                     model_executor=BrokenExecutor(), allow_network=True)
    assert first.record["source_flow"]["diagnostic_code"] == "ASSERTION_VERIFIER_LOCAL_PROCESSING_FAILED"
    replay = validate_with_assertions(validate, stage_id="business", journal=journal)
    assert replay.record["source_flow"]["diagnostic_code"] == first.record["source_flow"]["diagnostic_code"]
    assert replay.record["source_flow"]["submission_state"] == "not_submitted"


def test_unknown_context_does_not_invent_128k_rejection_and_sources_deduplicate(tmp_path):
    journal = setup(tmp_path)
    source = {**SOURCE, "raw_text": "Complete original source context " * 5000}
    provider = Provider()
    del Provider.context_window_tokens
    try:
        with journal.bind("business", 1):
            result = validate_with_assertions(validator(["Manufacturing remains the operating premise."], source),
                stage_id="business", journal=journal,
                model_executor=ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal),
                allow_network=True)
        assert result.record["assertion_flow"]["supported"] == 1
        assert len(provider.prompts) == 1
    finally:
        Provider.context_window_tokens = 1_000_000


def test_unknown_verifier_outcome_is_not_resubmitted_by_another_stage(tmp_path):
    journal = setup(tmp_path)
    class UnknownProvider(Provider):
        def generate(self, _system, user, **kwargs):
            self.prompts.append(json.loads(user))
            raise TimeoutError('Unknown remote outcome')
    provider = UnknownProvider()
    executor = ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal)
    validate = validator(['Equipment sales are the core operating activity.'])
    with journal.bind('business', 1):
        first = validate_with_assertions(validate, stage_id='business', journal=journal,
            model_executor=executor, allow_network=True)
    assert first.record['source_flow']['submission_state'] == 'remote_unknown'
    with journal.bind('synthesis', 1):
        second = validate_with_assertions(validate, stage_id='synthesis', journal=journal,
            model_executor=executor, allow_network=True)
    assert len(provider.prompts) == 1
    assert second.record['source_flow']['unknown_submission_assertion_ids']
    assert second.record['source_flow']['submission_state'] == 'remote_unknown'
    assert second.record['assertion_flow']['supported'] == 0
