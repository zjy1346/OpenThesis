"""Locally recompile accepted financial evidence and publish a stale overlay.

The application use case owns the refresh, while the pure pipeline owns all
numeric admission. No model or download collaborator is available here. CAS
publication binds the report and run to the same accepted source snapshot.
"""
from dataclasses import replace
from concurrent.futures import CancelledError
import json

from ..domain import EvidenceRef, FinancialFact, ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from ..financial_contracts import CompilationPolicy
from ..financial_pipeline import FinancialResearchSnapshot
from ..financials import deterministic_summary
from ..financial_evidence import build_fact_evidence
from ..resilience.contracts import digest
from .reports import artifacts_for_report_projection


class FinancialArtifactRefresher:
    def __init__(self, storage, pipeline):
        self.storage, self.pipeline = storage, pipeline

    def refresh(self, run_id, company, *, cancel_check=None):
        if cancel_check is not None and cancel_check():
            raise CancelledError("financial refresh cancelled")
        stored = self.storage.get_run(run_id)
        if stored is None:
            return ()
        payload = json.loads(stored["payload_json"])
        if not isinstance(payload, dict):
            raise RuntimeError("RESEARCH_RUN_SNAPSHOT_INVALID")
        captured_run_digest = digest(payload)
        storage_key = company.cik if company.market == "US" else company.security_id
        financial = self.storage.get_financial_research_snapshot(storage_key, include_evidence=True)
        latest = self.storage.latest_report_revision(run_id)
        artifacts = artifacts_for_report_projection(self.storage.get_artifacts(run_id),
            latest["payload"] if latest else None)
        base = next((item for item in reversed(artifacts) if item.get("artifact_type") == "research-report"), None)
        allowed = set(FinancialFact.__dataclass_fields__)
        facts = tuple(FinancialFact(**{key: value for key, value in row.items() if key in allowed})
            for row in (*financial["facts"], *financial.get("supplemental_facts", ())))
        if not facts:
            return ()
        configuration = payload.get("research_configuration", {})
        history = configuration.get("annual_history_years") if isinstance(configuration, dict) else None
        view = self.pipeline.prepare(FinancialResearchSnapshot.from_facts(
            replace(company), self.storage.get_filings(storage_key), facts,
            evidence=tuple(EvidenceRef(**row) for row in financial["evidence"]),
            requested_annual_count=int(history) if str(history).isdigit() else None,
            parsing_observations=tuple(financial.get("parsing_observations", ())),
        ), CompilationPolicy(reporting_currency=company.reporting_currency))
        profile = view.profile
        if not (profile.metrics or profile.interim_metrics):
            return ()
        metrics, interim = list(profile.metrics), list(profile.interim_metrics)
        quality = {"status": profile.status.value, "rejected_periods": list(profile.rejected_periods),
            "period_continuity": list(profile.period_continuity), "period_coverage": dict(profile.period_coverage)}
        summary = {
            "company": company.to_dict(),
            "research_ready": bool(view.research_ready),
            "capabilities": dict(view.capabilities),
            "markdown": deterministic_summary(company.name, metrics, str(payload.get("report_language", "zh-CN")),
                company.reporting_currency),
            "metrics": metrics, "interim_metrics": interim,
            "evidence": build_fact_evidence([*profile.fact_dicts,
                *(fact.to_dict() for fact in profile.disclosure_facts)],
                source_records=tuple(ref.to_dict() for ref in profile.source_evidence)), "currency": company.reporting_currency,
            "financial_generations": {str(item["accession_number"]): str(item["generation_id"])
                for item in profile.fact_dicts if item.get("accession_number") and item.get("generation_id")},
            "financial_quality": quality,
        }
        now = utc_now_iso()
        from .research_snapshot import financial_semantics_digest
        from ..research_coverage import has_analysis
        from ..report_readiness import REQUIRED_REPORT_SECTIONS
        analysis_exists = any(item.get('artifact_type') in {'agent-analysis', 'growth-opportunities', 'counter-analysis', 'forecast-scenarios'}
            or item.get('artifact_type') == 'research-report' and isinstance(item.get('content'), dict)
            and any(has_analysis(item['content'].get(section)) for section in REQUIRED_REPORT_SECTIONS)
            for item in artifacts)
        old_snapshot = payload.get('data_snapshot', {})
        old_semantics = old_snapshot.get('financial_semantics_sha256', '') if isinstance(old_snapshot, dict) else ''
        prior_stale = bool((base or {}).get('content', {}).get('financial_refresh', {}).get('qualitative_snapshot_stale'))
        qualitative_stale = bool(analysis_exists and (prior_stale or old_semantics != financial_semantics_digest(profile.fact_dicts)))
        overlay = {"mode": "financial-refresh-overlay",
            "base_report_artifact_id": str(base["artifact_id"]) if base else "",
            "financial_refresh": {"updated_at": now, "fact_count": len(profile.fact_dicts),
                "status": profile.status.value, "model_called": False,
                "qualitative_snapshot_stale": qualitative_stale, "financial_quality": quality}}
        outputs = [
            ResearchArtifact(f"{run_id}:deterministic-financial-summary:retry-{digest(summary)[:16]}", run_id,
                "deterministic-financial-summary", "Deterministic Financial Overview", summary,
                agent_id="calculation-engine-retry"),
            ResearchArtifact(f"{run_id}:research-report:overlay-{digest(overlay)[:16]}", run_id,
                "research-report", "Financial Refresh Report", overlay, agent_id="financial-refresh"),
        ]
        from ..storage import DERIVED_PIPELINE_CONTRACT
        payload["data_snapshot"] = {**dict(payload.get("data_snapshot", {})), "captured_at": now,
            "financial_facts_sha256": digest(list(profile.fact_dicts)),
            "financial_semantics_sha256": financial_semantics_digest(profile.fact_dicts),
            "financial_fact_count": len(profile.fact_dicts),
            "derived_pipeline_contract": DERIVED_PIPELINE_CONTRACT,
            "financial_generations": dict(summary["financial_generations"]),
            "financial_fact_ids_sha256": digest(sorted(item.get("fact_id", "") for item in profile.fact_dicts))}
        run = ResearchRun(run_id, replace(company), str(payload.get("workflow_id", "")),
            str(payload.get("research_pack_id", "")), str(payload.get("research_pack_version", "")),
            str(payload.get("provider_id", "")), str(payload.get("model_id", "")),
            str(payload.get("data_as_of", now)), status=RunStatus(stored["status"]),
            started_at=str(payload.get("started_at", stored["started_at"])), completed_at=stored.get("completed_at"),
            errors=list(payload.get("errors", [])), report_language=str(payload.get("report_language", "zh-CN")),
            market_snapshot=payload.get("market_snapshot"), model_configuration=dict(payload.get("model_configuration", {})),
            research_configuration=dict(payload.get("research_configuration", {})), data_snapshot=payload["data_snapshot"])
        self.storage.save_run_with_artifacts(run, outputs,
            expected_run_digest=captured_run_digest,
            expected_financial_snapshot=(storage_key, financial),
            expected_report_generation=int(latest["generation"]) if latest else 0,
            cancel_check=cancel_check,
            preserve_existing_payload=True)
        return ("deterministic-financial-summary", "research-report")
