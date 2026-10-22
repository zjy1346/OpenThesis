from __future__ import annotations

from copy import deepcopy

import base64
import binascii
import hashlib
import json
import locale
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .build_identity import load_build_info
from .application_services import (
    DisclosureService,
    FinancialSourceService,
    ResearchOrchestrator,
    ResearchComparisonCancelled,
)
from .application.reports import (
    ReportReadError,
    ReportReadSnapshot,
    ReportService,
    artifacts_for_report_projection as _artifacts_for_report_projection,
)
from .application.financial_refresh import FinancialArtifactRefresher
from .application.financial_repair import FinancialRepairer, FinancialRetryResult
from .application.financial_outcomes import unavailable_outcome, continuity_financial_outcome
from .application.research_requests import (
    ResearchResumeRequest, ResearchExecutionProgress,
)
from .application.financials import (
    OfficialFinancialPreparer, OfficialFinancialRequest, OfficialFinancialUnavailable,
    compiler_validation_groups,
)
from .application.research_preparation import (
    NewResearchPreparer, ResearchPreparationRequest,
    ResearchPreparationUnavailable as _ResearchDataUnavailable,
)
from .application.research_snapshot import (
    canonical_snapshot_digest as _canonical_snapshot_digest,
    scanner_diagnostic_code as _scanner_diagnostic_code,
)
from concurrent.futures import CancelledError
from .demo import DEMO_COMPANY
from .domain import Company, EvidenceRef, FilingDocument, FinancialFact, ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from .financial_cloud_recovery import DurableCloudVisionAdapter
from .financial_sources.disclosures import (
    cached_filing_evidence as _cached_filing_evidence,
    saved_filing_evidence as _saved_filing_evidence,
)
from .research_content_flow import report_flow
from .research_readiness import readiness, primary_market_alternative
from .filing_selection import select_research_filings
from .i18n import EN, ZH_HANT, normalize_language, resolve_system_language, resolve_ui_language, translate_error
from .market_data import MarketDataError, MarketDataModule
from .company_search import OfficialCompanySearch, SEARCH_CONTRACT
from .official_network import OfficialNetworkPolicy, OfficialNetworkError
from .market_capabilities import DEFAULT_MARKET_CAPABILITIES
from .market_snapshot import (
    FxRouter,
    EastmoneyPublicQuoteAdapter,
    MarketSnapshotModule,
    ReverseDcfPolicy,
    StorageSnapshotCache,
    YahooChartQuoteAdapter,
)
from .financial_ingestion import (
    FinancialIngestionEngine,
    FinancialGroupValidation,
    FinancialProfile,
)
from .financial_compiler import (
    CoveragePlanner,
    FactGroupValidation,
    concepts_cover_profile,
)
from .financial_recognition import FinancialRecognitionCoordinator
from .financial_recovery import FinancialRecoveryController, RecoveryState
from .financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from .financial_sources.downloads import (
    bounded_download_filings as _bounded_download_filings,
    download_error_is_transient as _download_error_is_transient,
    filing_cache_valid as _filing_cache_valid,
    filing_identity_matches as _filing_identity_matches,
)
from .financial_sources.acquisition import OfficialFilingAcquisition
from .financial_sources.deferred import DeferredOfficialSource
from .financial_contracts import CompilationPolicy, GapStageKind
from .market_financials import FinancialValidation, ValidationStatus
from .markets import COMMON_MARKET_COMPANIES, MARKET_PROFILES, Market, normalize_market
from .onboarding import COMMON_COMPANIES, build_sec_user_agent
from .ot import OtValidationError, compile_studio_draft, validate_studio_draft
from .packs import (
    MAX_PACKAGE_BYTES,
    ResearchPack,
    builtin_pack,
    install_pack,
    list_installed_packs,
)
from .providers import ModelConfig, ProviderError, create_provider
from .report_html import render_research_html
from .research import ResearchCancelled
from .research_continuity import ResearchContinuityCoordinator, StageOutcome
from .research_recovery import ResearchRecoveryPlanner
from .research_execution.manual_attempts import read_wire_states, authorize_new_attempt
from .reporting import render_research_run
from .safe_report import SafeReportAssembler
from .report_readiness import (
    readiness_for_report_artifact,
)
from .sec_client import SEC_HK_ISSUERS, SecClient, SecClientError, SecFinancialSourceAdapter
from .storage import DERIVED_PIPELINE_CONTRACT, Storage
from .vision_financials import (
    GatewayVisionAdapter,
    MineruFlashAdapter,
    VisionAdapterError,
    VisionFallbackConfig,
    VisionFinancialSourceAdapter,
    VisionTaskCoordinator,
    VISION_TASK_SCHEMA_VERSION,
)


CONTRACT_VERSION = "2.0"


def _ui_message(language: str, english: str, simplified: str, traditional: str | None = None) -> str:
    """Select a user-facing status message without binary language fallthrough."""
    locale = normalize_language(language)
    if locale == EN:
        return english
    if locale == ZH_HANT:
        return traditional or simplified
    return simplified

PREFERENCE_DEFAULTS: dict[str, str] = {
    "ui_language": "zh-CN",
    "ui_language_mode": "system",
    "report_language": "zh-CN",
    "sidebar_collapsed": "true",
    # New users get bounded parallel scheduling; an explicitly persisted
    # false preference continues to opt out.
    "parallel_agents": "true",
    "research_market": "US",
    "sec_contact_profile": "personal",
    "sec_contact_email": "",
    "sec_user_agent": "",
    "official_network_policy": "",
    # Atomic, non-secret visual fallback policy written by the desktop UI.
    # Keeping the complete policy in one setting prevents a partially written
    # provider/authorization pair from ever enabling uploads accidentally.
    "vision_fallback_policy": "",
    # Versioned, non-secret visual fallback policy.  A standing authorization
    # is always explicit and can be revoked independently of provider/model
    # selection.  Legacy per-run consent is never promoted into these keys.
    "vision_policy_version": "1",
    "vision_enabled": "false",
    "vision_provider": "mineru_flash",
    "vision_configured_model_id": "",
    "vision_connection_id": "",
    "vision_configuration_version": "1",
    "vision_approval_mode": "review_each_plan",
    "vision_standing_authorization": "false",
    "vision_authorized_provider": "",
    "vision_authorized_scope": "failed_financial_statement_pages",
    "vision_authorized_policy_version": "",
    "vision_authorized_at": "",
}


class PreferenceValidationError(ValueError):
    """Raised when a caller attempts to persist an unsupported preference."""


