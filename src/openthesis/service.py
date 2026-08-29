from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import locale
import random
import re
import threading
import time
import uuid
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .comparison import compare_research_runs
from .demo import DEMO_COMPANY, demo_facts
from .domain import Company, FilingDocument, FinancialFact, ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from .filing_parser import build_filing_evidence
from .filing_selection import select_research_filings
from .i18n import EN, ZH_HANT, normalize_language, resolve_system_language, resolve_ui_language, translate_error
from .market_data import MarketDataError, MarketDataModule
from .financial_ingestion import (
    FinancialIngestionEngine,
    FinancialDataset,
    FinancialGroupValidation,
    build_financial_profile,
    FinancialProfile,
)
from .financial_compiler import (
    CoveragePlanner,
    FinancialFactCompiler,
    concepts_cover_profile,
)
from .financials import deterministic_summary
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
from .providers import ModelConfig, create_provider
from .report_html import render_research_html
from .research import ResearchCancelled, ResearchWorkflow
from .reporting import render_research_run
from .sec_client import SEC_HK_ISSUERS, SecClient, SecClientError, SecFinancialSourceAdapter
from .storage import Storage
from .vision_financials import (
    GatewayVisionAdapter,
    MineruFlashAdapter,
    VisionAdapterError,
    VisionFallbackConfig,
    VisionFinancialSourceAdapter,
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
    "parallel_agents": "false",
    "research_market": "US",
    "sec_contact_profile": "personal",
    "sec_contact_email": "",
    "sec_user_agent": "",
}


class PreferenceValidationError(ValueError):
    """Raised when a caller attempts to persist an unsupported preference."""


