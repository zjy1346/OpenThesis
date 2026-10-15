"""Deep application seams used by :mod:`openthesis.service`.

These components own one concern each while preserving the existing JSON-RPC
surface.  They deliberately accept the already-configured collaborators so
desktop, tests, and headless callers keep the same dependency seams.
"""

from __future__ import annotations

from copy import deepcopy
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError as FutureTimeout
import re
import time
import math
from uuid import uuid4
from typing import Any, Callable, Sequence

from .financial_recovery import FinancialRecoveryController
from .financial_recognition import FinancialRecognitionCoordinator
from .financial_ingestion import FinancialIngestionEngine
from .research import ResearchCancelled, ResearchWorkflow
from .comparison import compare_research_runs
from .domain import ResearchRun, ResearchArtifact
from .financial_sources.disclosures import cached_filing_evidence
from .research_materials import has_sourced_disclosure
from .research_continuity import ResearchContinuityCoordinator
from .application.research_requests import (
    ResearchStartRequest, ResearchResumeRequest, ResearchExecutionProgress, ResearchComparisonFailure,
)
from .application.research_preparation import NewResearchPreparer, ResearchPreparationRequest
from .application.reports import (
    ReportReadError, ReportReadResult, ReportReadSnapshot, ReportService, _report_read_state,
)
from .storage_research_publication import ResearchPublicationConflict
from .resilience.contracts import RecoveryLimits


class DisclosureService:
    """Resolve companies and capture market disclosures through one seam."""

    def __init__(self, market_data: Any, market_snapshot: Any):
        self.market_data = market_data
        self.market_snapshot = market_snapshot

    def resolve(self, query: str, market: Any, *, limit: int = 15) -> Sequence[Any]:
        return self.market_data.resolve(query, market, limit=limit)

    def adapter_for(self, company: Any) -> Any:
        return self.market_data.adapter_for(company)

    def capture(self, company: Any, policy: Any, manual: Any = None) -> Any:
        if manual is None:
            return self.market_snapshot.capture(company, policy)
        return self.market_snapshot.capture(company, policy, manual)


class FinancialSourceService:
    """Official discovery and source compatibility configuration only.

    Candidate collection, compilation and admission belong to the core
    FinancialPipeline, not this application-side source configuration seam.
    """

    def __init__(
        self,
        ingestion: FinancialIngestionEngine,
        recognition: FinancialRecognitionCoordinator,
        recovery: FinancialRecoveryController,
    ):
        self.ingestion = ingestion
        self.recognition = recognition
        self.recovery = recovery

    def ingestion_for(self, company: Any, filings: Sequence[Any]) -> Any:
        clone = getattr(self.ingestion, "with_compatibility_rules", None)
        if not callable(clone):
            return self.ingestion
        report_type = "annual" if any(
            str(item.fiscal_period or "FY").upper() == "FY" for item in filings
        ) else "quarterly"
        rules = self.recovery.compatibility_rules(company, report_type)
        return clone(rules)

    def recognition_for(self, company: Any, filings: Sequence[Any]) -> FinancialRecognitionCoordinator:
        engine = self.ingestion_for(company, filings)
        if engine is self.ingestion:
            return self.recognition
        return FinancialRecognitionCoordinator(engine)

    def discover(self, adapter: Any, company: Any, *, annual_limit: int, force: bool = False) -> Any:
        return self.recovery.discover(adapter, company, annual_limit=annual_limit, force=force)

    def local_filings(self, company: Any, *, annual_limit: int) -> Sequence[Any]:
        return self.recovery.local_filings(company, annual_limit=annual_limit)

    def compatibility_rules(self, company: Any, report_type: str) -> Any:
        return self.recovery.compatibility_rules(company, report_type)

    def compatibility_summary(self, market: Any, report_type: str) -> Any:
        return self.recovery.compatibility_summary(market, report_type)


class ResearchComparisonCancelled(ResearchCancelled):
    """Cancellation of additive work does not invalidate a committed primary."""

    def __init__(self, primary_run: ResearchRun, comparison_index: int):
        super().__init__(run_id=primary_run.run_id)
        self.primary_run = primary_run
        self.comparison_index = comparison_index


