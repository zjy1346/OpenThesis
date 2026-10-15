"""One-time, server-owned permission for an unresolved physical model wire.

This does not mark an old submission rejected or pretend it was free. Grants
are frozen against durable heads and old wire identities, then bound to the
exact new input/model/generation and consumed with the new journal intent.
No prompt, credential or response content enters this permission record.
"""
from __future__ import annotations

import json
import time
import uuid

from ..providers import ProviderError
from ..resilience.contracts import candidate_json, digest


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS model_manual_attempt_grants (
        authorization_id TEXT NOT NULL,run_id TEXT NOT NULL,stage_id TEXT NOT NULL,
        source_head_generation INTEGER NOT NULL,source_head_identity TEXT NOT NULL,
        configuration_fingerprint TEXT NOT NULL,plan_hash TEXT NOT NULL,
        source_wires_json TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,
        prepared_generation INTEGER,material_digest TEXT,new_input_digest TEXT,consumed_request_id TEXT,
        PRIMARY KEY(authorization_id,stage_id),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id))""",
)


def _request_wire(row, now):
    value = {"kind": "request", "id": row["request_id"], "stage_id": row["stage_id"],
        "generation": row["generation"], "input_digest": row["input_digest"],
        "configuration_fingerprint": row["configuration_fingerprint"],
        "state": row["state"], "response_received": bool(row["response_received"]),
        "has_candidate": row["candidate_json"] is not None,
        "lease_until": row["lease_until"]}
    value["identity_digest"] = digest(value)
    value["active"] = row["state"] == "running" and row["lease_until"] > now
    value["unknown"] = (row["state"] in {"running", "unknown"}
                        and not value["response_received"] and not value["has_candidate"])
    return value


def read_wire_states(db, run_id: str, now: float) -> tuple[dict, ...]:
    """Safe, content-free snapshot for planner and authorization binding."""
    result = [_request_wire(row, now) for row in db.execute(
        "SELECT * FROM model_request_journal WHERE run_id=? ORDER BY request_id", (run_id,))]
    for row in db.execute("""SELECT a.*,p.payload_json FROM recovery_actions a
        JOIN recovery_plans p ON p.plan_id=a.plan_id
        JOIN recovery_incidents i ON i.incident_id=a.incident_id
        WHERE i.run_id=? AND i.target LIKE 'model-format:%'
        AND a.external_intent=1 ORDER BY a.action_id""", (run_id,)):
        # Plan JSON is our typed, bounded local contract, not model output.
        plan = json.loads(row["payload_json"])
        action = next((item for item in plan["actions"]
                       if digest({"run": run_id, "target": plan["target"], "action": item})
                       == row["action_id"]), None)
        if not action or len(action["target_ids"]) != 1:
            continue
        value = {"kind": "repair", "id": row["action_id"], "stage_id": action["target_ids"][0],
            "generation": plan["expected_generation"], "input_digest": action["input_digest"],
            "configuration_fingerprint": action["configuration_fingerprint"],
            "state": row["state"], "response_received": bool(row["response_received"]),
            "has_candidate": row["result_json"] is not None,
            "external_task_id": row["external_task_id"], "lease_until": row["lease_until"]}
        value["identity_digest"] = digest(value)
        value["active"] = row["state"] == "running" and (row["lease_until"] or 0) > now
        value["unknown"] = (not value["response_received"] and not value["has_candidate"]
                            and not value["external_task_id"]
                            and row["state"] not in {"committed", "cancelled"})
        result.append(value)
    return tuple(sorted(result, key=lambda item: (item["stage_id"], item["kind"], item["id"])))


def authorize_new_attempt(store, run_id: str, stages: tuple[str, ...], configuration: str,
                          plan_hash: str, *, now: float | None = None) -> str:
    """Called only after explicit RPC confirmation of possible duplicate charge."""
    now = time.time() if now is None else now
    if not stages or not configuration or not plan_hash:
        raise ValueError("MODEL_NEW_ATTEMPT_SCOPE_REQUIRED")
    authorization_id = uuid.uuid4().hex
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM research_runs WHERE run_id=? AND status='cancelled'", (run_id,)).fetchone():
            raise ProviderError("Research cancelled", code="MODEL_CANCELLED")
        wires = read_wire_states(db, run_id, now)
        granted = 0
        for stage in stages:
            relevant = [item for item in wires if item["stage_id"] == stage]
            if any(item["active"] for item in relevant):
                raise ProviderError("A request is still active", code="MODEL_REQUEST_IN_PROGRESS")
            unresolved = [item for item in relevant if item["unknown"]]
            if not unresolved:
                continue
            head = db.execute("SELECT * FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                              (run_id, stage)).fetchone()
            if head is None:
                raise ProviderError("Request head unavailable", code="MODEL_NEW_ATTEMPT_HEAD_REQUIRED")
            db.execute("""INSERT INTO model_manual_attempt_grants
                (authorization_id,run_id,stage_id,source_head_generation,source_head_identity,
                 configuration_fingerprint,plan_hash,source_wires_json,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (authorization_id, run_id, stage,
                    head["generation"], head["identity_digest"], configuration, plan_hash,
                    candidate_json(unresolved), now, now + 1800))
            granted += 1
        if not granted:
            raise ProviderError("No unknown request requires authorization",
                                code="MODEL_NEW_ATTEMPT_NOT_REQUIRED")
    return authorization_id


