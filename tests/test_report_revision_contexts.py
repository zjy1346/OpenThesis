import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from openthesis.storage import SCHEMA_VERSION, Storage
from openthesis.storage_comparison import ComparisonError, _canonical


def _run(run_id: str, model_id: str) -> ResearchRun:
    run = ResearchRun(
        run_id, DEMO_COMPANY, "workflow", "pack", "1", "gateway", model_id,
        "2026-10-06", RunStatus.COMPLETED,
    )
    run.model_configuration = {
        "api_key": "do-not-persist-in-revision-context",
        "endpoint": "https://secret.example.invalid/v1",
        "configuration_version": "cfg-1",
    }
    return run


def _report(store: Storage, run: ResearchRun, suffix: str) -> ResearchArtifact:
    artifact = ResearchArtifact(
        f"{run.run_id}:report:{suffix}", run.run_id, "research-report", "Report",
        {"report": {"claims": [{"text": suffix}]}},
        model_id=run.model_id, agent_id="synthesis",
    )
    store.save_artifact(artifact)
    return artifact


def _pair(tmp_path):
    store = Storage(tmp_path / "store")
    store.save_company(DEMO_COMPANY)
    primary = _run("primary", "primary-model")
    secondary = _run("secondary", "secondary-model")
    store.save_run(primary)
    store.save_run(secondary)
    _report(store, primary, "primary-original")
    _report(store, secondary, "secondary-original")
    return store, primary, secondary


def _context_row(store: Storage, revision_id: str):
    with store.connect() as db:
        return db.execute(
            "SELECT context_json, context_sha256 FROM report_revision_contexts "
            "WHERE revision_id=?", (revision_id,)
        ).fetchone()


def test_new_revision_context_freezes_public_identity_without_secrets(tmp_path):
    store, primary, _ = _pair(tmp_path)
    revision = store.latest_report_revision(primary.run_id)
    row = _context_row(store, revision["revision_id"])
    assert row is not None

    context = json.loads(row["context_json"])
    assert context["schema"] == "report-revision-context-v1"
    assert context["revision_id"] == revision["revision_id"]
    assert context["run_id"] == primary.run_id
    assert context["provider"] == primary.provider_id
    assert context["model"] == primary.model_id
    assert "api_key" not in row["context_json"]
    assert "secret.example.invalid" not in row["context_json"]
    assert row["context_sha256"] == hashlib.sha256(
        row["context_json"].encode("utf-8")
    ).hexdigest()
    with store.connect() as db:
        payload = json.loads(db.execute(
            "SELECT payload_json FROM research_runs WHERE run_id=?", (primary.run_id,)
        ).fetchone()[0])
    assert context["publishing_run_sha256"] == hashlib.sha256(
        _canonical(payload).encode("utf-8")
    ).hexdigest()


def test_repeated_same_revision_does_not_relabel_context_after_metadata_change(tmp_path):
    store, primary, _ = _pair(tmp_path)
    artifact = store.get_artifacts(primary.run_id)[0]
    revision_before = store.latest_report_revision(primary.run_id)
    context_before = json.loads(_context_row(
        store, revision_before["revision_id"]
    )["context_json"])

    changed = deepcopy(primary)
    changed.model_id = "later-model"
    changed.model_configuration = {"api_key": "new-secret"}
    store.save_run(changed)
    store.save_artifact(ResearchArtifact(
        artifact["artifact_id"], primary.run_id, "research-report", "Report",
        deepcopy(artifact["content"]), model_id=primary.model_id,
        agent_id=artifact["agent_id"], created_at=artifact["created_at"],
    ))

    revision_after = store.latest_report_revision(primary.run_id)
    assert revision_after["revision_id"] == revision_before["revision_id"]
    context_after = json.loads(_context_row(
        store, revision_after["revision_id"]
    )["context_json"])
    assert context_after == context_before
    assert context_after["model"] == "primary-model"


