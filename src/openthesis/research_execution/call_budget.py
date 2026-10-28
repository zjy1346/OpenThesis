"""Atomic, run-scoped physical-call reservations shared by every purpose.

Unknown submissions and unknown usage keep their reservations. Counters are
never process-local and changing purpose cannot obtain another repair slot.
No tariff or supplier token count is inferred from a local estimate.
"""
from __future__ import annotations

import json
import time
import hashlib
import math

from ..providers import ProviderError
from ..resilience.contracts import canonical_json


REPAIR_PURPOSES = frozenset({"claim_repair", "field_repair", "output_repair", "format_repair", "section_delivery"})
BUDGET_GAP_CODES = frozenset({"MODEL_RUN_CALL_BUDGET_EXHAUSTED", "MODEL_RUN_VERIFICATION_BUDGET_EXHAUSTED", "MODEL_RUN_TOKEN_BUDGET_EXHAUSTED",
    "MODEL_RUN_TOTAL_BUDGET_EXHAUSTED", "MODEL_RUN_TIME_BUDGET_EXHAUSTED", "MODEL_RUN_PLAN_REQUIRES_APPROVAL", "MODEL_RUN_COST_BUDGET_EXHAUSTED",
    "MODEL_RUN_REPAIR_BUDGET_EXHAUSTED", "MODEL_STAGE_GENERATION_BUDGET_EXHAUSTED"})
LEGACY_STANDARD_ENVELOPE = {"mode": "standard", "max_input_tokens": 1_500_000, "max_output_tokens": 100_000,
    "max_total_tokens": 1_600_000, "max_calls": 16, "warning_seconds": 900,
    "max_automatic_seconds": 1200, "max_cost": None, "price_quote": None}
LIMIT_KEYS = ("max_input_tokens", "max_output_tokens", "max_total_tokens", "max_calls", "max_automatic_seconds", "max_cost")
STANDARD_ENVELOPE = {"policy": "adaptive", "mode": "adaptive", **{key: None for key in LIMIT_KEYS},
    "warning_seconds": 900, "price_quote": None, "limit_source": "unset",
    "user_hard_limits": {key: None for key in LIMIT_KEYS}}


def user_envelope(value=None):
    """A user limit, not a vendor capability, tariff or quality waiver."""
    if value is not None and (not isinstance(value, dict) or set(value) - set(STANDARD_ENVELOPE)):
        raise ValueError("MODEL_RUN_ENVELOPE_INVALID")
    value = value or {}
    nested = value.get("user_hard_limits", {})
    if not isinstance(nested, dict) or set(nested) - set(LIMIT_KEYS):
        raise ValueError("MODEL_RUN_ENVELOPE_INVALID")
    if any(key in value and key in nested and value[key] != nested[key] for key in LIMIT_KEYS):
        raise ValueError("MODEL_RUN_ENVELOPE_CONFLICT")
    limits = {key: nested.get(key, value.get(key)) for key in LIMIT_KEYS}
    result = {**STANDARD_ENVELOPE, **value, **limits, "policy": "adaptive", "mode": "adaptive",
        "user_hard_limits": limits,
        "limit_source": "user_explicit" if any(v is not None for v in limits.values()) else "unset"}
    if value.get("policy", "adaptive") != "adaptive" or value.get("mode", "adaptive") not in ("adaptive", "standard", "deep"):
        raise ValueError("MODEL_RUN_ENVELOPE_INVALID")
    for key in (*LIMIT_KEYS[:-1], "warning_seconds"):
        if result[key] is not None and (type(result[key]) is not int or not 0 < result[key] <= 9_007_199_254_740_991):
            raise ValueError("MODEL_RUN_ENVELOPE_INVALID")
    if type(result["warning_seconds"]) is not int:
        raise ValueError("MODEL_RUN_ENVELOPE_INVALID")
    amount = result["max_cost"]
    if amount is not None and (type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0):
        raise ValueError("MODEL_RUN_ENVELOPE_INVALID")
    quote = result["price_quote"]
    if quote is not None:
        required = {"input_per_million", "output_per_million", "currency", "source", "route_identity"}
        if (not isinstance(quote, dict) or not required <= set(quote) or set(quote) - required - {"cached_input_per_million"}
                or any(type(quote.get(key)) not in (int, float) or not math.isfinite(quote[key]) or quote[key] < 0
                       for key in ("input_per_million", "output_per_million", *(["cached_input_per_million"] if "cached_input_per_million" in quote else [])))
                or any(not isinstance(quote.get(key), str) or not quote[key].strip() or len(quote[key]) > 256
                       for key in ("currency", "source", "route_identity"))):
            raise ValueError("MODEL_PRICE_QUOTE_INVALID")
    if amount is not None and quote is None:
        raise ValueError("MODEL_PRICE_QUOTE_REQUIRED")
    return result


