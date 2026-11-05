"""Apply document-local model hints by rereading originals, never model amounts.

No provider is available here. Existing research responses supply optional
mapping suggestions; the original-cell reader and ordinary financial compiler
own numeric recovery. Publication preserves all previously admitted amounts.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import CancelledError
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
import time
import uuid

from ..domain import EvidenceRef, FinancialFact
from ..financial_admission import FinancialAdmission
from ..financial_cell_repair import mapping_reference, reread_cell_mappings
from ..financial_compiler import FinancialFactCompiler
from ..financial_contracts import candidate_batches_from_facts, CompilationPolicy
from ..financial_statement_identity import STATEMENT_MAPPING_VERSION
from ..research_source_context import observation_source_record


MODEL_FINANCIAL_REPAIR_VERSION = "research-original-reread-v1"


def model_mapping_references(proposals, observations, filings):
    """Bind untrusted suggestions to this run's actual original observations."""
    sources = {}
    for observation in observations:
        record = observation_source_record(observation)
        if record is not None:
            sources[record["evidence_id"]] = (observation, record)
    by_document = {filing.document_id: filing for filing in filings}
    result, diagnostics, seen = defaultdict(list), [], set()
    for proposal in proposals:
        if not isinstance(proposal, dict):
            diagnostics.append("MODEL_REPAIR_PROPOSAL_INVALID")
            continue
        ids = proposal.get("evidence_ids")
        if (not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str)
                or ids[0] not in sources):
            diagnostics.append("MODEL_REPAIR_ORIGINAL_REFERENCE_INVALID")
            continue
        observation, record = sources[ids[0]]
        filing = by_document.get(record["document_id"])
        if (filing is None or filing.content_hash != record["document_sha256"]
                or filing.company_cik != observation.get("company_cik")
                or filing.accession_number != observation.get("accession_number")
                or proposal.get("original_page") != record["page"]
                or proposal.get("row_quote") != record["raw_text"]):
            diagnostics.append("MODEL_REPAIR_ORIGINAL_BINDING_MISMATCH")
            continue
        mapping = {key: proposal.get(key) for key in ("concept", "label", "row_quote", "original_page")}
        ref = mapping_reference(mapping, filing, {record["page"]})
        if ref is None:
            diagnostics.append("MODEL_REPAIR_MAPPING_INVALID")
        elif (ref.evidence_id, repr(proposal.get("proposed_value")),
              repr(proposal.get("proposed_currency"))) not in seen:
            seen.add((ref.evidence_id, repr(proposal.get("proposed_value")),
                      repr(proposal.get("proposed_currency"))))
            result[filing.accession_number].append((ref, proposal))
    return dict(result), tuple(dict.fromkeys(diagnostics))


def _amount_identity(fact):
    return (fact.accession_number, fact.concept, fact.start_date or fact.period_start,
        fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency,
        tuple((item.axis, item.member, item.kind) for item in fact.dimensions))


def _exact_amount(fact):
    lineage = fact.normalization_lineage
    return Decimal(lineage.normalized_value) if lineage is not None else Decimal(str(fact.value))