class _ResearchDataUnavailable(RuntimeError):
    """Stop a run before model creation when official evidence is unavailable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _FinancialReportRefreshError(RuntimeError):
    """The financial stage finished but its deterministic report refresh failed."""

    code = "FILING_REPORT_REFRESH_FAILED"

    def __init__(self, result: dict[str, Any] | None = None):
        super().__init__(self.code)
        self.result = result or {}


@dataclass(frozen=True, slots=True)
class FinancialRetryResult:
    """Auditable outcome of a model-free financial refresh."""

    mode: str
    targets: tuple[str, ...] = ()
    downloaded: tuple[str, ...] = ()
    accepted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    status: str = "failed"
    error: str = ""
    updated_artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "targets": list(self.targets),
            "downloaded": list(self.downloaded),
            "accepted": list(self.accepted),
            "rejected": list(self.rejected),
            "status": self.status,
            "error": self.error,
            "updated_artifacts": list(self.updated_artifacts),
        }


@dataclass(slots=True)
class _ResearchJob:
    job_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
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
    vision_upload_preview: dict[str, Any] | None = None
    vision_approval: bool | None = None
    vision_approval_pending: bool = False
    vision_approval_event: threading.Event = field(default_factory=threading.Event, repr=False)
    operation_result: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        now = self.finished_at or time.perf_counter()
        timings = dict(self.stage_timings)
        timings[self.stage] = timings.get(self.stage, 0.0) + max(
            0.0, now - self.stage_started_at
        )
        return {
            "job_id": self.job_id,
            "state": self.state,
            "message": self.message,
            "percent": self.percent,
            "run_id": self.run_id,
            "stage": self.stage,
            "stage_current": self.stage_current,
            "stage_total": self.stage_total,
            "agent_states": dict(self.agent_states),
            "completed_agents": sum(state == "completed" for state in self.agent_states.values()),
            "total_agents": len(self.agent_states),
            "cancel_requested": self.cancel_requested,
            "elapsed_seconds": int(max(0, now - self.started_at)),
            "stage_elapsed_seconds": round(max(0.0, now - self.stage_started_at), 3),
            "stage_timings": {
                key: round(value, 3) for key, value in timings.items()
            },
            "error_code": self.error_code,
            "market": self.market,
            "disclosure_url": self.disclosure_url,
            "vision_upload_preview": dict(self.vision_upload_preview) if self.vision_upload_preview else None,
            "vision_approval": self.vision_approval,
            "vision_approval_pending": self.vision_approval_pending,
            "operation_result": dict(self.operation_result) if self.operation_result else None,
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
        financial_ingestion_engine: FinancialIngestionEngine | None = None,
        vision_adapter_factory: Callable[[VisionFallbackConfig], VisionFinancialSourceAdapter] | None = None,
    ):
        self.storage = Storage(data_dir)
        self.interrupted_run_count = self.storage.interrupt_running_runs()
        self.app_version = app_version
        self._sec_client_factory = sec_client_factory
        self._provider_factory = provider_factory
        self._market_data = market_data or MarketDataModule()
        self._financial_ingestion = financial_ingestion_engine or FinancialIngestionEngine()
        self._vision_adapter_factory = vision_adapter_factory or _default_vision_adapter_factory
        self._jobs: dict[str, _ResearchJob] = {}
        self._jobs_lock = threading.Lock()

    def hello(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "app_version": self.app_version,
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
                "research.start",
                "research.retry_growth",
                "research.retry_synthesis",
                "research.retry_financials",
                "research.rebuild_financials",
                "research.refresh_financial_report",
                "research.status",
                "research.cancel",
                "research.vision_decision",
            ],
        }

    def bootstrap(self) -> dict[str, Any]:
        return {
            **self.hello(),
            "preferences": self.preferences(),
            "recent_runs": self.list_research_runs(limit=20),
            "common_companies": self.common_companies(),
            "market_catalog": self.market_catalog(),
            "research_packs": self.research_packs(),
            "interrupted_runs": self.interrupted_run_count,
        }

    def preferences(self) -> dict[str, str]:
        stored_ui = self.storage.get_setting("ui_language", "")
        stored_mode = self.storage.get_setting("ui_language_mode", "")
        mode = stored_mode or ("manual" if stored_ui else "system")
        system_tag = locale.getlocale()[0] or "en"
        system_language = resolve_system_language((system_tag,))
        ui_language = resolve_ui_language(mode, stored_ui or system_language, (system_language,))
        report_stored = self.storage.get_setting("report_language", "")
        return {
            key: self.storage.get_setting(key, default)
            for key, default in PREFERENCE_DEFAULTS.items()
        } | {
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
            self.storage.set_setting(key, value)
        return self.preferences()

    def common_companies(self) -> list[dict[str, Any]]:
        return [
            company.to_dict()
            for company in (*COMMON_COMPANIES, *COMMON_MARKET_COMPANIES)
        ]

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
    ) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            raise ValueError("company query is required")
        selected_market = normalize_market(market)
        bounded_limit = min(50, max(1, int(limit)))
        if selected_market != Market.US:
            return [
                company.to_dict()
                for company in self._market_data.resolve(
                    normalized,
                    selected_market,
                    limit=bounded_limit,
                )
            ]
        preferences = self.preferences()
        profile = _normalize_sec_profile(preferences["sec_contact_profile"])
        user_agent = build_sec_user_agent(profile, preferences["sec_contact_email"])
        client = self._sec_client_factory(
            user_agent, self.storage.data_dir / "sec-cache"
        )
        return [
            company.to_dict()
            for company in client.search_companies(normalized, limit=bounded_limit)
        ]

    def list_research_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = min(200, max(1, int(limit)))
        summaries: list[dict[str, Any]] = []
        for row in self.storage.list_runs(limit=bounded_limit):
            payload = _decode_payload(row.get("payload_json"))
            company = payload.get("company", {})
            if not isinstance(company, dict):
                company = {}
            summaries.append(
                {
                    "run_id": row["run_id"],
                    "ticker": row["ticker"],
                    "company_name": row["name"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "report_language": normalize_language(
                        str(payload.get("report_language", "zh-CN"))
                    ),
                    "market": str(company.get("market", "US")),
                    "exchange": str(company.get("exchange", "")),
                    "listing_currency": str(company.get("listing_currency", "USD")),
                    "reporting_currency": str(company.get("reporting_currency", company.get("listing_currency", "USD"))),
                    "industry_support": str(company.get("industry_support", "standard")),
                }
            )
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
        return thesis

    def save_thesis_version(
        self, company_cik: str, content: dict[str, Any]
    ) -> dict[str, Any]:
        if not company_cik.strip() or not isinstance(content, dict):
            raise ValueError("thesis content is invalid")
        return self.storage.save_thesis_version(
            company_cik.strip(),
            content,
            created_by="user",
            created_at=utc_now_iso(),
        )

    def get_report(
        self,
        run_id: str,
        *,
        language: str | None = None,
        include_technical: bool = False,
    ) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        if run is None:
            raise KeyError("research run not found")
        payload = _decode_payload(run.get("payload_json"))
        report_language = normalize_language(
            language
            or str(payload.get("report_language", ""))
            or self.storage.get_setting("report_language", "zh-CN")
        )
        artifacts = self.storage.get_artifacts(run_id)
        company = payload.get("company", {})
        if not isinstance(company, dict):
            company = {}
        financial_status = _financial_status(self.storage, company, payload)
        return {
            "run_id": run_id,
            "ticker": run["ticker"],
            "company_name": run["name"],
            "status": run["status"],
            "report_language": report_language,
            "market": str(company.get("market", "US")),
            "exchange": str(company.get("exchange", "")),
            "listing_currency": str(company.get("listing_currency", "USD")),
            "reporting_currency": str(company.get("reporting_currency", company.get("listing_currency", "USD"))),
            "industry_support": str(company.get("industry_support", "standard")),
            "market_snapshot": payload.get("market_snapshot"),
            "reproducibility": {
                "model_configuration": payload.get("model_configuration", {}),
                "research_configuration": payload.get("research_configuration", {}),
                "data_snapshot": payload.get("data_snapshot", {}),
            },
            "retryable_synthesis": _report_retryable(artifacts),
            "retryable_growth": _growth_retryable(artifacts),
            "financial_status": financial_status,
            "markdown": render_research_run(
                run_id,
                artifacts,
                language=report_language,
                company_name=run["name"],
                include_technical=include_technical,
            ),
            "html": render_research_html(
                run_id,
                artifacts,
                language=report_language,
                company_name=run["name"],
                include_technical=include_technical,
            ),
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
    ) -> dict[str, Any]:
        """Refresh official financial evidence without constructing a model provider.

        This operation deliberately works one filing at a time.  A failed or
        rejected node is retained for audit, while a successful sibling is
        committed immediately through the accession-scoped storage operation.
        Calling it again is idempotent for verified nodes with an existing
        local document.
        """

        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        payload = _decode_payload(stored.get("payload_json"))
        company_payload = payload.get("company")
        if not isinstance(company_payload, dict):
            raise ValueError("saved company is invalid")
        company = Company(**company_payload)
        storage_key = _financial_storage_key(company.to_dict())
        retry_trace: dict[str, set[str]] = {
            "targets": set(), "downloaded": set(), "processed": set(), "warnings": set()
        }
        progress = progress or (lambda _stage, _current, _total: None)
        cancel_check = cancel_check or (lambda: False)
        if cancel_check():
            raise ResearchCancelled()
        progress("filing-discovery", 0, 1)
        errors: list[str] = []
        try:
            if normalize_market(company.market) == Market.US:
                errors.extend(self._retry_us_financials(
                    company, payload, force=force, trace=retry_trace,
                    progress=progress, cancel_check=cancel_check,
                ))
            else:
                errors.extend(self._retry_market_financials(
                    company, payload, force=force, trace=retry_trace,
                    progress=progress, cancel_check=cancel_check,
                ))
        except ResearchCancelled:
            raise
        except (MarketDataError, SecClientError, OSError) as exc:
            errors.append(getattr(exc, "code", "FILING_FETCH_FAILED"))
        except Exception as exc:
            errors.append(type(exc).__name__)
        target_accessions = tuple(sorted(retry_trace["targets"]))
        current_audit = self.storage.get_facts_audit(storage_key)
        processed_facts = [
            item for item in current_audit
            if str(item.get("accession_number", "")) in retry_trace["processed"]
        ]
        accepted_ids = tuple(
            sorted(
                str(item["fact_id"])
                for item in processed_facts
                if str(item.get("validation_status", "")).upper() != ValidationStatus.REJECTED.value
            )
        )
        rejected_ids = tuple(
            sorted(
                str(item["fact_id"])
                for item in processed_facts
                if str(item.get("validation_status", "")).upper() == ValidationStatus.REJECTED.value
            )
        )
        if not target_accessions and not accepted_ids and not self.storage.get_facts(storage_key):
            errors.append("quality:no_financial_facts")
        updated_artifacts: tuple[str, ...] = ()
        if not errors or accepted_ids:
            try:
                progress("artifact-rebuild", 0, 1)
                updated_artifacts = self._rebuild_financial_artifacts(
                    run_id, payload, company
                )
                progress("artifact-rebuild", 1, 1)
            except Exception as exc:
                errors.append(f"artifact-rebuild:{type(exc).__name__}")
        processed_any = bool(retry_trace["processed"])
        if accepted_ids and not errors:
            retry_status = "succeeded"
        elif (accepted_ids or processed_any) and errors:
            retry_status = "partial"
        else:
            retry_status = "failed" if errors else "succeeded"
        if any(":download:" in item for item in errors):
            stage = "filing-download"
        elif any(":parse:" in item for item in errors):
            stage = "filing-parse"
        elif errors:
            stage = "filing-validation"
        else:
            stage = "completed"
        self.storage.record_financial_retry_attempt(
            storage_key,
            stage=stage,
            error="; ".join(dict.fromkeys(errors))[:800],
        )
        retry_result = FinancialRetryResult(
            mode="rebuild" if force else "retry",
            targets=target_accessions,
            downloaded=tuple(sorted(retry_trace["downloaded"])),
            accepted=accepted_ids,
            rejected=rejected_ids,
            status=retry_status,
            error="; ".join(dict.fromkeys(errors))[:800],
            updated_artifacts=updated_artifacts,
        )
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
                    storage_key,
                    stage="report-refresh",
                    error="FILING_REPORT_REFRESH_FAILED",
                )
                failed_result = replace(
                    retry_result, status="partial" if accepted_ids else "failed",
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
            ui_language=normalize_language(self.preferences().get("ui_language", "zh-CN")),
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
        return job.snapshot()

    def _run_financial_retry_job(
        self, job: _ResearchJob, run_id: str, force: bool
    ) -> None:
        language = job.ui_language
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
            )
            retry = report.get("financial_retry", {})
            operation_status = str(retry.get("status", "failed"))
            succeeded = operation_status == "succeeded"
            self._update_job(
                job,
                state="completed" if succeeded else "failed",
                stage="completed" if succeeded else "financial-validation",
                percent=100,
                operation_result=dict(retry) if isinstance(retry, dict) else {},
                error_code=(
                    None if succeeded
                    else "FILING_DATA_QUALITY_PARTIAL" if operation_status == "partial"
                    else "FILING_DATA_QUALITY_FAILED"
                ),
                message=(
                    _ui_message(language, "Financial refresh completed", "财务数据刷新完成", "財務資料刷新完成")
                    if succeeded
                    else _ui_message(language, "Financial refresh completed with unresolved fields", "财务资料已重建，但仍有字段未解决", "財務資料已重建，但仍有欄位未解決")
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
        """Recompute the deterministic view after a financial refresh.

        This deliberately reads only the storage-visible accepted facts and
        validation groups.  Rejected, foreign-currency and non-consolidated
        audit rows therefore cannot leak into metrics or the report snapshot.
        Each new artifact receives a unique timestamped identity, so readers
        never mistake the previous artifact for the current refresh.
        """

        storage_key = _financial_storage_key(company.to_dict())
        fact_rows = self.storage.get_facts(storage_key)
        audit_rows = self.storage.get_facts_audit(storage_key)
        allowed = set(FinancialFact.__dataclass_fields__)
        facts = [
            FinancialFact(**{key: row.get(key) for key in allowed})
            for row in fact_rows
            if str(row.get("validation_status", "")).upper() != ValidationStatus.REJECTED.value
            and str(row.get("consolidated_scope", row.get("scope", "consolidated"))).lower() == "consolidated"
            and (
                not company.reporting_currency
                or not row.get("currency")
                or str(row.get("currency")).upper() == company.reporting_currency.upper()
            )
        ]
        if not facts:
            return ()
        groups: list[FinancialGroupValidation] = []
        for row in self.storage.get_validation_groups(storage_key):
            identity = (
                str(row.get("accession_number", "")),
                str(row.get("period_end", "")),
                str(row.get("fiscal_period", "")),
                str(row.get("consolidated_scope", "")),
                str(row.get("currency", "")),
            )
            if not all(identity):
                continue
            try:
                status = ValidationStatus(str(row.get("status", "REJECTED")))
            except ValueError:
                status = ValidationStatus.REJECTED
            group_facts = tuple(
                item for item in facts if item.accession_number == identity[0]
            )
            quarantined = tuple(
                FinancialFact(
                    **{key: item.get(key) for key in allowed}
                )
                for item in audit_rows
                if item.get("accession_number") == identity[0]
                and str(item.get("validation_status", "")).upper() == ValidationStatus.REJECTED.value
            )
            validation = FinancialValidation(
                status,
                tuple(str(item) for item in row.get("issues", [])),
                frozenset(str(item) for item in row.get("covered_concepts", [])),
                group_facts,
                quarantined,
            )
            groups.append(FinancialGroupValidation(identity, validation))
        profile = build_financial_profile(
            facts,
            groups,
            company.reporting_currency,
            selected_filings=self.storage.get_filings(storage_key),
        )
        from .research import build_fact_evidence

        evidence = build_fact_evidence(list(profile.fact_dicts))
        metrics = list(profile.metrics)
        interim_metrics = list(profile.interim_metrics)
        summary = deterministic_summary(
            company.name,
            metrics,
            str(payload.get("report_language", "zh-CN")),
            company.reporting_currency,
        )
        digest = _canonical_snapshot_digest(
            [item.get("fact_id") for item in profile.fact_dicts]
        )[:12]
        quality = {
            "status": profile.status.value,
            "rejected_periods": list(profile.rejected_periods),
            "period_continuity": list(profile.period_continuity),
        }
        summary_artifact = ResearchArtifact(
            artifact_id=f"{run_id}:deterministic-financial-summary:retry-{digest}",
            run_id=run_id,
            artifact_type="deterministic-financial-summary",
            title="Deterministic Financial Overview",
            content={
                "markdown": summary,
                "metrics": metrics,
                "interim_metrics": interim_metrics,
                "evidence": evidence,
                "currency": company.reporting_currency,
                "financial_quality": quality,
            },
            agent_id="calculation-engine-retry",
        )
        stored = self.storage.get_run(run_id)
        if stored is None:
            return ()
        if stored is not None:
            payload = _decode_payload(stored.get("payload_json"))
            payload["data_snapshot"] = {
                **dict(payload.get("data_snapshot", {})),
                "captured_at": utc_now_iso(),
                "financial_fact_count": len(profile.fact_dicts),
                "financial_fact_ids_sha256": _canonical_snapshot_digest(
                    sorted(item.get("fact_id", "") for item in profile.fact_dicts)
                ),
            }
            existing_report = next(
                (
                    item for item in reversed(self.storage.get_artifacts(run_id))
                    if item.get("artifact_type") == "research-report"
                ),
                None,
            )
            content = dict(existing_report.get("content", {})) if existing_report else {}
            content["mode"] = "financial-refresh"
            content["financial_refresh"] = {
                "updated_at": utc_now_iso(),
                "fact_count": len(profile.fact_dicts),
                "status": profile.status.value,
                "model_called": False,
                "qualitative_snapshot_stale": True,
            }
            report_value = content.get("report")
            if isinstance(report_value, dict):
                report_value = dict(report_value)
                report_value["financial_quality"] = quality
                content["report"] = report_value
            else:
                content["financial_quality"] = quality
            report_artifact = ResearchArtifact(
                artifact_id=f"{run_id}:research-report:retry-{digest}",
                run_id=run_id,
                artifact_type="research-report",
                title="Financial Refresh Report",
                content=content,
                agent_id="financial-refresh",
            )
            payload["financial_profile"] = {
                "status": profile.status.value,
                "metrics": metrics,
                "interim_metrics": interim_metrics,
            }
            run_data = dict(payload)
            run = ResearchRun(
                run_id=run_id,
                company=company,
                workflow_id=str(run_data.get("workflow_id", "")),
                research_pack_id=str(run_data.get("research_pack_id", "")),
                research_pack_version=str(run_data.get("research_pack_version", "")),
                provider_id=str(run_data.get("provider_id", "")),
                model_id=str(run_data.get("model_id", "")),
                data_as_of=str(run_data.get("data_as_of", utc_now_iso())),
                status=RunStatus(str(stored.get("status", RunStatus.PARTIAL.value))),
                started_at=str(run_data.get("started_at", stored.get("started_at", utc_now_iso()))),
                completed_at=stored.get("completed_at"),
                errors=list(run_data.get("errors", [])),
                report_language=str(run_data.get("report_language", "zh-CN")),
                market_snapshot=run_data.get("market_snapshot"),
                model_configuration=dict(run_data.get("model_configuration", {})),
                research_configuration=dict(run_data.get("research_configuration", {})),
                data_snapshot=dict(payload["data_snapshot"]),
            )
            self.storage.save_run_with_artifacts(
                run, [summary_artifact, report_artifact]
            )
        return ("deterministic-financial-summary", "research-report")

    def _retry_market_financials(
        self, company: Company, payload: dict[str, Any], *, force: bool = False,
        trace: dict[str, set[str]] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[str]:
        progress = progress or (lambda _stage, _current, _total: None)
        cancel_check = cancel_check or (lambda: False)
        if cancel_check():
            raise ResearchCancelled()
        adapter = self._market_data.adapter_for(company)
        configuration = payload.get("research_configuration", {})
        history_years = _research_history_years({
            "evidence_policy": {
                "annual_history_years": configuration.get("annual_history_years", 5)
            }
        } if isinstance(configuration, dict) else {})
        candidates = adapter.list_financial_filings(company, limit=history_years + 3)
        plan = select_research_filings(candidates, annual_limit=history_years)
        progress("filing-discovery", 1, 1)
        stored_filings = self.storage.get_filings(company.security_id)
        stored_by_accession: dict[str, FilingDocument] = {
            item.accession_number: item for item in stored_filings if item.accession_number
        }
        planned: list[FilingDocument] = []
        for item in plan.documents:
            # Fresh discovery metadata wins, including a corrected revision.
            previous = stored_by_accession.get(item.accession_number)
            if previous is not None:
                if not item.local_path:
                    item.local_path = previous.local_path
                if not item.content_hash:
                    item.content_hash = previous.content_hash
            planned.append(item)
        groups = self.storage.get_validation_groups(company.security_id)
        statuses: dict[str, set[str]] = {}
        for group in groups:
            statuses.setdefault(str(group.get("accession_number", "")), set()).add(
                str(group.get("status", "")).upper()
            )
        unhealthy = {"REJECTED", "READY_WITH_WARNINGS", "UNVALIDATED", ""}

        def needs_refresh(filing: FilingDocument) -> bool:
            path_ok = bool(filing.local_path) and Path(filing.local_path).is_file()
            current = statuses.get(filing.accession_number)
            return (
                not path_ok
                or not current
                or bool(current & unhealthy)
            )

        targets = list(planned) if force else [item for item in planned if needs_refresh(item)]
        if trace is not None:
            trace["targets"].update(
                item.accession_number for item in targets if item.accession_number
            )
        if not targets:
            return []
        target_dir = self.storage.filings_dir / company.security_id.replace(":", "_")
        cached = [] if force else [
            item for item in targets
            if item.local_path and Path(item.local_path).is_file()
        ]
        needs_download = [item for item in targets if item not in cached]
        progress("filing-download", 0, len(needs_download))
        downloaded, download_errors = _bounded_download_filings(
            adapter, needs_download, target_dir
        )
        if cancel_check():
            raise ResearchCancelled()
        progress("filing-download", len(downloaded), len(needs_download))
        if trace is not None:
            trace["downloaded"].update(
                item.accession_number for item in downloaded if item.accession_number
            )
        errors = [
            f"{filing.accession_number}:download:{type(error).__name__}"
            for filing, error in download_errors
        ]
        downloaded_by_accession = {item.accession_number: item for item in downloaded}
        cached_by_accession = {item.accession_number: item for item in cached}
        reparsed = [
            downloaded_by_accession.get(item.accession_number)
            or cached_by_accession.get(item.accession_number)
            for item in targets
        ]
        parse_targets = [item for item in reparsed if item is not None]
        for index, filing in enumerate(parse_targets, start=1):
            if cancel_check():
                raise ResearchCancelled()
            progress("filing-parse", index - 1, len(parse_targets))
            try:
                if hasattr(self._financial_ingestion, "collect_candidate_batches"):
                    dataset = FinancialFactCompiler().compile_from_ingestion(
                        company,
                        [filing],
                        self._financial_ingestion,
                        cancel_check=cancel_check,
                        progress=progress,
                        reporting_currency=company.reporting_currency,
                    )
                else:
                    # Compatibility-only seam for injected pre-canonical
                    # engines in legacy tests.  Production always supplies
                    # FinancialIngestionEngine.collect_candidate_batches.
                    dataset = self._financial_ingestion.ingest(company, [filing])
            except Exception as exc:
                errors.append(f"{filing.accession_number}:parse:{type(exc).__name__}")
                continue
            manifest_by_id = {item.document_id: item for item in dataset.manifest}
            manifest = manifest_by_id.get(filing.document_id)
            if manifest is not None:
                filing.form_type = manifest.form_type
                filing.fiscal_period = manifest.fiscal_period
                filing.period_end = manifest.period_end
                filing.revision = manifest.revision
                filing.supersedes_document_id = manifest.supersedes_document_id
                filing.content_hash = manifest.content_hash
            self.storage.save_filings([filing])
            # Recompile retry output through the canonical compiler.  The
            # ingestion dataset remains the audit source, but only compiler
            # resolved facts can be persisted as research facts.
            canonical = (
                dataset
                if hasattr(dataset, "research_facts")
                else FinancialFactCompiler().compile_facts(
                    company,
                    [filing],
                    dataset.accepted_facts,
                    reporting_currency=company.reporting_currency,
                )
            )
            quarantined: list[FinancialFact] = list(dataset.validation.quarantined)
            for group in dataset.group_validations:
                quarantined.extend(group.validation.quarantined)
            quarantined.extend(canonical.quarantined_facts)
            seen_quarantine: set[str] = set()
            unique_quarantine: list[FinancialFact] = []
            for fact in quarantined:
                if fact.fact_id in seen_quarantine:
                    continue
                seen_quarantine.add(fact.fact_id)
                unique_quarantine.append(fact)
            quarantined = unique_quarantine
            # Persist structurally validated siblings for deterministic retry
            # audit/reporting, but only ``canonical.research_facts`` is ever a
            # research/model input.  INCOMPLETE groups therefore remain
            # visible without being promoted to AI-eligible data.
            accepted_facts = list({fact.fact_id: fact for fact in canonical.resolved_facts}.values())
            canonical_groups = _compiler_validation_groups(canonical.validations)
            self.storage.replace_financial_ingestion(
                company.security_id,
                [filing.accession_number],
                accepted_facts,
                quarantined,
                canonical_groups,
                list(dataset.evidence) + list(build_filing_evidence([filing])),
            )
            if accepted_facts and canonical.allow_ai is False and any(
                item.validation.status is ValidationStatus.READY_WITH_WARNINGS
                and item.validation.quality_class == "field_missing"
                for item in dataset.group_validations
            ):
                # A partial, auditable repair is useful for deterministic
                # reporting, but must never be presented as a full success.
                errors.append(f"{filing.accession_number}:quality:field_missing")
            if trace is not None and filing.accession_number:
                trace["processed"].add(filing.accession_number)
            progress("filing-validation", index, len(parse_targets))
            if not accepted_facts:
                # A parser can produce auditable but incomplete fields.  Keep
                # that node visibly partial while reserving the hard quality
                # failure for files with no accepted facts at all.
                errors.append(
                    f"{filing.accession_number}:quality:"
                    f"{'incomplete_profile' if dataset.accepted_facts else 'FILING_DATA_QUALITY_FAILED'}"
                )
        return errors

    def _retry_us_financials(
        self, company: Company, payload: dict[str, Any], *, force: bool = False,
        trace: dict[str, set[str]] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[str]:
        progress = progress or (lambda _stage, _current, _total: None)
        cancel_check = cancel_check or (lambda: False)
        if cancel_check():
            raise ResearchCancelled()
        config = payload.get("research_configuration", {})
        history = 5
        if isinstance(config, dict):
            try:
                history = max(2, min(10, int(config.get("annual_history_years", 5))))
            except (TypeError, ValueError):
                history = 5
        # SEC filing/fact tables are keyed by the official CIK. Do not rely on
        # ``security_id`` merely happening to equal the CIK for common US rows.
        stored_filings = self.storage.get_filings(company.cik)
        stored_facts = self.storage.get_facts(company.cik)
        if not force and _cached_us_annual_window_is_complete(
            company, stored_filings, stored_facts, required_count=history + 1
        ):
            return []

        preferences = self.preferences()
        client = self._sec_client_factory(
            build_sec_user_agent(
                _normalize_sec_profile(preferences["sec_contact_profile"]),
                preferences["sec_contact_email"],
            ),
            self.storage.data_dir / "sec-cache",
        )
        filings = client.list_annual_filings(company, limit=history + 1)
        progress("filing-discovery", 1, 1)
        stored_by_accession = {
            item.accession_number: item for item in stored_filings if item.accession_number
        }
        known_fact_accessions = {
            str(item.get("accession_number", ""))
            for item in stored_facts
        }
        for item in filings:
            previous = stored_by_accession.get(item.accession_number)
            if previous is not None:
                item.local_path = item.local_path or previous.local_path
                item.content_hash = item.content_hash or previous.content_hash
        targets = list(filings) if force else [
            item for item in filings
            if not item.local_path or not Path(item.local_path).is_file()
            or item.accession_number not in known_fact_accessions
        ]
        if trace is not None:
            trace["targets"].update(
                item.accession_number for item in targets if item.accession_number
            )
        target_dir = self.storage.filings_dir / company.cik
        downloaded, download_errors = _bounded_download_filings(client, targets, target_dir)
        if cancel_check():
            raise ResearchCancelled()
        progress("filing-download", len(downloaded), len(targets))
        if trace is not None:
            trace["downloaded"].update(
                item.accession_number for item in downloaded if item.accession_number
            )
        errors = [
            f"{filing.accession_number}:download:{type(error).__name__}"
            for filing, error in download_errors
        ]
        if downloaded:
            self.storage.save_filings(downloaded)
        normalized = client.get_company_facts(company)
        if cancel_check():
            raise ResearchCancelled()
        progress("filing-parse", 1, 1)
        expected_end = max(
            (str(item.period_end) for item in filings if item.form_type in {"10-K", "20-F", "40-F"}),
            default="",
        )
        accepted = _latest_sec_verified_group(
            normalized, self._financial_ingestion, company=company,
            expected_period_end=expected_end
        )
        if accepted is None:
            errors.append("quality:FILING_DATA_QUALITY_FAILED")
            return errors
        # Company Facts are already normalized and provenance-rich.  Persist
        # only the group that passed the same validator; failed alternatives
        # remain untouched for audit and cannot displace prior good facts.
        self.storage.save_facts(list(accepted))
        if trace is not None:
            trace["processed"].update(
                fact.accession_number for fact in accepted if fact.accession_number
            )
        progress("filing-validation", 1, 1)
        return errors

    def retry_research_synthesis(
        self, run_id: str, model: dict[str, Any]
    ) -> dict[str, Any]:
        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        payload = _decode_payload(stored.get("payload_json"))
        company_payload = payload.get("company")
        if not isinstance(company_payload, dict):
            raise ValueError("saved company is invalid")
        config = _model_config_from_request(model)
        if not config.enabled:
            raise ValueError("an enabled model is required")
        company = Company(**company_payload)
        retry_facts = _canonical_retry_snapshot(self.storage, company, payload)
        run = ResearchRun(
            run_id=run_id,
            company=company,
            workflow_id=str(payload.get("workflow_id", "long-term-fundamentals")),
            research_pack_id=str(payload.get("research_pack_id", "")),
            research_pack_version=str(payload.get("research_pack_version", "")),
            provider_id=config.provider,
            model_id=config.model,
            data_as_of=str(payload.get("data_as_of", date.today().isoformat())),
            status=RunStatus(str(stored.get("status", "partial"))),
            started_at=str(payload.get("started_at", stored.get("started_at", utc_now_iso()))),
            completed_at=stored.get("completed_at"),
            errors=list(payload.get("errors", [])),
            report_language=normalize_language(str(payload.get("report_language", "zh-CN"))),
            market_snapshot=payload.get("market_snapshot"),
        )
        workflow = ResearchWorkflow(
            self.storage,
            self._select_pack(run.research_pack_id),
            self._provider_factory(config),
            config,
            report_language=run.report_language,
            ui_language=normalize_language(self.preferences().get("ui_language", "zh-CN")),
            parallel_agents=False,
        )
        workflow.retry_synthesis(
            run,
            self.storage.get_artifacts(run_id),
            [fact.to_dict() for fact in retry_facts],
        )
        return self.get_report(run_id, language=run.report_language)

    def retry_research_growth(
        self, run_id: str, model: dict[str, Any]
    ) -> dict[str, Any]:
        stored = self.storage.get_run(run_id)
        if stored is None:
            raise KeyError("research run not found")
        payload = _decode_payload(stored.get("payload_json"))
        company_payload = payload.get("company")
        if not isinstance(company_payload, dict):
            raise ValueError("saved company is invalid")
        config = _model_config_from_request(model)
        if not config.enabled:
            raise ValueError("an enabled model is required")
        company = Company(**company_payload)
        retry_facts = _canonical_retry_snapshot(self.storage, company, payload)
        run = ResearchRun(
            run_id=run_id,
            company=company,
            workflow_id=str(payload.get("workflow_id", "long-term-fundamentals")),
            research_pack_id=str(payload.get("research_pack_id", "")),
            research_pack_version=str(payload.get("research_pack_version", "")),
            provider_id=config.provider,
            model_id=config.model,
            data_as_of=str(payload.get("data_as_of", date.today().isoformat())),
            status=RunStatus(str(stored.get("status", "partial"))),
            started_at=str(payload.get("started_at", stored.get("started_at", utc_now_iso()))),
            completed_at=stored.get("completed_at"),
            errors=list(payload.get("errors", [])),
            report_language=normalize_language(str(payload.get("report_language", "zh-CN"))),
            market_snapshot=payload.get("market_snapshot"),
        )
        workflow = ResearchWorkflow(
            self.storage,
            self._select_pack(run.research_pack_id),
            self._provider_factory(config),
            config,
            report_language=run.report_language,
            ui_language=normalize_language(self.preferences().get("ui_language", "zh-CN")),
            parallel_agents=False,
        )
        workflow.retry_growth(
            run,
            self.storage.get_artifacts(run_id),
            [fact.to_dict() for fact in retry_facts],
        )
        return self.get_report(run_id, language=run.report_language)

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

        job = _ResearchJob(
            job_id=uuid.uuid4().hex,
            ui_language=normalize_language(self.preferences().get("ui_language", "zh-CN")),
        )
        with self._jobs_lock:
            self._jobs[job.job_id] = job
        threading.Thread(
            target=self._run_research,
            args=(job, dict(request)),
            name=f"openthesis-research-{job.job_id[:8]}",
            daemon=True,
        ).start()
        return job.snapshot()

    def get_research_status(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("research job not found")
            return job.snapshot()

    def cancel_research(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("research job not found")
            job.cancel_event.set()
            if job.vision_approval_pending:
                # Wake an approval waiter immediately; the callback checks the
                # cancellation flag before allowing any upload.
                job.vision_approval_event.set()
            if job.state in {"queued", "running"}:
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
            return job.snapshot()

    def vision_decision(self, job_id: str, approved: bool) -> dict[str, Any]:
        """Resolve a session-only failed-page upload approval request."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("research job not found")
            if job.stage != "vision-approval" or not job.vision_approval_pending:
                raise ValueError("vision upload approval is not pending")
            job.vision_approval = bool(approved)
            job.vision_approval_pending = False
            job.vision_approval_event.set()
            return job.snapshot()

    def _update_job(self, job: _ResearchJob, **updates: Any) -> None:
        with self._jobs_lock:
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

    def _ingestion_progress(
        self,
        job: _ResearchJob,
        stage: str,
        current: int,
        total: int,
    ) -> None:
        safe_total = max(1, int(total))
        safe_current = min(safe_total, max(0, int(current)))
        if stage == "filing-parse":
            percent = 18 + round(safe_current * 6 / safe_total)
        elif stage == "filing-validation":
            percent = 24 + round(safe_current * 5 / safe_total)
        else:
            percent = max(job.percent, 24)
        self._update_job(
            job,
            stage=stage,
            stage_current=safe_current,
            stage_total=safe_total,
            percent=max(job.percent, percent),
        )

    def _auto_retry_financial_evidence(
        self,
        job: _ResearchJob,
        request: dict[str, Any],
        company: Company | None,
        ui_language: str,
    ) -> bool:
        """Run the one allowed model-free repair attempt for a research job."""

        if company is None or request.get("_financial_auto_retry_done"):
            return False
        request["_financial_auto_retry_done"] = True
        self._update_job(
            job,
            stage="financial-retry",
            message=_ui_message(
                ui_language,
                "Refreshing failed financial evidence without a model",
                "正在无模型重试财务资料",
                "正在無模型重試財務資料",
            ),
            percent=max(24, job.percent),
        )
        trace: dict[str, set[str]] = {
            "targets": set(), "downloaded": set(), "processed": set()
        }
        progress = lambda stage, current, total: self._ingestion_progress(
            job, stage, current, total
        )
        errors = (
            self._retry_us_financials(
                company, request, trace=trace, progress=progress,
                cancel_check=job.cancel_event.is_set,
            )
            if normalize_market(company.market) == Market.US
            else self._retry_market_financials(
                company, request, trace=trace, progress=progress,
                cancel_check=job.cancel_event.is_set,
            )
        )
        return not errors and bool(trace["processed"])

    def _run_research(self, job: _ResearchJob, request: dict[str, Any]) -> None:
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
            state="running",
            stage="preparing",
            message=_ui_message(ui_language, "Preparing research data", "正在准备研究数据", "正在準備研究資料"),
            percent=2,
        )
        try:
            mode = request.get("mode")
            company = (
                DEMO_COMPANY
                if mode == "demo"
                else _company_from_request(request.get("company"))
            )
            company_market = normalize_market(company.market)
            market_profile = MARKET_PROFILES[company_market]
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
                preferences.get("parallel_agents", "false") == "true",
            )
            market_snapshot = _market_snapshot(
                request.get("market_snapshot"),
                company,
            )
            valuation_inputs = _valuation_inputs(request.get("valuation"))
            if market_snapshot and market_snapshot.get("market_cap", 0) > 0:
                valuation_inputs = {
                    "market_cap": market_snapshot["market_cap"],
                    "discount_rate": (valuation_inputs or {}).get("discount_rate", 0.10),
                    "terminal_growth": (valuation_inputs or {}).get("terminal_growth", 0.03),
                }
            filing_evidence: list[dict[str, Any]] = []
            financial_profile: FinancialProfile | None = None
            vision_config = _vision_config_from_request(request.get("vision_fallback"))
            vision_adapter: VisionFinancialSourceAdapter | None = None
            if vision_config is not None and vision_config.enabled:
                if vision_config.require_page_approval:
                    def approve_upload(summary: dict[str, Any]) -> bool:
                        safe_summary = {
                            key: summary.get(key)
                            for key in ("provider", "pages", "total_bytes", "source_document", "filing_hash", "document_hashes")
                            if key in summary
                        }
                        # Establish the pending state and clear stale
                        # decisions atomically before publishing the preview.
                        # This prevents a fast UI decision from being lost
                        # between stage publication and event.clear().
                        with self._jobs_lock:
                            if job.cancel_event.is_set():
                                return False
                            job.vision_approval_event.clear()
                            job.vision_approval = None
                            job.vision_approval_pending = True
                            job.vision_upload_preview = safe_summary
                            job.stage = "vision-approval"
                            job.message = "Review failed financial pages before upload"
                        while not job.vision_approval_event.wait(0.1):
                            if job.cancel_event.is_set():
                                with self._jobs_lock:
                                    job.vision_approval_pending = False
                                return False
                        with self._jobs_lock:
                            approved = bool(job.vision_approval)
                            cancelled = job.cancel_event.is_set()
                            job.vision_approval_pending = False
                            job.vision_upload_preview = None
                        return approved and not cancelled
                    vision_config = replace(vision_config, approve_upload=approve_upload)
                try:
                    vision_config.validate()
                    vision_adapter = self._vision_adapter_factory(vision_config)
                except VisionAdapterError as exc:
                    raise _ResearchDataUnavailable(exc.code) from exc
                self._update_job(
                    job,
                    stage="filing-discovery",
                    message=_ui_message(
                        ui_language,
                        "Vision fallback is enabled; it will upload only failed financial-table pages",
                        "视觉财报兜底已启用，仅在本地失败后上传失败的财务表页",
                        "雲端財報備援已啟用，只會在本地解析失敗後上傳失敗的財務表頁",
                    ),
                    percent=3,
                )

            self.storage.save_company(company)
            if mode == "demo":
                facts = demo_facts()
                self.storage.save_facts([FinancialFact(**item) for item in facts])
                self._update_job(
                    job,
                    message=_ui_message(ui_language, "Synthetic data ready", "演示数据准备完成", "示範資料準備完成"),
                    percent=30,
                )
            elif company_market == Market.US:
                client = self._sec_client_factory(
                    build_sec_user_agent(
                        _normalize_sec_profile(preferences["sec_contact_profile"]),
                        preferences["sec_contact_email"],
                    ),
                    self.storage.data_dir / "sec-cache",
                )
                self._update_job(
                    job,
                    message=_ui_message(ui_language, "Loading SEC annual filings", "正在获取 SEC 年报清单", "正在取得 SEC 年報清單"),
                    percent=5,
                )
                filings = client.list_annual_filings(company, limit=history_years + 1)
                if not filings:
                    raise _ResearchDataUnavailable("NO_FILINGS_AVAILABLE")
                if bool(request.get("download_filings", True)):
                    target = self.storage.filings_dir / company.cik
                    self._update_job(
                        job,
                        stage="filing-download",
                        stage_current=0,
                        stage_total=len(filings),
                        message=_ui_message(
                            ui_language,
                            "Downloading SEC annual reports",
                            "正在下载 SEC 年报",
                            "正在下載 SEC 年報",
                        ),
                        percent=6,
                    )
                    downloaded, download_errors = _bounded_download_filings(
                        client,
                        filings,
                        target,
                        cancel_event=job.cancel_event,
                        progress=lambda completed, total: self._update_job(
                            job,
                            stage="filing-download",
                            stage_current=completed,
                            stage_total=total,
                            message=_ui_message(
                                ui_language,
                                f"Downloading SEC annual reports ({completed}/{total})",
                                f"正在下载 SEC 年报（{completed}/{total}）",
                                f"正在下載 SEC 年報（{completed}/{total}）",
                            ),
                            percent=6 + round(completed * 10 / max(1, total)),
                        ),
                    )
                    filings = downloaded
                    if job.cancel_event.is_set():
                        raise ResearchCancelled()
                    if not downloaded and download_errors:
                        raise _ResearchDataUnavailable("FILING_DOWNLOAD_FAILED")
                    filing_evidence = build_filing_evidence(filings)
                self.storage.save_filings(filings)
                if job.cancel_event.is_set():
                    raise ResearchCancelled()
                self._update_job(
                    job,
                    stage="filing-parse",
                    stage_current=0,
                    stage_total=len(filings),
                    message=_ui_message(ui_language, "Loading SEC Company Facts", "正在获取 SEC Company Facts", "正在取得 SEC Company Facts"),
                    percent=23,
                )
                normalized = client.get_company_facts(company)
                expected_period_end = max(
                    (str(item.period_end) for item in filings if item.form_type in {"10-K", "20-F", "40-F"}),
                    default="",
                )
                latest_sec = _latest_sec_verified_group(
                    normalized, self._financial_ingestion, company=company,
                    expected_period_end=expected_period_end
                )
                if latest_sec is None:
                    raise _ResearchDataUnavailable("FILING_DATA_QUALITY_FAILED")
                # Recompile the complete requested annual window through the
                # same canonical target view.  The latest-group probe above is
                # only a display/selection check; model input must never use
                # the raw Company Facts list or incomplete sibling groups.
                # The synthetic DEMO fixture predates accession-linked SEC
                # facts, so retain its explicit compatibility path only.
                matched_accessions = {
                    item.accession_number for item in filings
                }
                if normalized and all(item.form_type == "DEMO" for item in normalized) \
                        and not any(item.accession_number in matched_accessions for item in normalized):
                    facts = [item.to_dict() for item in latest_sec]
                else:
                    canonical = FinancialFactCompiler().compile_facts(
                        company,
                        filings,
                        normalized,
                        reporting_currency=company.reporting_currency,
                    )
                    if not canonical.allow_ai:
                        raise _ResearchDataUnavailable("FILING_DATA_QUALITY_FAILED")
                    self.storage.save_facts(list(canonical.research_facts))
                    facts = [item.to_dict() for item in canonical.research_facts]
                self._update_job(
                    job,
                    stage="filing-validation",
                    stage_current=len(filings),
                    stage_total=len(filings),
                    percent=29,
                )
                if normalized and all(item.form_type == "DEMO" for item in normalized) \
                        and not any(item.accession_number in matched_accessions for item in normalized):
                    self.storage.save_facts(list(latest_sec))
            else:
                adapter = self._market_data.adapter_for(company)
                market_label = _ui_message(ui_language, "A/H-share", "A/港股", "A/港股")
                self._update_job(
                    job,
                    stage="filing-discovery",
                    message=_ui_message(
                        ui_language,
                        f"Loading official {market_label} financial reports",
                        f"正在获取{market_label}官方财报清单",
                        f"正在取得{market_label}官方財報清單",
                    ),
                    percent=5,
                )
                candidates = adapter.list_financial_filings(company, limit=history_years + 3)
                if not candidates:
                    raise _ResearchDataUnavailable("NO_FILINGS_AVAILABLE")
                plan = select_research_filings(candidates, annual_limit=history_years)
                filings = list(plan.documents)
                downloaded = []
                if bool(request.get("download_filings", True)):
                    target = self.storage.filings_dir / company.security_id.replace(":", "_")
                    self._update_job(
                        job,
                        stage="filing-download",
                        stage_current=0,
                        stage_total=len(filings),
                        message=_ui_message(
                            ui_language,
                            "Downloading official financial reports",
                            "正在下载官方财报",
                            "正在下載官方財報",
                        ),
                        percent=6,
                    )
                    downloaded, download_errors = _bounded_download_filings(
                        adapter,
                        filings,
                        target,
                        cancel_event=job.cancel_event,
                        progress=lambda completed, total: self._update_job(
                            job,
                            stage="filing-download",
                            stage_current=completed,
                            stage_total=total,
                            message=_ui_message(
                                ui_language,
                                f"Downloading official reports ({completed}/{total})",
                                f"正在下载官方财报（{completed}/{total}）",
                                f"正在下載官方財報（{completed}/{total}）",
                            ),
                            percent=6 + round(completed * 10 / max(1, total)),
                        ),
                    )
                    if job.cancel_event.is_set():
                        raise ResearchCancelled()
                    if not downloaded and download_errors:
                        raise _ResearchDataUnavailable("FILING_DOWNLOAD_FAILED")
                    filings = downloaded
                else:
                    raise _ResearchDataUnavailable("FILING_DOWNLOAD_REQUIRED")
                self.storage.save_filings(filings)
                research_reports = list(downloaded)
                self._update_job(
                    job,
                    stage="filing-parse",
                    stage_current=0,
                    stage_total=len(research_reports),
                    percent=18,
                )
                structured_sources: tuple[Any, ...] = ()
                # Dual-listed issuers may expose an official SEC Company Facts
                # feed.  Only construct the adapter when the user has supplied
                # a valid contact address; the address itself is never logged
                # or persisted by this path.  PDF AST remains the fallback.
                sec_mapping = SEC_HK_ISSUERS.get(company.ticker.upper())
                sec_email = str(preferences.get("sec_contact_email", "")).strip()
                if sec_mapping and "@" in sec_email and " " not in sec_email:
                    try:
                        sec_client = self._sec_client_factory(
                            build_sec_user_agent(
                                _normalize_sec_profile(preferences["sec_contact_profile"]),
                                sec_email,
                            ),
                            self.storage.data_dir / "sec-cache",
                        )
                        structured_sources = (SecFinancialSourceAdapter(sec_client),)
                    except Exception:
                        structured_sources = ()
                if hasattr(self._financial_ingestion, "collect_candidate_batches"):
                    try:
                        dataset = FinancialFactCompiler().compile_from_ingestion(
                            company,
                            research_reports,
                            self._financial_ingestion,
                            structured_sources=structured_sources,
                            vision_fallback=vision_adapter,
                            vision_config=vision_config,
                            cancel_check=job.cancel_event.is_set,
                            progress=lambda stage, current, total: self._ingestion_progress(
                                job, stage, current, total
                            ),
                            reporting_currency=company.reporting_currency,
                        )
                    except VisionAdapterError as exc:
                        if job.cancel_event.is_set() or exc.code == "VISION_CANCELLED":
                            raise ResearchCancelled() from exc
                        raise _ResearchDataUnavailable(exc.code) from exc
                    if job.cancel_event.is_set():
                        raise ResearchCancelled()
                elif structured_sources or vision_adapter is not None:
                    try:
                        try:
                            dataset: FinancialDataset = self._financial_ingestion.ingest(
                                company,
                                research_reports,
                                structured_sources=structured_sources,
                                vision_fallback=vision_adapter,
                                vision_config=vision_config,
                                cancel_check=job.cancel_event.is_set,
                                progress=lambda stage, current, total: self._ingestion_progress(
                                    job, stage, current, total
                                ),
                            )
                        except TypeError as exc:
                            if "vision_fallback" not in str(exc):
                                raise
                            dataset = self._financial_ingestion.ingest(
                                company, research_reports, structured_sources=structured_sources
                            )
                    except VisionAdapterError as exc:
                        if job.cancel_event.is_set() or exc.code == "VISION_CANCELLED":
                            raise ResearchCancelled() from exc
                        raise _ResearchDataUnavailable(exc.code) from exc
                    if job.cancel_event.is_set():
                        raise ResearchCancelled()
                else:
                    # Compatibility-only fallback for injected test/legacy
                    # engines that predate the canonical collection seam;
                    # this branch is not used by the production engine.
                    try:
                        dataset = self._financial_ingestion.ingest(
                            company,
                            research_reports,
                            progress=lambda stage, current, total: self._ingestion_progress(
                                job, stage, current, total
                            ),
                        )
                    except TypeError as exc:
                        if "progress" not in str(exc):
                            raise
                        dataset = self._financial_ingestion.ingest(company, research_reports)
                self._update_job(
                    job,
                    stage="filing-validation",
                    stage_current=len(research_reports),
                    stage_total=len(research_reports),
                    percent=29,
                )
                filing_evidence.extend(item.to_dict() for item in dataset.evidence)
                manifest_by_document = {item.document_id: item for item in dataset.manifest}
                for filing in research_reports:
                    manifest = manifest_by_document.get(filing.document_id)
                    if manifest is None:
                        continue
                    filing.form_type = manifest.form_type
                    filing.fiscal_period = manifest.fiscal_period
                    filing.period_end = manifest.period_end
                    filing.revision = manifest.revision
                    filing.supersedes_document_id = manifest.supersedes_document_id
                # Persist corrected period/form metadata, never the SEC contact.
                self.storage.save_filings(research_reports)
                canonical = (
                    dataset
                    if hasattr(dataset, "research_facts")
                    else FinancialFactCompiler().compile_facts(
                        company,
                        research_reports,
                        dataset.accepted_facts,
                        reporting_currency=company.reporting_currency,
                    )
                )
                canonical_groups = _compiler_validation_groups(canonical.validations)
                latest_annual = max(
                    (
                        manifest for manifest in dataset.manifest
                        if manifest.fiscal_period == "FY"
                        and manifest.form_type == "ANNUAL_REPORT"
                    ),
                    key=lambda item: item.period_end,
                    default=None,
                )
                # Promote a single disclosed reporting currency for the latest
                # consolidated full-core FY group, while retaining HKD as the
                # listing currency.  Mixed/ambiguous currencies fail closed.
                latest_candidates = [
                    item for item in canonical.validations
                    if latest_annual is not None
                    and item.identity[1] == latest_annual.period_end
                    and item in canonical.research_validations
                ]
                currencies = {str(item.identity[4]).upper() for item in latest_candidates if item.identity[4]}
                if len(currencies) > 1:
                    raise _ResearchDataUnavailable("FILING_DATA_QUALITY_FAILED")
                if len(currencies) == 1:
                    disclosed_currency = next(iter(currencies))
                    if disclosed_currency != company.reporting_currency.upper():
                        company.reporting_currency = disclosed_currency
                        self.storage.save_company(company)
                latest_group = next(
                    (
                        item for item in canonical.validations
                        if item in latest_candidates
                    ),
                    None,
                )
                if latest_annual is None or latest_group is None or not canonical.allow_ai:
                    raise _ResearchDataUnavailable("FILING_DATA_QUALITY_FAILED")
                # The ingestion engine may retain accepted facts from multiple
                # statement scopes/currencies for auditability.  Only the
                # consolidated facts in the issuer's reporting currency are a
                # valid research context; parent-company and foreign-currency
                # groups remain in the audit store but never reach an Agent.
                accepted = list({fact.fact_id: fact for fact in canonical.research_facts}.values())
                accepted_ids = {fact.fact_id for fact in accepted}
                # Keep facts which the parser marked accepted but which do not
                # belong to the research scope as audit-only; do not mutate
                # their VERIFIED status into REJECTED.
                audit_only = [
                    fact for fact in dataset.accepted_facts
                    if fact.fact_id not in accepted_ids
                ]
                quarantined = [
                    fact for group in dataset.group_validations
                    for fact in group.validation.quarantined
                ]
                known_quarantined = {fact.fact_id for fact in quarantined}
                quarantined.extend(
                    fact for fact in dataset.validation.quarantined
                    if fact.fact_id not in known_quarantined
                )
                quarantined.extend(
                    fact for fact in canonical.quarantined_facts
                    if fact.fact_id not in {item.fact_id for item in quarantined}
                )
                quarantined.extend(audit_only)
                self.storage.replace_financial_ingestion(
                    company.security_id,
                    [item.accession_number for item in research_reports],
                    accepted,
                    quarantined,
                    canonical_groups,
                    list(dataset.evidence),
                )
                facts = [item.to_dict() for item in accepted]
                financial_profile = build_financial_profile(
                    accepted,
                    canonical_groups,
                    company.reporting_currency,
                    selected_filings=research_reports,
                    manifests=dataset.manifest,
                )
                if not facts:
                    raise _ResearchDataUnavailable("FILING_DATA_QUALITY_FAILED")

            if mode == "company" and not facts:
                raise _ResearchDataUnavailable("FILING_FORMAT_UNSUPPORTED")

            reproducibility = _build_research_snapshot(
                company,
                facts,
                filing_evidence,
                selected_pack,
                report_language,
                parallel_agents,
                history_years,
                valuation_inputs,
                market_snapshot,
            )

            def agent_progress(agent_id: str, state: str) -> None:
                with self._jobs_lock:
                    job.agent_states[agent_id] = state
                    if parallel_agents:
                        job.stage = "base-analysis"
                    else:
                        job.stage = {
                            "financial-analyst": "financial-analysis",
                            "business-analyst": "business-analysis",
                            "accounting-risk-analyst": "risk-analysis",
                        }.get(agent_id, job.stage)

            workflow = ResearchWorkflow(
                self.storage,
                selected_pack,
                self._provider_factory(config),
                config,
                cancel_check=job.cancel_event.is_set,
                report_language=report_language,
                ui_language=ui_language,
                parallel_agents=parallel_agents,
                agent_progress=agent_progress,
            )

            def progress(
                message: str,
                percent: int,
                *,
                base: int,
                span: int,
                prefix: str = "",
            ) -> None:
                self._update_job(
                    job,
                    state="running",
                    message=f"{prefix}{message}",
                    stage=_workflow_stage(percent, comparison=bool(prefix)),
                    percent=base
                    + round(min(100, max(0, int(percent))) * span / 100),
                )

            run_count = 1 + len(comparison_configs)

            def run_segment(index: int) -> tuple[int, int]:
                lower = 30 + round(70 * index / run_count)
                upper = 30 + round(70 * (index + 1) / run_count)
                return lower, max(1, upper - lower)

            primary_base, primary_span = run_segment(0)
            primary = workflow.run(
                company,
                financial_profile or facts,
                filing_evidence=filing_evidence,
                valuation_inputs=valuation_inputs,
                market_snapshot=market_snapshot,
                reproducibility=reproducibility,
                progress=lambda message, percent: progress(
                    message,
                    percent,
                    base=primary_base,
                    span=primary_span,
                    prefix=(
                        _ui_message(ui_language, "Primary: ", "主模型：", "主模型：")
                        if comparison_configs
                        else ""
                    ),
                ),
            )
            for index, comparison_config in enumerate(comparison_configs, start=1):
                if job.cancel_event.is_set():
                    raise ResearchCancelled()
                comparison_workflow = ResearchWorkflow(
                    self.storage,
                    selected_pack,
                    self._provider_factory(comparison_config),
                    comparison_config,
                    cancel_check=job.cancel_event.is_set,
                    report_language=report_language,
                    ui_language=ui_language,
                    parallel_agents=parallel_agents,
                    agent_progress=agent_progress,
                )
                comparison_base, comparison_span = run_segment(index)
                secondary = comparison_workflow.run(
                    company,
                    financial_profile or facts,
                    filing_evidence=filing_evidence,
                    valuation_inputs=valuation_inputs,
                    market_snapshot=market_snapshot,
                    reproducibility=reproducibility,
                    progress=lambda message, percent, base=comparison_base, span=comparison_span, current=index: progress(
                        message,
                        percent,
                        base=base,
                        span=span,
                        prefix=_ui_message(
                            ui_language,
                            f"Comparison {current}/{len(comparison_configs)}: ",
                            f"对比模型 {current}/{len(comparison_configs)}：",
                            f"比較模型 {current}/{len(comparison_configs)}：",
                        ),
                    ),
                )
                compare_research_runs(self.storage, primary, secondary, report_language)
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
        except ResearchCancelled:
            self._update_job(
                job,
                state="cancelled",
                stage="cancelled",
                message=_ui_message(ui_language, "Research cancelled", "研究已取消", "研究已取消"),
            )
        except _ResearchDataUnavailable as exc:
            # A financial quality failure gets one bounded, model-free repair
            # attempt before the run is closed.  The marker prevents recursion
            # and the retry path never constructs a provider.
            if exc.code in {
                "FILING_DATA_QUALITY_FAILED",
                "FILING_FORMAT_UNSUPPORTED",
                "FILING_FETCH_FAILED",
            }:
                try:
                    if self._auto_retry_financial_evidence(
                        job, request, company, ui_language
                    ):
                        self._run_research(job, request)
                        return
                except ResearchCancelled:
                    raise
                except Exception:
                    # The original quality error remains the user-visible
                    # classification; retry diagnostics are persisted by the
                    # retry helper and must not open the model path.
                    pass
            self._update_job(
                job,
                state="failed",
                stage="data-unavailable",
                error_code=exc.code,
                message=_research_data_message(exc.code, ui_language),
            )
        except (MarketDataError, SecClientError) as exc:
            code = getattr(exc, "code", "FILING_FETCH_FAILED")
            if code == "FILING_FETCH_FAILED":
                try:
                    if self._auto_retry_financial_evidence(
                        job, request, company, ui_language
                    ):
                        self._run_research(job, request)
                        return
                except ResearchCancelled:
                    self._update_job(
                        job, state="cancelled", stage="cancelled",
                        message=_ui_message(ui_language, "Research cancelled", "研究已取消", "研究已取消"),
                    )
                    return
                except Exception:
                    pass
            self._update_job(
                job,
                state="failed",
                stage="data-unavailable",
                error_code=code,
                message=_research_data_message(code, ui_language),
            )
        except Exception as exc:
            safe_error = _redact(str(exc), secrets)[:800]
            timed_out = "timeout" in safe_error.lower() or "timed out" in safe_error.lower()
            if timed_out:
                timeout = _normalize_timeout_seconds(
                    (request.get("model") or {}).get("timeout_seconds")
                    if isinstance(request.get("model"), dict)
                    else None
                )
                safe_error = _ui_message(
                    ui_language,
                    f"Model request timed out after {timeout}s; increase the timeout or retry.",
                    f"模型请求超过 {timeout} 秒未响应；可提高超时设置后重试。",
                    f"模型請求超過 {timeout} 秒未回應；可提高逾時設定後重試。",
                )
            self._update_job(
                job,
                state="failed",
                stage="failed",
                error_code=("MODEL_TIMEOUT" if timed_out else "RESEARCH_FAILED"),
                message=safe_error
                or _ui_message(ui_language, "Research failed", "研究失败", "研究失敗"),
            )

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


