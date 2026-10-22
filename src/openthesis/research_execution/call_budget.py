"""Atomic, run-scoped physical-call reservations shared by every purpose.

Unknown submissions and unknown usage keep their reservations. Counters are
never process-local and changing purpose cannot obtain another repair slot.
No tariff or supplier token count is inferred from a local estimate.
"""
from __future__ import annotations

import json
import time
import hashlib

from ..providers import ProviderError
from ..resilience.contracts import canonical_json


REPAIR_PURPOSES = frozenset({"claim_repair", "field_repair", "output_repair", "format_repair", "section_delivery"})
BUDGET_GAP_CODES = frozenset({"MODEL_RUN_CALL_BUDGET_EXHAUSTED", "MODEL_RUN_VERIFICATION_BUDGET_EXHAUSTED", "MODEL_RUN_TOKEN_BUDGET_EXHAUSTED"})
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS model_budget_gaps (
        run_id TEXT NOT NULL, gap_id TEXT NOT NULL, request_id TEXT NOT NULL, stage_id TEXT NOT NULL,
        purpose TEXT NOT NULL, input_estimate INTEGER NOT NULL, output_limit INTEGER NOT NULL,
        diagnostic_code TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(run_id,gap_id),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_run_call_plans (
        run_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_call_reservations (
        run_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
        stage_id TEXT NOT NULL, generation INTEGER NOT NULL, purpose TEXT NOT NULL,
        input_estimate INTEGER NOT NULL, output_limit INTEGER NOT NULL,
        input_actual INTEGER, output_actual INTEGER, state TEXT NOT NULL,
        parent_request_id TEXT NOT NULL, trigger_code TEXT NOT NULL,
        PRIMARY KEY(request_id,attempt_id),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
)


def record_gap(db, run_id, context, purpose, input_estimate, output_limit, code):
    if code not in BUDGET_GAP_CODES:
        return
    gap_id = hashlib.sha256(canonical_json([context["request_id"], purpose, input_estimate, output_limit]).encode()).hexdigest()
    db.execute("""INSERT INTO model_budget_gaps VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id,gap_id) DO UPDATE SET state='pending'
        WHERE model_budget_gaps.state='consumed'""", (run_id, gap_id, context["request_id"], context["stage_id"],
        purpose, input_estimate, output_limit, code, "pending"))


def plan_status(db, run_id):
    row = db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    plan = json.loads(row[0])
    return {**plan, "plan_digest": hashlib.sha256(canonical_json(plan).encode()).hexdigest(),
        "pending_budget_gaps": [dict(row) for row in db.execute("""SELECT gap_id,stage_id,purpose,input_estimate,
            output_limit,diagnostic_code FROM model_budget_gaps WHERE run_id=? AND state='pending' ORDER BY gap_id""", (run_id,))]}


def extend_plan(db, run_id, expected_digest, gap_ids):
    """User-initiated addition, not an automatic plan version upgrade."""
    db.execute("BEGIN IMMEDIATE")
    current = plan_status(db, run_id)
    if current is None or current["plan_digest"] != expected_digest:
        raise ValueError("MODEL_BUDGET_PLAN_CHANGED")
    gaps = [gap for gap in current["pending_budget_gaps"] if gap["gap_id"] in gap_ids]
    if not gaps or {gap["gap_id"] for gap in gaps} != set(gap_ids):
        raise ValueError("MODEL_BUDGET_GAP_CHANGED")
    prior = db.execute("SELECT * FROM model_call_reservations WHERE run_id=? AND state!='not_sent'", (run_id,)).fetchall()
    plan = {key: value for key, value in current.items() if key not in {"plan_digest", "pending_budget_gaps"}}
    plan["max_calls"] = max(plan["max_calls"], len(prior) + len(gaps))
    plan["verification_batches"] = max(plan["verification_batches"],
        sum(row["purpose"] == "semantic_verification" for row in prior) + sum(gap["purpose"] == "semantic_verification" for gap in gaps))
    for name, actual, reserved, addition in (("max_input_estimate", "input_actual", "input_estimate", "input_estimate"),
                                            ("max_output_tokens", "output_actual", "output_limit", "output_limit")):
        if plan[name] is not None:
            required = sum(row[actual] if row[actual] is not None else row[reserved] for row in prior) + sum(gap[addition] for gap in gaps)
            plan[name] = max(plan[name], required)
    plan["user_extensions"] = [*plan.get("user_extensions", ()), {"gap_ids": sorted(gap_ids), "approved_at": time.time()}]
    db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (canonical_json(plan), run_id))
    db.executemany("UPDATE model_budget_gaps SET state='approved' WHERE run_id=? AND gap_id=?", [(run_id, gap["gap_id"]) for gap in gaps])
    return plan_status(db, run_id)


def prepare_plan(db, run_id, stage_ids, input_estimate=None, *, request_plan=None):
    """Freeze the plan once; restart/configuration changes do not reset it.

    Two semantic batches per planned stage plus the two shared repairs form
    a finite automatic envelope. A depleted envelope retains content and
    exposes the reason, rather than silently granting more paid requests.
    """
    stages = sorted(set(stage_ids))
    calls = len(stages) * 3 + 2
    estimate = input_estimate if type(input_estimate) is int and input_estimate > 0 else None
    plan = {"schema": "research-call-plan-v1", "stage_ids": stages,
            "generation_calls": len(stages), "verification_batches": len(stages) * 2,
            "repair_calls": 2, "repair_calls_per_stage": 1, "max_calls": calls,
            "max_input_estimate": calls * estimate if estimate else None,
            "max_output_tokens": calls * 65_536,
            "input_counting": "planning_estimate_not_supplier_usage", "usage_unknown_keeps_reservation": True}
    if request_plan is not None:
        rows = list(request_plan)
        if (any(not isinstance(row, dict) or row.get("stage_id") not in stages
                or type(row.get("input_estimate")) is not int or row["input_estimate"] < 0
                or type(row.get("output_limit")) is not int or row["output_limit"] <= 0
                or row.get("purpose") not in {"generation", "semantic_verification"} for row in rows)):
            raise ValueError("MODEL_REQUEST_PLAN_INVALID")
        repairs = sorted(rows, key=lambda row: row["input_estimate"] + row["output_limit"], reverse=True)[:2]
        plan = {**plan, "schema": "research-call-plan-v2-role-packets", "request_plan": rows,
            "generation_calls": sum(row["purpose"] == "generation" for row in rows),
            "verification_batches": sum(row["purpose"] == "semantic_verification" for row in rows),
            "max_calls": len(rows) + 2,
            "max_input_estimate": sum(row["input_estimate"] for row in rows) + sum(row["input_estimate"] for row in repairs),
            "max_output_tokens": sum(row["output_limit"] for row in rows) + sum(row["output_limit"] for row in repairs),
            "basis": "compiled_role_packet_plus_explicit_dependency_output_estimate; verification_source_workload; not_supplier_usage"}
    db.execute("INSERT INTO model_run_call_plans VALUES(?,?) ON CONFLICT(run_id) DO NOTHING",
               (run_id, canonical_json(plan)))


def reserve(db, run_id, context, *, purpose, input_estimate, output_limit, trigger=""):
    existing = db.execute("SELECT 1 FROM model_call_reservations WHERE request_id=? AND attempt_id=?",
                          (context["request_id"], context["attempt_id"])).fetchone()
    if existing:
        # Same physical identity cannot be dispatched a second time.
        raise ProviderError("Physical request already reserved", code="MODEL_BUDGET_ATTEMPT_ALREADY_RESERVED")
    row = db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ProviderError("Model call plan is not frozen", code="MODEL_RUN_BUDGET_PLAN_REQUIRED")
    plan = json.loads(row[0])
    prior = db.execute("SELECT * FROM model_call_reservations WHERE run_id=? AND state!='not_sent'", (run_id,)).fetchall()
    repairs = [item for item in prior if item["purpose"] in REPAIR_PURPOSES]
    is_repair = purpose in REPAIR_PURPOSES
    stage = context["stage_id"]
    if is_repair and (len(repairs) >= plan["repair_calls"] or
            sum(item["stage_id"] == stage for item in repairs) >= plan["repair_calls_per_stage"]):
        raise ProviderError("Automatic repair budget exhausted", code="MODEL_RUN_REPAIR_BUDGET_EXHAUSTED")
    if purpose == "generation" and any(item["purpose"] == "generation" and
            item["stage_id"] == stage and item["generation"] == context["generation"] for item in prior):
        raise ProviderError("Stage generation already attempted", code="MODEL_STAGE_GENERATION_BUDGET_EXHAUSTED")
    verifications = sum(item["purpose"] == "semantic_verification" for item in prior)
    if purpose == "semantic_verification" and verifications >= plan["verification_batches"]:
        raise ProviderError("Verification plan exhausted", code="MODEL_RUN_VERIFICATION_BUDGET_EXHAUSTED")
    if len(prior) >= plan["max_calls"]:
        raise ProviderError("Run call budget exhausted", code="MODEL_RUN_CALL_BUDGET_EXHAUSTED")
    for limit_key, estimate_value, actual_key, reserved_key in (
            ("max_input_estimate", input_estimate, "input_actual", "input_estimate"),
            ("max_output_tokens", output_limit, "output_actual", "output_limit")):
        total = sum(item[actual_key] if item[actual_key] is not None else item[reserved_key] for item in prior)
        if plan[limit_key] is not None and total + estimate_value > plan[limit_key]:
            raise ProviderError("Run token budget exhausted", code="MODEL_RUN_TOKEN_BUDGET_EXHAUSTED")
    db.execute("INSERT INTO model_call_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, context["request_id"], context["attempt_id"], stage, context["generation"], purpose,
         input_estimate, output_limit, None, None, "reserved", context["request_id"].partition(":repair:")[0], trigger))


def settle(db, context, usage, state):
    def known(name):
        value = (usage or {}).get(name)
        return value if type(value) is int and value >= 0 else None
    db.execute("""UPDATE model_call_reservations SET input_actual=?,output_actual=?,state=?
        WHERE request_id=? AND attempt_id=?""",
        (known("input_tokens"), known("output_tokens"),
         "not_sent" if state == "not_sent" else "settled" if known("input_tokens") is not None
         and known("output_tokens") is not None else "usage_unknown",
         context["request_id"], context["attempt_id"]))
