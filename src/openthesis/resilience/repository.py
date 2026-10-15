"""SQLite recovery journal adapter; lease and budget decisions are atomic."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import sqlite3
from typing import Any, Callable, Protocol

from .contracts import (
    RecoveryAction, RecoveryLimits, RecoveryPlan, RecoveryState, Usage,
    candidate_fingerprint, candidate_json, candidate_document_json, canonical_json, digest,
)
from .payloads import SCHEMA_STATEMENT, store_payload, load_payload


SCHEMA_STATEMENTS = (
    SCHEMA_STATEMENT,
    """CREATE TABLE IF NOT EXISTS recovery_incidents (
        incident_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, target TEXT NOT NULL,
        input_digest TEXT NOT NULL, limits_json TEXT NOT NULL,
        action_count INTEGER NOT NULL DEFAULT 0, usage_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, UNIQUE(run_id, target, input_digest),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_plans (
        plan_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, plan_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL, state TEXT NOT NULL, outcome_json TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        FOREIGN KEY(incident_id) REFERENCES recovery_incidents(incident_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_actions (
        action_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, plan_id TEXT NOT NULL,
        state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL,
        started_at REAL, reservation_json TEXT NOT NULL DEFAULT '{}',
        usage_json TEXT NOT NULL DEFAULT '{}', diagnostic_code TEXT NOT NULL DEFAULT '',
        external_intent INTEGER NOT NULL DEFAULT 0, external_task_id TEXT NOT NULL DEFAULT '',
        validation_json TEXT, result_json TEXT, updated_at REAL NOT NULL,
        response_received INTEGER NOT NULL DEFAULT 0, response_receipt_json TEXT,
        FOREIGN KEY(incident_id) REFERENCES recovery_incidents(incident_id),
        FOREIGN KEY(plan_id) REFERENCES recovery_plans(plan_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_target_leases (
        run_id TEXT NOT NULL, target TEXT NOT NULL, plan_id TEXT NOT NULL,
        lease_token TEXT NOT NULL, lease_until REAL NOT NULL,
        PRIMARY KEY(run_id, target), FOREIGN KEY(plan_id) REFERENCES recovery_plans(plan_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL,
        action_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at REAL NOT NULL,
        FOREIGN KEY(plan_id) REFERENCES recovery_plans(plan_id))""",
    """CREATE TABLE IF NOT EXISTS recovery_provider_circuits (
        provider_id TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL,
        transient_failures INTEGER NOT NULL DEFAULT 0, open_until REAL NOT NULL DEFAULT 0,
        probe_token TEXT, probe_until REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(provider_id, configuration_fingerprint))""",
)


class RecoveryConflict(RuntimeError):
    pass


class RecoveryBudgetExceeded(RuntimeError):
    pass


class ConnectionStore(Protocol):
    def connect(self) -> Any: ...


SnapshotReader = Callable[[sqlite3.Connection, str], tuple[int, str]]
Publisher = Callable[[sqlite3.Connection, RecoveryPlan, dict[str, Any]], dict[str, Any]]


class RecoveryRepository:
    """One transaction boundary for plans, attempts and candidate publication.

    Domain-specific snapshot checking and append-only publication are injected
    by the composition root; adapters never receive a database connection.
    """

    def __init__(self, store: ConnectionStore, snapshot_reader: SnapshotReader, publisher: Publisher,
                 *, revalidate_rejected: bool = False):
        self.store = store
        self.snapshot_reader = snapshot_reader
        self.publisher = publisher
        self.revalidate_rejected = revalidate_rejected

    def _check_snapshot(self, db: sqlite3.Connection, plan: RecoveryPlan) -> None:
        generation, input_digest = self.snapshot_reader(db, plan.run_id)
        if generation != plan.expected_generation or input_digest != plan.input_digest:
            raise RecoveryConflict("RECOVERY_PLAN_STALE")

    @staticmethod
    def _event(db: sqlite3.Connection, plan_id: str, state: str, payload: Any, now: float, action_id: str = "") -> None:
        db.execute("INSERT INTO recovery_events(plan_id, action_id, state, payload_json, created_at) VALUES(?,?,?,?,?)",
                   (plan_id, action_id, state, store_payload(db, payload), now))

    @staticmethod
    def candidate_identity(plan: RecoveryPlan, action: RecoveryAction) -> dict[str, Any]:
        action_id = action.identity(plan.run_id, plan.target)
        return {
            "run_id": plan.run_id,
            "target": plan.target,
            "input_digest": plan.input_digest,
            "expected_generation": plan.expected_generation,
            "action_id": action_id,
            "strategy_id": action.strategy_id,
            "strategy_version": action.strategy_version,
            "verifier_id": action.verifier_id,
            "configuration_fingerprint": action.configuration_fingerprint,
        }

    @classmethod
    def _decode_saved_candidate(cls, db, plan: RecoveryPlan, action: RecoveryAction,
                                row: sqlite3.Row, *, require_validation: bool = False,
                                allow_rejected: bool = False) -> dict[str, Any] | None:
        if not row["result_json"] or not row["validation_json"]:
            return None
        try:
            result = load_payload(db, row["result_json"])
            validation = json.loads(row["validation_json"])
            candidate_document_json(result)
            payload = result["candidate"]
            if (not isinstance(result, dict)
                    or set(result) != {"candidate", "artifact_ids", "identity"}
                    or not isinstance(payload, dict)
                    or result.get("identity") != cls.candidate_identity(plan, action)
                    or not isinstance(result.get("artifact_ids"), list)
                    or any(not isinstance(item, str) or not item for item in result["artifact_ids"])
                    or not isinstance(validation, dict)
                    or not (validation.get("passed") is True or validation.get("delta_verified") is True
                            or (not require_validation and (validation.get("pending") is True or allow_rejected)
                                and validation.get("passed") is False))
                    or validation.get("candidate_sha256") != candidate_fingerprint(
                        payload, result["artifact_ids"], result["identity"])):
                return None
            return {"candidate": payload, "artifact_ids": tuple(result["artifact_ids"]),
                    "validation": validation}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save_plan(self, plan: RecoveryPlan, now: float) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_snapshot(db, plan)
            bound = db.execute("SELECT run_id,target,input_digest,limits_json FROM recovery_incidents WHERE incident_id=?",
                               (plan.incident_id,)).fetchone()
            if bound and (bound["run_id"], bound["target"], bound["input_digest"]) != (
                plan.run_id, plan.incident_target, plan.incident_input_digest
            ):
                raise RecoveryConflict("RECOVERY_INCIDENT_IDENTITY_CONFLICT")
            if bound:
                self._effective_limits(bound, plan)
            existing = db.execute("SELECT incident_id FROM recovery_incidents WHERE run_id=? AND target=? AND input_digest=?",
                                  (plan.run_id, plan.incident_target, plan.incident_input_digest)).fetchone()
            if existing and existing["incident_id"] != plan.incident_id:
                raise RecoveryConflict("RECOVERY_INCIDENT_IDENTITY_CONFLICT")
            # Limits belong to the incident, not to a particular model or plan.
            # Extending them needs a separate explicitly authorized operation.
            db.execute("""INSERT OR IGNORE INTO recovery_incidents(
                incident_id,run_id,target,input_digest,limits_json,created_at) VALUES(?,?,?,?,?,?)""",
                (plan.incident_id, plan.run_id, plan.incident_target, plan.incident_input_digest,
                 canonical_json(asdict(plan.limits)), now))
            inserted = db.execute("""INSERT OR IGNORE INTO recovery_plans(
                plan_id,incident_id,plan_hash,payload_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                (plan.plan_id, plan.incident_id, plan.plan_hash, canonical_json(plan.to_dict()), RecoveryState.PLANNED, now, now))
            if inserted.rowcount:
                self._event(db, plan.plan_id, RecoveryState.PLANNED, {"plan_hash": plan.plan_hash}, now)

    def get_plan(self, plan_id: str) -> RecoveryPlan:
        with self.store.connect() as db:
            row = db.execute("SELECT payload_json FROM recovery_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError("RECOVERY_PLAN_NOT_FOUND")
        return RecoveryPlan.from_dict(json.loads(row["payload_json"]))

    def status(self, plan_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM recovery_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise KeyError("RECOVERY_PLAN_NOT_FOUND")
            incident = db.execute("SELECT * FROM recovery_incidents WHERE incident_id=?", (row["incident_id"],)).fetchone()
            actions = db.execute("SELECT * FROM recovery_actions WHERE plan_id=? ORDER BY updated_at,action_id", (plan_id,)).fetchall()
            events = db.execute("SELECT * FROM recovery_events WHERE plan_id=? ORDER BY event_id", (plan_id,)).fetchall()
        return {"plan_id": plan_id, "state": row["state"],
                "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None,
                "action_count": incident["action_count"], "usage": json.loads(incident["usage_json"]),
                "limits": asdict(self._effective_limits(incident, RecoveryPlan.from_dict(json.loads(row["payload_json"])))),
                "actions": [dict(item) for item in actions], "events": [dict(item) for item in events]}

    @staticmethod
    def _effective_limits(incident: Any, plan: RecoveryPlan) -> RecoveryLimits:
        """Intersect frozen incident ceilings with this plan's current consent.

        A later plan cannot enlarge the persisted allowance, but an earlier
        allowance is not permission to ignore a newly smaller one. Usage is
        still charged to the same incident across pages and process restarts.
        A lease is ownership, not spend permission; use this worker's deadline.
        """
        frozen = RecoveryLimits(**json.loads(incident["limits_json"]))
        if frozen.policy_version != plan.limits.policy_version:
            raise RecoveryConflict("RECOVERY_INCIDENT_POLICY_VERSION_CONFLICT")
        return replace(frozen, **{field: min(getattr(frozen, field), getattr(plan.limits, field))
            for field in ("automatic_actions", "transient_retries", "seconds", "calls", "tokens", "pages")},
            lease_seconds=plan.limits.lease_seconds)

    def _owned(self, db: sqlite3.Connection, plan: RecoveryPlan, token: str, now: float) -> None:
        lease = db.execute("SELECT * FROM recovery_target_leases WHERE run_id=? AND target=?", (plan.run_id, plan.target)).fetchone()
        if not lease or lease["lease_token"] != token or lease["plan_id"] != plan.plan_id or lease["lease_until"] <= now:
            raise RecoveryConflict("RECOVERY_LEASE_LOST")
        state = db.execute("SELECT state FROM recovery_plans WHERE plan_id=?", (plan.plan_id,)).fetchone()
        if not state or state["state"] == RecoveryState.CANCELLED:
            raise RecoveryConflict("RECOVERY_CANCELLED")
        self._check_snapshot(db, plan)

    def acquire(self, plan: RecoveryPlan, token: str, now: float) -> bool:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_snapshot(db, plan)
            state = db.execute("SELECT state FROM recovery_plans WHERE plan_id=?", (plan.plan_id,)).fetchone()
            if state is None:
                raise KeyError("RECOVERY_PLAN_NOT_FOUND")
            if state["state"] in {RecoveryState.COMMITTED, RecoveryState.PARTIAL_COMMITTED}:
                return False
            if state["state"] == RecoveryState.CANCELLED:
                raise RecoveryConflict("RECOVERY_CANCELLED")
            existing = db.execute("SELECT * FROM recovery_target_leases WHERE run_id=? AND target=?", (plan.run_id, plan.target)).fetchone()
            if existing and existing["lease_until"] > now:
                raise RecoveryConflict("RECOVERY_TARGET_BUSY")
            if existing:
                previous = db.execute("SELECT * FROM recovery_actions WHERE plan_id=? AND state IN ('running','validating')", (existing["plan_id"],)).fetchall()
                for item in previous:
                    resume_action = next((candidate for candidate in plan.actions
                        if candidate.identity(plan.run_id, plan.target) == item["action_id"]), None)
                    if (item["state"] in {RecoveryState.RUNNING, RecoveryState.VALIDATING}
                            and existing["plan_id"] == plan.plan_id
                            and resume_action is not None
                            and self._decode_saved_candidate(db, plan, resume_action, item) is not None):
                        if item["state"] == RecoveryState.RUNNING:
                            self._charge_interrupted_time(db, item, now)
                        db.execute("UPDATE recovery_actions SET state='validating',lease_token=?,lease_until=?,updated_at=? WHERE action_id=?",
                                   (token, now + plan.limits.lease_seconds, now, item["action_id"]))
                        self._event(db, existing["plan_id"], RecoveryState.VALIDATING,
                                    {"code": "RECOVERY_CANDIDATE_REVERIFY_REQUIRED"}, now, item["action_id"])
                        continue
                    if item["state"] == RecoveryState.VALIDATING:
                        code, new_state = "RECOVERY_CANDIDATE_IDENTITY_INVALID", RecoveryState.BLOCKED_INTEGRITY
                        db.execute("UPDATE recovery_actions SET state=?,diagnostic_code=?,updated_at=? WHERE action_id=?",
                                   (new_state, code, now, item["action_id"]))
                        self._event(db, existing["plan_id"], new_state, {"code": code}, now, item["action_id"])
                        continue
                    # A crashed process may have completed an external request.
                    # Reserved usage is retained, never refunded speculatively.
                    new_state = RecoveryState.WAITING_EXTERNAL if item["external_task_id"] else RecoveryState.NEEDS_ACTION
                    code = ("RECOVERY_EXTERNAL_RESUME" if item["external_task_id"] else
                            "RECOVERY_LOCAL_PROCESSING_INTERRUPTED" if item["response_received"] else
                            "RECOVERY_SUBMISSION_UNKNOWN" if item["external_intent"] else "RECOVERY_EXECUTION_INTERRUPTED")
                    if item["state"] == RecoveryState.RUNNING:
                        self._charge_interrupted_time(db, item, now)
                    db.execute("UPDATE recovery_actions SET state=?,diagnostic_code=?,updated_at=? WHERE action_id=?", (new_state, code, now, item["action_id"]))
                    self._event(db, existing["plan_id"], new_state, {"code": code}, now, item["action_id"])
            # A normally finished plan releases its target lease, but a
            # verifier error can leave an exact pending candidate. Rebind it
            # for validation-only continuation even without an expired lease.
            resumable_states = ("('validating','needs_action','blocked_integrity')" if self.revalidate_rejected
                                else "('validating','needs_action')")
            for item in db.execute(f"SELECT * FROM recovery_actions WHERE plan_id=? AND state IN {resumable_states}",
                                   (plan.plan_id,)).fetchall():
                action = next((candidate for candidate in plan.actions
                               if candidate.identity(plan.run_id, plan.target) == item["action_id"]), None)
                if action is not None and self._decode_saved_candidate(
                        db, plan, action, item, allow_rejected=self.revalidate_rejected) is not None:
                    db.execute("UPDATE recovery_actions SET state='validating',lease_token=?,lease_until=?,updated_at=? WHERE action_id=?",
                               (token, now + plan.limits.lease_seconds, now, item["action_id"]))
            db.execute("""INSERT INTO recovery_target_leases(run_id,target,plan_id,lease_token,lease_until) VALUES(?,?,?,?,?)
                ON CONFLICT(run_id,target) DO UPDATE SET plan_id=excluded.plan_id,lease_token=excluded.lease_token,lease_until=excluded.lease_until""",
                (plan.run_id, plan.target, plan.plan_id, token, now + plan.limits.lease_seconds))
            db.execute("UPDATE recovery_plans SET state=?,updated_at=? WHERE plan_id=?", (RecoveryState.RUNNING, now, plan.plan_id))
            self._event(db, plan.plan_id, RecoveryState.RUNNING, {}, now)
        return True

    def heartbeat(self, plan: RecoveryPlan, token: str, now: float) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            db.execute("UPDATE recovery_target_leases SET lease_until=? WHERE run_id=? AND target=? AND lease_token=?",
                       (now + plan.limits.lease_seconds, plan.run_id, plan.target, token))

    def begin_action(self, plan: RecoveryPlan, action: RecoveryAction, token: str, now: float, *,
                     transient_retry: bool = False, external_observation: bool = False,
                     observation_usage: Usage | None = None) -> str:
        action_id = action.identity(plan.run_id, plan.target)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            incident = db.execute("SELECT * FROM recovery_incidents WHERE incident_id=?", (plan.incident_id,)).fetchone()
            limits = self._effective_limits(incident, plan)
            used = Usage(**json.loads(incident["usage_json"]))
            old = db.execute("SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)).fetchone()
            retries = int(old["retry_count"]) if old else 0
            interrupted = bool(old and old["diagnostic_code"] == "RECOVERY_EXECUTION_INTERRUPTED")
            reservation = action.estimated_usage
            if external_observation:
                if (transient_retry or old is None or old["incident_id"] != plan.incident_id
                        or not old["external_task_id"] or not action.external_submission
                        or old["state"] not in {RecoveryState.WAITING_EXTERNAL, RecoveryState.NEEDS_ACTION}
                        or not isinstance(observation_usage, Usage) or observation_usage.pages
                        or observation_usage.calls < 1
                        or observation_usage.calls > reservation.calls
                        or observation_usage.tokens > reservation.tokens
                        or observation_usage.seconds > reservation.seconds):
                    raise RecoveryConflict("RECOVERY_EXTERNAL_OBSERVATION_INVALID")
                reservation = observation_usage
            elif old and (old["state"] == RecoveryState.COMMITTED or (not transient_retry and not interrupted)
                        or old["external_intent"] or old["external_task_id"]):
                raise RecoveryConflict("RECOVERY_ACTION_ALREADY_ATTEMPTED")
            if transient_retry and (not old or retries >= limits.transient_retries):
                raise RecoveryBudgetExceeded("RECOVERY_TRANSIENT_RETRIES_EXHAUSTED")
            if incident["action_count"] >= limits.automatic_actions or not limits.permits(used, reservation):
                raise RecoveryBudgetExceeded("RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
            reserved = used.plus(reservation)
            db.execute("UPDATE recovery_incidents SET action_count=action_count+1,usage_json=? WHERE incident_id=?", (canonical_json(asdict(reserved)), plan.incident_id))
            db.execute("""INSERT INTO recovery_actions(action_id,incident_id,plan_id,state,attempt,retry_count,
                lease_token,lease_until,started_at,reservation_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(action_id) DO UPDATE SET plan_id=excluded.plan_id,state=excluded.state,
                attempt=recovery_actions.attempt+1,retry_count=excluded.retry_count,lease_token=excluded.lease_token,
                lease_until=excluded.lease_until,started_at=excluded.started_at,reservation_json=excluded.reservation_json,
                validation_json=NULL,result_json=NULL,response_received=0,response_receipt_json=NULL,
                updated_at=excluded.updated_at""",
                (action_id, plan.incident_id, plan.plan_id, RecoveryState.RUNNING, 1, retries + int(transient_retry), token,
                 now + limits.lease_seconds, now, canonical_json(asdict(reservation)), now))
            self._event(db, plan.plan_id, RecoveryState.RUNNING,
                        {"strategy": action.strategy_id, "external_observation": external_observation,
                         "owner_digest": digest(token)}, now, action_id)
        return action_id

    def resume_candidate(self, plan: RecoveryPlan, action: RecoveryAction,
                         token: str, now: float) -> dict[str, Any] | None:
        """Return a durable candidate for independent verification, never production."""
        action_id = action.identity(plan.run_id, plan.target)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            row = db.execute("SELECT * FROM recovery_actions WHERE action_id=? AND plan_id=?",
                             (action_id, plan.plan_id)).fetchone()
            if row is None or row["state"] != RecoveryState.VALIDATING:
                return None
            saved = self._decode_saved_candidate(db, plan, action, row, allow_rejected=self.revalidate_rejected)
            if saved is None:
                code = "RECOVERY_CANDIDATE_IDENTITY_INVALID"
                db.execute("UPDATE recovery_actions SET state=?,diagnostic_code=?,updated_at=? WHERE action_id=?",
                           (RecoveryState.BLOCKED_INTEGRITY, code, now, action_id))
                self._event(db, plan.plan_id, RecoveryState.BLOCKED_INTEGRITY, {"code": code}, now, action_id)
                return None
            return saved

    def record_candidate_validation(self, plan: RecoveryPlan, action: RecoveryAction,
                                    token: str, now: float, validation: dict[str, Any], *,
                                    passed: bool, pending: bool = False,
                                    diagnostic_code: str = "") -> None:
        """Persist fresh post-restart verification without charging production again."""
        action_id = action.identity(plan.run_id, plan.target)
        encoded = candidate_json(validation)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            row = db.execute("SELECT * FROM recovery_actions WHERE action_id=? AND plan_id=? AND lease_token=?",
                             (action_id, plan.plan_id, token)).fetchone()
            saved = self._decode_saved_candidate(db, plan, action, row, allow_rejected=self.revalidate_rejected) if row is not None else None
            if row is None or row["state"] != RecoveryState.VALIDATING or saved is None:
                raise RecoveryConflict("RECOVERY_CANDIDATE_IDENTITY_INVALID")
            if (not isinstance(passed, bool) or not isinstance(pending, bool)
                    or (passed and pending) or validation.get("passed") is not passed
                    or (pending and validation.get("pending") is not True)
                    or validation.get("candidate_sha256") != candidate_fingerprint(
                        saved["candidate"], saved["artifact_ids"], self.candidate_identity(plan, action))):
                raise RecoveryConflict("RECOVERY_VALIDATION_REQUIRED")
            # Production was already accounted at the candidate checkpoint.
            # Charge only validation time; never reserve or charge the remote
            # request again just because a verifier is being resumed.
            elapsed = max(0.0, now - float(row["updated_at"]))
            incident = db.execute("SELECT usage_json FROM recovery_incidents WHERE incident_id=?",
                                  (plan.incident_id,)).fetchone()
            incident_usage = Usage(**json.loads(incident["usage_json"])).plus(Usage(seconds=elapsed))
            action_usage = Usage(**json.loads(row["usage_json"])).plus(Usage(seconds=elapsed))
            db.execute("UPDATE recovery_incidents SET usage_json=? WHERE incident_id=?",
                       (canonical_json(asdict(incident_usage)), plan.incident_id))
            delta_verified = (validation.get("delta_verified") is True and bool(validation.get("accepted_delta"))
                              and not validation.get("invariant_failures"))
            state = RecoveryState.VALIDATING if passed or pending or delta_verified else RecoveryState.BLOCKED_INTEGRITY
            code = diagnostic_code if pending else "" if passed else "RECOVERY_VERIFIED_PARTIAL_DELTA" if delta_verified else "RECOVERY_CANDIDATE_REJECTED"
            db.execute("UPDATE recovery_actions SET state=?,diagnostic_code=?,validation_json=?,usage_json=?,updated_at=? WHERE action_id=?",
                       (state, code, encoded, canonical_json(asdict(action_usage)), now, action_id))
            self._event(db, plan.plan_id, state, {"code": code, "candidate_sha256": candidate_fingerprint(
                saved["candidate"], saved["artifact_ids"], self.candidate_identity(plan, action))}, now, action_id)

    def observe_candidate(self, plan: RecoveryPlan, action: RecoveryAction, token: str,
                          now: float, *, candidate: dict[str, Any],
                          artifact_ids: tuple[str, ...]) -> None:
        """Preserve a returned observation without granting publication rights.

        A head change, cancellation or lease expiry does not erase a response
        already obtained. An immutable event retains its original identity even
        if a newer owner has taken the action row. Only the original owner's
        still-unvalidated row may receive the pending checkpoint. No state,
        usage or lease is promoted here; validation and settlement remain owned.
        """
        action_id = action.identity(plan.run_id, plan.target)
        result = {"identity": self.candidate_identity(plan, action),
                  "candidate": candidate, "artifact_ids": artifact_ids}
        candidate_document_json(result)
        validation = {"passed": False, "pending": True,
                      "candidate_sha256": candidate_fingerprint(
                          candidate, artifact_ids, result["identity"])}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)).fetchone()
            owns_row = bool(row and row["plan_id"] == plan.plan_id and row["lease_token"] == token)
            if not owns_row:
                # A replaced worker may append its observation only if the
                # durable start event proves it actually owned this attempt.
                starts = db.execute("""SELECT payload_json FROM recovery_events
                    WHERE plan_id=? AND action_id=? AND state='running'""",
                    (plan.plan_id, action_id)).fetchall()
                if not any(load_payload(db, item["payload_json"]).get("owner_digest") == digest(token)
                           for item in starts):
                    raise RecoveryConflict("RECOVERY_ACTION_LEASE_LOST")
            self._event(db, plan.plan_id, RecoveryState.RUNNING,
                        {"code": "RECOVERY_CANDIDATE_OBSERVED", "result": result, "validation": validation,
                         "owner_digest": digest(token)}, now, action_id)
            if owns_row and row["result_json"] is None:
                encoded = store_payload(db, result)
                db.execute("""UPDATE recovery_actions SET result_json=?,validation_json=?
                    WHERE action_id=? AND plan_id=? AND lease_token=? AND result_json IS NULL""",
                    (encoded, candidate_json(validation), action_id, plan.plan_id, token))

    def checkpoint_candidate(self, plan: RecoveryPlan, action: RecoveryAction, token: str,
                             now: float, *, usage: Usage, candidate: dict[str, Any],
                             artifact_ids: tuple[str, ...]) -> None:
        """Persist detached production BEFORE verification; not publishable yet.

        A process dying in the validator can revalidate this exact candidate
        instead of invoking a paid producer again. Pending proof is accepted
        for resume only; the publication seam still requires a passed proof.
        """
        result = {"identity": self.candidate_identity(plan, action),
                  "candidate": candidate, "artifact_ids": artifact_ids}
        fingerprint = candidate_fingerprint(candidate, artifact_ids, result["identity"])
        self.finish_action(plan, action.identity(plan.run_id, plan.target), token, now,
                           state=RecoveryState.VALIDATING, usage=usage, result=result,
                           validation={"passed": False, "pending": True,
                                       "candidate_sha256": fingerprint})

    def external_intent(self, plan: RecoveryPlan, action_id: str, token: str, now: float) -> None:
        self._update_action(plan, action_id, token, now, "external_intent", 1)

    def external_task(self, plan: RecoveryPlan, action_id: str, task_id: str, token: str, now: float) -> None:
        if not task_id:
            raise ValueError("RECOVERY_EXTERNAL_TASK_REQUIRED")
        self._update_action(plan, action_id, token, now, "external_task_id", task_id)

    def external_response(self, plan: RecoveryPlan, action_id: str, token: str, now: float, receipt: dict) -> None:
        if not isinstance(receipt, dict) or receipt.get("response_received") is not True:
            raise ValueError("RECOVERY_RESPONSE_RECEIPT_UNPROVEN")
        encoded = candidate_json(receipt)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM recovery_actions WHERE action_id=?", (action_id,)).fetchone()
            owns_row = bool(row and row["plan_id"] == plan.plan_id
                            and row["lease_token"] == token and row["external_intent"] == 1)
            if not owns_row:
                # Receiving a wire result is an observation, not permission to
                # publish into the current generation. Retain late receipts only
                # for an owner proven by the immutable start event.
                starts = db.execute("""SELECT payload_json FROM recovery_events
                    WHERE plan_id=? AND action_id=? AND state='running'""",
                    (plan.plan_id, action_id)).fetchall()
                if not any(load_payload(db, item["payload_json"]).get("owner_digest") == digest(token)
                           for item in starts):
                    raise RecoveryConflict("RECOVERY_ACTION_LEASE_LOST")
            if owns_row:
                db.execute("""UPDATE recovery_actions SET response_received=1,
                response_receipt_json=?,updated_at=?
                WHERE action_id=? AND plan_id=? AND lease_token=?
                  AND external_intent=1 AND response_received=0""",
                    (encoded, now, action_id, plan.plan_id, token))
            self._event(db, plan.plan_id, RecoveryState.RUNNING,
                        {"code": "RECOVERY_RESPONSE_OBSERVED", "response_received": True,
                         "receipt": receipt, "receipt_sha256": digest(receipt),
                         "owner_digest": digest(token)}, now, action_id)

    def external_rejection(self, plan: RecoveryPlan, action_id: str, token: str, now: float,
                           status: int, *, reason: str = "") -> None:
        """Clear only a proven rejected creation, not a lost/failed response."""
        format_rejection = status in {400, 422} and reason == "response_format_unsupported"
        if (reason not in {"", "response_format_unsupported"}
                or isinstance(status, bool) or not isinstance(status, int)
                or not (status in {401, 403, 429} or format_rejection)):
            raise ValueError("RECOVERY_EXTERNAL_REJECTION_UNPROVEN")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            changed = db.execute("""UPDATE recovery_actions SET external_intent=0,updated_at=?
                WHERE action_id=? AND plan_id=? AND lease_token=? AND state='running'
                  AND external_intent=1 AND external_task_id=''""", (now, action_id, plan.plan_id, token))
            if changed.rowcount != 1:
                raise RecoveryConflict("RECOVERY_EXTERNAL_REJECTION_UNPROVEN")
            self._event(db, plan.plan_id, RecoveryState.RUNNING,
                        {"remote_rejection_status": status,
                         **({"reason": reason} if reason else {})}, now, action_id)

    def reserve_remote_calls(self, plan: RecoveryPlan, action_id: str, token: str,
                             now: float, *, calls: int, max_calls: int) -> None:
        """Debit a registered single-wire extension BEFORE the next request.

        Its policy ceiling comes from StrategyRegistry, not a provider reply.
        No counter is refunded on crash, cancellation or an unknown response.
        The action and incident updates share the owning lease transaction.
        """
        if (isinstance(calls, bool) or isinstance(max_calls, bool)
                or not isinstance(calls, int) or not isinstance(max_calls, int)
                or not 0 <= calls <= max_calls <= 128):
            raise ValueError("RECOVERY_REMOTE_CALL_RESERVATION_INVALID")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            row = db.execute("""SELECT * FROM recovery_actions
                WHERE action_id=? AND plan_id=? AND lease_token=? AND state='running'""",
                (action_id, plan.plan_id, token)).fetchone()
            if row is None:
                raise RecoveryConflict("RECOVERY_ACTION_LEASE_LOST")
            reservation = Usage(**json.loads(row["reservation_json"]))
            if calls <= reservation.calls:
                return
            incident = db.execute("SELECT * FROM recovery_incidents WHERE incident_id=?",
                                  (plan.incident_id,)).fetchone()
            extra = Usage(calls=calls - reservation.calls)
            used = Usage(**json.loads(incident["usage_json"]))
            if not self._effective_limits(incident, plan).permits(used, extra):
                raise RecoveryBudgetExceeded("RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
            db.execute("UPDATE recovery_incidents SET usage_json=? WHERE incident_id=?",
                       (canonical_json(asdict(used.plus(extra))), plan.incident_id))
            db.execute("UPDATE recovery_actions SET reservation_json=?,updated_at=? WHERE action_id=?",
                       (canonical_json(asdict(reservation.plus(extra))), now, action_id))
            self._event(db, plan.plan_id, RecoveryState.RUNNING,
                        {"remote_call_reservation": calls}, now, action_id)

    def _update_action(self, plan: RecoveryPlan, action_id: str, token: str, now: float, column: str, value: Any) -> None:
        if column not in {"external_intent", "external_task_id"}:
            raise ValueError("RECOVERY_JOURNAL_FIELD_INVALID")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            cursor = db.execute(f"UPDATE recovery_actions SET {column}=?,updated_at=? WHERE action_id=? AND plan_id=? AND lease_token=?",
                                (value, now, action_id, plan.plan_id, token))
            if cursor.rowcount != 1:
                raise RecoveryConflict("RECOVERY_ACTION_LEASE_LOST")
            self._event(db, plan.plan_id, RecoveryState.WAITING_EXTERNAL, {column: value}, now, action_id)

    def finish_action(self, plan: RecoveryPlan, action_id: str, token: str, now: float, *, state: RecoveryState,
                      usage: Usage, diagnostic_code: str = "", validation: Any = None, result: Any = None,
                      preserve_candidate: bool = False) -> None:
        if result is not None:
            candidate_document_json(result)
        encoded_validation = candidate_json(validation) if validation is not None else None
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            encoded_result = store_payload(db, result) if result is not None else None
            row = db.execute("SELECT * FROM recovery_actions WHERE action_id=? AND plan_id=? AND lease_token=?", (action_id, plan.plan_id, token)).fetchone()
            if row is None or row["state"] not in {RecoveryState.RUNNING, RecoveryState.VALIDATING}:
                raise RecoveryConflict("RECOVERY_ACTION_LEASE_LOST")
            if preserve_candidate:
                action = next((item for item in plan.actions
                               if item.identity(plan.run_id, plan.target) == action_id), None)
                saved = self._decode_saved_candidate(db, plan, action, row) if action is not None else None
                if saved is not None:
                    if row["state"] == RecoveryState.VALIDATING:
                        # Production and validation already settled. A failed
                        # publication transaction must not erase the paid raw
                        # candidate, reset usage, or force production on resume.
                        self._event(db, plan.plan_id, RecoveryState.FAILED_INTERNAL,
                                    {"code": diagnostic_code, "candidate_preserved": True}, now, action_id)
                        return
                    state = RecoveryState.VALIDATING
                    encoded_result, encoded_validation = row["result_json"], row["validation_json"]
            reservation = Usage(**json.loads(row["reservation_json"]))
            actual = usage.plus(Usage(seconds=max(0.0, now - row["started_at"])))
            incident = db.execute("SELECT * FROM recovery_incidents WHERE incident_id=?", (plan.incident_id,)).fetchone()
            used = Usage(**json.loads(incident["usage_json"]))
            # Keep conservative call/token/page charges, including requests
            # whose external outcome is unknown. Wall time is different: this
            # owned worker has finished, so its elapsed time is known. A
            # 60-second transport ceiling must not become 60 seconds spent for
            # every sub-second status query. Interrupted workers still use the
            # separate conservative reconciliation path below.
            extra = Usage(max(0, actual.calls - reservation.calls), max(0, actual.tokens - reservation.tokens),
                          max(0, actual.pages - reservation.pages))
            settled = Usage(used.calls + extra.calls, used.tokens + extra.tokens,
                used.pages + extra.pages, max(0.0, used.seconds - reservation.seconds) + actual.seconds)
            db.execute("UPDATE recovery_incidents SET usage_json=? WHERE incident_id=?",
                (canonical_json(asdict(settled)), plan.incident_id))
            prior_accounted = Usage(**json.loads(row["usage_json"] or "{}"))
            accounted = prior_accounted.plus(Usage(reservation.calls + extra.calls,
                reservation.tokens + extra.tokens, reservation.pages + extra.pages, actual.seconds))
            if state == RecoveryState.VALIDATING:
                action = next((item for item in plan.actions
                               if item.identity(plan.run_id, plan.target) == action_id), None)
                try:
                    saved = load_payload(db, encoded_result) if encoded_result else None
                    verified = json.loads(encoded_validation) if encoded_validation else None
                except (TypeError, json.JSONDecodeError):
                    saved, verified = None, None
                if (action is None or not isinstance(saved, dict) or not isinstance(saved.get("candidate"), dict)
                        or saved.get("identity") != self.candidate_identity(plan, action)
                        or not isinstance(verified, dict)
                        or not (verified.get("passed") is True
                                or (verified.get("pending") is True and verified.get("passed") is False))
                        or verified.get("candidate_sha256") != candidate_fingerprint(
                            saved["candidate"], saved["artifact_ids"], saved["identity"])):
                    raise RecoveryConflict("RECOVERY_VALIDATION_REQUIRED")
            db.execute("UPDATE recovery_actions SET state=?,usage_json=?,diagnostic_code=?,validation_json=?,result_json=?,updated_at=? WHERE action_id=?",
                       (state, canonical_json(asdict(accounted)), diagnostic_code,
                        encoded_validation, encoded_result, now, action_id))
            self._event(db, plan.plan_id, state, {"code": diagnostic_code, "usage": asdict(accounted)}, now, action_id)

    def _with_committed_progress(self, db: sqlite3.Connection, plan: RecoveryPlan,
                                 outcome: dict[str, Any]) -> dict[str, Any]:
        """Fold only this frozen plan's published, independently bound deltas.

        Later failure, budget exhaustion or process interruption cannot erase
        earlier publication. An outcome's self-reported fields are not proof.
        """
        accepted, resolved, invalidated, provenance = [], [], [], []
        for row in db.execute("SELECT * FROM recovery_actions WHERE plan_id=? AND state='committed' ORDER BY updated_at,action_id",
                              (plan.plan_id,)).fetchall():
            action = next((item for item in plan.actions
                           if item.identity(plan.run_id, plan.target) == row["action_id"]), None)
            saved = self._decode_saved_candidate(db, plan, action, row, require_validation=True) if action else None
            if saved is None:
                raise RecoveryConflict("RECOVERY_COMMITTED_DELTA_PROOF_INVALID")
            proof = saved["validation"]
            accepted.extend(proof.get("accepted_delta", ()))
            resolved.extend(proof.get("resolved_issue_ids", ()))
            invalidated.extend(proof.get("invalidated_dependencies", ()))
            for item in proof.get("provenance", ()):
                if item not in provenance:
                    provenance.append(item)
        if not accepted and not resolved:
            return outcome
        resolved_set = set(resolved)
        return {**outcome,
                "accepted_delta": list(dict.fromkeys(accepted)),
                "resolved_issue_ids": list(dict.fromkeys(resolved)),
                "remaining_issue_ids": [issue.issue_id for issue in plan.issues
                                        if issue.issue_id not in resolved_set],
                "invalidated_dependencies": list(dict.fromkeys(invalidated)),
                "provenance": provenance}

    def finish(self, plan: RecoveryPlan, token: str, now: float, outcome: dict[str, Any], *,
               candidate: dict[str, Any] | None = None, continue_plan: bool = False) -> dict[str, Any]:
        if candidate is not None:
            candidate_document_json(candidate)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            if candidate is not None:
                validated = db.execute("SELECT * FROM recovery_actions WHERE plan_id=? AND state='validating' AND validation_json IS NOT NULL", (plan.plan_id,)).fetchall()
                matching_action_id = ""
                for row in validated:
                    action = next((item for item in plan.actions
                                   if item.identity(plan.run_id, plan.target) == row["action_id"]), None)
                    saved = self._decode_saved_candidate(db, plan, action, row, require_validation=True) if action is not None else None
                    # JSON has one array type: tuples in typed checkpoints
                    # round-trip as lists. Compare canonical bytes, not Python
                    # container types, without weakening the payload identity.
                    if saved is not None and canonical_json(saved["candidate"]) == canonical_json(candidate):
                        if matching_action_id:
                            raise RecoveryConflict("RECOVERY_VALIDATION_AMBIGUOUS")
                        matching_action_id = row["action_id"]
                if not matching_action_id:
                    raise RecoveryConflict("RECOVERY_VALIDATION_REQUIRED")
                incident = db.execute("SELECT * FROM recovery_incidents WHERE incident_id=?", (plan.incident_id,)).fetchone()
                if not self._effective_limits(incident, plan).permits(Usage(**json.loads(incident["usage_json"])), Usage()):
                    raise RecoveryBudgetExceeded("RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
                proof = json.loads(next(row["validation_json"] for row in validated
                                        if row["action_id"] == matching_action_id))
                if any(set(outcome.get(key, ())) != set(proof.get(key, ())) for key in (
                        "accepted_delta", "remaining_issue_ids", "resolved_issue_ids", "invalidated_dependencies")):
                    raise RecoveryConflict("RECOVERY_DELTA_COMMIT_CONTRACT_INVALID")
                if canonical_json(outcome.get("provenance", ())) != canonical_json(proof.get("provenance", ())):
                    raise RecoveryConflict("RECOVERY_DELTA_COMMIT_CONTRACT_INVALID")
                publication = self.publisher(db, plan, candidate)
                outcome = {**outcome, "state": RecoveryState.PARTIAL_COMMITTED
                           if outcome.get("remaining_issue_ids") else RecoveryState.COMMITTED,
                           "committed_revision_id": publication.get("revision_id", "")}
                db.execute("UPDATE recovery_actions SET state='committed' WHERE action_id=?", (matching_action_id,))
                if continue_plan and outcome.get("remaining_issue_ids"):
                    try:
                        # A publisher may advance its business generation.
                        # Do not relabel frozen inputs or silently rebase the
                        # user's plan: retain the delta and request a new plan.
                        self._check_snapshot(db, plan)
                    except RecoveryConflict:
                        outcome = {**outcome, "next_actions": ["refresh_plan_from_current_snapshot"]}
                    else:
                        outcome = {**outcome, "state": RecoveryState.RUNNING}
            outcome = self._with_committed_progress(db, plan, outcome)
            db.execute("UPDATE recovery_plans SET state=?,outcome_json=?,updated_at=? WHERE plan_id=?",
                       (outcome["state"], canonical_json(outcome), now, plan.plan_id))
            self._event(db, plan.plan_id, outcome["state"], outcome, now)
            if outcome["state"] != RecoveryState.RUNNING:
                db.execute("DELETE FROM recovery_target_leases WHERE run_id=? AND target=? AND lease_token=?", (plan.run_id, plan.target, token))
        return outcome

    def release_owned_lease(self, plan: RecoveryPlan, token: str, now: float) -> None:
        """Let an unwound worker resume without erasing its pending journal.

        Keep the expired lease row so acquire can reconcile interrupted actions
        and uncertain external intents. Never release another worker's lease.
        A hard process kill still relies on normal lease expiry.
        """
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE recovery_target_leases SET lease_until=MIN(lease_until,?)
                WHERE run_id=? AND target=? AND plan_id=? AND lease_token=?""",
                       (now, plan.run_id, plan.target, plan.plan_id, token))

    @staticmethod
    def _charge_interrupted_time(db: sqlite3.Connection, action: sqlite3.Row, now: float) -> None:
        reservation = Usage(**json.loads(action["reservation_json"]))
        seconds = max(0.0, now - float(action["started_at"] or now))
        extra = max(0.0, seconds - reservation.seconds)
        incident = db.execute("SELECT usage_json FROM recovery_incidents WHERE incident_id=?", (action["incident_id"],)).fetchone()
        used = Usage(**json.loads(incident["usage_json"]))
        db.execute("UPDATE recovery_incidents SET usage_json=? WHERE incident_id=?",
                   (canonical_json(asdict(used.plus(Usage(seconds=extra)))), action["incident_id"]))
        prior = Usage(**json.loads(action["usage_json"] or "{}"))
        accounted = prior.plus(reservation).plus(Usage(seconds=extra))
        db.execute("UPDATE recovery_actions SET usage_json=? WHERE action_id=?",
                   (canonical_json(asdict(accounted)), action["action_id"]))

    def charge_wait(self, plan: RecoveryPlan, token: str, now: float, seconds: float) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, plan, token, now)
            row = db.execute("SELECT * FROM recovery_incidents WHERE incident_id=?", (plan.incident_id,)).fetchone()
            used = Usage(**json.loads(row["usage_json"])).plus(Usage(seconds=seconds))
            db.execute("UPDATE recovery_incidents SET usage_json=? WHERE incident_id=?", (canonical_json(asdict(used)), plan.incident_id))
            last_action = db.execute("SELECT * FROM recovery_actions WHERE plan_id=? AND lease_token=? ORDER BY started_at DESC LIMIT 1",
                                     (plan.plan_id, token)).fetchone()
            if last_action is not None:
                action_usage = Usage(**json.loads(last_action["usage_json"] or "{}")).plus(Usage(seconds=seconds))
                db.execute("UPDATE recovery_actions SET usage_json=? WHERE action_id=?",
                           (canonical_json(asdict(action_usage)), last_action["action_id"]))
            if not self._effective_limits(row, plan).permits(used, Usage()):
                # Accounting commits even when this wait used the last budget.
                self._event(db, plan.plan_id, RecoveryState.EXHAUSTED, {"code": "RECOVERY_INCIDENT_TIME_EXHAUSTED"}, now)

    def abandon(self, plan: RecoveryPlan, token: str, now: float, outcome: dict[str, Any]) -> dict[str, Any]:
        """Journal a stopped/stale worker without publishing any candidate."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT state,outcome_json FROM recovery_plans WHERE plan_id=?", (plan.plan_id,)).fetchone()
            lease = db.execute("SELECT * FROM recovery_target_leases WHERE run_id=? AND target=?", (plan.run_id, plan.target)).fetchone()
            if saved and saved["state"] in {RecoveryState.COMMITTED, RecoveryState.PARTIAL_COMMITTED, RecoveryState.CANCELLED}:
                return json.loads(saved["outcome_json"]) if saved["outcome_json"] else {**outcome, "state": saved["state"]}
            if lease and lease["lease_token"] != token:
                return outcome
            active = db.execute("SELECT * FROM recovery_actions WHERE plan_id=? AND lease_token=? AND state='running'", (plan.plan_id, token)).fetchall()
            for action in active:
                self._charge_interrupted_time(db, action, now)
            db.execute("UPDATE recovery_actions SET state=?,diagnostic_code=?,updated_at=? WHERE plan_id=? AND lease_token=? AND state IN ('running','validating')",
                       (outcome["state"], outcome.get("diagnostic_code", ""), now, plan.plan_id, token))
            incident = db.execute("SELECT usage_json FROM recovery_incidents WHERE incident_id=?", (plan.incident_id,)).fetchone()
            outcome = {**outcome, "usage": json.loads(incident["usage_json"])}
            outcome = self._with_committed_progress(db, plan, outcome)
            db.execute("UPDATE recovery_plans SET state=?,outcome_json=?,updated_at=? WHERE plan_id=?",
                       (outcome["state"], canonical_json(outcome), now, plan.plan_id))
            db.execute("DELETE FROM recovery_target_leases WHERE run_id=? AND target=? AND lease_token=?", (plan.run_id, plan.target, token))
            self._event(db, plan.plan_id, outcome["state"], outcome, now)
        return outcome

    def cancel(self, plan_id: str, now: float) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,incident_id FROM recovery_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise KeyError("RECOVERY_PLAN_NOT_FOUND")
            if row["state"] in {RecoveryState.COMMITTED, RecoveryState.PARTIAL_COMMITTED}:
                return
            running = db.execute("SELECT * FROM recovery_actions WHERE plan_id=? AND state='running'", (plan_id,)).fetchall()
            for action in running:
                self._charge_interrupted_time(db, action, now)
            db.execute("UPDATE recovery_actions SET state=?,diagnostic_code=?,updated_at=? WHERE plan_id=? AND state IN ('running','validating')",
                       (RecoveryState.CANCELLED, "RECOVERY_CANCELLED", now, plan_id))
            incident = db.execute("SELECT usage_json FROM recovery_incidents WHERE incident_id=?", (row["incident_id"],)).fetchone()
            outcome = {"state": RecoveryState.CANCELLED,
                       "next_actions": ["resume_with_new_plan"],
                       "diagnostic_code": "RECOVERY_CANCELLED",
                       "usage": json.loads(incident["usage_json"])}
            db.execute("UPDATE recovery_plans SET state=?,outcome_json=?,updated_at=? WHERE plan_id=?",
                       (RecoveryState.CANCELLED, canonical_json(outcome), now, plan_id))
            db.execute("DELETE FROM recovery_target_leases WHERE plan_id=?", (plan_id,))
            self._event(db, plan_id, RecoveryState.CANCELLED, outcome, now)

    def allow_provider_request(self, provider_id: str, fingerprint: str, token: str, now: float) -> bool:
        """After three transient failures only one post-cooldown probe runs."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO recovery_provider_circuits(provider_id,configuration_fingerprint) VALUES(?,?)", (provider_id, fingerprint))
            row = db.execute("SELECT * FROM recovery_provider_circuits WHERE provider_id=? AND configuration_fingerprint=?", (provider_id, fingerprint)).fetchone()
            if row["transient_failures"] < 3:
                return True
            if row["open_until"] > now or (row["probe_token"] and row["probe_until"] > now):
                return False
            db.execute("UPDATE recovery_provider_circuits SET probe_token=?,probe_until=? WHERE provider_id=? AND configuration_fingerprint=?", (token, now + 60.0, provider_id, fingerprint))
            return True

    def provider_result(self, provider_id: str, fingerprint: str, token: str, now: float, *, success: bool, transient: bool) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM recovery_provider_circuits WHERE provider_id=? AND configuration_fingerprint=?", (provider_id, fingerprint)).fetchone()
            if row is None:
                return
            if row["probe_token"] and row["probe_token"] != token:
                return
            failures = 0 if success else int(row["transient_failures"]) + int(transient)
            db.execute("UPDATE recovery_provider_circuits SET transient_failures=?,open_until=?,probe_token=NULL,probe_until=0 WHERE provider_id=? AND configuration_fingerprint=?",
                       (failures, now + 60.0 if failures >= 3 else 0.0, provider_id, fingerprint))