def test_context_insert_failure_rolls_back_report_artifact_revision_and_head(tmp_path):
    store, primary, _ = _pair(tmp_path)
    before_head = store.latest_report_revision(primary.run_id)
    before_artifacts = store.get_artifacts(primary.run_id)
    with store.connect() as db:
        db.execute("""CREATE TRIGGER revision_context_abort
            BEFORE INSERT ON report_revision_contexts
            BEGIN SELECT RAISE(ABORT, 'INJECTED_REVISION_CONTEXT_FAILURE'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_REVISION_CONTEXT_FAILURE"):
        _report(store, primary, "must-rollback")

    assert store.latest_report_revision(primary.run_id) == before_head
    assert store.get_artifacts(primary.run_id) == before_artifacts
    with store.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM report_revision_contexts WHERE revision_id IN "
            "(SELECT revision_id FROM report_revisions WHERE run_id=?)",
            (primary.run_id,),
        ).fetchone()[0] == 1


def test_schema17_upgrade_preserves_old_revisions_without_backfilling_context(tmp_path):
    store, primary, secondary = _pair(tmp_path)
    with store.connect() as db:
        artifacts_before = [tuple(row) for row in db.execute(
            "SELECT artifact_id, payload_json FROM artifacts ORDER BY artifact_id"
        ).fetchall()]
        revisions_before = [tuple(row) for row in db.execute(
            "SELECT revision_id, payload_json, content_sha256 FROM report_revisions "
            "ORDER BY revision_id"
        ).fetchall()]
        db.execute("UPDATE metadata SET value='17' WHERE key='schema_version'")
        db.execute("DROP TABLE report_revision_contexts")

    migrated = Storage(store.data_dir)
    backup = migrated.migration_backup
    assert backup is not None
    assert backup.parent == store.data_dir / "backups"
    with sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True) as backup_db:
        backup_db.execute("PRAGMA query_only = ON")
        assert backup_db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup_db.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "17"
        assert backup_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='report_revision_contexts'"
        ).fetchone() is None
        assert [tuple(row) for row in backup_db.execute(
            "SELECT artifact_id, payload_json FROM artifacts ORDER BY artifact_id"
        ).fetchall()] == artifacts_before
        assert [tuple(row) for row in backup_db.execute(
            "SELECT revision_id, payload_json, content_sha256 FROM report_revisions "
            "ORDER BY revision_id"
        ).fetchall()] == revisions_before
    with migrated.connect() as db:
        assert db.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        assert db.execute("SELECT COUNT(*) FROM report_revision_contexts").fetchone()[0] == 0
        assert [tuple(row) for row in db.execute(
            "SELECT artifact_id, payload_json FROM artifacts ORDER BY artifact_id"
        ).fetchall()] == artifacts_before
        assert [tuple(row) for row in db.execute(
            "SELECT revision_id, payload_json, content_sha256 FROM report_revisions "
            "ORDER BY revision_id"
        ).fetchall()] == revisions_before

    with pytest.raises(ComparisonError, match="COMPARISON_REPORT_IDENTITY_UNVERIFIED"):
        migrated.get_comparison_snapshot(primary.run_id, secondary.run_id)


@pytest.mark.parametrize("corruption", ["json", "hash"])
def test_corrupt_revision_context_is_rejected(corruption, tmp_path):
    store, primary, secondary = _pair(tmp_path)
    revision_id = store.latest_report_revision(primary.run_id)["revision_id"]
    with store.connect() as db:
        if corruption == "json":
            db.execute(
                "UPDATE report_revision_contexts SET context_json=? WHERE revision_id=?",
                ('{"schema":"report-revision-context-v1","revision_id":"wrong"}', revision_id),
            )
        else:
            db.execute(
                "UPDATE report_revision_contexts SET context_sha256=? WHERE revision_id=?",
                ("0" * 64, revision_id),
            )
    with pytest.raises(ComparisonError, match="COMPARISON_REPORT_INTEGRITY"):
        store.get_comparison_snapshot(primary.run_id, secondary.run_id)


def test_delete_run_removes_only_its_revision_context(tmp_path):
    store, primary, secondary = _pair(tmp_path)
    primary_revision = store.latest_report_revision(primary.run_id)["revision_id"]
    secondary_revision = store.latest_report_revision(secondary.run_id)["revision_id"]
    assert store.delete_run(primary.run_id) is True
    with store.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM report_revision_contexts WHERE revision_id=?",
            (primary_revision,),
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM report_revision_contexts WHERE revision_id=?",
            (secondary_revision,),
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM report_revisions WHERE run_id=?",
            (secondary.run_id,),
        ).fetchone()[0] == 1
