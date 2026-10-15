"""Authorized cloud recovery; candidates still cross the pure compiler.

The Adapter hides persisted budgets, query-only task continuation and local
revalidation behind the existing financial-source extract Interface. It never
changes providers, edits an original filing or promotes a partial dataset.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import time
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
    _check_request, _serialize_vision_result, _deserialize_vision_result, vision_task_key,
)


class DurableCloudVisionAdapter:
    """One approved page is one recovery target, shared by start and resume."""

    def __init__(self, adapter: Any, store: Any, run_id: str, *,
                 compiler: FinancialFactCompiler | None = None,
                 clock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep,
                 authorization_check: Callable[[], bool] | None = None,
                 limits: RecoveryLimits | None = None,
                 budget_scope: IncidentScope | None = None):
        self.adapter, self.store, self.run_id = adapter, store, run_id
        self.compiler = compiler or FinancialFactCompiler()
        self.clock, self.sleeper, self.limits = clock, sleeper, limits
        self.budget_scope = budget_scope
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
            plan = RecoveryPlan("incident:" + digest({"run": self.run_id, "target": target,
                "input": frozen_input}) if self.budget_scope is None else "incident:" + digest({
                    "run": self.run_id, "target": self.budget_scope.target, "input": self.budget_scope.input_digest}),
                self.run_id, target, repository.generation, frozen_input,
                (issue,), (action,), limits=limits, authorization_scope=authorization, budget_scope=self.budget_scope)

            def produce(context, task_id=""):
                if time.monotonic() >= deadline:
                    raise RecoveryError("VISION_TIMEOUT", needs_action="resume_saved_page_with_fresh_task_budget")
                if flash:
                    result = self.adapter.extract(company, filing, (page,), config,
                        cancel_check=cancelled, recovery_context=context, remote_task_id=task_id)
                else:
                    result = self.adapter.extract(company, filing, (page,), config,
                        cancel_check=cancelled, recovery_context=context)
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