def plan_workload(rows):
    repairs = sorted(rows, key=lambda row: row["input_estimate"] + row["output_limit"], reverse=True)[:2]
    return {"max_calls": len(rows) + 2,
        "max_input_estimate": sum(row["input_estimate"] for row in rows) + sum(row.get("repair_input_estimate", row["input_estimate"]) for row in repairs),
        "max_output_tokens": sum(row["output_limit"] for row in rows) + sum(min(4096, row["output_limit"]) for row in repairs)}
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS model_work_items (
        run_id TEXT NOT NULL, work_digest TEXT NOT NULL, request_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
        work_json TEXT NOT NULL, PRIMARY KEY(run_id,work_digest),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
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
    prior = db.execute("SELECT * FROM model_call_reservations WHERE run_id=? AND state!='not_sent'", (run_id,)).fetchall()
    used_input = sum(row["input_actual"] if row["input_actual"] is not None else row["input_estimate"] for row in prior)
    used_output = sum(row["output_actual"] if row["output_actual"] is not None else row["output_limit"] for row in prior)
    limits = plan.get("user_envelope", {})
    elapsed = max(0, time.time() - plan["started_at"]) if "started_at" in plan else None
    remaining = lambda key, used: max(0, plan[key] - used) if plan.get(key) is not None else None
    forecast = plan.get("initial_forecast", plan.get("forecast", plan.get("workload_bounds", {})))
    exceeded = [key for key, used in (("max_calls", len(prior)), ("max_input_estimate", used_input),
        ("max_output_tokens", used_output), ("expected_seconds", elapsed))
        if used is not None and forecast.get(key) is not None and used > forecast[key]]
    adaptive = plan.get("policy") == "adaptive"
    provenance = plan.get("limit_source", "legacy_default" if limits == LEGACY_STANDARD_ENVELOPE else "legacy_unknown")
    gaps = [dict(row) for row in db.execute("""SELECT gap_id,stage_id,purpose,input_estimate,
        output_limit,diagnostic_code FROM model_budget_gaps WHERE run_id=? AND state='pending' ORDER BY gap_id""", (run_id,))]
    blocked = bool(gaps) or plan.get("workload_fits") is False
    return {**plan, "plan_digest": hashlib.sha256(canonical_json(plan).encode()).hexdigest(),
        "limit_source": provenance, "forecast_exceeded": exceeded,
        "adaptive_resume_available": not adaptive and blocked,
        "migration_choice_required": not adaptive and blocked and provenance == "legacy_unknown",
        "consumption": {"calls": len(prior), "input_reserved_or_actual": used_input,
            "output_reserved_or_actual": used_output, "unknown_usage_calls": sum(row["state"] != "settled" for row in prior)},
        "remaining": {"calls": remaining("max_calls", len(prior)),
            "input_tokens": remaining("max_input_estimate", used_input),
            "output_tokens": remaining("max_output_tokens", used_output)},
        "remaining_calls": remaining("max_calls", len(prior)),
        "remaining_input_tokens": remaining("max_input_estimate", used_input),
        "remaining_output_tokens": remaining("max_output_tokens", used_output),
        "elapsed_seconds": elapsed,
        "time_warning": bool(elapsed is not None and elapsed >= limits.get("warning_seconds", 900)),
        "pending_budget_gaps": gaps}


def extend_plan(db, run_id, expected_digest, gap_ids, adaptive_choice=None):
    """User-initiated addition, not an automatic plan version upgrade."""
    db.execute("BEGIN IMMEDIATE")
    current = plan_status(db, run_id)
    if current is None or current["plan_digest"] != expected_digest:
        raise ValueError("MODEL_BUDGET_PLAN_CHANGED")
    if adaptive_choice is not None:
        if adaptive_choice not in {"keep", "remove"}:
            raise ValueError("MODEL_BUDGET_MIGRATION_CHOICE_INVALID")
        return _migrate_adaptive(db, run_id, current, adaptive_choice)
    gaps = [gap for gap in current["pending_budget_gaps"] if gap["gap_id"] in gap_ids]
    if not gaps or {gap["gap_id"] for gap in gaps} != set(gap_ids):
        raise ValueError("MODEL_BUDGET_GAP_CHANGED")
    prior = db.execute("SELECT * FROM model_call_reservations WHERE run_id=? AND state!='not_sent'", (run_id,)).fetchall()
    plan = {key: value for key, value in current.items() if key not in _STATUS_FIELDS}
    if plan["max_calls"] is not None:
        plan["max_calls"] = max(plan["max_calls"], len(prior) + len(gaps))
    plan["verification_batches"] = max(plan["verification_batches"],
        sum(row["purpose"] == "semantic_verification" for row in prior) + sum(gap["purpose"] == "semantic_verification" for gap in gaps))
    for name, actual, reserved, addition in (("max_input_estimate", "input_actual", "input_estimate", "input_estimate"),
                                            ("max_output_tokens", "output_actual", "output_limit", "output_limit")):
        if plan[name] is not None:
            required = sum(row[actual] if row[actual] is not None else row[reserved] for row in prior) + sum(gap[addition] for gap in gaps)
            plan[name] = max(plan[name], required)
    if plan.get("user_envelope"):
        limits = dict(plan["user_envelope"])
        limits.update(max_calls=plan["max_calls"], max_input_tokens=plan["max_input_estimate"],
            max_output_tokens=plan["max_output_tokens"])
        if limits.get("max_total_tokens") is not None:
            limits["max_total_tokens"] = max(limits["max_total_tokens"],
                sum((i["input_actual"] if i["input_actual"] is not None else i["input_estimate"]) +
                    (i["output_actual"] if i["output_actual"] is not None else i["output_limit"]) for i in prior)
                    + sum(gap["input_estimate"] + gap["output_limit"] for gap in gaps))
        if any(gap["diagnostic_code"] == "MODEL_RUN_TIME_BUDGET_EXHAUSTED" for gap in gaps):
            limits["max_automatic_seconds"] = max(limits["max_automatic_seconds"] or 0, int(time.time() - plan["started_at"]) + 1200)
        # Approval is for these gap identities, not a new automatic full run;
        # budget_only dispatch independently rejects all unapproved work.
        limits.pop("user_hard_limits", None)
        plan["user_envelope"] = user_envelope(limits)
        plan["user_hard_limits"] = plan["user_envelope"]["user_hard_limits"]
        plan["limit_source"] = plan["user_envelope"]["limit_source"]
        plan["workload_fits"] = True
    plan["user_extensions"] = [*plan.get("user_extensions", ()), {"gap_ids": sorted(gap_ids), "approved_at": time.time()}]
    db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (canonical_json(plan), run_id))
    db.executemany("UPDATE model_budget_gaps SET state='approved' WHERE run_id=? AND gap_id=?", [(run_id, gap["gap_id"]) for gap in gaps])
    return plan_status(db, run_id)


