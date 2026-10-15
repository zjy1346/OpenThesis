"""Financial source collection and orchestration, separate from compilation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
from concurrent.futures import CancelledError
import inspect
import time
from typing import Any, Callable, Sequence

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .financial_contracts import (
    CandidateBatch, FactCandidate, FilingSource, FactExtractor, CompilationPolicy,
    FinancialDataset, GapStageKind, ValidationStatus, candidate_batches_from_facts, STATEMENT_FOR,
)
from .financial_compiler import FinancialFactCompiler, CoveragePlanner, concepts_cover_profile
from .financial_admission import FinancialAdmission, FinancialResearchView
from .financial_sources import (
    GapResolver, MappedCandidateExtractor, StructuredFactExtractor,
    VisionCandidateExtractor, _prefetch_vision_batches,
)
from .vision_financials import VisionAdapterError
from .financial_sources.legacy import LegacyFinancialSource


@dataclass(frozen=True, slots=True)
class CollectionPolicy:
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


def _disclosed_optional_concepts(subject, filing, candidates) -> frozenset[str]:
    """Observed current original cells, not a wish-list of absent fields."""
    wanted = CoveragePlanner().plan(subject).optional_concepts | frozenset({"parent_net_income", "total_equity"})
    identities = {subject.cik, subject.security_id}
    return frozenset(candidate.fact.concept for candidate in candidates
        if candidate.fact.concept in wanted and candidate.fact.company_cik in identities
        and candidate.fact.accession_number == filing.accession_number
        and candidate.fact.end_date == filing.period_end
        and candidate.fact.fiscal_period == filing.fiscal_period
        and (candidate.fact.consolidated_scope or candidate.fact.scope) == "consolidated"
        and candidate.fact.usage_status != "comparator"
        and any(ref.document_id == filing.document_id and ref.content_hash == filing.content_hash
                and bool(ref.content_hash) and ref.source_url == filing.source_url
                and ref.excerpt == candidate.fact.raw_text and ref.locator == f"page:{candidate.fact.source_page}"
                for ref in candidate.evidence))


@dataclass(frozen=True, slots=True)
class FinancialResearchSnapshot:
    """Frozen inputs for one collection, compilation, and admission attempt."""

    subject: Company
    filings: tuple[FilingDocument, ...]
    candidate_sources: tuple[Any, ...] = ()
    period_range: object | None = None
    authorized_source_stages: frozenset[GapStageKind] = frozenset()
    same_year_sources: tuple[Any, ...] = ()
    same_year_extractors: tuple[FactExtractor, ...] = ()
    vision_fallback: Any = None
    vision_config: Any = None
    requested_annual_count: int | None = None
    research_as_of: str | None = None
    cancel_check: Any = None
    progress: Any = None
    frozen_candidate_batches: tuple[CandidateBatch, ...] | None = None
    source_manifests: tuple[Any, ...] = ()
    source_diagnostics: tuple[str, ...] = ()
    source_failures: tuple[Any, ...] = ()
    parsing_observations: tuple[dict[str, Any], ...] = ()
    task_deadline: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject", replace(self.subject))
        object.__setattr__(self, "filings", tuple(replace(item) for item in self.filings))
        object.__setattr__(self, "candidate_sources", tuple(self.candidate_sources))
        object.__setattr__(self, "authorized_source_stages", frozenset(
            GapStageKind(item) for item in self.authorized_source_stages
        ))
        object.__setattr__(self, "same_year_sources", tuple(self.same_year_sources))
        object.__setattr__(self, "same_year_extractors", tuple(self.same_year_extractors))
        object.__setattr__(self, "source_manifests", tuple(self.source_manifests))
        object.__setattr__(self, "source_diagnostics", tuple(self.source_diagnostics))
        object.__setattr__(self, "source_failures", tuple(self.source_failures))
        object.__setattr__(self, "parsing_observations", deepcopy(tuple(self.parsing_observations)))
        if self.frozen_candidate_batches is not None:
            object.__setattr__(
                self, "frozen_candidate_batches", deepcopy(tuple(self.frozen_candidate_batches))
            )

    @classmethod
    def from_facts(
        cls,
        subject: Company,
        filings: Sequence[FilingDocument],
        facts: Sequence[FinancialFact],
        *,
        evidence: Sequence[EvidenceRef] = (),
        **snapshot_fields: Any,
    ) -> "FinancialResearchSnapshot":
        """Freeze persisted facts as candidates without manufacturing evidence."""

        batches = candidate_batches_from_facts(filings, facts, evidence)
        return cls(
            subject=subject,
            filings=tuple(filings),
            frozen_candidate_batches=batches,
            **snapshot_fields,
        )

    @classmethod
    def from_canonical(
        cls,
        subject: Company,
        filings: Sequence[FilingDocument],
        canonical: Any,
        **snapshot_fields: Any,
    ) -> "FinancialResearchSnapshot":
        """Re-enter a cached canonical projection as candidates for revalidation."""

        resolved = getattr(canonical, "resolved_facts", None)
        accepted = getattr(canonical, "accepted_facts", None)
        if resolved is None or (not resolved and accepted):
            resolved = accepted or ()
        facts = tuple(resolved or ()) + tuple(
            getattr(canonical, "quarantined_facts", ()) or ()
        )
        evidence = tuple(getattr(canonical, "evidence", ()) or ())
        manifests = snapshot_fields.pop("source_manifests", None)
        if not manifests:
            manifests = getattr(canonical, "manifests", None)
        if not manifests:
            manifests = getattr(canonical, "manifest", ())
        # Recompilation may add failures, never erase the source's existing
        # failure fences. Deduplicate stable typed records without dropping
        # siblings or replacing them with display-only diagnostic strings.
        failures = tuple(dict.fromkeys((
            *getattr(canonical, "source_failures", ()),
            *snapshot_fields.pop("source_failures", ()),
        )))
        return cls.from_facts(
            subject, filings, facts, evidence=evidence,
            source_manifests=tuple(manifests or ()),
            source_failures=failures, **snapshot_fields
        )

def _emit_filing_progress(
    progress: Any,
    stage: str,
    current: int,
    total: int,
    filing: FilingDocument,
    *,
    status: str,
    elapsed_seconds: float = 0.0,
    error_code: str = "",
) -> None:
    """Emit stable filing states while retaining three-argument callbacks."""

    if progress is None:
        return
    detail = {
        "filing_id": filing.document_id,
        "label": filing.primary_document or filing.accession_number or filing.document_id,
        "status": status,
        "error_code": str(error_code),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
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

class FinancialCandidatePipeline:
    """One collector-to-compiler execution path for initial and repair inputs."""

    def __init__(self, compiler: FinancialFactCompiler | None = None):
        self.compiler = compiler or FinancialFactCompiler()

    def compile_from_ingestion(
        self,
        subject: Company,
        filings: Sequence[FilingDocument],
        engine: Any = None,
        *,
        structured_sources: Sequence[Any] = (),
        vision_fallback: Any = None,
        vision_config: Any = None,
        cancel_check: Any = None,
        progress: Any = None,
        reporting_currency: str = "",
        same_year_sources: Sequence[Any] = (),
        same_year_extractors: Sequence[FactExtractor] = (),
        compilation_policy: CompilationPolicy | None = None,
        deadline: float | None = None,
    ) -> FinancialDataset:
        """Collect all source candidates once, then apply one quality gate.

        ``same_year_sources`` is deliberately separate from the primary
        structured feed: it is an explicit, ordered gap stage and can only
        contribute candidates after the primary structured and PDF AST
        stages.  Callers that provide extractors directly must declare the
        same ``SAME_YEAR_OFFICIAL`` stage kind; names are not inspected.
        """

        _raise_if_cancelled(cancel_check)
        # One absolute task budget starts before collection. Local hashes,
        # indexes, windows and authorized cloud preparation consume it; a new
        # phase must not reset the allowance after local work has exhausted it.
        task_deadline = min(deadline, time.monotonic() + float(getattr(engine, "_batch_timeout_seconds", 280.0))) if deadline is not None else time.monotonic() + float(getattr(engine, "_batch_timeout_seconds", 280.0))
        collector = engine if callable(getattr(engine, "collect_candidate_batches", None)) else LegacyFinancialSource(engine)
        collection_options = dict(structured_sources=structured_sources,
                                  cancel_check=cancel_check, progress=progress)
        parameters = inspect.signature(collector.collect_candidate_batches).parameters
        if "deadline" in parameters or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
            collection_options["deadline"] = task_deadline
        collection = collector.collect_candidate_batches(
            subject,
            filings,
            **collection_options,
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
        extractors: list[Any] = [
            MappedCandidateExtractor(primary, GapStageKind.OFFICIAL_STRUCTURED, "official-structured"),
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
        target_fiscal_period = _target_fiscal_period(filings)
        compiler_policy = compilation_policy or CompilationPolicy()
        policy_kwargs = dict(
            filings=tuple(filings),
            extractors=tuple(extractors),
            required_concepts=compiler_policy.required_concepts,
            reporting_currency=(
                compiler_policy.reporting_currency
                or reporting_currency
                or subject.reporting_currency
            ),
            period_range=(
                compiler_policy.period_range
                if compiler_policy.period_range is not None else actual_range
            ),
            fiscal_period=(
                compiler_policy.fiscal_period
                if compiler_policy.fiscal_period
                and compiler_policy.fiscal_period.upper() != "FY"
                else target_fiscal_period
            ),
            scope=compiler_policy.scope or "consolidated",
        )
        total_filings = len(filings)
        filing_started_at = {
            filing.document_id: time.monotonic() for filing in filings
        }
        for index, filing in enumerate(filings, start=1):
            _emit_filing_progress(
                progress,
                "filing-validation",
                index,
                total_filings,
                filing,
                status="local-validating",
                elapsed_seconds=time.monotonic() - filing_started_at[filing.document_id],
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
                elapsed_seconds=time.monotonic() - filing_started_at[filing.document_id],
            )
        dataset = self.compile(
            subject,
            actual_range,
            CollectionPolicy(**policy_kwargs),
        )

        def finalize(current: FinancialDataset, extra: Sequence[str] = ()) -> FinancialDataset:
            _raise_if_cancelled(cancel_check)
            if extra:
                current = _append_dataset_diagnostics(current, extra)
            for index, filing in enumerate(filings, start=1):
                groups = tuple(
                    item for item in current.research_validations
                    if tuple(getattr(item, "identity", ()))
                    and item.identity[0] == filing.accession_number
                    and getattr(item, "role", "target") == "target"
                )
                final_status = (
                    "validated"
                    if groups
                    and any(
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
                    elapsed_seconds=time.monotonic() - filing_started_at[filing.document_id],
                    error_code=(
                        "FILING_VALIDATION_BLOCKED" if final_status == "blocked" else ""
                    ),
                )
            return replace(
                current,
                manifests=collection.manifests,
                diagnostics=tuple(dict.fromkeys((*current.diagnostics, *collection.diagnostics))),
                source_failures=collection.source_failures,
            )

        vision_diagnostics: tuple[str, ...] = ()
        optional_gaps = {}
        for filing in filings:
            observed = _disclosed_optional_concepts(subject, filing, tuple(
                candidate for batch in collection.batches_by_document.get(filing.document_id, ())
                for candidate in batch.candidates))
            # Independently proven source cells outside the selected display
            # cohort/currency remain valid observations; selecting a research
            # lane is not an extraction failure or permission to upload again.
            accepted = {fact.concept for group in dataset.validations for fact in group.accepted
                        if fact.accession_number == filing.accession_number and fact.end_date == filing.period_end
                        and fact.company_cik == filing.company_cik
                        and (fact.consolidated_scope or fact.scope) == "consolidated"}
            missing = observed - accepted
            if missing:
                optional_gaps[filing.document_id] = missing
        if (
            (not dataset.allow_ai or optional_gaps)
            and vision_config is not None
            and bool(getattr(vision_config, "enabled", False))
        ):
            if vision_fallback is None:
                return finalize(dataset, ("VISION_ADAPTER_UNAVAILABLE",))
            validate = getattr(vision_config, "validate", None)
            if not callable(validate):
                return finalize(
                    dataset, ("VISION_CONFIG_VALIDATION_REQUIRED",)
                )
            try:
                validate()
            except VisionAdapterError as exc:
                if exc.code == "VISION_CANCELLED":
                    raise CancelledError("vision candidate extraction cancelled") from exc
                return finalize(dataset, (exc.code,))
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
                ) + tuple(f"missing_disclosed_optional:{concept}:{STATEMENT_FOR[concept]}"
                          for concept in sorted(optional_gaps.get(filing.document_id, ())))
                existing_facts_by_document[filing.document_id] = tuple(
                    fact for fact in dataset.resolved_facts
                    if fact.accession_number == accession
                )
                # Group status also reports quarantined foreign/optional
                # observations. It is not itself a request to upload a page.
                # Only a concrete uncovered required role or original-bound
                # optional cell can justify another extraction attempt.
                current_concepts = {fact.concept for item in groups for fact in item.accepted
                    if fact.end_date == filing.period_end
                    and fact.fiscal_period == filing.fiscal_period
                    and (fact.consolidated_scope or fact.scope) == (compiler_policy.scope or "consolidated")
                    and fact.company_cik == filing.company_cik}
                required = compiler_policy.required_concepts or CoveragePlanner().plan(subject).required_concepts
                if filing.document_id in optional_gaps or not concepts_cover_profile(current_concepts, required):
                    visual_filings.append(filing)
            if not visual_filings:
                return finalize(dataset, vision_diagnostics)
            vision_extractor = VisionCandidateExtractor(
                engine,
                vision_fallback,
                vision_config,
                cancel_check,
                validation_issues_by_document,
                existing_facts_by_document,
                period_header_pages_by_document={
                    document_id: tuple(sorted({int(ref.locator.removeprefix("page:"))
                        for ref in batch.evidence if ref.evidence_id.startswith("period-gap:")
                        and ref.locator.startswith("page:") and ref.locator.removeprefix("page:").isdigit()
                        and ref.content_hash == batch.filing.content_hash}))
                    for document_id, batch in pdf.items()
                },
                deadline=task_deadline,
            )
            for index, filing in enumerate(visual_filings, start=1):
                _emit_filing_progress(
                    progress,
                    "vision-processing",
                    index,
                    len(visual_filings),
                    filing,
                    status="cloud-processing",
                    elapsed_seconds=time.monotonic() - filing_started_at[filing.document_id],
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
                deadline=task_deadline,
            )
            vision_diagnostics = tuple(
                f"{document_id}:{diagnostic}"
                for document_id, batch in vision_batches.items()
                for diagnostic in batch.diagnostics
            )
            if not any(batch.candidates for batch in vision_batches.values()):
                return finalize(dataset, vision_diagnostics)
            extractors.append(
                MappedCandidateExtractor(
                    vision_batches, GapStageKind.MINERU, "vision-fallback"
                )
            )
            dataset = self.compile(
                subject,
                actual_range,
                CollectionPolicy(
                    **{**policy_kwargs, "extractors": tuple(extractors)},
                    gap_user_authorized=bool(
                        getattr(vision_config, "enabled", False)
                        and getattr(vision_config, "consent", False)
                    ),
                ),
            )
        return finalize(dataset, vision_diagnostics)

    def compile(self, subject: Company, period_range: object, policy: CollectionPolicy) -> FinancialDataset:
        """Collect candidate observations, then use the only acceptance gate."""
        filings = tuple(policy.filings)
        profile = CoveragePlanner().plan(subject)
        required_concepts = policy.required_concepts or profile.required_concepts
        diagnostics: list[str] = []
        candidates: list[FactCandidate] = []
        evidence: list[EvidenceRef] = []
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
            primary_candidates: list[FactCandidate] = []
            primary_evidence: list[EvidenceRef] = []
            for extractor in primary:
                batch = extractor.extract(subject, filing)
                candidates.extend(batch.candidates)
                primary_candidates.extend(batch.candidates)
                evidence.extend(batch.evidence)
                primary_evidence.extend(batch.evidence)
                evidence.extend(ref for item in batch.candidates for ref in item.evidence)
                primary_evidence.extend(ref for item in batch.candidates for ref in item.evidence)
                diagnostics.extend(
                    f"{filing.document_id}:{message}" for message in batch.diagnostics
                )
            # Precollected local AST is not a network fallback. Even a complete
            # structured core can lack capex/operating profit or comparative
            # rows already present here. Do not throw those cells away or
            # reparse them later just because the mandatory core is complete.
            gap_stages = list(policy.gap_extractors or extractors[1:])
            local_stages = [item for item in gap_stages
                            if isinstance(item, MappedCandidateExtractor)
                            and getattr(item, "stage_kind", None) == GapStageKind.PDF_AST]
            for extractor in local_stages:
                batch = extractor.extract(subject, filing)
                candidates.extend(batch.candidates)
                primary_candidates.extend(batch.candidates)
                source_refs = tuple((*batch.evidence, *(ref for item in batch.candidates for ref in item.evidence)))
                evidence.extend(source_refs)
                primary_evidence.extend(source_refs)
                diagnostics.extend(f"{filing.document_id}:{message}" for message in batch.diagnostics)
            gap_stages = tuple(item for item in gap_stages if item not in local_stages)
            target_currency = (
                policy.reporting_currency or subject.reporting_currency or ""
            ).strip().upper()
            target_period = (filing.fiscal_period or policy.fiscal_period or "FY").upper()
            target_scope = (policy.scope or "consolidated").strip().casefold()
            canonical_gate = isinstance(self.compiler, FinancialFactCompiler)

            def verified_for(
                candidate_items: Sequence[FactCandidate],
                candidate_evidence: Sequence[EvidenceRef],
            ) -> frozenset[str]:
                """Recompile the frozen filing batch before declaring coverage."""
                # Production always uses FinancialFactCompiler.  A few
                # compatibility callers inject a recording compiler whose
                # compile result is intentionally a fixed fixture and cannot
                # provide the canonical research view needed for gap
                # decisions; leave those callers on their single final
                # compile pass rather than treating their audit fixture as a
                # quality gate.
                if not canonical_gate:
                    return frozenset()
                try:
                    result = self.compiler.compile(
                        subject,
                        (CandidateBatch(filing, tuple(candidate_items), tuple(candidate_evidence)),),
                        CompilationPolicy(
                            filings=(filing,),
                            required_concepts=frozenset(required_concepts),
                            reporting_currency=target_currency,
                            fiscal_period=target_period,
                            scope=target_scope,
                            period_range=(filing.period_end, filing.period_end),
                        ),
                    )
                except Exception as exc:
                    diagnostics.append(
                        f"{filing.document_id}:verified_coverage_compile_failed:{type(exc).__name__}"
                    )
                    return frozenset()
                target_end = str(filing.period_end or "")[:10]
                # ``resolved_facts`` is an audit projection and may include
                # non-canonical siblings.  Gap coverage must be based only on
                # the compiler's admitted research view.
                return frozenset(
                    fact.concept
                    for fact in getattr(result, "research_facts", ())
                    if fact.accession_number == filing.accession_number
                    and str(fact.end_date or "")[:10] == target_end
                    and str(fact.fiscal_period or "FY").upper() == target_period
                    and str(fact.consolidated_scope or fact.scope or "").casefold() == target_scope
                    and (not target_currency or str(fact.currency or "").upper() == target_currency)
                )

            primary_refs = tuple(
                {ref.evidence_id: ref for ref in primary_evidence}.values()
            )
            primary_concepts = verified_for(primary_candidates, primary_refs)
            missing = {
                concept for concept in required_concepts
                if not concepts_cover_profile(primary_concepts, (concept,))
            }
            missing.update(_disclosed_optional_concepts(subject, filing, primary_candidates) - primary_concepts)
            if missing and gap_stages:
                gap = GapResolver().resolve(
                    subject,
                    filing,
                    tuple(sorted(missing)),
                    gap_stages,
                    existing_concepts=tuple(primary_concepts),
                    existing_candidates=tuple(primary_candidates),
                    verified_concepts=verified_for,
                    user_authorized=policy.gap_user_authorized,
                    max_attempts=policy.gap_max_attempts,
                )
                candidates.extend(gap.candidates)
                evidence.extend(gap.evidence)
                diagnostics.extend(f"{filing.document_id}:{message}" for message in gap.diagnostics)


        batches = tuple(CandidateBatch(
            filing,
            tuple(item for item in candidates if item.fact.accession_number == filing.accession_number),
            tuple(ref for ref in evidence if ref.document_id == filing.document_id),
            tuple(diagnostics) if index == 0 else (),
        ) for index, filing in enumerate(filings))
        return self.compiler.compile(subject, batches, CompilationPolicy(
            filings=filings, required_concepts=policy.required_concepts,
            reporting_currency=policy.reporting_currency, fiscal_period=policy.fiscal_period,
            scope=policy.scope,
            period_range=policy.period_range if policy.period_range is not None else period_range,
        ))

    def compile_frozen(
        self,
        subject: Company,
        batches: Sequence[CandidateBatch],
        policy: CompilationPolicy,
    ) -> FinancialDataset:
        """Validate already-frozen batches without calling any source adapter."""

        return self.compiler.compile(subject, tuple(batches), policy)


class FinancialPipeline:
    """Public prepare boundary composing source collection and pure compiler."""

    def __init__(
        self,
        engine: Any,
        compiler: FinancialFactCompiler | None = None,
        *,
        candidate_pipeline: FinancialCandidatePipeline | None = None,
        admission: FinancialAdmission | None = None,
        engine_for: Callable[
            [Company, Sequence[FilingDocument]], Any
        ] | None = None,
    ) -> None:
        self.engine = engine
        self.engine_for = engine_for
        self.candidates = candidate_pipeline or FinancialCandidatePipeline(compiler)
        self.admission = admission or FinancialAdmission()

    def prepare(
        self,
        snapshot: FinancialResearchSnapshot,
        policy: CompilationPolicy,
    ) -> FinancialResearchView:
        """Collect from frozen inputs, compile once, then admit one canonical view."""

        if not isinstance(snapshot, FinancialResearchSnapshot):
            raise TypeError("FROZEN_FINANCIAL_RESEARCH_SNAPSHOT_REQUIRED")
        if not isinstance(policy, CompilationPolicy):
            raise TypeError("COMPILATION_POLICY_REQUIRED")
        if (snapshot.same_year_sources or snapshot.same_year_extractors) and (
            GapStageKind.SAME_YEAR_OFFICIAL not in snapshot.authorized_source_stages
        ):
            raise ValueError("SAME_YEAR_SOURCE_NOT_AUTHORIZED")
        vision_enabled = bool(getattr(snapshot.vision_config, "enabled", False))
        if vision_enabled and (
            GapStageKind.MINERU not in snapshot.authorized_source_stages
        ):
            raise ValueError("MINERU_SOURCE_NOT_AUTHORIZED")

        _raise_if_cancelled(snapshot.cancel_check)

        frozen_policy = replace(
            policy,
            filings=snapshot.filings,
            fiscal_period=_effective_fiscal_period(
                snapshot.filings, policy.fiscal_period
            ),
            period_range=(
                snapshot.period_range
                if snapshot.period_range is not None else policy.period_range
            ),
        )
        if snapshot.frozen_candidate_batches is None:
            run_engine = (
                self.engine_for(snapshot.subject, snapshot.filings)
                if self.engine_for is not None else self.engine
            )
            if run_engine is None:
                raise ValueError("FINANCIAL_SOURCE_ENGINE_REQUIRED")
            dataset = self.candidates.compile_from_ingestion(
                snapshot.subject,
                snapshot.filings,
                run_engine,
                structured_sources=snapshot.candidate_sources,
                vision_fallback=snapshot.vision_fallback,
                vision_config=snapshot.vision_config,
                same_year_sources=snapshot.same_year_sources,
                same_year_extractors=snapshot.same_year_extractors,
                cancel_check=snapshot.cancel_check,
                progress=snapshot.progress,
                reporting_currency=frozen_policy.reporting_currency,
                compilation_policy=frozen_policy,
                deadline=snapshot.task_deadline,
            )
        else:
            if (
                snapshot.candidate_sources or snapshot.same_year_sources
                or snapshot.same_year_extractors or snapshot.vision_fallback is not None
                or snapshot.vision_config is not None
            ):
                raise ValueError("FROZEN_CANDIDATES_CANNOT_MIX_SOURCE_ADAPTERS")
            dataset = self.candidates.compile_frozen(
                snapshot.subject,
                snapshot.frozen_candidate_batches,
                frozen_policy,
            )
            if snapshot.source_manifests and not dataset.manifests:
                dataset = replace(dataset, manifests=snapshot.source_manifests)
        _raise_if_cancelled(snapshot.cancel_check)
        if snapshot.source_diagnostics:
            dataset = _append_dataset_diagnostics(dataset, snapshot.source_diagnostics)
        if snapshot.source_failures:
            dataset = replace(dataset, source_failures=(*dataset.source_failures, *snapshot.source_failures))
        view = self.admission.admit(
            snapshot.subject,
            dataset,
            selected_filings=snapshot.filings,
            manifests=dataset.manifests or snapshot.source_manifests,
            evidence=dataset.evidence,
            requested_annual_count=snapshot.requested_annual_count,
            research_as_of=snapshot.research_as_of,
            parsing_observations=snapshot.parsing_observations,
        )
        return replace(
            view, canonical_dataset=dataset,
            publishable_accessions=_publishable_accessions(snapshot, dataset, view),
        )


def disclosed_reporting_currency(
    subject: Company, dataset: FinancialDataset, filings: Sequence[FilingDocument],
) -> tuple[str, bool]:
    """Resolve a metadata currency only from complete, verified annual groups.

    A translated note/currency lane is not a mandate to convert or replace
    the preferred lane. Ambiguity stays explicit; no amounts are changed.
    """
    annual_ends = [str(item.period_end)[:10] for item in filings
                   if str(item.fiscal_period).upper() == "FY" and item.period_end]
    if not annual_ends:
        return subject.reporting_currency, False
    latest = max(annual_ends)
    required = CoveragePlanner().plan(subject).required_concepts
    currencies = {
        str(group.identity[4]).upper() for group in dataset.validations
        if str(group.identity[1])[:10] == latest
        and str(group.identity[2]).upper() == "FY"
        and str(group.identity[3]).casefold() == "consolidated"
        and group.role == "target" and group.status == ValidationStatus.VERIFIED.value
        and concepts_cover_profile((fact.concept for fact in group.accepted), required)
        and group.identity[4]
    }
    preferred = str(subject.reporting_currency).upper()
    if preferred in currencies or not currencies:
        return subject.reporting_currency, False
    if len(currencies) == 1:
        return next(iter(currencies)), False
    return subject.reporting_currency, True


def _publishable_accessions(
    snapshot: FinancialResearchSnapshot, dataset: FinancialDataset,
    view: FinancialResearchView,
) -> tuple[str, ...]:
    """Publish safe cache facts, not a claim of complete research coverage.

    Interrupted collection must not replace a previous complete cache. Missing
    desired fields remain coverage gaps even when safe sibling facts can be
    published. The independent compiler/admission decisions remain authority.
    """
    failures = (
        "pdf_window_incomplete", "pdf_window_failed", "pdf_window_exhausted",
        "pdf_parse_cancelled", "pdf_file_missing", "pdf_file_unreadable",
        "pdf_candidate_pages_unavailable", "cache_hash_mismatch", "legacy_source_failed",
    )
    admitted = {fact.accession_number for fact in view.facts}
    publishable = []
    for filing in snapshot.filings:
        if filing.accession_number not in admitted:
            continue
        if any(item.required and item.accession_number == filing.accession_number
               for item in dataset.source_failures):
            continue
        failed = False
        for diagnostic in dataset.diagnostics:
            text = str(diagnostic).casefold()
            if not any(marker in text for marker in failures):
                continue
            affected = next((item.accession_number for item in snapshot.filings
                             if str(diagnostic).startswith(item.document_id + ":")
                             or str(diagnostic).startswith(item.accession_number + ":")), None)
            # Unknown/global errors conservatively fence every publication.
            if affected is None or affected == filing.accession_number:
                failed = True
                break
        if not failed:
            publishable.append(filing.accession_number)
    return tuple(sorted(set(publishable)))


def _raise_if_cancelled(cancel_check: Any) -> None:
    if cancel_check is not None and cancel_check():
        raise CancelledError("financial candidate pipeline cancelled")


def _target_fiscal_period(filings: Sequence[FilingDocument]) -> str:
    declared_periods = {
        str(item.fiscal_period or "FY").strip().upper() for item in filings
    }
    if len(declared_periods) == 1:
        return next(iter(declared_periods))
    if not filings:
        return "FY"
    latest = max(
        filings,
        key=lambda item: (str(item.period_end)[:10], str(item.filed_at)),
    )
    return str(latest.fiscal_period or "FY").strip().upper()


def _effective_fiscal_period(
    filings: Sequence[FilingDocument], requested: str | None
) -> str:
    requested_period = str(requested or "FY").strip().upper()
    if requested_period and requested_period != "FY":
        return requested_period
    return _target_fiscal_period(filings)


def _append_dataset_diagnostics(
    dataset: FinancialDataset, diagnostics: Sequence[str]
) -> FinancialDataset:
    return replace(
        dataset,
        diagnostics=tuple(dict.fromkeys((*dataset.diagnostics, *diagnostics))),
    )
