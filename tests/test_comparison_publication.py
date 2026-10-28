"""The comparator consumes pinned revisions and never publishes stale input."""
from copy import deepcopy
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import replace
import threading
from unittest.mock import patch

import pytest

from openthesis import comparison
from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from openthesis.storage import Storage


def pair(tmp_path):
    store = Storage(tmp_path / "store")
    store.save_company(DEMO_COMPANY)
    runs = [ResearchRun(name, DEMO_COMPANY, "wf", "pack", "1", "gateway", name,
                        "2026-10-06", RunStatus.COMPLETED) for name in ("primary", "peer")]
    for run in runs:
        store.save_run(run)
        report(store, run, "original")
    return store, runs


def report(store, run, suffix):
    artifact = ResearchArtifact(f"{run.run_id}:report:{suffix}", run.run_id,
        "research-report", "Report", {"report": {"claims": [{"text": suffix}]}},
        model_id=run.model_id, agent_id="synthesis")
    store.save_artifact(artifact)
    return artifact


def comparisons(store, run):
    return [item for item in store.get_artifacts(run.run_id)
            if item["artifact_type"] == "model-comparison"]


@pytest.mark.parametrize("target", [0, 1])
@pytest.mark.parametrize("change", ["report", "metadata"])
def test_input_change_during_comparison_prevents_publication(tmp_path, target, change):
    store, runs = pair(tmp_path)
    original = comparison._claim_texts
    changed = False

    def inject(value):
        nonlocal changed
        if not changed:
            changed = True
            if change == "report":
                report(store, runs[target], "newer")
            else:
                latest = deepcopy(runs[target])
                latest.model_id = "newer-model"
                store.save_run(latest)
        return original(value)

    with patch("openthesis.comparison._claim_texts", inject):
        with pytest.raises(RuntimeError, match="COMPARISON_INPUT_CONFLICT"):
            comparison.compare_research_runs(store, *runs)
    assert comparisons(store, runs[0]) == []
    assert store.get_run(runs[target].run_id)["status"] == "completed"
    if change == "report":
        assert store.latest_report_revision(runs[target].run_id)["generation"] == 2


def test_comparison_uses_current_revision_not_last_artifact_timestamp(tmp_path):
    store, runs = pair(tmp_path)
    original = store.latest_report_revision(runs[0].run_id)
    report(store, runs[0], "newer")
    replayed = ResearchArtifact("primary:replayed-old-report", runs[0].run_id,
        "research-report", "Old report observed late", deepcopy(original["payload"]),
        created_at="2099-01-01T00:00:00+00:00")
    store.save_artifact(replayed)
    head = store.latest_report_revision(runs[0].run_id)
    assert head["generation"] == 2
    result = comparison.compare_research_runs(store, *runs)
    assert result.content["primary_only_claims"] == ["newer"]
    assert result.content["primary"]["revision_id"] == head["revision_id"]
    assert result.content["primary"]["content_sha256"] == head["content_sha256"]


def test_changed_run_configuration_does_not_relabel_an_existing_revision(tmp_path):
    store, runs = pair(tmp_path)
    current = deepcopy(runs[0])
    current.provider_id, current.model_id = "current-provider", "current-model"
    store.save_run(current)
    result = comparison.compare_research_runs(store, *runs)
    assert result.content["primary"]["provider"] == runs[0].provider_id
    assert result.content["primary"]["model"] == runs[0].model_id


def test_missing_peer_report_cannot_be_published_as_empty_comparison(tmp_path):
    store, runs = pair(tmp_path)
    with store.connect() as db:
        db.execute("DELETE FROM latest_report_revisions WHERE run_id=?", (runs[1].run_id,))
        db.execute("DELETE FROM report_revision_contexts WHERE revision_id IN "
                   "(SELECT revision_id FROM report_revisions WHERE run_id=?)", (runs[1].run_id,))
        db.execute("DELETE FROM report_revisions WHERE run_id=?", (runs[1].run_id,))
        db.execute("DELETE FROM artifacts WHERE run_id=?", (runs[1].run_id,))
    with pytest.raises(RuntimeError, match="COMPARISON_REPORT_UNAVAILABLE"):
        comparison.compare_research_runs(store, *runs)
    assert comparisons(store, runs[0]) == []