def _proposal_agrees(proposal, fact):
    try:
        value = proposal.get("proposed_value")
        currency = proposal.get("proposed_currency")
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                return False
            suggested = Decimal(str(value))
            if not suggested.is_finite() or suggested != _exact_amount(fact):
                return False
        if currency is not None and str(currency).strip().upper() != fact.currency:
            return False
    except (InvalidOperation, TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class ModelFinancialRepairResult:
    view: object | None = None
    snapshot: dict | None = None
    adopted_count: int = 0
    diagnostics: tuple[str, ...] = ()


def repair_model_financials(storage, subject, observations, proposals, *,
                           expected_generations=None, requested_annual_count=None,
                           cancel_check=None, deadline=None, reader=None):
    """Recompile frozen originals and CAS-publish only an actual coverage gain.

    ``reader`` is the same local original-cell Adapter used by visual recovery;
    injecting it makes the production compiler/publication seam testable without
    provider calls. Its output remains untrusted until compilation succeeds.
    """
    def cancelled():
        if cancel_check is not None and cancel_check():
            raise CancelledError("model financial repair cancelled")
    cancelled()
    key = subject.cik if subject.market == "US" else subject.security_id
    snapshot = storage.get_financial_research_snapshot(key, include_evidence=True)
    fields = set(FinancialFact.__dataclass_fields__)
    existing = tuple(FinancialFact(**{k: v for k, v in row.items() if k in fields})
        for row in (*snapshot["facts"], *snapshot.get("supplemental_facts", ())))
    accessions = {fact.accession_number for fact in existing}
    accessions.update(item.get("accession_number") for item in observations if isinstance(item, dict))
    filings = tuple(filing for filing in storage.get_filings(key) if filing.accession_number in accessions)
    grouped, issues = model_mapping_references(proposals, observations, filings)
    if not grouped:
        return ModelFinancialRepairResult(diagnostics=issues)
    if expected_generations is not None and any(
            snapshot["generations"].get(accession, "") != expected_generations.get(accession, "")
            for accession in grouped):
        return ModelFinancialRepairResult(diagnostics=(*issues, "MODEL_REPAIR_STALE_GENERATION"))
    diagnostics = list(issues)
    new_facts, new_evidence = [], []
    read = reader or reread_cell_mappings
    allowance = deadline if deadline is not None else time.monotonic() + 120
    for filing in filings:
        entries = grouped.get(filing.accession_number, ())
        for start in range(0, len(entries), 200):
            cancelled()
            batch_entries = entries[start:start + 200]
            batch = read(subject, filing, tuple(item[0] for item in batch_entries),
                         deadline=allowance, cancel_check=cancel_check)
            diagnostics.extend(batch.diagnostics)
            for candidate in batch.candidates:
                matching = [proposal for _ref, proposal in batch_entries
                    if candidate.fact.concept == proposal["concept"]
                    and candidate.fact.source_page == proposal["original_page"]
                    and re.sub(r"\s+", " ", candidate.fact.raw_text).strip()
                        == re.sub(r"\s+", " ", proposal["row_quote"]).strip()]
                if matching and all(_proposal_agrees(proposal, candidate.fact) for proposal in matching):
                    new_facts.append(candidate.fact)
                elif matching:
                    diagnostics.append("MODEL_REPAIR_SUGGESTED_VALUE_CONFLICT")
            new_evidence.extend(batch.evidence)
    cancelled()
    if not new_facts:
        return ModelFinancialRepairResult(diagnostics=tuple(dict.fromkeys(diagnostics)))
    evidence = {ref.evidence_id: ref for ref in (
        *(EvidenceRef(**row) for row in snapshot.get("evidence", ())), *new_evidence)}
    # Keep independent candidates. The compiler, not the model or insertion
    # order, resolves duplicates/conflicts and verifies each source dimension.
    # A repeated mapping can reread the exact same candidate already active.
    # Do not feed it twice: conflict resolution is for independent source
    # observations, not two copies of one stable fact identity.
    combined = {fact.fact_id: fact for fact in existing}
    for fact in new_facts:
        if fact.fact_id not in combined:
            combined[fact.fact_id] = fact
        elif (_amount_identity(combined[fact.fact_id]) != _amount_identity(fact)
              or _exact_amount(combined[fact.fact_id]) != _exact_amount(fact)):
            return ModelFinancialRepairResult(diagnostics=(*diagnostics, "MODEL_REPAIR_FACT_ID_CONFLICT"))
    batches = candidate_batches_from_facts(filings, tuple(combined.values()), tuple(evidence.values()))
    dataset = FinancialFactCompiler().compile(subject, batches,
        CompilationPolicy(filings=filings, reporting_currency=subject.reporting_currency))
    admitted = {_amount_identity(fact): _exact_amount(fact) for fact in dataset.resolved_facts}
    if any(admitted.get(_amount_identity(fact)) != _exact_amount(fact) for fact in existing):
        return ModelFinancialRepairResult(diagnostics=(*diagnostics, "MODEL_REPAIR_EXISTING_FACT_LOSS"))
    existing_keys = {_amount_identity(fact) for fact in existing}
    eligible = (*dataset.research_facts, *dataset.supplemental_facts)
    new_ids = {fact.fact_id for fact in new_facts}
    gained = [fact for fact in eligible if _amount_identity(fact) not in existing_keys
              and fact.fact_id in new_ids]
    if not gained:
        return ModelFinancialRepairResult(diagnostics=tuple(dict.fromkeys((*diagnostics,
            "MODEL_REPAIR_NOT_YET_VERIFIED"))))
    targets = {fact.accession_number for fact in gained}
    cancelled()
    publication = storage.replace_financial_ingestion(key, sorted(targets),
        list(dataset.research_facts), list(dataset.quarantined_facts), dataset.group_validations,
        tuple(evidence.values()), tuple(fact for fact in dataset.resolved_facts
            if fact.fact_id not in {item.fact_id for item in dataset.research_facts}),
        generation_id=f"{uuid.uuid4().hex}:model-repair:{STATEMENT_MAPPING_VERSION}",
        parser_version=MODEL_FINANCIAL_REPAIR_VERSION,
        source_hashes={filing.accession_number: filing.content_hash for filing in filings
                       if filing.accession_number in targets},
        complete_accessions=targets,
        # Cross-filing reconciliation consumed the whole captured cohort.
        # A concurrent change to a non-target dependency also invalidates it.
        expected_generations={filing.accession_number: snapshot["generations"].get(filing.accession_number, "")
                              for filing in filings},
        fact_validation_records=dataset.coverage.get("fact_validation_records", {}),
        cancel_check=cancel_check)
    if not publication or publication.get("state") == "stale_generation":
        return ModelFinancialRepairResult(diagnostics=(*diagnostics, "MODEL_REPAIR_STALE_GENERATION"))
    promoted = {accession for accession, state in publication.get("accessions", {}).items()
                if state == "promoted"}
    if not (targets & promoted):
        return ModelFinancialRepairResult(diagnostics=(*diagnostics, "MODEL_REPAIR_PUBLICATION_NOT_PROMOTED"))
    if not targets.issubset(promoted):
        diagnostics.append("MODEL_REPAIR_PARTIAL_PUBLICATION")
    committed = storage.get_financial_research_snapshot(key, include_evidence=True)
    committed_facts = tuple(FinancialFact(**{k: v for k, v in row.items() if k in fields})
        for row in (*committed["facts"], *committed.get("supplemental_facts", ())))
    committed_refs = tuple(EvidenceRef(**row) for row in committed.get("evidence", ()))
    committed_amounts = {_amount_identity(fact): _exact_amount(fact) for fact in committed_facts}
    actual_gained = [fact for fact in gained if committed_amounts.get(_amount_identity(fact)) == _exact_amount(fact)]
    if not actual_gained:
        return ModelFinancialRepairResult(diagnostics=(*diagnostics, "MODEL_REPAIR_PUBLICATION_NOT_VISIBLE"))
    compiled = FinancialFactCompiler().compile_facts(subject, filings, committed_facts,
        evidence=committed_refs, reporting_currency=subject.reporting_currency)
    view = FinancialAdmission().admit(subject, compiled, selected_filings=filings,
        evidence=committed_refs, requested_annual_count=requested_annual_count)
    return ModelFinancialRepairResult(view, committed, len(actual_gained), tuple(dict.fromkeys(diagnostics)))