_STATUS_FIELDS = {"plan_digest", "pending_budget_gaps", "consumption", "remaining", "elapsed_seconds", "time_warning",
    "remaining_calls", "remaining_input_tokens", "remaining_output_tokens", "forecast_exceeded",
    "adaptive_resume_available", "migration_choice_required"}


def _migrate_adaptive(db, run_id, current, choice):
    """Explicit continuation migrates policy only, never source/head/history."""
    if db.execute("SELECT 1 FROM model_request_journal WHERE run_id=? AND state IN ('running','received') AND lease_until>? LIMIT 1",
            (run_id, time.time())).fetchone():
        raise ValueError("MODEL_REQUEST_IN_PROGRESS")
    if current.get("policy") == "adaptive":
        raise ValueError("MODEL_BUDGET_PLAN_CHANGED")
    old = current.get("user_envelope") or {}
    kept = {key: old.get(key) for key in LIMIT_KEYS} if choice == "keep" else {}
    # Older v1 envelopes stored limits only on the plan itself.
    if choice == "keep" and not old:
        kept.update(max_calls=current.get("max_calls"), max_input_tokens=current.get("max_input_estimate"),
            max_output_tokens=current.get("max_output_tokens"))
    limits = user_envelope({"user_hard_limits": kept, "price_quote": old.get("price_quote")})
    plan = {key: value for key, value in current.items() if key not in _STATUS_FIELDS}
    plan.update(schema="research-call-plan-v4-adaptive", policy="adaptive", user_envelope=limits,
        user_hard_limits=limits["user_hard_limits"], limit_source=limits["limit_source"],
        max_calls=limits["max_calls"], max_input_estimate=limits["max_input_tokens"],
        max_output_tokens=limits["max_output_tokens"], repair_calls=None, repair_calls_per_stage=None,
        workload_fits=True, forecast=plan.get("workload_bounds", {}))
    plan["policy_migrations"] = [*plan.get("policy_migrations", ()), {"from": current.get("schema"),
        "previous_limit_source": current["limit_source"], "choice": choice, "approved_at": time.time()}]
    db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (canonical_json(plan), run_id))
    db.execute("UPDATE model_budget_gaps SET state='approved' WHERE run_id=? AND state='pending'", (run_id,))
    return plan_status(db, run_id)