def _bounded_download_filings(
    adapter: Any,
    filings: list[FilingDocument],
    target: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    max_workers: int = 3,
    per_host: int = 2,
    retry_delay: Callable[[], None] | None = None,
) -> tuple[list[FilingDocument], list[tuple[FilingDocument, Exception]]]:
    """Download unique filings with bounded host/global concurrency.

    Futures may finish in any order, but results and failures are assembled in
    first-input order.  Only transient source exceptions get one immediate
    retry; model providers are never involved here.
    """
    unique: list[FilingDocument] = []
    seen_urls: set[str] = set()
    for filing in filings:
        url = str(filing.source_url).strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique.append(filing)
    if not unique:
        return [], []

    cancel_event = cancel_event or threading.Event()
    host_locks: dict[str, threading.BoundedSemaphore] = {}
    host_lock_guard = threading.Lock()

    def semaphore_for(filing: FilingDocument) -> threading.BoundedSemaphore:
        parsed = urllib.parse.urlparse(filing.source_url)
        host = (parsed.hostname or parsed.netloc or "unknown").casefold()
        with host_lock_guard:
            return host_locks.setdefault(host, threading.BoundedSemaphore(max(1, per_host)))

    def download_one(index: int, filing: FilingDocument) -> tuple[int, FilingDocument | None, Exception | None]:
        if cancel_event.is_set():
            return index, None, None
        gate = semaphore_for(filing)
        with gate:
            if cancel_event.is_set():
                return index, None, None
            attempts = 0
            while True:
                try:
                    return index, adapter.download_filing(filing, target), None
                except (MarketDataError, SecClientError, OSError) as exc:
                    if attempts >= 1 or not _download_error_is_transient(exc):
                        return index, None, exc
                    if cancel_event.is_set():
                        return index, None, None
                    attempts += 1
                    if retry_delay is not None:
                        retry_delay()
                    else:
                        time.sleep(random.uniform(0.05, 0.15) * (2 ** (attempts - 1)))
                except Exception as exc:
                    return index, None, exc

    successes: dict[int, FilingDocument] = {}
    failures: dict[int, tuple[FilingDocument, Exception]] = {}
    completed = 0
    worker_count = max(1, min(max_workers, 3, len(unique)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="filing-download") as executor:
        futures = [executor.submit(download_one, index, filing) for index, filing in enumerate(unique)]
        for future in as_completed(futures):
            index, downloaded, error = future.result()
            completed += 1
            if downloaded is not None:
                successes[index] = downloaded
            elif error is not None:
                failures[index] = (unique[index], error)
            if progress is not None:
                progress(completed, len(unique))

    return (
        [successes[index] for index in range(len(unique)) if index in successes],
        [failures[index] for index in range(len(unique)) if index in failures],
    )


def _download_error_is_transient(error: Exception) -> bool:
    if isinstance(error, MarketDataError):
        return str(getattr(error, "code", "")).upper() in {
            "FILING_FETCH_FAILED",
            "FILING_DOWNLOAD_FAILED",
            "FILING_TIMEOUT",
            "RATE_LIMITED",
        }
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, SecClientError):
        message = str(error).casefold()
        return any(token in message for token in ("timeout", "temporar", "429", "rate limit", " 500", " 502", " 503", " 504"))
    if isinstance(error, OSError):
        return getattr(error, "errno", None) in {54, 60, 10053, 10054, 10060, 110, 111}
    return False


