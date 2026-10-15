"""Candidate-only compatibility adapter for injected pre-collection sources.

Legacy acceptance flags are never authority: every returned fact is re-entered
as an observation at the normal pure compiler seam. Production PDF sources do
not use this adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Sequence

from ..domain import Company, FilingDocument
from ..financial_contracts import FinancialCandidateCollection, FinancialSourceFailure, candidate_batches_from_facts


@dataclass(frozen=True, slots=True)
class LegacyFinancialSource:
    engine: Any

    def collect_candidate_batches(
        self, subject: Company, filings: Sequence[FilingDocument], **options: Any
    ) -> FinancialCandidateCollection:
        method = self.engine.ingest
        parameters = inspect.signature(method).parameters.values()
        accepts_all = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters)
        allowed = {item.name for item in parameters}
        forwarded = {
            key: value for key, value in options.items()
            if accepts_all or key in allowed
        }
        # No TypeError-based second invocation: a producer may already have
        # performed work before raising, so retrying it could duplicate I/O.
        observations = {}
        evidence = []
        manifests = []
        diagnostics = []
        failures = []
        for filing in filings:
            try:
                output = method(subject, (filing,), **forwarded)
            except OSError as exc:
                diagnostics.append(f"{filing.accession_number}:legacy_source_failed:{type(exc).__name__}")
                failures.append(FinancialSourceFailure(filing.document_id, filing.accession_number,
                    "parse", "FILING_LOCAL_PARSE_FAILED"))
                continue
            resolved = getattr(output, "resolved_facts", None)
            if resolved is None:
                resolved = getattr(output, "accepted_facts", ())
            for fact in resolved or ():
                observations.setdefault(fact.fact_id, fact)
            for group in getattr(output, "group_validations", ()) or ():
                validation = getattr(group, "validation", group)
                for fact in getattr(validation, "quarantined", ()) or ():
                    observations.setdefault(fact.fact_id, fact)
            for fact in getattr(output, "quarantined_facts", ()) or ():
                observations.setdefault(fact.fact_id, fact)
            evidence.extend(getattr(output, "evidence", ()) or ())
            manifests.extend(getattr(output, "manifests", None) or getattr(output, "manifest", ()) or ())
            diagnostics.extend(getattr(output, "diagnostics", ()) or ())
        batches = candidate_batches_from_facts(
            filings, tuple(observations.values()), tuple(evidence)
        )
        return FinancialCandidateCollection(
            manifests=tuple({item.document_id: item for item in manifests}.values()),
            batches_by_document={item.filing.document_id: (item,) for item in batches},
            evidence=tuple(evidence),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            source_failures=tuple(failures),
        )
