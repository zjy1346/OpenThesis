from pathlib import Path
from unittest.mock import patch
import pytest

from openthesis.storage import Storage


def test_second_start_does_not_scan_current_candidate_ledger(tmp_path: Path):
    Storage(tmp_path)
    with patch.object(Storage, "_backfill_active_fact_generations", wraps=Storage._backfill_active_fact_generations) as backfill:
        Storage(tmp_path)
    assert backfill.call_count == 0, "ordinary startup must not parse all historical candidates"


def test_second_start_does_not_run_full_integrity_scan(tmp_path: Path):
    Storage(tmp_path)
    import sqlite3
    original = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(lambda sql: statements.append(sql))
        return connection

    with patch("openthesis.storage.sqlite3.connect", side_effect=traced):
        Storage(tmp_path)
    assert not any("quick_check" in statement.casefold() for statement in statements), "full health checks belong outside the normal startup critical path"


def test_interrupted_required_migration_does_not_mark_ready(tmp_path: Path):
    storage = Storage(tmp_path)
    with storage.connect() as db:
        db.execute("UPDATE metadata SET value='19' WHERE key='schema_version'")
        db.execute("DELETE FROM metadata WHERE key='startup_migration'")
    with patch.object(Storage, "_backfill_active_fact_generations", side_effect=RuntimeError("interrupted")):
        with pytest.raises(RuntimeError, match="interrupted"):
            Storage(tmp_path)
    with storage.connect() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == '19'
        assert db.execute("SELECT value FROM metadata WHERE key='startup_migration'").fetchone() is None
    restarted = Storage(tmp_path)
    with restarted.connect() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == '20'


def test_minimum_bootstrap_does_not_load_history_or_packs(tmp_path):
    from openthesis.service import AppService
    service = AppService(tmp_path)
    with patch.object(service, "list_research_runs", side_effect=AssertionError("history blocks navigation")), \
         patch.object(service, "research_packs", side_effect=AssertionError("packs block navigation")), \
         patch.object(service, "common_companies", side_effect=AssertionError("companies block navigation")):
        result = service.bootstrap(minimal=True)
    assert result["preferences"]["ui_language"]
    assert result["recent_runs"] == []
def test_startup_progress_reports_real_migration_but_no_fake_same_version_backfill(tmp_path):
    from openthesis.storage import Storage
    events = []
    store = Storage(tmp_path, initialization_progress=lambda *args: events.append(args))
    assert events[0][0] == "checking_schema"
    assert events[-1][0] == "storage_ready"
    assert "migrating_schema" in {event[0] for event in events}
    with store.connect() as db:
        db.execute("DELETE FROM metadata WHERE key='startup_migration'")
    events.clear()
    Storage(tmp_path, initialization_progress=lambda *args: events.append(args))
    assert "checking_integrity" in {event[0] for event in events}
    backup = [event for event in events if event[0] == "backing_up" and event[1] is not None]
    assert backup and all(0 <= event[1] <= event[2] for event in backup)
    events.clear()
    Storage(tmp_path, initialization_progress=lambda *args: events.append(args))
    assert [event[0] for event in events] == ["checking_schema", "storage_ready"]


def test_background_corruption_signal_preserves_original_and_requires_explicit_recovery(tmp_path):
    from unittest.mock import MagicMock
    from openthesis.storage import StorageCorruptionError
    store = Storage(tmp_path)
    before = store.db_path.read_bytes()
    with patch("openthesis.storage.threading.Timer") as timer:
        store.start_background_maintenance()
    check = timer.call_args.args[1]
    prior, result = MagicMock(), MagicMock()
    prior.fetchone.return_value = None
    result.fetchall.return_value = [("database disk image is malformed",)]
    with patch("openthesis.storage.sqlite3.connect") as connect:
        # query_only, prior timestamp, quick_check; order matters below.
        connect.return_value.execute.side_effect = [None, prior, result]
        check()
    assert store.maintenance_status == {"state": "recovery_required",
        "code": "STORAGE_HEALTH_RECOVERY_REQUIRED", "next_action": "restore_verified_backup_or_export"}
    with pytest.raises(StorageCorruptionError, match="STORAGE_HEALTH_RECOVERY_REQUIRED"):
        with store.connect():
            pass
    assert store.db_path.read_bytes() == before


def test_health_recovery_is_explicit_at_rpc_boundary_without_blocking_cancel_or_hello(tmp_path):
    import json
    from openthesis.service import AppService
    from openthesis.sidecar import JsonLineServer
    service = AppService(tmp_path)
    service.storage._health_error = "STORAGE_HEALTH_RECOVERY_REQUIRED"
    service.storage.maintenance_status = {"state": "recovery_required", "code": service.storage._health_error}
    server = JsonLineServer(service)
    def request(method, **params):
        return server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
    for method, params in [("app.bootstrap", {"minimal": True}),
                           ("research.get_report", {"run_id": "retained-run"}),
                           ("research.status", {"job_id": "job"})]:
        response = request(method, **params)
        assert response["error"] == {"code": -32040, "message": "STORAGE_HEALTH_RECOVERY_REQUIRED"}
    assert request("system.hello")["result"]["storage_health"]["state"] == "recovery_required"
    with patch.object(service, "cancel_research", return_value={"state": "cancelling"}) as cancel:
        assert request("research.cancel", job_id="job")["result"]["state"] == "cancelling"
        cancel.assert_called_once_with("job")


def test_corruption_signal_during_an_open_write_rolls_it_back(tmp_path):
    import sqlite3
    from openthesis.storage import StorageCorruptionError
    store = Storage(tmp_path)
    with pytest.raises(StorageCorruptionError, match="STORAGE_HEALTH_RECOVERY_REQUIRED"):
        with store.connect() as db:
            db.execute("INSERT INTO metadata VALUES('health-race-probe','must-not-commit')")
            store._health_error = "STORAGE_HEALTH_RECOVERY_REQUIRED"
    with sqlite3.connect(store.db_path.resolve().as_uri() + "?mode=ro", uri=True) as reader:
        assert reader.execute("SELECT value FROM metadata WHERE key='health-race-probe'").fetchone() is None


def test_rpc_keeps_health_code_when_report_wrapper_observes_signal_mid_read(tmp_path):
    import json
    from openthesis.application.reports import ReportReadError
    from openthesis.service import AppService
    from openthesis.sidecar import JsonLineServer
    service = AppService(tmp_path)
    def race(*_args, **_kwargs):
        service.storage.maintenance_status = {"state": "recovery_required", "code": "STORAGE_HEALTH_RECOVERY_REQUIRED"}
        raise ReportReadError("REPORT_STORAGE_UNAVAILABLE")
    with patch.object(service, "get_report", side_effect=race):
        result = JsonLineServer(service)._handle_line(json.dumps({"jsonrpc": "2.0", "id": 1,
            "method": "research.get_report", "params": {"run_id": "retained-report"}}))
    assert result["error"] == {"code": -32040, "message": "STORAGE_HEALTH_RECOVERY_REQUIRED"}

