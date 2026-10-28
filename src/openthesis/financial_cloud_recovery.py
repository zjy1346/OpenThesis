"""Authorized cloud recovery; candidates still cross the pure compiler.

The Adapter hides persisted budgets, query-only task continuation and local
revalidation behind the existing financial-source extract Interface. It never
changes providers, edits an original filing or promotes a partial dataset.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import time
import json
import uuid
from typing import Any, Callable

from .financial_compiler import FinancialFactCompiler
from .financial_contracts import SourceIdentity
from .resilience.cloud_repository import CloudCandidateRepository
from .resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryLimits, RecoveryPlan,
    RecoveryState, Usage, VerificationResult, digest, IncidentScope,
)
from .resilience.coordinator import RecoveryCoordinator
from .resilience.repository import RecoveryConflict
from .resilience.strategies import CandidateResult, RecoveryError, Strategy, StrategyRegistry
from .vision_financials import (
    MINERU_UPLOAD_PROTOCOL_VERSION, VisionExtractionResult, VisionAdapterError,
    _check_request, _serialize_vision_result, _deserialize_vision_result,
    replay_saved_table_sources, vision_task_key,
)


class _SharedRunCallContext:
    """Bridge physical page submissions to the existing run reservations.

    HTTP polling/PUTs still use the recovery incident's network budget; only
    an actual recognition submission consumes a run generation call. No new
    retry loop, ledger or trust boundary is introduced here.
    """
    def __init__(self, context, store, run_id, config, page):
        self.context, self.store, self.run_id, self.config = context, store, run_id, config
        self.page, self.current = page, None
        self.started = None

    def __getattr__(self, name):
        return getattr(self.context, name)

    def reserve_model_call(self, input_estimate=0, output_limit=0):
        from .research_execution.call_budget import prepare_external_plan, reserve, record_gap
        from .providers import ProviderError
        row = self.store.get_run(self.run_id)
        if row is None:
            return
        payload = json.loads(row["payload_json"])
        identity = {"request_id": "vision:" + digest([self.run_id, self.context.action_id]),
            "attempt_id": uuid.uuid4().hex, "stage_id": "financial-vision", "generation": 1}
        raw = {"schema": "model-wire-diagnostics-v1", "wire_state": "send_intent_recorded",
            "configured_model_id": self.config.configured_model_id, "provider_id": self.config.provider,
            "input_tokens_estimate": input_estimate, "reserved_output_tokens": output_limit,
            "token_counting_method": "vision_byte_planning_estimate_not_image_token_contract", "usage": None}
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                prepare_external_plan(db, self.run_id, payload.get("research_configuration", {}).get("run_envelope"))
                reserve(db, self.run_id, identity, purpose="vision_recognition", input_estimate=input_estimate,
                    output_limit=output_limit, trigger="failed_page_recognition")
                db.execute("INSERT INTO model_wire_diagnostics VALUES(?,?,?,?,?,?,?)", (identity["request_id"],
                    identity["attempt_id"], self.run_id, identity["stage_id"], "vision_recognition", json.dumps(raw), time.time()))
        except ProviderError as error:
            with self.store.connect() as db:
                record_gap(db, self.run_id, identity, "vision_recognition", input_estimate, output_limit, error.code)
            raise RecoveryError(error.code, needs_action="review_shared_run_budget_before_cloud_submission") from error
        self.current = identity
        self.started = time.monotonic()

    def settle_model_call(self, response=None, error=None):
        if self.current is None:
            return
        from .research_execution.call_budget import settle
        metadata = getattr(response, "receipt", {}).get("metadata", {})
        usage = {key: metadata.get(key) if type(metadata.get(key)) is int and metadata[key] >= 0 else None
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "reasoning_tokens", "answer_tokens")}
        with self.store.connect() as db:
            row = db.execute("SELECT diagnostics_json FROM model_wire_diagnostics WHERE request_id=? AND attempt_id=?",
                (self.current["request_id"], self.current["attempt_id"])).fetchone()
            raw = json.loads(row[0])
            status = getattr(error, "http_status", None)
            raw.update(wire_state="response_received" if response is not None else "not_sent" if getattr(error, "not_sent", False) else
                "rejected" if status in {400, 401, 403, 413, 422, 429} else "remote_unknown", usage=usage,
                reasoning_in_output=metadata.get("reasoning_in_output"))
            raw["elapsed_seconds"] = max(0, time.monotonic() - self.started) if self.started is not None else None
            if type(status) is int:
                raw["http_status"] = status
            for key in ("model_id", "provider_id", "endpoint_host", "usage_source", "provider_request_id", "http_status"):
                if key in metadata:
                    raw[key] = metadata[key]
            settle(db, self.current, usage, raw["wire_state"], reasoning_in_output=metadata.get("reasoning_in_output"))
            db.execute("UPDATE model_wire_diagnostics SET diagnostics_json=? WHERE request_id=? AND attempt_id=?",
                (json.dumps(raw), self.current["request_id"], self.current["attempt_id"]))
        self.current = None


class DurableCloudVisionAdapter:
    """One approved page is one recovery target, shared by start and resume."""

    def __init__(self, adapter: Any, store: Any, run_id: str, *,
                 compiler: FinancialFactCompiler | None = None,
                 clock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep,
                 authorization_check: Callable[[], bool] | None = None,
                 limits: RecoveryLimits | None = None,
                 budget_scope: IncidentScope | None = None,
                 automatic_budget_scope: bool = False):
        self.adapter, self.store, self.run_id = adapter, store, run_id
        self.compiler = compiler or FinancialFactCompiler()
        self.clock, self.sleeper, self.limits = clock, sleeper, limits
        self.budget_scope = budget_scope
        self.automatic_budget_scope = automatic_budget_scope is True
        self.authorization_check = authorization_check or (lambda: True)

    def extract(self, company, filing, pages, config, *, cancel_check=None):
        deadline = time.monotonic() + float(config.timeout_seconds)
        try:
            if (cancel_check and cancel_check()) or not self.authorization_check():
                raise VisionAdapterError("RECOVERY_CANCELLED")
            _check_request(config, pages, filing)
            if config.provider not in {"mineru_flash", "configured_model"}:
                raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")
            flash = config.provider == "mineru_flash"
            protocol = MINERU_UPLOAD_PROTOCOL_VERSION if flash else "vision-gateway-v2-single-wire"
            provider_id = "mineru_flash" if flash else f"configured_model:{config.configured_model_id}"
            if len(pages) != 1:
                raise VisionAdapterError("VISION_RECOVERY_REQUIRES_PAGE_TARGET")
            # Detach mutable domain objects from producers and concurrent jobs.
            company, filing = deepcopy(company), deepcopy(filing)
            page = pages[0]
            cancelled = lambda: bool((cancel_check and cancel_check()) or not self.authorization_check())
            configuration = digest({"provider": config.provider, "protocol": protocol,
                "configured_model_id": config.configured_model_id, "connection_id": config.connection_id,
                "configuration_version": config.configuration_version, "language": config.language,
                "timeout_seconds": config.configured_timeout_seconds or config.timeout_seconds, "max_pages": config.max_pages,
                "max_bytes": config.max_bytes})
            frozen_input = digest({"source": asdict(SourceIdentity.from_filing(filing)),
                "company": company.to_dict(), "page": page.original_page, "page_hash": page.content_hash,
                "unit_contexts": [asdict(item) for item in page.unit_contexts],
                "configuration": configuration, "validator": "financial-compiler-cloud-v2"})
            target = f"financial-page:{filing.document_id}:{page.original_page}"
            page_id = f"{filing.document_id}:{page.original_page}:{page.content_hash}"
            local_rereads = {}
            local_validation_code = ""

            def admit(payload):
                result = _deserialize_vision_result(payload)
                if result is None:
                    raise ValueError("CLOUD_CANDIDATE_INVALID")
                if (any(fact.company_cik != filing.company_cik or fact.source_page != page.original_page
                        or fact.accession_number != filing.accession_number for fact in result.facts)
                        or any(ref.document_id != filing.document_id for ref in result.evidence)):
                    raise ValueError("CLOUD_CANDIDATE_SOURCE_MISMATCH")
                result = replay_saved_table_sources(result, company, filing, (page,))
                if result.error_code:
                    if result.error_code == "VISION_CACHED_SOURCE_INVALID":
                        if "VISION_CANDIDATE_INTEGRITY_FAILED" in result.diagnostics:
                            raise ValueError("CLOUD_CANDIDATE_INVALID")
                        raise RecoveryError(result.error_code,
                            needs_action="review_saved_candidate_source")
                    raise RecoveryError(result.error_code,
                        needs_action="retry_saved_candidate_validation_or_export_diagnostic")
                from .financial_cell_repair import reread_cell_mappings
                # First verification is outside the SQLite publication
                # transaction. Reuse that immutable native observation at
                # commit; the pure compiler still rechecks every source proof.
                # A committed native candidate already carries its reread
                # proof and can be replayed without reopening a PDF in a DB lock.
                key = digest(payload)
                if key not in local_rereads:
                    from .financial_contracts import CandidateBatch
                    local_rereads[key] = (CandidateBatch(filing) if any(
                        any(item.startswith("cell-reread:") for item in fact.classification_evidence_ids)
                        for fact in result.facts) else reread_cell_mappings(company, filing, result.evidence,
                            deadline=deadline, cancel_check=cancelled))
                repair = local_rereads[key]
                # The paid response is already journaled. A local worker that
                # timed out or could not start has not disproved any amount;
                # leave validation pending so a fresh local budget can reuse
                # that same response. Known source/semantic contradictions
                # still cross the ordinary compiler rejection below.
                pending_code = next((code for code in repair.diagnostics if code in {
                    "CELL_REPAIR_TIMEOUT", "CELL_REPAIR_LOCAL_UNAVAILABLE", "VISION_CANCELLED",
                }), "")
                if pending_code:
                    raise RecoveryError(pending_code,
                        needs_action="retry_saved_candidate_validation_or_export_diagnostic")
                combined_facts = (*result.facts, *(item.fact for item in repair.candidates))
                combined_evidence = (*result.evidence, *repair.evidence)
                compiled = self.compiler.compile_facts(company, (filing,), combined_facts,
                    required_concepts=frozenset(fact.concept for fact in combined_facts), evidence=combined_evidence)
                if (compiled.quarantined_facts or not compiled.accepted_facts
                        or not {fact.fact_id for fact in combined_facts}.issubset(
                            fact.fact_id for fact in compiled.accepted_facts)):
                    raise ValueError("CLOUD_CANDIDATE_FINANCIAL_INVARIANT_FAILED")
                return _serialize_vision_result(VisionExtractionResult(
                    compiled.accepted_facts, compiled.evidence,
                    (*result.diagnostics, *repair.diagnostics), None))

            repository = CloudCandidateRepository(self.store, self.run_id, target, frozen_input,
                cancel_check=cancelled, admit_candidate=admit, provider_id=provider_id,
                submission_identity=page_id)
            issue = Issue("FINANCIAL_PAGE_RECOGNITION_REQUIRED", "document", "page", self.run_id,
                frozen_input, affected_ids=(page_id,), retryability="transient")
            # Action/plan identity must describe the configured policy, not
            # this invocation's shrinking task remainder. Actual transports
            # and native rereads still receive config.timeout_seconds/deadline.
            duration = float(config.configured_timeout_seconds or config.timeout_seconds)
            action = RecoveryAction("mineru-flash-page" if flash else "gateway-vision-page", protocol, (page_id,),
                frozen_input, "financial-compiler-cloud-v2", configuration_fingerprint=configuration,
                provider_id=provider_id, page_ids=(page_id,), external_submission=True,
                estimated_usage=Usage(calls=4 if flash else 1, pages=1, seconds=duration))
            authorization = AuthorizationScope((provider_id,), (page_id,), (configuration,))
            limits = self.limits or RecoveryLimits(calls=32, pages=config.max_pages)
            limits = replace(limits, pages=min(limits.pages, config.max_pages))
            # A lease must cover one allowed synchronous transport deadline.
            # This does not extend the incident/runtime budget: it only avoids
            # treating an in-budget HTTP response as an abandoned worker.
            limits = replace(limits, lease_seconds=max(limits.lease_seconds, duration + 5.0))
            scope = self.budget_scope
            call_plan = self.store.model_call_plan(self.run_id) or {}
            if (call_plan.get("policy") == "adaptive" and self.limits is None
                    and self.automatic_budget_scope):
                # One source-bound page is one finite recovery work item.
                # Page consent and unknown-submission guards stay unchanged;
                # unrelated pages cannot consume a hidden run-wide 32-call
                # quota. Only an app-generated scope is discarded here;
                # explicit scopes and user-supplied limits remain authoritative.
                scope = None
            plan = RecoveryPlan("incident:" + digest({"run": self.run_id, "target": target,
                "input": frozen_input}) if scope is None else "incident:" + digest({
                    "run": self.run_id, "target": scope.target, "input": scope.input_digest}),
                self.run_id, target, repository.generation, frozen_input,
                (issue,), (action,), limits=limits, authorization_scope=authorization, budget_scope=scope)

            def produce(context, task_id=""):
                if time.monotonic() >= deadline:
                    raise RecoveryError("VISION_TIMEOUT", needs_action="resume_saved_page_with_fresh_task_budget")
                wire_context = _SharedRunCallContext(context, self.store, self.run_id, config, page)
                try:
                    if flash:
                        result = self.adapter.extract(company, filing, (page,), config,
                            cancel_check=cancelled, recovery_context=wire_context, remote_task_id=task_id)
                    else:
                        result = self.adapter.extract(company, filing, (page,), config,
                            cancel_check=cancelled, recovery_context=wire_context)
                finally:
                    if wire_context.current is not None:
                        from .providers import ProviderError
                        wire_context.settle_model_call(error=ProviderError("Page attempt did not return a receipt",
                            code="MODEL_SUBMISSION_UNKNOWN", not_sent=not context.external_intent))
                if result.error_code:
                    key = vision_task_key(f"mineru_flash:{MINERU_UPLOAD_PROTOCOL_VERSION}",
                        filing.content_hash, (page,), namespace="page")
                    remote = self.store.get_vision_task(key) if flash else None
                    # Import an already known legacy task into the shared
                    # journal without claiming that a second POST was sent.
                    if (remote and remote.get("remote_task_id") and not task_id
                            and not context.external_intent):
                        context.record_external_intent()
                        context.record_external_task(str(remote["remote_task_id"]))
                    pending = result.error_code in {"VISION_PENDING", "VISION_TIMEOUT", "VISION_NETWORK_ERROR", "VISION_RATE_LIMITED"}
                    known = bool(task_id or context.external_task_id or remote and remote.get("remote_task_id"))
                    raise RecoveryError(result.error_code,
                        transient=result.error_code in {"VISION_NETWORK_ERROR", "VISION_RATE_LIMITED"},
                        retry_after=getattr(result, "retry_after_seconds", 0.0),
                        waiting_external=pending and known,
                        needs_action="query_saved_external_task" if pending and known else "review_cloud_configuration_or_source")
                return CandidateResult(_serialize_vision_result(result))

            def verify(candidate, _plan):
                nonlocal local_validation_code
                try:
                    admit(candidate.payload)
                    return VerificationResult(True, (issue.issue_id,))
                except RecoveryError as error:
                    local_validation_code = error.code
                    # The coordinator's verifier-exception branch retains the
                    # saved body as pending, never marks it financially invalid
                    # and never invokes the producer to fix a local failure.
                    raise
                except (TypeError, ValueError):
                    return VerificationResult(False, remaining_issue_ids=(issue.issue_id,),
                        invariant_failures=("CLOUD_CANDIDATE_FINANCIAL_INVARIANT_FAILED",))

            registry = StrategyRegistry()
            registry.register(Strategy(action.strategy_id, action.strategy_version, action.verifier_id,
                produce, verify, lambda _plan, _action: True,
                resume_external=produce if flash else None,
                observation_usage=Usage(calls=2, seconds=duration) if flash else Usage(),
                auto_observe=flash, max_remote_calls=None if flash else 2))
            outcome = RecoveryCoordinator(repository, registry, clock=self.clock, sleeper=self.sleeper).execute(
                plan, expected_generation=repository.generation, plan_hash=plan.plan_hash,
                authorization=authorization)
            if outcome["state"] == RecoveryState.COMMITTED:
                result = repository.committed_result(str(outcome.get("committed_revision_id") or ""))
                restored = _deserialize_vision_result(result) if result else None
                if restored is not None:
                    return restored
                return VisionExtractionResult(diagnostics=("VISION_SAVED_CANDIDATE_INVALID",),
                    error_code="VISION_SAVED_CANDIDATE_INVALID")
            code = str(outcome.get("diagnostic_code") or "VISION_RECOVERY_REQUIRED")
            if code == "RECOVERY_VERIFIER_FAILED" and local_validation_code:
                code = local_validation_code
            return VisionExtractionResult(diagnostics=(code,), error_code=code)
        except (VisionAdapterError, RecoveryConflict) as exc:
            code = exc.code if isinstance(exc, VisionAdapterError) else str(exc)
            return VisionExtractionResult(diagnostics=(code,), error_code=code)
