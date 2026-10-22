"""Candidate-only source Adapters and bounded collection compatibility.

All acceptance is owned by the pure compiler. Recovery retry policy migrates
from GapResolver to the persistent coordinator during stage C/D.
"""
from __future__ import annotations

from concurrent.futures import (
    CancelledError, ThreadPoolExecutor, wait, FIRST_COMPLETED,
)
from dataclasses import dataclass, replace, is_dataclass
from copy import copy
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from ..domain import Company, FilingDocument, EvidenceRef, FinancialFact
from ..financial_contracts import (
    CandidateBatch, FactCandidate, GapStageKind, concepts_cover_profile,
    fact_row_evidence_ids,
)
from .pdf import FinancialPdfSource


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
        existing_candidates: Sequence[FactCandidate] = (),
        verified_concepts: Callable[[Sequence[FactCandidate], Sequence[EvidenceRef]], Sequence[str]] | None = None,
        cancel_check: Any = None,
        user_authorized: bool = False,
        max_attempts: int = 1,
    ) -> GapResolution:
        wanted = frozenset(missing_concepts)
        # Keep the legacy argument for API compatibility, but do not collapse
        # candidates by concept: comparative columns can share a concept while
        # differing in period, scope, unit, or source.
        del existing_concepts

        def candidate_identity(candidate: FactCandidate) -> tuple[Any, ...]:
            fact = candidate.fact
            return (
                filing.document_id,
                fact.accession_number or filing.accession_number,
                fact.concept,
                str(fact.end_date or "")[:10],
                str(fact.start_date or fact.period_start or "")[:10],
                str(fact.fiscal_period or "FY").upper(),
                fact.statement or "",
                fact.consolidated_scope or fact.scope or "unknown",
                fact.currency or fact.unit or "",
                str(fact.unit_scale),
                fact.revision or "original",
                fact.source_document or filing.primary_document,
                fact.source_page,
                fact.source_column or "",
                str(candidate.extractor),
                tuple(sorted(str(ref.evidence_id) for ref in candidate.evidence)),
            )

        found: list[FactCandidate] = []
        seen_keys = {
            candidate_identity(candidate) for candidate in existing_candidates
        }
        refs: list[EvidenceRef] = []
        stages: list[str] = []
        diagnostics: list[str] = []
        previous_order = -1

        def admitted_concepts() -> frozenset[str]:
            candidate_items = tuple((*existing_candidates, *found))
            if verified_concepts is not None:
                try:
                    return frozenset(str(item) for item in verified_concepts(candidate_items, tuple(refs)) if item)
                except Exception as exc:  # compiler is a trust boundary too
                    diagnostics.append(f"verified_concepts_failed:{type(exc).__name__}")
                    return frozenset()
            # Collection adapters do not choose a compiler or its policy.
            # The production pipeline injects the canonical, run-bound gate.
            # Candidate-only callers may still collect within their budget,
            # but neither raw concept names nor their own success flags are
            # evidence that a gap has been filled.
            if "verified_concepts_required" not in diagnostics:
                diagnostics.append("verified_concepts_required")
            return frozenset()

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
                # Keep source-only observations for audit/report visibility
                # even when no numeric candidate can safely be admitted.
                refs.extend(batch.evidence)
                previous_count = len(found)
                for candidate in batch.candidates:
                    key = candidate_identity(candidate)
                    # Conflict resolution belongs to the compiler after
                    # canonical period/scope/currency grouping.  Suppressing
                    # a same-concept candidate here loses comparative facts.
                    if key not in seen_keys:
                        found.append(candidate)
                        refs.extend(candidate.evidence)
                        seen_keys.add(key)
                if concepts_cover_profile(admitted_concepts(), wanted):
                    break
                # A repeated empty/duplicate batch is not a reason to spin
                # until the retry cap.  Errors above remain bounded retries.
                if len(found) == previous_count:
                    break
            if concepts_cover_profile(admitted_concepts(), wanted):
                break
        if not concepts_cover_profile(admitted_concepts(), wanted):
            diagnostics.append("gap_unresolved")
        unique_refs: dict[str, EvidenceRef] = {}
        for ref in refs:
            unique_refs.setdefault(ref.evidence_id, ref)
        return GapResolution(tuple(found), tuple(unique_refs.values()), tuple(stages), tuple(diagnostics))


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
        facts = [
            replace(fact, unit_provenance="structured_normalized")
            if fact.unit_provenance == "unknown" and fact.concept != "reported_roe"
            else fact
            for fact in facts
        ]
        refs_by_id: dict[str, list[EvidenceRef]] = {}
        for ref in refs:
            refs_by_id.setdefault(ref.evidence_id, []).append(ref)
        candidates = tuple(
            FactCandidate(
                fact,
                tuple(
                    ref
                    for evidence_id in fact_row_evidence_ids(fact)
                    for ref in refs_by_id.get(evidence_id, ())
                ),
                self.name,
            )
            for fact in facts
        )
        return CandidateBatch(filing, candidates, tuple(refs))

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
    period_header_pages_by_document: Mapping[str, Sequence[int]] | None = None
    deadline: float | None = None
    parser_failed_pages_by_document: Mapping[str, Sequence[int]] | None = None

    def extract(self, subject: Company, filing: FilingDocument) -> CandidateBatch:
        from ..financial_ingestion import _vision_failed_pages

        if self.cancel_check is not None and self.cancel_check():
            raise CancelledError("vision candidate extraction cancelled")
        deadline = min(self.deadline, time.monotonic() + float(self.config.timeout_seconds)) if self.deadline is not None else time.monotonic() + float(self.config.timeout_seconds)
        if time.monotonic() >= deadline:
            return CandidateBatch(filing, diagnostics=("vision_timeout",))
        issues = (self.validation_issues_by_document or {}).get(filing.document_id, ())
        existing_facts = (self.existing_facts_by_document or {}).get(
            filing.document_id, ()
        )
        try:
            pages = _vision_failed_pages(filing, self.config, issues, existing_facts,
                cancel_check=self.cancel_check, period_header_pages=(
                    self.period_header_pages_by_document or {}).get(filing.document_id, ()),
                deadline=deadline, parser_failed_pages=(
                    self.parser_failed_pages_by_document or {}).get(filing.document_id, ()))
        except Exception as exc:
            from ..vision_financials import VisionAdapterError
            if not isinstance(exc, VisionAdapterError):
                raise
            if exc.code == "VISION_CANCELLED":
                raise CancelledError("vision page preparation cancelled") from exc
            return CandidateBatch(filing, diagnostics=(exc.code,))
        if not pages:
            return CandidateBatch(filing, diagnostics=("vision_no_failed_pages",))
        if self.cancel_check is not None and self.cancel_check():
            raise CancelledError("vision candidate extraction cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return CandidateBatch(filing, diagnostics=("vision_timeout",))
        # Configuration is a run snapshot. Never mutate a user's configured
        # timeout to pass the queue/page-preparation remainder to an Adapter.
        config = _vision_remaining_config(self.config, remaining)
        result = self.adapter.extract(
            subject, filing, pages, config, cancel_check=self.cancel_check
        )
        if self.cancel_check is not None and self.cancel_check():
            raise CancelledError("vision candidate extraction cancelled")
        if time.monotonic() >= deadline:
            return CandidateBatch(filing, diagnostics=("vision_timeout",))
        if getattr(result, "error_code", None) == "VISION_CANCELLED":
            raise CancelledError("vision candidate extraction cancelled")
        refs_by_id = {}
        for ref in result.evidence:
            refs_by_id.setdefault(ref.evidence_id, []).append(ref)
        from ..financial_cell_repair import reread_cell_mappings, unconsumed_mappings
        repair = reread_cell_mappings(subject, filing, unconsumed_mappings(result.facts, result.evidence),
            rules=getattr(self.engine, "_compatibility_rules", None), deadline=deadline,
            cancel_check=self.cancel_check)
        if (self.cancel_check is not None and self.cancel_check()) or "VISION_CANCELLED" in repair.diagnostics:
            raise CancelledError("vision cell reread cancelled")
        if time.monotonic() >= deadline:
            return CandidateBatch(filing, diagnostics=("vision_timeout",))
        candidates = tuple(
            FactCandidate(
                fact,
                tuple(ref for key in fact_row_evidence_ids(fact)
                      for ref in refs_by_id.get(key, ())),
                self.name,
            )
            for fact in result.facts
        )
        candidates = (*candidates, *repair.candidates)
        diagnostics = [*result.diagnostics, *repair.diagnostics]
        error_code = getattr(result, "error_code", None)
        if isinstance(error_code, str) and error_code not in diagnostics:
            diagnostics.append(error_code)
        if repair.candidates:
            diagnostics.append("CELL_REPAIR_REREAD_CANDIDATES")
            diagnostics = [item for item in diagnostics if item != "VISION_NO_CANDIDATES"]
        return CandidateBatch(filing, candidates, tuple((*result.evidence, *repair.evidence)), tuple(diagnostics))


def _vision_remaining_config(config: Any, remaining: float) -> Any:
    timeout = min(float(config.timeout_seconds), remaining)
    if is_dataclass(config):
        fields = {"timeout_seconds": timeout}
        if hasattr(config, "configured_timeout_seconds"):
            fields["configured_timeout_seconds"] = config.configured_timeout_seconds or float(config.timeout_seconds)
        return replace(config, **fields)
    # Injected compatibility Adapters use mutable config objects in tests.
    current = copy(config)
    current.timeout_seconds = timeout
    return current


def _prefetch_vision_batches(
    subject: Company,
    filings: Sequence[FilingDocument],
    extractor: VisionCandidateExtractor,
    *,
    timeout_seconds: float,
    deadline: float | None = None,
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
    user_cancel = getattr(extractor, "cancel_check", None)
    stop = threading.Event()
    deadline = min(deadline, time.monotonic() + max(0.01, float(timeout_seconds))) if deadline is not None else time.monotonic() + max(0.01, float(timeout_seconds))

    def cancel_check() -> bool:
        return bool(user_cancel and user_cancel())

    def worker_cancel() -> bool:
        return stop.is_set() or cancel_check() or time.monotonic() >= deadline

    if isinstance(extractor, VisionCandidateExtractor):
        extractor = replace(extractor, cancel_check=worker_cancel, deadline=deadline)
    if cancel_check():
        raise CancelledError("vision candidate extraction cancelled")
    if time.monotonic() >= deadline:
        return {item.document_id: CandidateBatch(item, diagnostics=("vision_timeout",)) for item in ordered}
    worker_count = max(1, min(2, len(ordered)))
    executor = ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="financial-vision"
    )

    def collect_one(filing: FilingDocument) -> CandidateBatch:
        if worker_cancel():
            return CandidateBatch(filing, diagnostics=("vision_timeout",))
        result = extractor.extract(subject, filing)
        if worker_cancel():
            return CandidateBatch(filing, diagnostics=("vision_timeout",))
        return result

    futures = {
        executor.submit(collect_one, filing): filing
        for filing in ordered
    }
    results: dict[str, CandidateBatch] = {}
    try:
        while futures:
            if cancel_check():
                raise CancelledError("vision candidate extraction cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Drain completed siblings before fencing unfinished work.
                done = {future for future in futures if future.done()}
            else:
                done, _ = wait(tuple(futures), timeout=min(.05, remaining), return_when=FIRST_COMPLETED)
            for future in done:
                filing = futures.pop(future)
                try:
                    batch = future.result()
                except CancelledError:
                    if cancel_check():
                        raise
                    batch = CandidateBatch(filing, diagnostics=("vision_timeout",))
                except Exception as exc:
                    code = getattr(exc, "code", "")
                    if cancel_check():
                        raise CancelledError("vision candidate extraction cancelled") from exc
                    if code == "VISION_CANCELLED":
                        if time.monotonic() < deadline:
                            raise CancelledError("vision candidate extraction cancelled") from exc
                        batch = CandidateBatch(filing, diagnostics=("vision_timeout",))
                    else:
                        batch = CandidateBatch(filing, diagnostics=(f"vision_failed:{_safe_vision_error_code(code)}",))
                if cancel_check():
                    raise CancelledError("vision candidate extraction cancelled")
                if any(item.casefold() in {"vision_cancelled", "vision_cancelled: true"} for item in batch.diagnostics):
                    if time.monotonic() < deadline:
                        raise CancelledError("vision candidate extraction cancelled")
                    batch = CandidateBatch(filing, diagnostics=("vision_timeout",))
                results[filing.document_id] = batch
            if remaining <= 0:
                for filing in futures.values():
                    results[filing.document_id] = CandidateBatch(filing, diagnostics=("vision_timeout",))
                break
    finally:
        stop.set()
        for future in futures:
            future.cancel()
        # Native page work is killable and real cloud transports receive the
        # same remaining budget/cancel signal. A defective injected Adapter
        # cannot make the collector wait forever; its late return is ignored
        # and never reaches the compiler or collection cache.
        executor.shutdown(wait=not futures, cancel_futures=True)
    return {filing.document_id: results[filing.document_id] for filing in ordered}


def _safe_vision_error_code(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", value):
        return value
    return "VISION_ADAPTER_FAILED"
