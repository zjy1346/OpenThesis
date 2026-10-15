"""Official byte acquisition shared by first research and financial repair.

This module owns cache identity, byte reuse and bounded downloads. It never
accepts financial facts, alters source bytes, writes application repositories
or calls a model. Discovery/selection inputs are supplied by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from ..domain import FilingDocument
from ..market_data import MarketDataError
from .downloads import (assess_filing_identity, bounded_download_filings, filing_cache_valid,
                        filing_identity_matches, local_cache_diagnostic)


@dataclass(frozen=True, slots=True)
class FilingAcquisitionResult:
    filings: tuple[FilingDocument, ...]
    cached: tuple[FilingDocument, ...]
    downloaded: tuple[FilingDocument, ...]
    errors: tuple[tuple[FilingDocument, Exception], ...]
    cancelled: bool = False


class _Cancellation:
    def __init__(self, check: Callable[[], bool]):
        self._check = check

    def is_set(self) -> bool:
        return bool(self._check())


class OfficialFilingAcquisition:
    """Acquire a selected cohort without re-downloading verified originals."""

    def acquire(
        self, adapter: Any, filings: Sequence[FilingDocument], target: Path, *,
        stored_filings: Sequence[FilingDocument] = (),
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> FilingAcquisitionResult:
        check = cancel_check or (lambda: False)
        # Work on detached metadata; cache reuse must not mutate discovery's
        # snapshots or the caller's historical filing records.
        selected = tuple(replace(item) for item in filings)
        stored = {item.accession_number: item for item in stored_filings}
        cached: dict[str, FilingDocument] = {}
        pending: list[FilingDocument] = []
        for item in selected:
            if check():
                return FilingAcquisitionResult((), (), (), (), True)
            prior = stored.get(item.accession_number)
            cache = self.verified_cache(item, prior)
            if cache is not None:
                cached[item.accession_number] = cache
            else:
                pending.append(item)
        if progress is not None:
            progress(len(cached), len(selected))
        downloaded, errors = bounded_download_filings(
            # Adapters may mutate metadata while filling local_path/hash. Do
            # not let that mutate the expected source identity used below.
            adapter, [replace(item) for item in pending], target, cancel_event=_Cancellation(check),
            progress=(lambda completed, _total: progress(len(cached) + completed, len(selected)))
            if progress is not None else None,
        )
        errors.extend((item, MarketDataError("Conflicting disclosure period metadata",
            code=item.period_metadata_diagnostic)) for item in cached.values()
            if item.period_metadata_status == "conflict")
        if check():
            # A late download response does not publish a cancelled cohort.
            return FilingAcquisitionResult((), tuple(cached.values()), (), tuple(errors), True)
        expected = {item.accession_number: item for item in selected}
        verified = []
        for item in downloaded:
            original = expected.get(item.accession_number)
            if original is None or not self._same_source(original, item):
                errors.append((original or item, MarketDataError(
                    "Downloaded filing does not match the requested source",
                    code="FILING_SOURCE_IDENTITY_MISMATCH",
                )))
            elif (diagnostic := local_cache_diagnostic(item)):
                errors.append((original, MarketDataError(
                    "Downloaded filing bytes do not match their content hash",
                    code=diagnostic,
                )))
            else:
                verified.append(OfficialFilingAcquisition._with_metadata(original, item))
        by_accession = {item.accession_number: item for item in verified}
        acquired = tuple(
            item for original in selected
            if (item := by_accession.get(original.accession_number)
                or cached.get(original.accession_number)) is not None
        )
        failed = {item.accession_number for item, _error in errors}
        acquired_accessions = {item.accession_number for item in acquired}
        for original in selected:
            if original.accession_number not in acquired_accessions | failed:
                errors.append((original, MarketDataError(
                    "Requested filing did not produce verified source bytes",
                    code="FILING_DOWNLOAD_INCOMPLETE",
                )))
        if check():
            return FilingAcquisitionResult((), tuple(cached.values()), (), tuple(errors), True)
        return FilingAcquisitionResult(acquired, tuple(cached.values()), tuple(verified), tuple(errors))

    @staticmethod
    def verified_cache(filing: FilingDocument, prior: FilingDocument | None = None) -> FilingDocument | None:
        """Read-only source identity and byte proof used also by repair selection."""
        if prior is not None:
            assessment = assess_filing_identity(filing, prior)
            if assessment.same_original and filing_cache_valid(prior):
                return OfficialFilingAcquisition._with_metadata(filing, prior)
            # A new edition/hash must not silently inherit old metadata even
            # when the caller already has valid local bytes for the new file.
        if assess_filing_identity(filing, filing).same_original and filing_cache_valid(filing):
            return OfficialFilingAcquisition._with_metadata(filing, filing)
        return None

    @staticmethod
    def _with_metadata(filing: FilingDocument, source: FilingDocument) -> FilingDocument:
        assessment = assess_filing_identity(filing, source)
        return replace(filing, local_path=source.local_path, content_hash=source.content_hash,
            period_end=(assessment.resolved_period_end if assessment.metadata_compatible else source.period_end),
            revision=assessment.actual_revision, period_metadata_status=assessment.period_state,
            period_metadata_diagnostic=assessment.diagnostic_code)

    @staticmethod
    def _same_source(expected: FilingDocument, actual: FilingDocument) -> bool:
        return (
            expected.company_cik == actual.company_cik
            and expected.accession_number == actual.accession_number
            and expected.document_id == actual.document_id
            and filing_identity_matches(expected, actual)
        )
