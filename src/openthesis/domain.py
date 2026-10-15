from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


CURRENT_DERIVED_VERSION = "financial-facts-v3"


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
    # Optional equity-scope metadata used by market-cap safety checks.  Empty
    # means the provider must supply a verified market cap; the DCF layer will
    # not derive one from a single quote across multiple share classes.
    share_classes: tuple[str, ...] = ()
    total_shares: float | None = None

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
    # Metadata knowledge is not an edition of the original PDF. Empty is the
    # backwards-compatible legacy state; financial statements still prove dates.
    period_metadata_status: str = ""
    period_metadata_diagnostic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FinancialNormalizationLineage:
    """Exact source-to-canonical monetary/ratio lineage for one fact.

    ``raw_value`` and ``scale`` are source strings, never recovered from the
    compatibility float field. ``inputs`` is populated only for deterministic
    facts aggregated from independently evidenced source rows.
    """

    raw_value: str
    scale: str
    normalized_value: str
    quantity_kind: str
    currency: str
    unit_evidence_ids: tuple[str, ...]
    method: str = "explicit-source-unit-v1"
    value_transform: str = "identity"
    inputs: tuple["FinancialNormalizationLineage", ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "FinancialNormalizationLineage | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        try:
            raw_inputs = value.get("inputs", ())
            raw_evidence_ids = value.get("unit_evidence_ids", ())
            if (
                not isinstance(raw_inputs, (list, tuple))
                or not isinstance(raw_evidence_ids, (list, tuple))
                or any(not isinstance(item, dict) and not isinstance(item, cls) for item in raw_inputs)
            ):
                return None
            parsed_inputs = tuple(cls.from_value(item) for item in raw_inputs)
            if any(item is None for item in parsed_inputs):
                return None
            return cls(
                raw_value=str(value.get("raw_value", "")),
                scale=str(value.get("scale", "")),
                normalized_value=str(value.get("normalized_value", "")),
                quantity_kind=str(value.get("quantity_kind", "unknown")),
                currency=str(value.get("currency", "")),
                unit_evidence_ids=tuple(str(item) for item in raw_evidence_ids),
                method=str(value.get("method", "explicit-source-unit-v1")),
                value_transform=str(value.get("value_transform", "identity")),
                inputs=parsed_inputs,
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class FinancialDimension:
    """Source identity only: an axis is not automatically a business segment.

    Explicit members use expanded QNames. Bounded scalar typed members retain
    their expanded domain name and exact XML text in a canonical JSON pair.
    Admission must bind these values to the captured source context.
    """

    axis: str
    member: str
    kind: str

    def __post_init__(self) -> None:
        if (not isinstance(self.axis, str) or not self.axis.startswith("{")
                or "}" not in self.axis or not isinstance(self.member, str)
                or not self.member or self.kind not in {"explicit", "typed"}):
            raise ValueError("FINANCIAL_DIMENSION_INVALID")

    @classmethod
    def from_value(cls, value: Any) -> "FinancialDimension":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or set(value) != {"axis", "member", "kind"}:
            raise ValueError("FINANCIAL_DIMENSION_INVALID")
        return cls(**value)


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
    # Unit/scale origin is separate from validation and usage status. Unknown
    # units must never be made to look explicit by a compatibility projection.
    unit_provenance: str = "unknown"
    revision: str = "original"
    source_document: str = ""
    source_page: int | None = None
    source_bbox: tuple[float, float, float, float] | None = None
    raw_text: str = ""
    parser_version: str = ""
    validation_status: str = "unvalidated"
    # Orthogonal lifecycle fields.  ``validation_status`` remains a
    # compatibility projection for older callers; it must not be used to
    # encode whether a fact is intended for research or audit only.
    extraction_status: str = "unresolved"
    usage_status: str = "audit_only"
    provenance_status: str = "unresolved"
    derived_version: str = CURRENT_DERIVED_VERSION
    # Original visual column header for same-filing comparison provenance.
    # Appended so older positional/database callers remain compatible.
    source_column: str = ""
    # Active parser generation which produced this fact. Empty values are
    # retained only for pre-generation legacy imports and synthetic fixtures.
    generation_id: str = ""
    # Appended typed source amount lineage. Old persisted facts hydrate as
    # ``None`` (unknown); they are never reverse-engineered from ``value``.
    normalization_lineage: FinancialNormalizationLineage | None = None
    # Role-prefixed source proofs: formal statement title (statement:) or
    # neighbouring equity hierarchy rows (classification:). Old facts stay
    # empty; admission independently verifies each role rather than trusting
    # a parser's concept/scope classification.
    classification_evidence_ids: tuple[str, ...] = ()
    # Append-only JSON storage contract. Missing legacy dimensions stay empty;
    # they cannot be reconstructed from a scope hash or guessed from a label.
    dimensions: tuple[FinancialDimension, ...] = ()

    def __post_init__(self) -> None:
        # JSON arrays and native tuples encode the same captured coordinates.
        # Rehydrate containers before source-ID replay; do not alter coordinate
        # values or weaken any geometry/amount checks in the compiler.
        if isinstance(self.source_bbox, list):
            self.source_bbox = tuple(self.source_bbox)
        if isinstance(self.classification_evidence_ids, list):
            self.classification_evidence_ids = tuple(self.classification_evidence_ids)
        if self.normalization_lineage is not None and not isinstance(
            self.normalization_lineage, FinancialNormalizationLineage
        ):
            self.normalization_lineage = FinancialNormalizationLineage.from_value(
                self.normalization_lineage
            )
        if not isinstance(self.dimensions, (tuple, list)) or len(self.dimensions) > 32:
            raise ValueError("FINANCIAL_DIMENSION_INVALID")
        self.dimensions = tuple(FinancialDimension.from_value(item) for item in self.dimensions)
        if len({item.axis for item in self.dimensions}) != len(self.dimensions):
            raise ValueError("FINANCIAL_DIMENSION_DUPLICATE")

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

    def __post_init__(self) -> None:
        if isinstance(self.bbox, list):
            self.bbox = tuple(self.bbox)

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
        # Artifacts accept extensible JSON business content, not arbitrary
        # external-container constructors. Keep the same explicit wire fields.
        from .resilience.contracts import plain_json_value
        return {"artifact_id": self.artifact_id, "run_id": self.run_id,
                "artifact_type": self.artifact_type, "title": self.title,
                "content": plain_json_value(self.content), "model_id": self.model_id,
                "agent_id": self.agent_id, "created_at": self.created_at}


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