def prepare_plan(db, run_id, stage_ids, input_estimate=None, *, request_plan=None, envelope=None):
    """Freeze source/task identities, not a quota inferred from a forecast."""
    stages = sorted(set(stage_ids))
    calls = len(stages) * 3 + 2
    estimate = input_estimate if type(input_estimate) is int and input_estimate > 0 else None
    limits = user_envelope(envelope)
    plan = {"schema": "research-call-plan-v4-adaptive", "stage_ids": stages,
            "generation_calls": len(stages), "verification_batches": len(stages) * 2,
            "repair_calls": None, "repair_calls_per_stage": None, "max_calls": limits["max_calls"],
            "max_input_estimate": limits["max_input_tokens"], "max_output_tokens": limits["max_output_tokens"],
            "policy": "adaptive", "user_envelope": limits, "user_hard_limits": limits["user_hard_limits"],
            "limit_source": limits["limit_source"], "started_at": time.time(), "workload_fits": True,
            "forecast": {"max_calls": calls, "max_input_estimate": calls * estimate if estimate else None,
                "max_output_tokens": calls * 65_536, "expected_seconds": 1200},
            "input_counting": "planning_estimate_not_supplier_usage", "usage_unknown_keeps_reservation": True}
    if request_plan is not None:
        rows = list(request_plan)
        if (any(not isinstance(row, dict) or row.get("stage_id") not in stages
                or type(row.get("input_estimate")) is not int or row["input_estimate"] < 0
                or type(row.get("output_limit")) is not int or row["output_limit"] <= 0
                or row.get("purpose") not in {"generation", "semantic_verification"} for row in rows)):
            raise ValueError("MODEL_REQUEST_PLAN_INVALID")
        workload = plan_workload(rows)
        previous = plan_status(db, run_id)
        if previous and previous.get("compilation_pending"):
            envelope = previous["user_envelope"]
            for field, used in (("max_calls", previous["consumption"]["calls"]),
                    ("max_input_estimate", previous["consumption"]["input_reserved_or_actual"]),
                    ("max_output_tokens", previous["consumption"]["output_reserved_or_actual"])):
                workload[field] += used
        limits = user_envelope(envelope)
        plan = {**plan, "request_plan": rows,
            "generation_calls": sum(row["purpose"] == "generation" for row in rows),
            "verification_batches": sum(row["purpose"] == "semantic_verification" for row in rows),
            "user_envelope": limits, "user_hard_limits": limits["user_hard_limits"], "limit_source": limits["limit_source"],
            "workload_bounds": workload, "forecast": {**workload, "expected_seconds": 1200},
            "max_calls": limits["max_calls"], "max_input_estimate": limits["max_input_tokens"],
            "max_output_tokens": limits["max_output_tokens"],
            "basis": "compiled_role_packet_plus_explicit_dependency_output_estimate; verification_source_workload; not_supplier_usage"}
        if previous and previous.get("compilation_pending"):
            plan["started_at"] = previous["started_at"]
            plan["initial_forecast"] = dict(plan["forecast"])
            db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (canonical_json(plan), run_id))
    plan["initial_forecast"] = dict(plan["forecast"])
    db.execute("INSERT INTO model_run_call_plans VALUES(?,?) ON CONFLICT(run_id) DO NOTHING",
               (run_id, canonical_json(plan)))