def prepare_grant(db, authorization_id, run_id, stage_id, prior, configuration,
                  material_digest, proposed_generation, now):
    row = db.execute("""SELECT * FROM model_manual_attempt_grants
        WHERE authorization_id=? AND run_id=? AND stage_id=?""",
        (authorization_id, run_id, stage_id)).fetchone()
    if row is None:
        return proposed_generation
    if (row["configuration_fingerprint"] != configuration or prior is None
            or (row["expires_at"] <= now and not row["consumed_request_id"])):
        raise ProviderError("Manual attempt permission is stale", code="MODEL_NEW_ATTEMPT_STALE")
    if row["prepared_generation"] is not None:
        if (prior["generation"] != row["prepared_generation"]
                or row["material_digest"] != material_digest):
            raise ProviderError("Manual attempt input changed", code="MODEL_NEW_ATTEMPT_STALE")
        return row["prepared_generation"]
    if (prior["generation"] != row["source_head_generation"]
            or prior["identity_digest"] != row["source_head_identity"]):
        raise ProviderError("Manual attempt source changed", code="MODEL_NEW_ATTEMPT_STALE")
    generation = max(proposed_generation, prior["generation"] + 1)
    db.execute("""UPDATE model_manual_attempt_grants SET prepared_generation=?,material_digest=?
        WHERE authorization_id=? AND stage_id=?""",
        (generation, material_digest, authorization_id, stage_id))
    return generation


def consume_grant(db, authorization_id, run_id, stage_id, generation, configuration,
                  input_digest, request_id, now):
    if not authorization_id:
        return (), ()
    row = db.execute("""SELECT * FROM model_manual_attempt_grants
        WHERE authorization_id=? AND run_id=? AND stage_id=?""",
        (authorization_id, run_id, stage_id)).fetchone()
    if row is None:
        return (), ()
    if (row["configuration_fingerprint"] != configuration or row["expires_at"] <= now
            or row["prepared_generation"] != generation or row["consumed_request_id"]):
        raise ProviderError("Manual attempt permission is stale", code="MODEL_NEW_ATTEMPT_STALE")
    source = json.loads(row["source_wires_json"])
    current = {(item["kind"], item["id"]): item for item in read_wire_states(db, run_id, now)}
    for item in source:
        saved = current.get((item["kind"], item["id"]))
        if (not saved or saved["identity_digest"] != item["identity_digest"]
                or not saved["unknown"] or saved["active"]):
            raise ProviderError("Unknown wire or input changed", code="MODEL_NEW_ATTEMPT_STALE")
    # New code/model may produce a new prompt. Bind that exact new input once
    # rather than mislabel it as the old wire. The prepared material/head and
    # old identity remain immutable and auditable beside this binding.
    changed = db.execute("""UPDATE model_manual_attempt_grants SET consumed_request_id=?,new_input_digest=?
        WHERE authorization_id=? AND stage_id=? AND consumed_request_id IS NULL""",
        (request_id, input_digest, authorization_id, stage_id))
    if changed.rowcount != 1:
        raise ProviderError("Manual attempt already used", code="MODEL_NEW_ATTEMPT_STALE")
    return (tuple(item["id"] for item in source if item["kind"] == "request"),
            tuple(item["id"] for item in source if item["kind"] == "repair"))
