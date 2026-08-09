from __future__ import annotations

import base64
import binascii
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .comparison import compare_research_runs
from .demo import DEMO_COMPANY, demo_facts
from .domain import Company, FilingDocument, FinancialFact, ResearchRun, RunStatus, utc_now_iso
from .filing_parser import build_filing_evidence
from .i18n import normalize_language, translate_error
from .market_data import MarketDataError, MarketDataModule
from .market_financials import (
    FinancialExtractionError,
    extract_pdf_financials,
    financial_quality_issues,
)
from .markets import COMMON_MARKET_COMPANIES, MARKET_PROFILES, Market, normalize_market
from .model_catalog import (
    MODEL_PRESETS,
    ModelDiscoveryError,
    ModelPreset,
    discover_models,
    get_model_preset,
    infer_model_preset,
    merge_model_ids,
)
from .onboarding import COMMON_COMPANIES, build_sec_user_agent
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
from .sec_client import SecClient, SecClientError
from .storage import Storage


CONTRACT_VERSION = "1.0"

PREFERENCE_DEFAULTS: dict[str, str] = {
    "ui_language": "zh-CN",
    "report_language": "zh-CN",
    "sidebar_collapsed": "true",
    "parallel_agents": "false",
    "research_market": "US",
    "provider": "none",
    "model_preset": "none",
    "model": "",
    "base_url": "",
    "compare_provider": "none",
    "compare_model_preset": "none",
    "compare_model": "",
    "compare_base_url": "",
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


@dataclass(slots=True)
class _ResearchJob:
    job_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    state: str = "queued"
    message: str = ""
    percent: int = 0
    run_id: str | None = None
    stage: str = "preparing"
    agent_states: dict[str, str] = field(default_factory=dict)
    cancel_requested: bool = False
    ui_language: str = "zh-CN"
    error_code: str | None = None
    market: str | None = None
    disclosure_url: str | None = None
    started_at: float = field(default_factory=time.monotonic, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "message": self.message,
            "percent": self.percent,
            "run_id": self.run_id,
            "stage": self.stage,
            "agent_states": dict(self.agent_states),
            "completed_agents": sum(state == "completed" for state in self.agent_states.values()),
            "total_agents": len(self.agent_states),
            "cancel_requested": self.cancel_requested,
            "elapsed_seconds": int(max(0, time.monotonic() - self.started_at)),
            "error_code": self.error_code,
            "market": self.market,
            "disclosure_url": self.disclosure_url,
        }


class AppService:
    """Headless interface consumed by every desktop platform adapter."""

    def __init__(
        self,
        data_dir: Path,
        *,
        app_version: str = __version__,
        sec_client_factory: Callable[[str, Path], Any] = SecClient,
        model_discoverer: Callable[..., tuple[str, ...]] = discover_models,
        provider_factory: Callable[[ModelConfig], Any] = create_provider,
        market_data: Any | None = None,
    ):
        self.storage = Storage(data_dir)
        self.interrupted_run_count = self.storage.interrupt_running_runs()
        self.app_version = app_version
        self._sec_client_factory = sec_client_factory
        self._model_discoverer = model_discoverer
        self._provider_factory = provider_factory
        self._market_data = market_data or MarketDataModule()
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
                "models.catalog",
                "models.discover",
                "models.test",
                "packs.install",
                "thesis.list",
                "thesis.get",
                "thesis.save",
                "research.list",
                "research.delete",
                "research.get_report",
                "research.start",
                "research.retry_synthesis",
                "research.status",
                "research.cancel",
            ],
        }

    def bootstrap(self) -> dict[str, Any]:
        return {
            **self.hello(),
            "preferences": self.preferences(),
            "recent_runs": self.list_research_runs(limit=20),
            "common_companies": self.common_companies(),
            "market_catalog": self.market_catalog(),
            "model_catalog": self.model_catalog(),
            "research_packs": self.research_packs(),
            "interrupted_runs": self.interrupted_run_count,
        }

    def preferences(self) -> dict[str, str]:
        return {
            key: self.storage.get_setting(key, default)
            for key, default in PREFERENCE_DEFAULTS.items()
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
                "label_en": profile.label_en,
                "exchanges": [exchange.value for exchange in profile.exchanges],
                "default_currency": profile.default_currency,
                "requires_sec_identity": profile.requires_sec_identity,
                "disclosure_home": profile.disclosure_home,
            }
            for profile in MARKET_PROFILES.values()
        ]

    def model_catalog(self) -> list[dict[str, Any]]:
        return [_serialize_model_preset(preset) for preset in MODEL_PRESETS]

    def research_packs(self) -> list[dict[str, str]]:
        return [
            _serialize_pack(pack)
            for pack in list_installed_packs(self.storage.packs_dir)
        ]

    def install_research_pack(
        self, filename: str, encoded_archive: str
    ) -> dict[str, str]:
        if Path(filename).suffix.lower() != ".othesis":
            raise ValueError("research pack must use the .othesis extension")
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
        archive = incoming / f"{uuid.uuid4().hex}.othesis"
        try:
            archive.write_bytes(payload)
            return _serialize_pack(install_pack(archive, self.storage.packs_dir))
        finally:
            archive.unlink(missing_ok=True)

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

    def discover_models_for_session(self, request: dict[str, Any]) -> dict[str, Any]:
        preset = _preset_for_selection(request)
        base_url = str(request.get("base_url", "")).strip() or preset.base_url
        api_key = str(request.get("api_key", "")).strip()
        models = preset.recommended_models
        warning = ""
        try:
            discovered = self._model_discoverer(preset, base_url, api_key)
            models = merge_model_ids(preset.recommended_models, discovered)
        except ModelDiscoveryError as exc:
            warning = translate_error(
                _redact(str(exc), (api_key,)),
                self.preferences().get("ui_language", "zh-CN"),
            )
        finally:
            api_key = ""
        return {
            "preset_id": preset.preset_id,
            "models": list(models),
            "warning": warning,
            "endpoint": base_url,
            "source": "builtin" if warning else "online",
        }

    def test_model_connection(self, request: dict[str, Any]) -> dict[str, Any]:
        api_key = str(request.get("api_key", "")).strip()
        try:
            config = _model_config_from_request(request)
            provider = self._provider_factory(config)
            if provider is None:
                return {"ok": True, "message": "Deterministic mode does not call AI."}
            return {"ok": True, "message": str(provider.test_connection())}
        except Exception as exc:
            return {"ok": False, "message": _redact(str(exc), (api_key,))[:800]}
        finally:
            api_key = ""

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
        return {
            "run_id": run_id,
            "ticker": run["ticker"],
            "company_name": run["name"],
            "status": run["status"],
            "report_language": report_language,
            "market": str(company.get("market", "US")),
            "exchange": str(company.get("exchange", "")),
            "listing_currency": str(company.get("listing_currency", "USD")),
            "industry_support": str(company.get("industry_support", "standard")),
            "market_snapshot": payload.get("market_snapshot"),
            "retryable_synthesis": _report_retryable(artifacts),
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
        try:
            workflow.retry_synthesis(
                run,
                self.storage.get_artifacts(run_id),
                self.storage.get_facts(company.cik),
            )
        finally:
            model["api_key"] = ""
        return self.get_report(run_id, language=run.report_language)

    def start_research(self, request: dict[str, Any]) -> dict[str, Any]:
        mode = request.get("mode")
        if mode not in {"demo", "company"}:
            raise ValueError("unsupported research mode")
        if mode == "company":
            _company_from_request(request.get("company"))
        primary_config = _model_config_from_request(
            request.get("model", {"preset_id": "none"})
        )
        if request.get("compare_enabled"):
            comparison_config = _model_config_from_request(
                request.get("comparison_model")
            )
            if not primary_config.enabled or not comparison_config.enabled:
                raise ValueError(
                    "when comparison is enabled, both models require an enabled provider"
                )

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
            if job.state in {"queued", "running"}:
                job.state = "cancelling"
                job.cancel_requested = True
                job.stage = "cancelling"
                job.message = (
                    "Stopping unfinished agents…"
                    if job.ui_language == "en"
                    else "正在停止未完成的 Agent…"
                )
                for agent_id, state in list(job.agent_states.items()):
                    if state in {"queued", "running"}:
                        job.agent_states[agent_id] = "cancelled"
            return job.snapshot()

    def _update_job(self, job: _ResearchJob, **updates: Any) -> None:
        with self._jobs_lock:
            for key, value in updates.items():
                setattr(job, key, value)

    def _run_research(self, job: _ResearchJob, request: dict[str, Any]) -> None:
        preferences = self.preferences()
        ui_language = normalize_language(preferences["ui_language"])
        report_language = normalize_language(preferences["report_language"])
        secrets = _request_secrets(request)
        if job.cancel_event.is_set():
            self._update_job(
                job,
                state="cancelled",
                stage="cancelled",
                message=(
                    "Research cancelled"
                    if ui_language == "en"
                    else "研究已取消"
                ),
            )
            return
        self._update_job(
            job,
            state="running",
            message="Preparing research data" if ui_language == "en" else "正在准备研究数据",
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
            self._update_job(
                job,
                market=company_market.value,
                disclosure_url=market_profile.disclosure_home,
            )
            config = _model_config_from_request(
                request.get("model", {"preset_id": "none"})
            )
            compare_enabled = bool(request.get("compare_enabled"))
            comparison_config = (
                _model_config_from_request(request.get("comparison_model"))
                if compare_enabled
                else None
            )
            parallel_agents = _request_bool(
                request.get("parallel_agents"),
                preferences.get("parallel_agents", "false") == "true",
            )
            selected_pack = self._select_pack(str(request.get("pack_id", "")))
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

            self.storage.save_company(company)
            if mode == "demo":
                facts = demo_facts()
                self.storage.save_facts([FinancialFact(**item) for item in facts])
                self._update_job(
                    job,
                    message=(
                        "Synthetic data ready"
                        if ui_language == "en"
                        else "演示数据准备完成"
                    ),
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
                    message=(
                        "Loading SEC annual filings"
                        if ui_language == "en"
                        else "正在获取 SEC 年报清单"
                    ),
                    percent=5,
                )
                filings = client.list_annual_filings(company, limit=5)
                if not filings:
                    raise _ResearchDataUnavailable("NO_FILINGS_AVAILABLE")
                if bool(request.get("download_filings", True)):
                    downloaded: list[FilingDocument] = []
                    target = self.storage.filings_dir / company.cik
                    for index, filing in enumerate(filings, start=1):
                        if job.cancel_event.is_set():
                            raise ResearchCancelled()
                        self._update_job(
                            job,
                            message=(
                                f"Downloading SEC 10-K ({index}/{len(filings)})"
                                if ui_language == "en"
                                else f"正在下载 SEC 10-K（{index}/{len(filings)}）"
                            ),
                            percent=6 + round(index * 10 / max(1, len(filings))),
                        )
                        downloaded.append(client.download_filing(filing, target))
                    filings = downloaded
                    filing_evidence = build_filing_evidence(filings)
                self.storage.save_filings(filings)
                if job.cancel_event.is_set():
                    raise ResearchCancelled()
                self._update_job(
                    job,
                    message=(
                        "Loading SEC Company Facts"
                        if ui_language == "en"
                        else "正在获取 SEC Company Facts"
                    ),
                    percent=23,
                )
                normalized = client.get_company_facts(company)
                self.storage.save_facts(normalized)
                facts = [item.to_dict() for item in normalized]
            else:
                adapter = self._market_data.adapter_for(company)
                market_label = "A/港股" if ui_language != "en" else "A/H-share"
                self._update_job(
                    job,
                    message=(
                        f"Loading official {market_label} financial reports"
                        if ui_language == "en"
                        else f"正在获取{market_label}官方财报清单"
                    ),
                    percent=5,
                )
                filings = adapter.list_financial_filings(company, limit=5)
                if not filings:
                    raise _ResearchDataUnavailable("NO_FILINGS_AVAILABLE")
                downloaded = []
                if bool(request.get("download_filings", True)):
                    target = self.storage.filings_dir / company.security_id.replace(":", "_")
                    download_failures = 0
                    for index, filing in enumerate(filings, start=1):
                        if job.cancel_event.is_set():
                            raise ResearchCancelled()
                        self._update_job(
                            job,
                            message=(
                                f"Downloading official report ({index}/{len(filings)})"
                                if ui_language == "en"
                                else f"正在下载官方财报（{index}/{len(filings)}）"
                            ),
                            percent=6 + round(index * 10 / max(1, len(filings))),
                        )
                        try:
                            downloaded.append(adapter.download_filing(filing, target))
                        except (MarketDataError, OSError):
                            download_failures += 1
                    if not downloaded and download_failures:
                        raise _ResearchDataUnavailable("FILING_DOWNLOAD_FAILED")
                    filings = downloaded
                else:
                    raise _ResearchDataUnavailable("FILING_DOWNLOAD_REQUIRED")
                self.storage.save_filings(filings)
                normalized_facts: list[FinancialFact] = []
                research_reports = list(downloaded)
                for index, filing in enumerate(research_reports, start=1):
                    if job.cancel_event.is_set():
                        raise ResearchCancelled()
                    self._update_job(
                        job,
                        message=(
                            f"Normalizing official disclosure ({index}/{len(research_reports)})"
                            if ui_language == "en"
                            else f"正在标准化官方披露文件（{index}/{len(research_reports)}）"
                        ),
                        percent=18 + index * 4,
                    )
                    try:
                        extracted, report_evidence, _warnings = extract_pdf_financials(
                            filing,
                            company,
                        )
                    except FinancialExtractionError:
                        continue
                    normalized_facts.extend(extracted)
                    filing_evidence.extend(report_evidence)
                deduplicated = {item.fact_id: item for item in normalized_facts}
                normalized_facts = list(deduplicated.values())
                if not normalized_facts:
                    raise _ResearchDataUnavailable("FILING_FORMAT_UNSUPPORTED")
                quality_issues = financial_quality_issues(normalized_facts)
                if quality_issues:
                    raise _ResearchDataUnavailable("FILING_DATA_QUALITY_FAILED")
                self.storage.replace_facts_for_filings(
                    company.security_id,
                    [item.accession_number for item in research_reports],
                    normalized_facts,
                )
                facts = [item.to_dict() for item in normalized_facts]

            if mode == "company" and not facts:
                raise _ResearchDataUnavailable("FILING_FORMAT_UNSUPPORTED")

            def agent_progress(agent_id: str, state: str) -> None:
                with self._jobs_lock:
                    job.agent_states[agent_id] = state
                    job.stage = "parallel-agents"

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
                    stage=("parallel-agents" if 20 <= percent <= 50 else "research"),
                    percent=base
                    + round(min(100, max(0, int(percent))) * span / 100),
                )

            primary = workflow.run(
                company,
                facts,
                filing_evidence=filing_evidence,
                valuation_inputs=valuation_inputs,
                market_snapshot=market_snapshot,
                progress=lambda message, percent: progress(
                    message,
                    percent,
                    base=30,
                    span=35 if compare_enabled else 70,
                    prefix=(
                        ("Primary: " if ui_language == "en" else "主模型：")
                        if compare_enabled
                        else ""
                    ),
                ),
            )
            if compare_enabled and comparison_config is not None:
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
                secondary = comparison_workflow.run(
                    company,
                    facts,
                    filing_evidence=filing_evidence,
                    valuation_inputs=valuation_inputs,
                    market_snapshot=market_snapshot,
                    progress=lambda message, percent: progress(
                        message,
                        percent,
                        base=65,
                        span=35,
                        prefix=(
                            "Comparison: "
                            if ui_language == "en"
                            else "对比模型："
                        ),
                    ),
                )
                compare_research_runs(
                    self.storage, primary, secondary, report_language
                )
            self._update_job(
                job,
                state="completed",
                stage=("partial" if primary.status.value == "partial" else "completed"),
                percent=100,
                message=(
                    "Research stages completed; synthesized report needs retry"
                    if ui_language == "en" and primary.status.value == "partial"
                    else "研究阶段已完成；综合报告需要重试"
                    if primary.status.value == "partial"
                    else "Research completed"
                    if ui_language == "en"
                    else "研究完成"
                ),
                run_id=primary.run_id,
            )
        except ResearchCancelled:
            self._update_job(
                job,
                state="cancelled",
                stage="cancelled",
                message="Research cancelled" if ui_language == "en" else "研究已取消",
            )
        except _ResearchDataUnavailable as exc:
            self._update_job(
                job,
                state="failed",
                stage="data-unavailable",
                error_code=exc.code,
                message=_research_data_message(exc.code, ui_language),
            )
        except (MarketDataError, SecClientError) as exc:
            code = getattr(exc, "code", "FILING_FETCH_FAILED")
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
                safe_error = (
                    f"Model request timed out after {timeout}s; increase the timeout or retry."
                    if ui_language == "en"
                    else f"模型请求超过 {timeout} 秒未响应；可提高超时设置后重试。"
                )
            self._update_job(
                job,
                state="failed",
                stage="failed",
                error_code=("MODEL_TIMEOUT" if timed_out else "RESEARCH_FAILED"),
                message=safe_error
                or ("Research failed" if ui_language == "en" else "研究失败"),
            )
        finally:
            for selection_name in ("model", "comparison_model"):
                selection = request.get(selection_name)
                if isinstance(selection, dict):
                    selection["api_key"] = ""
    def _select_pack(self, pack_id: str) -> ResearchPack:
        packs = list_installed_packs(self.storage.data_dir / "research-packs")
        if not pack_id:
            return packs[0] if packs else builtin_pack()
        for pack in packs:
            if pack.pack_id == pack_id:
                return pack
        raise ValueError("research pack not found")


def _research_data_message(code: str, language: str) -> str:
    messages = {
        "zh-CN": {
            "NO_FILINGS_AVAILABLE": "官方披露平台暂未提供该公司的可用财务报告。公司可能尚未发布定期报告，或当前没有符合条件的报告。",
            "FILING_FETCH_FAILED": "未能完成官方财报数据获取。请检查网络后重新获取，或打开官方披露平台核对。",
            "FILING_STATUS_UNVERIFIED": "官方数据源未返回可验证的财报结果。请稍后重新获取，或打开官方披露平台核对。",
            "FILING_DOWNLOAD_FAILED": "已找到官方财报，但下载未完成。请检查网络后重新获取。",
            "FILING_DOWNLOAD_REQUIRED": "需要下载官方财报原文后才能开始研究。请启用财报原文下载并重新获取。",
            "FILING_FORMAT_UNSUPPORTED": "已找到官方公告，但当前版本无法从中生成可用的财务数据。",
            "FILING_DATA_QUALITY_FAILED": "已获取官方披露文件，但关键财务字段未通过一致性校验。为避免错误数据进入 AI，本次研究已停止。",
        },
        "en": {
            "NO_FILINGS_AVAILABLE": "The official disclosure platform does not currently provide a usable financial report for this company. The company may not have published a periodic report yet, or no report matches the current criteria.",
            "FILING_FETCH_FAILED": "Official financial-report data could not be retrieved. Check the network and try again, or verify it on the official disclosure platform.",
            "FILING_STATUS_UNVERIFIED": "The official source did not return a verifiable financial-report result. Try again later or verify it on the official disclosure platform.",
            "FILING_DOWNLOAD_FAILED": "An official financial report was found, but its download did not complete. Check the network and try again.",
            "FILING_DOWNLOAD_REQUIRED": "The official report must be downloaded before research can start. Enable report downloads and try again.",
            "FILING_FORMAT_UNSUPPORTED": "Official disclosures were found, but this version could not produce usable financial data from them.",
            "FILING_DATA_QUALITY_FAILED": "Official disclosures were retrieved, but critical financial fields failed consistency checks. Research stopped before any data was sent to AI.",
        },
    }
    catalog = messages["en" if language == "en" else "zh-CN"]
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


def _report_retryable(artifacts: list[dict[str, Any]]) -> bool:
    final = next(
        (item for item in reversed(artifacts) if item.get("artifact_type") == "research-report"),
        None,
    )
    return bool(final and final.get("content", {}).get("retryable"))


def _serialize_model_preset(preset: ModelPreset) -> dict[str, Any]:
    return {
        "preset_id": preset.preset_id,
        "label": preset.label,
        "region": preset.region,
        "protocol": preset.protocol,
        "base_url": preset.base_url,
        "recommended_models": list(preset.recommended_models),
        "models_path": preset.models_path,
        "help_url": preset.help_url,
        "requires_api_key": preset.requires_api_key,
        "temperature": preset.temperature,
    }


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


def _model_config_from_request(value: Any) -> ModelConfig:
    if value is None or not isinstance(value, dict):
        raise ValueError("model configuration is required")
    preset = _preset_for_selection(value)
    model = str(value.get("model", "")).strip()
    base_url = str(value.get("base_url", "")).strip() or preset.base_url
    api_key = str(value.get("api_key", "")).strip()
    if preset.preset_id == "none":
        return ModelConfig(provider="none", model="", base_url="", api_key="")
    if not model or not base_url:
        raise ValueError("model name and endpoint are required")
    if preset.requires_api_key and not api_key.strip():
        raise ValueError("API Key is required for this provider")
    return ModelConfig(
        provider=preset.protocol,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=preset.temperature,
        timeout_seconds=_normalize_timeout_seconds(value.get("timeout_seconds")),
    )


def _preset_for_selection(value: dict[str, Any]) -> ModelPreset:
    """Resolve legacy Kimi settings by the saved endpoint region.

    Older settings stored only ``preset_id=kimi`` while using the international
    endpoint.  Preserve that explicit endpoint instead of silently sending a
    mainland key to the wrong Kimi region.
    """
    requested = get_model_preset(str(value.get("preset_id", "custom")))
    base_url = str(value.get("base_url", "")).strip()
    if requested.preset_id not in {"kimi", "kimi-global"} or not base_url:
        return requested
    inferred = infer_model_preset(requested.protocol, base_url)
    return inferred if inferred.preset_id in {"kimi", "kimi-global"} else requested


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


def _request_secrets(request: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for name in ("model", "comparison_model"):
        selection = request.get(name)
        if isinstance(selection, dict):
            secret = str(selection.get("api_key", "")).strip()
            if secret:
                values.append(secret)
    return tuple(values)


def _redact(message: str, secrets: tuple[str, ...]) -> str:
    safe = message
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    return safe