def prepare_external_plan(db, run_id, envelope=None):
    """Early vision uses the same ledger before text materials are compiled."""
    limits = user_envelope(envelope)
    plan = {"schema": "research-call-plan-v4-adaptive", "policy": "adaptive", "compilation_pending": True,
        "stage_ids": [], "generation_calls": 0, "verification_batches": 0,
        "repair_calls": None, "repair_calls_per_stage": None, "max_calls": limits["max_calls"],
        "max_input_estimate": limits["max_input_tokens"], "max_output_tokens": limits["max_output_tokens"],
        "user_envelope": limits, "user_hard_limits": limits["user_hard_limits"], "limit_source": limits["limit_source"],
        "forecast": {"expected_seconds": 1200}, "workload_fits": True, "started_at": time.time(),
        "usage_unknown_keeps_reservation": True, "input_counting": "planning_estimate_not_supplier_usage"}
    db.execute("INSERT INTO model_run_call_plans VALUES(?,?) ON CONFLICT(run_id) DO NOTHING", (run_id, canonical_json(plan)))


def reserve(db, run_id, context, *, purpose, input_estimate, output_limit, trigger="", elapsed_seconds=None, route_identity=None):
    existing = db.execute("SELECT 1 FROM model_call_reservations WHERE request_id=? AND attempt_id=?",
                          (context["request_id"], context["attempt_id"])).fetchone()
    if existing:
        # Same physical identity cannot be dispatched a second time.
        raise ProviderError("Physical request already reserved", code="MODEL_BUDGET_ATTEMPT_ALREADY_RESERVED")
    row = db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ProviderError("Model call plan is not frozen", code="MODEL_RUN_BUDGET_PLAN_REQUIRED")
    plan = json.loads(row[0])
    limits = plan.get("user_envelope")
    adaptive = plan.get("policy") == "adaptive"
    if limits:
        if not adaptive and plan.get("workload_fits") is False:
            raise ProviderError("Compiled work requires explicit user budget approval", code="MODEL_RUN_PLAN_REQUIRES_APPROVAL", not_sent=True)
        elapsed = max(0, time.time() - plan["started_at"]) if elapsed_seconds is None else elapsed_seconds
        if limits.get("max_automatic_seconds") is not None and elapsed >= limits["max_automatic_seconds"]:
            raise ProviderError("Automatic research time budget exhausted", code="MODEL_RUN_TIME_BUDGET_EXHAUSTED", not_sent=True)
    prior = db.execute("SELECT * FROM model_call_reservations WHERE run_id=? AND state!='not_sent'", (run_id,)).fetchall()
    repairs = [item for item in prior if item["purpose"] in REPAIR_PURPOSES]
    is_repair = purpose in REPAIR_PURPOSES
    stage = context["stage_id"]
    if not adaptive and is_repair and (len(repairs) >= plan["repair_calls"] or
            sum(item["stage_id"] == stage for item in repairs) >= plan["repair_calls_per_stage"]):
        raise ProviderError("Automatic repair budget exhausted", code="MODEL_RUN_REPAIR_BUDGET_EXHAUSTED")
    if not adaptive and purpose == "generation" and any(item["purpose"] == "generation" and
            item["stage_id"] == stage and item["generation"] == context["generation"] for item in prior):
        raise ProviderError("Stage generation already attempted", code="MODEL_STAGE_GENERATION_BUDGET_EXHAUSTED")
    verifications = sum(item["purpose"] == "semantic_verification" for item in prior)
    if not adaptive and purpose == "semantic_verification" and verifications >= plan["verification_batches"]:
        raise ProviderError("Verification plan exhausted", code="MODEL_RUN_VERIFICATION_BUDGET_EXHAUSTED")
    if plan["max_calls"] is not None and len(prior) >= plan["max_calls"]:
        raise ProviderError("Run call budget exhausted", code="MODEL_RUN_CALL_BUDGET_EXHAUSTED")
    for limit_key, estimate_value, actual_key, reserved_key in (
            ("max_input_estimate", input_estimate, "input_actual", "input_estimate"),
            ("max_output_tokens", output_limit, "output_actual", "output_limit")):
        total = sum(item[actual_key] if item[actual_key] is not None else item[reserved_key] for item in prior)
        if plan[limit_key] is not None and total + estimate_value > plan[limit_key]:
            raise ProviderError("Run token budget exhausted", code="MODEL_RUN_TOKEN_BUDGET_EXHAUSTED")
    if limits:
        reserved_total = sum((item["input_actual"] if item["input_actual"] is not None else item["input_estimate"])
            + (item["output_actual"] if item["output_actual"] is not None else item["output_limit"]) for item in prior)
        if limits.get("max_total_tokens") is not None and reserved_total + input_estimate + output_limit > limits["max_total_tokens"]:
            raise ProviderError("Combined run token budget exhausted", code="MODEL_RUN_TOTAL_BUDGET_EXHAUSTED", not_sent=True)
        quote = limits.get("price_quote")
        if quote and limits.get("max_cost") is not None:
            if quote["route_identity"] != route_identity:
                raise ProviderError("Tariff is not bound to this exact model route", code="MODEL_RUN_PRICE_QUOTE_ROUTE_MISMATCH", not_sent=True)
            # Reserve every input token at the uncached price. Cache savings
            # cannot finance another call before actual usage has arrived.
            cost = sum(_reserved_cost(db, item, quote) for item in prior)
            next_cost = (input_estimate * max(quote["input_per_million"], quote.get("cached_input_per_million", 0))
                + output_limit * quote["output_per_million"]) / 1_000_000
            if cost + next_cost > limits["max_cost"]:
                raise ProviderError("Run cost budget exhausted", code="MODEL_RUN_COST_BUDGET_EXHAUSTED", not_sent=True)
    semantic_work = None
    if adaptive and isinstance(context.get("work_item"), dict):
        # Semantic identity intentionally excludes generation, attempt, clock,
        # prompt revision and trigger wording. Only changed work can proceed.
        work = {key: context["work_item"].get(key) for key in (
            "obligation", "targets", "source_version", "completed_digest", "strategy")}
        work["targets"] = sorted(set(work.get("targets") or [stage]))
        identity = hashlib.sha256(canonical_json([stage, purpose, work]).encode()).hexdigest()
        previous = db.execute("""SELECT r.state FROM model_work_items w JOIN model_call_reservations r
            ON r.request_id=w.request_id AND r.attempt_id=w.attempt_id
            WHERE w.run_id=? AND w.work_digest=?""", (run_id, identity)).fetchone()
        if previous and previous["state"] != "not_sent":
            raise ProviderError("The same semantic work has no new progress", code="MODEL_WORK_ITEM_NO_PROGRESS", not_sent=True)
        semantic_work = (identity, work)
    db.execute("INSERT INTO model_call_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, context["request_id"], context["attempt_id"], stage, context["generation"], purpose,
         input_estimate, output_limit, None, None, "reserved", context["request_id"].partition(":repair:")[0], trigger))
    if semantic_work is not None:
        identity, work = semantic_work
        db.execute("""INSERT INTO model_work_items VALUES(?,?,?,?,?) ON CONFLICT(run_id,work_digest)
            DO UPDATE SET request_id=excluded.request_id,attempt_id=excluded.attempt_id,work_json=excluded.work_json""",
            (run_id, identity, context["request_id"], context["attempt_id"], canonical_json(work)))
        forecast = dict(plan.get("forecast", {}))
        for key, used in (("max_calls", len(prior) + 1),
                ("max_input_estimate", sum(i["input_actual"] if i["input_actual"] is not None else i["input_estimate"] for i in prior) + input_estimate),
                ("max_output_tokens", sum(i["output_actual"] if i["output_actual"] is not None else i["output_limit"] for i in prior) + output_limit)):
            forecast[key] = max(forecast.get(key) or 0, used)
        plan["forecast"] = forecast
        plan["initial_forecast"] = plan.get("initial_forecast", {**plan.get("workload_bounds", {}), "expected_seconds": 1200})
        db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (canonical_json(plan), run_id))


