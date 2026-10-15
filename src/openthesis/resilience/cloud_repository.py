"""Storage Adapter binding cloud page candidates to frozen stage generations."""
from __future__ import annotations

import json
from typing import Any, Callable

from ..domain import utc_now_iso
from .contracts import RecoveryPlan, canonical_json, digest
from .repository import RecoveryConflict, RecoveryRepository


class CloudCandidateRepository(RecoveryRepository):
    head_namespace = "cloud-source:"
    artifact_namespace = "cloud-candidate:"
    candidate_schema = "openthesis.cloud-candidate.v1"
    artifact_type = "vision-candidate-set"
    provider_id = "mineru_flash"
    producer_id = "financial-cloud-recovery"

    def __init__(self, store: Any, run_id: str, target: str, input_digest: str, *,
                 cancel_check: Callable[[], bool],
                 admit_candidate: Callable[[dict], dict], provider_id: str | None = None,
                 submission_identity: str = ""):
        if provider_id is not None:
            self.provider_id = provider_id
        self.run_id, self.target, self.input_digest = run_id, target, input_digest
        self.cancel_check, self.admit_candidate = cancel_check, admit_candidate
        # Reuse the durable stage-head storage rather than a second generation
        # protocol. Cloud source stages occupy a disjoint namespace.
        self.head_id = self.head_namespace + target
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_run(db)
            if submission_identity:
                # Policy/protocol upgrades change the input/config digest, not
                # the truth of an earlier unknown billable submission. Check
                # before replacing the head so the saved intent stays current.
                prior = db.execute("""SELECT p.payload_json,a.state
                    FROM recovery_actions a JOIN recovery_plans p ON p.plan_id=a.plan_id
                    WHERE p.incident_id IN (SELECT incident_id FROM recovery_incidents WHERE run_id=?)
                      AND a.external_intent=1 AND a.external_task_id=''
                      AND a.state NOT IN ('committed','validating')""", (run_id,)).fetchall()
                for item in prior:
                    try:
                        saved = RecoveryPlan.from_dict(json.loads(item["payload_json"]))
                    except (TypeError, ValueError, KeyError) as exc:
                        raise RecoveryConflict("RECOVERY_SAVED_SUBMISSION_IDENTITY_INVALID") from exc
                    if (saved.run_id == run_id and saved.target == target
                            and saved.input_digest != input_digest
                            and any(action.provider_id == self.provider_id
                                    and submission_identity in action.page_ids for action in saved.actions)):
                        raise RecoveryConflict("RECOVERY_SUBMISSION_UNKNOWN")
            row = db.execute("SELECT * FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                             (run_id, self.head_id)).fetchone()
            self.generation = (int(row["generation"]) if row and row["identity_digest"] == input_digest
                               else int(row["generation"]) + 1 if row else 1)
            db.execute("""INSERT INTO model_stage_request_heads VALUES(?,?,?,?)
                ON CONFLICT(run_id,stage_id) DO UPDATE SET generation=excluded.generation,
                identity_digest=excluded.identity_digest""",
                (run_id, self.head_id, self.generation, input_digest))
        super().__init__(store, self._read_snapshot, self._publish)

    def _check_run(self, db) -> None:
        if self.cancel_check():
            raise RecoveryConflict("RECOVERY_CANCELLED")
        run = db.execute("SELECT status FROM research_runs WHERE run_id=?", (self.run_id,)).fetchone()
        if run is None:
            raise RecoveryConflict("RECOVERY_RUN_NOT_FOUND")
        if run["status"] == "cancelled":
            raise RecoveryConflict("RECOVERY_CANCELLED")

    def _read_snapshot(self, db, run_id):
        self._check_run(db)
        row = db.execute("SELECT * FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                         (run_id, self.head_id)).fetchone()
        if row is None:
            raise RecoveryConflict("RECOVERY_PLAN_STALE")
        return int(row["generation"]), row["identity_digest"]

    def _publish(self, db, plan, candidate):
        self._check_run(db)
        # Re-run the pure financial gate at the publication seam as well. An
        # adapter success or a stale verification flag cannot write facts.
        admitted = self.admit_candidate(candidate)
        content = {
            "schema": self.candidate_schema, "target": self.target,
            "input_digest": self.input_digest, "generation": self.generation,
            "candidate_digest": digest(candidate), "result": admitted,
        }
        artifact_id = self.artifact_namespace + digest({"run": plan.run_id, **content})
        encoded = canonical_json(content)
        existing = db.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if existing and existing["payload_json"] != encoded:
            raise RecoveryConflict("ARTIFACT_IDENTITY_CONFLICT")
        if existing is None:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                (artifact_id, plan.run_id, self.artifact_type, self.target, encoded,
                 self.provider_id, self.producer_id, utc_now_iso()))
        return {"revision_id": artifact_id}

    def committed_result(self, revision_id: str) -> dict | None:
        with self.store.connect() as db:
            self._check_run(db)
            generation, input_digest = self._read_snapshot(db, self.run_id)
            if (generation, input_digest) != (self.generation, self.input_digest):
                raise RecoveryConflict("RECOVERY_PLAN_STALE")
            row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND run_id=?",
                             (revision_id, self.run_id)).fetchone()
        if row is None or row["artifact_type"] != self.artifact_type:
            return None
        try:
            value = json.loads(row["payload_json"])
            if (revision_id != self.artifact_namespace + digest({"run": self.run_id, **value})
                    or row["model_id"] != self.provider_id
                    or value.get("schema") != self.candidate_schema
                    or value.get("target") != self.target
                    or value.get("input_digest") != self.input_digest
                    or value.get("generation") != self.generation):
                return None
            return self.admit_candidate(value["result"])
        except (KeyError, TypeError, ValueError, RecoveryConflict):
            return None
