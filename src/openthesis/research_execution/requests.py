"""Durable initial model requests, separate from additional repair budgets.

No prompts, credentials or guessed prices enter this journal. Request digests
include the full prompt and configured-model identity. A returned response is
only a candidate; stage business validation remains the sole trust boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import math
import re
import threading
import time
import uuid
from typing import Any, Callable

from ..providers import ProviderError
from ..resilience.contracts import RecoveryLimits, candidate_json, candidate_document_json, digest, MAX_CANDIDATE_JSON_BYTES
from .responses import ModelResponse, RESPONSE_SCHEMA, MAX_RESPONSE_BYTES, response_record, restore_response
from .manual_attempts import SCHEMA_STATEMENTS as MANUAL_SCHEMA, prepare_grant, consume_grant
from .assertion_cache import SCHEMA_STATEMENTS as ASSERTION_SCHEMA
from .call_budget import SCHEMA_STATEMENTS as CALL_BUDGET_SCHEMA
from .model_format import (
    ModelFormatRecovery, check_unknown_format_submission, proven_format_rejection,
    proven_rate_rejection, rejection_retry_after,
)


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS model_wire_diagnostics (
        request_id TEXT NOT NULL, attempt_id TEXT NOT NULL, run_id TEXT NOT NULL,
        stage_id TEXT NOT NULL, purpose TEXT NOT NULL, diagnostics_json TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY(request_id,attempt_id),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_stage_request_heads (
        run_id TEXT NOT NULL, stage_id TEXT NOT NULL, generation INTEGER NOT NULL,
        identity_digest TEXT NOT NULL, PRIMARY KEY(run_id,stage_id),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id))""",
    """CREATE TABLE IF NOT EXISTS model_request_journal (
        request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage_id TEXT NOT NULL,
        generation INTEGER NOT NULL, input_digest TEXT NOT NULL,
        configuration_fingerprint TEXT NOT NULL, state TEXT NOT NULL,
        owner_id TEXT NOT NULL, lease_until REAL NOT NULL,
        candidate_json TEXT, candidate_digest TEXT, diagnostic_code TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        response_received INTEGER NOT NULL DEFAULT 0, receipt_json TEXT,
        diagnostic_trace_id TEXT NOT NULL DEFAULT '', candidate_schema TEXT NOT NULL DEFAULT 'legacy',
        authorization_id TEXT NOT NULL DEFAULT '', supersedes_json TEXT NOT NULL DEFAULT '[]',
        purpose TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id))""",
    """CREATE INDEX IF NOT EXISTS model_request_wire_identity
        ON model_request_journal(run_id,stage_id,input_digest,state)""",
    """CREATE TABLE IF NOT EXISTS model_response_chunks (
        request_id TEXT NOT NULL, ordinal INTEGER NOT NULL, content TEXT NOT NULL,
        content_digest TEXT NOT NULL, PRIMARY KEY(request_id,ordinal),
        FOREIGN KEY(request_id) REFERENCES model_request_journal(request_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_result_reuse (
        run_id TEXT NOT NULL, stage_id TEXT NOT NULL, generation INTEGER NOT NULL,
        source_request_id TEXT NOT NULL, input_digest TEXT NOT NULL,
        PRIMARY KEY(run_id,stage_id,generation,source_request_id),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
) + MANUAL_SCHEMA + ASSERTION_SCHEMA + CALL_BUDGET_SCHEMA


class ModelRequestJournal:
    def __init__(self, store: Any, run_id: str, configuration_fingerprint: str, *,
                 timeout_seconds: float, cancel_check: Callable[[], bool],
                 clock: Callable[[], float] = time.time, repair_limits: RecoveryLimits | None = None,
                 recovery_sleeper: Callable[[float], None] = time.sleep,
                 manual_authorization_id: str = ""):
        if (not isinstance(run_id, str) or not run_id.strip()
                or not isinstance(configuration_fingerprint, str) or not configuration_fingerprint.strip()
                or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
                or (repair_limits is not None and not isinstance(repair_limits, RecoveryLimits))):
            raise ValueError("MODEL_REQUEST_JOURNAL_CONFIGURATION_INVALID")
        self.store, self.run_id = store, run_id
        self.configuration_fingerprint = configuration_fingerprint
        self.timeout_seconds = timeout_seconds
        self.cancel_check, self.clock = cancel_check, clock
        self.repair_limits = repair_limits or RecoveryLimits(calls=1, transient_retries=0)
        self.recovery_sleeper = recovery_sleeper
        self.manual_authorization_id = manual_authorization_id
        self.budget_only = False
        self._local = threading.local()
        self._stage_configurations: dict[str, str] = {}
        self.candidate_read_issues: list[dict[str, str]] = []
        self.run_envelope = None
        self._automatic_deadline = None
        self._automatic_limit_seconds = None
        self.adaptive_resume = False

    def prepare(self, registry, material_digest: str, invalidated: tuple[str, ...] = (), *,
                stage_configurations: dict[str, str] | None = None,
                local_only: bool = False, stage_material_digests: dict[str, str] | None = None,
                planned_input_tokens: int | None = None, planned_requests=None) -> dict[str, int]:
        """Freeze graph generations before submitting any external request."""
        configurations = dict(stage_configurations or {})
        material_digests = dict(stage_material_digests or {})
        if (set(configurations) - {spec.stage_id for spec in registry.stages}
                or set(material_digests) - {spec.stage_id for spec in registry.stages}
                or any(not isinstance(value, str) or not value for value in (*configurations.values(), *material_digests.values()))):
            raise ValueError("MODEL_STAGE_CONFIGURATION_INVALID")
        self._stage_configurations = configurations
        invalid = set(registry.descendants(invalidated)) if stage_configurations is None else set(invalidated)
        generations = {}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from .call_budget import prepare_plan
            prepare_plan(db, self.run_id, [spec.stage_id for spec in registry.stages], planned_input_tokens,
                         request_plan=planned_requests, envelope=self.run_envelope)
            from .call_budget import plan_status
            frozen_plan = plan_status(db, self.run_id)
            self.adaptive_resume = bool(self.budget_only and (frozen_plan or {}).get("policy") == "adaptive")
            limits = (frozen_plan or {}).get("user_envelope")
            self._automatic_deadline = None
            self._automatic_limit_seconds = None
            if limits and limits.get("max_automatic_seconds") is not None:
                remaining = limits["max_automatic_seconds"] - max(0, self.clock() - frozen_plan["started_at"])
                self._automatic_deadline = time.monotonic() + max(0, remaining)
                self._automatic_limit_seconds = limits["max_automatic_seconds"]
            # The service preflight is advisory: another worker can acquire a
            # wire lease between that read and this transaction. Local reuse
            # must never invalidate the head of a still-running paid request.
            if local_only and db.execute("""SELECT 1 FROM model_request_journal
                WHERE run_id=? AND state IN ('running','received') AND lease_until>? LIMIT 1""",
                (self.run_id, self.clock())).fetchone():
                raise ProviderError("A model request is still active", code="MODEL_REQUEST_IN_PROGRESS")
            if local_only and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_actions'").fetchone():
                if db.execute("""SELECT 1 FROM recovery_actions a
                    JOIN recovery_incidents i ON i.incident_id=a.incident_id
                    WHERE i.run_id=? AND a.state='running' AND a.external_intent=1
                      AND a.lease_until>? LIMIT 1""", (self.run_id, self.clock())).fetchone():
                    raise ProviderError("A recovery request is still active", code="MODEL_REQUEST_IN_PROGRESS")
            for spec in registry.stages:
                stage_material = material_digests.get(spec.stage_id, material_digest)
                identity = digest({"material": stage_material, "stage": spec.identity,
                                   "configuration": configurations.get(spec.stage_id, self.configuration_fingerprint)})
                prior = db.execute("SELECT * FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                                   (self.run_id, spec.stage_id)).fetchone()
                if self.budget_only and (not prior or prior["identity_digest"] != identity or spec.stage_id in invalid):
                    raise ProviderError("Budget approval does not authorize changed inputs", code="MODEL_BUDGET_RESUME_INPUT_CHANGED")
                generation = int(prior["generation"]) if prior else 1
                if prior and (identity != prior["identity_digest"] or spec.stage_id in invalid):
                    generation += 1
                if self.manual_authorization_id:
                    generation = prepare_grant(db, self.manual_authorization_id, self.run_id,
                        spec.stage_id, prior, self.configuration_fingerprint,
                        stage_material, generation, self.clock())
                db.execute("""INSERT INTO model_stage_request_heads VALUES(?,?,?,?)
                    ON CONFLICT(run_id,stage_id) DO UPDATE SET generation=excluded.generation,
                    identity_digest=excluded.identity_digest""", (self.run_id, spec.stage_id, generation, identity))
                generations[spec.stage_id] = generation
        return generations

    @contextmanager
    def bind(self, stage_id: str, generation: int):
        previous = getattr(self._local, "scope", None)
        self._local.scope = (stage_id, generation)
        try:
            yield
        finally:
            self._local.scope = previous

    def capture_scope(self):
        return getattr(self._local, "scope", None)

    def transport_context(self):
        return getattr(self._local, "transport", None)

    def ensure_automatic_time_available(self):
        if self._automatic_deadline is not None and time.monotonic() >= self._automatic_deadline:
            context = self.transport_context()
            if context is not None:
                from .call_budget import record_gap
                with self.store.connect() as db:
                    request = getattr(self._local, "pending_work", {})
                    record_gap(db, self.run_id, context, request.get("purpose", "generation"),
                        request.get("input_estimate", 0), request.get("output_limit", 1), "MODEL_RUN_TIME_BUDGET_EXHAUSTED")
            raise ProviderError("Automatic research time budget exhausted; retain completed work",
                code="MODEL_RUN_TIME_BUDGET_EXHAUSTED", not_sent=True)

    def note_preparation(self, queue_seconds, material_seconds):
        self._local.queue_seconds = queue_seconds
        self._local.material_seconds = material_seconds

    def note_queued_work(self, purpose, input_estimate, output_limit):
        # Queue/time exhaustion can precede wire preparation, including the
        # first call of this run. Do not attribute it to a stale sibling wire.
        self._local.pending_work = {"purpose": purpose, "input_estimate": input_estimate,
            "output_limit": output_limit}

    def record_stage_timing(self, stage_id, timing):
        with self.store.connect() as db:
            row = db.execute("SELECT request_id,attempt_id,diagnostics_json FROM model_wire_diagnostics WHERE run_id=? AND stage_id=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                (self.run_id, stage_id, timing.get("queued_at_epoch", self.clock()))).fetchone()
            if row is None:
                return  # A local-only replay cannot rewrite old transport time.
            raw = json.loads(row["diagnostics_json"])
            raw["stage_timeline"] = timing
            raw["publication_seconds"] = timing.get("publication_seconds")
            raw["verification_seconds"] = timing.get("local_verification_seconds")
            db.execute("UPDATE model_wire_diagnostics SET diagnostics_json=? WHERE request_id=? AND attempt_id=?",
                (json.dumps(raw, allow_nan=False), row["request_id"], row["attempt_id"]))

    def record_wire_diagnostics(self, system_prompt, user_prompt, *, provider, purpose, target_paths=()):
        """Safe physical-attempt accounting; no prompt, key or reasoning body."""
        context = self.transport_context()
        if context is None:
            return
        sizes = {"system": len(system_prompt.encode("utf-8")), "user": len(user_prompt.encode("utf-8"))}
        components = {}
        prefix_metadata = {}
        try:
            raw = json.loads(user_prompt)
            if isinstance(raw, dict):
                for key in ("material_registry", "research_context", "prior_artifacts", "claim_contract",
                            "task_instructions", "assigned_research_obligations", "assertions", "material", "common_core", "source_package", "late_evidence"):
                    if key in raw:
                        components[key] = len(json.dumps(raw[key], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                # Stable byte/message boundaries, never prompts or raw source.
                prefix_keys = [key for key in raw if key not in {"agent", "task_instructions", "prior_artifacts",
                    "assigned_research_obligations", "agent_role", "output_schema", "late_evidence", "reading_assignment"}]
                frozen = json.dumps({key: raw[key] for key in prefix_keys}, ensure_ascii=False, separators=(",", ":"))
                tail = json.dumps({key: raw[key] for key in raw if key not in prefix_keys}, ensure_ascii=False, separators=(",", ":"))
                prefix_metadata = {"common_prefix_digest": digest(system_prompt), "source_package_digest": digest(frozen),
                    "task_tail_digest": digest(tail), "source_package_bytes": len(frozen.encode()),
                    "task_tail_bytes": len(tail.encode()), "message_boundary_version": "system-user-v1"}
                # The actual byte prefix ends at the top-level task boundary,
                # not at a reconstructed/sorted approximation of that JSON.
                boundary = user_prompt.find(',"agent":')
                if boundary >= 0:
                    prefix_metadata["wire_prefix_digest"] = digest(user_prompt[:boundary])
                    prefix_metadata["wire_prefix_bytes"] = len(user_prompt[:boundary].encode())
        except (ValueError, TypeError):
            pass
        total = sum(sizes.values())
        config = getattr(provider, "config", None)
        from .model import provider_context_capability
        capability = provider_context_capability(provider)
        from .input_budget import input_measurement
        measured = input_measurement(provider, system_prompt, user_prompt)
        diagnostic = {"schema": "model-wire-diagnostics-v1", "generation": context["generation"],
            "parent_request_id": context["request_id"].partition(":repair:")[0],
            "target_paths": list(target_paths),
            "trigger_reason": "targeted_gap_repair" if target_paths else purpose,
            "input_digest": digest({"system": system_prompt, "user": user_prompt}),
            "configured_model_id": getattr(config, "configured_model_id", ""),
            "configuration_version": getattr(config, "configuration_version", None),
            "endpoint_category": "configured_gateway" if config is not None else "injected_provider",
            "input_bytes": total, "message_bytes": sizes, "component_bytes": components,
            "input_tokens_estimate": measured["tokens"],
            "token_counting_method": measured["method"],
            "tokenizer": measured["tokenizer"],
            "context_tokens_hint": getattr(provider, "context_window_tokens", None),
            "reserved_output_tokens": capability.reserved_output_tokens,
            "reserved_repair_tokens": capability.reserved_repair_tokens,
            "queue_seconds": getattr(self._local, "queue_seconds", None),
            "material_preparation_seconds": getattr(self._local, "material_seconds", None),
            "sent_at_epoch": self.clock(),
            "wire_state": "send_intent_recorded", "usage": None, **prefix_metadata}
        self._local.wire_started = time.monotonic()
        self._local.pending_work = {"purpose": purpose, "input_estimate": measured["tokens"],
            "output_limit": capability.reserved_output_tokens}
        # Host-computed semantic work, independent of transport attempt IDs
        # and prompt wording. No source text enters the work-item ledger.
        try:
            semantic, _ = json.JSONDecoder().raw_decode(user_prompt[user_prompt.index("{"):])
        except (ValueError, TypeError):
            semantic = {}
        if not isinstance(semantic, dict):
            semantic = {}
        targets = list(target_paths) or semantic.get("target_fields") or (semantic.get("sectional_delivery") or {}).get("sections") or [
            item.get("assertion_id") for item in semantic.get("assertions", ()) if isinstance(item, dict)]
        material = {key: semantic[key] for key in ("research_context", "material_registry", "common_core",
            "source_package", "late_evidence", "sources", "repair_material", "material") if key in semantic}
        claims = [{"path": item.get("path"), "original_claim": item.get("original_claim")}
            for item in semantic.get("targets", ()) if isinstance(item, dict) and "original_claim" in item]
        def content_only(value):
            if isinstance(value, dict):
                return {key: content_only(child) for key, child in value.items()
                    if not key.startswith("_") and key not in {"created_at", "updated_at", "attempt_id"}}
            if isinstance(value, list):
                return [content_only(child) for child in value]
            return value
        work = {"obligation": digest(semantic.get("assigned_research_obligations", purpose)),
            "targets": targets or [context["stage_id"]],
            "source_version": digest(material) if material else digest(user_prompt),
            "completed_digest": digest(content_only(claims or semantic.get("prior_artifacts", {}))),
            "strategy": "source-bound:" + purpose}
        if self.manual_authorization_id:
            # Only the existing explicit retry grant can authorize repeating
            # unchanged semantic work. A timestamp/attempt ID cannot do so.
            work["strategy"] += ":manual:" + self.manual_authorization_id
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if self.budget_only and not self.adaptive_resume and not db.execute("SELECT 1 FROM model_budget_gaps WHERE run_id=? AND request_id=? AND state='consumed'",
                        (self.run_id, context["request_id"])).fetchone():
                    raise ProviderError("Only approved missing work may continue", code="MODEL_BUDGET_RESUME_SCOPE_CHANGED")
                from .call_budget import reserve
                reserve(db, self.run_id, {**context, **({"work_item": work} if purpose != "transport_retry" else {})}, purpose=purpose, input_estimate=measured["tokens"],
                        output_limit=capability.reserved_output_tokens,
                        route_identity=getattr(config, "public_id", None),
                        elapsed_seconds=(None if self._automatic_deadline is None else max(0,
                            self._automatic_limit_seconds - (self._automatic_deadline - time.monotonic()))),
                        trigger="targeted_repair" if purpose.endswith("repair") or purpose == "section_delivery" else purpose)
                db.execute("""INSERT INTO model_wire_diagnostics VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(request_id,attempt_id) DO NOTHING""",
                    (context["request_id"], context["attempt_id"], self.run_id, context["stage_id"], purpose,
                     json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, allow_nan=False), self.clock()))
        except ProviderError as error:
            from .call_budget import record_gap
            with self.store.connect() as db:
                record_gap(db, self.run_id, context, purpose, measured["tokens"], capability.reserved_output_tokens, error.code)
            raise

    def finish_wire_diagnostics(self, response=None, error=None):
        context = self.transport_context()
        if context is None:
            return
        metadata = response.receipt.get("metadata", {}) if isinstance(response, ModelResponse) else {}
        with self.store.connect() as db:
            row = db.execute("SELECT diagnostics_json,created_at FROM model_wire_diagnostics WHERE request_id=? AND attempt_id=?",
                             (context["request_id"], context["attempt_id"])).fetchone()
            if row is None:
                return
            diagnostic = json.loads(row["diagnostics_json"])
            code = getattr(error, "code", "")
            status = getattr(error, "http_status", None) if error is not None else metadata.get("http_status")
            elapsed = max(0, time.monotonic() - getattr(self._local, "wire_started", time.monotonic()))
            observation = next((item for item in self.store.model_activity.snapshot(self.run_id)
                if item["request_id"] == context["request_id"] and item["attempt_id"] == context["attempt_id"]), {})
            queue = diagnostic.get("queue_seconds") or 0
            material = diagnostic.get("material_preparation_seconds") or 0
            for name in ("first_response_seconds", "first_content_seconds"):
                offset = observation.get(name)
                diagnostic[name] = max(0, offset - queue - material) if type(offset) in (int, float) else None
            diagnostic.update({"elapsed_seconds": elapsed, "last_response_seconds": elapsed if response is not None else None,
                "finished_at_epoch": self.clock(),
                "wire_state": "response_received" if error is None else
                "not_sent" if getattr(self._local, "dispatch_started", False) is False or getattr(error,"not_sent",False) or code in {"MODEL_CONFIGURATION_CHANGED", "MODEL_OUTPUT_BUDGET_POLICY_REQUIRED", "MODEL_CONNECTION_AUTH_PAUSED"} else
                "rejected" if status in {400, 401, 403, 404, 413, 415, 422, 429}
                or code in {"MODEL_CONTEXT_CAPACITY", "MODEL_REQUEST_BODY_TOO_LARGE", "MODEL_CALL_CANCELLED_BEFORE_SEND"}
                else "local_processing_failed" if getattr(error, "response_received", False) else "remote_unknown",
                "error_code": code if isinstance(code, str) and re.fullmatch(r"MODEL_[A-Z0-9_]{1,80}", code) else "",
                "http_status": status if type(status) is int else None,
                "usage": {key: metadata.get(key) if type(metadata.get(key)) is int and metadata[key] >= 0 else None
                    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
                                "reasoning_tokens", "answer_tokens")}})
            actual_input = diagnostic["usage"]["input_tokens"]
            budget = metadata.get("output_budget_tokens")
            diagnostic["applied_output_budget_tokens"] = budget if type(budget) is int and budget > 0 else None
            if actual_input:
                diagnostic["observed_bytes_per_input_token"] = diagnostic["input_bytes"] / actual_input
            for key in ("model_id", "configured_model_id", "provider_id", "endpoint_host", "finish_reason",
                        "output_budget_wire_field", "output_budget_semantics", "usage_source", "usage_contract", "transport_observation", "provider_request_id"):
                if isinstance(metadata.get(key), str):
                    diagnostic[key] = metadata[key][:256]
            diagnostic["reasoning_in_output"] = metadata.get("reasoning_in_output") if type(metadata.get("reasoning_in_output")) is bool else None
            db.execute("UPDATE model_wire_diagnostics SET diagnostics_json=? WHERE request_id=? AND attempt_id=?",
                       (json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, allow_nan=False),
                        context["request_id"], context["attempt_id"]))
            from .call_budget import settle
            settle(db, context, diagnostic["usage"], "not_sent" if code == "MODEL_CALL_CANCELLED_BEFORE_SEND"
                   else diagnostic["wire_state"], reasoning_in_output=diagnostic["reasoning_in_output"])

    def hydrate_input_budget(self, provider) -> None:
        """Restore only safe received usage of this exact model configuration."""
        observe = getattr(provider, "observe_input_usage", None)
        config = getattr(provider, "config", None)
        if not callable(observe) or config is None:
            return
        try:
            with self.store.connect() as db:
                rows = db.execute("SELECT diagnostics_json FROM model_wire_diagnostics ORDER BY created_at DESC LIMIT 128").fetchall()
            for row in reversed(rows):
                item = json.loads(row["diagnostics_json"])
                if (item.get("configured_model_id") != config.configured_model_id
                        or item.get("configuration_version") != config.configuration_version
                        or item.get("wire_state") != "response_received"):
                    continue
                usage = item.get("usage") or {}
                observe(input_bytes=item.get("input_bytes"), input_tokens=usage.get("input_tokens"),
                        provider_id=item.get("provider_id"), model_id=item.get("model_id"),
                        endpoint_host=item.get("endpoint_host"))
                observe_output = getattr(provider, "observe_output_usage", None)
                if callable(observe_output):
                    observe_output(output_tokens=usage.get("output_tokens"), reasoning_tokens=usage.get("reasoning_tokens"),
                        reasoning_in_output=item.get("reasoning_in_output"))
        except Exception:
            # Old/absent observations only remove an optimization, not results.
            return

    @contextmanager
    def transport_binding(self, request_id: str, scope, attempt_id: str, *, before_dispatch=None):
        context = {"protocol": "model-events-v1", "request_id": request_id, "run_id": self.run_id,
                   "stage_id": scope[0], "generation": scope[1], "attempt_id": attempt_id}
        previous = self.transport_context()
        previous_dispatch = getattr(self._local, "before_dispatch", None)
        previous_started = getattr(self._local, "dispatch_started", False)
        self._local.transport = context
        self._local.before_dispatch = before_dispatch
        self._local.dispatch_started = False
        self.store.model_activity.begin(context)
        try:
            yield
        except BaseException as error:
            code = getattr(error, "code", "")
            state = ("cancelled" if code in {"MODEL_CANCELLED", "MODEL_CALL_CANCELLED", "MODEL_CALL_CANCELLED_BEFORE_SEND"} or self.cancel_check() else
                     "rate_limited" if proven_rate_rejection(error) else
                     "rejected" if getattr(error,"not_sent",False) or getattr(error, "http_status", None) in {400, 401, 403, 413, 422}
                     or code in {"MODEL_CONTEXT_CAPACITY", "MODEL_REQUEST_BODY_TOO_LARGE", "MODEL_QUOTA_EXHAUSTED"} else
                     "local_failure" if getattr(error, "response_received", False) else "remote_unknown")
            self.store.model_activity.update(context, {"state": state})
            raise
        else:
            self.store.model_activity.update(context, {"state": "verifying"})
        finally:
            self._local.transport = previous
            self._local.before_dispatch = previous_dispatch
            self._local.dispatch_started = previous_started

    def before_dispatch(self):
        callback = getattr(self._local, "before_dispatch", None)
        if callback is not None:
            callback()
            self._local.before_dispatch = None
        self._local.dispatch_started = True

    def request_observation(self, request, stage_id):
        """Read persisted transport evidence, never infer submission from text."""
        with self.store.connect() as db:
            row = db.execute("""SELECT request_id,state,response_received,diagnostic_code FROM model_request_journal
                WHERE run_id=? AND stage_id=? AND input_digest=? AND configuration_fingerprint=?
                ORDER BY updated_at DESC LIMIT 1""", (self.run_id, stage_id, request.input_digest,
                self.stage_configuration(stage_id))).fetchone()
            if row is not None:
                from .call_budget import BUDGET_GAP_CODES
                if (row["state"] == "rejected" and row["diagnostic_code"] in BUDGET_GAP_CODES and not row["response_received"]
                        and not db.execute("SELECT 1 FROM model_wire_diagnostics WHERE request_id=? LIMIT 1", (row["request_id"],)).fetchone()):
                    return {"submission_state": "not_submitted", "journal_state": "budget_blocked"}
        if row is None:
            return {"submission_state": "not_submitted", "journal_state": "absent"}
        state = ("received" if row["response_received"] else "rejected" if row["state"] == "rejected"
                 else "remote_unknown")
        return {"submission_state": state, "journal_state": row["state"],
                "journal_diagnostic_code": row["diagnostic_code"]}

    def saved_candidate(self, request, stage_id: str) -> dict | None:
        """Recover an exact received body without starting a remote intent.

        The content is still only a candidate. Semantic/business verification
        must run again against the current sources. A late body receives a new
        local owner lease; no head advancement, wire retry, billing grant or
        unknown-request clearance is performed.
        """
        configuration = self._stage_configurations.get(stage_id, self.configuration_fingerprint)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT * FROM model_request_journal WHERE run_id=?
                AND stage_id=? AND input_digest=? AND configuration_fingerprint=?
                AND state IN ('candidate','late_candidate') AND response_received=1 ORDER BY updated_at DESC""",
                (self.run_id, stage_id, request.input_digest, configuration)).fetchall()
            for row in rows:
                if row["state"] == "late_candidate" and not self._recover_received(db, row):
                    continue
                return deepcopy(self._read_candidate(row))
        return None

    def _recover_received(self, db, row) -> bool:
        """Adopt a saved body under a new local lease, never the old worker.

        Frozen head binds source snapshot and stage/prompt policy. A cancelled
        or replaced generation cannot borrow this response. Digest/type checks
        precede mutation; a body is still an unverified candidate afterwards.
        """
        head = db.execute("SELECT generation FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                          (self.run_id, row["stage_id"])).fetchone()
        if (not head or int(head["generation"]) != int(row["generation"])
                or self.cancel_check() or self._persisted_cancelled(db)
                or not row["response_received"] or not row["candidate_json"]):
            return False
        self._read_candidate(row)
        recovered = db.execute("""UPDATE model_request_journal SET state='candidate',owner_id=?,
            lease_until=?,updated_at=?,diagnostic_code='MODEL_LATE_CANDIDATE_LOCAL_RECOVERED'
            WHERE request_id=? AND owner_id=? AND state='late_candidate' AND candidate_digest=?""",
            (uuid.uuid4().hex, self.clock() + 15, self.clock(), row["request_id"], row["owner_id"], row["candidate_digest"]))
        return recovered.rowcount == 1

    def stage_configuration(self, stage_id: str) -> str:
        return self._stage_configurations.get(stage_id, self.configuration_fingerprint)

    def saved_generation_candidates(self) -> dict[str, dict]:
        """Bodies received at the current head, before any head advancement.

        Auxiliary verification/repair responses are never mistaken for research
        prose. Legacy rows without an explicit purpose need exact-request replay
        or a formal checkpoint; their purpose is not guessed from JSON shape.
        """
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT j.* FROM model_request_journal j
                JOIN model_stage_request_heads h ON h.run_id=j.run_id AND h.stage_id=j.stage_id
                AND h.generation=j.generation WHERE j.run_id=? AND j.purpose='generation'
                AND j.state IN ('candidate','late_candidate') AND j.response_received=1 ORDER BY j.updated_at DESC""", (self.run_id,)).fetchall()
            candidates = {}
            for row in rows:
                if row["stage_id"] in candidates:
                    continue
                try:
                    if row["state"] == "late_candidate" and not self._recover_received(db, row):
                        continue
                    candidates[row["stage_id"]] = {"candidate": deepcopy(self._read_candidate(row)),
                        "input_digest": row["input_digest"], "configuration_fingerprint": row["configuration_fingerprint"]}
                except (ValueError, TypeError, ProviderError) as exc:
                    # Corruption is isolated to this stage, including adoption;
                    # do not substitute an older response for the same head.
                    if isinstance(exc, ProviderError) and exc.code != "MODEL_REQUEST_CANDIDATE_CORRUPT":
                        raise
                    self.candidate_read_issues.append({"code": "MODEL_REQUEST_CANDIDATE_CORRUPT",
                        "stage_id": row["stage_id"], "path": "model_request_journal." + row["request_id"] + ".candidate"})
                    candidates[row["stage_id"]] = None
        return {key: value for key, value in candidates.items() if value is not None}

    def execute(self, request, invoke: Callable[[], dict], scope: tuple[str, int] | None) -> dict:
        if scope is None:
            raise ProviderError("Model request has no frozen stage scope", code="MODEL_REQUEST_SCOPE_REQUIRED")
        stage_id, generation = scope
        configuration = self._stage_configurations.get(stage_id, self.configuration_fingerprint)
        identity = {"run": self.run_id, "stage": stage_id, "generation": generation,
                    "input": request.input_digest, "configuration": configuration}
        request_id, owner, now = "request:" + digest(identity), uuid.uuid4().hex, self.clock()
        if self.cancel_check():
            raise ProviderError("Research cancelled", code="MODEL_CANCELLED")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_generation(db, stage_id, generation)
            saved = db.execute("SELECT * FROM model_request_journal WHERE request_id=?", (request_id,)).fetchone()
            from .call_budget import BUDGET_GAP_CODES
            budget_resume = bool(self.budget_only and saved and saved["state"] == "rejected"
                and not saved["response_received"] and saved["diagnostic_code"] in BUDGET_GAP_CODES
                and not db.execute("SELECT 1 FROM model_wire_diagnostics WHERE request_id=? LIMIT 1", (request_id,)).fetchone()
                and (self.adaptive_resume or db.execute("SELECT 1 FROM model_budget_gaps WHERE run_id=? AND request_id=? AND state='approved'",
                    (self.run_id, request_id)).fetchone()))
            if saved and not budget_resume:
                if saved["state"] == "candidate" or saved["state"] == "late_candidate" and self._recover_received(db, saved):
                    candidate = self._read_candidate(saved)
                    return deepcopy(candidate)
                # A provider rejection is known, but retrying it is a separate
                # authorized repair action. Unknown submission is never retried.
                code = ("MODEL_REQUEST_IN_PROGRESS" if saved["state"] == "running" and saved["lease_until"] > now
                        else saved["diagnostic_code"] if saved["state"] == "local_processing_failed"
                        else saved["diagnostic_code"] if saved["state"] == "rejected"
                        else "MODEL_RESPONSE_LOCAL_PROCESSING_INTERRUPTED" if saved["response_received"]
                        else "MODEL_SUBMISSION_UNKNOWN")
                status = self._format_rejection_status(saved) if code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED" else None
                if code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED" and status is None:
                    code = "MODEL_SUBMISSION_UNKNOWN"
                rate_proof = self._transient_rejection_proof(saved)
                if rate_proof is not None:
                    status = rate_proof["http_status"]
                raise ProviderError("Saved request requires recovery", code=code, retryable=False,
                    http_status=status, retry_after_seconds=rate_proof["retry_after_seconds"] if rate_proof else None)
            if self.budget_only and not self.adaptive_resume and not budget_resume:
                raise ProviderError("Only approved missing work may continue", code="MODEL_BUDGET_RESUME_SCOPE_CHANGED")
            # Changing model configuration cannot erase an unknown compensated
            # wire for this same physical prompt/stage. This read precedes I/O.
            supersedes_requests, supersedes_repairs = consume_grant(db,
                self.manual_authorization_id if getattr(request, "purpose", "generation") in {
                    "generation", "material_coverage", "section_delivery"} else "",
                self.run_id, stage_id, generation, self.configuration_fingerprint,
                request.input_digest, request_id, now)
            self.check_unknown_submission(db, stage_id, request.input_digest, now,
                                          excluded_requests=supersedes_requests)
            check_unknown_format_submission(self.store, self.run_id, stage_id, request.input_digest, now,
                                            excluded_actions=supersedes_repairs)
            if not self.manual_authorization_id:
                # Host audit/parser bookkeeping can change a graph generation
                # without changing a single physical prompt byte. Reuse only
                # the exact same configured wire, after all unknown guards.
                # This candidate still enters current source/business checks.
                prior = db.execute("""SELECT * FROM model_request_journal WHERE run_id=? AND stage_id=?
                    AND input_digest=? AND configuration_fingerprint=? AND state='candidate'
                    AND response_received=1 ORDER BY updated_at DESC LIMIT 1""",
                    (self.run_id, stage_id, request.input_digest, configuration)).fetchone()
                if prior is not None:
                    candidate = deepcopy(self._read_candidate(prior))
                    db.execute("INSERT OR IGNORE INTO model_result_reuse VALUES(?,?,?,?,?)",
                        (self.run_id, stage_id, generation, prior["request_id"], request.input_digest))
                    return candidate
            if budget_resume:
                # No wire was sent. Keep the original denial in the gap audit;
                # continue the same logical identity, not a new generation.
                db.execute("UPDATE model_budget_gaps SET state='consumed' WHERE run_id=? AND request_id=? AND state='approved'",
                    (self.run_id, request_id))
                db.execute("UPDATE model_request_journal SET state='running',owner_id=?,lease_until=?,diagnostic_code='',updated_at=? WHERE request_id=?",
                    (owner, now + max(1, self.timeout_seconds) + 15, now, request_id))
            else:
                db.execute("""INSERT INTO model_request_journal
                (request_id,run_id,stage_id,generation,input_digest,configuration_fingerprint,
                 state,owner_id,lease_until,candidate_json,candidate_digest,diagnostic_code,created_at,updated_at,purpose)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (request_id, self.run_id, stage_id, generation, request.input_digest,
                        configuration, "running", owner,
                        now + max(1, self.timeout_seconds) + 15, None, None, "", now, now,
                        getattr(request, "purpose", "generation")))
            if supersedes_requests or supersedes_repairs:
                db.execute("UPDATE model_request_journal SET authorization_id=?,supersedes_json=? WHERE request_id=?",
                    (self.manual_authorization_id, candidate_json({"requests": supersedes_requests,
                       "repairs": supersedes_repairs}), request_id))
        # The transaction above is committed before the potentially paid call.
        try:
            with self.transport_binding(request_id, scope, owner):
                result = invoke()
        except BaseException as exc:
            if getattr(exc, "response_received", False):
                self._record_local_failure(request_id, owner, exc, getattr(exc, "safe_receipt", None))
                raise
            code = getattr(exc, "code", "")
            format_rejection = proven_format_rejection(exc)
            confirmed_rejection = format_rejection or getattr(exc,"not_sent",False) or code.startswith(("MODEL_RUN_", "MODEL_BUDGET_", "MODEL_STAGE_GENERATION_BUDGET_")) or code in {
                "MODEL_RATE_LIMITED", "MODEL_HTTP_429", "MODEL_CONTEXT_CAPACITY",
                "MODEL_CONFIGURATION_ERROR", "MODEL_AUTHENTICATION_ERROR", "MODEL_UNAUTHORIZED", "MODEL_QUOTA_EXHAUSTED",
                "MODEL_CONFIGURATION_CHANGED", "MODEL_OUTPUT_BUDGET_POLICY_REQUIRED", "MODEL_CONNECTION_AUTH_PAUSED",
                "MODEL_HTTP_400", "MODEL_HTTP_401", "MODEL_HTTP_403",
                "MODEL_CALL_CANCELLED_BEFORE_SEND",
                "MODEL_REQUEST_BODY_TOO_LARGE",
            } or (isinstance(exc, ProviderError) and type(exc.http_status) is int
                  and exc.http_status in {400, 401, 403, 404, 405, 413, 415, 422, 429})
            proof = ({"schema": "model-format-rejection-v1", "code": code,
                      "http_status": exc.http_status} if format_rejection else
                     {"schema": "model-rate-rejection-v1", "code": code, "http_status": 429,
                      "retry_after_seconds": rejection_retry_after(exc)} if proven_rate_rejection(exc) else None)
            with self.store.connect() as db:
                receipt = self._store_partial(db, request_id, owner, getattr(exc, "partial_content", ""))
                db.execute("""UPDATE model_request_journal SET state=?,diagnostic_code=?,updated_at=?,
                    candidate_json=?,candidate_digest=?,receipt_json=COALESCE(?,receipt_json)
                    WHERE request_id=? AND owner_id=? AND state='running'""",
                           ("rejected" if confirmed_rejection else "unknown",
                            code if confirmed_rejection else "MODEL_SUBMISSION_UNKNOWN", self.clock(),
                            candidate_json(proof) if proof else None, digest(proof) if proof else None,
                            receipt, request_id, owner))
            raise
        # Receiving a response is a durable fact, independent of whether the
        # local Adapter/serializer/validator later succeeds. This commit occurs
        # before potentially fallible local processing; it never stores raw wire.
        receipt = deepcopy(result.receipt) if isinstance(result, ModelResponse) else {
            "schema": RESPONSE_SCHEMA, "response_received": True}
        trace_id = uuid.uuid4().hex
        try:
            receipt_encoded = candidate_json(receipt)
            with self.store.connect() as db:
                db.execute("""UPDATE model_request_journal SET response_received=1,
                    receipt_json=?,diagnostic_trace_id=?,updated_at=?
                    WHERE request_id=? AND owner_id=? AND state='running'""",
                    (receipt_encoded, trace_id, self.clock(), request_id, owner))
            record, receipt = response_record(result)
            from .reference_aliases import bind_response_record
            record, receipt = bind_response_record(record, receipt, getattr(request, "reference_bindings", ()), request.input_digest)
            encoded = candidate_document_json(record, max_bytes=MAX_RESPONSE_BYTES)
            candidate_digest = digest(record)
        except Exception as exc:
            code = self._record_local_failure(request_id, owner, exc, receipt, trace_id)
            raise ProviderError("Received model response needs local processing recovery",
                code=code, response_received=True, processing_phase="candidate_storage",
                content_path="$", diagnostic_trace_id=trace_id, safe_receipt=receipt) from exc
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Preserve late candidates for diagnostics but never promote one
            # whose request generation has been replaced or cancelled.
            current = db.execute("SELECT generation FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                                 (self.run_id, stage_id)).fetchone()
            cancelled = self.cancel_check() or self._persisted_cancelled(db)
            intent = db.execute("SELECT state,lease_until FROM model_request_journal WHERE request_id=? AND owner_id=?",
                                (request_id, owner)).fetchone()
            expired = intent is not None and intent["lease_until"] <= self.clock()
            active = (current and int(current["generation"]) == generation and not cancelled
                      and intent is not None and intent["state"] == "running" and not expired)
            # A replaced owner may not modify chunks belonging to an already
            # settled response, even if its late UPDATE would affect zero rows.
            if intent is not None and intent["state"] == "running":
                encoded = self._store_record(db, request_id, encoded, candidate_digest)
                updated = db.execute("""UPDATE model_request_journal SET state=?,candidate_json=?,candidate_digest=?,updated_at=?,
                    candidate_schema=?,receipt_json=?
                    WHERE request_id=? AND owner_id=? AND state='running'""",
                           ("candidate" if active else "late_candidate", encoded, candidate_digest, self.clock(),
                            RESPONSE_SCHEMA if record.get("schema") == RESPONSE_SCHEMA else "legacy",
                            candidate_json(receipt), request_id, owner))
            else:
                updated = None
            if updated is None or updated.rowcount != 1:
                active = False
        if not active:
            self.store.model_activity.update({"request_id": request_id, "attempt_id": owner},
                {"state": "cancelled" if cancelled else "local_failure"})
            raise ProviderError("Model response is no longer current",
                                code="MODEL_CANCELLED" if cancelled else "MODEL_REQUEST_EXPIRED"
                                if expired else "MODEL_REQUEST_GENERATION_STALE")
        self.store.model_activity.update({"request_id": request_id, "attempt_id": owner}, {"state": "saved"})
        return restore_response(record)

    @staticmethod
    def _store_partial(db, request_id, owner, text):
        """Retain incomplete text without satisfying the unknown-wire guard."""
        if not isinstance(text, str) or not text:
            return None
        from .responses import redact_response_text
        safe, _issues = redact_response_text(text)
        if len(safe.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return None
        active = db.execute("SELECT 1 FROM model_request_journal WHERE request_id=? AND owner_id=? AND state='running'",
                            (request_id, owner)).fetchone()
        if not active:
            return None
        chunks = [safe[offset:offset + 64 * 1024] for offset in range(0, len(safe), 64 * 1024)]
        db.execute("DELETE FROM model_response_chunks WHERE request_id=?", (request_id,))
        db.executemany("INSERT INTO model_response_chunks VALUES(?,?,?,?)",
            ((request_id, index, value, digest(value)) for index, value in enumerate(chunks)))
        return candidate_json({"schema": "openthesis.model-partial.v1", "response_received": False,
            "incomplete": True, "partial_bytes": len(safe.encode("utf-8")),
            "partial_digest": digest(safe), "chunks": len(chunks)})

    def _record_local_failure(self, request_id, owner, error, receipt=None, trace_id="") -> str:
        known = {"MODEL_STAGE_CANDIDATE_INVALID", "MODEL_RESPONSE_ENVELOPE_INVALID",
                 "MODEL_RESPONSE_TOTAL_LIMIT", "RECOVERY_CANDIDATE_SECRET_FIELD",
                 "RECOVERY_CANDIDATE_TOO_LARGE", "MODEL_RESPONSE_IDENTITY_MISMATCH"}
        raw = getattr(error, "code", "") or str(error)
        code = raw if raw in known else "MODEL_RESPONSE_LOCAL_PROCESSING_FAILED"
        safe = {"schema": RESPONSE_SCHEMA, "response_received": True}
        # Only the normalized host-owned receipt may enter this column.
        if isinstance(receipt, dict):
            try:
                candidate_json(receipt)
                safe = receipt
            except (TypeError, ValueError, RecursionError):
                pass
        trace_id = trace_id or uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("""UPDATE model_request_journal SET state='local_processing_failed',
                diagnostic_code=?,response_received=1,receipt_json=?,diagnostic_trace_id=?,updated_at=?
                WHERE request_id=? AND owner_id=? AND state='running'""",
                (code, candidate_json(safe), trace_id, self.clock(), request_id, owner))
        if hasattr(error, "diagnostic_trace_id"):
            error.diagnostic_trace_id = trace_id
        self.store.model_activity.update({"request_id": request_id, "attempt_id": owner},
                                        {"state": "local_failure"})
        return code

    @staticmethod
    def _validate_large_record(record):
        candidate_document_json(record, max_bytes=MAX_RESPONSE_BYTES)

    @staticmethod
    def _store_record(db, request_id, encoded, record_digest):
        if len(encoded.encode("utf-8")) <= MAX_CANDIDATE_JSON_BYTES:
            return encoded
        # SQLite chunks share the request transaction and FK lifecycle. The
        # journal retains only the bounded manifest, never an unchecked blob.
        chunks = [encoded[offset:offset + 64 * 1024] for offset in range(0, len(encoded), 64 * 1024)]
        db.execute("DELETE FROM model_response_chunks WHERE request_id=?", (request_id,))
        db.executemany("INSERT INTO model_response_chunks VALUES(?,?,?,?)",
                      ((request_id, index, value, digest(value)) for index, value in enumerate(chunks)))
        return candidate_json({"schema": "openthesis.model-response-chunks.v1",
            "request_id": request_id, "count": len(chunks), "bytes": len(encoded.encode("utf-8")),
            "record_digest": record_digest})

    def recover_format(self, request, invoke: Callable[[], dict], scope) -> dict:
        if scope is None:
            raise ProviderError("Model request has no frozen stage scope", code="MODEL_REQUEST_SCOPE_REQUIRED")
        return ModelFormatRecovery(self).execute(request, invoke, scope)

    def recover_transient(self, request, invoke: Callable[[], dict], scope) -> dict:
        if scope is None:
            raise ProviderError("Model request has no frozen stage scope", code="MODEL_REQUEST_SCOPE_REQUIRED")
        return ModelFormatRecovery(self).execute(request, invoke, scope, transient=True)

    def recover_output(self, request, invoke: Callable[[], dict], scope) -> dict:
        if scope is None:
            raise ProviderError("Model request has no frozen stage scope", code="MODEL_REQUEST_SCOPE_REQUIRED")
        return ModelFormatRecovery(self).execute(request, invoke, scope, output_repair=True)

    def recover_fields(self, request, invoke: Callable[[], dict], scope) -> dict:
        if scope is None:
            raise ProviderError("Model request has no frozen stage scope", code="MODEL_REQUEST_SCOPE_REQUIRED")
        return ModelFormatRecovery(self).execute(request, invoke, scope, field_repair=True)

    def recover_claims(self, request, invoke: Callable[[], dict], scope) -> dict:
        if scope is None:
            raise ProviderError("Model request has no frozen stage scope", code="MODEL_REQUEST_SCOPE_REQUIRED")
        return ModelFormatRecovery(self).execute(request, invoke, scope, claim_repair=True)

    def check_unknown_submission(self, db, stage_id: str, input_digest: str, now: float, *,
                                 excluded_requests: tuple[str, ...] = ()) -> None:
        """Check the physical required wire, not its replaceable configuration.

        Execute holds BEGIN IMMEDIATE while checking and inserting a new intent,
        so different model configurations cannot race two identical submissions.
        A persisted returned candidate is known; a running/unknown intent without
        one stays unresolved even after its lease or stage generation changes.
        """
        rows = db.execute("""SELECT request_id,state,lease_until FROM model_request_journal
            WHERE run_id=? AND stage_id=? AND input_digest=?
              AND state IN ('running','unknown') AND candidate_json IS NULL
              AND response_received=0
            ORDER BY CASE WHEN state='running' AND lease_until>? THEN 0 ELSE 1 END,
                     updated_at DESC""", (self.run_id, stage_id, input_digest, now)).fetchall()
        unresolved = next((row for row in rows if row["request_id"] not in excluded_requests), None)
        if unresolved:
            code = ("MODEL_REQUEST_IN_PROGRESS" if unresolved["state"] == "running"
                    and unresolved["lease_until"] > now else "MODEL_SUBMISSION_UNKNOWN")
            raise ProviderError("Saved model submission is unresolved", code=code, retryable=False)
        received = db.execute("""SELECT state,lease_until FROM model_request_journal
            WHERE run_id=? AND stage_id=? AND input_digest=? AND response_received=1
              AND state IN ('running','local_processing_failed') AND candidate_json IS NULL
            ORDER BY updated_at DESC LIMIT 1""", (self.run_id, stage_id, input_digest)).fetchone()
        if received:
            code = ("MODEL_REQUEST_IN_PROGRESS" if received["state"] == "running"
                    and received["lease_until"] > now else "MODEL_RESPONSE_LOCAL_PROCESSING_INTERRUPTED")
            raise ProviderError("Received response requires local recovery", code=code,
                                response_received=True, processing_phase="candidate_storage")

    def _transient_rejection_proof(self, saved) -> dict | None:
        try:
            proof = self._read_candidate(saved)
            if (set(proof) == {"schema", "code", "http_status", "retry_after_seconds"}
                    and proof["schema"] == "model-rate-rejection-v1"
                    and proof["code"] == saved["diagnostic_code"]
                    and proof["code"] in {"MODEL_RATE_LIMITED", "MODEL_HTTP_429"}
                    and type(proof["http_status"]) is int and proof["http_status"] == 429
                    and not isinstance(proof["retry_after_seconds"], bool)
                    and isinstance(proof["retry_after_seconds"], (int, float))
                    and math.isfinite(proof["retry_after_seconds"]) and proof["retry_after_seconds"] >= 0):
                return proof
        except (TypeError, ValueError, KeyError, RecursionError, ProviderError):
            pass
        return None

    def _format_rejection_status(self, saved) -> int | None:
        try:
            proof = self._read_candidate(saved)
            if (isinstance(proof, dict) and set(proof) == {"schema", "code", "http_status"}
                    and proof["schema"] == "model-format-rejection-v1"
                    and proof["code"] == saved["diagnostic_code"] == "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
                    and proof["http_status"] in {400, 422} and not isinstance(proof["http_status"], bool)
                    and digest(proof) == saved["candidate_digest"]):
                return proof["http_status"]
        except (TypeError, ValueError, RecursionError, ProviderError):
            pass
        return None

    def _read_candidate(self, saved) -> dict:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result

        def reject_constant(_value):
            raise ValueError("non_finite_json_number")

        try:
            candidate = json.loads(saved["candidate_json"], object_pairs_hook=unique_object,
                                   parse_constant=reject_constant)
            candidate_json(candidate)
            if isinstance(candidate, dict) and candidate.get("schema") == "openthesis.model-response-chunks.v1":
                if (candidate.get("request_id") != saved["request_id"] or type(candidate.get("count")) is not int
                        or not 1 <= candidate["count"] <= 256 or type(candidate.get("bytes")) is not int
                        or not 0 < candidate["bytes"] <= MAX_RESPONSE_BYTES
                        or candidate.get("record_digest") != saved["candidate_digest"]):
                    raise ValueError("candidate_chunk_manifest_invalid")
                with self.store.connect() as db:
                    chunks = db.execute("SELECT * FROM model_response_chunks WHERE request_id=? ORDER BY ordinal",
                                        (saved["request_id"],)).fetchall()
                if (len(chunks) != candidate["count"] or any(row["ordinal"] != index
                        or digest(row["content"]) != row["content_digest"] for index, row in enumerate(chunks))):
                    raise ValueError("candidate_chunk_corrupt")
                encoded = "".join(row["content"] for row in chunks)
                if len(encoded.encode("utf-8")) != candidate["bytes"]:
                    raise ValueError("candidate_chunk_size_changed")
                candidate = json.loads(encoded, object_pairs_hook=unique_object, parse_constant=reject_constant)
                self._validate_large_record(candidate)
            if not isinstance(candidate, dict) or digest(candidate) != saved["candidate_digest"]:
                raise ValueError("candidate_digest_mismatch")
            response = restore_response(candidate) if saved["candidate_schema"] == RESPONSE_SCHEMA else candidate
            from .reference_aliases import restore_saved_references
            return restore_saved_references(response, saved["input_digest"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise ProviderError("Stored model candidate failed integrity",
                                code="MODEL_REQUEST_CANDIDATE_CORRUPT") from exc

    def _check_generation(self, db, stage_id: str, generation: int) -> None:
        if self._persisted_cancelled(db):
            raise ProviderError("Research cancelled", code="MODEL_CANCELLED", retryable=False)
        row = db.execute("SELECT generation FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                         (self.run_id, stage_id)).fetchone()
        if not row or int(row["generation"]) != generation:
            raise ProviderError("Model request generation changed", code="MODEL_REQUEST_GENERATION_STALE")

    def _persisted_cancelled(self, db) -> bool:
        row = db.execute("SELECT status FROM research_runs WHERE run_id=?", (self.run_id,)).fetchone()
        return bool(row and row["status"] == "cancelled")

    def is_cancelled(self) -> bool:
        if self.cancel_check():
            return True
        with self.store.connect() as db:
            return self._persisted_cancelled(db)
