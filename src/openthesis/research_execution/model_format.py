"""Durable text-response recovery, not a second hidden retry loop.

The original required request and the additional wire have separate journals.
Format compensation, confirmed 429 retries and returned-output repair use one
reservation/intent/validation/publication path and shared incident budget.
Their successful result is still only an unverified stage
candidate; research business validation remains with the stage executor.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import math
from typing import Any, Callable

from ..providers import ProviderError
from ..resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryPlan, RecoveryState,
    Usage, VerificationResult, candidate_json, candidate_document_json, digest,
)
from ..resilience.coordinator import RecoveryCoordinator
from ..resilience.repository import RecoveryConflict, RecoveryRepository
from ..resilience.strategies import CandidateResult, RecoveryError, Strategy, StrategyRegistry
from .model_output import (
    FIELD_REPAIR_VERSION, OUTPUT_REPAIR_VERSION, merge_field_targets,
    repairable_fields, repairable_output, repaired_output_valid, normalize_stage_output,
)
from .responses import (ModelResponse, RESPONSE_SCHEMA, MAX_RESPONSE_BYTES,
                        redact_response_text, response_record, restore_response)


def proven_format_rejection(error: Any) -> bool:
    return (isinstance(error, ProviderError)
            and error.code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
            and error.http_status in {400, 422})


def proven_rate_rejection(error: Any) -> bool:
    return (isinstance(error, ProviderError)
            and error.code in {"MODEL_RATE_LIMITED", "MODEL_HTTP_429"}
            and type(error.http_status) is int and error.http_status == 429)


def rejection_retry_after(error: Any) -> float:
    value = error.retry_after_seconds
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return 30.0
    # Do not shorten a server's minimum; the incident may instead exhaust.
    return float(value)


def format_target(stage_id: str, input_digest: str) -> str:
    # Keep the durable anchor. All repairs of this same physical request share
    # this physical request's one budget and unknown-submission guard.
    return "model-format:" + digest({"stage": stage_id, "input": input_digest})


def check_unknown_format_submission(store, run_id, stage_id, input_digest, now, *, excluded_actions=()):
    """An unknown wire remains unknown across configuration and generation changes."""
    with store.connect() as db:
        rows = db.execute("""SELECT a.action_id,a.state,a.lease_until FROM recovery_actions a
            JOIN recovery_plans p ON p.plan_id=a.plan_id
            JOIN recovery_incidents i ON i.incident_id=a.incident_id
            WHERE i.run_id=? AND p.payload_json IS NOT NULL AND (i.target=? OR i.target LIKE ?)
              AND a.external_intent=1 AND a.external_task_id='' AND a.result_json IS NULL
              AND a.response_received=0
            ORDER BY a.updated_at DESC""",
            (run_id, format_target(stage_id, input_digest), format_target(stage_id, input_digest) + ':work:%')).fetchall()
    row = next((row for row in rows if row["action_id"] not in excluded_actions), None)
    if row:
        code = ("MODEL_REQUEST_IN_PROGRESS" if row["state"] == RecoveryState.RUNNING
                and row["lease_until"] and row["lease_until"] > now
                else "MODEL_SUBMISSION_UNKNOWN")
        raise ProviderError(code, code=code)


class ModelFormatRecovery:
    def __init__(self, journal):
        self.journal = journal

    def execute(self, request, invoke: Callable[[], dict], scope, *, transient: bool = False,
                output_repair: bool = False, field_repair: bool = False, claim_repair: bool = False) -> dict:
        if sum((transient, output_repair, field_repair, claim_repair)) > 1:
            raise ValueError("MODEL_RECOVERY_MODE_INVALID")
        candidate_repair = output_repair or field_repair or claim_repair
        journal = self.journal
        stage_id, generation = scope
        if journal.is_cancelled():
            raise ProviderError("Research cancelled", code="MODEL_CANCELLED")
        check_unknown_format_submission(journal.store, journal.run_id, stage_id,
                                        request.input_digest, journal.clock())
        with journal.store.connect() as db:
            journal.check_unknown_submission(db, stage_id, request.input_digest, journal.clock())
            journal._check_generation(db, stage_id, generation)
            original = db.execute("""SELECT * FROM model_request_journal
                WHERE run_id=? AND stage_id=? AND generation=? AND input_digest=?
                  AND configuration_fingerprint=?""", (journal.run_id, stage_id, generation,
                request.input_digest, journal.configuration_fingerprint)).fetchone()
        proof = (journal._read_candidate(original) if candidate_repair and original is not None
                 and original["state"] == "candidate" else
                 journal._transient_rejection_proof(original) if transient and original is not None
                 else journal._format_rejection_status(original) if original is not None else None)
        if isinstance(proof, dict):
            proof = request.decode_candidate(proof)
            proof = normalize_stage_output(proof, request.output_contract)
        fields = repairable_fields(proof, request.output_contract) if field_repair else ()
        claim_contract = request.claim_repair_contract if claim_repair else None
        claim_plan = claim_contract.plan(proof) if claim_contract is not None and isinstance(proof, dict) else None
        expected_state = "candidate" if candidate_repair else "rejected"
        if (original is None or original["state"] != expected_state or proof is None
                or output_repair and not repairable_output(proof) or field_repair and not fields
                or claim_repair and claim_plan is None):
            code = ("MODEL_CLAIM_REPAIR_UNPROVEN" if claim_repair else "MODEL_FIELD_REPAIR_UNPROVEN" if field_repair else "MODEL_OUTPUT_REPAIR_UNPROVEN" if output_repair else
                    "MODEL_TRANSIENT_REJECTION_UNPROVEN" if transient else "MODEL_FORMAT_REJECTION_UNPROVEN")
            raise ProviderError("A durable unusable response or confirmed rejection is required", code=code)
        request_id = original["request_id"]
        configuration = digest({"configured_model": journal.configuration_fingerprint})
        provider_id = journal.configuration_fingerprint
        authorization = AuthorizationScope(providers=(provider_id,), configuration_fingerprints=(configuration,))
        target = format_target(stage_id, request.input_digest)
        issue = Issue("MODEL_CLAIM_FIELDS_INVALID" if claim_repair else "MODEL_OUTPUT_FIELDS_INVALID" if field_repair else
                      "MODEL_OUTPUT_PROTOCOL_INVALID" if output_repair else original["diagnostic_code"],
                      "model_transport" if transient else "model_protocol", target,
                      journal.run_id, request.input_digest, stage_id=stage_id)
        # Each independently frozen target owns an issue. Progress is not a
        # claim that the entire original repair request has been resolved.
        target_paths = (tuple(item.path for item in claim_plan.targets) if claim_repair else
                        fields if field_repair else ())
        issues_by_path = {path: replace(issue, affected_ids=(path,)) for path in target_paths}
        issues = tuple(issues_by_path.values()) or (issue,)
        verifier_id = ("model-claim-candidate-v2" if claim_repair else "model-field-candidate-v2" if field_repair else
                       "model-output-candidate-v1" if output_repair else "model-format-candidate-v1")
        contract_digest = digest(request.output_contract.to_dict()) if field_repair else ""
        preconditions = ((claim_plan.version, digest(proof), digest(claim_plan.to_dict())) if claim_repair else
                         (FIELD_REPAIR_VERSION, digest(proof), contract_digest, *fields) if field_repair else
                         (OUTPUT_REPAIR_VERSION, digest(proof)) if output_repair else ())
        if candidate_repair:
            # A bounded incident owns one concrete repair state, not all
            # future gaps in this stage. New target/content is new work; the
            # same state reuses its prior outcome without another charge.
            target += ":work:" + digest({"verifier": verifier_id, "preconditions": preconditions})
        action = RecoveryAction("model-text-claims" if claim_repair else "model-text-fields" if field_repair else "model-text-output" if output_repair else
            "model-text-rate" if transient else "model-text-format", "v1", (stage_id,), request.input_digest,
            verifier_id, configuration_fingerprint=configuration,
            preconditions=preconditions,
            estimated_usage=Usage(calls=1, seconds=min(600, journal.timeout_seconds + 20)),
            provider_id=provider_id, external_submission=True)
        plan = RecoveryPlan("incident:" + digest({"run": journal.run_id, "target": target}),
            journal.run_id, target, generation, request.input_digest, issues, (action,),
            limits=replace(journal.repair_limits, lease_seconds=max(
                journal.repair_limits.lease_seconds, journal.timeout_seconds + 25)),
            authorization_scope=authorization)

        def snapshot(db, _run_id):
            head = db.execute("""SELECT generation FROM model_stage_request_heads
                WHERE run_id=? AND stage_id=?""", (journal.run_id, stage_id)).fetchone()
            return int(head["generation"]) if head else 0, request.input_digest

        def check_cancelled(context):
            if journal.is_cancelled():
                raise RecoveryConflict("RECOVERY_CANCELLED")
            context.checkpoint()

        identity = {"schema": verifier_id, "request_id": request_id,
                    "generation": generation, "input_digest": request.input_digest,
                    "configuration_fingerprint": configuration}
        if candidate_repair:
            identity["original_digest"] = digest(proof)
        if field_repair:
            identity["contract_digest"] = contract_digest
            identity["repair_fields"] = list(fields)
        if claim_repair:
            identity["claim_repair_plan"] = claim_plan.to_dict()

        transport_contexts = []

        def produce(context):
            check_cancelled(context)
            saved = next(item for item in context.coordinator.status(plan.plan_id)["actions"]
                         if item["action_id"] == context.action_id)
            if transient:
                if saved["attempt"] == 1:
                    context.wait(proof["retry_after_seconds"])
                check_cancelled(context)
            def record_dispatch():
                context.consume(Usage(calls=1))
                context.record_external_intent()
            try:
                # A coordinator lease may span more than one proven 429 wire.
                # Wire attempts must still have distinct event identities.
                attempt_id = context.token + ":" + str(saved["attempt"])
                with journal.transport_binding(request_id + ":repair:" + context.action_id, scope, attempt_id,
                                               before_dispatch=record_dispatch):
                    transport_contexts.append(dict(journal.transport_context()))
                    context.coordinator.repository.model_call_observed(plan, context.action_id, context.token,
                        journal.clock(), {"response_received": False, "model_call": transport_contexts[-1]})
                    result = invoke()
            except TimeoutError as error:
                # Intent exists and the wire outcome is not observed. Never
                # turn a timeout into proof that the provider did no work.
                raise RecoveryError("MODEL_TIMEOUT", needs_action="query_provider_before_resubmission") from error
            except ProviderError as error:
                if error.partial_content:
                    safe, _issues = redact_response_text(error.partial_content)
                    if len(safe.encode("utf-8")) <= MAX_RESPONSE_BYTES:
                        context.coordinator.repository.external_partial(plan, context.action_id, context.token,
                            journal.clock(), {"schema": "openthesis.model-partial.v1", "response_received": False,
                                "incomplete": True, "partial_content": safe, "partial_digest": digest(safe),
                                "partial_bytes": len(safe.encode("utf-8")),
                                "model_call": transport_contexts[-1]})
                if error.response_received:
                    context.record_external_response(error.safe_receipt or {"response_received": True})
                if proven_format_rejection(error):
                    context.record_external_rejection(error.http_status, reason="response_format_unsupported")
                elif (error.http_status in {400, 401, 403, 404, 405, 413, 415, 422, 429}
                      and error.code in {"MODEL_AUTHENTICATION_ERROR", "MODEL_UNAUTHORIZED", "MODEL_QUOTA_EXHAUSTED", "MODEL_RATE_LIMITED",
                                         "MODEL_HTTP_ERROR", "MODEL_CONTEXT_CAPACITY", "MODEL_HTTP_401", "MODEL_HTTP_403", "MODEL_HTTP_429"}):
                    reason = "request_rejected" if error.http_status in {400, 404, 405, 413, 415, 422} else ""
                    context.record_external_rejection(error.http_status, reason=reason)
                known_rate = transient and proven_rate_rejection(error)
                if not context.external_intent:
                    context.coordinator.repository.model_call_observed(plan, context.action_id, context.token,
                        journal.clock(), {"response_received": False, "model_call": transport_contexts[-1],
                            "rejected_status": error.http_status, "diagnostic_code": error.code})
                raise RecoveryError(error.code, transient=known_rate,
                    retry_after=rejection_retry_after(error) if known_rate else 0,
                    needs_action="wait_for_provider_cooldown_or_select_alternative" if known_rate
                        else "select_compatible_model_or_review_saved_request") from error
            # The coordinator persists the exact returned candidate before
            # this independent protocol verifier or any research validator.
            # The repair lane crosses the same safe response Adapter as the
            # initial wire. A sibling credential cannot erase received content.
            call_receipt = deepcopy(result.receipt) if isinstance(result, ModelResponse) else {
                "schema": RESPONSE_SCHEMA, "response_received": True}
            call_receipt["model_call"] = transport_contexts[-1]
            context.record_external_response(call_receipt)
            if isinstance(result, ModelResponse):
                journal.store.model_activity.update(transport_contexts[-1],
                    {"state": "verifying", "usage": result.receipt.get("metadata", {})})
            try:
                record, _receipt = response_record(result)
            except (TypeError, ValueError, RecursionError) as error:
                raise RecoveryError("MODEL_RESPONSE_LOCAL_PROCESSING_FAILED",
                    needs_action="revalidate_saved_content_without_resubmission") from error
            result = restore_response(record)
            if isinstance(result, ModelResponse):
                # Keep the typed nullable receipt adjacent while the recovery
                # journal detaches this dict subclass into plain JSON. The
                # serializer strips this control mirror from answer content.
                result["_response_receipt"] = deepcopy(result.receipt)
            value = {**identity, "result": result}
            if candidate_repair:
                value["original_result"] = deepcopy(proof)
            return CandidateResult(value)

        def verify(candidate, _plan):
            if journal.is_cancelled():
                raise RecoveryConflict("RECOVERY_CANCELLED")
            value = candidate.payload
            result = request.decode_candidate(candidate.payload.get("result"))
            expected_keys = {*identity, "result", *(["original_result"] if candidate_repair else [])}
            valid = (digest({key: value.get(key) for key in identity}) == digest(identity)
                     and set(value) == expected_keys and isinstance(result, dict)
                     and (True if claim_repair or field_repair else
                          repaired_output_valid(result) if output_repair else
                          result.get("structured_output_valid") is not False))
            if candidate_repair:
                valid = valid and digest(value.get("original_result")) == identity["original_digest"]
            if valid:
                candidate_document_json(result)
            if valid and (claim_repair or field_repair):
                delta = target_merge(result)
                # The claim merger applies each complete claim target
                # atomically and uses the existing business verifier. Field
                # repair remains protocol-only, subject to stage validation.
                if delta.progress:
                    candidate_document_json(delta.candidate)
                    resolved = tuple(issues_by_path[path].issue_id for path in delta.accepted_patches)
                    remaining = tuple(issues_by_path[path].issue_id for path in delta.unresolved_targets)
                    return VerificationResult(not remaining, resolved, remaining,
                                              accepted_delta=delta.accepted_patches)
            if target_paths:
                return VerificationResult(False, remaining_issue_ids=tuple(item.issue_id for item in issues))
            return VerificationResult(valid, (issue.issue_id,) if valid else (),
                                      () if valid else (issue.issue_id,))

        def target_merge(result):
            result = request.decode_candidate(result)
            return (claim_contract.merge_targets(proof, result, claim_plan) if claim_repair else
                    merge_field_targets(proof, result, request.output_contract, fields))

        def publish(db, _plan, value):
            if journal.cancel_check() or journal._persisted_cancelled(db):
                raise RecoveryConflict("RECOVERY_CANCELLED")
            journal._check_generation(db, stage_id, generation)
            saved = db.execute("SELECT * FROM model_request_journal WHERE request_id=?", (request_id,)).fetchone()
            saved_proof = (journal._read_candidate(saved) if candidate_repair and saved is not None
                           and saved["state"] == "candidate" else
                           journal._transient_rejection_proof(saved) if transient and saved is not None
                           else journal._format_rejection_status(saved) if saved is not None else None)
            if isinstance(saved_proof, dict):
                saved_proof = normalize_stage_output(request.decode_candidate(saved_proof), request.output_contract)
            verified = verify(CandidateResult(value), plan)
            if (saved is None or saved["state"] != expected_state
                    or digest(saved_proof) != digest(proof)
                    or not (verified.passed or verified.accepted_delta)):
                raise RecoveryConflict("RECOVERY_MODEL_REQUEST_COMMIT_CONTRACT_INVALID")
            if claim_repair or field_repair:
                delta = target_merge(value["result"])
                result = delta.candidate
                key = "_claim_repair" if claim_repair else "_field_repair"
                if delta.unresolved_targets:
                    result[key] = {"state": "pending", "code": "MODEL_REPAIR_TARGETS_REMAIN",
                        "targets": list(delta.unresolved_targets),
                        "accepted_targets": list(delta.accepted_patches),
                        "ignored_extensions": list(delta.ignored_extensions),
                        "issues": list(delta.issues)}
                else:
                    result.pop(key, None)
                if isinstance(proof, ModelResponse):
                    result = ModelResponse(result, proof.receipt, proof.diagnostics)
            else:
                result = request.decode_candidate(value["result"])
            record, receipt = response_record(result)
            from .reference_aliases import bind_response_record
            record, receipt = bind_response_record(record, receipt, getattr(request, "reference_bindings", ()), request.input_digest)
            # The stored stage body may become a repaired answer, but that
            # never changes which physical wire incurred the original usage.
            # Preserve the first wire's receipt separately from the candidate's
            # latest response envelope. Repair wires have their own events.
            prior_receipt = json.loads(saved["receipt_json"] or "{}")
            stored_receipt = {**receipt,
                "physical_call_receipt": prior_receipt.get("physical_call_receipt", prior_receipt),
                "physical_call_state": prior_receipt.get("physical_call_state",
                    "rejected" if expected_state == "rejected" else "saved"),
                "physical_call_elapsed_seconds": prior_receipt.get("physical_call_elapsed_seconds",
                    max(0, saved["updated_at"] - saved["created_at"]))}
            encoded_record = candidate_document_json(record, max_bytes=16 * 1024 * 1024)
            encoded = journal._store_record(db, request_id, encoded_record, digest(record))
            changed = db.execute("""UPDATE model_request_journal SET state='candidate', candidate_json=?,
                candidate_digest=?, diagnostic_code='', updated_at=?,candidate_schema=?,
                response_received=1,receipt_json=? WHERE request_id=? AND state=?""",
                (encoded, digest(record), journal.clock(),
                 RESPONSE_SCHEMA if record.get("schema") == RESPONSE_SCHEMA else "legacy",
                 candidate_json(stored_receipt), request_id, expected_state))
            if changed.rowcount != 1:
                raise RecoveryConflict("RECOVERY_MODEL_REQUEST_COMMIT_CONFLICT")
            return {"revision_id": request_id}

        registry = StrategyRegistry()
        registry.register(Strategy(action.strategy_id, action.strategy_version, action.verifier_id,
            produce, verify, lambda _plan, _action: True))
        repository = RecoveryRepository(journal.store, snapshot, publish)

        def sleep_checked(seconds):
            remaining = seconds
            while remaining > 0:
                if journal.is_cancelled():
                    raise RecoveryConflict("RECOVERY_CANCELLED")
                chunk = min(1.0, remaining)
                journal.recovery_sleeper(chunk)
                remaining -= chunk
            if journal.is_cancelled():
                raise RecoveryConflict("RECOVERY_CANCELLED")

        try:
            outcome = RecoveryCoordinator(repository, registry, clock=journal.clock,
                sleeper=sleep_checked).execute(plan,
                expected_generation=generation, plan_hash=plan.plan_hash, authorization=authorization)
        except RecoveryConflict as error:
            code = ("MODEL_REQUEST_IN_PROGRESS" if str(error) == "RECOVERY_TARGET_BUSY"
                    else "MODEL_CANCELLED" if str(error) == "RECOVERY_CANCELLED"
                    else str(error))
            if transport_contexts:
                journal.store.model_activity.update(transport_contexts[-1],
                    {"state": "cancelled" if code == "MODEL_CANCELLED" else "local_failure"})
            raise ProviderError(code, code=code) from error
        if journal.is_cancelled():
            raise ProviderError("Research cancelled", code="MODEL_CANCELLED")
        if outcome.get("state") in {RecoveryState.COMMITTED, RecoveryState.PARTIAL_COMMITTED}:
            with journal.store.connect() as db:
                saved = db.execute("SELECT * FROM model_request_journal WHERE request_id=?", (request_id,)).fetchone()
            if transport_contexts:
                journal.store.model_activity.update(transport_contexts[-1], {"state": "saved"})
            return deepcopy(journal._read_candidate(saved))
        code = str(outcome.get("diagnostic_code") or
                   ("MODEL_CLAIM_REPAIR_UNAVAILABLE" if claim_repair else "MODEL_FIELD_REPAIR_UNAVAILABLE" if field_repair else
                    "MODEL_OUTPUT_REPAIR_UNAVAILABLE" if output_repair else "MODEL_FORMAT_RECOVERY_UNAVAILABLE"))
        if code == "RECOVERY_SUBMISSION_UNKNOWN":
            code = "MODEL_SUBMISSION_UNKNOWN"
        if candidate_repair and code == "RECOVERY_CANDIDATE_REJECTED":
            code = "MODEL_CLAIM_REPAIR_INVALID" if claim_repair else "MODEL_FIELD_REPAIR_INVALID" if field_repair else "MODEL_OUTPUT_REPAIR_INVALID"
        if transport_contexts:
            journal.store.model_activity.update(transport_contexts[-1],
                {"state": "remote_unknown" if code == "MODEL_SUBMISSION_UNKNOWN" else "local_failure"})
        raise ProviderError(code, code=code)