class ServiceConfigurationError(RuntimeError):
    """A user-fixable configuration gap with a stable public error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_CORE_FINANCIAL_CONCEPTS = frozenset({
    "revenue", "net_income", "operating_cash_flow",
    "assets", "liabilities", "equity",
})


def _latest_annual_validations(
    validations: tuple[FactGroupValidation, ...] | list[FactGroupValidation],
    period_end: str,
) -> list[FactGroupValidation]:
    """Select complete consolidated FY groups independently of interim targets."""
    return [
        item for item in validations
        if str(item.identity[1])[:10] == str(period_end)[:10]
        and str(item.identity[2]).upper() == "FY"
        and str(item.identity[3]).strip().lower() == "consolidated"
        and str(getattr(item, "role", "target")) == "target"
        and str(item.status) == ValidationStatus.VERIFIED.value
        and concepts_cover_profile(
            (fact.concept for fact in item.accepted), _CORE_FINANCIAL_CONCEPTS
        )
    ]


class _FinancialReportRefreshError(RuntimeError):
    """The financial stage finished but its deterministic report refresh failed."""

    code = "FILING_REPORT_REFRESH_FAILED"

    def __init__(self, result: dict[str, Any] | None = None):
        super().__init__(self.code)
        self.result = result or {}


@dataclass(slots=True)
class _ResearchJob:
    job_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    generation: int = 0
    revision: int = 0
    terminal_write_revision: int | None = field(default=None, repr=False)
    pre_terminal_snapshot: dict[str, Any] | None = field(default=None, repr=False)
    state: str = "queued"
    message: str = ""
    percent: int = 0
    run_id: str | None = None
    stage: str = "preparing"
    stage_current: int | None = None
    stage_total: int | None = None
    agent_states: dict[str, str] = field(default_factory=dict)
    cancel_requested: bool = False
    ui_language: str = "zh-CN"
    error_code: str | None = None
    market: str | None = None
    disclosure_url: str | None = None
    # perf_counter avoids the coarse timer resolution found on some Windows
    # hosts; these values are durations, not wall-clock timestamps.
    started_at: float = field(default_factory=time.perf_counter, repr=False)
    stage_started_at: float = field(default_factory=time.perf_counter, repr=False)
    finished_at: float | None = field(default=None, repr=False)
    stage_timings: dict[str, float] = field(default_factory=dict)
    engine_active_seconds: float = 0.0
    external_wait_seconds: float = 0.0
    timing_started_at: float = field(default_factory=time.perf_counter, repr=False)
    filing_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    vision_upload_preview: dict[str, Any] | None = None
    vision_approval: bool | None = None
    vision_approval_pending: bool = False
    vision_approval_event: threading.Event = field(default_factory=threading.Event, repr=False)
    operation_result: dict[str, Any] | None = None
    recovery_outcome: dict[str, Any] | None = None
    run: ResearchRun | None = field(default=None, repr=False)

    @staticmethod
    def _is_external_stage(stage: str) -> bool:
        return stage in {
            "filing-download",
            "vision-approval",
            "vision-processing",
            "comparison",
        }

    def roll_timing(self, next_stage: str, now: float) -> None:
        elapsed = max(0.0, now - self.timing_started_at)
        if self._is_external_stage(self.stage):
            self.external_wait_seconds += elapsed
        else:
            self.engine_active_seconds += elapsed
        self.timing_started_at = now

    def visible_snapshot(self) -> dict[str, Any]:
        # A short job lock must not contain SQLite I/O, but a terminal status
        # cannot promise completion while the worker still owns its final
        # write. Keep the last non-terminal view until that write commits.
        if self.terminal_write_revision is not None and self.pre_terminal_snapshot is not None:
            return deepcopy(self.pre_terminal_snapshot)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        now = self.finished_at or time.perf_counter()
        timings = dict(self.stage_timings)
        timings[self.stage] = timings.get(self.stage, 0.0) + max(
            0.0, now - self.stage_started_at
        )
        current_bucket = max(0.0, now - self.timing_started_at)
        active_seconds = self.engine_active_seconds
        external_seconds = self.external_wait_seconds
        if self._is_external_stage(self.stage):
            external_seconds += current_bucket
        else:
            active_seconds += current_bucket
        attempt_complete = self.state in {"completed", "failed", "cancelled"}
        research_complete = bool(
            attempt_complete
            and self.run is not None
            and self.run.status is RunStatus.COMPLETED
        )
        return {
            "job_id": self.job_id,
            "revision": self.revision,
            "state": self.state,
            "message": self.message,
            "percent": self.percent,
            "run_id": self.run_id,
            "stage": self.stage,
            "stage_current": self.stage_current,
            "stage_total": self.stage_total,
            "agent_states": dict(self.agent_states),
            "completed_agents": sum(state in {"completed", "completed_verified", "completed_partial", "reused_verified"}
                                    for state in self.agent_states.values()),
            "total_agents": len(self.agent_states),
            "cancel_requested": self.cancel_requested,
            "elapsed_seconds": int(max(0, now - self.started_at)),
            "stage_elapsed_seconds": round(max(0.0, now - self.stage_started_at), 3),
            "stage_timings": {
                key: round(value, 3) for key, value in timings.items()
            },
            "engine_active_seconds": round(active_seconds, 3),
            "external_wait_seconds": round(external_seconds, 3),
            "filing_states": {
                key: dict(value) for key, value in self.filing_states.items()
            },
            "error_code": self.error_code,
            "market": self.market,
            "disclosure_url": self.disclosure_url,
            "vision_upload_preview": dict(self.vision_upload_preview) if self.vision_upload_preview else None,
            "vision_approval": self.vision_approval,
            "vision_approval_pending": self.vision_approval_pending,
            "operation_result": dict(self.operation_result) if self.operation_result else None,
            "recovery_outcome": dict(self.recovery_outcome) if self.recovery_outcome else None,
            "attempt_complete": attempt_complete,
            "research_complete": research_complete,
            "action_required": bool(attempt_complete and not research_complete),
        }


class AppService:
    """Headless interface consumed by every desktop platform adapter."""

    def __init__(
        self,
        data_dir: Path,
        *,
        app_version: str = __version__,
        sec_client_factory: Callable[[str, Path], Any] = SecClient,
        provider_factory: Callable[[ModelConfig], Any] = create_provider,
        market_data: Any | None = None,
        market_snapshot_module: MarketSnapshotModule | None = None,
        market_quote_adapter_factory: Callable[[Storage], Any | None] | None = None,
        financial_ingestion_engine: FinancialIngestionEngine | None = None,
        vision_adapter_factory: Callable[[VisionFallbackConfig], VisionFinancialSourceAdapter] | None = None,
        initialization_progress=None,
    ):
        self.storage = Storage(data_dir, initialization_progress=initialization_progress) if initialization_progress else Storage(data_dir)
        if initialization_progress:
            initialization_progress("preparing_services")
        self.interrupted_run_count = self.storage.interrupt_running_runs()
        self._continuity = ResearchContinuityCoordinator(self.storage)
        self.app_version = app_version
        self._sec_client_factory = sec_client_factory
        self._provider_factory = provider_factory
        self._market_data = market_data or MarketDataModule()
        self._configure_official_network(self.storage.get_setting("official_network_policy", ""))
        self._company_search = OfficialCompanySearch(self.storage, decorate=self._company_search_result)
        # Quote acquisition is injected at this boundary so tests and
        # deployments can use in-memory adapters without touching the network.
        if market_snapshot_module is not None:
            self._market_snapshot = market_snapshot_module
        else:
            snapshot_adapters = None
            if market_quote_adapter_factory is not None:
                # The factory is the narrow seam for a secure settings
                # backend (e.g. the Rust model-center secret vault).  It
                # returns an adapter, never a request/report credential.
                try:
                    configured_adapter = market_quote_adapter_factory(self.storage)
                except Exception:
                    # A missing/locked secure vault must not disable the
                    # default keyless sources or zero-input research.
                    configured_adapter = None
                if configured_adapter is not None:
                    snapshot_adapters = (
                        configured_adapter,
                        EastmoneyPublicQuoteAdapter(),
                        YahooChartQuoteAdapter(),
                    )
            self._market_snapshot = MarketSnapshotModule(
                adapters=snapshot_adapters,
                cache=StorageSnapshotCache(self.storage), fx_adapter=FxRouter()
            )
        self._disclosure_service = DisclosureService(self._market_data, self._market_snapshot)
        self._financial_ingestion = financial_ingestion_engine or FinancialIngestionEngine(
            cache_dir=self.storage.data_dir / "financial-parse-cache",
            checkpoint_dir=self.storage.data_dir / "financial-window-checkpoints",
        )
        self._financial_recognition = FinancialRecognitionCoordinator(
            self._financial_ingestion
        )
        self._financial_recovery = FinancialRecoveryController(
            self.storage, app_version=self.app_version
        )
        self._financial_source_service = FinancialSourceService(
            self._financial_ingestion,
            self._financial_recognition,
            self._financial_recovery,
        )
        self._financial_pipeline = FinancialPipeline(
            self._financial_ingestion,
            engine_for=self._financial_source_service.ingestion_for,
        )
        self._financial_artifact_refresher = FinancialArtifactRefresher(
            self.storage, self._financial_pipeline
        )
        self._filing_acquisition = OfficialFilingAcquisition()
        self._official_financial_preparer = OfficialFinancialPreparer(
            self.storage, self._financial_source_service, self._filing_acquisition, self._financial_pipeline)
        self._financial_repairer = FinancialRepairer(
            self.storage, self._official_financial_preparer, self._financial_artifact_refresher)
        self._research_orchestrator = ResearchOrchestrator(
            self.storage, self._provider_factory,
            new_research_preparer=NewResearchPreparer(
                self.storage, self._official_financial_preparer, self._disclosure_service))
        self._report_service = ReportService(render_research_run, render_research_html)
        self._vision_adapter_factory = vision_adapter_factory or _default_vision_adapter_factory
        self._jobs: dict[str, _ResearchJob] = {}
        self._jobs_lock = threading.Lock()
        self._recovery_planner = ResearchRecoveryPlanner()
        self._active_recoveries: dict[tuple[str, str], tuple[threading.Event, str]] = {}
        if initialization_progress:
            initialization_progress("ready")

    def _configure_official_network(self, value: str) -> None:
        policy = _parse_official_network_policy(value)
        configure = getattr(self._market_data, "configure_network", None)
        if callable(configure):
            configure(policy)

    def _ingestion_for(
        self, company: Company, filings: Sequence[FilingDocument]
    ) -> Any:
        """Compatibility alias; ownership lives in :class:`FinancialPipeline`."""
        return self._financial_source_service.ingestion_for(company, filings)

    def _recognition_for(
        self, company: Company, filings: Sequence[FilingDocument]
    ) -> FinancialRecognitionCoordinator:
        """Compatibility alias; ownership lives in :class:`FinancialPipeline`."""
        return self._financial_source_service.recognition_for(company, filings)

    def _financial_recognition_capabilities(
        self,
        company: Company,
        filings: Sequence[FilingDocument],
        *,
        job: _ResearchJob | None = None,
        run_id: str = "",
        authorized_filing_hashes: set[str] | None = None,
    ) -> tuple[tuple[Any, ...], VisionFinancialSourceAdapter | None, VisionFallbackConfig | None]:
        """Resolve current structured and visual adapters for every entry path.

        Historical run payloads are audit records, not authorization.  Manual
        and automatic retries therefore re-read the current settings policy so
        revocation takes effect immediately.
        """

        preferences = self.preferences()
        structured_sources: tuple[Any, ...] = ()
        sec_mapping = SEC_HK_ISSUERS.get(company.ticker.upper())
        sec_email = str(preferences.get("sec_contact_email", "")).strip()
        if sec_mapping and "@" in sec_email and " " not in sec_email:
            try:
                client = self._sec_client_factory(
                    build_sec_user_agent(
                        _normalize_sec_profile(preferences["sec_contact_profile"]),
                        sec_email,
                    ),
                    self.storage.data_dir / "sec-cache",
                )
                structured_sources = (SecFinancialSourceAdapter(client),)
            except Exception:
                structured_sources = ()

        config = _vision_config_from_request(
            _vision_request_from_preferences(preferences)
        )
        if config is None or not config.enabled:
            return structured_sources, None, config
        policy_snapshot = _vision_policy_snapshot(preferences)
        filing_hashes = authorized_filing_hashes if authorized_filing_hashes is not None else {
            str(item.content_hash) for item in filings if str(item.content_hash or "")}
        approval_ledger: dict[str, tuple[str, int, int]] = {}

        def approve_upload(summary: dict[str, Any]) -> bool:
            if _vision_policy_snapshot(self.preferences()) != policy_snapshot:
                return False
            if config.approval_mode == "review_each_plan":
                if job is None:
                    return False
                safe_summary = {
                    key: summary.get(key)
                    for key in (
                        "plan_id", "provider", "pages", "total_bytes",
                        "source_document", "filing_hash", "document_hashes",
                        "max_pages", "max_bytes",
                    )
                    if key in summary
                }
                with self._jobs_lock:
                    if job.cancel_event.is_set() or job.state not in {"queued", "running"}:
                        return False
                    job.vision_approval_event.clear()
                    job.vision_approval = None
                    job.vision_approval_pending = True
                    job.vision_upload_preview = safe_summary
                self._update_job(job, active_only=True, stage="vision-approval",
                    message=_ui_message(
                        job.ui_language,
                        "Review failed financial pages before upload",
                        "上传前请审核本地识别失败的财务表页",
                        "上傳前請審核本地辨識失敗的財務表頁",
                    ))
                while not job.vision_approval_event.wait(0.1):
                    if job.cancel_event.is_set():
                        with self._jobs_lock:
                            job.vision_approval_pending = False
                        return False
                with self._jobs_lock:
                    approved = bool(job.vision_approval)
                    job.vision_approval_pending = False
                    job.vision_upload_preview = None
                if not approved or job.cancel_event.is_set():
                    return False
            # Re-check after the dialog, then account concurrent page plans in
            # one run-local critical section. A stale dialog cannot restore
            # settings authorization or race past the cumulative page cap.
            if _vision_policy_snapshot(self.preferences()) != policy_snapshot:
                return False
            with self._jobs_lock:
                approved = _vision_batch_upload_allowed(
                    summary, config, frozenset(filing_hashes),
                    job_active=job is None or job.state in {"queued", "running"},
                    cancelled=bool(job and job.cancel_event.is_set()),
                    approved_plans=approval_ledger,
                )
            if approved and job is not None:
                vision_progress(0, len(summary.get("pages") or ()))
            return approved

        def vision_progress(current: int, total: int) -> None:
            if job is not None and not job.cancel_event.is_set():
                self._update_job(job, active_only=True, stage="vision-processing",
                    stage_current=current, stage_total=total,
                    message=_ui_message(job.ui_language,
                        "Recognizing the approved financial pages",
                        "正在识别已授权的财务页面",
                        "正在辨識已授權的財務頁面"))

        config = replace(config, approve_upload=approve_upload)
        config.validate()
        adapter = self._vision_adapter_for_run(
            config, run_id or (job.run_id if job else ""), policy_snapshot, company=company
        )
        return (
            structured_sources,
            VisionTaskCoordinator(adapter, journal=self.storage, max_workers=2,
                progress=vision_progress if job is not None else None),
            config,
        )

    def _vision_adapter_for_run(
        self, config: VisionFallbackConfig, run_id: str, policy_snapshot: dict[str, Any],
        *, company: Company | None = None,
    ) -> VisionFinancialSourceAdapter:
        """Use the same durable cloud boundary for starts and repairs."""
        adapter = self._vision_adapter_factory(config)
        if isinstance(adapter, (MineruFlashAdapter, GatewayVisionAdapter)):
            from .financial_recovery import financial_incident_scope
            if isinstance(adapter, MineruFlashAdapter):
                adapter.journal = self.storage
            adapter = DurableCloudVisionAdapter(
                adapter, self.storage, run_id,
                authorization_check=lambda: _vision_policy_snapshot(self.preferences()) == policy_snapshot,
                budget_scope=financial_incident_scope(company) if company is not None else None,
            )
        return adapter

    def hello(self) -> dict[str, Any]:
        build_info = load_build_info()
        return {
            "contract_version": CONTRACT_VERSION,
            "company_search_contract": SEARCH_CONTRACT,
            "app_version": self.app_version,
            "build_info": build_info,
            "storage_health": dict(self.storage.maintenance_status),
            "capabilities": [
                "app.bootstrap",
                "settings.update",
                "company.search",
                "packs.install",
                "ot.validate",
                "ot.compile",
                "ot.suggest",
                "thesis.list",
                "thesis.get",
                "thesis.save",
                "research.list",
                "research.delete",
                "research.start_financial_retry",
                "research.start_financial_rebuild",
                "research.get_report",
                "rpc.start",
                "rpc.result",
                "research.extend_budget",
                "research.call_history",
                "research.financial_diagnostics",
                "research.start",
                "research.market_snapshot",
                "research.retry_growth",
                "research.retry_model_stages",
                "research.retry_synthesis",
                "research.retry_stage",
                "research.retry_financials",
                "research.rebuild_financials",
                "research.refresh_financial_report",
                "research.status",
                "research.cancel",
                "research.cancel_model_call",
                "research.vision_decision",
            ],
        }

    def bootstrap(self, *, minimal: bool = False) -> dict[str, Any]:
        if minimal:
            self.storage.start_background_maintenance()
        return {
            **self.hello(),
            "preferences": self.preferences(),
            "recent_runs": [] if minimal else self.list_research_runs(limit=20),
            "common_companies": [] if minimal else self.common_companies(),
            "market_catalog": self.market_catalog(),
            "research_packs": [] if minimal else self.research_packs(),
            "interrupted_runs": self.interrupted_run_count,
        }

    def preferences(self) -> dict[str, str]:
        stored = self.storage.get_settings({**PREFERENCE_DEFAULTS, "ui_language": "", "ui_language_mode": "", "report_language": ""})
        stored_ui = stored["ui_language"]
        stored_mode = stored["ui_language_mode"]
        mode = stored_mode or ("manual" if stored_ui else "system")
        system_tag = locale.getlocale()[0] or "en"
        system_language = resolve_system_language((system_tag,))
        ui_language = resolve_ui_language(mode, stored_ui or system_language, (system_language,))
        report_stored = stored["report_language"]
        return stored | {
            "ui_language": ui_language,
            "ui_language_mode": mode,
            "report_language": normalize_language(report_stored or ui_language),
        }

    def update_preferences(self, updates: dict[str, Any]) -> dict[str, str]:
        unknown = sorted(set(updates) - set(PREFERENCE_DEFAULTS))
        if unknown:
            raise PreferenceValidationError("unsupported preference key")
        for key, raw_value in updates.items():
            if not isinstance(raw_value, (str, bool)):
                raise PreferenceValidationError(
                    "preference values must be strings or booleans"
                )
            value = str(raw_value).lower() if isinstance(raw_value, bool) else raw_value
            if key == "ui_language_mode" and value not in {"system", "manual"}:
                raise PreferenceValidationError("ui_language_mode must be system or manual")
            if key in {"ui_language", "report_language"}:
                value = normalize_language(value)
            if key == "research_market":
                value = normalize_market(value).value
            if key == "official_network_policy":
                try:
                    _parse_official_network_policy(value)
                except (ValueError, TypeError) as error:
                    raise PreferenceValidationError("official network policy is invalid") from error
            if key in {"vision_enabled", "vision_standing_authorization"} and value not in {"true", "false"}:
                raise PreferenceValidationError(f"{key} must be true or false")
            if key in {"vision_policy_version", "vision_configuration_version"}:
                try:
                    value = str(max(1, int(value)))
                except (TypeError, ValueError) as exc:
                    raise PreferenceValidationError(f"{key} must be a positive integer") from exc
            if key in {"vision_provider", "vision_authorized_provider"} and value not in {"", "mineru_flash", "configured_model"}:
                raise PreferenceValidationError(f"{key} is unsupported")
            if key == "vision_approval_mode" and value not in {"review_each_plan", "approve_current_research"}:
                raise PreferenceValidationError("vision_approval_mode is unsupported")
            if key == "vision_authorized_scope" and value not in {"failed_financial_statement_pages"}:
                raise PreferenceValidationError("vision authorization scope is unsupported")
            if key == "vision_configured_model_id" and (
                len(value) > 128
                or any(not (character.isalnum() or character in "_.-") for character in value)
            ):
                raise PreferenceValidationError("vision configured model id is invalid")
            if key == "vision_fallback_policy":
                _parse_persisted_vision_policy(value, strict=True)
            self.storage.set_setting(key, value)
            if key == "official_network_policy":
                self._configure_official_network(value)
        return self.preferences()

    def common_companies(self) -> list[dict[str, Any]]:
        return [
            self._company_search_result(company)
            for company in (*COMMON_COMPANIES, *COMMON_MARKET_COMPANIES)
        ]

    def _company_readiness(self, company: Company) -> dict[str, Any]:
        key = _financial_storage_key(company.to_dict())
        return readiness(company, self.storage.get_validation_groups(key))

    def _company_search_result(self, company: Company) -> dict[str, Any]:
        status = self._company_readiness(company)
        alternative = primary_market_alternative(company, COMMON_MARKET_COMPANIES)
        if not status['recommended'] and alternative is not None:
            status['alternative'] = alternative.to_dict()
        return {**company.to_dict(), 'research_readiness': status}

    def market_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "market": profile.market.value,
                "label_zh": profile.label_zh,
                "label_zh_hant": profile.label_zh_hant,
                "label_en": profile.label_en,
                "exchanges": [exchange.value for exchange in profile.exchanges],
                "default_currency": profile.default_currency,
                "requires_sec_identity": profile.requires_sec_identity,
                "disclosure_home": profile.disclosure_home,
            }
            for profile in MARKET_PROFILES.values()
        ]

    def research_packs(self) -> list[dict[str, str]]:
        return [
            _serialize_pack(pack)
            for pack in list_installed_packs(self.storage.packs_dir)
        ]

    def install_research_pack(
        self, filename: str, encoded_archive: str
    ) -> dict[str, str]:
        if Path(filename).suffix.lower() != ".ot":
            raise ValueError("research pack must use the .ot extension")
        if not isinstance(encoded_archive, str) or not encoded_archive:
            raise ValueError("research pack payload is required")
        if len(encoded_archive) > ((MAX_PACKAGE_BYTES + 2) // 3) * 4 + 8:
            raise ValueError("research pack exceeds the size limit")
        try:
            payload = base64.b64decode(encoded_archive, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("research pack payload is invalid") from exc
        if not payload or len(payload) > MAX_PACKAGE_BYTES:
            raise ValueError("research pack exceeds the size limit")

        incoming = self.storage.data_dir / ".incoming-packs"
        incoming.mkdir(parents=True, exist_ok=True)
        archive = incoming / f"{uuid.uuid4().hex}.ot"
        try:
            archive.write_bytes(payload)
            return _serialize_pack(install_pack(archive, self.storage.packs_dir))
        finally:
            archive.unlink(missing_ok=True)

    def validate_ot_draft(self, draft: Any) -> dict[str, Any]:
        if not isinstance(draft, dict):
            raise ValueError("OT draft must be an object")
        diagnostics = validate_studio_draft(draft)
        return {
            "valid": not any(item.severity == "error" for item in diagnostics),
            "diagnostics": [item.as_dict() for item in diagnostics],
        }

    def compile_ot_draft(self, draft: Any) -> dict[str, Any]:
        if not isinstance(draft, dict):
            raise ValueError("OT draft must be an object")
        try:
            raw, package = compile_studio_draft(draft)
        except OtValidationError as exc:
            return {
                "valid": False,
                "diagnostics": [item.as_dict() for item in exc.diagnostics],
            }
        return {
            "valid": True,
            "filename": f"{package.package_id}-{package.version}.ot",
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "content_identity": package.content_identity,
            "manifest": package.manifest,
            "diagnostics": [item.as_dict() for item in package.diagnostics],
        }

    def suggest_ot_patch(
        self,
        draft: Any,
        selected_path: str,
        instruction: str,
        model_reference: Any,
    ) -> dict[str, Any]:
        if not isinstance(draft, dict):
            raise ValueError("OT draft must be an object")
        path = _validate_ot_suggestion_path(selected_path, draft)
        request = instruction.strip()
        if not request or len(request) > 2000:
            raise ValueError("OT suggestion instruction is invalid")
        config = _model_config_from_request(model_reference)
        if config.role != "ot_assistant":
            raise ValueError("OT assistant model role is required")
        provider = create_provider(config)
        if provider is None:
            raise ValueError("configured OT assistant model is unavailable")
        current_value = _read_json_pointer(draft, path)
        payload = {
            "editable_path": path,
            "current_value": current_value,
            "instruction": request,
            "constraints": {
                "one_path_only": True,
                "maximum_serialized_bytes": 4096,
                "secrets_prohibited": True,
                "no_network_or_code_permissions": True,
            },
        }
        result = provider.generate(
            "You are a bounded OT Studio field assistant. Return JSON with one key named suggestion. Never add credentials, URLs that receive credentials, executable code, or changes outside editable_path.",
            json.dumps(payload, ensure_ascii=False),
            json_mode=True,
        )
        if "suggestion" not in result:
            raise ValueError("model did not return a bounded OT suggestion")
        suggestion = result["suggestion"]
        encoded = json.dumps(suggestion, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 4096:
            raise ValueError("OT suggestion exceeds the field limit")
        candidate = json.loads(json.dumps(draft, ensure_ascii=False))
        _write_json_pointer(candidate, path, suggestion)
        diagnostics = validate_studio_draft(candidate)
        if any(item.severity == "error" for item in diagnostics):
            return {
                "accepted": False,
                "path": path,
                "before": current_value,
                "after": suggestion,
                "diagnostics": [item.as_dict() for item in diagnostics],
            }
        return {
            "accepted": True,
            "path": path,
            "before": current_value,
            "after": suggestion,
            "diagnostics": [item.as_dict() for item in diagnostics],
        }
    def search_companies(
        self,
        query: str,
        *,
        market: str = "US",
        limit: int = 15,
        request_id: str = "",
    ) -> dict[str, Any]:
        normalized = query.strip()
        if not normalized:
            raise ValueError("company query is required")
        selected_market = normalize_market(market)
        bounded_limit = min(50, max(1, int(limit)))
        return self._company_search.search(normalized, selected_market,
            lambda: self._fetch_official_companies(normalized, selected_market, bounded_limit),
            limit=bounded_limit, request_id=request_id).to_dict()

    def _fetch_official_companies(self, query: str, market: Market, limit: int = 15) -> list[Company]:
        if market != Market.US:
            return list(self._disclosure_service.resolve(query, market, limit=limit))
        preferences = self.preferences()
        profile = _normalize_sec_profile(preferences["sec_contact_profile"])
        try:
            user_agent = build_sec_user_agent(profile, preferences["sec_contact_email"])
        except ValueError as exc:
            raise ServiceConfigurationError("SEC_EMAIL_CONFIG_REQUIRED") from exc
        client = self._sec_client_factory(
            user_agent, self.storage.data_dir / "sec-cache"
        )
        if isinstance(client, SecClient):
            client.request_deadline_seconds = 12.0
            client.max_attempts = 1
        try:
            return client.search_companies(query, limit=limit)
        except SecClientError as error:
            raise OfficialNetworkError("SEC directory is unavailable", code="SOURCE_NETWORK_UNAVAILABLE") from error

    def list_research_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        from .storage_read_models import list_run_summaries
        with self.storage.connect() as db:
            summaries = list_run_summaries(db, limit)
        for summary in summaries:
            summary["report_language"] = normalize_language(str(summary.get("report_language", "zh-CN")))
        return summaries

    def delete_research_run(self, run_id: str) -> dict[str, Any]:
        if not run_id.strip():
            raise ValueError("run_id is required")
        if not self.storage.delete_run(run_id.strip()):
            raise KeyError("research run not found")
        return {"run_id": run_id.strip(), "deleted": True}

    def list_theses(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = min(500, max(1, int(limit)))
        return self.storage.list_thesis_versions(limit=bounded_limit)

    def get_thesis(self, thesis_version_id: str) -> dict[str, Any]:
        thesis = self.storage.get_thesis_version(thesis_version_id)
        if thesis is None:
            raise KeyError("thesis version not found")
        sources = []
        if thesis.get('run_id'):
            for artifact in self.storage.get_artifacts(thesis['run_id']):
                if artifact['artifact_type'] == 'deterministic-financial-summary':
                    sources = artifact.get('content', {}).get('evidence', [])
                    break
        thesis['sources'] = sources
        return thesis

    def save_thesis_version(
        self,
        company_cik: str,
        content: dict[str, Any],
        *,
        base_thesis_version_id: str | None = None,
        company: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not company_cik.strip() or not isinstance(content, dict):
            raise ValueError("thesis content is invalid")
        run_id = None
        company_cik = company_cik.strip()
        if base_thesis_version_id is not None:
            base = self.storage.get_thesis_version(base_thesis_version_id)
            if base is None or base['company_cik'] != company_cik:
                raise ValueError('thesis base must belong to the same company')
            run_id = base.get('run_id')
            content = {**content, '_parent_thesis_version_id': base_thesis_version_id}
        if not self.storage.company_exists(company_cik):
            if not isinstance(company, dict):
                raise ServiceConfigurationError("COMPANY_IDENTITY_REQUIRED")
            allowed = set(Company.__dataclass_fields__)
            try:
                identity = Company(**{
                    key: value for key, value in company.items() if key in allowed
                })
            except (TypeError, ValueError) as exc:
                raise ServiceConfigurationError("COMPANY_IDENTITY_INVALID") from exc
            if identity.cik != company_cik:
                raise ServiceConfigurationError("COMPANY_IDENTITY_MISMATCH")
            self.storage.save_company(identity)
        saved = self.storage.save_thesis_version(
            company_cik,
            content,
            run_id=run_id,
            created_by="user",
            created_at=utc_now_iso(),
        )
        return self.get_thesis(saved['thesis_version_id'])

    def get_report(
        self,
        run_id: str,
        *,
        language: str | None = None,
        include_technical: bool = False,
    ) -> dict[str, Any]:
        try:
            run = self.storage.get_run(run_id)
        except Exception as exc:
            raise ReportReadError("REPORT_STORAGE_UNAVAILABLE") from exc
        if run is None:
            raise KeyError("research run not found")
        if str(run.get("run_id") or "") != str(run_id):
            raise ReportReadError("REPORT_IDENTITY_MISMATCH")

        read_diagnostics: list[dict[str, str]] = []
        try:
            payload = _decode_payload(run.get("payload_json"))
        except Exception:
            payload = {}
            read_diagnostics.append({"code": "REPORT_PROJECTION_FAILED", "location": "run-payload"})
        if not isinstance(payload, dict):
            payload = {}
            read_diagnostics.append({"code": "REPORT_PROJECTION_FAILED", "location": "run-payload"})
        try:
            saved_report_language = self.storage.get_setting("report_language", "zh-CN")
        except Exception:
            saved_report_language = "zh-CN"
            read_diagnostics.append({"code": "REPORT_STORAGE_UNAVAILABLE", "location": "report-language"})
        report_language = normalize_language(
            language
            or str(payload.get("report_language", ""))
            or saved_report_language
        )
        try:
            read = self.storage.read_artifacts(run_id)
            artifacts = list(read.records)
            read_diagnostics.extend({**issue, "location": issue["path"]} for issue in read.issues)
        except Exception as exc:
            raise ReportReadError("REPORT_STORAGE_UNAVAILABLE") from exc
        recovery_artifacts = list(artifacts)
        try:
            raw_revision = self.storage.latest_report_revision(run_id)
        except Exception:
            raw_revision = None
            read_diagnostics.append({"code": "REPORT_STORAGE_UNAVAILABLE", "location": "report-revision"})
        report_revision_payload = raw_revision.get("payload") if isinstance(raw_revision, dict) else None
        if raw_revision is not None:
            try:
                artifacts = _artifacts_for_report_projection(artifacts, report_revision_payload)
            except Exception:
                read_diagnostics.append({"code": "REPORT_PROJECTION_FAILED", "location": "report-revision"})
        company = payload.get("company", {})
        if not isinstance(company, dict):
            company = {}
        company, currency_confirmation = _reporting_currency_identity(self.storage, company, payload)
        try:
            continuity = self._continuity.snapshot(run_id)
        except Exception:
            continuity = {"state": "unavailable", "latest": None, "attempts": []}
            read_diagnostics.append({"code": "RECOVERY_METADATA_UNAVAILABLE", "location": "continuity"})
        try:
            financial_status = _financial_status(
                self.storage, company, payload, run_id=run_id, continuity_snapshot=continuity,
            )
        except Exception:
            financial_status = {
                "state": "unavailable", "retryable": False, "history_years": 0,
                "expected_periods": [], "available_periods": [], "missing_periods": [],
                "unverified_periods": [], "nodes": [], "issues": [], "attempt_count": 0,
                "last_stage": "", "last_error": "", "updated_at": "",
                "next_action": "", "model_calls": 0, "token_delta": 0,
            }
            read_diagnostics.append({"code": "FINANCIAL_STATUS_UNAVAILABLE", "location": "financial-status"})
        revision_id = str(raw_revision.get("revision_id") or "") if raw_revision else ""
        revision_generation = int(raw_revision.get("generation") or 0) if raw_revision else 0
        report_revision = {
            key: value for key, value in raw_revision.items() if key != "payload"
        } if isinstance(raw_revision, dict) else None
        snapshot = ReportReadSnapshot(
            run_id=run_id,
            company_name=str(run.get("name") or ""),
            run_status=str(run.get("status") or ""),
            artifacts=artifacts,
            continuity=continuity,
            language=report_language,
            include_technical=include_technical,
            revision_id=revision_id,
            revision_generation=revision_generation,
        )
        try:
            report_read = self._report_service.read(snapshot)
        except Exception as projection_error:
            try:
                safe = SafeReportAssembler().assemble(
                    run_id=run_id, company_name=str(run.get("name") or ""),
                    status=str(run.get("status") or ""), artifacts=artifacts,
                    continuity=continuity, language=report_language,
                    renderer_error="report_projection_failed",
                )
            except Exception as exc:
                raise ReportReadError("REPORT_PROJECTION_FAILED") from exc
            from .application.reports import ReportReadResult
            from .report_content import trusted_node_is_substantive
            substantive = any(trusted_node_is_substantive(node) for node in safe.nodes)
            report_read = ReportReadResult(
                run_id=run_id, report_contract_version="1",
                report_revision_id=revision_id or None,
                report_input_generation=snapshot.input_generation,
                read_state="partial" if substantive else "diagnostic_only", is_substantive=substantive,
                visible_sections=tuple({"section_id": node["node_id"], "title": node["section_id"],
                    "source_artifact_ids": [node["source_artifact_id"]],
                    "verification_state": node["validation_state"], "is_substantive": trusted_node_is_substantive(node),
                    "is_visible": True, "completeness": "incomplete"} for node in safe.nodes),
                diagnostics=(_report_projection_failure(projection_error, technical=include_technical),),
                markdown=safe.markdown, html=safe.html,
            )
        if report_read.run_id != run_id:
            raise ReportReadError("REPORT_IDENTITY_MISMATCH")
        read_diagnostics.extend(report_read.diagnostics)
        try:
            completion = _report_completion_state(
                run, artifacts, continuity,
                readiness=report_read.document.readiness.to_dict()
                if report_read.document is not None else None,
            )
        except Exception:
            completion = {
                "attempt_complete": str(run.get("status")) in {"completed", "partial", "failed", "cancelled"},
                "research_complete": False,
                "report_readiness": {
                    "state": "unknown", "complete": False,
                    "substantive_sections": [], "missing_sections": [],
                    "missing_stages": [], "issues": ["REPORT_READINESS_UNAVAILABLE"],
                    "recovery_action": "review_run_diagnostics",
                },
                "action_required": True,
            }
            read_diagnostics.append({"code": "REPORT_PROJECTION_FAILED", "location": "readiness"})
        try:
            recovery_description = self._recovery_planner.describe(
                recovery_artifacts, continuity, target="auto",
                request_states=self._recovery_wire_states(run_id),
            )
        except Exception:
            recovery_description = {
                "target": "auto", "stages": [], "reason": "recovery_metadata_unavailable",
                "input_artifact_ids": [], "plan_hash": "", "available": False,
                "error_code": "RECOVERY_METADATA_UNAVAILABLE",
            }
            read_diagnostics.append({"code": "RECOVERY_METADATA_UNAVAILABLE", "location": "recovery-plan"})
        try:
            retryable_synthesis = _report_retryable(artifacts)
            retryable_model_stages = _model_stages_retryable(artifacts, continuity)
            synthesis_error_code = _synthesis_error_code(artifacts)
            retryable_growth = _growth_retryable(artifacts)
        except Exception:
            retryable_synthesis = retryable_model_stages = retryable_growth = False
            synthesis_error_code = ""
            recovery_description = {
                **recovery_description,
                "available": False,
                "stages": [],
                "reason": "recovery_metadata_unavailable",
                "error_code": "RECOVERY_METADATA_UNAVAILABLE",
            }
            read_diagnostics.append({"code": "RECOVERY_METADATA_UNAVAILABLE", "location": "retry-metadata"})

        try:
            call_plan = self.storage.model_call_plan(run_id)
        except Exception:
            call_plan = None
            read_diagnostics.append({"code": "MODEL_PLAN_METADATA_UNAVAILABLE", "location": "model-call-plan"})
        return {
            "run_id": run_id,
            "call_plan": call_plan,
            "ticker": run["ticker"],
            "company_name": run["name"],
            "status": run["status"],
            "report_language": report_language,
            "market": str(company.get("market", "US")),
            "exchange": str(company.get("exchange", "")),
            "listing_currency": str(company.get("listing_currency", "USD")),
            "reporting_currency": str(company.get("reporting_currency") or ""),
            "reporting_currency_confirmation": currency_confirmation,
            "industry_support": str(company.get("industry_support", "standard")),
            "market_snapshot": payload.get("market_snapshot"),
            "reproducibility": {
                "model_configuration": payload.get("model_configuration", {}),
                "research_configuration": payload.get("research_configuration", {}),
                "data_snapshot": payload.get("data_snapshot", {}),
            },
            "retryable_synthesis": retryable_synthesis,
            "retryable_model_stages": retryable_model_stages,
            "synthesis_error_code": synthesis_error_code,
            "retryable_growth": retryable_growth,
            "recovery_plan": recovery_description,
            **completion,
            "report_contract_version": report_read.report_contract_version,
            "report_revision_id": report_read.report_revision_id,
            "report_input_generation": report_read.report_input_generation,
            "report_read_state": report_read.read_state,
            "is_substantive": report_read.is_substantive,
            "visible_sections": list(report_read.visible_sections),
            "content_flow": report_flow(recovery_artifacts, report_read.visible_sections),
            "report_read_diagnostics": read_diagnostics,
            "financial_status": financial_status,
            "continuity": continuity,
            "report_revision": report_revision,
            "markdown": report_read.markdown,
            "html": report_read.html,
        }

    def extend_model_budget(self, run_id: str, expected_digest: str, gap_ids: list[str]) -> dict:
        if (not isinstance(run_id, str) or not run_id or not isinstance(expected_digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_digest)
                or not isinstance(gap_ids, list) or not 1 <= len(gap_ids) <= 100
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in gap_ids)
                or len(set(gap_ids)) != len(gap_ids)):
            raise ValueError("MODEL_BUDGET_APPROVAL_INVALID")
        from .research_execution.call_budget import extend_plan
        try:
            with self.storage.connect() as db:
                return extend_plan(db, run_id, expected_digest, gap_ids)
        except ValueError as error:
            if str(error) in {"MODEL_BUDGET_PLAN_CHANGED", "MODEL_BUDGET_GAP_CHANGED"}:
                raise ServiceConfigurationError(str(error)) from error
            raise

    def financial_diagnostics(self, run_id: str) -> dict[str, Any]:
        """Return a bounded, exportable diagnostic snapshot for one run.

        This endpoint intentionally contains identifiers and recovery metadata
        only.  It never serializes the run payload, report prose, local paths,
        provider configuration, or credentials.
        """
        run = self.storage.get_run(run_id)
        if run is None:
            raise KeyError("research run not found")
        payload = _decode_payload(run.get("payload_json"))
        company = payload.get("company", {})
        if not isinstance(company, dict):
            company = {}
        status = _financial_status(self.storage, company, payload, run_id=run_id)
        company_key = _financial_storage_key(company)
        filing_periods = {
            str(item.accession_number): str(item.period_end or item.fiscal_period)
            for item in self.storage.get_filings(company_key)
        }
        safe_company = {
            key: str(company.get(key, ""))[:160]
            for key in ("ticker", "name", "security_id", "market", "exchange", "reporting_currency")
            if company.get(key) is not None
        }
        cases = []
        for case in status.get("recovery_cases", []):
            cases.append({
                "accession_number": str(case.get("accession_number", ""))[:160],
                "period": str(case.get("period") or case.get("fiscal_period") or filing_periods.get(str(case.get("accession_number", "")), ""))[:40],
                "pages": [int(page) for page in case.get("pages", []) if isinstance(page, int)][:100],
                "fields": [str(field)[:160] for field in case.get("fields", [])][:100],
                "stage": str(case.get("stage", ""))[:120],
                "error_code": FinancialRecoveryController.classify_error(case.get("error_code", "")),
                "attempts": int(case.get("attempts", 0) or 0),
                "status": str(case.get("status", "open"))[:40],
                "next_action": str(case.get("next_action", ""))[:160],
                "freshness": "stale" if status.get("snapshot_stale") else "current",
            })
        market = str(company.get("market", "US"))
        pack = self._financial_source_service.compatibility_summary(market, "annual")
        return {
            "schema": "openthesis.financial-diagnostics.v1",
            "app": {"version": self.app_version, "contract_version": CONTRACT_VERSION},
            "run": {"run_id": str(run_id), "status": str(run.get("status", ""))},
            "company": safe_company,
            "financial_status": {
                key: status.get(key)
                for key in ("state", "expected_periods", "available_periods", "missing_periods",
                            "unverified_periods", "attempt_count", "last_stage", "updated_at",
                            "next_action", "snapshot_stale", "quality_summary")
            },
            "recovery_cases": cases,
            "issues": [
                {key: issue.get(key) for key in ("period", "stage", "code", "status", "quality_class", "next_action")}
                for issue in status.get("issues", [])
            ],
            "active_compatibility_pack": pack,
        }

    def refresh_financial_report(
        self, run_id: str, *, language: str | None = None
    ) -> dict[str, Any]:
        """Rebuild only deterministic report artifacts from stored facts.

        This endpoint intentionally has no discovery, download, parser,
        provider, or model path. It is used after financial data succeeded but
        the report projection/refresh failed, so retrying it cannot duplicate
        external work or reinterpret a filing.
        """
        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        payload = _decode_payload(stored.get("payload_json"))
        company_payload = payload.get("company", {})
        if not isinstance(company_payload, dict):
            raise _FinancialReportRefreshError(
                {"status": "failed", "error": "FILING_REPORT_REFRESH_FAILED"}
            )
        try:
            company = Company(**company_payload)
            updated = self._rebuild_financial_artifacts(run_id, payload, company)
            if not updated:
                raise RuntimeError("no accepted financial facts")
            report = self.get_report(run_id, language=language)
        except _FinancialReportRefreshError:
            raise
        except Exception as exc:
            raise _FinancialReportRefreshError(
                {"status": "failed", "error": "FILING_REPORT_REFRESH_FAILED"}
            ) from exc
        report["financial_report_refresh"] = {
            "status": "succeeded",
            "updated_artifacts": list(updated),
        }
        return report

    def retry_financials(
        self, run_id: str, *, force: bool = False,
        progress: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        _vision_job: _ResearchJob | None = None,
    ) -> dict[str, Any]:
        """Capture authorized collaborators and delegate the model-free repair."""

        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        payload = _decode_payload(stored.get("payload_json"))
        company_payload = payload.get("company")
        if not isinstance(company_payload, dict):
            raise ValueError("saved company is invalid")
        company = Company(**{**company_payload, "reporting_currency": str(company_payload.get("reporting_currency") or "")})
        request, on_progress = self._capture_financial_repair_request(
            company, payload, force=force, progress=progress, run_id=run_id, vision_job=_vision_job)
        try:
            retry_result = self._financial_repairer.repair(request, progress=on_progress, cancel_check=cancel_check)
        except CancelledError as exc:
            raise ResearchCancelled() from exc
        try:
            report = self.get_report(run_id, language=str(payload.get("report_language", "zh-CN")))
        except Exception as first_refresh_error:
            # Deterministic report refreshes are safe to retry once.  Keep the
            # retry bounded and model-free; a second failure remains a distinct
            # report-refresh error instead of being presented as success.
            try:
                time.sleep(0.05)
                report = self.get_report(run_id, language=str(payload.get("report_language", "zh-CN")))
            except Exception as exc:
                del first_refresh_error
                self.storage.record_financial_retry_attempt(
                    _financial_storage_key(company.to_dict()),
                    stage="report-refresh",
                    error="FILING_REPORT_REFRESH_FAILED",
                )
                failed_result = replace(
                    retry_result, status="partial" if retry_result.accepted else "failed",
                    error="FILING_REPORT_REFRESH_FAILED",
                )
                raise _FinancialReportRefreshError(failed_result.to_dict()) from exc
        report["financial_retry"] = retry_result.to_dict()
        return report

    def start_financial_retry(
        self, run_id: str, *, force: bool = False, confirmed: bool = False
    ) -> dict[str, Any]:
        """Start a non-model financial repair without occupying the RPC loop."""

        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        if force and confirmed is not True:
            raise ValueError("explicit confirmation is required")
        if str(stored.get("status", "")) in {
            RunStatus.CREATED.value, RunStatus.RUNNING.value,
        }:
            raise ValueError("an active research run cannot rebuild financial cache")
        job = _ResearchJob(
            job_id=uuid.uuid4().hex,
            run_id=run_id,
            stage="filing-discovery",
        )
        with self._jobs_lock:
            self._jobs[job.job_id] = job
        threading.Thread(
            target=self._run_financial_retry_job,
            args=(job, run_id, force),
            name=f"openthesis-financial-{job.job_id[:8]}",
            daemon=True,
        ).start()
        with self._jobs_lock:
            return job.visible_snapshot()

    def _run_financial_retry_job(
        self, job: _ResearchJob, run_id: str, force: bool
    ) -> None:
        # Preference reads are SQLite-backed and can be delayed by antivirus
        # hooks on Windows.  Resolve them after returning control to the RPC
        # loop so retry/cancel/status controls never wait on that I/O.
        try:
            language = normalize_language(
                self.preferences().get("ui_language", job.ui_language)
            )
        except Exception:
            language = job.ui_language
        job.ui_language = language
        self._update_job(
            job, state="running", stage="filing-discovery", percent=3,
            message=_ui_message(
                language,
                "Discovering official financial reports…",
                "正在查找官方财报……",
                "正在查找官方財報……",
            ),
        )

        def update(stage: str, current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise ResearchCancelled()
            stage_percent = {
                "filing-discovery": 8,
                "filing-download": 15,
                "filing-parse": 45,
                "filing-validation": 75,
                "artifact-rebuild": 92,
            }.get(stage, job.percent)
            progress = stage_percent
            if total > 0 and stage in {"filing-download", "filing-parse", "filing-validation"}:
                progress = min(95, stage_percent + round(15 * current / total))
            messages = {
                "filing-discovery": _ui_message(language, "Discovering official financial reports…", "正在查找官方财报……", "正在查找官方財報……"),
                "filing-download": _ui_message(language, "Downloading required financial reports…", "正在下载缺失财报……", "正在下載缺失財報……"),
                "filing-parse": _ui_message(language, "Reading financial report data…", "正在读取财报数据……", "正在讀取財報資料……"),
                "filing-validation": _ui_message(language, "Validating periods, scope and currency…", "正在校验期间、口径与币种……", "正在校驗期間、口徑與幣種……"),
                "artifact-rebuild": _ui_message(language, "Updating deterministic financial report…", "正在更新确定性财务报告……", "正在更新確定性財務報告……"),
            }
            self._update_job(
                job, stage=stage, stage_current=current, stage_total=total,
                percent=progress, message=messages.get(stage, job.message),
            )

        try:
            report = self.retry_financials(
                run_id, force=force, progress=update,
                cancel_check=job.cancel_event.is_set,
                _vision_job=job,
            )
            retry = report.get("financial_retry", {})
            operation_status = str(retry.get("status", "failed"))
            succeeded = operation_status == "succeeded"
            partial = operation_status == "partial"
            completed = succeeded or (partial and retry.get("research_can_continue") is True
                and retry.get("updated_artifacts") and not any(code in str(retry.get("error", ""))
                for code in ("FILING_REPORT_REFRESH_FAILED", "FINANCIAL_REPAIR_INTERNAL_ERROR",
                             "REPORT_REVISION_CONFLICT", "FINANCIAL_GENERATION_CONFLICT",
                             "RESEARCH_RUN_SNAPSHOT_CONFLICT")))
            # The use case owns classification. Do not relabel a local host,
            # configuration or source outage as financial inconsistency.
            classified = continuity_financial_outcome({"latest": {
                "stage": "data-unavailable", "diagnostics": {
                    "recovery_outcome": retry.get("recovery_outcome")}}})
            recovery = dict(retry["recovery_outcome"]) if classified else None
            failure_code = (recovery["diagnostic_code"] if recovery else
                "FILING_DATA_QUALITY_PARTIAL" if operation_status == "partial" else
                "FILING_DATA_QUALITY_FAILED")
            self._update_job(
                job,
                state="completed" if completed else "failed",
                stage="completed" if completed else "financial-validation",
                percent=100,
                operation_result=dict(retry) if isinstance(retry, dict) else {},
                recovery_outcome=recovery,
                error_code=None if completed else failure_code,
                message=(
                    _ui_message(language, "Financial refresh completed", "财务数据刷新完成", "財務資料刷新完成")
                    if succeeded
                    else _ui_message(language,
                        "Financial refresh completed with localized verification gaps; research can continue",
                        "财报更新完成，部分指标待核验，可继续研究",
                        "財報更新完成，部分指標待核驗，可繼續研究") if completed
                    else _research_data_message(failure_code, language)
                ),
            )
        except ResearchCancelled:
            self._update_job(
                job, state="cancelled", stage="cancelled",
                message=_ui_message(language, "Financial refresh cancelled", "财务数据刷新已取消", "財務資料刷新已取消"),
            )
        except _FinancialReportRefreshError as exc:
            self._update_job(
                job, state="failed", stage="report-refresh",
                error_code="FILING_REPORT_REFRESH_FAILED",
                run_id=run_id,
                percent=100,
                operation_result=exc.result,
                message=_ui_message(
                    language,
                    "Financial refresh succeeded, but the report refresh failed",
                    "财务资料已重建，但报告刷新失败",
                    "財務資料已重建，但報告刷新失敗",
                ),
            )
        except Exception as exc:
            self._update_job(
                job, state="failed", stage="failed", error_code="FILING_RETRY_FAILED",
                message=type(exc).__name__,
            )

    def rebuild_financials(
        self, run_id: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
        """Force a safe full refresh while preserving the last good cache.

        New downloads and parser output replace each filing only after that
        filing succeeds. A failed rebuild therefore leaves the prior auditable
        cache available instead of deleting it before network work begins.
        """

        if confirmed is not True:
            raise ValueError("explicit confirmation is required")
        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        if str(stored.get("status", "")) in {
            RunStatus.CREATED.value,
            RunStatus.RUNNING.value,
        }:
            raise ValueError("an active research run cannot rebuild financial cache")
        return self.retry_financials(run_id, force=True)

    def _rebuild_financial_artifacts(
        self,
        run_id: str,
        payload: dict[str, Any],
        company: Company,
    ) -> tuple[str, ...]:
        """Compatibility wrapper; `payload` is retained for RPC callers."""
        return self._financial_artifact_refresher.refresh(run_id, company)

    def _retry_market_financials(
        self, company: Company, payload: dict[str, Any], *, force: bool = False,
        trace: dict[str, set[str]] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        run_id: str = "", vision_job: _ResearchJob | None = None,
    ) -> list[str]:
        return self._retry_official_financials(company, payload, force=force,
            trace=trace, progress=progress, cancel_check=cancel_check,
            run_id=run_id, vision_job=vision_job)

    def _retry_us_financials(
        self, company: Company, payload: dict[str, Any], *, force: bool = False,
        trace: dict[str, set[str]] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        run_id: str = "",
    ) -> list[str]:
        return self._retry_official_financials(company, payload, force=force,
            trace=trace, progress=progress, cancel_check=cancel_check, run_id=run_id)

    def _capture_financial_repair_request(
        self, company: Company, payload: dict[str, Any], *, force: bool = False,
        progress: Callable[[str, int, int], None] | None = None,
        run_id: str = "", vision_job: _ResearchJob | None = None,
    ) -> tuple[OfficialFinancialRequest, Callable[..., None]]:
        # Composition only: capture preferences, source and authorized page
        # identity. Target selection and acceptance belong to the use case.
        configuration = payload.get("research_configuration", {})
        history = _research_history_years({"evidence_policy": {
            "annual_history_years": configuration.get("annual_history_years", 5)
        }} if isinstance(configuration, dict) else {})
        filing_hashes: set[str] = set()
        sources, vision_adapter, vision_config = self._financial_recognition_capabilities(
            company, (), job=vision_job, run_id=run_id,
            authorized_filing_hashes=filing_hashes)
        if company.market == "US":
            preferences = self.preferences()
            adapter = DeferredOfficialSource(lambda: self._sec_client_factory(
                build_sec_user_agent(_normalize_sec_profile(preferences["sec_contact_profile"]),
                                     preferences["sec_contact_email"]),
                self.storage.data_dir / "sec-cache"))
            sources = (SecFinancialSourceAdapter(adapter),)
        else:
            adapter = self._disclosure_service.adapter_for(company)
            language_setter = getattr(adapter, "set_preferred_language", None)
            if callable(language_setter):
                language_setter(str(payload.get("report_language") or "zh-Hant"))
        notify = progress or (lambda *_args: None)
        def on_progress(stage, current, total, detail=None):
            if stage == "filing-acquisition-complete":
                filing_hashes.update(item.content_hash for item in detail or () if item.content_hash)
                return
            notify(stage, current, total)
        return (OfficialFinancialRequest(company, adapter, candidate_sources=tuple(sources),
            annual_history_years=history, force=force, repair=True, run_id=run_id,
            recovery_targets=frozenset() if force else frozenset(
                str(item) for item in payload.get("_financial_recovery_targets", ()) if str(item)),
            vision_fallback=vision_adapter, vision_config=vision_config,
            authorized_source_stages=(frozenset({GapStageKind.MINERU})
                if vision_config is not None and vision_config.enabled else frozenset())), on_progress)

    def _retry_official_financials(
        self, company: Company, payload: dict[str, Any], *, force: bool = False,
        trace: dict[str, Any] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        run_id: str = "", vision_job: _ResearchJob | None = None,
    ) -> list[str]:
        """Legacy candidate-only seam, sharing the production preparer."""
        request, on_progress = self._capture_financial_repair_request(
            company, payload, force=force, progress=progress, run_id=run_id, vision_job=vision_job)
        try:
            result = self._official_financial_preparer.prepare(
                request, cancel_check=cancel_check, progress=on_progress)
        except CancelledError as exc:
            raise ResearchCancelled() from exc
        except OfficialFinancialUnavailable as exc:
            if trace is not None:
                trace["recovery_outcome"] = exc.outcome.to_dict()
                trace.setdefault("terminal_state", set()).add(exc.outcome.state.value)
                trace.setdefault("next_action", set()).update(exc.outcome.next_actions)
                trace.setdefault("diagnostics", set()).update(exc.diagnostics)
            return [exc.code]
        errors = list(result.errors)
        pending = {accession for accession, state in (result.publication or {}).get("accessions", {}).items()
                   if state != "promoted"}
        if trace is not None:
            trace["recovery_outcome"] = result.recovery_outcome.to_dict()
            trace.setdefault("targets", set()).update(result.target_accessions)
            trace.setdefault("downloaded", set()).update(result.downloaded_accessions)
            trace.setdefault("processed", set()).update(
                set(result.view.publishable_accessions) - pending)
            trace.setdefault("diagnostics", set()).update((*result.discovery.diagnostics, *result.view.diagnostics))
            trace.setdefault("freshness", set()).add(result.discovery.freshness)
            trace.setdefault("terminal_state", set()).add(
                result.recovery_outcome.state.value if errors else result.discovery.state.value)
            if result.recovery_outcome.next_actions:
                trace.setdefault("next_action", set()).update(result.recovery_outcome.next_actions)
            elif result.discovery.next_action:
                trace.setdefault("next_action", set()).add(result.discovery.next_action)
        return errors


    def _recovery_wire_states(self, run_id: str) -> tuple[dict, ...]:
        with self.storage.connect() as db:
            return read_wire_states(db, run_id, time.time())

    def _execute_recovery_plan(
        self,
        run_id: str,
        *,
        target: str,
        operation: Callable[[Any, str], dict[str, Any]],
        model: dict[str, Any] | None = None,
        plan_hash: str | None = None,
        confirm_new_attempt: bool = False,
    ) -> dict[str, Any]:
        """Execute one planner-owned recovery and coalesce duplicate clicks."""

        artifacts = self.storage.get_artifacts(run_id)
        plan = self._recovery_planner.plan(
            artifacts, self._continuity.snapshot(run_id), target=target,
            request_states=self._recovery_wire_states(run_id),
        )
        if not plan.available:
            raise ValueError("MODEL_STAGE_RESUME_NOT_AVAILABLE")
        if plan_hash and plan_hash != plan.plan_hash:
            raise ValueError("MODEL_RECOVERY_PLAN_STALE")
        key = (run_id, plan.plan_hash)
        model_identity = json.dumps(
            {
                "configured_model_id": str((model or {}).get("configured_model_id", "")),
                "connection_id": str((model or {}).get("connection_id", "")),
                "configuration_version": int((model or {}).get("configuration_version", 0) or 0),
                "confirm_new_attempt": confirm_new_attempt,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) if isinstance(model, dict) else "saved-model"
        with self._jobs_lock:
            active = self._active_recoveries.get(key)
            owner = active is None
            if owner:
                active = threading.Event()
                self._active_recoveries[key] = (active, model_identity)
            elif active[1] != model_identity:
                raise ValueError("MODEL_RECOVERY_ALREADY_RUNNING")
        assert active is not None
        if not owner:
            # The first request owns all provider calls.  A duplicate waits
            # for the same durable run result rather than starting a second
            # stage attempt.
            active[0].wait()
            return self.get_report(run_id)
        configuration = model if isinstance(model, dict) else {}
        diagnostics = {
            **plan.to_dict(),
            "configured_model_id": str(configuration.get("configured_model_id", ""))[:128],
            "configuration_version": int(configuration.get("configuration_version", 0) or 0),
        }
        try:
            authorization_id = ""
            if confirm_new_attempt:
                if not isinstance(model, dict) or not model:
                    raise ValueError("MODEL_NEW_ATTEMPT_EXPLICIT_MODEL_REQUIRED")
                config = _model_config_from_request(model)
                if not config.enabled:
                    raise ValueError("an enabled model is required")
                authorization_id = authorize_new_attempt(self.storage, run_id, plan.stages,
                                                        config.public_id, plan.plan_hash)
            result = operation(plan, authorization_id)
            self._continuity.checkpoint(
                run_id, "model-recovery", StageOutcome.COMPLETE,
                diagnostics=diagnostics,
            )
            return result
        except ProviderError as exc:
            self._continuity.checkpoint(
                run_id,
                "model-recovery",
                StageOutcome.WAITING_RETRYABLE if exc.retryable else StageOutcome.NEEDS_ACTION,
                error_code=exc.code,
                diagnostics=diagnostics,
            )
            raise
        except Exception as exc:
            self._continuity.checkpoint(
                run_id, "model-recovery", StageOutcome.NEEDS_ACTION,
                error_code=str(getattr(exc, "code", type(exc).__name__))[:80],
                diagnostics=diagnostics,
            )
            raise
        finally:
            with self._jobs_lock:
                event, _identity = self._active_recoveries.pop(key, (active, model_identity))
                event.set()

    def retry_research_synthesis(
        self, run_id: str, model: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._execute_recovery_plan(
            run_id,
            target="synthesis",
            model=model,
            operation=lambda _plan, _authorization: self._retry_research_synthesis_once(run_id, model),
        )

    def retry_research_model_stages(
        self, run_id: str, model: dict[str, Any] | None = None, *, budget_only: bool = False
    ) -> dict[str, Any]:
        if type(budget_only) is not bool:
            raise ValueError("RESEARCH_BUDGET_RESUME_INVALID")
        if budget_only:
            with self.storage.connect() as db:
                if not db.execute("SELECT 1 FROM model_budget_gaps WHERE run_id=? AND state='approved'", (run_id,)).fetchone():
                    raise ValueError("MODEL_BUDGET_APPROVAL_REQUIRED")
            return self._retry_research_model_stages_once(run_id, model, budget_only=True)
        return self._execute_recovery_plan(
            run_id,
            target="model-stages",
            model=model,
            operation=lambda plan, _authorization: self._retry_research_model_stages_once(
                run_id, model, resume_target=plan.target
            ),
        )

    def retry_research_growth(
        self, run_id: str, model: dict[str, Any]
    ) -> dict[str, Any]:
        return self._execute_recovery_plan(
            run_id,
            target="growth",
            model=model,
            operation=lambda plan, _authorization: self._retry_research_model_stages_once(
                run_id, model, resume_target=plan.target
            ),
        )

    def retry_research_stage(
        self,
        run_id: str,
        target: str,
        model: dict[str, Any] | None,
        plan_hash: str | None = None,
        confirm_new_attempt: bool = False,
    ) -> dict[str, Any]:
        """Resume exactly one planner-selected stage chain with an explicit model."""
        if target not in {
            "model-stages", "growth", "counter-analysis",
            "forecast-scenarios", "synthesis",
        }:
            raise ValueError("MODEL_RECOVERY_TARGET_INVALID")
        if type(confirm_new_attempt) is not bool:
            raise ValueError("MODEL_NEW_ATTEMPT_CONFIRMATION_INVALID")
        if confirm_new_attempt and not plan_hash:
            raise ValueError("MODEL_NEW_ATTEMPT_PLAN_REQUIRED")
        operation = lambda plan, authorization: self._retry_research_model_stages_once(
            run_id, model, resume_target=plan.target, manual_authorization_id=authorization
        )
        return self._execute_recovery_plan(
            run_id,
            target=target,
            model=model,
            plan_hash=plan_hash,
            confirm_new_attempt=confirm_new_attempt,
            operation=operation,
        )

    def _retry_research_synthesis_once(
        self, run_id: str, model: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._retry_research_model_stages_once(run_id, model, resume_target="synthesis")

    def revalidate_saved_research(self, run_id: str) -> dict[str, Any]:
        """Explicit zero-provider action; does not clear unknown wire history."""
        if any(item.get("active") for item in self._recovery_wire_states(run_id)):
            raise ValueError("MODEL_REQUEST_IN_PROGRESS")
        return self._retry_research_model_stages_once(run_id, local_only=True)

    def _retry_research_model_stages_once(
        self, run_id: str, model: dict[str, Any] | None = None,
        *, resume_target: str = "model-stages",
        manual_authorization_id: str = "",
        local_only: bool = False,
        budget_only: bool = False,
    ) -> dict[str, Any]:
        """Resume failed base agents without repeating previously trusted work.

        This seam deliberately reuses canonical facts and cached qualitative
        evidence. It revalidates cached canonical facts but performs no new
        official discovery, download or PDF recognition. Persisted successful base-agent artifacts are
        supplied to the workflow so only failed/missing roles are called again;
        dependent analysis is then refreshed from the rebuilt dossier.
        """

        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        artifacts = self.storage.get_artifacts(run_id)
        payload = _decode_payload(stored.get("payload_json"))
        company_payload = payload.get("company")
        if not isinstance(company_payload, dict):
            raise ValueError("saved company is invalid")
        model_reference = model if isinstance(model, dict) and model else payload.get("model_configuration")
        config = _model_config_from_request(model_reference) if model_reference else ModelConfig()
        if not config.enabled and not local_only:
            raise ValueError("an enabled model is required")
        company = Company(**{**company_payload, "reporting_currency": str(company_payload.get("reporting_currency") or "")})
        retry_issues: list[dict[str, str]] = []
        retry_facts = _canonical_retry_snapshot(self.storage, company, payload,
                                               partial_issues=retry_issues)
        filings = self.storage.get_filings(_financial_storage_key(company.to_dict()))
        filing_evidence = []
        if resume_target != "synthesis":
            saved_evidence = _saved_filing_evidence(artifacts, filings, company_cik=company.cik)
            current_evidence = _cached_filing_evidence(self.storage, filings, cache_only=local_only)
            filing_evidence = list({str(item.get("evidence_id")): item
                for item in [*saved_evidence, *current_evidence]}.values())
        # A stale/missing numeric snapshot removes only quantitative inputs.
        # It does not manufacture new facts or veto independent disclosure
        # analysis. All fresh content still enters the normal stage verifier.
        for issue in retry_issues:
            filing_evidence.append({"evidence_id": "gap:resume:" + issue["code"],
                "kind": "material_gap", "topic": "financial_snapshot",
                "reason": issue["code"], "raw_text": "",
                "next_action": "repair_financial_snapshot"})
        research_configuration = payload.get("research_configuration", {})
        research_configuration = research_configuration if isinstance(research_configuration, dict) else {}
        run = ResearchRun(
            run_id=run_id,
            company=company,
            workflow_id=str(payload.get("workflow_id", "long-term-fundamentals")),
            research_pack_id=str(payload.get("research_pack_id", "")),
            research_pack_version=str(payload.get("research_pack_version", "")),
            # The run envelope is the persisted publication identity.  A
            # newly selected resume model travels only in ResearchResumeRequest
            # and is bound by the workflow after the input CAS succeeds.
            provider_id=str(payload.get("provider_id", "none")),
            model_id=str(payload.get("model_id", "")),
            data_as_of=str(payload.get("data_as_of", date.today().isoformat())),
            status=RunStatus(str(stored.get("status", "partial"))),
            started_at=str(payload.get("started_at", stored.get("started_at", utc_now_iso()))),
            completed_at=stored.get("completed_at"),
            errors=[*list(payload.get("errors", [])), *[item["code"] for item in retry_issues]],
            report_language=normalize_language(str(payload.get("report_language", "zh-CN"))),
            market_snapshot=payload.get("market_snapshot"),
            model_configuration=dict(payload.get("model_configuration") or {}),
            research_configuration=dict(research_configuration),
            data_snapshot=dict(payload.get("data_snapshot", {})),
        )
        self._research_orchestrator.resume(ResearchResumeRequest(
            run=run, selected_pack=self._select_pack(run.research_pack_id), model=config,
            facts=tuple(fact.to_dict() for fact in retry_facts), artifacts=tuple(artifacts),
            filing_evidence=tuple(filing_evidence), target=resume_target,
            ui_language=normalize_language(self.preferences().get("ui_language", "zh-CN")),
            parallel_agents=bool(research_configuration.get("parallel_agents", True)),
            manual_authorization_id=manual_authorization_id,
            local_only=local_only,
            budget_only=budget_only,
        ))
        return self.get_report(run_id, language=run.report_language)

    def _retry_research_growth_once(
        self, run_id: str, model: dict[str, Any]
    ) -> dict[str, Any]:
        return self._retry_research_model_stages_once(run_id, model, resume_target="growth")

    def preview_market_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fetch an optional preflight quote without creating a research run.

        The desktop invokes this independently and debounces/cancels stale
        requests.  It deliberately returns the normalized audit shape only.
        """
        company = _company_from_request(payload.get("company"))
        values = payload.get("valuation") if isinstance(payload.get("valuation"), dict) else {}
        policy = ReverseDcfPolicy(
            horizon_years=int(values.get("horizon_years", 5) or 5),
            discount_rate=float(values.get("discount_rate_percent", 10) or 10) / 100,
            terminal_growth=float(values.get("terminal_growth_percent", 3) or 3) / 100,
        )
        return self._disclosure_service.capture(company, policy).to_dict()

    def start_research(self, request: dict[str, Any]) -> dict[str, Any]:
        mode = request.get("mode")
        if mode not in {"demo", "company"}:
            raise ValueError("unsupported research mode")
        if mode == "company":
            _company_from_request(request.get("company"))
        primary_config = _model_config_from_request(request.get("model", {}))
        comparison_configs = (
            _comparison_configs_from_request(request.get("comparison_models"))
            if request.get("compare_enabled")
            else []
        )
        if request.get("compare_enabled") and not comparison_configs:
            raise ValueError("comparison requires at least one configured model")
        if comparison_configs and not primary_config.enabled:
            raise ValueError("comparison requires an enabled primary model")

        company = DEMO_COMPANY if mode == "demo" else _company_from_request(request.get("company"))
        if mode == "company":
            try:
                company = self._company_search.require_identity(company,
                    lambda: self._fetch_official_companies(company.ticker, normalize_market(company.market)))
            except OfficialNetworkError as error:
                raise ServiceConfigurationError(error.code) from error
        selected_pack = self._select_pack(str(request.get("pack_id", "")))
        report_language = normalize_language(self.preferences().get("report_language", "zh-CN"))
        self.storage.save_company(company)
        run = ResearchRun(
            run_id=uuid.uuid4().hex,
            company=company,
            workflow_id="complete-fundamental-research",
            research_pack_id=selected_pack.pack_id,
            research_pack_version=selected_pack.version,
            provider_id=primary_config.provider,
            model_id=primary_config.model,
            data_as_of=utc_now_iso(),
            status=RunStatus.CREATED,
            report_language=report_language,
            model_configuration={
                "configured_model_id": primary_config.configured_model_id,
                "configuration_version": primary_config.configuration_version,
                "role": primary_config.role,
            },
            research_configuration={
                "report_language": report_language,
                "research_pack_id": selected_pack.pack_id,
                "research_pack_version": selected_pack.version,
                "research_pack_content_identity": selected_pack.content_hash,
                "annual_history_years": _research_history_years(request, selected_pack),
            },
        )
        self._continuity.start(run)
        job = _ResearchJob(
            job_id=uuid.uuid4().hex,
            ui_language=normalize_language(self.preferences().get("ui_language", "zh-CN")),
            run_id=run.run_id,
            run=run,
        )
        with self._jobs_lock:
            self._jobs[job.job_id] = job
        request_payload = dict(request)
        request_payload["company"] = company.to_dict()
        request_payload["_financial_recovery_session_id"] = run.run_id
        self.storage.save_research_job(job.snapshot())
        threading.Thread(
            target=self._run_research,
            args=(job, request_payload),
            name=f"openthesis-research-{job.job_id[:8]}",
            daemon=True,
        ).start()
        with self._jobs_lock:
            return job.visible_snapshot()

    def get_research_status(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            snapshot = job.visible_snapshot() if job is not None else None
        if snapshot is None:
            snapshot = self.storage.get_research_job(job_id)
            if snapshot is None:
                raise KeyError("research job not found")
        progress = self.storage.model_call_progress(snapshot.get("run_id") or "")
        # Storage work is outside the job lock. Re-copy the latest memory
        # revision rather than returning an older running/cancel state.
        with self._jobs_lock:
            current = self._jobs.get(job_id)
            if current is not None:
                snapshot = current.visible_snapshot()
        snapshot["model_calls"] = progress["calls"]
        snapshot["call_plan"] = self.storage.model_call_plan(snapshot.get("run_id") or "")
        snapshot["status_revision"] = f"{snapshot.get('revision', 0)}:{progress['revision']}"
        return snapshot

    def cancel_model_call(self, job_id: str, request_id: str, attempt_id: str) -> dict:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in {"queued", "running"}:
                raise ValueError("MODEL_CALL_NOT_ACTIVE")
            accepted = self.storage.model_activity.request_cancel(job.run_id, request_id, attempt_id)
            if not accepted:
                raise ValueError("MODEL_CALL_NOT_ACTIVE")
        # This only stops local waiting. Remote billing/termination is unknown.
        return self.get_research_status(job_id)

    def cancel_research(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("research job not found")
            job.cancel_event.set()
            job.revision += 1
            if job.vision_approval_pending:
                # Wake an approval waiter immediately; the callback checks the
                # cancellation flag before allowing any upload.
                job.vision_approval_event.set()
            if job.state in {"queued", "running"}:
                job.generation += 1
                job.state = "cancelling"
                job.cancel_requested = True
                job.stage = "cancelling"
                job.message = _ui_message(
                    job.ui_language,
                    "Stopping unfinished agents…",
                    "正在停止未完成的 Agent…",
                    "正在停止未完成的 Agent…",
                )
                for agent_id, state in list(job.agent_states.items()):
                    if state in {"queued", "running"}:
                        job.agent_states[agent_id] = "cancelled"
            return job.visible_snapshot()

    def vision_decision(self, job_id: str, approved: bool) -> dict[str, Any]:
        """Resolve a session-only failed-page upload approval request."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("research job not found")
            if job.stage != "vision-approval" or not job.vision_approval_pending:
                raise ValueError("vision upload approval is not pending")
            job.vision_approval = bool(approved)
            job.revision += 1
            job.vision_approval_pending = False
            job.vision_approval_event.set()
            return job.visible_snapshot()

    def _update_job(
        self,
        job: _ResearchJob,
        *,
        active_only: bool = False,
        expected_generation: int | None = None,
        **updates: Any,
    ) -> None:
        with self._jobs_lock:
            if expected_generation is not None and job.generation != expected_generation:
                return
            if active_only and (job.cancel_event.is_set() or job.state not in {"queued", "running"}):
                return
            if updates.get("state") in {"completed", "failed", "cancelled"} and job.terminal_write_revision is None:
                job.pre_terminal_snapshot = job.snapshot()
            if "percent" in updates:
                updates["percent"] = max(job.percent, min(100, max(0, int(updates["percent"]))))
            if updates.get("stage_total") is not None:
                updates["stage_total"] = max(0, int(updates["stage_total"]))
            if updates.get("stage_current") is not None:
                current = max(0, int(updates["stage_current"]))
                total = updates.get("stage_total", job.stage_total)
                updates["stage_current"] = min(current, int(total)) if total is not None else current
            if "stage" in updates and updates["stage"] != job.stage:
                now = time.perf_counter()
                job.roll_timing(str(updates["stage"]), now)
                job.stage_timings[job.stage] = job.stage_timings.get(job.stage, 0.0) + max(
                    0.0, now - job.stage_started_at
                )
                job.stage_started_at = now
                updates.setdefault("stage_current", None)
                updates.setdefault("stage_total", None)
            if updates.get("state") in {"completed", "failed", "cancelled"}:
                job.finished_at = time.perf_counter()
            for key, value in updates.items():
                setattr(job, key, value)
            job.revision += 1
            snapshot = job.snapshot()
            terminal = snapshot["state"] in {"completed", "failed", "cancelled"}
            if terminal:
                job.terminal_write_revision = snapshot["revision"]
        self.storage.save_research_job(snapshot)
        if terminal:
            with self._jobs_lock:
                if job.terminal_write_revision == snapshot["revision"]:
                    job.terminal_write_revision = None
                    job.pre_terminal_snapshot = None

    def _ingestion_progress(
        self,
        job: _ResearchJob,
        stage: str,
        current: int,
        total: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        safe_total = max(1, int(total))
        safe_current = min(safe_total, max(0, int(current)))
        if stage == "cache-check":
            percent = 16 + round(safe_current * 2 / safe_total)
        elif stage == "filing-index":
            percent = 16 + round(safe_current * 2 / safe_total)
        elif stage == "filing-parse":
            percent = 18 + round(safe_current * 6 / safe_total)
        elif stage == "filing-validation":
            percent = 24 + round(safe_current * 5 / safe_total)
        else:
            percent = max(job.percent, 24)
        display_stage = job.stage if stage == "filing-status" else stage
        updates: dict[str, Any] = {
            "stage": display_stage,
            "stage_current": safe_current,
            "stage_total": safe_total,
            "percent": max(job.percent, percent),
        }
        if detail and detail.get("filing_id"):
            filing_states = {key: dict(value) for key, value in job.filing_states.items()}
            filing_id = str(detail["filing_id"])
            filing_states[filing_id] = {
                **filing_states.get(filing_id, {}),
                "filing_id": filing_id,
                "label": str(detail.get("label") or filing_id),
                "status": str(detail.get("status") or stage),
                "error_code": str(detail.get("error_code") or ""),
                "elapsed_seconds": round(float(detail.get("elapsed_seconds") or 0.0), 3),
                "window_index": detail.get("window_index"),
                "window_total": detail.get("window_total"),
                "last_activity_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            for field in ("processing_state", "availability", "roles", "candidate_count", "admitted_count",
                          "quarantined_count", "issue_codes", "source_hash", "accession_number", "period_end", "fiscal_period",
                          "page_count", "native_text_page_count", "indexed_page_count", "window_total",
                          "completed_window_count", "parse_elapsed_seconds", "failure_stage", "root_error_code",
                          "attempt_count", "cache_reused", "recognized_statements", "extracted_pages"):
                if field in detail:
                    filing_states[filing_id][field] = deepcopy(detail[field])
            updates["filing_states"] = filing_states
        self._update_job(job, **updates)

    def _run_research(self, job: _ResearchJob, request: dict[str, Any]) -> None:
        progress_generation = job.generation
        preferences = self.preferences()
        ui_language = normalize_language(preferences["ui_language"])
        report_language = normalize_language(preferences["report_language"])
        secrets = _request_secrets(request)
        company: Company | None = None
        if job.cancel_event.is_set():
            self._update_job(
                job,
                state="cancelled",
                stage="cancelled",
                message=_ui_message(ui_language, "Research cancelled", "研究已取消", "研究已取消"),
            )
            return
        self._update_job(
            job,
            active_only=True,
            state="running",
            stage="preparing",
            message=_ui_message(ui_language, "Preparing research data", "正在准备研究数据", "正在準備研究資料"),
            percent=2,
        )
        # Batch vision consent is deliberately scoped to this invocation and
        # to the exact downloaded/selected filing hashes populated below.
        # Keeping this state in the run closure prevents it from becoming a
        # persisted or reusable application-wide authorization.
        vision_filing_hashes: set[str] = set()
        config = ModelConfig()
        try:
            mode = request.get("mode")
            company = (
                DEMO_COMPANY
                if mode == "demo"
                else _company_from_request(request.get("company"))
            )
            company_market = normalize_market(company.market)
            market_profile = MARKET_PROFILES[company_market]
            DEFAULT_MARKET_CAPABILITIES.require(company_market.value)
            selected_pack = self._select_pack(str(request.get("pack_id", "")))
            history_years = _research_history_years(request, selected_pack)
            self._update_job(
                job,
                stage="company-profile",
                percent=3,
                market=company_market.value,
                disclosure_url=market_profile.disclosure_home,
            )
            config = _model_config_from_request(request.get("model", {}))
            compare_enabled = bool(request.get("compare_enabled"))
            comparison_configs = (
                _comparison_configs_from_request(request.get("comparison_models"))
                if compare_enabled
                else []
            )
            parallel_agents = _request_bool(
                request.get("parallel_agents"),
                preferences.get("parallel_agents", "true") == "true",
            )
            manual_market_snapshot = _market_snapshot(
                request.get("market_snapshot"),
                company,
            )
            valuation_inputs = _valuation_inputs(request.get("valuation"))
            policy_values = request.get("valuation") if isinstance(request.get("valuation"), dict) else {}
            valuation_policy = ReverseDcfPolicy(
                horizon_years=int(policy_values.get("horizon_years", 5) or 5),
                discount_rate=float(policy_values.get("discount_rate_percent", 10) or 10) / 100,
                terminal_growth=float(policy_values.get("terminal_growth_percent", 3) or 3) / 100,
            )
            # Settings is the sole current authorization source.  A historical
            # or legacy request payload may be retained for audit, but cannot
            # reactivate cloud page uploads after the user revoked the policy.
            legacy_vision_request = request.get("vision_fallback")
            if (
                isinstance(legacy_vision_request, dict)
                and bool(legacy_vision_request.get("enabled"))
                and not bool(_vision_policy_snapshot(preferences).get("enabled"))
            ):
                raise _ResearchDataUnavailable("VISION_CONSENT_REQUIRED")
            vision_policy = _vision_policy_snapshot(preferences)
            try:
                structured_sources, vision_adapter, vision_config = self._financial_recognition_capabilities(
                    company, (), job=job, run_id=job.run_id,
                    authorized_filing_hashes=vision_filing_hashes)
            except VisionAdapterError as exc:
                raise _ResearchDataUnavailable(exc.code) from exc
            if vision_config is not None and vision_config.enabled:
                self._update_job(job, stage="filing-discovery", percent=3,
                    message=_ui_message(ui_language,
                        "Vision fallback is enabled; it will upload only failed financial-table pages",
                        "视觉财报兜底已启用，仅在本地失败后上传失败的财务表页",
                        "雲端財報備援已啟用，只會在本地解析失敗後上傳失敗的財務表頁"))


            official_request = None
            if mode != "demo":
                # Transport only captures configured collaborators. Discovery,
                # acquisition, source compilation and publication are one
                # application use case for all markets and for rebuild.
                if company_market == Market.US:
                    source_adapter = DeferredOfficialSource(lambda: self._sec_client_factory(
                        build_sec_user_agent(
                            _normalize_sec_profile(preferences["sec_contact_profile"]),
                            preferences["sec_contact_email"]),
                        self.storage.data_dir / "sec-cache"))
                    structured_sources = (SecFinancialSourceAdapter(source_adapter),)
                else:
                    source_adapter = self._disclosure_service.adapter_for(company)
                    language_setter = getattr(source_adapter, "set_preferred_language", None)
                    if callable(language_setter):
                        language_setter(report_language)

                official_request = OfficialFinancialRequest(
                    company, source_adapter, candidate_sources=structured_sources,
                    annual_history_years=history_years,
                    download_filings=bool(request.get("download_filings", True)),
                    run_id=str(request.get("_financial_recovery_session_id") or job.job_id),
                    vision_fallback=vision_adapter, vision_config=vision_config,
                    authorized_source_stages=(frozenset({GapStageKind.MINERU})
                        if vision_config is not None and vision_config.enabled else frozenset()))

            def preparation_progress(stage, current, total, detail=None):
                if stage == "filing-acquisition-complete":
                    vision_filing_hashes.update(item.content_hash for item in detail or () if item.content_hash)
                    return
                if stage == "synthetic-data-ready":
                    self._update_job(job, percent=30, message=_ui_message(ui_language,
                        "Synthetic data ready", "演示数据准备完成", "示範資料準備完成"))
                elif stage in {"filing-discovery", "filing-download"}:
                    self._update_job(job, stage=stage, stage_current=current, stage_total=total,
                        message=_ui_message(ui_language, "Reading official financial sources",
                            "正在读取官方财务来源", "正在讀取官方財務來源"),
                        percent=5 if stage == "filing-discovery" else 6 + round(current * 10 / max(1, total)))
                else:
                    self._ingestion_progress(job, stage, current, total, detail)

            captured_start = ResearchPreparationRequest(
                company, selected_pack, (config, *comparison_configs), official=official_request,
                mode=str(mode), valuation_inputs=valuation_inputs,
                manual_market_snapshot=manual_market_snapshot,
                # Preserve legacy opt-in: absent is not authority to fetch quotes.
                auto_market_snapshot=bool(request.get("auto_market_snapshot")),
                valuation_policy=valuation_policy, vision_policy=vision_policy,
                report_language=report_language, ui_language=ui_language,
                parallel_agents=parallel_agents, annual_history_years=history_years,
                existing_run=job.run, recovery_session_id=str(request.get("_financial_recovery_session_id", "")),
                job_id=job.job_id,
                allow_partial_research=request.get("allow_partial_research", False),
                policy_source=(
                    "manual_override"
                    if isinstance(request.get("valuation"), dict)
                    and any(request["valuation"].get(key) not in (None, "", 0) for key in ("market_cap_billions", "discount_rate_percent", "terminal_growth_percent", "horizon_years"))
                    else "policy_default"
                ),
            )

            def agent_progress(agent_id: str, state: str) -> None:
                with self._jobs_lock:
                    if (job.generation != progress_generation or job.cancel_event.is_set()
                            or job.state not in {"queued", "running"}):
                        return
                    job.agent_states[agent_id] = state
                    if parallel_agents and agent_id in {"financial-analyst", "business-analyst", "accounting-risk-analyst",
                                                         "source-industry-analysis", "source-risk-analysis"}:
                        job.stage = "base-analysis"
                    else:
                        job.stage = {
                            "financial-analyst": "financial-analysis",
                            "business-analyst": "business-analysis",
                            "accounting-risk-analyst": "risk-analysis",
                            "source-industry-analysis": "source-industry-analysis",
                            "source-risk-analysis": "source-risk-analysis",
                            "growth-opportunity-analyst": "growth-analysis",
                            "skeptical-analyst": "counter-analysis",
                            "forecast-analyst": "scenario-analysis",
                            "research-synthesizer": "synthesis",
                        }.get(agent_id, job.stage)

            def progress(event: ResearchExecutionProgress) -> None:
                prefix = ""
                if event.comparison_count:
                    prefix = (_ui_message(ui_language, "Primary: ", "主模型：", "主模型：")
                              if event.model_index == 0 else _ui_message(
                                  ui_language,
                                  f"Comparison {event.model_index}/{event.comparison_count}: ",
                                  f"对比模型 {event.model_index}/{event.comparison_count}：",
                                  f"比較模型 {event.model_index}/{event.comparison_count}："))
                self._update_job(
                    job,
                    active_only=True,
                    expected_generation=progress_generation,
                    state="running",
                    message=f"{prefix}{event.message}",
                    stage=_workflow_stage(event.model_percent, comparison=event.model_index > 0),
                    percent=30 + round(min(100, max(0, event.percent)) * 70 / 100),
                )

            primary = self._research_orchestrator.start(captured_start,
                cancel_check=job.cancel_event.is_set, progress=progress,
                preparation_progress=preparation_progress,
                disclosure_coverage=lambda coverage: self._record_disclosure_coverage(job, coverage),
                agent_progress=agent_progress)
            self._update_job(
                job,
                state="completed",
                stage=("partial" if primary.status.value == "partial" else "completed"),
                percent=100,
                message=(
                    _ui_message(
                        ui_language,
                        "Research stages completed; synthesized report needs retry",
                        "研究阶段已完成；综合报告需要重试",
                        "研究階段已完成；綜合報告需要重試",
                    )
                    if primary.status.value == "partial"
                    else _ui_message(ui_language, "Research completed", "研究完成", "研究完成")
                ),
                run_id=primary.run_id,
            )
        except ResearchCancelled as exc:
            # Optional comparison cancellation keeps the committed primary
            # result and its quality status. Only unfinished primary research
            # is itself cancelled; job completion and report quality differ.
            if isinstance(exc, ResearchComparisonCancelled):
                job.run = exc.primary_run
                job.run_id = exc.primary_run.run_id
                self._update_job(
                    job, state="cancelled", stage="cancelled", run_id=job.run_id,
                    message=_ui_message(
                        ui_language, "Comparison cancelled; primary research preserved",
                        "对比研究已取消；主模型研究已保留", "比較研究已取消；主模型研究已保留",
                    ),
                )
                return
            if job.run is not None:
                self._continuity.cancel(job.run, stage=job.stage or "cancelled",
                    message=_ui_message(ui_language, "Research cancelled", "研究已取消", "研究已取消"))
            self._update_job(
                job,
                state="cancelled",
                stage="cancelled",
                message=_ui_message(ui_language, "Research cancelled", "研究已取消", "研究已取消"),
                run_id=(job.run.run_id if job.run is not None else job.run_id),
            )
        except _ResearchDataUnavailable as exc:
            # The use case owns financial diagnostics and repair cases; the
            # transport only projects its typed outcome onto the visible job.
            if exc.coverage is not None:
                self._record_disclosure_coverage(job, exc.coverage)
            final_error = exc.code
            message = _research_data_message(final_error, ui_language)
            self._preserve_interrupted_research(
                job, stage="data-unavailable", error_code=final_error,
                message=message, outcome=StageOutcome.from_recovery(exc.outcome),
                recovery_outcome=exc.outcome,
            )
        except (MarketDataError, SecClientError) as exc:
            code = getattr(exc, "code", "FILING_FETCH_FAILED")
            final_error = code
            recovery = unavailable_outcome(code)
            self._preserve_interrupted_research(
                job, stage="data-unavailable", error_code=final_error,
                message=_research_data_message(final_error, ui_language),
                outcome=StageOutcome.from_recovery(recovery), recovery_outcome=recovery,
            )
        except Exception as exc:
            safe_error = _redact(str(exc), secrets)[:800]
            failure_code, retryable, timed_out = _classify_research_exception(exc, safe_error)
            if timed_out:
                timeout = _normalize_timeout_seconds(config.timeout_seconds)
                safe_error = _ui_message(
                    ui_language,
                    f"Model request timed out after {timeout}s; increase the timeout or retry.",
                    f"模型请求超过 {timeout} 秒未响应；可提高超时设置后重试。",
                    f"模型請求超過 {timeout} 秒未回應；可提高逾時設定後重試。",
                )
            self._preserve_interrupted_research(
                job,
                stage="model-wait" if timed_out else "interrupted",
                error_code=failure_code,
                message=safe_error or _ui_message(ui_language, "Research interrupted", "研究已中断", "研究已中斷"),
                outcome=StageOutcome.WAITING_RETRYABLE if retryable else StageOutcome.NEEDS_ACTION,
            )
        finally:
            # Explicitly invalidate run-scoped batch consent on every terminal
            # path, including cancellation and data failure.
            vision_filing_hashes.clear()

    def _preserve_interrupted_research(
        self,
        job: _ResearchJob,
        *,
        stage: str,
        error_code: str,
        message: str,
        outcome: StageOutcome,
        recovery_outcome=None,
    ) -> None:
        run = self._continuity.preserve_interruption(
            job.run_id or (job.run.run_id if job.run else ""), run=job.run,
            stage=stage, outcome=outcome, error_code=error_code,
            message=message, recovery_outcome=recovery_outcome)
        if run is not None:
            job.run = run
            job.run_id = run.run_id
        self._update_job(
            job, state="completed", stage="partial", percent=100,
            error_code=error_code, message=message,
            recovery_outcome=recovery_outcome.to_dict() if recovery_outcome else None,
        )

    def _record_disclosure_coverage(self, job: _ResearchJob, coverage: Any) -> None:
        if job.run is None:
            return
        self._continuity.record_disclosure_coverage(job.run, coverage)

    def _select_pack(self, pack_id: str) -> ResearchPack:
        packs = list_installed_packs(self.storage.data_dir / "research-packs")
        if not pack_id:
            return packs[0] if packs else builtin_pack()
        for pack in packs:
            if pack.pack_id == pack_id:
                return pack
        raise ValueError("research pack not found")


def _research_history_years(request: dict[str, Any], pack: Any | None = None) -> int:
    """Normalize the requested annual display history without trusting input."""
    policy = request.get("evidence_policy")
    raw = policy.get("annual_history_years") if isinstance(policy, dict) else None
    if raw is None and pack is not None:
        workflow = getattr(pack, "workflow", {})
        settings = workflow.get("settings", {}) if isinstance(workflow, dict) else {}
        pack_policy = settings.get("evidence_policy", {}) if isinstance(settings, dict) else {}
        raw = pack_policy.get("annual_history_years") if isinstance(pack_policy, dict) else None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 5
    return max(2, min(10, value))


def _report_projection_failure(error: Exception, *, technical: bool = False) -> dict[str, str]:
    """Bounded local diagnostics: no exception body, locals, source lines or user paths."""
    result = {"code": "REPORT_PROJECTION_FAILED", "location": "report"}
    if technical:
        kind = re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__)[:64]
        locations = []
        for frame, line in traceback.walk_tb(error.__traceback__):
            parts = Path(frame.f_code.co_filename).parts
            if "openthesis" in parts:
                module = Path(frame.f_code.co_filename).name
                function = re.sub(r"[^A-Za-z0-9_]", "", frame.f_code.co_name)[:64]
                locations.append(f"{module}:{line}:{function}")
        result.update(exception_type=kind, frames=";".join(locations[-3:])[:512])
    return result


def _research_data_message(code: str, language: str) -> str:
    messages = {
        "zh-CN": {
            "PDF_WORKER_START_FAILED": "本地财报解析进程无法启动，并非已确认的财务字段冲突。已保留原件和完成的进度；请检查运行环境，或配置并授权云端识别后继续。",
            "DOCUMENT_CONTEXT_TIMEOUT": "本地财报预读达到本次任务时限，尚未完成表头与期间读取；这不代表财务字段冲突。原件与已完成成果保留，可本地恢复，或配置并授权云端识别后继续。",
            "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE": "本地财报预读暂时不可用，尚无法读取表头与期间；这不代表财务字段冲突。请检查本地运行环境，或配置并授权云端识别后继续。",
            "NO_FILINGS_AVAILABLE": "官方披露平台暂未提供该公司的可用财务报告。公司可能尚未发布定期报告，或当前没有符合条件的报告。",
            "FILING_FETCH_FAILED": "未能完成官方财报数据获取。请检查网络后重新获取，或打开官方披露平台核对。",
            "FILING_CONTENT_UNSAFE": "安全软件阻止了官方财报文件写入。请确认目标目录权限后重试；残缺文件不会进入研究模型。",
            "FILING_STATUS_UNVERIFIED": "官方数据源未返回可验证的财报结果。请稍后重新获取，或打开官方披露平台核对。",
            "FILING_DOWNLOAD_FAILED": "已找到官方财报，但下载未完成。请检查网络后重新获取。",
            "FILING_DOWNLOAD_REQUIRED": "需要下载官方财报原文后才能开始研究。请启用财报原文下载并重新获取。",
            "FILING_FORMAT_UNSUPPORTED": "已找到官方公告，但当前版本无法从中生成可用的财务数据。",
            "FILING_DATA_QUALITY_FAILED": "已获取官方披露文件，但关键财务字段未通过一致性校验。为避免错误数据进入 AI，本次研究已停止。",
            "FILING_DATA_QUALITY_PARTIAL": "财务资料已重建，但仍有字段未通过校验；已保留可验证数据。",
            "FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED": "资料处理已结束，但必要财务期间尚无可用数据。原件和可用披露已保存，尚未调用研究模型。可继续修复财报，或明确选择仅用现有资料研究（报告会保留这些缺口）。",
            "FILING_REPORT_REFRESH_FAILED": "财务资料已重建，但报告刷新失败；可单独重试报告刷新。",
            "SCANNED_IMAGE_FILING_DETECTED": "已定位到疑似扫描版合并财报页面。请在模型中心配置具备视觉能力的解析方式并授权这些页面后重试；未授权前不会上传页面。",
            "SCANNED_IMAGE_LAYOUT_UNRESOLVED": "当前文件疑似包含图片版财报，但无法安全定位合并报表页。未上传任何页面；请人工核对或更换可解析的官方文件后重试。",
            "VISION_CONSENT_REQUIRED": "视觉财报兜底需要明确上传同意。",
            "VISION_UPLOAD_NOT_APPROVED": "视觉财报页面上传未获批准。",
            "VISION_RATE_LIMITED": "视觉服务暂时限流，请稍后重试。",
            "VISION_UNAUTHORIZED": "视觉服务凭证无效或已过期。",
            "VISION_TIMEOUT": "视觉财报解析超时，未使用不完整结果。",
            "VISION_CANCELLED": "视觉财报解析已取消。",
            "VISION_PAGE_LIMIT": "视觉上传页数超过 20 页限制。",
            "VISION_NETWORK_ERROR": "无法连接视觉解析服务；没有使用不完整结果。请检查网络后重试。",
            "VISION_HTTP_ERROR": "视觉解析服务暂时不可用；没有自动切换到收费模型。",
            "VISION_FORBIDDEN": "视觉解析服务拒绝了本次请求；请稍后重试或选择已配置的视觉模型。",
            "VISION_PROVIDER_UNSUPPORTED": "所选视觉解析方式不可用，请重新选择。",
            "VISION_SIZE_LIMIT": "视觉上传页面超过安全大小限制。",
            "VISION_UPLOAD_APPROVAL_REQUIRED": "视觉上传安全审批未建立，本次没有发送任何页面。请重新发起研究并逐页确认。",
            "VISION_INSECURE_URL": "视觉服务返回了不安全的传输地址，OpenThesis 已拒绝发送页面。",
            "VISION_MALFORMED_RESPONSE": "视觉服务返回了无法验证的响应，本次结果未被采用。",
            "VISION_REMOTE_FAILED": "视觉服务未能完成解析，请稍后重试或选择已配置的视觉模型。",
            "VISION_NO_CANDIDATES": "视觉解析没有提取到可验证的财务候选事实，本次结果未被采用。",
            "VISION_IMAGE_RENDER_FAILED": "待审核财报页无法安全转换为视觉模型输入，本次没有发送。",
            "VISION_MODEL_REQUIRED": "请先在模型中心配置并测试具备视觉能力的模型。",
            "VISION_MODEL_ERROR": "已配置的视觉模型解析失败，本次结果未被采用。",
        },
        "en": {
            "PDF_WORKER_START_FAILED": "The local financial parser process could not start; this is not a confirmed financial-field conflict. Originals and completed progress are preserved. Check the runtime environment, or configure and authorize cloud recognition to continue.",
            "DOCUMENT_CONTEXT_TIMEOUT": "Local report pre-reading reached this task's deadline before headers and periods could be read; this is not a financial-field conflict. Originals and completed work are preserved. Resume locally, or configure and authorize cloud recognition to continue.",
            "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE": "Local report pre-reading is unavailable, so headers and periods could not be read; this is not a financial-field conflict. Check the local runtime, or configure and authorize cloud recognition to continue.",
            "NO_FILINGS_AVAILABLE": "The official disclosure platform does not currently provide a usable financial report for this company. The company may not have published a periodic report yet, or no report matches the current criteria.",
            "FILING_FETCH_FAILED": "Official financial-report data could not be retrieved. Check the network and try again, or verify it on the official disclosure platform.",
            "FILING_CONTENT_UNSAFE": "Security software blocked the official financial-report file write. Check the destination permissions and retry; incomplete files are never sent to research models.",
            "FILING_STATUS_UNVERIFIED": "The official source did not return a verifiable financial-report result. Try again later or verify it on the official disclosure platform.",
            "FILING_DOWNLOAD_FAILED": "An official financial report was found, but its download did not complete. Check the network and try again.",
            "FILING_DOWNLOAD_REQUIRED": "The official report must be downloaded before research can start. Enable report downloads and try again.",
            "FILING_FORMAT_UNSUPPORTED": "Official disclosures were found, but this version could not produce usable financial data from them.",
            "FILING_DATA_QUALITY_FAILED": "Official disclosures were retrieved, but critical financial fields failed consistency checks. Research stopped before any data was sent to AI.",
            "FILING_DATA_QUALITY_PARTIAL": "Financial data was rebuilt, but some fields remain unverified; validated data was preserved.",
            "FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED": "Preparation finished, but required financial periods have no usable data. Originals and available disclosures were saved; no research model was called. Repair the financial sources, or explicitly research only the available material with these gaps disclosed.",
            "FILING_REPORT_REFRESH_FAILED": "Financial data was rebuilt, but report refresh failed; retry report refresh separately.",
            "SCANNED_IMAGE_FILING_DETECTED": "Scanned consolidated-statement pages were located. Configure and authorize a vision-capable parser, then retry; no pages are uploaded without approval.",
            "SCANNED_IMAGE_LAYOUT_UNRESOLVED": "The filing appears image-based, but consolidated-statement pages could not be located safely. Nothing was uploaded; verify the filing manually or use a parseable official file.",
            "VISION_CONSENT_REQUIRED": "Vision fallback requires explicit upload consent.",
            "VISION_UPLOAD_NOT_APPROVED": "The selected financial pages were not approved for upload.",
            "VISION_RATE_LIMITED": "The vision service is rate limited; try again later.",
            "VISION_UNAUTHORIZED": "The vision service credential is invalid or expired.",
            "VISION_TIMEOUT": "Vision parsing timed out; incomplete output was not used.",
            "VISION_CANCELLED": "Vision parsing was cancelled.",
            "VISION_PAGE_LIMIT": "The vision upload exceeds the 20-page limit.",
            "VISION_NETWORK_ERROR": "The vision service could not be reached; incomplete output was not used. Check the network and retry.",
            "VISION_HTTP_ERROR": "The vision service is unavailable; OpenThesis did not switch to a paid model.",
            "VISION_FORBIDDEN": "The vision service rejected this request. Retry later or choose a configured vision model.",
            "VISION_PROVIDER_UNSUPPORTED": "The selected vision path is unavailable. Choose another option.",
            "VISION_SIZE_LIMIT": "The selected vision pages exceed the safe upload limit.",
            "VISION_UPLOAD_APPROVAL_REQUIRED": "The page-upload approval step was not established, so nothing was sent. Start the research again and approve the page preview.",
            "VISION_INSECURE_URL": "The vision service returned an insecure transfer URL, so OpenThesis refused to send the pages.",
            "VISION_MALFORMED_RESPONSE": "The vision service returned an unverifiable response; no result was used.",
            "VISION_REMOTE_FAILED": "The vision service could not complete parsing. Retry later or choose a configured vision model.",
            "VISION_NO_CANDIDATES": "Vision parsing found no verifiable financial candidates; no result was used.",
            "VISION_IMAGE_RENDER_FAILED": "The approved filing page could not be rendered safely for the configured vision model; nothing was sent.",
            "VISION_MODEL_REQUIRED": "Configure and test a vision-capable model in Model Center first.",
            "VISION_MODEL_ERROR": "The configured vision model failed to parse the page; no result was used.",
        },
        "zh-Hant": {
            "PDF_WORKER_START_FAILED": "本機財報解析程序無法啟動，並非已確認的財務欄位衝突。已保留原件和完成的進度；請檢查執行環境，或設定並授權雲端辨識後繼續。",
            "DOCUMENT_CONTEXT_TIMEOUT": "本機財報預讀達到本次任務時限，尚未完成表頭與期間讀取；這不代表財務欄位衝突。原件與已完成成果保留，可本機恢復，或設定並授權雲端辨識後繼續。",
            "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE": "本機財報預讀暫時無法使用，尚無法讀取表頭與期間；這不代表財務欄位衝突。請檢查本機執行環境，或設定並授權雲端辨識後繼續。",
            "NO_FILINGS_AVAILABLE": "官方披露平台目前沒有提供可用的財報。公司可能尚未發布定期報告，或沒有報告符合目前條件。",
            "FILING_FETCH_FAILED": "無法取得官方財報資料。請檢查網路後重試，或在官方披露平台核對。",
            "FILING_CONTENT_UNSAFE": "安全軟體阻止了官方財報檔案寫入。請確認目標資料夾權限後重試；不完整檔案不會送入研究模型。",
            "FILING_STATUS_UNVERIFIED": "官方來源沒有返回可驗證的財報結果。請稍後重試，或在官方披露平台核對。",
            "FILING_DOWNLOAD_FAILED": "已找到官方財報，但下載未完成。請檢查網路後重試。",
            "FILING_DOWNLOAD_REQUIRED": "開始研究前必須先下載官方財報原文。請啟用財報下載後重試。",
            "FILING_FORMAT_UNSUPPORTED": "已找到官方披露，但目前版本無法從中產生可用的財務資料。",
            "FILING_DATA_QUALITY_FAILED": "已取得官方披露，但關鍵財務欄位未通過一致性檢查。為避免錯誤資料送入 AI，本次研究已停止。",
            "FILING_DATA_QUALITY_PARTIAL": "財務資料已重建，但仍有欄位未通過校驗；已保留可驗證資料。",
            "FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED": "資料處理已結束，但必要財務期間尚無可用資料。原件與可用披露已儲存，尚未呼叫研究模型。可繼續修復財報，或明確選擇僅用現有資料研究（報告會保留這些缺口）。",
            "FILING_REPORT_REFRESH_FAILED": "財務資料已重建，但報告刷新失敗；可單獨重試報告刷新。",
            "SCANNED_IMAGE_FILING_DETECTED": "已定位疑似掃描版合併財報頁面。請在模型中心設定具備視覺能力的解析方式並授權這些頁面後重試；未獲授權前不會上傳頁面。",
            "SCANNED_IMAGE_LAYOUT_UNRESOLVED": "目前檔案疑似包含圖片版財報，但無法安全定位合併報表頁面。未傳送任何頁面；請人工核對或更換可解析的官方檔案後重試。",
            "VISION_CONSENT_REQUIRED": "雲端視覺備援需要明確的上傳同意。",
            "VISION_UPLOAD_NOT_APPROVED": "雲端視覺財報頁面未獲准上傳。",
            "VISION_RATE_LIMITED": "雲端視覺服務目前受限流，請稍後重試。",
            "VISION_UNAUTHORIZED": "雲端視覺服務憑證無效或已過期。",
            "VISION_TIMEOUT": "雲端視覺財報解析逾時，未採用不完整結果。",
            "VISION_CANCELLED": "雲端視覺財報解析已取消。",
            "VISION_PAGE_LIMIT": "視覺上傳頁數超過 20 頁限制。",
            "VISION_NETWORK_ERROR": "無法連線視覺解析服務；未採用不完整結果。請檢查網路後重試。",
            "VISION_HTTP_ERROR": "視覺解析服務暫時不可用；沒有自動切換到付費模型。",
            "VISION_FORBIDDEN": "視覺解析服務拒絕了本次請求；請稍後重試或選擇已設定的視覺模型。",
            "VISION_PROVIDER_UNSUPPORTED": "所選視覺解析方式不可用，請重新選擇。",
            "VISION_SIZE_LIMIT": "選取的雲端視覺頁面超過安全上傳限制。",
            "VISION_UPLOAD_APPROVAL_REQUIRED": "未建立頁面上傳安全審批，本次沒有傳送任何頁面。請重新發起研究並逐頁確認。",
            "VISION_INSECURE_URL": "視覺服務返回不安全的傳輸位址，OpenThesis 已拒絕傳送頁面。",
            "VISION_MALFORMED_RESPONSE": "視覺服務返回無法驗證的回應，本次結果未被採用。",
            "VISION_REMOTE_FAILED": "視覺服務未能完成解析，請稍後重試或選擇已設定的視覺模型。",
            "VISION_NO_CANDIDATES": "視覺解析沒有擷取到可驗證的財務候選事實，本次結果未被採用。",
            "VISION_IMAGE_RENDER_FAILED": "待審核財報頁無法安全轉換為視覺模型輸入，本次沒有傳送。",
            "VISION_MODEL_REQUIRED": "請先在模型中心設定並測試具備視覺能力的模型。",
            "VISION_MODEL_ERROR": "已設定的視覺模型解析失敗，本次結果未被採用。",
        },
    }
    catalog = messages[ZH_HANT if normalize_language(language) == ZH_HANT else EN if normalize_language(language) == EN else "zh-CN"]
    return catalog.get(code, catalog["FILING_FETCH_FAILED"])


def _decode_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _compiler_validation_groups(validations: Any) -> list[FinancialGroupValidation]:
    """Compatibility name for the shared application's serialization only."""
    return compiler_validation_groups(validations)


def _cached_us_annual_window_is_complete(
    company: Company,
    filings: list[FilingDocument],
    facts: list[dict[str, Any]],
    *,
    required_count: int,
) -> bool:
    """Return whether a retry can be satisfied entirely from trusted local data."""

    if required_count <= 0:
        return False
    required_concepts = CoveragePlanner().plan(company).required_concepts
    annual_forms = {"10-K", "20-F", "40-F"}
    annual_by_accession = {
        filing.accession_number: filing
        for filing in filings
        if filing.accession_number
        and filing.form_type.upper() in annual_forms
        and filing.period_end
        and filing.local_path
        and Path(filing.local_path).is_file()
        and Path(filing.local_path).stat().st_size > 0
    }
    by_accession: dict[str, dict[str, dict[str, Any]]] = {}
    for fact in facts:
        accession = str(fact.get("accession_number", ""))
        filing = annual_by_accession.get(accession)
        if filing is None or str(fact.get("end_date", "")) != filing.period_end:
            continue
        if str(fact.get("fiscal_period", "FY")).upper() != "FY":
            continue
        if str(fact.get("consolidated_scope", fact.get("scope", ""))).lower() != "consolidated":
            continue
        currency = str(fact.get("currency", "")).upper()
        if currency != company.reporting_currency.upper():
            continue
        if not fact.get("source_url") or not fact.get("statement"):
            continue
        by_accession.setdefault(accession, {})[str(fact.get("concept", ""))] = fact

    eligible: list[tuple[int, str]] = []
    for accession, concepts in by_accession.items():
        if not concepts_cover_profile(concepts, required_concepts):
            continue
        try:
            year = int(annual_by_accession[accession].period_end[:4])
        except (KeyError, TypeError, ValueError):
            continue
        eligible.append((year, accession))
    if len({year for year, _ in eligible}) < required_count:
        return False
    years = sorted({year for year, _ in eligible}, reverse=True)[:required_count]
    return len(years) == required_count and all(
        years[index] - years[index + 1] == 1 for index in range(len(years) - 1)
    )


def _latest_sec_verified_group(
    facts: list[FinancialFact],
    engine: FinancialIngestionEngine,
    *,
    company: Company | None = None,
    expected_period_end: str | None = None,
) -> tuple[FinancialFact, ...] | None:
    """Resolve the latest SEC FY through the canonical compiler gate."""

    first = facts[0] if facts else None
    subject = company or Company(
        first.company_cik if first else "",
        "",
        "SEC issuer",
        market=first.market if first else "US",
        listing_currency=first.currency if first else "USD",
        reporting_currency=first.currency if first else "USD",
        accounting_standard="",
        industry="",
    )
    required_concepts = CoveragePlanner().plan(subject).required_concepts
    # Synthetic demo mode intentionally carries a compact legacy schema and is
    # never an external filing; preserve its deterministic report path.
    if facts and all(fact.form_type == "DEMO" for fact in facts):
        by_end: dict[str, list[FinancialFact]] = {}
        for fact in facts:
            by_end.setdefault(fact.end_date, []).append(fact)
        for end in sorted(by_end, reverse=True):
            group = tuple(by_end[end])
            concepts = {fact.concept for fact in group}
            if concepts_cover_profile(concepts, required_concepts):
                return group
        return None

    grouped: dict[str, list[FinancialFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.accession_number, []).append(fact)
    filings: list[FilingDocument] = []
    for accession, group in grouped.items():
        first = group[0]
        filings.append(FilingDocument(
            f"sec:{accession}", first.company_cik, accession, first.form_type or "10-K",
            first.fiscal_period or "FY", first.end_date, first.filed_at,
            first.source_document or accession, first.source_url,
        ))
    # Probe the requested/latest filing first, then walk backwards to the
    # latest complete FY. A malformed newly archived filing must not erase a
    # previously verified annual baseline.
    ordered = sorted(
        filings,
        key=lambda item: (
            item.period_end == expected_period_end if expected_period_end else False,
            item.period_end,
            item.filed_at,
        ),
        reverse=True,
    )
    for filing in ordered:
        group_facts = [
            fact for fact in facts
            if fact.accession_number == filing.accession_number
        ]
        monetary_currencies = {
            str(fact.currency or fact.unit).upper()
            for fact in group_facts
            if str(fact.currency or fact.unit).upper()
            not in {"", "SHARE", "SHARES", "UNIT", "UNITS", "PURE", "PERCENT"}
        }
        subject_currency = str(subject.reporting_currency or "").upper()
        if subject_currency in monetary_currencies:
            reporting_currency = subject_currency
        elif len(monetary_currencies) == 1:
            reporting_currency = next(iter(monetary_currencies))
        else:
            # A shares-only or mixed-currency archive is not a financial
            # baseline.  Let the ordered loop continue to the prior complete
            # FY instead of leaking a unit from another accession.
            continue
        admitted = FinancialPipeline(engine).prepare(
            FinancialResearchSnapshot.from_facts(
                replace(subject, reporting_currency=reporting_currency), [filing], group_facts,
            ), CompilationPolicy(reporting_currency=reporting_currency),
        )
        if admitted.model_ready:
            return admitted.facts
    return None


def _report_completion_state(
    run: dict[str, Any],
    artifacts: list[dict[str, Any]],
    continuity: dict[str, Any] | None,
    *,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep execution termination separate from research completeness."""

    status = str(run.get("status", "")).casefold()
    attempt_complete = status in {
        RunStatus.COMPLETED.value,
        RunStatus.PARTIAL.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    }
    final = next(
        (item for item in reversed(artifacts) if item.get("artifact_type") == "research-report"),
        None,
    )
    research_complete = False
    default_readiness: dict[str, Any] = {
        "state": "action_required" if final is None else "substantive_partial",
        "complete": False,
        "substantive_sections": [],
        "missing_sections": [],
        "missing_stages": [],
        "issues": ["research_report_artifact_missing"] if final is None else [],
        "recovery_action": "resume_or_repair_missing_research_stages",
    }
    readiness_value = readiness
    if readiness_value is None and isinstance(final, dict):
        readiness_value = readiness_for_report_artifact(
            final, artifacts, run_status=status
        )
    if readiness_value is None:
        readiness_value = default_readiness
    if isinstance(readiness_value, dict):
        readiness_value = dict(readiness_value)
        research_complete = (
            status == RunStatus.COMPLETED.value and bool(readiness_value.get("complete"))
        )
    latest = continuity.get("latest") if isinstance(continuity, dict) else None
    continuity_requires_action = bool(
        isinstance(latest, dict)
        and latest.get("outcome") in {
            StageOutcome.WAITING_RETRYABLE.value,
            StageOutcome.NEEDS_ACTION.value,
            StageOutcome.BLOCKED_INTEGRITY.value,
            StageOutcome.WAITING_EXTERNAL.value,
            StageOutcome.EXHAUSTED.value,
            StageOutcome.FAILED_INTERNAL.value,
        }
    )
    return {
        "attempt_complete": attempt_complete,
        "research_complete": research_complete,
        "report_readiness": readiness_value,
        "action_required": bool(
            attempt_complete and (not research_complete or continuity_requires_action)
        ),
    }


def _report_retryable(artifacts: list[dict[str, Any]]) -> bool:
    """The same capability plan owns execution and its visible recovery action."""
    return ResearchRecoveryPlanner().plan(artifacts, target="synthesis").available


def _model_stages_retryable(
    artifacts: list[dict[str, Any]], continuity: dict[str, Any] | None = None
) -> bool:
    """Expose only the planner-owned base-stage recovery operation."""

    return ResearchRecoveryPlanner().plan(
        artifacts, continuity, target="model-stages"
    ).available


def _synthesis_error_code(artifacts: list[dict[str, Any]]) -> str:
    final = next(
        (item for item in reversed(artifacts) if item.get("artifact_type") == "research-report"),
        None,
    )
    content = final.get("content", {}) if isinstance(final, dict) else {}
    if not isinstance(content, dict) or content.get("mode") != "synthesis-incomplete":
        return ""
    diagnostics = content.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    provider_code = str(diagnostics.get("provider_error_code") or "")
    if provider_code:
        return provider_code[:80]
    if diagnostics.get("parse_error_class") == "context_budget_exceeded":
        return "MODEL_CONTEXT_CAPACITY"
    if diagnostics.get("parse_error_class") == "provider_error":
        return "MODEL_PROVIDER_ERROR"
    return "MODEL_RESPONSE_INVALID"


def _growth_retryable(artifacts: list[dict[str, Any]]) -> bool:
    growth = next(
        (item for item in reversed(artifacts) if item.get("artifact_type") == "growth-opportunities"),
        None,
    )
    if not growth:
        return False
    content = growth.get("content", {})
    if not isinstance(content, dict) or content.get("opportunities"):
        return False
    validation = content.get("_validation")
    return content.get("_response_error") in {
        "empty_content",
        "invalid_json",
        "invalid_shape",
    } or (isinstance(validation, dict) and validation.get("passed") is False)


def _serialize_pack(pack: ResearchPack) -> dict[str, str]:
    return {
        "pack_id": pack.pack_id,
        "name": pack.name,
        "version": pack.version,
        "content_hash": pack.content_hash,
    }


def _normalize_sec_profile(value: str) -> str:
    return "personal" if value == "individual_investor" else value


def _company_from_request(value: Any) -> Company:
    if not isinstance(value, dict):
        raise ValueError("company is required")
    fields: dict[str, str] = {
        key: str(value.get(key, "")).strip()
        for key in ("cik", "ticker", "name", "exchange")
    }
    if not fields["cik"] or not fields["ticker"] or not fields["name"]:
        raise ValueError("company is incomplete")
    for key in (
        "issuer_id",
        "market",
        "security_id",
        "listing_currency",
        "reporting_currency",
        "accounting_standard",
        "industry",
        "industry_support",
        "source_url",
    ):
        normalized = str(value.get(key, "")).strip()
        if normalized:
            fields[key] = normalized
    return Company(**fields)


def _comparison_configs_from_request(value: Any) -> list[ModelConfig]:
    if not isinstance(value, list):
        raise ValueError("comparison_models must be an array")
    if not 1 <= len(value) <= 4:
        raise ValueError("comparison_models must contain between one and four models")
    configs = [_model_config_from_request(item) for item in value]
    if any(not config.enabled for config in configs):
        raise ValueError("comparison models must be configured")
    if any(config.role != "comparison" for config in configs):
        raise ValueError("comparison model role is invalid")
    identities = {(item.configured_model_id, item.configuration_version) for item in configs}
    if len(identities) != len(configs):
        raise ValueError("comparison models must be unique")
    return configs


def _model_config_from_request(value: Any) -> ModelConfig:
    if value is None or not isinstance(value, dict):
        raise ValueError("model reference is required")
    forbidden = {"api_key", "base_url", "preset_id", "provider", "model"}.intersection(value)
    if forbidden:
        raise ValueError("legacy model configuration fields are not accepted")
    configured_model_id = str(value.get("configured_model_id", "")).strip()
    if not configured_model_id:
        return ModelConfig()
    if len(configured_model_id) > 128 or not all(
        character.isalnum() or character in "_.-" for character in configured_model_id
    ):
        raise ValueError("configured model id is invalid")
    try:
        configuration_version = max(1, int(value.get("configuration_version", 1)))
    except (TypeError, ValueError) as exc:
        raise ValueError("model configuration version is invalid") from exc
    role = str(value.get("role", "primary")).strip() or "primary"
    if role not in {"primary", "comparison", "verification", "vision", "ot_assistant"}:
        raise ValueError("model role is invalid")
    return ModelConfig(
        configured_model_id=configured_model_id,
        configuration_version=configuration_version,
        role=role,
        timeout_seconds=600,
        connection_id=_normalize_model_connection_id(value.get("connection_id")),
        context_window_hint=value.get("context_window_hint"),
    )


def _normalize_model_connection_id(value: Any) -> str:
    connection_id = str(value or "").strip()
    if not connection_id:
        return ""
    if len(connection_id) > 128 or not all(
        character.isascii() and (character.isalnum() or character in "_.-")
        for character in connection_id
    ):
        raise ValueError("model connection id is invalid")
    return connection_id


def _validate_ot_suggestion_path(path: Any, draft: dict[str, Any]) -> str:
    if not isinstance(path, str) or len(path) > 160 or "~" in path:
        raise ValueError("OT suggestion path is invalid")
    allowed = (
        re.fullmatch(r"/package/(name|description|version)", path)
        or re.fullmatch(r"/settings/(horizon_years|depth|risk_emphasis|report_language)", path)
        or re.fullmatch(r"/workflow/steps/([0-9]|[1-5][0-9]|6[0-3])/(prompt|role|output_schema)", path)
        or re.fullmatch(r"/outputs/(formats|include_evidence|deterministic_transforms)", path)
    )
    if not allowed:
        raise ValueError("OT suggestion path is outside the editable scope")
    _read_json_pointer(draft, path)
    return path


def _read_json_pointer(value: Any, path: str) -> Any:
    current = value
    for segment in path.lstrip("/").split("/"):
        if isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError) as exc:
                raise ValueError("OT suggestion path does not exist") from exc
        elif isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            raise ValueError("OT suggestion path does not exist")
    return current


def _write_json_pointer(value: dict[str, Any], path: str, replacement: Any) -> None:
    segments = path.lstrip("/").split("/")
    current: Any = value
    for segment in segments[:-1]:
        current = current[int(segment)] if isinstance(current, list) else current[segment]
    final = segments[-1]
    if isinstance(current, list):
        current[int(final)] = replacement
    else:
        current[final] = replacement

def _valuation_inputs(value: Any) -> dict[str, float] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ValueError("valuation settings are invalid")
    market_cap_billions = float(value.get("market_cap_billions", 0) or 0)
    if market_cap_billions <= 0:
        return None
    discount_rate = float(value.get("discount_rate_percent", 10)) / 100
    terminal_growth = float(value.get("terminal_growth_percent", 3)) / 100
    if discount_rate <= terminal_growth:
        raise ValueError("discount rate must exceed terminal growth")
    return {
        "market_cap": market_cap_billions * 1_000_000_000,
        "discount_rate": discount_rate,
        "terminal_growth": terminal_growth,
        "horizon_years": float(value.get("horizon_years", 5) or 5),
    }


def _market_snapshot(value: Any, company: Company) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ValueError("market snapshot is invalid")
    price = float(value.get("price", 0) or 0)
    market_cap_billions = float(value.get("market_cap_billions", 0) or 0)
    if price < 0 or market_cap_billions < 0:
        raise ValueError("manual market values cannot be negative")
    if price == 0 and market_cap_billions == 0:
        return None
    as_of = str(value.get("as_of", "")).strip()
    if not as_of:
        raise ValueError("manual market values require an as-of date")
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("manual market as-of date must use YYYY-MM-DD") from exc
    currency = str(value.get("currency", "")).strip().upper() or company.listing_currency
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("market currency must use a three-letter code")
    return {
        "source": "manual",
        "price": price or None,
        "market_cap": market_cap_billions * 1_000_000_000 if market_cap_billions else None,
        "currency": currency,
        "as_of": as_of,
    }


def _request_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _classify_research_exception(
    error: BaseException, safe_message: str = ""
) -> tuple[str, bool, bool]:
    """Map an execution failure to the durable recovery vocabulary.

    Provider codes are authoritative; localized message text is only a legacy
    fallback.  This prevents a Chinese timeout message (or HTTP 503) from
    being mislabeled as an unrecoverable generic research failure.
    """

    if isinstance(error, ProviderError):
        code = str(error.code or "MODEL_ERROR")[:80]
        timed_out = code == "MODEL_TIMEOUT"
        return code, bool(error.retryable or timed_out), timed_out
    lowered = str(safe_message or error).casefold()
    timed_out = "timeout" in lowered or "timed out" in lowered or "超时" in lowered or "逾時" in lowered
    return (
        "MODEL_TIMEOUT" if timed_out else "RESEARCH_FAILED",
        timed_out,
        timed_out,
    )


def _normalize_timeout_seconds(value: Any, default: int = 180) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = default
    return min(600, max(30, seconds))


def _vision_config_from_request(value: Any) -> VisionFallbackConfig | None:
    if not isinstance(value, dict):
        return None
    forbidden = {"token", "api_key", "endpoint", "model_id"}.intersection(value)
    if forbidden:
        raise ValueError("legacy vision configuration fields are not accepted")
    enabled = bool(value.get("enabled", False))
    if not enabled:
        return VisionFallbackConfig(enabled=False)
    provider = str(value.get("provider", "configured_model"))
    if provider not in {"mineru_flash", "configured_model"}:
        raise ValueError("vision fallback provider is not supported")
    if not bool(value.get("require_page_approval", True)):
        raise ValueError("vision fallback requires per-run page approval")
    approval_mode = str(value.get("approval_mode", "review_each_plan"))
    if approval_mode not in {"review_each_plan", "approve_current_research"}:
        raise ValueError("vision fallback approval mode is not supported")
    if provider == "mineru_flash":
        if value.get("model") is not None:
            raise ValueError("MinerU Flash does not accept a configured model reference")
        configuration_version = value.get("configuration_version", 1)
        if isinstance(configuration_version, bool) or not isinstance(configuration_version, int) or configuration_version < 1:
            raise ValueError("MinerU Flash configuration version must be a positive integer")
        return VisionFallbackConfig(
            enabled=True,
            consent=bool(value.get("consent", False)),
            provider="mineru_flash",
            connection_id="",
            configuration_version=configuration_version,
            timeout_seconds=60.0,
            language=str(value.get("language", "auto")),
            require_page_approval=True,
            approval_mode=approval_mode,
        )
    model_config = _model_config_from_request(value.get("model"))
    if not model_config.enabled or model_config.role != "vision":
        raise ValueError("vision fallback requires a configured vision model")
    return VisionFallbackConfig(
        enabled=True,
        consent=bool(value.get("consent", False)),
        provider="configured_model",
        configured_model_id=model_config.configured_model_id,
        connection_id=model_config.connection_id,
        configuration_version=model_config.configuration_version,
        timeout_seconds=float(model_config.timeout_seconds),
        language=str(value.get("language", "auto")),
        require_page_approval=True,
        approval_mode=approval_mode,
    )


def _parse_persisted_vision_policy(
    raw_value: str,
    *,
    strict: bool = False,
) -> dict[str, Any] | None:
    """Parse the atomic visual policy without accepting secrets or stale consent."""

    if not raw_value:
        return None
    try:
        value = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if strict:
            raise PreferenceValidationError("vision fallback policy must be valid JSON") from exc
        return None
    if not isinstance(value, dict):
        if strict:
            raise PreferenceValidationError("vision fallback policy must be an object")
        return None
    forbidden = {"api_key", "apikey", "token", "secret", "endpoint", "base_url"}

    def contains_forbidden(item: Any) -> bool:
        if isinstance(item, dict):
            return any(
                str(key).casefold() in forbidden or contains_forbidden(child)
                for key, child in item.items()
            )
        if isinstance(item, list):
            return any(contains_forbidden(child) for child in item)
        return False

    if contains_forbidden(value):
        if strict:
            raise PreferenceValidationError("vision fallback policy must not contain secrets")
        return None
    valid = bool(
        value.get("schema_version") == 1
        and value.get("policy_version") == 1
        and value.get("scope_version") == 1
        and value.get("provider_terms_version") == 1
        and value.get("provider") in {"mineru_flash", "configured_model"}
        and value.get("approval_mode")
        in {"review_each_plan", "approve_current_research"}
        and value.get("authorization_scope") == "financial_failed_pages"
    )
    model = value.get("model")
    connection_id = ""
    if value.get("provider") == "configured_model":
        model_id = model.get("configured_model_id") if isinstance(model, dict) else None
        try:
            connection_id = _normalize_model_connection_id(
                model.get("connection_id") if isinstance(model, dict) else ""
            )
        except ValueError:
            valid = False
        valid = bool(
            valid
            and isinstance(model, dict)
            and isinstance(model_id, str)
            and model_id
            and len(model_id) <= 128
            and all(character.isalnum() or character in "_.-" for character in model_id)
            and isinstance(model.get("configuration_version"), int)
            and not isinstance(model.get("configuration_version"), bool)
            and model.get("configuration_version") >= 1
        )
    normalized = {
        "schema_version": 1,
        "policy_version": 1,
        "scope_version": 1,
        "provider_terms_version": 1,
        "enabled": value.get("enabled") is True,
        "provider": value.get("provider")
        if value.get("provider") in {"mineru_flash", "configured_model"}
        else "mineru_flash",
        "configured_model_id": str(model.get("configured_model_id", ""))
        if isinstance(model, dict)
        else "",
        "connection_id": connection_id,
        "configuration_version": int(model.get("configuration_version", 1))
        if isinstance(model, dict)
        and isinstance(model.get("configuration_version", 1), int)
        else 1,
        "approval_mode": value.get("approval_mode")
        if value.get("approval_mode")
        in {"review_each_plan", "approve_current_research"}
        else "review_each_plan",
        "authorization": {
            "valid": bool(
                valid
                and value.get("enabled") is True
                and value.get("standing_authorization") is True
                and isinstance(value.get("authorized_at"), str)
                and value.get("authorized_at")
            ),
            "scope": "financial_failed_pages",
            "policy_version": "1",
            "confirmed_at": str(value.get("authorized_at", "")),
        },
    }
    if strict and not valid:
        raise PreferenceValidationError("vision fallback policy is unsupported or stale")
    return normalized


def _vision_policy_snapshot(preferences: dict[str, str]) -> dict[str, Any]:
    """Return a non-secret immutable policy snapshot for one research run."""

    atomic = _parse_persisted_vision_policy(
        str(preferences.get("vision_fallback_policy", ""))
    )
    if atomic is not None:
        return atomic

    provider = str(preferences.get("vision_provider", "mineru_flash"))
    policy_version = str(preferences.get("vision_policy_version", "1"))
    standing = str(preferences.get("vision_standing_authorization", "false")) == "true"
    authorization_valid = bool(
        standing
        and preferences.get("vision_authorized_provider", "") == provider
        and preferences.get("vision_authorized_scope", "")
        == "failed_financial_statement_pages"
        and preferences.get("vision_authorized_policy_version", "") == policy_version
        and preferences.get("vision_authorized_at", "")
    )
    try:
        schema_version = max(1, int(policy_version or "1"))
    except (TypeError, ValueError):
        schema_version = 1
        authorization_valid = False
    try:
        configuration_version = max(
            1, int(preferences.get("vision_configuration_version", "1") or "1")
        )
    except (TypeError, ValueError):
        configuration_version = 1
        authorization_valid = False
    return {
        "schema_version": schema_version,
        "enabled": str(preferences.get("vision_enabled", "false")) == "true",
        "provider": provider,
        "configured_model_id": str(preferences.get("vision_configured_model_id", "")),
        "connection_id": str(preferences.get("vision_connection_id", "")),
        "configuration_version": configuration_version,
        "approval_mode": str(
            preferences.get("vision_approval_mode", "review_each_plan")
        ),
        "authorization": {
            "valid": authorization_valid,
            "scope": "failed_financial_statement_pages",
            "policy_version": policy_version,
            "confirmed_at": str(preferences.get("vision_authorized_at", "")),
        },
    }


def _vision_request_from_preferences(preferences: dict[str, str]) -> dict[str, Any]:
    """Build the current request view; revoked authorization always wins."""

    snapshot = _vision_policy_snapshot(preferences)
    if not snapshot["enabled"]:
        return {"enabled": False}
    provider = str(snapshot["provider"])
    request: dict[str, Any] = {
        "enabled": True,
        "provider": provider,
        "consent": bool(snapshot["authorization"]["valid"]),
        "require_page_approval": True,
        "approval_mode": str(snapshot["approval_mode"]),
        "language": "auto",
        "configuration_version": int(snapshot["configuration_version"]),
    }
    if provider == "configured_model":
        request["model"] = {
            "configured_model_id": str(snapshot["configured_model_id"]),
            "connection_id": str(snapshot.get("connection_id", "")),
            "configuration_version": int(snapshot["configuration_version"]),
            "role": "vision",
        }
    return request


def _vision_batch_upload_allowed(
    summary: dict[str, Any],
    config: VisionFallbackConfig,
    filing_hashes: set[str] | frozenset[str],
    *,
    job_active: bool,
    cancelled: bool,
    approved_plans: dict[str, tuple[str, int, int]] | None = None,
) -> bool:
    """Validate one current-research upload before entering a network adapter.

    The caller owns the run-scoped hash set; this helper intentionally accepts
    only the safe plan summary and never grants approval based on a source URL,
    local path, or any persisted authorization state.
    """

    expected_provider = (
        config.provider
        if config.provider == "mineru_flash"
        else config.configured_model_id
    )
    try:
        plan_id = str(summary.get("plan_id") or "")
        provider = str(summary.get("provider") or "")
        filing_hash = str(summary.get("filing_hash") or "")
        pages = tuple(int(item) for item in (summary.get("pages") or ()))
        document_hashes = tuple(str(item) for item in (summary.get("document_hashes") or ()))
        total_bytes = int(summary.get("total_bytes") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    valid = bool(
        job_active
        and not cancelled
        and plan_id
        and provider == expected_provider
        and filing_hash
        and filing_hash in filing_hashes
        and pages
        and len(set(pages)) == len(pages)
        and len(document_hashes) == len(pages)
        and all(document_hashes)
        and len(pages) <= config.max_pages
        and total_bytes > 0
        and total_bytes <= config.max_bytes
    )
    if not valid:
        return False
    provider_identity = config.provider
    if config.configured_model_id:
        provider_identity = (
            f"{config.provider}:{config.configured_model_id}:"
            f"configuration-{config.configuration_version}"
        )
    expected_plan_id = hashlib.sha256(
        "|".join(
            [VISION_TASK_SCHEMA_VERSION, "plan", provider_identity, filing_hash]
            + [f"{page}:{digest}" for page, digest in zip(pages, document_hashes)]
        ).encode("utf-8")
    ).hexdigest()
    if plan_id != expected_plan_id:
        return False

    if approved_plans is None:
        return True
    if not approved_plans:
        if approved_plans is not None:
            approved_plans[plan_id] = (
                _vision_batch_summary_fingerprint(summary), len(pages), total_bytes,
            )
        return True

    fingerprint = _vision_batch_summary_fingerprint(summary)
    existing = approved_plans.get(plan_id)
    if existing is not None:
        # Replaying the exact same immutable plan is idempotent. Any change to
        # its safe summary (including filing/page hashes) is treated as
        # tampering and cannot inherit the prior approval.
        return existing[0] == fingerprint
    used_pages = sum(item[1] for item in approved_plans.values())
    used_bytes = sum(item[2] for item in approved_plans.values())
    if used_pages + len(pages) > config.max_pages or used_bytes + total_bytes > config.max_bytes:
        return False
    approved_plans[plan_id] = (fingerprint, len(pages), total_bytes)
    return True


def _vision_batch_summary_fingerprint(summary: dict[str, Any]) -> str:
    """Hash the safe upload summary for run-local idempotency checks."""
    canonical = json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_vision_adapter_factory(
    config: VisionFallbackConfig,
) -> VisionFinancialSourceAdapter:
    if config.provider == "mineru_flash":
        return MineruFlashAdapter()
    if not config.configured_model_id:
        raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")
    provider = create_provider(
        ModelConfig(
            configured_model_id=config.configured_model_id,
            configuration_version=config.configuration_version,
            role="vision",
            timeout_seconds=max(30, min(600, int(config.timeout_seconds))),
            connection_id=config.connection_id,
        )
    )
    if provider is None:
        raise VisionAdapterError("VISION_MODEL_REQUIRED")
    return GatewayVisionAdapter(provider)

def _workflow_stage(percent: int, *, comparison: bool = False) -> str:
    if comparison:
        return "comparison"
    if percent <= 15:
        return "financial-analysis"
    if percent < 52:
        return "base-analysis"
    if percent < 67:
        return "growth-analysis"
    if percent < 77:
        return "counter-analysis"
    if percent < 90:
        return "scenario-analysis"
    return "synthesis"


def _financial_status(
    storage: Storage,
    company_payload: dict[str, Any],
    run_payload: dict[str, Any],
    *,
    run_id: str | None = None,
    continuity_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable, user-facing financial evidence health summary.

    This is deliberately derived from persisted deterministic evidence rather
    than report prose or model artifacts. Old runs remain readable because the
    evidence-history setting has a conservative default.
    """

    company_cik = _financial_storage_key(company_payload)
    configuration = run_payload.get("research_configuration", {})
    if not isinstance(configuration, dict):
        configuration = {}
    raw_history = configuration.get("annual_history_years", 5)
    try:
        history_years = min(10, max(2, int(raw_history)))
    except (TypeError, ValueError):
        history_years = 5

    filings = [
        item for item in storage.get_filings(company_cik)
        if item.fiscal_period.upper() == "FY"
        and item.form_type.upper() in {"ANNUAL_REPORT", "10-K", "20-F", "40-F"}
    ] if company_cik else []
    financial_read = storage.get_financial_research_snapshot(company_cik) if company_cik else {"facts": [], "storage_issues": []}
    facts = financial_read["facts"]
    groups = storage.get_validation_groups(company_cik) if company_cik else []
    retry = storage.get_financial_retry_state(company_cik) if company_cik else {
        "attempt_count": 0, "last_stage": "", "last_error": "", "updated_at": ""
    }
    recovery_cases = storage.get_financial_recovery_cases(run_id) if run_id else []
    # The legacy retry aggregate is keyed only by security.  It is useful for
    # diagnostics while this run is incomplete, but must not leak a previous
    # run's failure into a completed report.
    retry_belongs_to_current_run = bool(recovery_cases) or str(run_payload.get("status", "")).lower() in {"running", "partial", "failed"}
    if not retry_belongs_to_current_run:
        retry = {"attempt_count": 0, "last_stage": "", "last_error": "", "updated_at": ""}
    data_snapshot = run_payload.get("data_snapshot", {})
    expected_fact_ids_digest = (
        str(data_snapshot.get("financial_fact_ids_sha256", ""))
        if isinstance(data_snapshot, dict)
        else ""
    )
    current_fact_ids_digest = _canonical_snapshot_digest(sorted(
        str(item.get("fact_id", ""))
        for item in facts
        if item.get("fact_id")
    ))
    snapshot_stale = bool(
        expected_fact_ids_digest
        and expected_fact_ids_digest != current_fact_ids_digest
    )
    # A legacy report may still be rendered, but its deterministic artifacts
    # cannot be presented as current when the derived-data contract is absent
    # or differs.  Initialization deliberately does not rewrite that report;
    # the next research/rebuild creates a fresh snapshot.
    if "data_snapshot" in run_payload and isinstance(data_snapshot, dict):
        stored_contract = data_snapshot.get("derived_pipeline_contract")
        if stored_contract != DERIVED_PIPELINE_CONTRACT:
            snapshot_stale = True

    discovered_years = {
            int(str(item.period_end)[:4])
            for item in filings
            if len(str(item.period_end)) >= 4 and str(item.period_end)[:4].isdigit()
        }
    reporting_currency = str(
        company_payload.get("reporting_currency", "")
    ).upper()
    status_subject = Company(
        str(company_payload.get("cik") or company_payload.get("security_id") or ""),
        str(company_payload.get("ticker", "")),
        str(company_payload.get("name", "")),
        exchange=str(company_payload.get("exchange", "")),
        market=str(company_payload.get("market", "US")),
        listing_currency=str(company_payload.get("listing_currency", "USD")),
        reporting_currency=reporting_currency,
        accounting_standard=str(company_payload.get("accounting_standard", "")),
        industry=" ".join(
            str(company_payload.get(key, ""))
            for key in ("industry", "company_type")
            if company_payload.get(key)
        ),
        industry_support=str(company_payload.get("industry_support", "standard")),
    )
    required_concepts = CoveragePlanner().plan(status_subject).required_concepts
    # Validation groups intentionally store a compact coverage summary. Join
    # them with the persisted, verified facts for the same accession and
    # statement scope/currency before deciding a year is complete. Never let a
    # parent, foreign-currency, rejected, or warning fact fill a group gap.
    verified_group_years: set[int] = set()
    for group in groups:
        period = str(group.get("period_end", ""))
        if not (
            str(group.get("status", "")).upper() == ValidationStatus.VERIFIED.value
            and str(group.get("consolidated_scope", "")).lower() == "consolidated"
            and str(group.get("fiscal_period", "FY")).upper() == "FY"
            and period[:4].isdigit()
            and (
                not reporting_currency
                or str(group.get("currency", "")).upper() == reporting_currency
            )
        ):
            continue
        accession = str(group.get("accession_number", ""))
        covered = {str(item) for item in group.get("covered_concepts", [])}
        for fact in facts:
            if (
                str(fact.get("accession_number", "")) == accession
                and str(fact.get("validation_status", "")).upper() == ValidationStatus.VERIFIED.value
                and str(fact.get("end_date", "")) == period
                and str(fact.get("fiscal_period", "FY")).upper() == "FY"
                and str(fact.get("consolidated_scope", fact.get("scope", ""))).lower() == "consolidated"
                and (
                    not reporting_currency
                    or str(fact.get("currency", "")).upper() == reporting_currency
                )
            ):
                covered.add(str(fact.get("concept", "")))
        if concepts_cover_profile(covered, required_concepts):
            verified_group_years.add(int(period[:4]))
    # Legacy SEC caches created before validation groups existed can still be
    # considered complete only when every required concept is present for the
    # same FY, consolidated scope and reporting currency. New ingestion always
    # uses the stricter persisted group path above.
    legacy_complete_years: set[int] = set()
    if not groups:
        facts_by_year: dict[int, set[str]] = {}
        for fact in facts:
            if str(fact.get("fiscal_period", "FY")).upper() != "FY":
                continue
            if str(fact.get("consolidated_scope", fact.get("scope", "consolidated"))).lower() != "consolidated":
                continue
            if reporting_currency and fact.get("currency") and str(fact.get("currency")).upper() != reporting_currency:
                continue
            try:
                year = int(fact.get("fiscal_year"))
            except (TypeError, ValueError):
                continue
            facts_by_year.setdefault(year, set()).add(str(fact.get("concept", "")))
        legacy_complete_years = {
            year
            for year, concepts in facts_by_year.items()
            if concepts_cover_profile(concepts, required_concepts)
        }
    available_years = sorted(verified_group_years | legacy_complete_years, reverse=True)
    all_known_years = sorted(discovered_years | set(available_years), reverse=True)
    latest_year = all_known_years[0] if all_known_years else None
    expected_years = (
        list(range(latest_year, latest_year - history_years - 1, -1))
        if latest_year is not None else []
    )
    missing_years = [year for year in expected_years if year not in discovered_years]
    unverified_years = [
        year for year in expected_years
        if year in discovered_years and year not in available_years
    ]
    issues: list[dict[str, Any]] = [
        {**item, "stage": "storage-read", "status": "UNVERIFIABLE",
         "quality_class": "storage", "next_action": "repair_financial_snapshot"}
        for item in financial_read.get("storage_issues", ())
    ]
    for case in recovery_cases:
        if str(case.get("status", "open")) == "resolved":
            continue
        issues.append({
            "period": str(case.get("accession_number", "")),
            "stage": str(case.get("stage", "financial-recovery")),
            "code": str(case.get("error_code", "RECOVERY_FAILED")),
            "status": str(case.get("status", "open")),
            "quality_class": "recovery",
            "next_action": str(case.get("next_action", "retry_local_parse")),
        })
    group_by_year: dict[int, list[dict[str, Any]]] = {}
    coverage_issue_codes = {
        "income_statement_core_missing",
        "cash_flow_core_missing",
        "balance_sheet_core_missing",
        "core_coverage_insufficient",
    }

    def quality_class(group: dict[str, Any]) -> str:
        status = str(group.get("status", "")).upper()
        issue_codes = {str(item) for item in group.get("issues", [])}
        if status == ValidationStatus.VERIFIED.value:
            return "verified"
        if issue_codes & coverage_issue_codes:
            return "field_missing"
        if status == ValidationStatus.READY_WITH_WARNINGS.value:
            return "warning"
        return "statement_unavailable"

    for group in groups:
        period = str(group.get("period_end", ""))
        if len(period) >= 4 and period[:4].isdigit():
            group_by_year.setdefault(int(period[:4]), []).append(group)
        for issue in group.get("issues", []):
            issues.append({
                "period": period,
                "stage": "filing-validation",
                "code": str(issue),
                "status": str(group.get("status", "")),
                "quality_class": quality_class(group),
            })
    nodes = []
    for year in expected_years:
        year_groups = group_by_year.get(year, [])
        if year in missing_years:
            state = "missing"
            category = "file_unavailable"
        elif year in unverified_years:
            state = "unverified"
            category = (
                quality_class(year_groups[0])
                if year_groups else "statement_unavailable"
            )
        elif any(str(item.get("status", "")) == ValidationStatus.REJECTED.value for item in year_groups):
            state = "rejected"
            category = next(
                (quality_class(item) for item in year_groups
                 if str(item.get("status", "")) == ValidationStatus.REJECTED.value),
                "statement_unavailable",
            )
        elif any(str(item.get("status", "")) == ValidationStatus.READY_WITH_WARNINGS.value for item in year_groups):
            state = "warning"
            category = next(
                (quality_class(item) for item in year_groups
                 if str(item.get("status", "")) == ValidationStatus.READY_WITH_WARNINGS.value),
                "warning",
            )
        else:
            state = "verified"
            category = "verified"
        nodes.append({
            "period": str(year),
            "state": state,
            "quality_class": category,
            "comparison_only": bool(expected_years and year == expected_years[-1]),
        })

    problematic_groups = any(
        str(item.get("status", "")) in {
            ValidationStatus.REJECTED.value,
            ValidationStatus.READY_WITH_WARNINGS.value,
        }
        for item in groups
    )
    unresolved_recovery = any(
        str(item.get("status", "open")) != "resolved" for item in recovery_cases
    )
    retryable = (
        not available_years or bool(missing_years) or bool(unverified_years)
        or problematic_groups or unresolved_recovery
    )
    if not all_known_years:
        state = "unavailable"
        next_action = "retry_discovery"
    elif problematic_groups or unresolved_recovery:
        state = "warning"
        next_action = "retry_failed_nodes"
    elif missing_years or unverified_years:
        state = "incomplete"
        next_action = "retry_missing_periods" if missing_years else "retry_failed_nodes"
    else:
        state = "complete"
        next_action = "none"
    quality_summary = {
        category: sum(1 for item in nodes if item["quality_class"] == category)
        for category in (
            "verified", "warning", "field_missing", "statement_unavailable",
            "file_unavailable",
        )
    }
    if continuity_snapshot is None and run_id:
        reader = getattr(storage, "continuity_snapshot", None)
        try:
            continuity_snapshot = reader(run_id) if callable(reader) else {}
        except Exception:
            # Source health can still be read when optional recovery metadata
            # is unavailable. The report reader records that failure itself.
            continuity_snapshot = {}
    source_outcome = continuity_financial_outcome(continuity_snapshot)
    if source_outcome is not None:
        next_action = source_outcome["next_actions"][0]
        retryable = source_outcome["state"] == "waiting_external"
        retry = {**retry, "last_stage": "data-unavailable", "last_error": source_outcome["diagnostic_code"]}
    return {
        "state": state,
        "retryable": retryable,
        "history_years": history_years,
        "expected_periods": [str(year) for year in expected_years],
        "available_periods": [str(year) for year in available_years],
        "missing_periods": [str(year) for year in missing_years],
        "unverified_periods": [str(year) for year in unverified_years],
        "nodes": nodes,
        "issues": issues,
        "recovery_cases": recovery_cases,
        "quality_summary": quality_summary,
        "attempt_count": int(retry.get("attempt_count", 0)),
        "last_stage": str(retry.get("last_stage", "")),
        "last_error": str(retry.get("last_error", "")),
        "updated_at": str(retry.get("updated_at", "")),
        "next_action": next_action,
        "model_calls": 0,
        "token_delta": 0,
        "snapshot_stale": snapshot_stale,
        "recovery_outcome": source_outcome,
    }


def _financial_storage_key(company_payload: dict[str, Any]) -> str:
    """Return the identifier used by persisted filing and fact tables.

    SEC ingestion is keyed by the issuer's official CIK, while A/H market
    adapters are keyed by the listing security id. Keeping this decision in one
    place prevents status/retry rows from silently diverging from their facts.
    """

    market = str(company_payload.get("market", "")).strip().upper()
    if market == Market.US.value:
        return str(
            company_payload.get("cik")
            or company_payload.get("security_id")
            or ""
        )
    return str(
        company_payload.get("security_id")
        or company_payload.get("cik")
        or ""
    )


def _canonical_retry_snapshot(
    storage: Storage,
    company: Company,
    payload: dict[str, Any],
    *, partial_issues: list[dict[str, str]] | None = None,
) -> tuple[FinancialFact, ...]:
    """Admit only the run's current canonical facts, not a global retry gate."""

    def unavailable(code):
        if partial_issues is None:
            raise ValueError(code)
        partial_issues.append({"code": code, "path": "financial_snapshot"})
        return ()

    storage_key = _financial_storage_key(company.to_dict())
    filings = storage.get_filings(storage_key)
    saved = storage.get_financial_research_snapshot(storage_key, include_evidence=True)
    raw_facts = saved["facts"]
    if partial_issues is not None:
        partial_issues.extend({"code": item["code"], "path": item["path"]}
                              for item in saved.get("storage_issues", ()))
    facts: list[FinancialFact] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        try:
            facts.append(FinancialFact(**item))
        except (TypeError, ValueError):
            continue
    admitted = FinancialPipeline(None).prepare(FinancialResearchSnapshot.from_facts(
        company, filings, facts,
        evidence=tuple(EvidenceRef(**row) for row in saved.get("evidence", ())),
    ), CompilationPolicy(reporting_currency=company.reporting_currency))
    if not admitted.model_ready:
        return unavailable("FINANCIAL_DATA_QUALITY_FAILED")
    snapshot = payload.get("data_snapshot", {})
    expected = snapshot.get("financial_fact_ids_sha256") if isinstance(snapshot, dict) else None
    if not expected:
        return unavailable("FINANCIAL_SNAPSHOT_STALE")
    actual = _canonical_snapshot_digest(sorted(item.fact_id for item in admitted.facts))
    if str(expected) != actual:
        return unavailable("FINANCIAL_SNAPSHOT_STALE")
    expected_semantics = snapshot.get("financial_semantics_sha256")
    if expected_semantics:
        from .application.research_snapshot import financial_semantics_digest
        if str(expected_semantics) != financial_semantics_digest(admitted.fact_dicts):
            return unavailable("FINANCIAL_SNAPSHOT_STALE")
    return admitted.facts


def _reporting_currency_identity(
    storage: Storage, company: dict[str, Any], payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read-only metadata recovery from the run's still-valid official facts.

    A current company profile, trading quote or unrelated newer filing is not
    evidence of the currency of this historical research snapshot.
    """
    declared = str(company.get("reporting_currency") or "").strip().upper()
    if declared:
        return company, {"state": "saved_identity", "currency": declared, "source": "run"}
    unknown = {"state": "unresolved", "currency": "", "source": "none"}
    try:
        subject = Company(**{**company, "reporting_currency": ""})
        facts = _canonical_retry_snapshot(storage, subject, payload)
    except (KeyError, TypeError, ValueError):
        return company, unknown
    currencies = {str(fact.currency or "").strip().upper() for fact in facts
        if fact.statement in {"income_statement", "balance_sheet", "cash_flow"}
        and fact.concept != "reported_roe"}
    if len(currencies) != 1 or "" in currencies:
        return company, {**unknown, "state": "conflict" if len(currencies) > 1 else "unresolved"}
    currency = next(iter(currencies))
    return {**company, "reporting_currency": currency}, {"state": "official_verified",
        "currency": currency, "source": "canonical_run_snapshot", "fact_ids": sorted(fact.fact_id for fact in facts)}


def _parse_official_network_policy(value: str) -> OfficialNetworkPolicy:
    if not value:
        return OfficialNetworkPolicy()
    payload = json.loads(value)
    if (not isinstance(payload, dict) or set(payload) - {"version", "host_profiles", "connect_proxy"}
            or payload.get("version") != "official-network-v1"
            or not isinstance(payload.get("host_profiles", {}), dict)
            or not isinstance(payload.get("connect_proxy", ""), str)):
        raise ValueError("OFFICIAL_NETWORK_PROFILE_INVALID")
    return OfficialNetworkPolicy(host_profiles=payload.get("host_profiles", {}),
                                 connect_proxy=payload.get("connect_proxy", ""))


def _request_secrets(_request: dict[str, Any]) -> tuple[str, ...]:
    return ()


def _redact(message: str, secrets: tuple[str, ...]) -> str:
    safe = message
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    return safe
