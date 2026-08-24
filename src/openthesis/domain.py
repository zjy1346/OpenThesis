from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClaimKind(StrEnum):
    FACT = "fact"
    CALCULATION = "calculation"
    INFERENCE = "inference"
    ASSUMPTION = "assumption"
    FORECAST = "forecast"
    RISK = "risk"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Company:
    cik: str
    ticker: str
    name: str
    exchange: str = ""
    issuer_id: str = ""
    market: str = "US"
    security_id: str = ""
    listing_currency: str = "USD"
    reporting_currency: str = "USD"
    accounting_standard: str = "US_GAAP"
    industry: str = ""
    industry_support: str = "standard"
    source_url: str = ""

    def __post_init__(self) -> None:
        self.issuer_id = self.issuer_id or self.cik
        self.security_id = self.security_id or self.cik

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FilingDocument:
    document_id: str
    company_cik: str
    accession_number: str
    form_type: str
    fiscal_period: str
    period_end: str
    filed_at: str
    primary_document: str
    source_url: str
    local_path: str = ""
    content_hash: str = ""
    ingested_at: str = field(default_factory=utc_now_iso)
    revision: str = "original"
    supersedes_document_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FinancialFact:
    fact_id: str
    company_cik: str
    concept: str
    reported_concept: str
    value: float
    unit: str
    fiscal_year: int
    fiscal_period: str
    form_type: str
    start_date: str | None
    end_date: str
    filed_at: str
    accession_number: str
    source_url: str
    scope: str = "consolidated"
    # Rich provenance retained by the financial-ingestion layer.  The legacy
    # fields above remain the stable storage/API surface; these optional fields
    # make the statement context explicit instead of inferring it downstream.
    entity: str = ""
    market: str = ""
    statement: str = ""
    period_start: str | None = None
    consolidated_scope: str = "consolidated"
    currency: str = ""
    unit_scale: float = 1.0
    revision: str = "original"
    source_document: str = ""
    source_page: int | None = None
    source_bbox: tuple[float, float, float, float] | None = None
    raw_text: str = ""
    parser_version: str = ""
    validation_status: str = "unvalidated"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EvidenceRef:
    evidence_id: str
    document_id: str
    source_url: str
    title: str
    locator: str
    excerpt: str
    published_at: str
    content_hash: str = ""
    bbox: tuple[float, float, float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Claim:
    claim_id: str
    text: str
    kind: ClaimKind
    status: str
    confidence: float | None = None
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    created_by: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data


@dataclass(slots=True)
class GrowthOpportunity:
    opportunity_id: str
    title: str
    category: str
    mechanism: str
    maturity_stage: str
    evidence_grade: str
    time_horizon_years: int
    probability_low: float
    probability_high: float
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    capital_requirements: str = "unknown"
    leading_indicators: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    scenario_eligibility: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ForecastScenario:
    name: str
    probability: float
    horizon_years: int
    revenue_cagr_low: float
    revenue_cagr_high: float
    operating_margin_low: float | None = None
    operating_margin_high: float | None = None
    assumptions: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResearchArtifact:
    artifact_id: str
    run_id: str
    artifact_type: str
    title: str
    content: dict[str, Any]
    model_id: str = ""
    agent_id: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResearchRun:
    run_id: str
    company: Company
    workflow_id: str
    research_pack_id: str
    research_pack_version: str
    provider_id: str
    model_id: str
    data_as_of: str
    status: RunStatus = RunStatus.CREATED
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None
    errors: list[str] = field(default_factory=list)
    report_language: str = "zh-CN"
    market_snapshot: dict[str, Any] | None = None
    model_configuration: dict[str, Any] = field(default_factory=dict)
    research_configuration: dict[str, Any] = field(default_factory=dict)
    data_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data
