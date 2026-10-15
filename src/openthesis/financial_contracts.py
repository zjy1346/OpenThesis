"""I/O-free financial candidate, identity and normalization contracts.

Adapters collect observations; validators decide whether they can be admitted.
Legacy FinancialFact serialization stays in domain.py. Missing legacy metadata
remains unknown and is never inferred from issuer location or numeric size.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
import re

from typing import Any, Protocol, Sequence

from .domain import (
    Company, EvidenceRef, FilingDocument, FinancialFact,
    FinancialNormalizationLineage,
)

# Shared source/parser semantics used by both extraction and validation.
PDF_PARSER_VERSION = "financial-ingestion-ast-v31"
STATEMENT_FOR = {
    "revenue": "income_statement", "net_income": "income_statement",
    "total_operating_revenue": "income_statement",
    "comprehensive_income": "comprehensive_income",
    "parent_comprehensive_income": "comprehensive_income",
    "minority_interest_comprehensive_income": "comprehensive_income",
    "interest_revenue": "income_statement",
    "finance_interest_income": "income_statement",
    "operating_cash_flow": "cash_flow", "assets": "balance_sheet",
    "operating_cash_flow_before_tax": "cash_flow",
    "liabilities": "balance_sheet", "equity": "balance_sheet",
    "total_equity": "balance_sheet", "reported_roe": "summary",
    "mezzanine_equity": "balance_sheet", "minority_interest_equity": "balance_sheet",
    "profit_before_tax": "income_statement", "profit_after_tax": "income_statement",
    "parent_net_income": "income_statement", "minority_interest_profit": "income_statement",
    "ordinary_shareholder_net_income": "income_statement",
    "operating_income": "income_statement", "gross_profit": "income_statement",
    "cost_of_revenue": "income_statement", "capital_expenditure": "cash_flow",
    "capital_expenditure_ppe": "cash_flow", "capital_expenditure_intangible": "cash_flow",
    "share_repurchase": "cash_flow", "shareholder_dividends_paid": "cash_flow",
    "lease_liability_payment": "cash_flow", "deferred_asset_acquisition_payment": "cash_flow",
}


class ValidationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class SourceComponentObservation:
    """One source-bound component observation, separate from admission."""

    state: str
    value: Any
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {"observed", "unknown", "conflict", "verified"}:
            raise ValueError("SOURCE_COMPONENT_STATE_INVALID")
        object.__setattr__(self, "evidence_ids", tuple(str(item) for item in self.evidence_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "value": _json_safe_value(self.value),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class FactValidationRecord:
    """Machine-readable fact outcome retaining independently observed parts."""

    fact_id: str
    accepted: bool
    issues: tuple[str, ...]
    components: tuple[tuple[str, SourceComponentObservation], ...]
    dependencies: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        names = [name for name, _observation in self.components]
        if len(names) != len(set(names)):
            raise ValueError("SOURCE_COMPONENT_DUPLICATE")
        object.__setattr__(self, "issues", tuple(str(item) for item in self.issues))
        object.__setattr__(
            self, "components",
            tuple((str(name), observation) for name, observation in self.components),
        )
        object.__setattr__(
            self, "dependencies",
            tuple((str(name), tuple(str(item) for item in ids)) for name, ids in self.dependencies),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "accepted": self.accepted,
            "issues": list(self.issues),
            "components": {name: observation.to_dict() for name, observation in self.components},
            "dependencies": {name: list(ids) for name, ids in self.dependencies},
        }


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class FinancialValidation:
    status: ValidationStatus
    issues: tuple[str, ...]
    covered_concepts: frozenset[str]
    accepted: tuple[FinancialFact, ...] = ()
    quarantined: tuple[FinancialFact, ...] = ()
    fact_records: tuple[FactValidationRecord, ...] = ()

    @property
    def allow_ai(self) -> bool:
        return self.status in {ValidationStatus.VERIFIED, ValidationStatus.READY_WITH_WARNINGS}

    @property
    def quality_class(self) -> str:
        """Classify the failure boundary without flattening warnings.

        Coverage warnings mean that individual fields are unavailable while
        the accepted sibling facts remain auditable.  Other issues invalidate
        the statement/group as a whole; an empty group means the source file
        produced no usable statement facts.
        """
        coverage = {
            "income_statement_core_missing",
            "cash_flow_core_missing",
            "balance_sheet_core_missing",
            "core_coverage_insufficient",
        }
        if not self.covered_concepts and not self.accepted:
            return "file_unavailable"
        if any(issue not in coverage for issue in self.issues):
            return "statement_unavailable" if self.status is ValidationStatus.REJECTED else "warning"
        if self.issues:
            return "field_missing"
        return "verified"

    @property
    def category(self) -> str:
        """Backward-compatible alias for callers that use category wording."""
        return self.quality_class



class QuantityKind(StrEnum):
    MONEY = "money"
    SHARES = "shares"
    RATIO = "ratio"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    issuer_id: str
    document_id: str
    accession: str
    content_hash: str
    period_end: str
    fiscal_period: str
    revision: str = "original"

    @classmethod
    def from_filing(cls, filing: FilingDocument) -> SourceIdentity:
        return cls(
            filing.company_cik, filing.document_id, filing.accession_number,
            filing.content_hash, filing.period_end, filing.fiscal_period, filing.revision,
        )


@dataclass(frozen=True, slots=True)
class FactIdentity:
    issuer_id: str
    concept: str
    period_start: str | None
    period_end: str
    fiscal_period: str
    scope: str
    currency: str
    revision: str
    generation: str

    @classmethod
    def from_fact(cls, fact: FinancialFact) -> FactIdentity:
        return cls(
            fact.company_cik, fact.concept, fact.start_date or fact.period_start,
            fact.end_date, fact.fiscal_period,
            fact.consolidated_scope or fact.scope or "unknown",
            fact.currency or "", fact.revision,
            str(getattr(fact, "generation_id", "") or ""),
        )


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    invariant_id: str
    affected_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    severity: str = "error"


@dataclass(frozen=True, slots=True)
class CoverageGap:
    identity: FactIdentity
    concept: str
    reason: str
    dependent_ids: tuple[str, ...] = ()


NormalizationLineage = FinancialNormalizationLineage


def source_numeric_token(raw: str) -> str:
    """Canonical typography only; retain raw strings in normalization proof.

    OCR can print a full-width comma or accounting parenthesis. Normalize those
    glyphs, not numbers, O/0 guesses, decimal commas or spaces between amounts.
    The strict grouped-decimal grammar remains the caller's responsibility.
    """
    token = raw.strip().translate(str.maketrans(
        "０１２３４５６７８９，．（）＋－−", "0123456789,.()+--"))
    return re.sub(r"(?<=,)\s+(?=\d)|(?<=\d)\s+(?=,)", "", token)


@dataclass(frozen=True, slots=True)
class SourceAmount:
    """Exact observed decimal. Construct only from source strings, not float."""

    raw_value: str
    scale: str
    quantity_kind: QuantityKind
    currency: str = ""
    unit_evidence_ids: tuple[str, ...] = ()
    value_transform: str = "identity"

    def normalize(self) -> NormalizationLineage:
        if not isinstance(self.raw_value, str) or not isinstance(self.scale, str):
            raise ValueError("SOURCE_DECIMAL_STRING_REQUIRED")
        kind = QuantityKind(self.quantity_kind)
        if kind is QuantityKind.UNKNOWN or not self.unit_evidence_ids:
            raise ValueError("EXPLICIT_QUANTITY_UNIT_REQUIRED")
        if self.value_transform not in {"identity", "absolute_value", "negative_absolute_value"}:
            raise ValueError("UNSUPPORTED_SOURCE_VALUE_TRANSFORM")
        currency = self.currency.strip().upper()
        if kind is QuantityKind.MONEY and not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("EXPLICIT_CURRENCY_REQUIRED")
        token = source_numeric_token(self.raw_value)
        if token.startswith("(") and token.endswith(")"):
            token = "-" + token[1:-1].strip()
        if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", token):
            raise ValueError("INVALID_SOURCE_DECIMAL")
        token = token.replace(",", "")
        try:
            value, scale = Decimal(token), Decimal(self.scale)
        except InvalidOperation as exc:
            raise ValueError("INVALID_SOURCE_DECIMAL") from exc
        if not value.is_finite() or not scale.is_finite() or scale <= 0:
            raise ValueError("INVALID_SOURCE_SCALE_OR_VALUE")
        # Precision grows with both source strings; global Decimal context must
        # not truncate a long financial source value during unit conversion.
        normalized = exact_decimal_arithmetic((value, scale), "multiply")
        if self.value_transform == "absolute_value":
            normalized = normalized.copy_abs()
        elif self.value_transform == "negative_absolute_value":
            normalized = normalized.copy_abs().copy_negate()
        return NormalizationLineage(
            self.raw_value, self.scale, format(normalized, "f"),
            kind.value, currency, tuple(self.unit_evidence_ids),
            value_transform=self.value_transform,
        )


def exact_decimal_arithmetic(values: Sequence[Decimal], operation: str = "sum") -> Decimal:
    """Compose normalized amounts without depending on global precision."""
    amounts = tuple(values)
    if (
        not amounts or operation not in {"sum", "subtract", "multiply"}
        or any(not isinstance(value, Decimal) or not value.is_finite() for value in amounts)
    ):
        raise ValueError("INVALID_DERIVED_DECIMAL_OPERATION")
    minimum_exponent = min(value.as_tuple().exponent for value in amounts)
    maximum_adjusted = max(value.adjusted() for value in amounts)
    with localcontext() as context:
        # Align every input's decimal places and leave enough carry digits for
        # the number of operands. No intermediate addition may discard cents.
        context.prec = max(
            28, maximum_adjusted - minimum_exponent + 1 + len(str(len(amounts))) + 2,
            sum(len(value.as_tuple().digits) for value in amounts) + 2 if operation == "multiply" else 0,
        )
        if operation == "multiply":
            result = Decimal("1")
            for value in amounts:
                result *= value
            return result
        if operation == "subtract":
            return amounts[0] - sum(amounts[1:], Decimal("0"))
        return sum(amounts, Decimal("0"))


@dataclass(frozen=True, slots=True)
class FactCandidate:
    """An untrusted fact emitted by one extractor and its evidence."""

    fact: FinancialFact
    evidence: tuple[EvidenceRef, ...] = ()
    extractor: str = ""


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    filing: FilingDocument
    candidates: tuple[FactCandidate, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FinancialSourceFailure:
    document_id: str
    accession_number: str
    stage: str
    code: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class FinancialCandidateCollection:
    """Untrusted source observations, never an admission decision."""

    manifests: tuple[Any, ...]
    batches_by_document: dict[str, tuple[CandidateBatch, ...]]
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[str, ...] = ()
    source_failures: tuple[FinancialSourceFailure, ...] = ()


def fact_row_evidence_ids(fact: FinancialFact) -> tuple[str, ...]:
    """Stable explicit aliases used to bind a candidate to its source row."""
    evidence_ids = [f"fact:{fact.fact_id}"]
    if fact.fact_id.startswith("ingest:"):
        evidence_ids.append(f"fact:{fact.fact_id.removeprefix('ingest:')}")
    return tuple(evidence_ids)


def candidate_batches_from_facts(
    filings: Sequence[FilingDocument],
    facts: Sequence[FinancialFact],
    evidence: Sequence[EvidenceRef] = (),
) -> tuple[CandidateBatch, ...]:
    """Adapt persisted facts without inventing provenance or normalizing values.

    Evidence attaches to a fact only when the persisted evidence id follows the
    explicit ``fact:<fact_id>`` relation and names the same declared filing.
    Unmatched-accession facts are retained on a declared batch with a diagnostic
    so the compiler can quarantine and expose them instead of silently dropping
    them. Their original fields, including unknown source/unit metadata, remain
    unchanged.
    """

    selected_filings = tuple(filings)
    if not selected_filings and (facts or evidence):
        raise ValueError("FROZEN_FACTS_REQUIRE_DECLARED_FILINGS")
    if not selected_filings:
        return ()

    filing_by_accession = {
        filing.accession_number: filing for filing in selected_filings
    }
    facts_by_document: dict[str, list[FactCandidate]] = {
        filing.document_id: [] for filing in selected_filings
    }
    diagnostics_by_document: dict[str, list[str]] = {
        filing.document_id: [] for filing in selected_filings
    }
    evidence_by_document: dict[str, list[EvidenceRef]] = {
        filing.document_id: [] for filing in selected_filings
    }
    for ref in evidence:
        if ref.document_id in evidence_by_document:
            evidence_by_document[ref.document_id].append(ref)
        else:
            # Keep orphan evidence visible for audit, but never associate it
            # with a candidate from a different filing.
            first = selected_filings[0].document_id
            evidence_by_document[first].append(ref)
            diagnostics_by_document[first].append(
                f"undeclared_evidence_document:{ref.document_id}"
            )

    for fact in facts:
        filing = filing_by_accession.get(fact.accession_number)
        if filing is None:
            filing = selected_filings[0]
            diagnostics_by_document[filing.document_id].append(
                f"undeclared_accession_candidate:{fact.fact_id}"
            )
        # This is the parser's explicit serialized relation, not a positional
        # guess. Retain the original ref id, page, hash and locator unchanged.
        expected_ids = set(fact_row_evidence_ids(fact))
        refs = tuple(
            ref for ref in evidence
            if ref.evidence_id in expected_ids
            and ref.document_id == filing.document_id
        )
        facts_by_document[filing.document_id].append(
            FactCandidate(fact, refs, "persisted-fact")
        )

    return tuple(
        CandidateBatch(
            filing,
            tuple(facts_by_document[filing.document_id]),
            tuple(evidence_by_document[filing.document_id]),
            tuple(dict.fromkeys(diagnostics_by_document[filing.document_id])),
        )
        for filing in selected_filings
    )

CORE_CONCEPTS = frozenset(
    {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"}
)


def concepts_cover_profile(concepts: Sequence[str], required_concepts: Sequence[str]) -> bool:
    """Coverage of disclosed roles, never a monetary alias or derivation."""
    return not missing_profile_concepts(concepts, required_concepts)


def missing_profile_concepts(concepts: Sequence[str], required_concepts: Sequence[str]) -> set[str]:
    """A profit disclosure can be parent or group attributed.

    Parent earnings satisfy profit *availability*, not group earnings arithmetic.
    Stored concepts, cross-period comparisons and formula inputs stay distinct.
    An explicitly requested parent profit cannot be covered by group profit.
    """
    available = {str(item).strip().lower() for item in concepts if item}
    required = {str(item).strip().lower() for item in required_concepts if item}
    if "equity" in required and "total_equity" in available:
        available.add("equity")
    if "net_income" in required and "parent_net_income" in available:
        available.add("net_income")
    return required - available

class FilingSource(Protocol):
    """Discover official filings without deciding which facts are trusted."""

    def fetch(
        self, subject: Company, period_range: object
    ) -> Sequence[FilingDocument]: ...


class FactExtractor(Protocol):
    """Extract candidates; accepted/resolved status is compiler-owned."""

    def extract(self, subject: Company, filing: FilingDocument) -> CandidateBatch: ...


class GapStageKind(StrEnum):
    """Stable order for deterministic, zero-token gap resolution."""

    OFFICIAL_STRUCTURED = "official_structured"
    PDF_AST = "pdf_ast"
    SAME_YEAR_OFFICIAL = "same_year_official"
    MINERU = "mineru"

@dataclass(frozen=True, slots=True)
class FinancialGroupValidation:
    identity: tuple[str, str, str, str, str]
    validation: FinancialValidation
    role: str = ""

@dataclass(frozen=True, slots=True)
class CompilationPolicy:
    """Frozen validation scope; deliberately has no I/O collaborators."""

    filings: tuple[FilingDocument, ...] = ()
    required_concepts: frozenset[str] | None = None
    reporting_currency: str = ""
    fiscal_period: str = "FY"
    scope: str = "consolidated"
    period_range: object | None = None


@dataclass(frozen=True, slots=True)
class FactGroupValidation:
    identity: tuple[str, str, str, str, str]
    status: str
    issues: tuple[str, ...] = ()
    covered: frozenset[str] = frozenset()
    accepted: tuple[FinancialFact, ...] = ()
    quarantined: tuple[FinancialFact, ...] = ()
    role: str = "target"


@dataclass(frozen=True, slots=True)
class FinancialDataset:
    """Canonical compiler output; no downstream layer needs source knowledge."""

    filings: tuple[FilingDocument, ...]
    resolved_facts: tuple[FinancialFact, ...]
    quarantined_facts: tuple[FinancialFact, ...]
    evidence: tuple[EvidenceRef, ...]
    conflicts: tuple[dict[str, Any], ...]
    validations: tuple[FactGroupValidation, ...]
    coverage: dict[str, Any]
    diagnostics: tuple[str, ...]
    allow_ai: bool
    research_facts: tuple[FinancialFact, ...] = ()
    research_validations: tuple[FactGroupValidation, ...] = ()
    manifests: tuple[Any, ...] = ()
    annual_facts: tuple[FinancialFact, ...] = ()
    interim_facts: tuple[FinancialFact, ...] = ()
    comparator_facts: tuple[FinancialFact, ...] = ()
    source_failures: tuple[FinancialSourceFailure, ...] = ()

    @property
    def accepted_facts(self) -> tuple[FinancialFact, ...]:
        """Compatibility alias for callers migrating from the old dataset."""

        return self.resolved_facts

    @property
    def supplemental_facts(self) -> tuple[FinancialFact, ...]:
        """Validated disclosures, never inputs to consolidated metrics.

        Selection follows admitted period cohorts, not all audit records. The
        validator must have accepted each row; resolved audit-only fallbacks
        and quarantined siblings cannot qualify by their extractor label.
        """
        cohorts = {(item.identity[0], item.identity[1], item.identity[2])
                   for item in self.research_validations if item.accepted}
        ids = {fact.fact_id for item in self.validations
               if (item.identity[0], item.identity[1], item.identity[2]) in cohorts
               for fact in item.accepted
               if (fact.dimensions and fact.consolidated_scope.startswith("dimension:"))
               or fact.concept == "shares_outstanding"}
        return tuple(fact for fact in self.resolved_facts if fact.fact_id in ids)

    @property
    def manifest(self) -> tuple[Any, ...]:
        """Legacy spelling for collected filing manifests."""

        return self.manifests

    @property
    def group_validations(self) -> tuple[Any, ...]:
        """Compatibility projection; compiler remains the source of status."""


        projected = []
        for item in self.validations:
            status_name = str(item.status)
            status = (
                ValidationStatus.VERIFIED
                if status_name == ValidationStatus.VERIFIED.value
                else ValidationStatus.REJECTED
                if status_name in {ValidationStatus.REJECTED.value, "CONFLICTED"}
                else ValidationStatus.READY_WITH_WARNINGS
            )
            projected.append(FinancialGroupValidation(
                tuple(item.identity),
                FinancialValidation(
                    status, tuple(item.issues), item.covered,
                    tuple(item.accepted), tuple(item.quarantined),
                ),
                item.role,
            ))
        return tuple(projected)

    @property
    def validation(self) -> Any:
        """Compatibility aggregate for pre-compiler ingestion callers."""


        status = (
            ValidationStatus.VERIFIED if self.allow_ai
            else ValidationStatus.READY_WITH_WARNINGS if self.resolved_facts
            else ValidationStatus.REJECTED
        )
        return FinancialValidation(
            status,
            tuple(self.diagnostics),
            frozenset(item.concept for item in self.resolved_facts),
            tuple(self.resolved_facts), tuple(self.quarantined_facts),
        )

    @property
    def status(self) -> str:
        if self.allow_ai:
            return ValidationStatus.VERIFIED.value
        return ValidationStatus.REJECTED.value if not self.resolved_facts else "INCOMPLETE"

    @property
    def capabilities(self) -> dict[str, bool]:
        """Expose evidence capabilities instead of one pipeline-wide switch.

        A missing comparator must disable growth/trend calculations, but it
        must not erase a verified current-period balance sheet or stop
        qualitative research that can cite disclosure evidence.
        """

        usable_groups = tuple(
            item for item in self.research_validations
            if item.status in {
                ValidationStatus.VERIFIED.value,
                ValidationStatus.READY_WITH_WARNINGS.value,
                "PARTIAL",
                "INCOMPLETE",
            }
            and item.accepted
        )
        annual_years = {fact.fiscal_year for fact in self.annual_facts if fact.fiscal_year}
        current_financials = bool(self.research_facts and usable_groups)
        return {
            "financial_snapshot": current_financials,
            "annual_trend": current_financials and len(annual_years) >= 2,
            "interim_comparison": bool(self.interim_facts and self.comparator_facts),
            "model_financial_analysis": self.allow_ai,
            "qualitative_research": True,
        }

    def has_capability(self, name: str) -> bool:
        return bool(self.capabilities.get(name, False))


FactSet = FinancialDataset