def _reserved_cost(db, item, quote):
    inputs = item["input_actual"] if item["input_actual"] is not None else item["input_estimate"]
    outputs = item["output_actual"] if item["output_actual"] is not None else item["output_limit"]
    cached = 0
    cache_known = False
    if item["state"] == "settled" and quote.get("cached_input_per_million") is not None:
        observation = db.execute("SELECT diagnostics_json FROM model_wire_diagnostics WHERE request_id=? AND attempt_id=?",
            (item["request_id"], item["attempt_id"])).fetchone()
        if observation:
            usage = json.loads(observation[0]).get("usage") or {}
            value = usage.get("cache_read_input_tokens")
            if type(value) is int and 0 <= value <= inputs:
                cached = value
                cache_known = True
    input_price = quote["input_per_million"] if cache_known else max(quote["input_per_million"], quote.get("cached_input_per_million", 0))
    return ((inputs - cached) * input_price + cached * quote.get("cached_input_per_million", quote["input_per_million"])
        + outputs * quote["output_per_million"]) / 1_000_000


def settle(db, context, usage, state, *, reasoning_in_output=None):
    def known(name):
        value = (usage or {}).get(name)
        return value if type(value) is int and value >= 0 else None
    output = known("output_tokens")
    reasoning = known("reasoning_tokens")
    # Unknown inclusion is conservatively charged to the local budget, never
    # represented as a supplier bill. Separately billed reasoning cannot vanish.
    if output is not None and reasoning is not None and reasoning_in_output is not True:
        output += reasoning
    incomplete_reasoning = output is not None and reasoning is None and reasoning_in_output is not True
    if incomplete_reasoning:
        reservation = db.execute("SELECT output_limit FROM model_call_reservations WHERE request_id=? AND attempt_id=?",
            (context["request_id"], context["attempt_id"])).fetchone()
        if reservation:
            output = max(output, reservation[0])
    db.execute("""UPDATE model_call_reservations SET input_actual=?,output_actual=?,state=?
        WHERE request_id=? AND attempt_id=?""",
        (known("input_tokens"), output,
         "not_sent" if state == "not_sent" else "settled" if known("input_tokens") is not None
         and known("output_tokens") is not None and not incomplete_reasoning else "usage_unknown",
         context["request_id"], context["attempt_id"]))
