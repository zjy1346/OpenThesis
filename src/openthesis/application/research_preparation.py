"""Capture one material set for new research, without RPC/job dependencies.

Source discovery, compilation and publication remain with OfficialFinancialPreparer.
This use case owns the admitted material, optional market snapshot and reproducible
input assembled for ResearchOrchestrator; it never calls a model or repairs facts.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from ..demo import demo_facts
from ..disclosure_coverage import DisclosureCoverage, DisclosureCoveragePlanner
from ..domain import Company, FinancialFact, ResearchRun
from ..financial_sources.disclosures import cached_filing_evidence, financial_evidence_record
from ..financial_disclosure_bindings import bind_compiled_disclosures
from ..market_capabilities import DEFAULT_MARKET_CAPABILITIES
from ..market_snapshot import ReverseDcfPolicy
from ..providers import ModelConfig
from ..research import ResearchCancelled
from ..research_materials import has_sourced_disclosure
from .financial_outcomes import unavailable_outcome
from .financials import OfficialFinancialRequest, OfficialFinancialUnavailable
from .research_requests import ResearchStartRequest, captured_models
from .research_snapshot import build_research_snapshot, scanner_diagnostic_code


class ResearchPreparationUnavailable(RuntimeError):
    def __init__(self, code: str, *, outcome=None, coverage: DisclosureCoverage | None = None):
        self.code = code
        self.outcome = outcome or unavailable_outcome(code)
        self.coverage = coverage
        super().__init__(code)


def financial_scope_gaps(facts, filings) -> tuple[dict[str, Any], ...]:
    """Major table absence in the latest required annual/interim periods.

    This is a paid-research scope question, not an all-files quality gate.
    Missing one cell/table remains ordinary labelled degradation. Historical
    siblings cannot silently stand in for a latest period with two or three
    entire core statements absent.
    """
    annual = [item for item in filings if item.period_end and item.fiscal_period in {"FY", "CY", "ANNUAL"}]
    interim = [item for item in filings if item.period_end and item.fiscal_period not in {"FY", "CY", "ANNUAL"}]
    targets = [max(items, key=lambda item: item.period_end) for items in (annual, interim) if items]
    table_concepts = {"income_statement": {"revenue", "net_income", "parent_net_income", "operating_income"},
                      "balance_sheet": {"assets", "equity", "total_equity", "liabilities"},
                      "cash_flow": {"operating_cash_flow", "capital_expenditure"}}
    gaps = []
    for target in targets:
        concepts = {fact.get("concept") for fact in facts if fact.get("end_date") == target.period_end
                    and fact.get("fiscal_period") == target.fiscal_period
                    and (fact.get("consolidated_scope") or fact.get("scope", "consolidated")) == "consolidated"}
        missing = [table for table, members in table_concepts.items() if not concepts.intersection(members)]
        if len(missing) >= 2:
            gaps.append({"period_end": target.period_end, "fiscal_period": target.fiscal_period,
                         "missing_statements": missing})
    return tuple(gaps)


@dataclass(frozen=True, slots=True)
class ResearchPreparationRequest:
    company: Company
    selected_pack: Any
    models: tuple[ModelConfig, ...]
    official: OfficialFinancialRequest | None = None
    mode: str = "company"
    valuation_inputs: dict[str, Any] | None = None
    manual_market_snapshot: dict[str, Any] | None = None
    auto_market_snapshot: bool = False
    valuation_policy: ReverseDcfPolicy = field(default_factory=ReverseDcfPolicy)
    policy_source: str = "policy_default"
    vision_policy: dict[str, Any] = field(default_factory=dict)
    report_language: str = "zh-CN"
    ui_language: str = "zh-CN"
    parallel_agents: bool = False
    annual_history_years: int = 5
    existing_run: ResearchRun | None = None
    recovery_session_id: str = ""
    job_id: str = ""
    allow_partial_research: bool = False

    def __post_init__(self):
        if type(self.allow_partial_research) is not bool:
            raise ValueError("RESEARCH_PARTIAL_SCOPE_INVALID")
        if self.mode not in {"company", "demo"}:
            raise ValueError("RESEARCH_MODE_INVALID")
        if (isinstance(self.annual_history_years, bool)
                or not isinstance(self.annual_history_years, int)
                or not 1 <= self.annual_history_years <= 10):
            raise ValueError("INVALID_ANNUAL_HISTORY_WINDOW")
        if self.mode == "company" and not isinstance(self.official, OfficialFinancialRequest):
            raise ValueError("CAPTURED_OFFICIAL_FINANCIAL_REQUEST_REQUIRED")
        if self.official is not None and (
                self.official.company.to_dict() != self.company.to_dict()
                or self.official.annual_history_years != self.annual_history_years):
            raise ValueError("RESEARCH_SOURCE_CAPTURE_MISMATCH")
        object.__setattr__(self, "company", replace(self.company))
        if self.official is not None:
            object.__setattr__(self, "official", replace(self.official))
        object.__setattr__(self, "models", captured_models(self.models))
        for key in ("valuation_inputs", "manual_market_snapshot", "vision_policy"):
            object.__setattr__(self, key, deepcopy(getattr(self, key)))


@dataclass(frozen=True, slots=True)
class PreparedResearchStart:
    request: ResearchStartRequest
    coverage: DisclosureCoverage | None = None


class NewResearchPreparer:
    def __init__(self, storage: Any, official_financials: Any, disclosure_service: Any):
        self.storage = storage
        self.official_financials = official_financials
        self.disclosure_service = disclosure_service

    def prepare(self, request: ResearchPreparationRequest, *,
                cancel_check: Callable[[], bool] | None = None,
                progress: Callable[..., None] | None = None,
                sources_ready: Callable[[Company, tuple], None] | None = None) -> PreparedResearchStart:
        if not isinstance(request, ResearchPreparationRequest):
            raise TypeError("CAPTURED_RESEARCH_PREPARATION_REQUEST_REQUIRED")
        # UI progress callbacks can retain the public capture. Work on a private
        # detached copy so a callback cannot change this run's facts or policy.
        captured = replace(request)
        cancelled = cancel_check or (lambda: False)
        notify = progress or (lambda *_args: None)
        stage = "preparing"

        def check_cancelled():
            if cancelled():
                raise ResearchCancelled()

        def report(stage_id, current, total, detail=None):
            nonlocal stage
            stage = stage_id
            check_cancelled()
            notify(stage_id, current, total, detail)
            if stage_id == "filing-acquisition-complete" and sources_ready is not None and isinstance(detail, (list, tuple)):
                # Scheduling belongs to the orchestrator; preparation still
                # never invokes a model. These immutable source identities are
                # captured before the financial pipeline begins.
                sources_ready(replace(captured.company), tuple(replace(item) for item in detail))

        check_cancelled()
        subject = captured.company
        market_snapshot = captured.manual_market_snapshot
        valuation_inputs = captured.valuation_inputs
        if captured.auto_market_snapshot and market_snapshot is None:
            market_snapshot = self.disclosure_service.capture(subject, captured.valuation_policy).to_dict()
        elif market_snapshot is not None:
            market_snapshot = self.disclosure_service.capture(
                subject, captured.valuation_policy, market_snapshot).to_dict()
        if (market_snapshot
                and str(market_snapshot.get("status", "VERIFIED")).upper() in {"VERIFIED", "MANUAL"}
                and (market_snapshot.get("equity_market_value") or market_snapshot.get("market_cap", 0)) > 0):
            valuation_inputs = {
                "market_cap": market_snapshot.get("equity_market_value") or market_snapshot["market_cap"],
                "discount_rate": (valuation_inputs or {}).get("discount_rate", 0.10),
                "terminal_growth": (valuation_inputs or {}).get("terminal_growth", 0.03),
                "horizon_years": (valuation_inputs or {}).get("horizon_years", 5),
            }
        check_cancelled()
        self.storage.save_company(subject)
        facts, evidence, capabilities, generations = [], [], {}, {}
        profile, coverage = None, None
        source_state = None
        targets = ()
        try:
            if captured.mode == "demo":
                facts = demo_facts()
                self.storage.save_facts([FinancialFact(**item) for item in facts])
                report("synthetic-data-ready", 1, 1)
            else:
                capability = DEFAULT_MARKET_CAPABILITIES.require(subject.market)
                prepared = self.official_financials.prepare(captured.official,
                    cancel_check=cancelled, progress=report)
                subject = prepared.company
                filings = prepared.filings
                admitted = prepared.view
                coverage = DisclosureCoveragePlanner().evaluate(filings,
                    requested_count=captured.annual_history_years, authority=capability.official_authority,
                    failures=(*prepared.discovery.diagnostics, *prepared.errors))
                # Recovery targets are internal use-case state, not mutations
                # of a raw RPC request. No metadata can promote a candidate.
                publication = prepared.publication or {}
                targets = tuple(accession for accession, state in publication.get("accessions", {}).items()
                                if state != "promoted")
                if prepared.currency_lane_ambiguous and not targets:
                    targets = tuple(item.accession_number for item in filings)
                facts = list(admitted.fact_dicts)
                evidence = [financial_evidence_record(item) for item in admitted.evidence]
                disclosure_filings = filings
                if not admitted.research_ready:
                    # Financial readiness is not qualitative readiness. Reuse
                    # only originals whose captured issuer and actual bytes
                    # still match; a tampered/foreign source never qualifies.
                    from ..financial_sources.downloads import local_cache_diagnostic
                    disclosure_filings = tuple(filing for filing in filings
                        if filing.company_cik in {subject.cik, subject.security_id}
                        and not local_cache_diagnostic(filing))
                evidence.extend(bind_compiled_disclosures(
                    admitted, cached_filing_evidence(self.storage, disclosure_filings,
                        cancel_check=cancelled)))
                if not admitted.research_ready and not has_sourced_disclosure(evidence):
                    raise ResearchPreparationUnavailable(
                        scanner_diagnostic_code(admitted.diagnostics)
                        or (prepared.recovery_outcome.diagnostic_code if prepared.recovery_outcome else "")
                        or "FILING_DATA_QUALITY_FAILED", outcome=prepared.recovery_outcome)
                profile, capabilities = admitted.profile, admitted.capability_map
                scope_gaps = financial_scope_gaps(facts, filings)
                accessions = {item.accession_number for item in filings}
                generations = dict(prepared.financial_generations)
                if not facts or not (profile.metrics or profile.interim_metrics) or scope_gaps:
                    targets = tuple(sorted(accessions))
                    capabilities = {**capabilities, "financial_recovery_pending": True}
                    if any(model.enabled for model in captured.models) and not captured.allow_partial_research:
                        raise ResearchPreparationUnavailable("FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED",
                                                             outcome=prepared.recovery_outcome, coverage=coverage)
                source_state = {
                    "freshness": prepared.discovery.freshness,
                    "discovery_diagnostics": list(prepared.discovery.diagnostics),
                    "candidate_diagnostics": list(publication.get("diagnostics", ())),
                    "recovery_targets": list(targets),
                    "currency_lane_ambiguous": bool(prepared.currency_lane_ambiguous),
                }
                report("filing-validation", len(filings), len(filings))
                if not facts and not has_sourced_disclosure(evidence):
                    raise ResearchPreparationUnavailable(
                        "FILING_DATA_QUALITY_FAILED" if filings else "FILING_FORMAT_UNSUPPORTED")
        except CancelledError as exc:
            raise ResearchCancelled() from exc
        except (OfficialFinancialUnavailable, ResearchPreparationUnavailable) as exc:
            if exc.code.startswith("FILING_"):
                self._record_unavailable(captured, subject, targets, stage, exc)
            raise ResearchPreparationUnavailable(exc.code, outcome=exc.outcome, coverage=coverage) from exc
        check_cancelled()
        reproducibility = build_research_snapshot(
            subject, facts, evidence, captured.selected_pack, captured.report_language,
            captured.parallel_agents, captured.annual_history_years, valuation_inputs,
            market_snapshot, captured.valuation_policy, captured.vision_policy, captured.policy_source)
        if not generations:
            generations = {str(item["accession_number"]): str(item["generation_id"])
                           for item in facts if item.get("accession_number") and item.get("generation_id")}
        reproducibility["data_snapshot"].update({
            "financial_capabilities": capabilities, "financial_generations": generations,
            "partial_research_scope_authorized": captured.allow_partial_research})
        if source_state is not None:
            reproducibility["data_snapshot"]["official_source_state"] = source_state
        if coverage is not None:
            reproducibility["data_snapshot"]["disclosure_coverage"] = coverage.to_dict()
        return PreparedResearchStart(ResearchStartRequest(
            subject, profile or facts, captured.selected_pack, captured.models,
            filing_evidence=tuple(evidence), valuation_inputs=valuation_inputs,
            market_snapshot=market_snapshot, reproducibility=reproducibility,
            existing_run=captured.existing_run, report_language=captured.report_language,
            ui_language=captured.ui_language, parallel_agents=captured.parallel_agents,
            recovery_session_id=captured.recovery_session_id), coverage)

    def _record_unavailable(self, request, company, targets, stage, error):
        key = company.cik if company.market == "US" else company.security_id
        recovery_run = request.recovery_session_id or (request.existing_run.run_id if request.existing_run else "")
        if not recovery_run:
            return  # Preserve the scope error when this entry has no durable run yet.
        for filing in self.storage.get_filings(key):
            if targets and filing.accession_number not in targets:
                continue
            self.storage.save_financial_recovery_case(
                run_id=recovery_run, company_cik=key,
                accession_number=filing.accession_number, document_hash=filing.content_hash,
                stage=stage, error_code=error.code,
                next_action=next(iter(error.outcome.next_actions), "repair_required_official_evidence"),
                diagnostics={"run_job": request.job_id})
