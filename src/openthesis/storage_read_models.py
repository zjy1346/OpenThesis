"""Bounded, durable UI projections. Never use these as research evidence."""
from __future__ import annotations

import json
import sqlite3
import math
import re
from datetime import datetime


def research_diagnostic_snapshot(store, run_id):
    """Read-only, source-free attribution. Null never means zero billing."""
    row = store.get_run(run_id)
    if row is None:
        raise KeyError("research run not found")
    payload = json.loads(row.get("payload_json") or "{}")
    def number(value):
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    def token(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", value) else None
    def count(value):
        return value if type(value) is int and value >= 0 else None
    def mapping(value):
        return value if isinstance(value, dict) else {}
    def date(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            return None
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "reasoning_tokens", "answer_tokens")
    calls = []
    stage_timeline = {}
    job_phases = None
    quality = {"run_status": token(payload.get("status")), "assessment": "not_reassessed_by_export",
        "saved_report_result": None}
    with store.connect() as db:
        report = db.execute("SELECT payload_json FROM artifacts WHERE run_id=? AND artifact_type='research-report' ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (run_id,)).fetchone()
        if report:
            try:
                from .resilience.payloads import load_payload
                saved = mapping(load_payload(db, report[0]))
                readiness = mapping(saved.get("readiness"))
                verification = mapping(saved.get("verification"))
                from .report_readiness import REQUIRED_REPORT_SECTIONS
                quality["saved_report_result"] = {
                    "state": token(readiness.get("state")),
                    "complete": readiness.get("complete") if type(readiness.get("complete")) is bool else None,
                    "verification_passed": verification.get("passed") if type(verification.get("passed")) is bool else None,
                    "missing_sections": [key for key in readiness.get("missing_sections", ()) if key in REQUIRED_REPORT_SECTIONS],
                    **{key: count(verification.get(key)) for key in ("verified_claim_count", "unsupported_fact_count", "unknown_claim_count")}}
            except (ValueError, TypeError, KeyError, RecursionError):
                quality["saved_report_result"] = {"state": "saved_result_unavailable", "complete": None}
        job = db.execute("SELECT snapshot_json FROM research_jobs WHERE run_id=? ORDER BY updated_at DESC LIMIT 1", (run_id,)).fetchone()
        if job:
            snapshot = mapping(json.loads(job[0]))
            job_phases = {"stage_seconds": {token(key): number(value) for key, value in mapping(snapshot.get("stage_timings")).items() if token(key)},
                "elapsed_seconds": number(snapshot.get("elapsed_seconds")),
                "engine_active_seconds": number(snapshot.get("engine_active_seconds")),
                "external_wait_seconds": number(snapshot.get("external_wait_seconds")), "state": token(snapshot.get("state"))}
        wire = db.execute("SELECT * FROM model_wire_diagnostics WHERE run_id=? ORDER BY created_at,request_id,attempt_id", (run_id,)).fetchall()
        reservations = {(item["request_id"], item["attempt_id"]): dict(item) for item in db.execute(
            "SELECT * FROM model_call_reservations WHERE run_id=?", (run_id,))}
        for item in wire:
            raw = mapping(json.loads(item["diagnostics_json"]))
            timeline = raw.get("stage_timeline", {})
            if isinstance(timeline, dict) and timeline:
                safe = {key: number(timeline.get(key)) for key in ("queued_at_epoch", "producer_started_at_epoch", "producer_finished_at_epoch",
                    "producer_seconds", "queue_seconds", "verification_started_at_epoch", "verification_finished_at_epoch", "local_verification_seconds",
                    "publication_seconds", "published_at_epoch", "shared_wave_verification_seconds")}
                safe["depends_on"] = [token(value) for value in timeline.get("depends_on", ()) if token(value)]
                safe["shared_wave_id"] = token(timeline.get("shared_wave_id"))
                stage_timeline[token(item["stage_id"])] = safe
            reservation = reservations.get((item["request_id"], item["attempt_id"]), {})
            usage = {key: count(mapping(raw.get("usage")).get(key)) for key in fields}
            calls.append({"request_id": token(item["request_id"]), "attempt_id": token(item["attempt_id"]),
                "stage_id": token(item["stage_id"]), "purpose": token(item["purpose"]),
                "parent_request_id": token(reservation.get("parent_request_id")),
                "trigger_reason": token(reservation.get("trigger_code")),
                "created_at_epoch": number(item["created_at"]),
                "wire_state": token(raw.get("wire_state")), "http_status": number(raw.get("http_status")),
                "error_code": token(raw.get("error_code")), "provider_request_id": token(raw.get("provider_request_id")),
                "model": {key: token(raw.get(key)) for key in ("configured_model_id", "model_id", "provider_id", "endpoint_host")},
                "time": {key: number(raw.get(key)) for key in ("elapsed_seconds", "queue_seconds", "material_preparation_seconds",
                    "first_response_seconds", "first_content_seconds", "last_response_seconds", "verification_seconds", "publication_seconds")},
                "usage": usage, "usage_source": token(raw.get("usage_source")),
                # A request cap's semantics do not establish billing semantics.
                "reasoning_in_output": raw.get("reasoning_in_output") if type(raw.get("reasoning_in_output")) is bool else None,
                "amount": number(raw.get("billed_amount")), "currency": token(raw.get("billing_currency")),
                "price_source": token(raw.get("price_source")),
                "prefix": {key: token(raw.get(key)) for key in ("common_prefix_digest", "source_package_digest", "task_tail_digest", "message_boundary_version", "wire_prefix_digest")},
                "wire_prefix_bytes": count(raw.get("wire_prefix_bytes")),
                "component_bytes": {token(key): number(value) for key, value in mapping(raw.get("component_bytes")).items() if token(key)},
                "input_tokens_estimate": number(raw.get("input_tokens_estimate"))})
    physical = [call for call in calls if call["wire_state"] != "not_sent"]
    def summed(key):
        values = [call["usage"].get(key) for call in physical]
        known = [value for value in values if value is not None]
        return {"value": sum(known) if values and len(known) == len(values) else None,
            "known_sum": sum(known) if known else None, "unknown_attempts": len(values) - len(known)}
    totals = {key: summed(key) for key in fields}
    input_total, cached = totals["input_tokens"]["value"], totals["cache_read_input_tokens"]["value"]
    rate = cached / input_total if input_total and cached is not None and 0 <= cached <= input_total else None
    start, end = date(payload.get("started_at")), date(payload.get("completed_at"))
    begin = [call["created_at_epoch"] for call in physical if call["created_at_epoch"] is not None]
    finish = [call["created_at_epoch"] + call["time"]["elapsed_seconds"] for call in physical
              if call["created_at_epoch"] is not None and call["time"]["elapsed_seconds"] is not None]
    durations = {}
    for call in physical:
        elapsed = call["time"]["elapsed_seconds"]
        if elapsed is not None:
            durations[call["stage_id"]] = durations.get(call["stage_id"], 0) + elapsed
    plan = store.model_call_plan(run_id)
    if isinstance(plan, dict):
        # Never dump an open-ended saved plan into a public export. In
        # particular user-supplied quote prose/route strings are not evidence.
        raw_plan = plan
        plan = {key: count(raw_plan.get(key)) for key in ("generation_calls", "verification_batches",
            "repair_calls", "max_calls", "max_input_estimate", "max_output_tokens",
            "reserved_calls", "settled_calls", "unknown_calls", "remaining_calls",
            "remaining_input_tokens", "remaining_output_tokens") if key in raw_plan}
        plan.update({key: token(raw_plan.get(key)) for key in ("schema", "plan_digest", "policy", "limit_source")})
        plan["user_hard_limits"] = {key: number(value) for key, value in mapping(raw_plan.get("user_hard_limits")).items()
            if key in {"max_calls", "max_input_tokens", "max_output_tokens", "max_total_tokens", "max_automatic_seconds", "max_cost"}}
        for name in ("forecast", "initial_forecast"):
            plan[name] = {key: number(value) for key, value in mapping(raw_plan.get(name)).items()
                if key in {"max_calls", "max_input_estimate", "max_output_tokens", "expected_seconds", "estimated_cost"}}
        plan["forecast_exceeded"] = [key for key in raw_plan.get("forecast_exceeded", ())
            if key in {"max_calls", "max_input_estimate", "max_output_tokens", "expected_seconds"}]
        for name in ("adaptive_resume_available", "migration_choice_required"):
            plan[name] = raw_plan.get(name) is True
        plan["stage_ids"] = [token(key) for key in raw_plan.get("stage_ids", ()) if token(key)]
        plan["workload_bounds"] = {key: count(value) for key, value in mapping(raw_plan.get("workload_bounds")).items()
            if key in {"max_calls", "max_input_estimate", "max_output_tokens"}}
        plan["workload_fits"] = raw_plan.get("workload_fits") if type(raw_plan.get("workload_fits")) is bool else None
        plan["time_warning"] = raw_plan.get("time_warning") if type(raw_plan.get("time_warning")) is bool else None
        plan["elapsed_seconds"] = number(raw_plan.get("elapsed_seconds"))
        limits = mapping(raw_plan.get("user_envelope"))
        plan["user_envelope"] = {key: count(limits[key]) for key in ("max_input_tokens", "max_output_tokens", "max_total_tokens",
            "max_calls", "warning_seconds", "max_automatic_seconds") if key in limits}
        plan["user_envelope"].update({"mode": limits.get("mode") if limits.get("mode") in ("adaptive", "standard", "deep") else None,
            "max_cost": number(limits.get("max_cost"))})
        plan["pending_budget_gaps"] = [{key: token(gap.get(key)) if key in {"gap_id", "stage_id", "purpose", "diagnostic_code"}
            else count(gap.get(key)) for key in ("gap_id", "stage_id", "purpose", "diagnostic_code", "input_estimate", "output_limit")}
            for gap in raw_plan.get("pending_budget_gaps", ()) if isinstance(gap, dict)]
        plan["remaining"] = {key: count(value) for key, value in mapping(raw_plan.get("remaining")).items() if key in {"calls", "input_tokens", "output_tokens"}}
    currencies = {call["currency"] for call in physical}
    costs = [call["amount"] for call in physical]
    actual = sum(costs) if costs and all(value is not None for value in costs) and len(currencies) == 1 and None not in currencies else None
    def stage_end(key):
        item = stage_timeline[key]
        return item.get("published_at_epoch") or item.get("verification_finished_at_epoch") or item.get("producer_finished_at_epoch") or 0
    chain = []
    current = max(stage_timeline, key=stage_end) if stage_timeline else None
    while current and current not in chain and len(chain) < 64:
        chain.append(current)
        parents = [key for key in stage_timeline[current]["depends_on"] if key in stage_timeline]
        current = max(parents, key=stage_end) if parents else None
    return {"schema": "openthesis.research-diagnostics.v1", "snapshot_kind": "persisted_run_observations",
        "run": {"run_id": run_id, "status": token(payload.get("status") or row.get("status")),
            "started_at": payload.get("started_at"), "completed_at": payload.get("completed_at"),
            "wall_elapsed_seconds": max(0, end - start) if start is not None and end is not None else None},
        "attempt_count": len(calls), "physical_requests": len(physical),
        "physical_count_basis": "conservative_includes_unknown_submissions_not_UI_polls",
        "unknown_outcomes": sum(call["wire_state"] in {"remote_unknown", "send_intent_recorded"} for call in physical),
        "calls": calls, "totals": totals, "cache_hit_rate": rate,
        "cache_evidence": "endpoint_reported_upstream_unverified" if any(call["usage_source"] == "endpoint_reported_upstream_unverified" for call in physical) else "provider_receipts_or_unknown",
        "actual_cost": {"amount": actual, "currency": next(iter(currencies)) if actual is not None else None,
            "status": "endpoint_reported" if actual is not None else "unknown_without_complete_route_bound_tariffs_and_receipts"},
        "platform_bill_comparison": None,
        "critical_path": {"state": "observed_dependency_chain" if chain else "partial_model_timeline",
            "model_wall_span_seconds": max(finish) - min(begin) if begin and finish else None,
            "observed_stage_chain": list(reversed(chain)), "stage_timeline": stage_timeline, "job_phases": job_phases,
            "stage_call_seconds": durations, "note": "observed predecessors, not a P95 claim; shared wave durations and parallel work must not be summed into wall time"},
        "financial_generations": {token(key): token(value) for key, value in payload.get("data_snapshot", {}).get("financial_generations", {}).items()
            if token(key) and token(value)},
        "budget": plan, "quality": quality}


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
