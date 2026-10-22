"""Bounded, durable UI projections. Never use these as research evidence."""
from __future__ import annotations

import json
import sqlite3


def initialize_read_models(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS model_status_changes (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        kind TEXT NOT NULL, record_id TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS model_status_run_cursor ON model_status_changes(run_id,seq)")
    db.execute("""CREATE TRIGGER IF NOT EXISTS status_run_cleanup AFTER DELETE ON research_runs
        BEGIN DELETE FROM model_status_changes WHERE run_id=OLD.run_id; END""")
    db.execute("""CREATE TABLE IF NOT EXISTS model_status_snapshots (
        run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, calls_json TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""")
    for table, kind, key in (
        ("model_request_journal", "request", "request_id"),
        ("model_wire_diagnostics", "request", "request_id"),
        ("model_result_reuse", "meta", "run_id"),
        ("model_assertion_proofs", "meta", "run_id"),
        ("model_run_call_plans", "meta", "run_id"),
        ("model_budget_gaps", "meta", "run_id"),
    ):
        for event in ("INSERT", "UPDATE", "DELETE"):
            ref = "OLD" if event == "DELETE" else "NEW"
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS status_{table}_{event.lower()}
                AFTER {event} ON {table} BEGIN
                INSERT INTO model_status_changes(run_id,kind,record_id)
                VALUES({ref}.run_id,'{kind}',{ref}.{key}); END""")
    for table in ("recovery_events", "recovery_actions"):
        for event in ("INSERT", "UPDATE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS status_{table}_{event.lower()}
                AFTER {event} ON {table} BEGIN
                INSERT INTO model_status_changes(run_id,kind,record_id)
                SELECT i.run_id,'recovery',NEW.action_id FROM recovery_plans p
                JOIN recovery_incidents i ON i.incident_id=p.incident_id
                WHERE p.plan_id=NEW.plan_id; END""")

    db.execute("""CREATE TABLE IF NOT EXISTS research_run_summaries (
        run_id TEXT PRIMARY KEY, summary_json TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""")
    db.execute("CREATE INDEX IF NOT EXISTS research_runs_recent ON research_runs(started_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS research_runs_state ON research_runs(status)")
    db.execute("CREATE INDEX IF NOT EXISTS artifact_run_type ON artifacts(run_id,artifact_type)")
    # Executed at the original write, not 20 times when the history opens.
    expression = """json_object(
        'report_language',COALESCE(json_extract(NEW.payload_json,'$.report_language'),'zh-CN'),
        'market',COALESCE(json_extract(NEW.payload_json,'$.company.market'),'US'),
        'exchange',COALESCE(json_extract(NEW.payload_json,'$.company.exchange'),''),
        'listing_currency',COALESCE(json_extract(NEW.payload_json,'$.company.listing_currency'),'USD'),
        'reporting_currency',COALESCE(json_extract(NEW.payload_json,'$.company.reporting_currency'),''),
        'industry_support',COALESCE(json_extract(NEW.payload_json,'$.company.industry_support'),'standard'))"""
    for event, suffix in (("INSERT", "insert"), ("UPDATE OF payload_json", "update")):
        db.execute(f"DROP TRIGGER IF EXISTS run_summary_{suffix}")
        db.execute(f"""CREATE TRIGGER run_summary_{suffix}
            AFTER {event} ON research_runs WHEN json_valid(NEW.payload_json)
            BEGIN INSERT INTO research_run_summaries VALUES(NEW.run_id,{expression})
            ON CONFLICT(run_id) DO UPDATE SET summary_json=excluded.summary_json; END""")


def list_run_summaries(db: sqlite3.Connection, limit: int) -> list[dict]:
    rows = db.execute("""SELECT r.run_id,r.status,r.started_at,r.completed_at,c.ticker,c.name,
        s.summary_json,CASE WHEN s.run_id IS NULL THEN r.payload_json ELSE NULL END AS legacy_payload
        FROM research_runs r JOIN companies c ON c.cik=r.company_cik
        LEFT JOIN research_run_summaries s ON s.run_id=r.run_id
        ORDER BY r.started_at DESC LIMIT ?""", (min(200, max(1, limit)),)).fetchall()
    result = []
    for row in rows:
        if row["summary_json"] is not None:
            summary = json.loads(row["summary_json"])
        else:
            try:
                payload = json.loads(row["legacy_payload"] or "{}")
                company = payload.get("company", {})
                if not isinstance(company, dict):
                    company = {}
                summary = {key: company.get(key, default) for key, default in (
                    ("market", "US"), ("exchange", ""), ("listing_currency", "USD"),
                    ("reporting_currency", ""), ("industry_support", "standard"))}
                summary["report_language"] = payload.get("report_language", "zh-CN")
            except (ValueError, TypeError, AttributeError):
                summary = {"reporting_currency": "", "report_language": "zh-CN"}
            db.execute("INSERT OR IGNORE INTO research_run_summaries VALUES(?,?)",
                       (row["run_id"], json.dumps(summary, ensure_ascii=False)))
        currency = summary.get("reporting_currency") or ""
        result.append({**{key: row[key] for key in ("run_id", "status", "started_at", "completed_at")},
                       "ticker": row["ticker"], "company_name": row["name"], **summary,
                       "reporting_currency_confirmation": {"state": "saved_identity" if currency else "unresolved",
                                                           "currency": currency, "source": "run" if currency else "none"}})
    return result


def status_snapshot(storage, run_id: str) -> tuple[int, list[dict]]:
    """Rebuild once on restart/legacy; thereafter decode only changed receipts.

    The change cursor is captured before work. A concurrent producer appends a
    newer cursor and is never acknowledged by an older reader. CAS publication
    prevents another slow reader from overwriting a newer snapshot.
    """
    with storage.connect() as db:
        if not db.execute("SELECT 1 FROM research_runs WHERE run_id=?", (run_id,)).fetchone():
            return 0, []
        previous = db.execute("SELECT revision,calls_json FROM model_status_snapshots WHERE run_id=?", (run_id,)).fetchone()
        changes = db.execute("SELECT seq,kind,record_id FROM model_status_changes WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    if previous is not None and not changes:
        return int(previous[0]), json.loads(previous[1])
    revision = max([int(previous[0]) if previous else 0, *[int(row[0]) for row in changes]])
    requests = {str(row[2]) for row in changes if row[1] == "request"}
    recovery = {str(row[2]) for row in changes if row[1] == "recovery"}
    existing = {(call["request_id"], call["attempt_id"]): call for call in json.loads(previous[1])} if previous else {}
    updated = storage._build_model_call_status(run_id, request_ids=requests if previous else None,
                                               recovery_action_ids=recovery if previous else None)
    # Remove superseded owners of changed logical requests, not unrelated calls.
    for key in tuple(existing):
        if key[0] in requests:
            del existing[key]
    for call in updated:
        existing[(call["request_id"], call["attempt_id"])] = call
    # Metadata is small and shared. Do not duplicate the plan in every card.
    meta = next((call for call in updated if "reuse_summary" in call), None)
    if meta is None:
        with storage.connect() as db:
            from .call_plan import status_plan_metadata
            meta = status_plan_metadata(db, run_id)
    else:
        meta = {key: meta[key] for key in ("reuse_summary", "call_plan") if key in meta}
    calls = list(existing.values())
    for call in calls:
        call.pop("reuse_summary", None)
        call.pop("call_plan", None)
    if calls:
        calls[0].update(meta)
    with storage.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT revision,calls_json FROM model_status_snapshots WHERE run_id=?", (run_id,)).fetchone()
        if current and int(current[0]) >= revision:
            return int(current[0]), json.loads(current[1])
        db.execute("INSERT OR REPLACE INTO model_status_snapshots VALUES(?,?,?)", (run_id, revision, json.dumps(calls, ensure_ascii=False)))
        db.execute("DELETE FROM model_status_changes WHERE run_id=? AND seq<=?", (run_id, revision))
    return revision, calls