class ResearchOrchestrator:
    """Execute captured new/resumed research; transport only supplies inputs.

    The shared workflow owns stage DAG and validators. This application seam
    owns model selection, immutable peer inputs and additive comparison failure
    isolation; it does not discover, download or compile financial facts.
    """

    def __init__(self, storage: Any, provider_factory: Callable[[Any], Any], *,
                 new_research_preparer: NewResearchPreparer | None = None,
                 source_task_budget_seconds: float | None = None):
        self.storage = storage
        self.provider_factory = provider_factory
        self.new_research_preparer = new_research_preparer
        budget = RecoveryLimits().seconds if source_task_budget_seconds is None else source_task_budget_seconds
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
            raise ValueError("SOURCE_TASK_BUDGET_INVALID")
        self.source_task_budget_seconds = float(budget)
        self.continuity = ResearchContinuityCoordinator(storage)

    def start(
        self, request: ResearchStartRequest | ResearchPreparationRequest, *,
        cancel_check: Callable[[], bool] | None = None,
        preparation_progress: Callable[..., None] | None = None,
        disclosure_coverage: Callable[[Any], None] | None = None,
        progress: Callable[[ResearchExecutionProgress], None] | None = None,
        agent_progress: Callable[[str, str], None] | None = None,
        comparison_failure: Callable[[ResearchComparisonFailure], None] | None = None,
    ) -> ResearchRun:
        cancelled = cancel_check or (lambda: False)
        notify = progress or (lambda _event: None)
        if isinstance(request, ResearchPreparationRequest):
            if self.new_research_preparer is None:
                raise ValueError("RESEARCH_PREPARER_NOT_CONFIGURED")
            # Overlap only source-independent work and only the existing
            # primary run. No extra comparison, paid retry, or new lifecycle.
            source_futures = []
            source_deadlines = {}
            source_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-research")

            def source_ready(subject, filings):
                if source_futures or request.existing_run is None or cancelled():
                    return
                if getattr(request.selected_pack, "pack_id", "") != "official.long-term-fundamentals":
                    return
                frozen_run = deepcopy(request.existing_run)
                frozen_run.company = subject
                # A model's timeout bounds one network invocation, not source
                # indexing + generation + semantic verification together.
                # Reuse the existing recovery task allowance as a separate
                # absolute deadline; every wire still has its original limit.
                deadline = time.monotonic() + self.source_task_budget_seconds

                def source_work():
                    evidence = cached_filing_evidence(self.storage, filings, cancel_check=cancelled, deadline=deadline)
                    if cancelled() or not has_sourced_disclosure(evidence):
                        return
                    workflow = self.create(request.selected_pack, request.models[0], cancel_check=cancelled,
                        report_language=request.report_language, ui_language=request.ui_language,
                        parallel_agents=request.parallel_agents, agent_progress=agent_progress)
                    scope = getattr(getattr(workflow, "provider", None), "for_request_budget", None)
                    if callable(scope):
                        workflow.provider = scope(deadline, cancelled)
                    prefetch = getattr(workflow, "prefetch_source_stages", None)
                    if callable(prefetch):
                        prefetch(frozen_run, evidence)
                future = source_executor.submit(source_work)
                source_futures.append(future)
                source_deadlines[future] = deadline

            try:
                prepared = self.new_research_preparer.prepare(
                    request, cancel_check=cancelled, progress=preparation_progress,
                    sources_ready=source_ready)
                for future in source_futures:
                    try:
                        while True:
                            if cancelled():
                                raise ResearchCancelled()
                            remaining = source_deadlines[future] - time.monotonic()
                            if remaining <= 0 and not future.done():
                                # A transport that ignored its request budget
                                # cannot hold the whole research lifecycle.
                                # Receipt/request identity remains journal-owned;
                                # later generation publication fences this owner.
                                future.cancel()
                                raise FutureTimeout()
                            try:
                                future.result(timeout=max(0.0, min(.05, remaining)))
                                break
                            except FutureTimeout:
                                if future.done():
                                    raise
                    except (ResearchCancelled, CancelledError, ResearchPublicationConflict):
                        raise
                    except Exception as error:
                        # Durable request/checkpoint retains any received body;
                        # the full graph performs exact-scope local recovery.
                        # An early optional path cannot block final preparation.
                        # Record a code, never exception text/credentials. This
                        # is not a terminal report or a factual success flag.
                        code = getattr(error, "code", "")
                        if isinstance(error, FutureTimeout):
                            code = "SOURCE_PREFETCH_TASK_TIMEOUT"
                        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code):
                            code = "SOURCE_PREFETCH_LOCAL_FAILED"
                        self.storage.save_artifact(ResearchArtifact(
                            artifact_id=uuid4().hex,
                            run_id=request.existing_run.run_id, artifact_type="source-prefetch-diagnostic",
                            title="Source preparation requires local recovery", agent_id="source-prefetch",
                            content={"code": code,
                                     "error_type": type(error).__name__,
                                     "next_action": "check_saved_source_stage_candidate"}))
            finally:
                # Never join an ignored cancellation/deadline indefinitely.
                # Production native gateways are killable; stale generation
                # CAS prevents a non-cooperative late worker from publishing.
                source_executor.shutdown(wait=all(future.done() for future in source_futures), cancel_futures=True)
            if prepared.coverage is not None and disclosure_coverage is not None:
                disclosure_coverage(prepared.coverage)
            request = prepared.request
        if not isinstance(request, ResearchStartRequest):
            raise TypeError("CAPTURED_RESEARCH_START_REQUEST_REQUIRED")
        frozen_inputs = request.workflow_inputs()
        primary = None

        def cancel_comparison(index: int) -> None:
            assert primary is not None
            self.continuity.record_comparison_result(primary, comparison_index=index,
                error_code="RESEARCH_CANCELLED", model_id=request.models[index].public_id,
                cancelled=True)
            raise ResearchComparisonCancelled(primary, index)

        for index, config in enumerate(request.models):
            if cancelled():
                if primary is not None:
                    cancel_comparison(index)
                raise ResearchCancelled()

            def report_progress(message: str, percent: int, current: int = index) -> None:
                fraction = min(100, max(0, int(percent))) / 100
                notify(ResearchExecutionProgress(message,
                    (current + fraction) * 100 / len(request.models), current, len(request.models) - 1,
                    round(fraction * 100)))

            try:
                workflow = self.create(
                    request.selected_pack, config, cancel_check=cancelled,
                    report_language=request.report_language, ui_language=request.ui_language,
                    parallel_agents=request.parallel_agents, agent_progress=agent_progress,
                )
                run = workflow.run(**deepcopy(frozen_inputs), progress=report_progress,
                                   existing_run=request.existing_run if index == 0 else None)
                if index == 0:
                    primary = run
                    if request.recovery_session_id:
                        self.storage.reassign_financial_recovery_cases(request.recovery_session_id, run.run_id)
                else:
                    if cancelled():
                        cancel_comparison(index)
                    compare_research_runs(self.storage, primary, run, request.report_language,
                                          cancel_check=cancelled, stage_id=f"comparison:{index}")
                    if cancelled():
                        cancel_comparison(index)
            except ResearchComparisonCancelled:
                raise
            except (ResearchCancelled, CancelledError):
                if primary is not None:
                    cancel_comparison(index)
                raise
            except Exception as exc:
                if primary is None:
                    raise
                # Never retain arbitrary server exception text: it can contain
                # URLs or credentials. A known code or exception type is enough
                # for continuity; raw candidates remain with their own journal.
                code = str(getattr(exc, "code", ""))
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code):
                    code = type(exc).__name__
                failure = ResearchComparisonFailure(index, config.public_id, code,
                                                    bool(getattr(exc, "retryable", False)))
                self.continuity.record_comparison_result(primary, comparison_index=index,
                    error_code=code, model_id=failure.model_id, retryable=failure.retryable)
                if comparison_failure is not None:
                    comparison_failure(failure)
        assert primary is not None
        return primary

    def resume(
        self, request: ResearchResumeRequest, *, cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[str, int], None] | None = None,
        agent_progress: Callable[[str, str], None] | None = None,
    ) -> ResearchRun:
        if cancel_check is not None and cancel_check():
            raise ResearchCancelled()
        workflow = self.create(
            request.selected_pack, request.model, cancel_check=cancel_check,
            report_language=request.run.report_language, ui_language=request.ui_language,
            parallel_agents=request.parallel_agents, agent_progress=agent_progress,
            local_only=request.local_only,
        )
        workflow.manual_authorization_id = request.manual_authorization_id
        workflow.local_revalidation = request.local_only
        if request.target == "synthesis":
            return workflow.retry_synthesis(request.run, deepcopy(list(request.artifacts)),
                                            deepcopy(list(request.facts)))
        configuration = deepcopy(request.run.research_configuration)
        return workflow.run(
            request.run.company, deepcopy(list(request.facts)),
            filing_evidence=deepcopy(list(request.filing_evidence)),
            valuation_inputs=configuration.get("valuation_inputs"),
            market_snapshot=deepcopy(request.run.market_snapshot) or configuration.get("market_snapshot"),
            reproducibility={"research_configuration": configuration,
                             "data_snapshot": deepcopy(request.run.data_snapshot)},
            existing_run=request.run, resume_artifacts=deepcopy(list(request.artifacts)),
            resume_target=request.target, progress=progress,
        )

    def create(
        self,
        selected_pack: Any,
        config: Any,
        *,
        cancel_check: Callable[[], bool] | None = None,
        report_language: str = "zh-CN",
        ui_language: str = "zh-CN",
        parallel_agents: bool = False,
        agent_progress: Callable[[str, str], None] | None = None,
        local_only: bool = False,
    ) -> ResearchWorkflow:
        provider = None if local_only else self.provider_factory(config)
        set_cancel_check = getattr(provider, "set_cancel_check", None)
        if callable(set_cancel_check):
            set_cancel_check(cancel_check)
        return ResearchWorkflow(
            self.storage,
            selected_pack,
            provider,
            config,
            cancel_check=cancel_check,
            report_language=report_language,
            ui_language=ui_language,
            parallel_agents=parallel_agents,
            agent_progress=agent_progress,
        )
