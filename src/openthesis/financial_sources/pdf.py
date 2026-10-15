"""PDF and structured filing collection without compilation or admission."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import time
from typing import Any, Sequence

from ..domain import Company, EvidenceRef, FilingDocument
from ..financial_contracts import (
    CandidateBatch, FactCandidate, FinancialCandidateCollection,
    fact_row_evidence_ids,
    FinancialSourceFailure,
)
from ..disclosure_identity import DisclosureIdentityResolver
from ..financial_inline_xbrl import extract_inline_xbrl
from ..financial_source_errors import LOCAL_PARSER_AVAILABILITY_CODES
from ..disclosure_index import cached_page_texts


@dataclass(frozen=True, slots=True)
class FinancialPdfSource:
    """Collect source candidates while leaving every acceptance decision downstream."""

    engine: Any

    def collect_candidate_batches(
        self,
        company: Company,
        filings: Sequence[FilingDocument],
        *,
        structured_sources: Sequence[Any] = (),
        cancel_check: Any = None,
        progress: Any = None,
        deadline: float | None = None,
    ) -> Any:
        """Return explicit structured/PDF batches and their source manifests."""
        budget_deadline = time.monotonic() + float(self.engine._batch_timeout_seconds)
        deadline = min(deadline, budget_deadline) if deadline is not None else budget_deadline

        # The engine remains the bounded parser/cache implementation; this
        # source adapter owns the collection choreography, not the compiler.
        from ..financial_ingestion import (
            _manifest_for,
            _manifest_from_observed_pages,
            _pair_pdf_facts_evidence,
            _parse_local_pdfs_bounded,
        )

        manifests: list[Any] = []
        manifest_by_document: dict[str, Any] = {}
        diagnostics: list[str] = []
        failures = []
        for filing in filings:
            if cancel_check is not None and cancel_check():
                break
            if filing.period_metadata_status == "conflict":
                # Keep verified original bytes for audit/recovery, but do not
                # turn contradictory metadata into a usable monetary period.
                diagnostics.append(f"{filing.document_id}:FILING_PERIOD_METADATA_CONFLICT")
                failures.append(FinancialSourceFailure(filing.document_id, filing.accession_number,
                    "identity", "FILING_PERIOD_METADATA_CONFLICT"))
                continue
            manifest = _manifest_for(filing)
            if manifest is None:
                diagnostics.append(f"{filing.document_id}:ambiguous_period_identity")
                continue
            manifests.append(manifest)
            manifest_by_document[filing.document_id] = manifest
            filing.form_type = manifest.form_type
            filing.fiscal_period = manifest.fiscal_period
            filing.period_end = manifest.period_end
            filing.period_metadata_status = "source_confirmed" if manifest.period_end else "unresolved"
            # Unresolved period guards stay in the parser manifest. Source
            # candidates and persisted originals retain the captured edition.
            filing.revision = manifest.original_revision or manifest.revision
            filing.supersedes_document_id = manifest.supersedes_document_id

        inline_filings = {
            filing.document_id for filing in filings
            if company.market == "US"
            and Path(filing.local_path or filing.primary_document).suffix.lower() in {".htm", ".html", ".xhtml"}
        }
        pdf_filings = tuple(filing for filing in filings
            if filing.document_id in manifest_by_document and filing.document_id not in inline_filings)
        parsed_pdfs = _parse_local_pdfs_bounded(
            self.engine,
            company,
            pdf_filings,
            manifest_by_document,
            cancel_check=cancel_check,
            progress=progress,
            max_workers=self.engine._max_workers,
            parse_timeout_seconds=self.engine._parse_timeout_seconds,
            batch_timeout_seconds=self.engine._batch_timeout_seconds,
            deadline=deadline,
        ) if pdf_filings else {}
        batches: dict[str, tuple[CandidateBatch, ...]] = {}
        all_evidence = []
        for index, filing in enumerate(filings, start=1):
            if cancel_check is not None and cancel_check():
                break
            if filing.document_id not in manifest_by_document:
                continue
            # Workers already own the complete native text pass. Reuse its
            # source-bound result to resolve even a book with no known labels;
            # never serialize N whole-book reads before the first worker starts.
            texts = cached_page_texts(filing.local_path) if filing.local_path else None
            if texts is not None:
                observed = _manifest_from_observed_pages(manifest_by_document[filing.document_id], filing, texts)
                manifest_by_document[filing.document_id] = observed
                manifests[:] = [observed if item.document_id == filing.document_id else item for item in manifests]
                filing.period_end = observed.period_end
                filing.period_metadata_status = ("conflict" if observed.revision == "period_header_conflict"
                    else "source_confirmed" if observed.period_end else "unresolved")
            structured_candidates: list[FactCandidate] = []
            structured_refs = []
            if filing.document_id in inline_filings and filing.local_path:
                try:
                    source_path = Path(filing.local_path)
                    if source_path.stat().st_size > 32 * 1024 * 1024:
                        raise ValueError("INLINE_SOURCE_SIZE_INVALID")
                    raw = source_path.read_bytes()
                    native = extract_inline_xbrl(company, filing, raw)
                    structured_candidates.extend(native.candidates)
                    structured_refs.extend(native.evidence)
                    diagnostics.extend(f"{filing.document_id}:{item}" for item in native.diagnostics)
                except (OSError, ValueError) as exc:
                    # No arbitrary exception text or paths in public diagnostics.
                    diagnostics.append(f"{filing.document_id}:inline:{type(exc).__name__}")
                    failures.append(FinancialSourceFailure(filing.document_id, filing.accession_number,
                        "parse", "FILING_LOCAL_PARSE_FAILED"))
            for adapter in structured_sources:
                try:
                    sfacts, srefs, failure = adapter.fetch(company, filing)
                except Exception as exc:
                    diagnostics.append(
                        f"{filing.document_id}:structured:{type(exc).__name__}"
                    )
                    continue
                if failure:
                    diagnostics.append(f"{filing.document_id}:{failure}")
                # Row association and supplemental provenance are distinct.
                # Unit/formula evidence must reach the pure validator even
                # though it is not a financial row and has no fact: ID.
                structured_refs.extend(srefs)
                refs_by_id: dict[str, list[EvidenceRef]] = {}
                for ref in srefs:
                    refs_by_id.setdefault(ref.evidence_id, []).append(ref)
                for fact in sfacts:
                    if fact.unit_provenance == "unknown" and fact.concept != "reported_roe":
                        fact = replace(fact, unit_provenance="structured_normalized")
                    refs = tuple(
                        ref
                        for evidence_id in fact_row_evidence_ids(fact)
                        for ref in refs_by_id.get(evidence_id, ())
                    )
                    structured_candidates.append(FactCandidate(
                        fact, refs, f"structured:{type(adapter).__name__}"
                    ))
            structured_batch = CandidateBatch(
                filing,
                tuple(structured_candidates),
                tuple(structured_refs),
            )
            pdf_facts, pdf_refs, pdf_error = parsed_pdfs.get(
                filing.document_id, ([], [], None)
            )
            if pdf_error:
                diagnostics.append(f"{filing.document_id}:{pdf_error}")
                failures.append(FinancialSourceFailure(filing.document_id, filing.accession_number,
                    "parse", pdf_error if pdf_error in LOCAL_PARSER_AVAILABILITY_CODES | {"PERIOD_DECLARATION_NOT_FOUND"}
                    else "FILING_LOCAL_PARSE_FAILED", required=bool(filing.local_path)))
            paired_pdf, pairing_diagnostics = _pair_pdf_facts_evidence(
                pdf_facts, pdf_refs,
            )
            diagnostics.extend(
                f"{filing.document_id}:{item}" for item in pairing_diagnostics
            )
            local_unavailable = pdf_error in LOCAL_PARSER_AVAILABILITY_CODES | {"DOCUMENT_CONTEXT_CANCELLED"}
            period_diagnostics = ("PERIOD_DECLARATION_NOT_FOUND",) if not local_unavailable and (
                any(ref.evidence_id.startswith("period-gap:") for ref in pdf_refs)
                or not pdf_facts and not manifest_by_document[filing.document_id].period_end
            ) else ()
            diagnostics.extend(f"{filing.document_id}:{code}" for code in period_diagnostics)
            pdf_batch = CandidateBatch(
                filing,
                tuple(
                    FactCandidate(fact, (ref,), "financial-ingestion-ast")
                    for fact, ref in paired_pdf
                ),
                tuple(pdf_refs),
                period_diagnostics,
            )
            self._promote_observed_period_if_needed(
                filing,
                manifest_by_document[filing.document_id],
                structured_candidates,
                pdf_batch.candidates,
                manifests,
                manifest_by_document,
            )
            batches[filing.document_id] = (structured_batch, pdf_batch)
            all_evidence.extend(structured_refs)
            all_evidence.extend(pdf_refs)
            if progress is not None:
                progress("filing-candidates", index, len(filings))
        return FinancialCandidateCollection(
            tuple(manifests),
            batches,
            tuple(all_evidence),
            tuple(dict.fromkeys(diagnostics)),
            tuple(failures),
        )

    @staticmethod
    def _promote_observed_period_if_needed(
        filing: FilingDocument,
        manifest: Any,
        structured_candidates: Sequence[FactCandidate],
        pdf_candidates: Sequence[FactCandidate],
        manifests: list[Any],
        manifest_by_document: dict[str, Any],
    ) -> None:
        if filing.period_end or manifest.revision == "period_header_conflict":
            return
        observed_dates = [
            candidate.fact.end_date
            for candidate in (*structured_candidates, *pdf_candidates)
            if candidate.fact.end_date
            and (candidate.fact.fiscal_period or filing.fiscal_period).upper()
            == (filing.fiscal_period or "FY").upper()
        ]
        observed_identity = DisclosureIdentityResolver().resolve(
            title=filing.primary_document,
            filed_at=filing.filed_at,
            provider_metadata={
                "period_end": "",
                "fiscal_period": filing.fiscal_period,
                "revision": "period_end_provisional",
            },
            observed_statement_dates=tuple(observed_dates),
        )
        if not observed_identity.period_end or observed_identity.provisional:
            return
        filing.period_end = observed_identity.period_end
        filing.period_metadata_status = "source_confirmed"
        filing.revision = manifest.original_revision or filing.revision
        resolved_manifest = replace(
            manifest,
            period_end=filing.period_end,
            revision=filing.revision,
        )
        manifest_by_document[filing.document_id] = resolved_manifest
        manifests[:] = [
            resolved_manifest if item.document_id == filing.document_id else item
            for item in manifests
        ]
