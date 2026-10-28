"""Final reports must share the commit of the run outcome, not precede it."""
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchCancelled, ResearchWorkflow
from openthesis.storage import Storage


def setup(tmp_path, previous=False):
    store = Storage(tmp_path / "store")
    store.save_company(DEMO_COMPANY)
    run = ResearchRun("publication-run", DEMO_COMPANY, "wf", "pack", "1",
                      "gateway", "", "2026-10-06", RunStatus.RUNNING)
    store.save_run(run)
    if previous:
        store.save_artifact(ResearchArtifact("prior-report", run.run_id, "research-report",
            "Prior report", {"report": {"claims": [{"text": "Previously committed"}]}}))
    return store, run


@pytest.mark.parametrize("previous", [False, True])
def test_report_cannot_commit_before_its_run_outcome(tmp_path, previous):
    store, run = setup(tmp_path, previous)
    before = store.latest_report_revision(run.run_id)
    with store.connect() as db:
        db.execute("""CREATE TRIGGER final_run_update_abort
            BEFORE UPDATE OF status ON research_runs WHEN NEW.status='partial'
            BEGIN SELECT RAISE(ABORT, 'INJECTED_REPORT_RESULT_FAILURE'); END""")
    workflow = ResearchWorkflow(store, builtin_pack(), None, ModelConfig())
    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_REPORT_RESULT_FAILURE"):
        workflow.run(DEMO_COMPANY, demo_facts(), existing_run=run)
    assert store.latest_report_revision(run.run_id) == before
    reports = [item for item in store.get_artifacts(run.run_id)
               if item["artifact_type"] == "research-report"]
    assert len(reports) == int(previous)


def test_current_unknown_metadata_survives_final_publication(tmp_path):
    store, run = setup(tmp_path)

    def progress(_message, percent):
        if percent == 15:
            with store.connect() as db:
                payload = json.loads(db.execute("SELECT payload_json FROM research_runs WHERE run_id=?",
                                                (run.run_id,)).fetchone()[0])
                payload["future_extension"] = {"added_during_execution": True}
                db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                           (json.dumps(payload), run.run_id))

    result = ResearchWorkflow(store, builtin_pack(), None, ModelConfig()).run(
        DEMO_COMPANY, demo_facts(), existing_run=run, progress=progress)
    assert result.status is RunStatus.PARTIAL
    payload = json.loads(store.get_run(run.run_id)["payload_json"])
    assert payload["future_extension"] == {"added_during_execution": True}


def test_changed_financial_input_cannot_publish_old_report_or_be_overwritten(tmp_path):
    store, run = setup(tmp_path)

    def progress(_message, percent):
        if percent == 15:
            with store.connect() as db:
                payload = json.loads(db.execute("SELECT payload_json FROM research_runs WHERE run_id=?",
                                                (run.run_id,)).fetchone()[0])
                payload["data_snapshot"] = {"financial_generations": {"filing": "newer"}}
                db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                           (json.dumps(payload), run.run_id))

    with pytest.raises(RuntimeError, match="RESEARCH_PUBLICATION_INPUT_CONFLICT"):
        ResearchWorkflow(store, builtin_pack(), None, ModelConfig()).run(
            DEMO_COMPANY, demo_facts(), existing_run=run, progress=progress)
    assert store.latest_report_revision(run.run_id) is None
    payload = json.loads(store.get_run(run.run_id)["payload_json"])
    assert payload["data_snapshot"] == {"financial_generations": {"filing": "newer"}}


def test_cancel_before_final_publication_preserves_prior_revision(tmp_path):
    store, run = setup(tmp_path, previous=True)
    before = store.latest_report_revision(run.run_id)
    cancelled = threading.Event()

    def progress(_message, percent):
        if percent == 15:
            cancelled.set()

    with pytest.raises(ResearchCancelled):
        ResearchWorkflow(store, builtin_pack(), None, ModelConfig(),
            cancel_check=cancelled.is_set).run(DEMO_COMPANY, demo_facts(),
                                             existing_run=run, progress=progress)
    assert store.latest_report_revision(run.run_id) == before


def test_existing_extensions_are_preserved_when_execution_starts(tmp_path):
    store, run = setup(tmp_path)
    with store.connect() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM research_runs WHERE run_id=?",
                                        (run.run_id,)).fetchone()[0])
        payload["future_extension"] = {"preexisting": True}
        payload["errors"].append("prior-diagnostic")
        db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                   (json.dumps(payload), run.run_id))
    ResearchWorkflow(store, builtin_pack(), None, ModelConfig()).run(
        DEMO_COMPANY, demo_facts(), existing_run=run)
    saved = json.loads(store.get_run(run.run_id)["payload_json"])
    assert saved["future_extension"] == {"preexisting": True}
    assert "prior-diagnostic" in saved["errors"]


def test_stale_execution_envelope_cannot_reset_newer_input(tmp_path):
    store, run = setup(tmp_path)
    with store.connect() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM research_runs WHERE run_id=?",
                                        (run.run_id,)).fetchone()[0])
        payload["data_snapshot"] = {"financial_generations": {"filing": "newer-before-start"}}
        db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                   (json.dumps(payload), run.run_id))
    with pytest.raises(RuntimeError, match="RESEARCH_PUBLICATION_INPUT_CONFLICT"):
        ResearchWorkflow(store, builtin_pack(), None, ModelConfig()).run(
            DEMO_COMPANY, demo_facts(), existing_run=run)
    assert store.latest_report_revision(run.run_id) is None
    assert json.loads(store.get_run(run.run_id)["payload_json"])["data_snapshot"] == {
        "financial_generations": {"filing": "newer-before-start"}}


def test_publication_rejects_database_subject_mismatch(tmp_path):
    store, run = setup(tmp_path)
    other = replace(DEMO_COMPANY, cik="different-real-issuer")
    store.save_company(other)
    with store.connect() as db:
        db.execute("UPDATE research_runs SET company_cik=? WHERE run_id=?", (other.cik, run.run_id))
    with pytest.raises(RuntimeError, match="CONTINUITY_RUN_PAYLOAD_INVALID"):
        ResearchWorkflow(store, builtin_pack(), None, ModelConfig()).run(
            DEMO_COMPANY, demo_facts(), existing_run=run)
    assert store.latest_report_revision(run.run_id) is None