def _research_data_message(code: str, language: str) -> str:
    messages = {
        "zh-CN": {
            "NO_FILINGS_AVAILABLE": "官方披露平台暂未提供该公司的可用财务报告。公司可能尚未发布定期报告，或当前没有符合条件的报告。",
            "FILING_FETCH_FAILED": "未能完成官方财报数据获取。请检查网络后重新获取，或打开官方披露平台核对。",
            "FILING_CONTENT_UNSAFE": "安全软件阻止了官方财报文件写入。请确认目标目录权限后重试；残缺文件不会进入研究模型。",
            "FILING_STATUS_UNVERIFIED": "官方数据源未返回可验证的财报结果。请稍后重新获取，或打开官方披露平台核对。",
            "FILING_DOWNLOAD_FAILED": "已找到官方财报，但下载未完成。请检查网络后重新获取。",
            "FILING_DOWNLOAD_REQUIRED": "需要下载官方财报原文后才能开始研究。请启用财报原文下载并重新获取。",
            "FILING_FORMAT_UNSUPPORTED": "已找到官方公告，但当前版本无法从中生成可用的财务数据。",
            "FILING_DATA_QUALITY_FAILED": "已获取官方披露文件，但关键财务字段未通过一致性校验。为避免错误数据进入 AI，本次研究已停止。",
            "FILING_DATA_QUALITY_PARTIAL": "财务资料已重建，但仍有字段未通过校验；已保留可验证数据。",
            "FILING_REPORT_REFRESH_FAILED": "财务资料已重建，但报告刷新失败；可单独重试报告刷新。",
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
            "NO_FILINGS_AVAILABLE": "The official disclosure platform does not currently provide a usable financial report for this company. The company may not have published a periodic report yet, or no report matches the current criteria.",
            "FILING_FETCH_FAILED": "Official financial-report data could not be retrieved. Check the network and try again, or verify it on the official disclosure platform.",
            "FILING_CONTENT_UNSAFE": "Security software blocked the official financial-report file write. Check the destination permissions and retry; incomplete files are never sent to research models.",
            "FILING_STATUS_UNVERIFIED": "The official source did not return a verifiable financial-report result. Try again later or verify it on the official disclosure platform.",
            "FILING_DOWNLOAD_FAILED": "An official financial report was found, but its download did not complete. Check the network and try again.",
            "FILING_DOWNLOAD_REQUIRED": "The official report must be downloaded before research can start. Enable report downloads and try again.",
            "FILING_FORMAT_UNSUPPORTED": "Official disclosures were found, but this version could not produce usable financial data from them.",
            "FILING_DATA_QUALITY_FAILED": "Official disclosures were retrieved, but critical financial fields failed consistency checks. Research stopped before any data was sent to AI.",
            "FILING_DATA_QUALITY_PARTIAL": "Financial data was rebuilt, but some fields remain unverified; validated data was preserved.",
            "FILING_REPORT_REFRESH_FAILED": "Financial data was rebuilt, but report refresh failed; retry report refresh separately.",
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
            "NO_FILINGS_AVAILABLE": "官方披露平台目前沒有提供可用的財報。公司可能尚未發布定期報告，或沒有報告符合目前條件。",
            "FILING_FETCH_FAILED": "無法取得官方財報資料。請檢查網路後重試，或在官方披露平台核對。",
            "FILING_CONTENT_UNSAFE": "安全軟體阻止了官方財報檔案寫入。請確認目標資料夾權限後重試；不完整檔案不會送入研究模型。",
            "FILING_STATUS_UNVERIFIED": "官方來源沒有返回可驗證的財報結果。請稍後重試，或在官方披露平台核對。",
            "FILING_DOWNLOAD_FAILED": "已找到官方財報，但下載未完成。請檢查網路後重試。",
            "FILING_DOWNLOAD_REQUIRED": "開始研究前必須先下載官方財報原文。請啟用財報下載後重試。",
            "FILING_FORMAT_UNSUPPORTED": "已找到官方披露，但目前版本無法從中產生可用的財務資料。",
            "FILING_DATA_QUALITY_FAILED": "已取得官方披露，但關鍵財務欄位未通過一致性檢查。為避免錯誤資料送入 AI，本次研究已停止。",
            "FILING_DATA_QUALITY_PARTIAL": "財務資料已重建，但仍有欄位未通過校驗；已保留可驗證資料。",
            "FILING_REPORT_REFRESH_FAILED": "財務資料已重建，但報告刷新失敗；可單獨重試報告刷新。",
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
    """Project compiler groups to the legacy storage audit shape.

    The status and covered/accepted/quarantined fields originate in the
    compiler; this is only a serialization compatibility projection.
    """
    projected: list[FinancialGroupValidation] = []
    for item in validations:
        status_name = str(getattr(item, "status", "REJECTED"))
        status = (
            ValidationStatus.VERIFIED
            if status_name == ValidationStatus.VERIFIED.value
            else ValidationStatus.REJECTED
            if status_name in {ValidationStatus.REJECTED.value, "CONFLICTED"}
            else ValidationStatus.READY_WITH_WARNINGS
        )
        issues = tuple(getattr(item, "issues", ()))
        if status_name == "INCOMPLETE" and "compiler_incomplete_profile" not in issues:
            issues = (*issues, "compiler_incomplete_profile")
        validation = FinancialValidation(
            status,
            issues,
            frozenset(getattr(item, "covered", ())),
            tuple(getattr(item, "accepted", ())),
            tuple(getattr(item, "quarantined", ())),
        )
        projected.append(FinancialGroupValidation(tuple(item.identity), validation))
    return projected


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
    if expected_period_end:
        filings = [item for item in filings if item.period_end == expected_period_end]
        if not filings:
            return None
    canonical = FinancialFactCompiler().compile_facts(
        subject,
        filings,
        [fact for fact in facts if any(item.accession_number == fact.accession_number for item in filings)],
        reporting_currency=first.currency,
    )
    if not canonical.allow_ai or not canonical.research_facts:
        return None
    return tuple(canonical.research_facts)


def _report_retryable(artifacts: list[dict[str, Any]]) -> bool:
    final = next(
        (item for item in reversed(artifacts) if item.get("artifact_type") == "research-report"),
        None,
    )
    return bool(final and final.get("content", {}).get("retryable"))


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
    )


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
    if provider == "mineru_flash":
        if value.get("model") is not None:
            raise ValueError("MinerU Flash does not accept a configured model reference")
        return VisionFallbackConfig(
            enabled=True,
            consent=bool(value.get("consent", False)),
            provider="mineru_flash",
            timeout_seconds=60.0,
            language=str(value.get("language", "auto")),
            require_page_approval=True,
        )
    model_config = _model_config_from_request(value.get("model"))
    if not model_config.enabled or model_config.role != "vision":
        raise ValueError("vision fallback requires a configured vision model")
    return VisionFallbackConfig(
        enabled=True,
        consent=bool(value.get("consent", False)),
        provider="configured_model",
        configured_model_id=model_config.configured_model_id,
        configuration_version=model_config.configuration_version,
        timeout_seconds=float(model_config.timeout_seconds),
        language=str(value.get("language", "auto")),
        require_page_approval=True,
    )


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


def _canonical_snapshot_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _financial_status(
    storage: Storage,
    company_payload: dict[str, Any],
    run_payload: dict[str, Any],
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
    facts = storage.get_facts(company_cik) if company_cik else []
    groups = storage.get_validation_groups(company_cik) if company_cik else []
    retry = storage.get_financial_retry_state(company_cik) if company_cik else {
        "attempt_count": 0, "last_stage": "", "last_error": "", "updated_at": ""
    }
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
        reporting_currency=reporting_currency or str(company_payload.get("listing_currency", "USD")),
        accounting_standard=str(company_payload.get("accounting_standard", "")),
        industry=" ".join(
            str(company_payload.get(key, ""))
            for key in ("industry", "company_type")
            if company_payload.get(key)
        ),
        industry_support=str(company_payload.get("industry_support", "standard")),
    )
    required_concepts = CoveragePlanner().plan(status_subject).required_concepts
    verified_group_years = {
        int(str(item.get("period_end", ""))[:4])
        for item in groups
        if str(item.get("status", "")) == ValidationStatus.VERIFIED.value
        and str(item.get("consolidated_scope", "")).lower() == "consolidated"
        and (
            not reporting_currency
            or str(item.get("currency", "")).upper() == reporting_currency
        )
        and concepts_cover_profile(item.get("covered_concepts", []), required_concepts)
        and str(item.get("period_end", ""))[:4].isdigit()
    }
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
    issues: list[dict[str, Any]] = []
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
    retryable = not available_years or bool(missing_years) or bool(unverified_years) or problematic_groups
    if not all_known_years:
        state = "unavailable"
        next_action = "retry_discovery"
    elif problematic_groups:
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
        "quality_summary": quality_summary,
        "attempt_count": int(retry.get("attempt_count", 0)),
        "last_stage": str(retry.get("last_stage", "")),
        "last_error": str(retry.get("last_error", "")),
        "updated_at": str(retry.get("updated_at", "")),
        "next_action": next_action,
        "model_calls": 0,
        "token_delta": 0,
        "snapshot_stale": snapshot_stale,
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
) -> tuple[FinancialFact, ...]:
    """Require a current, canonical financial snapshot before model retries."""

    storage_key = _financial_storage_key(company.to_dict())
    filings = storage.get_filings(storage_key)
    raw_facts = storage.get_facts(storage_key)
    facts: list[FinancialFact] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        try:
            facts.append(FinancialFact(**item))
        except (TypeError, ValueError):
            continue
    canonical = FinancialFactCompiler().compile_facts(
        company,
        filings,
        facts,
        reporting_currency=company.reporting_currency,
    )
    if not canonical.allow_ai or not canonical.research_facts:
        raise ValueError("FINANCIAL_DATA_QUALITY_FAILED")
    snapshot = payload.get("data_snapshot", {})
    expected = snapshot.get("financial_fact_ids_sha256") if isinstance(snapshot, dict) else None
    if not expected:
        raise ValueError("FINANCIAL_SNAPSHOT_STALE")
    actual = _canonical_snapshot_digest(sorted(item.fact_id for item in canonical.research_facts))
    if str(expected) != actual:
        raise ValueError("FINANCIAL_SNAPSHOT_STALE")
    return tuple(canonical.research_facts)