def test_wrong_company_cannot_enter_comparison(tmp_path):
    store, runs = pair(tmp_path)
    other = replace(DEMO_COMPANY, cik="other-issuer", issuer_id="other-issuer",
                    security_id="other-security", ticker="OTHER")
    store.save_company(other)
    changed = ResearchRun("other-peer", other, "wf", "pack", "1", "gateway",
                          "other-model", "2026-10-06", RunStatus.COMPLETED)
    store.save_run(changed)
    report(store, changed, "other-report")
    with pytest.raises(RuntimeError, match="COMPARISON_COMPANY_CONFLICT"):
        comparison.compare_research_runs(store, runs[0], changed)
    assert comparisons(store, runs[0]) == []


def test_corrupt_revision_hash_never_becomes_comparison_input(tmp_path):
    store, runs = pair(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE report_revisions SET content_sha256=? WHERE run_id=?",
                   ("0" * 64, runs[1].run_id))
    with pytest.raises(RuntimeError, match="COMPARISON_REPORT_INTEGRITY"):
        comparison.compare_research_runs(store, *runs)
    assert comparisons(store, runs[0]) == []


def test_cancel_during_comparison_does_not_publish_artifact_or_complete_attempt(tmp_path):
    store, runs = pair(tmp_path)
    cancelled = threading.Event()
    original = comparison._claim_texts

    def cancel(value):
        cancelled.set()
        return original(value)

    with patch("openthesis.comparison._claim_texts", cancel):
        with pytest.raises(CancelledError):
            comparison.compare_research_runs(store, *runs, cancel_check=cancelled.is_set)
    assert comparisons(store, runs[0]) == []
    assert store.continuity_snapshot(runs[0].run_id)["attempts"] == []


def test_cancel_after_insert_rolls_back_both_artifact_and_attempt(tmp_path):
    store, runs = pair(tmp_path)
    cancelled = threading.Event()
    original = store._save_artifact_db

    def insert_then_cancel(db, artifact):
        original(db, artifact)
        cancelled.set()

    with patch.object(store, "_save_artifact_db", insert_then_cancel):
        with pytest.raises(CancelledError):
            comparison.compare_research_runs(store, *runs, cancel_check=cancelled.is_set)
    assert comparisons(store, runs[0]) == []
    assert store.continuity_snapshot(runs[0].run_id)["attempts"] == []


def test_stage_insert_failure_rolls_back_comparison_artifact(tmp_path):
    store, runs = pair(tmp_path)
    revisions = [store.latest_report_revision(run.run_id) for run in runs]
    with store.connect() as db:
        db.execute("""CREATE TRIGGER comparison_stage_abort
            BEFORE INSERT ON research_stage_attempts
            BEGIN SELECT RAISE(ABORT, 'INJECTED_COMPARISON_FAILURE'); END""")
    with pytest.raises(Exception, match="INJECTED_COMPARISON_FAILURE"):
        comparison.compare_research_runs(store, *runs, stage_id="comparison:1")
    assert comparisons(store, runs[0]) == []
    assert store.continuity_snapshot(runs[0].run_id)["attempts"] == []
    assert [store.latest_report_revision(run.run_id) for run in runs] == revisions


def test_comparison_and_success_attempt_pin_both_revisions(tmp_path):
    store, runs = pair(tmp_path)
    before = [store.latest_report_revision(run.run_id) for run in runs]
    result = comparison.compare_research_runs(store, *runs, stage_id="comparison:1")
    stage = store.continuity_snapshot(runs[0].run_id)["latest"]
    assert stage["stage"] == "comparison:1" and stage["outcome"] == "complete"
    assert stage["diagnostics"] == {
        "comparison_artifact_id": result.artifact_id,
        "primary_revision_id": before[0]["revision_id"],
        "secondary_revision_id": before[1]["revision_id"],
    }
    assert [store.latest_report_revision(run.run_id) for run in runs] == before


def test_two_report_heads_share_one_database_read_view(tmp_path):
    store, runs = pair(tmp_path)
    connect = store.connect
    injected = False

    class ConnectionProbe:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, params=()):
            nonlocal injected
            cursor = self.db.execute(sql, params)
            if not injected and "head_generation" in sql and params == (runs[0].run_id,):
                injected = True
                report(store, runs[1], "peer-newer")
            return cursor

    @contextmanager
    def probed_connect():
        with connect() as db:
            yield ConnectionProbe(db)

    with patch.object(store, "connect", probed_connect):
        captured = store.get_comparison_snapshot(runs[0].run_id, runs[1].run_id)
    assert injected
    assert captured["secondary"]["generation"] == 1
    assert store.latest_report_revision(runs[1].run_id)["generation"] == 2
