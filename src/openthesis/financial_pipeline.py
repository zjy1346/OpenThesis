"""Financial source collection and orchestration, separate from compilation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
from concurrent.futures import CancelledError
import inspect
import json
import time
from typing import Any, Callable, Sequence

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .financial_contracts import (
    CandidateBatch, FactCandidate, FilingSource, FactExtractor, CompilationPolicy,
    FinancialDataset, FinancialSourceFailure, GapStageKind, ValidationStatus,
    candidate_batches_from_facts, STATEMENT_FOR,
)
from .financial_compiler import FinancialFactCompiler, CoveragePlanner, concepts_cover_profile
from .financial_admission import FinancialAdmission, FinancialResearchView, ReadinessLevel
from .financial_source_errors import PDF_RUNTIME_FAILURE_CODES
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
    phase_timings: dict[str, Any] | None = None


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
    outcome: dict[str, Any] | None = None,
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
        **(outcome or {}),
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


def document_processing_outcome(filing: FilingDocument, dataset: FinancialDataset,
                                collection: Any) -> dict[str, Any]:
    """Describe this original's contribution independently of latest targets.

    A candidate belongs to the captured document/hash, not merely its year.
    Admission and conflict records remain compiler-owned; this projection does
    not promote a fact or turn a historical document into a verified target.
    """
    batches = collection.batches_by_document.get(filing.document_id, ())
    bound_batches = [batch for batch in batches if batch.filing.document_id == filing.document_id
        and batch.filing.content_hash == filing.content_hash
        and batch.filing.accession_number == filing.accession_number]
    document_candidates = [item.fact for batch in bound_batches for item in batch.candidates]
    candidate_ids = {fact.fact_id for fact in document_candidates}
    bound_refs = {ref.evidence_id for ref in dataset.evidence
                  if ref.document_id == filing.document_id and ref.content_hash == filing.content_hash
                  and bool(filing.content_hash) and ref.source_url == filing.source_url}
    candidate_ids.update(fact.fact_id for fact in (*dataset.resolved_facts, *dataset.quarantined_facts)
                         if fact.accession_number == filing.accession_number and
                         any(ref in bound_refs for ref in (f"fact:{fact.fact_id}",
                             f"fact:{fact.fact_id.removeprefix('ingest:')}")))
    def contributes(group):
        if not group.identity or group.identity[0] != filing.accession_number:
            return False
        # Accession and period identify a compiler group, not the original
        # bytes. Only a fact explicitly tied to this document's bound candidate
        # set can make that group relevant to this filing.
        return bool(candidate_ids) and any(
            fact.fact_id in candidate_ids for fact in (*group.accepted, *group.quarantined)
        )
    groups = [item for item in (*dataset.validations, *dataset.research_validations) if contributes(item)]
    bound_groups = [
        group for group in groups
        if (group_facts := (*group.accepted, *group.quarantined))
        and all(fact.fact_id in candidate_ids for fact in group_facts)
    ]
    admitted_ids = {fact.fact_id for group in groups for fact in group.accepted
                    if fact.fact_id in candidate_ids}
    rejected_ids = {fact.fact_id for group in groups for fact in group.quarantined
                    if fact.fact_id in candidate_ids}
    # Validation issues and role are group-level compiler metadata. If the
    # group mixes this original with another source/hash, retain this filing's
    # fact admission but do not attribute aggregate metadata to either source.
    roles = sorted({group.role for group in bound_groups if group.role})
    failures = [item for item in (*collection.source_failures, *dataset.source_failures)
                if item.document_id == filing.document_id and item.accession_number == filing.accession_number]
    def document_issues(group):
        observed = [fact for fact in (*group.accepted, *group.quarantined) if fact.fact_id in candidate_ids]
        supplemental = (group.role != "target" or group.identity[1] < filing.period_end) and observed and all(
            fact.statement not in {"income_statement", "balance_sheet", "cash_flow"} for fact in observed)
        for code in group.issues:
            # A summary may legitimately disclose only a third-year ROE. Its
            # absence of full third-year statements is a planner coverage gap,
            # not a failed extraction of tables this original never presented.
            if supplemental and (str(code).endswith("_core_missing") or str(code).startswith("required_profile_missing:")
                                 or code in {"core_coverage_insufficient", "compiler_incomplete_profile"}):
                continue
            yield str(code)
    codes = sorted({code for group in bound_groups for code in document_issues(group)}
                   | {item.code for item in failures}
                   | {str(code) for batch in bound_batches for code in batch.diagnostics})
    partial = bool(rejected_ids or failures or codes)
    historical = bool(roles) and "target" not in roles
    if admitted_ids:
        availability = "partial" if partial else "complete"
        status = "partial" if partial else "historical" if historical else "validated"
    else:
        availability = "none"
        status = "needs-action"
    execution_failed = any(item.required and item.stage in {"download", "parse", "worker", "index"} for item in failures)
    return {"status": status, "processing_state": "failed" if execution_failed else "completed",
            "availability": availability, "roles": roles,
            "candidate_count": len(candidate_ids), "admitted_count": len(admitted_ids),
            "quarantined_count": len(rejected_ids), "issue_codes": codes,
            "recognized_statements": sorted({fact.statement for group in groups
                for fact in group.accepted if fact.fact_id in admitted_ids and fact.statement}),
            "extracted_pages": sorted({fact.source_page for group in groups
                for fact in group.accepted if fact.fact_id in admitted_ids and fact.source_page}),
            "source_hash": filing.content_hash, "accession_number": filing.accession_number,
            "period_end": filing.period_end, "fiscal_period": filing.fiscal_period,
            "error_code": codes[0] if codes else "" if admitted_ids else "FILING_NO_ADMITTED_UNITS"}

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
        # The engine's batch timeout is a resumable scheduling slice, not an
        # implicit whole-task limit. Only the captured caller deadline ends an
        # adaptive collection; all local/cloud phases share that same value.
        # Otherwise the outer subscriber cancels healthy producers at 280 s
        # before their existing checkpoint-progress continuation can finish.
        task_deadline = deadline
        collector = engine if callable(getattr(engine, "collect_candidate_batches", None)) else LegacyFinancialSource(engine)
        collection_options = dict(structured_sources=structured_sources,
                                  cancel_check=cancel_check, progress=progress)
        parameters = inspect.signature(collector.collect_candidate_batches).parameters
        if "deadline" in parameters or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
            collection_options["deadline"] = task_deadline
        collection_started = time.monotonic()
        collection = collector.collect_candidate_batches(
            subject,
            filings,
            **collection_options,
        )
        phase_timings: dict[str, Any] = {
            "source_collection_elapsed_seconds": round(time.monotonic() - collection_started, 4),
        }
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
        if getattr(engine, '_local_structure_root', None) is not None:
            from .local_structure import LocalStructureExtractor
            extractors.append(LocalStructureExtractor(engine._local_structure_root, engine, cancel_check, progress))
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
        candidate_compile_started = time.monotonic()
        dataset = self.compile(
            subject,
            actual_range,
            CollectionPolicy(**policy_kwargs, phase_timings=phase_timings),
        )
        phase_timings["candidate_mapping_elapsed_seconds"] = round(
            time.monotonic() - candidate_compile_started, 4,
        )
        diagnostic_engine = getattr(engine, "engine", engine)
        execution = getattr(diagnostic_engine, "_document_execution", {})
        if isinstance(execution, dict):
            per_filing = phase_timings.get("candidate_mapping_by_filing_seconds", {})
            for filing in filings:
                observation = execution.get(filing.document_id)
                if not isinstance(observation, dict) or observation.get("source_hash") != filing.content_hash:
                    continue
                observation["stage_timings"] = {
                    key: float(value)
                    for key, value in phase_timings.items()
                    if key != "candidate_mapping_by_filing_seconds"
                    and isinstance(value, (int, float)) and not isinstance(value, bool)
                }
                if isinstance(per_filing, dict) and filing.document_id in per_filing:
                    observation["stage_timings"]["candidate_mapping_seconds"] = float(per_filing[filing.document_id])

        vision_batches = {}
        def finalize(current: FinancialDataset, extra: Sequence[str] = ()) -> FinancialDataset:
            _raise_if_cancelled(cancel_check)
            if extra:
                current = _append_dataset_diagnostics(current, extra)
            # A failed native backend is not a failed original after an
            # independently compiled structure path fills its required roles.
            # Keep the exact native failure for audit, but revoke its active
            # publication fence only for this same hash and complete work.
            failures = []
            for failure in collection.source_failures:
                filing = next((item for item in filings if item.document_id == failure.document_id
                    and item.accession_number == failure.accession_number), None)
                observation = getattr(engine, '_document_execution', {}).get(failure.document_id,{})
                local = observation.get('local_structure',{})
                from .financial_source_errors import PDF_RUNTIME_FAILURE_CODES
                allowed = failure.stage == 'parse' and failure.code in (PDF_RUNTIME_FAILURE_CODES | {
                    'DOCUMENT_CONTEXT_OCR_REQUIRED','PDF_TEXT_EXTRACTION_FAILED','DOCUMENT_CONTEXT_TIMEOUT',
                    'PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION','PDFIUM_WORKER_TIMEOUT'})
                facts = tuple(fact for group in current.validations for fact in group.accepted
                    if filing is not None and fact.accession_number == filing.accession_number
                    and fact.end_date == filing.period_end and fact.fiscal_period == filing.fiscal_period
                    and fact.consolidated_scope == current.coverage.get('target_scope',policy_kwargs['scope'])
                    and fact.currency == current.coverage.get('target_currency',policy_kwargs['reporting_currency']))
                required = compiler_policy.required_concepts or CoveragePlanner().plan(subject).required_concepts
                refs = {ref.evidence_id:ref for ref in current.evidence}
                def structure_proven(fact):
                    for key in fact.classification_evidence_ids:
                        proof = refs.get(key)
                        if (proof is None or filing is None or proof.document_id != filing.document_id
                                or proof.content_hash != filing.content_hash or not key.startswith('vision-table:')):
                            continue
                        try:
                            payload = json.loads(proof.excerpt)
                            if payload.get('source_structure',{}).get('backend') == 'paddle-structure-v3':
                                return True
                        except (ValueError,TypeError,AttributeError):
                            continue
                    return False
                structure_fact = any(structure_proven(fact) for fact in facts)
                local_ready = bool(filing is not None and structure_fact
                    and observation.get('source_hash') == filing.content_hash
                    and local.get('selected_pages',0) > 0
                    and (local.get('completed_pages') == local.get('selected_pages')
                        or local.get('targets_complete') is True)
                    and local.get('state') == 'ready')
                cloud = vision_batches.get(failure.document_id)
                cloud_ids = {candidate.fact.fact_id for candidate in cloud.candidates} if cloud is not None else set()
                cloud_ready = bool(filing is not None and cloud is not None
                    and cloud.filing.document_id == filing.document_id
                    and cloud.filing.content_hash == filing.content_hash
                    and any(fact.fact_id in cloud_ids for fact in facts))
                recovered = bool(allowed and filing is not None and (local_ready or cloud_ready)
                    and concepts_cover_profile({fact.concept for fact in facts},required))
                failures.append(replace(failure,required=False) if recovered else failure)
            current = replace(current,source_failures=tuple(failures))
            for index, filing in enumerate(filings, start=1):
                outcome = document_processing_outcome(filing, current,
                    replace(collection,source_failures=tuple(failures)))
                observation = getattr(engine, "_document_execution", {}).get(filing.document_id, {})
                if observation.get("source_hash") == filing.content_hash:
                    outcome.update({key: value for key, value in observation.items() if key in {
                        "page_count", "native_text_page_count", "indexed_page_count", "window_total",
                        "completed_page_count", "candidate_page_count",
                        "completed_window_count", "parse_elapsed_seconds", "failure_stage",
                        "root_error_code", "attempt_count", "cache_reused", "pdf_worker_diagnostics",
                        "work_progress", "catalog_pages", "local_structure"}})
                _emit_filing_progress(
                    progress,
                    "filing-validation",
                    index,
                    total_filings,
                    filing,
                    status=outcome["status"],
                    elapsed_seconds=time.monotonic() - filing_started_at[filing.document_id],
                    outcome=outcome,
                )
            return replace(
                current,
                manifests=collection.manifests,
                diagnostics=tuple(dict.fromkeys((*current.diagnostics, *collection.diagnostics))),
                source_failures=tuple(failures),
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
                from .financial_source_errors import PDF_RUNTIME_FAILURE_CODES
                if any(failure.document_id == filing.document_id and failure.code in PDF_RUNTIME_FAILURE_CODES
                       for failure in collection.source_failures):
                    # A broken local component is not permission to submit all
                    # originals to a cloud service. Other healthy docs proceed.
                    continue
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
                parser_failed_pages_by_document={
                    filing.document_id: tuple(observation.get("parser_failed_pages", ()))
                    for filing in visual_filings
                    for observation in [getattr(engine, "_document_execution", {}).get(filing.document_id, {})]
                    if observation.get("source_hash") == filing.content_hash
                },
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
        phase_timings = policy.phase_timings if isinstance(policy.phase_timings, dict) else None
        per_filing_mapping: dict[str, float] = {}
        compiler_elapsed = 0.0
        initially_verified: dict[str, set[tuple]] = {}
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
            filing_mapping_started = time.monotonic()
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
                nonlocal compiler_elapsed
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
                    compiler_started = time.monotonic()
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
                finally:
                    compiler_elapsed += time.monotonic() - compiler_started
                target_end = str(filing.period_end or "")[:10]
                # ``resolved_facts`` is an audit projection and may include
                # non-canonical siblings.  Gap coverage must be based only on
                # the compiler's admitted research view.
                if filing.document_id not in initially_verified:
                    initially_verified[filing.document_id] = {
                        _financial_value_identity(fact) for fact in getattr(result, "research_facts", ())}
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

            per_filing_mapping[filing.document_id] = round(time.monotonic() - filing_mapping_started, 4)


        batches = tuple(CandidateBatch(
            filing,
            tuple(item for item in candidates if item.fact.accession_number == filing.accession_number),
            tuple(ref for ref in evidence if ref.document_id == filing.document_id),
            tuple(diagnostics) if index == 0 else (),
        ) for index, filing in enumerate(filings))
        final_compile_started = time.monotonic()
        result = self.compiler.compile(subject, batches, CompilationPolicy(
            filings=filings, required_concepts=policy.required_concepts,
            reporting_currency=policy.reporting_currency, fiscal_period=policy.fiscal_period,
            scope=policy.scope,
            period_range=policy.period_range if policy.period_range is not None else period_range,
        ))
        compiler_elapsed += time.monotonic() - final_compile_started
        refs = {ref.evidence_id:ref for ref in getattr(result, 'evidence', ())}
        for filing in filings:
            for extractor in (*extractors, *policy.gap_extractors):
                if getattr(extractor, 'stage_kind', None) != GapStageKind.LOCAL_STRUCTURE:
                    continue
                observation = getattr(getattr(extractor, 'engine', None), '_document_execution', {}).get(filing.document_id, {})
                local = observation.get('local_structure')
                if not isinstance(local, dict):
                    continue
                admitted = set()
                for fact in getattr(result, 'research_facts', ()):
                    if fact.accession_number != filing.accession_number:
                        continue
                    for key in fact.classification_evidence_ids:
                        ref = refs.get(key)
                        if not ref or ref.document_id != filing.document_id or ref.content_hash != filing.content_hash or not key.startswith('vision-table:'):
                            continue
                        try:
                            if json.loads(ref.excerpt).get('source_structure', {}).get('backend') == 'paddle-structure-v3':
                                admitted.add(_financial_value_identity(fact))
                        except (ValueError, TypeError, AttributeError):
                            continue
                local['admitted_field_count'] = len(admitted)
                local['new_verified_field_count'] = len(admitted - initially_verified.get(filing.document_id, set()))
        if phase_timings is not None:
            phase_timings["compiler_elapsed_seconds"] = round(compiler_elapsed, 4)
            phase_timings["candidate_mapping_by_filing_seconds"] = per_filing_mapping
        return result

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

    def pending_local_mappings(self, subject: Company, filings: Sequence[FilingDocument]) -> frozenset[str]:
        engine = self.engine_for(subject, filings) if self.engine_for is not None else self.engine
        check = getattr(engine, "pending_row_mappings", None)
        return frozenset(check(subject, filings)) if callable(check) else frozenset()

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
        diagnostic_engine = None
        if snapshot.frozen_candidate_batches is None:
            run_engine = (
                self.engine_for(snapshot.subject, snapshot.filings)
                if self.engine_for is not None else self.engine
            )
            if run_engine is None:
                raise ValueError("FINANCIAL_SOURCE_ENGINE_REQUIRED")
            diagnostic_engine = getattr(run_engine, "engine", run_engine)
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
        parsing_observations = _parser_execution_observations(snapshot, diagnostic_engine)
        admission_started = time.monotonic()
        view = self.admission.admit(
            snapshot.subject,
            dataset,
            selected_filings=snapshot.filings,
            manifests=dataset.manifests or snapshot.source_manifests,
            evidence=dataset.evidence,
            requested_annual_count=snapshot.requested_annual_count,
            research_as_of=snapshot.research_as_of,
            parsing_observations=parsing_observations,
        )
        admission_elapsed = round(time.monotonic() - admission_started, 4)
        execution = getattr(diagnostic_engine, "_document_execution", {})
        if isinstance(execution, dict):
            for filing in snapshot.filings:
                observation = execution.get(filing.document_id)
                if not isinstance(observation, dict) or observation.get("source_hash") != filing.content_hash:
                    continue
                timings = observation.setdefault("stage_timings", {})
                if isinstance(timings, dict):
                    timings["admission_elapsed_seconds"] = admission_elapsed
        incremental_accessions = _incremental_page_accessions(snapshot, dataset, view)
        if incremental_accessions:
            source_failures = list(dataset.source_failures)
            filings_by_accession = {item.accession_number: item for item in snapshot.filings}
            existing_required = {
                (item.document_id, item.accession_number) for item in source_failures
                if item.required
            }
            for accession in incremental_accessions:
                filing = filings_by_accession.get(accession)
                if (filing is not None
                        and (filing.document_id, filing.accession_number) not in existing_required):
                    source_failures.append(FinancialSourceFailure(
                        filing.document_id, filing.accession_number,
                        "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION", required=True,
                    ))
            if len(source_failures) != len(dataset.source_failures):
                dataset = replace(dataset, source_failures=tuple(source_failures))
            readiness = view.readiness
            reasons = tuple(dict.fromkeys((*readiness.reasons, "source_page_plan_incomplete")))
            actions = tuple(dict.fromkeys((*readiness.actions, "source_page_plan_incomplete")))
            view = replace(view, readiness=replace(
                readiness,
                level=(ReadinessLevel.PARTIAL if readiness.level is ReadinessLevel.FULL
                    else readiness.level),
                reasons=reasons,
                actions=actions,
            ))
        publishable_accessions = tuple(sorted(set(
            _publishable_accessions(snapshot, dataset, view)
        ) | set(incremental_accessions)))
        return replace(
            view, canonical_dataset=dataset,
            profile=replace(view.profile, parsing_observations=_parser_execution_observations(snapshot, diagnostic_engine)),
            publishable_accessions=publishable_accessions,
            incremental_accessions=incremental_accessions,
        )


def _parser_execution_observations(snapshot, engine):
    """Copy only bounded diagnostic fields; parser internals can contain paths."""
    observations = {item["accession_number"]: deepcopy(item)
        for item in snapshot.parsing_observations if isinstance(item, dict)
        and isinstance(item.get("accession_number"), str) and item["accession_number"]}
    execution = getattr(engine, "_document_execution", {})
    timing_keys = {
        "source_hash_elapsed_seconds", "document_text_context_elapsed_seconds",
        "catalog_elapsed_seconds", "candidate_plan_elapsed_seconds", "pdf_open_elapsed_seconds",
        "page_text_cache_elapsed_seconds", "worker_session_open_elapsed_seconds", "worker_page_elapsed_seconds",
        "parse_elapsed_seconds", "source_collection_elapsed_seconds", "candidate_mapping_elapsed_seconds",
        "compiler_elapsed_seconds", "admission_elapsed_seconds", "candidate_mapping_seconds",
        "checkpoint_write_elapsed_seconds",
        "geometry_extract_elapsed_seconds",
    }
    for filing in snapshot.filings:
        raw = execution.get(filing.document_id) if isinstance(execution, dict) else None
        if not isinstance(raw, dict) or not filing.content_hash:
            continue
        if raw.get("source_hash") != filing.content_hash:
            if raw.get("source_hash") is not None:
                observations.pop(filing.accession_number, None)
            continue
        # Do not relabel an execution observation whose own period identity
        # contradicts the selected filing. Missing period fields retain the
        # compatibility behavior of binding to the filing metadata below.
        raw_period_end = raw.get("period_end")
        raw_fiscal_period = raw.get("fiscal_period")
        if (raw_period_end is not None
                and str(raw_period_end).strip() != str(filing.period_end or "").strip()):
            observations.pop(filing.accession_number, None)
            continue
        if (raw_fiscal_period is not None
                and str(raw_fiscal_period).strip().upper()
                != str(filing.fiscal_period or "").strip().upper()):
            observations.pop(filing.accession_number, None)
            continue
        observation = {"accession_number": filing.accession_number, "period_end": filing.period_end,
            "fiscal_period": filing.fiscal_period, "source_hash": filing.content_hash}
        timings = {**raw, **(raw["stage_timings"] if isinstance(raw.get("stage_timings"), dict) else {})}
        observation["stage_timings"] = {key: value for key, value in timings.items()
            if key in timing_keys and isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 86_400}
        for key in ("page_count", "candidate_page_count", "completed_page_count", "worker_session_start_count",
                    "geometry_cache_hits", "geometry_extracted_pages"):
            value = raw.get(key)
            if type(value) is int and 0 <= value <= 1_000_000:
                observation[key] = value
        for key in ("cache_reused", "catalog_cache_hit"):
            if type(raw.get(key)) is bool:
                observation[key] = raw[key]
        if raw.get("parse_strategy") == "positioned-page-checkpoint":
            observation["parse_strategy"] = raw["parse_strategy"]
            # This execution strategy is implemented by the positioned PDF
            # worker whose native backend is pdfplumber. An explicit backend
            # remains authoritative and is never relabeled when it disagrees.
            backend = raw.get("backend", "pdfplumber")
            if isinstance(backend, str):
                observation["backend"] = backend
        if isinstance(raw.get('local_structure'), dict):
            observation['local_structure'] = {key:value for key,value in raw['local_structure'].items() if key in {
                'ocr_calls','cache_hits','completed_pages','selected_pages','worker_start_count',
                'model_load_seconds','inference_seconds','peak_memory_bytes','admitted_field_count','new_verified_field_count'}}
        observations[filing.accession_number] = observation
    return tuple(observations[key] for key in sorted(observations) if key)


def _financial_value_identity(fact):
    return tuple(getattr(fact, key, None) for key in ('concept','start_date','end_date',
        'fiscal_period','currency','consolidated_scope','value','unit_scale'))


def disclosed_reporting_currency(
    subject: Company, dataset: FinancialDataset, filings: Sequence[FilingDocument],
) -> tuple[str, bool]:
    """Resolve a metadata currency from individually admitted monetary cells.

    A translated note/currency lane is not a mandate to convert or replace
    the preferred lane. Ambiguity stays explicit; no amounts are changed.
    """
    annual_ends = [str(item.period_end)[:10] for item in filings
                   if str(item.fiscal_period).upper() == "FY" and item.period_end]
    if not annual_ends:
        return subject.reporting_currency, False
    latest = max(annual_ends)
    currencies = {
        str(fact.currency).upper() for group in dataset.validations
        if str(group.identity[1])[:10] == latest and group.role == "target"
        and str(group.identity[2]).upper() == "FY"
        and str(group.identity[3]).casefold() == "consolidated"
        for fact in group.accepted
        if fact.statement in {"income_statement", "balance_sheet", "cash_flow"}
        and fact.currency and fact.validation_status == ValidationStatus.VERIFIED.value
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


_INCREMENTAL_PAGE_FAILURE_CODES = frozenset({
    "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION", "PDF_WINDOW_TIMEOUT", "PDF_WORKER_TIMEOUT",
    "PDF_WINDOW_DETERMINISTIC_FAILED", "PDF_PAGE_PARSE_FAILED", "FILING_LOCAL_PARSE_FAILED",
}) | PDF_RUNTIME_FAILURE_CODES
_INCREMENTAL_PAGE_DIAGNOSTICS = frozenset({
    "pdf_window_incomplete", "pdf_window_failed", "pdf_window_exhausted",
    "pdf_document_budget_exhausted_resumable",
})
_INCREMENTAL_PAGE_FENCES = frozenset({
    "pdf_parse_cancelled", "pdf_file_missing", "pdf_file_unreadable",
    "pdf_candidate_pages_unavailable", "cache_hash_mismatch", "legacy_source_failed",
})


def _incremental_page_accessions(
    snapshot: FinancialResearchSnapshot, dataset: FinancialDataset,
    view: FinancialResearchView,
) -> tuple[str, ...]:
    """Admit compiler-verified facts only behind a bound partial-page checkpoint.

    This is an additive cache-publication path, not a recovery-success claim.
    The original failures and diagnostics remain untouched on the canonical
    dataset and the existing full-publication policy is evaluated separately.
    """
    filings = tuple(snapshot.filings)
    if not filings:
        return ()
    by_document = {item.document_id: item for item in filings if item.document_id}
    by_accession = {item.accession_number: item for item in filings if item.accession_number}
    validations = dataset.coverage.get("fact_validation_records", {})
    if not isinstance(validations, dict):
        validations = {}
    observations = tuple(getattr(view.profile, "parsing_observations", ()) or ())

    def accepted_fact_exists(filing: FilingDocument) -> bool:
        for fact in view.facts:
            # The compiler owns each fact's admitted period and scope. A
            # same-filing comparison or AS_OF row need not match the filing's
            # primary period; only the source accession must match here.
            if fact.accession_number != filing.accession_number:
                continue
            record = validations.get(fact.fact_id)
            if isinstance(record, dict) and record.get("accepted") is True:
                return True
        return False

    def observation_for(filing: FilingDocument) -> dict[str, Any] | None:
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            candidate_pages = observation.get("candidate_page_count")
            completed_pages = observation.get("completed_page_count")
            if (observation.get("accession_number") == filing.accession_number
                    and observation.get("source_hash") == filing.content_hash
                    and observation.get("period_end") == filing.period_end
                    and str(observation.get("fiscal_period", "")).strip().upper()
                    == str(filing.fiscal_period or "").strip().upper()
                    and observation.get("parse_strategy") == "positioned-page-checkpoint"
                    and observation.get("backend") == "pdfplumber"
                    and type(candidate_pages) is int and type(completed_pages) is int
                    and 0 < completed_pages < candidate_pages):
                return observation
        return None

    def diagnostic_parts(diagnostic: Any) -> tuple[FilingDocument | None, str]:
        text = str(diagnostic)
        matches: list[tuple[int, FilingDocument, str]] = []
        for filing in filings:
            for identity in (filing.document_id, filing.accession_number):
                if identity and text.startswith(identity + ":"):
                    matches.append((len(identity), filing, text[len(identity) + 1:]))
        if not matches:
            return None, text
        longest = max(item[0] for item in matches)
        owners = {(item[1].document_id, item[1].accession_number)
                  for item in matches if item[0] == longest}
        if len(owners) != 1:
            return None, text
        owner_identity = next(iter(owners))
        owner = next(item for item in filings
                     if (item.document_id, item.accession_number) == owner_identity)
        suffix = next(item[2] for item in matches if item[0] == longest
                      and (item[1].document_id, item[1].accession_number) == owner_identity)
        return owner, suffix

    incremental: list[str] = []
    for filing in filings:
        if not filing.content_hash or observation_for(filing) is None or not accepted_fact_exists(filing):
            continue

        blocked = False
        saw_local_page_failure = False
        for failure in dataset.source_failures:
            if not getattr(failure, "required", False):
                continue
            document_id = str(getattr(failure, "document_id", "") or "")
            accession = str(getattr(failure, "accession_number", "") or "")
            document_owner = by_document.get(document_id)
            accession_owner = by_accession.get(accession)
            if document_owner is None and accession_owner is None:
                # A required failure with no filing identity is global/unknown.
                blocked = True
                break
            if document_owner is not filing and accession_owner is not filing:
                # A failure explicitly attributed to another filing is isolated.
                continue
            exact_source = document_owner is filing and accession_owner is filing
            code = str(getattr(failure, "code", "")).upper()
            stage = str(getattr(failure, "stage", "")).casefold()
            if (not exact_source or stage not in {"parse", "worker"}
                    or code not in _INCREMENTAL_PAGE_FAILURE_CODES):
                blocked = True
                break
            saw_local_page_failure = True
        if blocked:
            continue

        for diagnostic in dataset.diagnostics:
            owner, suffix = diagnostic_parts(diagnostic)
            if owner is not None and owner is not filing:
                # Do not let another known source's diagnostics taint this filing.
                continue
            lowered = suffix.casefold()
            if any(marker in lowered for marker in _INCREMENTAL_PAGE_FENCES):
                blocked = True
                break
            if owner is filing:
                code = lowered.split(":", 1)[0]
                if code in _INCREMENTAL_PAGE_DIAGNOSTICS:
                    saw_local_page_failure = True
            elif any(marker in lowered for marker in _INCREMENTAL_PAGE_DIAGNOSTICS):
                # An unbound page-plan failure is global, not evidence that this
                # filing alone may use the incremental publication path.
                blocked = True
                break
        if not blocked and saw_local_page_failure:
            incremental.append(filing.accession_number)
    return tuple(sorted(set(incremental)))


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
