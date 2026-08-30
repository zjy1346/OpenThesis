"""Canonical financial-fact compiler.

The compiler is the single decision boundary between source adapters and the
facts consumed by research.  Adapters produce candidates and evidence only;
resolution, conflict handling, validation, coverage and AI eligibility live
here so a service or retry path cannot accidentally create a second policy.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
import inspect
import time
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .financial_ingestion import FinancialIngestionEngine
from .market_financials import ValidationStatus


CORE_CONCEPTS = frozenset(
    {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"}
)


def _emit_filing_progress(
    progress: Any,
    stage: str,
    current: int,
    total: int,
    filing: FilingDocument,
    *,
    status: str,
) -> None:
    """Emit stable filing states while retaining three-argument callbacks."""

    if progress is None:
        return
    detail = {
        "filing_id": filing.document_id,
        "label": filing.primary_document or filing.accession_number or filing.document_id,
        "status": status,
        "error_code": "",
        "elapsed_seconds": 0.0,
    }
    try:
        signature = inspect.signature(progress)
        accepts_detail = any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ) or len(signature.parameters) >= 4
    except (TypeError, ValueError):
        accepts_detail = True
    if accepts_detail:
        progress(stage, current, total, detail)
    else:
        progress(stage, current, total)


def concepts_cover_profile(
    concepts: Iterable[str], required_concepts: Iterable[str]
) -> bool:
    """Return whether resolved concepts satisfy a declared coverage profile.

    ``total_equity`` is the canonical balance-sheet equivalent of ``equity``
    when the issuer does not disclose a separate attributable-equity row.  The
    alias is intentionally handled once here so service/status/cache paths
    cannot accidentally reintroduce an industrial-only six-concept gate.
    """

    available = {str(item).strip().lower() for item in concepts if item}
    required = {str(item).strip().lower() for item in required_concepts if item}
    if "equity" in required and "total_equity" in available:
        available.add("equity")
    return required.issubset(available)


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
class CoverageProfile:
    """Declared coverage contract for a subject, independent of extraction."""

    profile_id: str
    required_concepts: frozenset[str]
    optional_concepts: frozenset[str] = frozenset()
    reason: str = ""
    market: str = ""
    accounting_standard: str = ""
    company_type: str = ""


class CoveragePlanner:
    """Select a transparent profile without encoding company-specific rules."""

    def plan(self, subject: Company) -> CoverageProfile:
        company_type = str(getattr(subject, "company_type", "") or "")
        descriptor = f"{subject.industry} {subject.industry_support} {company_type}".casefold()
        if any(term in descriptor for term in ("insurance", "保险")):
            return CoverageProfile(
                "insurance",
                frozenset({"net_income", "assets", "liabilities", "equity"}),
                frozenset({"revenue", "operating_cash_flow", "profit_before_tax", "provisions"}),
                "insurance profile",
                subject.market,
                subject.accounting_standard,
                company_type,
            )
        if any(term in descriptor for term in ("broker", "brokerage", "securities", "券商", "证券")):
            return CoverageProfile(
                "securities",
                frozenset({"net_income", "assets", "liabilities", "equity"}),
                frozenset({"revenue", "operating_cash_flow", "profit_before_tax"}),
                "securities/broker profile",
                subject.market,
                subject.accounting_standard,
                company_type,
            )
        if any(term in descriptor for term in ("bank", "银行")):
            return CoverageProfile(
                "bank",
                frozenset({"net_income", "assets", "liabilities", "equity"}),
                frozenset({"revenue", "operating_cash_flow", "profit_before_tax", "provisions"}),
                "bank profile",
                subject.market,
                subject.accounting_standard,
                company_type,
            )
        return CoverageProfile(
            "non_financial",
            CORE_CONCEPTS,
            frozenset({"gross_profit", "operating_income", "capital_expenditure"}),
            "non-financial core-six profile",
            subject.market,
            subject.accounting_standard,
            company_type,
        )


@dataclass(frozen=True, slots=True)
class GapResolution:
    """Candidates found while resolving a bounded, explicit concept gap."""

    candidates: tuple[FactCandidate, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    attempted_stages: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    cancelled: bool = False


class GapResolver:
    """Run fixed source stages in order; never accepts or invents facts."""

    _ORDER = (
        GapStageKind.OFFICIAL_STRUCTURED,
        GapStageKind.PDF_AST,
        GapStageKind.SAME_YEAR_OFFICIAL,
        GapStageKind.MINERU,
    )

    def resolve(
        self,
        subject: Company,
        filing: FilingDocument,
        missing_concepts: Sequence[str],
        extractors: Sequence[FactExtractor],
        *,
        existing_concepts: Sequence[str] = (),
        cancel_check: Any = None,
        user_authorized: bool = False,
        max_attempts: int = 1,
    ) -> GapResolution:
        wanted = frozenset(missing_concepts)
        existing = frozenset(existing_concepts)
        found: list[FactCandidate] = []
        refs: list[EvidenceRef] = []
        stages: list[str] = []
        diagnostics: list[str] = []
        previous_order = -1
        for extractor in extractors:
            if cancel_check is not None and cancel_check():
                return GapResolution(tuple(found), tuple(refs), tuple(stages), tuple(diagnostics), True)
            name = str(getattr(extractor, "name", type(extractor).__name__))
            kind = getattr(extractor, "stage_kind", None)
            if kind is None:
                diagnostics.append(f"{name}:missing_stage_kind")
                continue
            try:
                kind = GapStageKind(kind)
            except ValueError:
                diagnostics.append(f"{name}:unknown_stage_kind")
                continue
            order = self._ORDER.index(kind)
            if order <= previous_order:
                diagnostics.append("invalid_gap_stage_order")
                continue
            previous_order = order
            stages.append(kind.value)
            if kind is GapStageKind.MINERU and not user_authorized:
                diagnostics.append("mineru_not_authorized")
                continue
            stage_limit = max(1, min(3, int(getattr(extractor, "max_attempts", max_attempts))))
            for _attempt in range(stage_limit):
                if cancel_check is not None and cancel_check():
                    return GapResolution(tuple(found), tuple(refs), tuple(stages), tuple(diagnostics), True)
                try:
                    batch = extractor.extract(subject, filing)
                except Exception as exc:  # adapters are untrusted boundaries
                    diagnostics.append(f"{kind.value}:failed:{type(exc).__name__}")
                    continue
                diagnostics.extend(f"{kind.value}:{item}" for item in batch.diagnostics)
                seen_keys = {
                    (filing.document_id, item.fact.concept, item.extractor, tuple(ref.evidence_id for ref in item.evidence))
                    for item in found
                }
                for candidate in batch.candidates:
                    key = (filing.document_id, candidate.fact.concept, name, tuple(ref.evidence_id for ref in candidate.evidence))
                    # A lower-priority source must not overwrite a concept
                    # already supplied by the primary source.  Keep the rest
                    # of the stage's atomic candidate batch, however: sibling
                    # facts such as ``total_equity`` are required to validate
                    # a requested ``equity`` fact and are also part of the
                    # public audit view.  Every retained sibling still passes
                    # through the same compiler quality gate.
                    if candidate.fact.concept not in existing and key not in seen_keys and candidate.fact.concept not in {
                        item.fact.concept for item in found
                    }:
                        found.append(candidate)
                        refs.extend(candidate.evidence)
                        seen_keys.add(key)
                if batch.candidates:
                    break
            if concepts_cover_profile((item.fact.concept for item in found), wanted):
                break
        if not concepts_cover_profile((item.fact.concept for item in found), wanted):
            diagnostics.append("gap_unresolved")
        return GapResolution(tuple(found), tuple(refs), tuple(stages), tuple(diagnostics))


@dataclass(frozen=True, slots=True)
class StructuredFactExtractor:
    """Adapt the existing structured-source protocol to candidate batches."""

    source: Any
    name: str = "structured-source"
    stage_kind: GapStageKind = GapStageKind.OFFICIAL_STRUCTURED

    def extract(self, subject: Company, filing: FilingDocument) -> CandidateBatch:
        facts, refs, failure = self.source.fetch(subject, filing)
        if failure:
            return CandidateBatch(filing, diagnostics=(str(failure),))
        refs_by_fact = {fact.fact_id: ref for fact, ref in zip(facts, refs)}
        candidates = tuple(
            FactCandidate(fact, ((refs_by_fact[fact.fact_id],) if fact.fact_id in refs_by_fact else ()), self.name)
            for fact in facts
        )
        return CandidateBatch(filing, candidates, tuple(refs))


@dataclass(frozen=True, slots=True)
class CompilerPolicy:
    """Immutable compilation policy and adapter injection seam."""

    filings: tuple[FilingDocument, ...] = ()
    filing_source: FilingSource | None = None
    extractors: tuple[FactExtractor, ...] = ()
    required_concepts: frozenset[str] | None = None
    reporting_currency: str = ""
    fiscal_period: str = "FY"
    scope: str = "consolidated"
    period_range: object | None = None
    gap_extractors: tuple[FactExtractor, ...] = ()
    gap_user_authorized: bool = False
    gap_max_attempts: int = 1


@dataclass(frozen=True, slots=True)
class FactGroupValidation:
    identity: tuple[str, str, str, str, str]
    status: str
    issues: tuple[str, ...] = ()
    covered: frozenset[str] = frozenset()
    accepted: tuple[FinancialFact, ...] = ()
    quarantined: tuple[FinancialFact, ...] = ()


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

    @property
    def accepted_facts(self) -> tuple[FinancialFact, ...]:
        """Compatibility alias for callers migrating from the old dataset."""

        return self.resolved_facts

    @property
    def manifest(self) -> tuple[Any, ...]:
        """Legacy spelling for collected filing manifests."""

        return self.manifests

    @property
    def group_validations(self) -> tuple[Any, ...]:
        """Compatibility projection; compiler remains the source of status."""

        from .financial_ingestion import FinancialGroupValidation, FinancialValidation
        from .market_financials import ValidationStatus

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
            ))
        return tuple(projected)

    @property
    def validation(self) -> Any:
        """Compatibility aggregate for pre-compiler ingestion callers."""

        from .financial_ingestion import FinancialValidation
        from .market_financials import ValidationStatus

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


@dataclass(frozen=True, slots=True)
class StaticFilingSource:
    """Small deterministic filing source for fixtures and offline callers."""

    filings: tuple[FilingDocument, ...]

    def fetch(self, subject: Company, period_range: object) -> Sequence[FilingDocument]:
        return self.filings


@dataclass(frozen=True, slots=True)
class InMemoryFactExtractor:
    """Fixture adapter that still goes through candidate resolution and gating."""

    batches: dict[str, CandidateBatch]

    def extract(self, subject: Company, filing: FilingDocument) -> CandidateBatch:
        return self.batches.get(filing.document_id, CandidateBatch(filing))


@dataclass(frozen=True, slots=True)
class MappedCandidateExtractor:
    """Expose pre-collected batches as a typed compiler stage."""

    batches: dict[str, CandidateBatch]
    stage_kind: GapStageKind
    name: str
    diagnostics: tuple[str, ...] = ()

    def extract(self, subject: Company, filing: FilingDocument) -> CandidateBatch:
        batch = self.batches.get(filing.document_id, CandidateBatch(filing))
        if not self.diagnostics:
            return batch
        return CandidateBatch(
            batch.filing, batch.candidates, batch.evidence,
            tuple((*batch.diagnostics, *self.diagnostics)),
        )


@dataclass(frozen=True, slots=True)
class VisionCandidateExtractor:
    """Candidate-only wrapper for the optional, user-authorized vision stage."""

    engine: Any
    adapter: Any
    config: Any
    cancel_check: Any = None
    validation_issues_by_document: Mapping[str, Sequence[str]] | None = None
    existing_facts_by_document: Mapping[str, Sequence[FinancialFact]] | None = None
    stage_kind: GapStageKind = GapStageKind.MINERU
    name: str = "vision-fallback"

    def extract(self, subject: Company, filing: FilingDocument) -> CandidateBatch:
        from .financial_ingestion import _vision_failed_pages

        if self.cancel_check is not None and self.cancel_check():
            return CandidateBatch(filing, diagnostics=("vision_cancelled",))
        issues = (self.validation_issues_by_document or {}).get(filing.document_id, ())
        existing_facts = (self.existing_facts_by_document or {}).get(
            filing.document_id, ()
        )
        pages = _vision_failed_pages(filing, self.config, issues, existing_facts)
        if not pages:
            return CandidateBatch(filing, diagnostics=("vision_no_failed_pages",))
        result = self.adapter.extract(
            subject, filing, pages, self.config, cancel_check=self.cancel_check
        )
        refs_by_fact = {
            fact.fact_id: ref for fact, ref in zip(result.facts, result.evidence)
        }
        candidates = tuple(
            FactCandidate(
                fact,
                ((refs_by_fact[fact.fact_id],) if fact.fact_id in refs_by_fact else ()),
                self.name,
            )
            for fact in result.facts
        )
        return CandidateBatch(
            filing, candidates, tuple(result.evidence), tuple(result.diagnostics)
        )


def _prefetch_vision_batches(
    subject: Company,
    filings: Sequence[FilingDocument],
    extractor: VisionCandidateExtractor,
    *,
    timeout_seconds: float,
) -> dict[str, CandidateBatch]:
    """Run authorized visual gap extraction with deterministic bounded fan-out.

    The first canonical pass determines the missing filing contexts before
    this function is called. Results are collected in filing order and then
    passed to one compiler quality gate; a timeout, cancellation, or adapter
    failure produces diagnostics only and is never cached as a candidate.
    """

    ordered = tuple(filings)
    if not ordered:
        return {}
    worker_count = max(1, min(2, len(ordered)))
    executor = ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="financial-vision"
    )
    futures = {
        filing.document_id: executor.submit(extractor.extract, subject, filing)
        for filing in ordered
    }
    results: dict[str, CandidateBatch] = {}
    deadline = time.monotonic() + max(0.01, float(timeout_seconds))
    try:
        for filing in ordered:
            future = futures[filing.document_id]
            remaining = max(0.0, deadline - time.monotonic())
            try:
                batch = future.result(timeout=remaining)
            except FuturesTimeoutError:
                batch = CandidateBatch(filing, diagnostics=("vision_timeout",))
            except Exception as exc:
                batch = CandidateBatch(
                    filing, diagnostics=(f"vision_failed:{type(exc).__name__}",)
                )
            results[filing.document_id] = batch
    finally:
        # Adapters receive the coordinator cancellation callback and own their
        # remote timeout. Waiting here guarantees no executor thread survives
        # the compiler call, while cancel_futures prevents queued work from
        # starting after a timeout/cancellation.
        executor.shutdown(wait=True, cancel_futures=True)
    return results


def _period_range_bounds(value: object) -> tuple[str | None, str | None]:
    """Normalize supported date/year windows without inventing a period."""

    if value is None:
        return None, None
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        start, end = value[0], value[1]
    else:
        return None, None

    def normalize(item: object, *, upper: bool) -> str | None:
        if item is None:
            return None
        text = str(item).strip()
        if text.isdigit() and len(text) == 4:
            return f"{text}-{'12-31' if upper else '01-01'}"
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
        return None

    return normalize(start, upper=False), normalize(end, upper=True)


class FinancialFactCompiler:
    """Deep module owning candidate resolution, validation and AI eligibility."""

    def compile_facts(
        self,
        subject: Company,
        filings: Sequence[FilingDocument],
        facts: Sequence[FinancialFact],
        *,
        reporting_currency: str = "",
        required_concepts: frozenset[str] | None = None,
    ) -> FinancialDataset:
        """Re-compile already extracted facts through the canonical gate.

        This is the migration seam for retry/SEC compatibility paths.  It
        creates explicit evidence associations from each fact's own
        provenance; no caller is allowed to mark these facts accepted.
        """
        by_accession: dict[str, list[FactCandidate]] = {}
        for fact in facts:
            source_filing = next(
                (item for item in filings if item.accession_number == fact.accession_number),
                None,
            )
            ref = EvidenceRef(
                f"compiler:{fact.fact_id}",
                source_filing.document_id if source_filing is not None else "",
                fact.source_url,
                fact.source_document or fact.reported_concept,
                f"page:{fact.source_page or 0}",
                fact.raw_text,
                fact.filed_at,
                source_filing.content_hash if source_filing is not None else "",
                fact.source_bbox,
            )
            by_accession.setdefault(fact.accession_number, []).append(
                FactCandidate(fact, (ref,), "canonical-recompile")
            )
        batches = {
            filing.document_id: CandidateBatch(
                filing,
                tuple(by_accession.get(filing.accession_number, ())),
            )
            for filing in filings
        }
        ends = sorted({str(item.period_end)[:10] for item in filings if item.period_end})
        actual_range = (ends[0], ends[-1]) if ends else None
        return self.compile(
            subject,
            actual_range,
            CompilerPolicy(
                filings=tuple(filings),
                extractors=(InMemoryFactExtractor(batches),),
                required_concepts=required_concepts,
                reporting_currency=reporting_currency or subject.reporting_currency,
                period_range=actual_range,
            ),
        )

    def compile_from_ingestion(
        self,
        subject: Company,
        filings: Sequence[FilingDocument],
        engine: Any,
        *,
        structured_sources: Sequence[Any] = (),
        vision_fallback: Any = None,
        vision_config: Any = None,
        cancel_check: Any = None,
        progress: Any = None,
        reporting_currency: str = "",
        same_year_sources: Sequence[Any] = (),
        same_year_extractors: Sequence[FactExtractor] = (),
    ) -> FinancialDataset:
        """Collect all source candidates once, then apply one quality gate.

        ``same_year_sources`` is deliberately separate from the primary
        structured feed: it is an explicit, ordered gap stage and can only
        contribute candidates after the primary structured and PDF AST
        stages.  Callers that provide extractors directly must declare the
        same ``SAME_YEAR_OFFICIAL`` stage kind; names are not inspected.
        """

        collection = engine.collect_candidate_batches(
            subject,
            filings,
            structured_sources=structured_sources,
            cancel_check=cancel_check,
            progress=progress,
        )
        # The collector always places structured first and PDF second.  An
        # empty structured batch is intentional: it makes PDF a deterministic
        # fallback while ensuring every structured adapter was queried.
        pdf = {
            document_id: batches[1] if len(batches) > 1 else CandidateBatch(filing)
            for document_id, batches in collection.batches_by_document.items()
            for filing in filings if filing.document_id == document_id
        }
        primary = {
            document_id: batches[0] if batches else CandidateBatch(filing)
            for document_id, batches in collection.batches_by_document.items()
            for filing in filings if filing.document_id == document_id
        }
        diagnostics = tuple(collection.diagnostics)
        extractors: list[Any] = [
            MappedCandidateExtractor(primary, GapStageKind.OFFICIAL_STRUCTURED, "official-structured", diagnostics),
            MappedCandidateExtractor(pdf, GapStageKind.PDF_AST, "financial-ingestion-ast"),
        ]
        for extractor in same_year_extractors:
            try:
                stage_kind = GapStageKind(getattr(extractor, "stage_kind"))
            except (TypeError, ValueError):
                raise ValueError("same_year_extractor_stage_kind_required")
            if stage_kind is not GapStageKind.SAME_YEAR_OFFICIAL:
                raise ValueError("same_year_extractor_stage_kind_required")
            extractors.append(extractor)
        for source in same_year_sources:
            extractors.append(
                StructuredFactExtractor(
                    source,
                    name="same-year-official",
                    stage_kind=GapStageKind.SAME_YEAR_OFFICIAL,
                )
            )
        ends = sorted({str(item.period_end)[:10] for item in filings if item.period_end})
        actual_range = (ends[0], ends[-1]) if ends else None
        declared_periods = {
            str(item.fiscal_period or "FY").strip().upper() for item in filings
        }
        if len(declared_periods) == 1:
            target_fiscal_period = next(iter(declared_periods))
        elif filings:
            latest = max(
                filings,
                key=lambda item: (str(item.period_end)[:10], str(item.filed_at)),
            )
            target_fiscal_period = str(
                latest.fiscal_period or "FY"
            ).strip().upper()
        else:
            target_fiscal_period = "FY"
        policy_kwargs = dict(
            filings=tuple(filings),
            extractors=tuple(extractors),
            reporting_currency=reporting_currency or subject.reporting_currency,
            period_range=actual_range,
            fiscal_period=target_fiscal_period,
        )
        total_filings = len(filings)
        for index, filing in enumerate(filings, start=1):
            _emit_filing_progress(
                progress,
                "filing-validation",
                index,
                total_filings,
                filing,
                status="local-validating",
            )
        # Resolve structured/PDF/same-year candidates first.  A local
        # success must never trigger a network vision call, while an
        # incomplete target group must validate the session-only vision
        # configuration before its extractor is even constructed.
        for index, filing in enumerate(filings, start=1):
            _emit_filing_progress(
                progress,
                "filing-validation",
                index,
                total_filings,
                filing,
                status="canonical-compiling",
            )
        dataset = self.compile(
            subject,
            actual_range,
            CompilerPolicy(**policy_kwargs),
        )
        if (
            not dataset.allow_ai
            and vision_fallback is not None
            and vision_config is not None
            and bool(getattr(vision_config, "enabled", False))
        ):
            validate = getattr(vision_config, "validate", None)
            if not callable(validate):
                raise ValueError("vision_config_validation_required")
            validate()
            validation_issues_by_document: dict[str, tuple[str, ...]] = {}
            existing_facts_by_document: dict[str, tuple[FinancialFact, ...]] = {}
            visual_filings: list[FilingDocument] = []
            for filing in filings:
                accession = filing.accession_number
                groups = tuple(
                    item for item in dataset.validations
                    if tuple(getattr(item, "identity", ()))
                    and item.identity[0] == accession
                )
                validation_issues_by_document[filing.document_id] = tuple(
                    issue for item in groups for issue in getattr(item, "issues", ())
                )
                existing_facts_by_document[filing.document_id] = tuple(
                    fact for fact in dataset.resolved_facts
                    if fact.accession_number == accession
                )
                if not groups or any(
                    getattr(item.status, "value", item.status)
                    != ValidationStatus.VERIFIED.value
                    for item in groups
                ):
                    visual_filings.append(filing)
            vision_extractor = VisionCandidateExtractor(
                engine,
                vision_fallback,
                vision_config,
                cancel_check,
                validation_issues_by_document,
                existing_facts_by_document,
            )
            for index, filing in enumerate(visual_filings, start=1):
                _emit_filing_progress(
                    progress,
                    "vision-processing",
                    index,
                    len(visual_filings),
                    filing,
                    status="cloud-processing",
                )
            # Determine all filing gaps from the first canonical pass, then
            # perform authorized page extraction with two bounded workers. The
            # resulting candidate map is deterministic before the second,
            # shared compiler quality gate runs.
            vision_batches = _prefetch_vision_batches(
                subject,
                visual_filings,
                vision_extractor,
                timeout_seconds=float(getattr(vision_config, "timeout_seconds", 60.0)),
            )
            extractors.append(
                MappedCandidateExtractor(
                    vision_batches, GapStageKind.MINERU, "vision-fallback"
                )
            )
            dataset = self.compile(
                subject,
                actual_range,
                CompilerPolicy(
                    **{**policy_kwargs, "extractors": tuple(extractors)},
                    gap_user_authorized=bool(
                        getattr(vision_config, "enabled", False)
                        and getattr(vision_config, "consent", False)
                    ),
                ),
            )
        for index, filing in enumerate(filings, start=1):
            groups = tuple(
                item for item in dataset.validations
                if tuple(getattr(item, "identity", ()))
                and item.identity[0] == filing.accession_number
            )
            final_status = (
                "validated"
                if groups
                and all(
                    getattr(item.status, "value", item.status)
                    == ValidationStatus.VERIFIED.value
                    for item in groups
                )
                else "blocked"
            )
            _emit_filing_progress(
                progress,
                "filing-validation",
                index,
                total_filings,
                filing,
                status=final_status,
            )
        return replace(dataset, manifests=collection.manifests)

    def compile(
        self,
        subject: Company,
        period_range: object,
        policy: CompilerPolicy,
    ) -> FinancialDataset:
        filings = tuple(policy.filings)
        profile = CoveragePlanner().plan(subject)
        required_concepts = policy.required_concepts or profile.required_concepts
        target_fiscal_period = (policy.fiscal_period or "FY").strip().upper()
        target_scope = (policy.scope or "consolidated").strip().lower()
        target_currency = (
            policy.reporting_currency or subject.reporting_currency or ""
        ).strip().upper()
        range_start, range_end = _period_range_bounds(
            policy.period_range if policy.period_range is not None else period_range
        )
        if not filings and policy.filing_source is not None:
            filings = tuple(policy.filing_source.fetch(subject, period_range))
        extractors = policy.extractors
        if not extractors and any(filing.local_path for filing in filings):
            # Lazy import avoids the adapter/compiler cycle while making the
            # public compiler seam usable from a FilingDocument directly.
            from .financial_ast_adapter import PdfAstFactExtractor
            extractors = (PdfAstFactExtractor(),)
        diagnostics: list[str] = []
        candidates: list[FactCandidate] = []
        evidence: list[EvidenceRef] = []
        for filing in filings:
            if not extractors:
                diagnostics.append(f"{filing.document_id}:no_fact_extractor")
                continue
            # The first adapter is the primary source.  Remaining adapters are
            # gap stages and are invoked only for concepts absent from the
            # primary batch, so successful sources are never re-run.
            primary = extractors[:1]
            for extractor in primary:
                batch = extractor.extract(subject, filing)
                candidates.extend(batch.candidates)
                evidence.extend(batch.evidence)
                evidence.extend(ref for item in batch.candidates for ref in item.evidence)
                diagnostics.extend(
                    f"{filing.document_id}:{message}" for message in batch.diagnostics
                )
            primary_concepts = {
                item.fact.concept for item in candidates
                if item.fact.accession_number == filing.accession_number
            }
            missing = {
                concept for concept in required_concepts
                if not concepts_cover_profile(primary_concepts, (concept,))
            }
            gap_stages = policy.gap_extractors or extractors[1:]
            if missing and gap_stages:
                gap = GapResolver().resolve(
                    subject,
                    filing,
                    tuple(sorted(missing)),
                    gap_stages,
                    existing_concepts=tuple(primary_concepts),
                    user_authorized=policy.gap_user_authorized,
                    max_attempts=policy.gap_max_attempts,
                )
                candidates.extend(gap.candidates)
                evidence.extend(gap.evidence)
                diagnostics.extend(f"{filing.document_id}:{message}" for message in gap.diagnostics)

        grouped: dict[tuple[str, str, str, str, str], list[FactCandidate]] = {}
        for item in candidates:
            fact = item.fact
            identity = (
                fact.accession_number,
                fact.end_date,
                (fact.fiscal_period or "FY").upper(),
                fact.consolidated_scope or fact.scope or "unknown",
                fact.currency or subject.reporting_currency,
            )
            grouped.setdefault(identity, []).append(item)

        engine = FinancialIngestionEngine()
        resolved: list[FinancialFact] = []
        quarantined: list[FinancialFact] = []
        validations: list[FactGroupValidation] = []
        conflicts: list[dict[str, Any]] = []
        selected_evidence: list[EvidenceRef] = []

        for identity, items in grouped.items():
            by_concept: dict[str, list[FactCandidate]] = {}
            for item in items:
                by_concept.setdefault(item.fact.concept, []).append(item)
            chosen: list[FactCandidate] = []
            group_quarantine: list[FinancialFact] = []
            for concept, options in by_concept.items():
                conflict_keys = {
                    (
                        Decimal(str(option.fact.value)),
                        option.fact.end_date,
                        (option.fact.fiscal_period or "FY").upper(),
                        option.fact.consolidated_scope or option.fact.scope or "unknown",
                        option.fact.currency or "",
                        str(option.fact.unit_scale),
                        option.fact.statement or "",
                    )
                    for option in options
                }
                if len(conflict_keys) > 1:
                    conflicts.append(
                        {
                            "identity": identity,
                            "concept": concept,
                            "fact_ids": tuple(option.fact.fact_id for option in options),
                            "reason": "conflicting_candidate_values",
                        }
                    )
                    group_quarantine.extend(option.fact for option in options)
                    continue
                # Same-value candidates are independently auditable, but one
                # canonical fact is resolved to prevent last-write-wins drift.
                chosen.append(sorted(options, key=lambda option: (option.extractor, option.fact.fact_id))[0])

            group_facts = [item.fact for item in chosen]
            refs: dict[str, EvidenceRef] = {
                item.fact.fact_id: ref
                for item in chosen
                for ref in item.evidence
            }
            validation = engine.validate_group(
                group_facts, identity, refs, required_concepts=required_concepts
            )
            covered = frozenset(fact.concept for fact in group_facts)
            missing = set(required_concepts) - set(covered)
            if "equity" in missing and "total_equity" in covered:
                missing.remove("equity")
            issues = list(validation.validation.issues)
            if missing:
                issues.append("required_profile_missing:" + ",".join(sorted(missing)))
            if group_quarantine:
                issues.append("candidate_conflict")
            # Coverage gaps are non-fatal to the sibling fields that passed
            # the same validator.  Keep those fields resolved for audit and
            # deterministic repair, while ``allow_ai`` remains false until
            # the profile is complete.  Structural/equation failures still
            # quarantine the entire group.
            accepted = tuple(group_facts) if not group_quarantine and not validation.validation.quarantined else ()
            rejected = tuple(group_quarantine) + tuple(validation.validation.quarantined)
            if accepted:
                status = validation.validation.status.value
                if missing:
                    status = "INCOMPLETE"
                resolved.extend(accepted)
                selected_evidence.extend(refs.values())
            else:
                status = "CONFLICTED" if group_quarantine else "INCOMPLETE" if missing else ValidationStatus.REJECTED.value
                quarantined.extend(rejected or tuple(group_facts))
            validations.append(
                FactGroupValidation(
                    identity, status, tuple(dict.fromkeys(issues)), covered,
                    accepted, rejected or (() if accepted else tuple(group_facts)),
                )
            )

        def target_identity(identity: tuple[str, str, str, str, str]) -> bool:
            _accession, end_date, fiscal_period, scope, currency = identity
            if fiscal_period.upper() != target_fiscal_period:
                return False
            if scope.strip().lower() != target_scope:
                return False
            if target_currency and currency.strip().upper() != target_currency:
                return False
            if range_start and end_date[:10] < range_start:
                return False
            if range_end and end_date[:10] > range_end:
                return False
            return True

        target_validations = tuple(
            item for item in validations if target_identity(item.identity)
        )
        target_complete = tuple(
            item for item in target_validations
            if item.status == ValidationStatus.VERIFIED.value
            and concepts_cover_profile(
                (fact.concept for fact in item.accepted), required_concepts
            )
        )
        expected_target_filings = {
            (filing.accession_number, str(filing.period_end)[:10], target_fiscal_period)
            for filing in filings
            if filing.accession_number
            and (not filing.fiscal_period or filing.fiscal_period.upper() == target_fiscal_period)
            and (not range_start or str(filing.period_end)[:10] >= range_start)
            and (not range_end or str(filing.period_end)[:10] <= range_end)
        }
        complete_target_keys = {
            (item.identity[0], item.identity[1][:10], item.identity[2].upper())
            for item in target_complete
        }
        target_groups_complete = bool(target_complete) and (
            not expected_target_filings
            or expected_target_filings.issubset(complete_target_keys)
        )
        research_facts = tuple(
            fact for item in target_complete for fact in item.accepted
        )
        required = set(required_concepts)
        coverage = {
            "profile_id": profile.profile_id,
            "profile_reason": profile.reason,
            "required_concepts": tuple(sorted(required)),
            "resolved_concepts": tuple(sorted({fact.concept for fact in resolved})),
            "complete_groups": sum(1 for item in validations if item.status == ValidationStatus.VERIFIED.value),
            "total_groups": len(validations),
            "target_fiscal_period": target_fiscal_period,
            "target_scope": target_scope,
            "target_currency": target_currency,
            "target_period_range": (range_start, range_end),
            "target_group_count": len(target_validations),
            "target_complete_group_count": len(target_complete),
        }
        allow_ai = target_groups_complete and all(
            item.status == ValidationStatus.VERIFIED.value for item in target_validations
        )
        if not allow_ai:
            diagnostics.append("compiler_quality_gate_failed")
        # Preserve every source ref for audit, while deduplicating IDs.
        unique_refs: dict[str, EvidenceRef] = {}
        for ref in (*evidence, *selected_evidence):
            unique_refs.setdefault(ref.evidence_id, ref)
        return FinancialDataset(
            filings, tuple(resolved), tuple(quarantined), tuple(unique_refs.values()),
            tuple(conflicts), tuple(validations), coverage,
            tuple(dict.fromkeys(diagnostics)), allow_ai,
            research_facts, target_validations,
        )