def _build_research_snapshot(
    company: Company,
    facts: list[dict[str, Any]],
    filing_evidence: list[dict[str, Any]],
    pack: ResearchPack,
    report_language: str,
    parallel_agents: bool,
    annual_history_years: int,
    valuation_inputs: dict[str, Any] | None,
    market_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "data_snapshot": {
            "captured_at": utc_now_iso(),
            "company_identity": _canonical_snapshot_digest(company.to_dict()),
            "financial_facts_sha256": _canonical_snapshot_digest(facts),
            "financial_fact_count": len(facts),
            "financial_fact_ids_sha256": _canonical_snapshot_digest(sorted(
                str(item.get("fact_id", ""))
                for item in facts
                if item.get("fact_id")
            )),
            "filing_evidence_sha256": _canonical_snapshot_digest(filing_evidence),
            "filing_evidence_count": len(filing_evidence),
        },
        "research_configuration": {
            "report_language": report_language,
            "parallel_agents": bool(parallel_agents),
            "valuation_inputs": valuation_inputs,
            "market_snapshot": market_snapshot,
            "research_pack_id": pack.pack_id,
            "research_pack_version": pack.version,
            "research_pack_content_identity": pack.content_hash,
            "annual_history_years": int(annual_history_years),
        },
    }

def _request_secrets(_request: dict[str, Any]) -> tuple[str, ...]:
    return ()


def _redact(message: str, secrets: tuple[str, ...]) -> str:
    safe = message
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    return safe
