from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .demo import DEMO_COMPANY, demo_facts
from .domain import FinancialFact
from .i18n import normalize_language
from .packs import builtin_pack
from .providers import ModelConfig
from .research import ResearchCancelled, ResearchWorkflow
from .reporting import render_research_run
from .storage import Storage


CONTRACT_VERSION = "1.0"

PREFERENCE_DEFAULTS: dict[str, str] = {
    "ui_language": "zh-CN",
    "report_language": "zh-CN",
    "sidebar_collapsed": "true",
    "provider": "none",
    "model_preset": "none",
    "model": "",
    "base_url": "",
    "compare_provider": "none",
    "compare_model_preset": "none",
    "compare_model": "",
    "compare_base_url": "",
    "sec_contact_profile": "individual_investor",
    "sec_contact_email": "",
    "sec_user_agent": "",
}


class PreferenceValidationError(ValueError):
    """Raised when a caller attempts to persist an unsupported preference."""


@dataclass(slots=True)
class _ResearchJob:
    job_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    state: str = "queued"
    message: str = ""
    percent: int = 0
    run_id: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "message": self.message,
            "percent": self.percent,
            "run_id": self.run_id,
        }


class AppService:
    """Headless interface consumed by every desktop platform adapter."""

    def __init__(self, data_dir: Path, *, app_version: str = __version__):
        self.storage = Storage(data_dir)
        self.app_version = app_version
        self._jobs: dict[str, _ResearchJob] = {}
        self._jobs_lock = threading.Lock()

    def hello(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "app_version": self.app_version,
            "capabilities": [
                "app.bootstrap",
                "settings.update",
                "research.list",
                "research.get_report",
                "research.start",
                "research.status",
                "research.cancel",
            ],
        }

    def bootstrap(self) -> dict[str, Any]:
        return {
            **self.hello(),
            "preferences": self.preferences(),
            "recent_runs": self.list_research_runs(limit=20),
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
                raise PreferenceValidationError("preference values must be strings or booleans")
            value = str(raw_value).lower() if isinstance(raw_value, bool) else raw_value
            if key in {"ui_language", "report_language"}:
                value = normalize_language(value)
            self.storage.set_setting(key, value)
        return self.preferences()

    def list_research_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = min(200, max(1, int(limit)))
        summaries: list[dict[str, Any]] = []
        for row in self.storage.list_runs(limit=bounded_limit):
            payload = _decode_payload(row.get("payload_json"))
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
                }
            )
        return summaries

    def get_report(self, run_id: str, *, language: str | None = None) -> dict[str, Any]:
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
        return {
            "run_id": run_id,
            "ticker": run["ticker"],
            "company_name": run["name"],
            "status": run["status"],
            "report_language": report_language,
            "markdown": render_research_run(
                run_id,
                artifacts,
                language=report_language,
                company_name=run["name"],
            ),
        }

    def start_research(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("mode") != "demo":
            raise ValueError("unsupported research mode")
        job = _ResearchJob(job_id=uuid.uuid4().hex)
        with self._jobs_lock:
            self._jobs[job.job_id] = job
        threading.Thread(
            target=self._run_demo_research,
            args=(job,),
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
            return job.snapshot()

    def _update_job(self, job: _ResearchJob, **updates: Any) -> None:
        with self._jobs_lock:
            for key, value in updates.items():
                setattr(job, key, value)

    def _run_demo_research(self, job: _ResearchJob) -> None:
        preferences = self.preferences()
        ui_language = normalize_language(preferences["ui_language"])
        report_language = normalize_language(preferences["report_language"])
        self._update_job(
            job,
            state="running",
            message="Starting synthetic demo research"
            if ui_language == "en"
            else "正在启动合成演示研究",
        )
        try:
            facts = demo_facts()
            self.storage.save_company(DEMO_COMPANY)
            self.storage.save_facts([FinancialFact(**item) for item in facts])
            config = ModelConfig(provider="none", model="", base_url="")
            workflow = ResearchWorkflow(
                self.storage,
                builtin_pack(),
                None,
                config,
                cancel_check=job.cancel_event.is_set,
                report_language=report_language,
                ui_language=ui_language,
            )

            def progress(message: str, percent: int) -> None:
                self._update_job(
                    job,
                    state="running",
                    message=message,
                    percent=min(100, max(0, int(percent))),
                )

            run = workflow.run(DEMO_COMPANY, facts, progress=progress)
            self._update_job(
                job,
                state="completed",
                percent=100,
                run_id=run.run_id,
            )
        except ResearchCancelled:
            self._update_job(
                job,
                state="cancelled",
                message="Research cancelled" if ui_language == "en" else "研究已取消",
            )
        except Exception:
            self._update_job(
                job,
                state="failed",
                message="Research failed" if ui_language == "en" else "研究失败",
            )


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
