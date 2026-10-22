"""Deterministic compiler: frozen candidates to validated facts.

No downloads, extraction, network calls or retries belong in this Module.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .domain import CURRENT_DERIVED_VERSION, Company, EvidenceRef, FilingDocument, FinancialFact
from .financial_contracts import (
    CORE_CONCEPTS, CandidateBatch, FactCandidate, FactGroupValidation, FactValidationRecord,
    FinancialDataset, SourceIdentity, ValidationStatus, CompilationPolicy as CompilerPolicy,
    SourceComponentObservation,
    candidate_batches_from_facts, concepts_cover_profile, missing_profile_concepts,
)
from .financial_validation import FinancialValidator
from .financial_candidate_resolution import resolve_display_rounding, resolve_issuer_presentation
from .financial_unit_declarations import literal_money_unit, physical_unit_context


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
        evidence: Sequence[EvidenceRef] = (),
    ) -> FinancialDataset:
        """Re-compile already extracted facts through the canonical gate.

        Compatibility adapter for retry/SEC callers. It preserves only supplied
        evidence associations and never fills absent source, unit, or period
        metadata. Facts with undeclared accessions remain compiler candidates
        with explicit diagnostics instead of disappearing from the result.
        """
        batches = candidate_batches_from_facts(filings, facts, evidence)
        ends = sorted({str(item.period_end)[:10] for item in filings if item.period_end})
        actual_range = (ends[0], ends[-1]) if ends else None
        return self.compile(
            subject,
            batches,
            CompilerPolicy(
                filings=tuple(filings),
                required_concepts=required_concepts,
                reporting_currency=reporting_currency or subject.reporting_currency,
                period_range=actual_range,
            ),
        )

    def compile(
        self,
        subject: Company,
        batches: Sequence[CandidateBatch],
        policy: CompilerPolicy,
    ) -> FinancialDataset:
        """Validate already frozen candidates; never discover or extract."""
        batches = tuple(batches)
        filings = tuple(policy.filings) or tuple(batch.filing for batch in batches)
        profile = CoveragePlanner().plan(subject)
        required_concepts = policy.required_concepts or profile.required_concepts
        target_fiscal_period = (policy.fiscal_period or "FY").strip().upper()
        target_scope = (policy.scope or "consolidated").strip().lower()
        target_currency = (policy.reporting_currency or subject.reporting_currency or "").strip().upper()
        range_start, range_end = _period_range_bounds(policy.period_range)
        diagnostics: list[str] = []
        candidates: list[FactCandidate] = []
        evidence: list[EvidenceRef] = []
        declared_sources: dict[str, SourceIdentity] = {}
        for filing in filings:
            identity = SourceIdentity.from_filing(filing)
            prior = declared_sources.setdefault(filing.document_id, identity)
            if prior != identity:
                raise ValueError("CANDIDATE_SOURCE_IDENTITY_CONFLICT")
        for batch in batches:
            if not isinstance(batch, CandidateBatch):
                raise TypeError("FROZEN_CANDIDATE_BATCH_REQUIRED")
            if declared_sources.get(batch.filing.document_id) != SourceIdentity.from_filing(batch.filing):
                raise ValueError("CANDIDATE_SOURCE_IDENTITY_CONFLICT")
            candidates.extend(batch.candidates)
            evidence.extend(batch.evidence)
            evidence.extend(ref for item in batch.candidates for ref in item.evidence)
            diagnostics.extend(f"{batch.filing.document_id}:{message}" for message in batch.diagnostics)

        grouped: dict[tuple[str, str, str, str, str], list[FactCandidate]] = {}
        for item in candidates:
            fact = item.fact
            # A reporting currency is not a safe default for share/unit
            # observations. Monetary statement facts still enter the issuer
            # currency lane so the validator rejects an omitted currency
            # instead of silently treating it as the issuer's currency.
            monetary_statement_fact = (
                fact.concept != "reported_roe"
                and fact.statement in {"income_statement", "balance_sheet", "cash_flow"}
            )
            identity = (
                fact.accession_number,
                fact.end_date,
                (fact.fiscal_period or "FY").upper(),
                fact.consolidated_scope or fact.scope or "unknown",
                fact.currency or (subject.reporting_currency if monetary_statement_fact else ""),
            )
            grouped.setdefault(identity, []).append(item)

        validator = FinancialValidator()
        resolved: list[FinancialFact] = []
        quarantined: list[FinancialFact] = []
        validations: list[FactGroupValidation] = []
        conflicts: list[dict[str, Any]] = []
        candidate_resolutions: list[dict[str, Any]] = []
        selected_evidence: list[EvidenceRef] = []
        fact_records_by_id: dict[str, FactValidationRecord] = {}
        compiler_issues_by_fact: dict[str, list[str]] = {}

        def merge_fact_record(record: FactValidationRecord) -> None:
            previous = fact_records_by_id.get(record.fact_id)
            if previous is None:
                fact_records_by_id[record.fact_id] = record
                return
            prior_components = dict(previous.components)
            merged_components: list[tuple[str, SourceComponentObservation]] = []
            for name, observation in record.components:
                prior = prior_components.get(name)
                if prior is None:
                    merged_components.append((name, observation))
                    continue
                evidence_ids = tuple(dict.fromkeys((*prior.evidence_ids, *observation.evidence_ids)))
                if (prior.value is not None and observation.value is not None
                        and prior.value != observation.value):
                    merged_components.append((
                        name, SourceComponentObservation("conflict", observation.value, evidence_ids),
                    ))
                elif "conflict" in {prior.state, observation.state}:
                    merged_components.append((
                        name, SourceComponentObservation("conflict", observation.value, evidence_ids),
                    ))
                elif "verified" in {prior.state, observation.state}:
                    merged_components.append((
                        name, SourceComponentObservation("verified", observation.value, evidence_ids),
                    ))
                elif "observed" in {prior.state, observation.state}:
                    merged_components.append((
                        name, SourceComponentObservation("observed", observation.value, evidence_ids),
                    ))
                else:
                    merged_components.append((
                        name, SourceComponentObservation("unknown", observation.value, evidence_ids),
                    ))
            merged_dependencies = dict(previous.dependencies)
            for name, evidence_ids in record.dependencies:
                merged_dependencies[name] = tuple(dict.fromkeys(
                    (*merged_dependencies.get(name, ()), *evidence_ids)
                ))
            fact_records_by_id[record.fact_id] = FactValidationRecord(
                record.fact_id,
                previous.accepted and record.accepted,
                tuple(dict.fromkeys((*previous.issues, *record.issues))),
                tuple(merged_components),
                tuple(merged_dependencies.items()),
            )

        filing_by_accession = {filing.accession_number: filing for filing in filings}
        subject_company_ids = {
            str(value).strip()
            for value in (subject.cik, subject.security_id, subject.issuer_id)
            if str(value or "").strip()
        }

        for identity, items in grouped.items():
            filing = filing_by_accession.get(identity[0])
            source_evidence = tuple(
                ref for ref in evidence
                if filing is not None and ref.document_id == filing.document_id
            )
            expected_company_id = "__filing_identity_mismatch__"
            if filing is not None:
                filing_company_id = str(filing.company_cik or "").strip()
                expected_company_id = (
                    filing_company_id
                    if filing_company_id and filing_company_id in subject_company_ids
                    else "__filing_identity_mismatch__"
                )
            chosen: list[FactCandidate] = []
            group_quarantine: list[FinancialFact] = []
            candidate_issues: list[str] = []
            group_has_conflict = False
            # Prove all source observations before resolving either their
            # numerical duplicates or a source-specific presentation role.
            # Otherwise an invalid native row could suppress a valid source.
            defensible_all: list[FactCandidate] = []
            for option in items:
                # A proof may be internally coherent but belong to an old
                # source generation. Hash-bound original rows must also be
                # bound to the captured official filing, not just each other.
                # CompanyFacts has its own accession/QName proof and no PDF
                # hash; do not invent an original-document hash for that lane.
                bound_rows = tuple(ref for ref in option.evidence if ref.content_hash)
                unbound_original = bool(bound_rows and (filing is None or any(
                    ref.document_id != filing.document_id
                    or ref.source_url != filing.source_url
                    or ref.content_hash != filing.content_hash
                    for ref in bound_rows
                )))
                option_validation = validator.validate_group(
                    [option.fact], identity,
                    {option.fact.fact_id: ref for ref in option.evidence},
                    required_concepts={option.fact.concept},
                    expected_company_id=expected_company_id,
                    source_evidence=source_evidence,
                ).validation
                for record in option_validation.fact_records:
                    merge_fact_record(record)
                if unbound_original:
                    group_quarantine.append(option.fact)
                    candidate_issues.extend((*option_validation.issues, "source_document_identity_mismatch"))
                    compiler_issues_by_fact.setdefault(option.fact.fact_id, []).append(
                        "source_document_identity_mismatch"
                    )
                elif any(fact.fact_id == option.fact.fact_id for fact in option_validation.accepted):
                    defensible_all.append(option)
                else:
                    group_quarantine.append(option.fact)
                    candidate_issues.extend(option_validation.issues)
                    compiler_issues_by_fact.setdefault(option.fact.fact_id, []).extend(
                        option_validation.issues
                    )
            superseded = resolve_issuer_presentation(defensible_all, source_evidence, filing)
            by_concept: dict[str, list[FactCandidate]] = {}
            for option in defensible_all:
                authority = superseded.get(option.fact.fact_id)
                if authority is not None:
                    group_quarantine.append(option.fact)
                    candidate_issues.append("structured_role_contradicted_by_original_presentation")
                    compiler_issues_by_fact.setdefault(option.fact.fact_id, []).append(
                        "structured_role_contradicted_by_original_presentation"
                    )
                    candidate_resolutions.append({
                        "identity": identity, "concept": authority.fact.concept,
                        "policy": "original-issuer-presentation-v1",
                        "selected_fact_id": authority.fact.fact_id,
                        "quarantined_fact_id": option.fact.fact_id,
                        "source_candidate_ids": (authority.fact.fact_id, option.fact.fact_id),
                        "evidence_ids": authority.fact.classification_evidence_ids,
                    })
                else:
                    by_concept.setdefault(option.fact.concept, []).append(option)
            for concept, defensible in by_concept.items():
                conflict_keys = {
                    (
                        # Compatibility floats may collapse distinct original
                        # decimals (including cents above 2**53). Conflict
                        # identity must use the retained source-normalized
                        # amount, never reconstruct source precision from float.
                        Decimal(option.fact.normalization_lineage.normalized_value)
                        if option.fact.normalization_lineage is not None
                        else Decimal(str(option.fact.value)),
                        option.fact.start_date or option.fact.period_start,
                        option.fact.end_date,
                        (option.fact.fiscal_period or "FY").upper(),
                        option.fact.consolidated_scope or option.fact.scope or "unknown",
                        option.fact.currency or "",
                        option.fact.statement or "",
                        option.fact.revision,
                        option.fact.generation_id,
                    )
                    for option in defensible
                }
                if len(conflict_keys) > 1:
                    rounding = resolve_display_rounding(defensible, filing) if filing else None
                    if rounding is not None:
                        chosen.append(rounding.selected)
                        candidate_resolutions.append({
                            "identity": identity,
                            "concept": concept,
                            "policy": rounding.policy,
                            "selected_fact_id": rounding.selected.fact.fact_id,
                            "source_candidate_ids": tuple(sorted(option.fact.fact_id for option in defensible)),
                            "rounded_candidate_ids": rounding.rounded_candidate_ids,
                            "observations": tuple({
                                "fact_id": option.fact.fact_id,
                                "raw_value": option.fact.normalization_lineage.raw_value,
                                "scale": option.fact.normalization_lineage.scale,
                                "normalized_value": option.fact.normalization_lineage.normalized_value,
                            } for option in sorted(defensible, key=lambda item: item.fact.fact_id)),
                        })
                        continue
                    group_has_conflict = True
                    conflicts.append(
                        {
                            "identity": identity,
                            "concept": concept,
                            "fact_ids": tuple(option.fact.fact_id for option in defensible),
                            "reason": "conflicting_candidate_values",
                        }
                    )
                    group_quarantine.extend(option.fact for option in defensible)
                    for option in defensible:
                        compiler_issues_by_fact.setdefault(option.fact.fact_id, []).append(
                            "conflicting_candidate_values"
                        )
                    continue
                # Same-value candidates are independently auditable, but one
                # canonical fact is resolved to prevent last-write-wins drift.
                chosen.append(sorted(defensible, key=lambda option: (option.extractor, option.fact.fact_id))[0])

            group_facts = [item.fact for item in chosen]
            refs: dict[str, EvidenceRef] = {
                item.fact.fact_id: ref
                for item in chosen
                for ref in item.evidence
            }
            validation = validator.validate_group(
                group_facts,
                identity,
                refs,
                required_concepts=required_concepts,
                expected_company_id=expected_company_id,
                source_evidence=source_evidence,
            )
            for record in validation.validation.fact_records:
                merge_fact_record(record)
            accepted = tuple(validation.validation.accepted)
            rejected = tuple(group_quarantine) + tuple(validation.validation.quarantined)
            covered = frozenset(fact.concept for fact in accepted)
            missing = missing_profile_concepts(covered, required_concepts)
            issues = candidate_issues + list(validation.validation.issues)
            if missing:
                issues.append("required_profile_missing:" + ",".join(sorted(missing)))
            if group_has_conflict:
                issues.append("candidate_conflict")
            elif group_quarantine:
                issues.append("candidate_validation_failed")
            # Group conflicts and validation failures quarantine only their
            # affected facts. Verified siblings keep their evidence and can
            # support the capabilities that do not depend on the missing or
            # conflicting concepts.
            if accepted:
                status = validation.validation.status.value
                if missing or rejected:
                    status = "PARTIAL" if rejected else "INCOMPLETE"
                elif status == ValidationStatus.READY_WITH_WARNINGS.value:
                    status = "INCOMPLETE"
                # Cell admission and cohort completeness are different facts.
                # Only validator-accepted cells reach here; a missing sibling
                # cannot revoke their evidence. Extractor flags are not trusted.
                accepted = tuple(replace(fact, validation_status=ValidationStatus.VERIFIED.value)
                                 for fact in accepted)
                resolved.extend(accepted)
                quarantined.extend(
                    replace(fact, validation_status=ValidationStatus.REJECTED.value) for fact in rejected
                )
                accepted_ids = {fact.fact_id for fact in accepted}
                selected_evidence.extend(
                    ref for fact_id, ref in refs.items() if fact_id in accepted_ids
                )
                selected_unit_ids = {
                    evidence_id
                    for fact in accepted
                    if fact.normalization_lineage is not None
                    for evidence_id in fact.normalization_lineage.unit_evidence_ids
                }
                selected_evidence.extend(
                    ref for ref in source_evidence
                    if ref.evidence_id in selected_unit_ids
                )
            else:
                status = (
                    "CONFLICTED" if group_has_conflict
                    else ValidationStatus.REJECTED.value if rejected
                    else "INCOMPLETE"
                )
                rejected_facts = tuple(rejected or tuple(group_facts))
                # Facts outside the requested profile (for example a
                # non-period-end cover-date share count) remain available for
                # audit/valuation provenance. They are never target facts and
                # therefore cannot satisfy or block the research gate.
                if (
                    rejected_facts
                    and not group_quarantine
                    and not validation.validation.quarantined
                    and not set(fact.concept for fact in rejected_facts).intersection(required_concepts)
                ):
                    resolved.extend(
                        replace(fact, validation_status=status)
                        for fact in rejected_facts
                    )
                else:
                    quarantined.extend(
                        replace(fact, validation_status=status)
                        for fact in rejected_facts
                    )
            # A separately extracted prior-year note/ratio can share the
            # comparative group's period without carrying the AST's usage
            # annotation. It must not promote that whole group into a later
            # authoritative annual filing and displace the original year.
            source_end = str(filing.period_end or "")[:10] if filing is not None else ""
            annotated_comparison = any(fact.usage_status == "comparator" for fact in group_facts)
            prior_source_period = bool(source_end and identity[1][:10] < source_end
                and identity[2].upper() == (filing.fiscal_period or "FY").upper()
                and identity[1][5:10] == source_end[5:10]) if filing is not None else False
            comparison_role = bool(group_facts and (
                all(fact.usage_status == "comparator" for fact in group_facts)
                or (annotated_comparison and prior_source_period)))
            validations.append(
                FactGroupValidation(
                    identity, status, tuple(dict.fromkeys(issues)), covered,
                    accepted, rejected or (() if accepted else tuple(group_facts)),
                    "comparator" if comparison_role else "target",
                )
            )
        def identity_has_valid_disclosure_date(identity: tuple[str, str, str, str, str]) -> bool:
            filing = filing_by_accession.get(identity[0])
            group_facts = tuple(
                fact
                for item in validations
                if item.identity == identity
                for fact in item.accepted
            )
            # An extractor may resolve a provisional discovery manifest from
            # the statement itself.  Prefer that source-supported fact identity
            # over stale provider metadata, while still requiring an observed
            # date no later than filed_at.
            if group_facts:
                supported = [
                    fact for fact in group_facts
                    if str(fact.revision or "").casefold() != "period_end_provisional"
                    and str(fact.end_date or "")[:10] == identity[1][:10]
                ]
                if supported:
                    return all(
                        not (
                            len(str(fact.end_date or "")[:10]) == 10
                            and len(str(fact.filed_at or "")[:10]) == 10
                            and str(fact.end_date)[:10] > str(fact.filed_at)[:10]
                        )
                        for fact in supported
                    )
            if filing is None:
                return True
            # A provider-supplied annual-looking 12/31 on an interim filing is
            # provisional and cannot become a research fact without an observed
            # statement date.  Never pass a date later than the filing date to
            # the compiler either.
            if str(getattr(filing, "revision", "") or "").casefold() == "period_end_provisional":
                return False
            end = str(identity[1] or "")[:10]
            filed = str(getattr(filing, "filed_at", "") or "")[:10]
            return not (len(end) == 10 and len(filed) == 10 and end > filed)

        def target_identity(identity: tuple[str, str, str, str, str]) -> bool:
            accession, end_date, fiscal_period, scope, currency = identity
            if fiscal_period.upper() != target_fiscal_period:
                return False
            filing = filing_by_accession.get(accession)
            if filing is not None and filing.period_end:
                if end_date[:10] != str(filing.period_end)[:10]:
                    return False
            return identity_in_scope(identity)

        def identity_in_scope(identity: tuple[str, str, str, str, str]) -> bool:
            _accession, end_date, _fiscal_period, scope, currency = identity
            if scope.strip().lower() != target_scope:
                return False
            if target_currency and currency.strip().upper() != target_currency:
                return False
            if not identity_has_valid_disclosure_date(identity):
                return False
            if range_start and end_date[:10] < range_start:
                return False
            if range_end and end_date[:10] > range_end:
                return False
            return True

        def is_same_filing_comparator(item: FactGroupValidation) -> bool:
            """Identify the hidden comparative column without treating it as a filing."""
            return item.role == "comparator"

        def comparator_identity_compatible(item: FactGroupValidation) -> bool:
            """Apply target scope/currency/date rules without annual range filtering."""
            identity = item.identity
            filing = filing_by_accession.get(identity[0])
            filing_end = str(getattr(filing, "period_end", "") or "")[:10]
            comparator_end = str(identity[1] or "")[:10]
            return (
                identity[3].strip().lower() == target_scope
                and (not target_currency or identity[4].strip().upper() == target_currency)
                and identity_has_valid_disclosure_date(identity)
                # Same-filing comparisons must represent the same fiscal
                # period shape (for example H1 against H1), not an unrelated
                # quarter or a stale period silently relabeled as FY.
                and (
                    not filing_end
                    or len(comparator_end) < 10
                    or comparator_end[5:] == filing_end[5:]
                    or (item.accepted and all(fact.statement == "balance_sheet"
                                             for fact in item.accepted)
                        and comparator_end.endswith("-12-31")
                        and comparator_end < filing_end)
                )
            )

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
        def authority_key(item: FactGroupValidation) -> tuple[int, str, str]:
            filing = filing_by_accession.get(item.identity[0])
            if filing is None:
                return 0, "", item.identity[0]
            revision = str(getattr(filing, "revision", "") or "").casefold()
            revision_rank = 1 if revision not in {
                "", "original", "orig", "period_end_provisional",
                "period_end_verified", "period_header_conflict",
            } else 0
            return revision_rank, str(getattr(filing, "filed_at", "") or ""), item.identity[0]

        def select_latest_per_identity(items: Iterable[FactGroupValidation], limit: int | None = None) -> tuple[FactGroupValidation, ...]:
            # A correction/mirror is one fiscal cohort, not another year.  Keep
            # the authoritative member while retaining all other facts in the
            # audit-resolved view below.
            by_cohort: dict[tuple[str, str, str], FactGroupValidation] = {}
            for item in items:
                key = (item.identity[1][:10], item.identity[3].casefold(), item.identity[4].upper())
                current = by_cohort.get(key)
                if current is None or authority_key(item) > authority_key(current):
                    by_cohort[key] = item
            ordered = sorted(
                by_cohort.values(),
                key=lambda item: (item.identity[1][:10], authority_key(item)),
                reverse=True,
            )
            return tuple(ordered[:limit] if limit is not None else ordered)

        usable_group_statuses = {
            ValidationStatus.VERIFIED.value,
            ValidationStatus.READY_WITH_WARNINGS.value,
            "PARTIAL",
            "INCOMPLETE",
        }
        annual_candidates = tuple(
            item for item in validations
            if item.status in usable_group_statuses
            and item.accepted
            and item.identity[2].upper() == "FY"
            and identity_in_scope(item.identity)
            and not is_same_filing_comparator(item)
        )
        # Five displayed years plus one hidden comparison cohort. Older
        # verified groups remain in resolved_facts as audit_only.
        annual_validations = select_latest_per_identity(annual_candidates, limit=6)
        interim_all_candidates = tuple(
            item for item in validations
            if item.identity[2].upper() != "FY"
            and identity_in_scope(item.identity)
            and not is_same_filing_comparator(item)
        )
        interim_candidates = tuple(
            item for item in interim_all_candidates
            if item.status in usable_group_statuses
            and item.accepted
        )
        # Select one globally newest interim cohort.  Per-period selection
        # would incorrectly mix Q1/H1/Q3 into a single research snapshot.
        latest_interim_validation = (
            max(
                interim_all_candidates,
                key=lambda item: (item.identity[1][:10], authority_key(item)),
            ) if interim_all_candidates else None
        )
        if (
            latest_interim_validation is not None
            and (
                latest_interim_validation.status != ValidationStatus.VERIFIED.value
                or not concepts_cover_profile(
                    (fact.concept for fact in latest_interim_validation.accepted),
                    required_concepts,
                )
            )
        ):
            diagnostics.append("latest_interim_incomplete")
        interim_validations = (latest_interim_validation,) if latest_interim_validation else ()
        # Same-filing comparative columns have a distinct prior-period group.
        # Keep them in the hidden comparator lane even when the containing
        # filing is the only discovered document for that period.
        comparator_validations: tuple[FactGroupValidation, ...] = tuple(
            item for item in validations
            if item.status in usable_group_statuses and item.accepted
            and is_same_filing_comparator(item)
            and comparator_identity_compatible(item)
        )
        def comparator_targets_research_lane(item: FactGroupValidation) -> bool:
            identity = item.identity
            return (
                identity[2].upper() == target_fiscal_period
                and identity[3].strip().lower() == target_scope
            )

        incompatible_same_filing_comparator = any(
            is_same_filing_comparator(item)
            and comparator_targets_research_lane(item)
            and not comparator_identity_compatible(item)
            for item in validations
        )
        if incompatible_same_filing_comparator:
            diagnostics.append("same_filing_comparator_identity_mismatch")
        interim_comparator_complete = latest_interim_validation is None
        if latest_interim_validation is not None:
            latest_period = latest_interim_validation.identity[2].upper()
            latest_year = int(latest_interim_validation.identity[1][:4])
            same_filing_prior = tuple(
                item for item in comparator_validations
                if item.identity[0] == latest_interim_validation.identity[0]
                and item.identity[2].upper() == latest_period
                and item.identity[1][:4].isdigit()
                and int(item.identity[1][:4]) == latest_year - 1
            )
            if same_filing_prior:
                interim_comparator_complete = True
            prior_all = tuple(
                item for item in interim_all_candidates
                if item.identity[2].upper() == latest_period
                and item.identity[1][:4].isdigit()
                and int(item.identity[1][:4]) == latest_year - 1
            )
            prior_verified = tuple(
                item for item in prior_all
                if item.status in usable_group_statuses and item.accepted
            )
            if prior_verified:
                # Retain genuine alternative versions for each metric's
                # conflict check; do not hide one behind a group-wide winner.
                for prior in prior_verified:
                    if not any(item.identity == prior.identity for item in comparator_validations):
                        comparator_validations = (*comparator_validations, prior)
                interim_comparator_complete = True
            elif not same_filing_prior:
                interim_comparator_complete = False

        def cohort_facts(items: Iterable[FactGroupValidation], usage: str) -> tuple[FinancialFact, ...]:
            return tuple(
                replace(
                    fact,
                    validation_status=ValidationStatus.VERIFIED.value,
                    extraction_status="extracted",
                    usage_status=usage,
                    provenance_status="verified",
                    derived_version=CURRENT_DERIVED_VERSION,
                )
                for item in items
                if item.status in usable_group_statuses
                and item.accepted
                for fact in item.accepted
            )

        annual_facts = cohort_facts(annual_validations, "canonical_research")
        interim_facts = cohort_facts(interim_validations, "canonical_research")
        comparator_facts = cohort_facts(comparator_validations, "comparator")
        research_facts = annual_facts + interim_facts + comparator_facts
        from .financial_comparatives import build_comparison_bindings
        comparison_bindings = build_comparison_bindings(research_facts)
        if latest_interim_validation is not None:
            current_ids = {fact.fact_id for fact in interim_facts
                           if fact.statement in {"income_statement", "cash_flow"}}
            flow_bindings = [item for item in comparison_bindings
                             if item.current_fact_id in current_ids]
            interim_comparator_complete = bool(flow_bindings) and all(
                item.status == "available" for item in flow_bindings)
        unit_evidence_by_id = {ref.evidence_id: ref for batch in batches for ref in batch.evidence}

        def independently_declared_unit(fact: FinancialFact) -> bool:
            lineage = fact.normalization_lineage
            if lineage is None or not lineage.unit_evidence_ids:
                return False
            refs = [unit_evidence_by_id.get(key) for key in lineage.unit_evidence_ids]
            return all(ref is not None and literal_money_unit(ref.excerpt).proves(fact.currency, lineage.scale)
                       for ref in refs)

        # Compare unit declarations for the same concept across annual
        # cohorts.  Only exact order-of-magnitude changes are suspicious;
        # genuine business changes at one stable scale are not rejected.
        continuity_issues: list[dict[str, Any]] = []
        facts_by_concept: dict[str, list[FinancialFact]] = {}
        for fact in annual_facts:
            if fact.concept == "reported_roe" or not fact.statement:
                continue
            facts_by_concept.setdefault(fact.concept, []).append(fact)
        for concept, concept_facts in facts_by_concept.items():
            if all(independently_declared_unit(fact) for fact in concept_facts):
                # Different annual originals can legitimately change their
                # displayed unit. Admission has independently proven every
                # declaration; a power-of-ten difference is not an invariant.
                continue
            scales = sorted({Decimal(str(fact.unit_scale)) for fact in concept_facts})
            if len(scales) < 2:
                continue
            for low, high in zip(scales, scales[1:]):
                if low <= 0 or high <= 0:
                    continue
                ratio = high / low
                if any(
                    abs(ratio - Decimal(str(power))) <= Decimal("0.000001")
                    for power in (1000, 10000, 1000000)
                ):
                    continuity_issues.append({
                        "concept": concept,
                        "scales": tuple(str(scale) for scale in scales),
                        "reason": "unit_scale_order_of_magnitude_jump",
                    })
                    break
        if continuity_issues:
            diagnostics.append("unit_scale_continuity_failed")
        def comparator_unit_conflicts(comparative: FinancialFact) -> bool:
            for current in annual_facts + interim_facts:
                if ((current.accession_number, current.concept, current.statement, current.currency) !=
                        (comparative.accession_number, comparative.concept, comparative.statement, comparative.currency)):
                    continue
                if Decimal(str(current.unit_scale)) == Decimal(str(comparative.unit_scale)):
                    continue
                current_context = physical_unit_context(current, unit_evidence_by_id)
                prior_context = physical_unit_context(comparative, unit_evidence_by_id)
                if (current_context and prior_context and current_context != prior_context
                        and independently_declared_unit(current) and independently_declared_unit(comparative)):
                    continue
                # Unknown/legacy context still cannot excuse an inconsistency.
                return True
            return False

        mismatched_comparator_ids = {fact.fact_id for fact in comparator_facts
                                     if comparator_unit_conflicts(fact)}
        comparator_unit_mismatch = bool(mismatched_comparator_ids)
        if comparator_unit_mismatch:
            diagnostics.append("same_filing_comparator_unit_mismatch")
            isolated = tuple(fact for fact in comparator_facts if fact.fact_id in mismatched_comparator_ids)
            quarantined.extend(replace(fact, validation_status=ValidationStatus.REJECTED.value)
                               for fact in isolated)
            resolved = [fact for fact in resolved if fact.fact_id not in mismatched_comparator_ids]
            comparator_facts = tuple(fact for fact in comparator_facts
                                     if fact.fact_id not in mismatched_comparator_ids)
            research_facts = annual_facts + interim_facts + comparator_facts
            validations = [replace(item,
                accepted=tuple(fact for fact in item.accepted if fact.fact_id not in mismatched_comparator_ids),
                quarantined=(*item.quarantined, *(fact for fact in item.accepted
                                                if fact.fact_id in mismatched_comparator_ids)),
                status="PARTIAL", issues=(*item.issues, "same_filing_comparator_unit_mismatch"))
                if any(fact.fact_id in mismatched_comparator_ids for fact in item.accepted)
                else item for item in validations]
            for fact_id in mismatched_comparator_ids:
                compiler_issues_by_fact.setdefault(fact_id, []).append("same_filing_comparator_unit_mismatch")
            comparison_bindings = build_comparison_bindings(research_facts)
            if latest_interim_validation is not None:
                flow_bindings = [item for item in comparison_bindings if item.current_fact_id in current_ids]
                interim_comparator_complete = bool(flow_bindings) and all(
                    item.status == "available" for item in flow_bindings)
        # A comparative column is an issuer's restatement of a prior period,
        # not a replacement for the separately filed historical fact.  Keep
        # both auditable values, but surface a deterministic conflict when
        # their semantic identity differs.  Evidence location and fact IDs
        # are deliberately excluded from the matching key.
        comparator_facts_all = [
            fact for item in validations
            if is_same_filing_comparator(item)
            for fact in item.accepted
        ]
        historical_facts_all = [
            fact for item in validations
            if not is_same_filing_comparator(item)
            and item.status != ValidationStatus.REJECTED.value
            for fact in item.accepted
        ]
        restatement_conflicts: set[tuple[object, ...]] = set()
        for comparative in comparator_facts_all:
            if not str(comparative.fiscal_year or "").isdigit():
                continue
            key = (
                comparative.concept,
                int(comparative.fiscal_year),
                str(comparative.end_date or "")[:10],
                str(comparative.scope or comparative.consolidated_scope or "").casefold(),
                str(comparative.currency or comparative.unit or "").upper(),
                str(comparative.statement or "").casefold(),
                "" if comparative.statement == "balance_sheet" else str(comparative.start_date or comparative.period_start or ""),
                "" if comparative.statement == "balance_sheet" else str(comparative.fiscal_period).upper(),
                comparative.dimensions,
            )
            for historical in historical_facts_all:
                historical_key = (
                    historical.concept,
                    int(historical.fiscal_year) if str(historical.fiscal_year or "").isdigit() else -1,
                    str(historical.end_date or "")[:10],
                    str(historical.scope or historical.consolidated_scope or "").casefold(),
                    str(historical.currency or historical.unit or "").upper(),
                    str(historical.statement or "").casefold(),
                    "" if historical.statement == "balance_sheet" else str(historical.start_date or historical.period_start or ""),
                    "" if historical.statement == "balance_sheet" else str(historical.fiscal_period).upper(),
                    historical.dimensions,
                )
                if (
                    key != historical_key
                    or comparative.accession_number == historical.accession_number
                ):
                    continue
                from .financial_comparatives import comparative_amounts_agree
                if comparative_amounts_agree(comparative, historical):
                    continue
                conflict_key = (*key, comparative.accession_number)
                if conflict_key in restatement_conflicts:
                    continue
                restatement_conflicts.add(conflict_key)
                conflicts.append({
                    "identity": key,
                    "concept": comparative.concept,
                    "fact_ids": (comparative.fact_id, historical.fact_id),
                    "reason": "restatement_conflict",
                })
        if restatement_conflicts:
            diagnostics.append("same_filing_restatement_conflict")
        selected_validations_list: list[FactGroupValidation] = []
        selected_validation_keys: set[tuple[tuple[str, str, str, str, str], str]] = set()
        for item in (*annual_validations, *interim_validations, *comparator_validations):
            key = (item.identity, item.role)
            if key not in selected_validation_keys:
                selected_validation_keys.add(key)
                selected_validations_list.append(item)
        selected_validations = tuple(selected_validations_list)
        annual_ids = {fact.fact_id for fact in annual_facts}
        interim_ids = {fact.fact_id for fact in interim_facts}
        comparator_ids = {fact.fact_id for fact in comparator_facts}
        resolved_for_dataset = tuple(
            replace(
                fact,
                derived_version=CURRENT_DERIVED_VERSION,
                usage_status=(
                    "canonical_research"
                    if fact.fact_id in annual_ids or fact.fact_id in interim_ids
                    else "comparator"
                    if fact.fact_id in comparator_ids
                    else "audit_only"
                ),
            )
            for fact in resolved
        )
        quarantined_for_dataset = tuple(
            replace(fact, usage_status="quarantined") for fact in quarantined
        )
        required = set(required_concepts)
        final_accepted_ids = {
            fact.fact_id for item in validations for fact in item.accepted
        }
        final_quarantined_ids = {
            fact.fact_id for item in validations for fact in item.quarantined
        }
        fact_validation_records: dict[str, dict[str, Any]] = {}
        for fact_id, record in fact_records_by_id.items():
            accepted_record = fact_id in final_accepted_ids and fact_id not in final_quarantined_ids
            issues = tuple(dict.fromkeys((
                *record.issues,
                *compiler_issues_by_fact.get(fact_id, ()),
            )))
            components = record.components
            if not accepted_record:
                components = tuple(
                    (
                        name,
                        SourceComponentObservation("observed", observation.value, observation.evidence_ids),
                    )
                    if observation.state == "verified"
                    else (name, observation)
                    for name, observation in components
                )
            final_record = FactValidationRecord(
                fact_id, accepted_record, issues, components, record.dependencies,
            )
            fact_validation_records[fact_id] = final_record.to_dict()
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
            "fact_validation_records": fact_validation_records,
            "comparison_bindings": tuple(item.to_dict() for item in comparison_bindings),
            "scoped_issues": tuple({
                "source_accession": item.identity[0], "period_end": item.identity[1],
                "fiscal_period": item.identity[2], "scope": item.identity[3],
                "currency": item.identity[4],
                "fact_ids": tuple(fact.fact_id for fact in item.quarantined),
                "concepts": tuple(sorted({fact.concept for fact in item.quarantined})),
                "missing_concepts": tuple(sorted(missing_profile_concepts(
                    (fact.concept for fact in item.accepted), required_concepts))),
                "issues": item.issues,
                "capability_scope": "dependent_metrics_only",
            } for item in validations if item.issues),
        }
        by_fact_id = {fact.fact_id: fact for fact in resolved}
        coverage["scoped_issues"] += tuple({
            "source_accession": str(by_fact_id[item["fact_ids"][0]].accession_number),
            "period_end": str(by_fact_id[item["fact_ids"][0]].end_date),
            "fiscal_period": str(by_fact_id[item["fact_ids"][0]].fiscal_period),
            "concepts": (item["concept"],), "fact_ids": item["fact_ids"],
            "issues": ("same_filing_restatement_conflict",),
            "capability_scope": "dependent_comparisons_only",
        } for item in conflicts if item.get("reason") == "restatement_conflict"
            and item.get("fact_ids") and item["fact_ids"][0] in by_fact_id)
        if candidate_resolutions:
            coverage["candidate_resolutions"] = tuple(candidate_resolutions)
        if continuity_issues:
            coverage["unit_scale_continuity_issues"] = tuple(
                (item["concept"], item["scales"]) for item in continuity_issues
            )
        annual_years = sorted({
            int(item.identity[1][:4]) for item in annual_validations
            if item.identity[1][:4].isdigit()
        })
        annual_missing_years: list[int] = []
        if len(annual_years) > 1:
            for year in range(annual_years[-1], annual_years[0], -1):
                if year not in annual_years:
                    annual_missing_years.append(year)
        # A multi-year requested FY window must include every requested annual
        # cohort (the oldest one is the hidden YoY comparator).  A deliberately
        # single-year request remains a valid compatibility mode.
        if (
            target_fiscal_period == "FY"
            and annual_years
            and range_start
            and range_end
            and len(range_start) >= 4
            and len(range_end) >= 4
            and range_start[:4].isdigit()
            and range_end[:4].isdigit()
            and min(int(range_end[:4]), annual_years[-1]) > int(range_start[:4])
        ):
            for year in range(int(range_start[:4]), min(int(range_end[:4]), annual_years[-1]) + 1):
                if year not in annual_years and year not in annual_missing_years:
                    annual_missing_years.append(year)
            annual_missing_years.sort(reverse=True)
        if annual_missing_years:
            diagnostics.append("annual_comparator_missing:" + ",".join(map(str, annual_missing_years)))
            coverage["annual_missing_years"] = tuple(annual_missing_years)
        if latest_interim_validation is not None and not interim_comparator_complete:
            diagnostics.append("interim_comparator_missing")
            coverage["interim_comparator_status"] = "missing_or_incomplete"
        elif latest_interim_validation is not None:
            coverage["interim_comparator_status"] = "verified"
        selected_interim_complete = (
            not interim_validations
            or all(
                item.status == ValidationStatus.VERIFIED.value
                and concepts_cover_profile(
                    (fact.concept for fact in item.accepted), required_concepts
                )
                for item in interim_validations
            )
        )
        allow_ai = (
            target_groups_complete
            and selected_interim_complete
            and not annual_missing_years
            and all(
            item.status == ValidationStatus.VERIFIED.value
            for item in target_validations
            )
        )
        if continuity_issues:
            allow_ai = False
        if not allow_ai:
            diagnostics.append("compiler_quality_gate_failed")
        # Preserve every source ref for audit, while deduplicating IDs.
        unique_refs: dict[str, EvidenceRef] = {}
        for ref in (*evidence, *selected_evidence):
            unique_refs.setdefault(ref.evidence_id, ref)
        return FinancialDataset(
            filings, resolved_for_dataset, quarantined_for_dataset, tuple(unique_refs.values()),
            tuple(conflicts), tuple(validations), coverage,
            tuple(dict.fromkeys(diagnostics)), allow_ai,
            research_facts, selected_validations, (),
            annual_facts, interim_facts, comparator_facts,
        )
