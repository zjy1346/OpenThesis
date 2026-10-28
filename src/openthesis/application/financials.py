"""One official-source financial preparation use case for first runs and repairs.

The transport supplies captured collaborators and preferences. This module
owns discovery, immutable acquisition, canonical preparation and publication;
it does not parse tables or decide numeric validity itself.
"""
from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable
import uuid

from ..domain import Company, EvidenceRef, FilingDocument, FinancialFact
from ..financial_admission import FinancialResearchView, ReadinessLevel
from ..financial_contracts import CompilationPolicy, GapStageKind
from ..financial_compiler import CoveragePlanner, concepts_cover_profile
from ..financial_pipeline import FinancialResearchSnapshot, disclosed_reporting_currency
from ..financial_taxonomy import FINANCIAL_MAPPING_VERSION, cost_row_mentions
from ..market_financials import FinancialValidation, ValidationStatus
from ..financial_contracts import FinancialGroupValidation
from ..financial_recovery import FinancialRecoveryController
from ..financial_recovery import RecoveryOutcome as DiscoveryOutcome, RecoveryState as DiscoveryState
from ..resilience.contracts import RecoveryOutcome, RecoveryState
from .financial_outcomes import preparation_outcome, unavailable_outcome
from ..financial_sources.recovery import DurableOfficialSource
from ..resilience.contracts import RecoveryLimits


_FINANCIAL_MAPPING_GENERATION_MARKER = f"financial-mapping:{FINANCIAL_MAPPING_VERSION}"


def _publication_fact_key(fact):
    """Canonical amount identity, not a parser/row/multiplier identity.

    Values here have already passed the compiler's unit normalization. A
    differently scaled source row must not disguise a conflicting base amount.
    """
    return (
        fact.accession_number, fact.concept, fact.period_start or fact.start_date,
        fact.end_date, str(fact.fiscal_period).upper(), str(fact.currency).upper(),
        fact.consolidated_scope,
        tuple(sorted((item.axis, item.member, item.kind) for item in fact.dimensions)),
    )


def _financial_mapping_generation_id() -> str:
    """Persist the local semantic-mapping dependency with each active head."""

    return f"{uuid.uuid4().hex}:{_FINANCIAL_MAPPING_GENERATION_MARKER}"


@dataclass(frozen=True, slots=True)
class OfficialFinancialRequest:
    company: Company
    source: Any
    candidate_sources: tuple[Any, ...] = ()
    annual_history_years: int = 5
    download_filings: bool = True
    force: bool = False
    run_id: str = ""
    vision_fallback: Any = None
    vision_config: Any = None
    authorized_source_stages: frozenset[GapStageKind] = frozenset()
    research_as_of: str | None = None
    repair: bool = False
    recovery_targets: frozenset[str] = frozenset()

    def __post_init__(self):
        if (isinstance(self.annual_history_years, bool)
            or not isinstance(self.annual_history_years, int)
            or not 1 <= self.annual_history_years <= 10):
            raise ValueError("INVALID_ANNUAL_HISTORY_WINDOW")
        object.__setattr__(self, "company", replace(self.company))
        object.__setattr__(self, "candidate_sources", tuple(self.candidate_sources))
        object.__setattr__(self, "authorized_source_stages", frozenset(self.authorized_source_stages))
        object.__setattr__(self, "recovery_targets", frozenset(self.recovery_targets))


@dataclass(frozen=True, slots=True)
class OfficialFinancialPreparation:
    company: Company
    filings: tuple[FilingDocument, ...]
    view: FinancialResearchView
    discovery: Any
    errors: tuple[str, ...]
    publication: dict[str, Any] | None
    downloaded_accessions: tuple[str, ...] = ()
    currency_lane_ambiguous: bool = False
    target_accessions: tuple[str, ...] = ()
    reused_accessions: tuple[str, ...] = ()
    recovery_outcome: RecoveryOutcome | None = None
    financial_generations: tuple[tuple[str, str], ...] = ()


class OfficialFinancialUnavailable(RuntimeError):
    def __init__(self, code: str, *, outcome: RecoveryOutcome | None = None, diagnostics=()):
        self.code = code
        self.outcome = outcome or unavailable_outcome(code)
        self.diagnostics = tuple(diagnostics)
        super().__init__(code)


class OfficialFinancialPreparer:
    def __init__(self, storage: Any, discovery: Any, acquisition: Any, pipeline: Any):
        self.storage = storage
        self.discovery = discovery
        self.acquisition = acquisition
        self.pipeline = pipeline

    def prepare(
        self, request: OfficialFinancialRequest, *,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[..., None] | None = None,
    ) -> OfficialFinancialPreparation:
        if not isinstance(request, OfficialFinancialRequest):
            raise TypeError("CAPTURED_OFFICIAL_FINANCIAL_REQUEST_REQUIRED")
        cancelled = cancel_check or (lambda: False)
        notify = progress or (lambda *_args: None)

        def check_cancelled():
            if cancelled():
                raise CancelledError("financial preparation cancelled")

        check_cancelled()
        subject = request.company
        storage_key = subject.cik if subject.market == "US" else subject.security_id
        initial_generations = self.storage.get_financial_research_snapshot(storage_key)["generations"]
        source = request.source
        candidate_sources = request.candidate_sources
        if request.run_id:
            wrappers = {}
            def wrap(original):
                key = id(original)
                if key not in wrappers:
                    wrappers[key] = DurableOfficialSource(original, self.storage, request.run_id, subject,
                        cancel_check=cancelled, limits=RecoveryLimits(calls=32,
                            pages=getattr(request.vision_config, "max_pages", 20)))
                return wrappers[key]
            source = wrap(source)
            candidate_sources = tuple(item.with_recovery_source(wrap)
                if hasattr(item, "with_recovery_source") else item for item in candidate_sources)
        cached_view = None
        discovered = None
        if request.repair and not request.force and not request.recovery_targets:
            local = tuple(self.discovery.local_filings(subject, annual_limit=request.annual_history_years))
            if local and all(self.acquisition.verified_cache(item) is not None for item in local):
                check_cancelled()
                local_view = self._active_view(subject, local, request, cancelled)
                if (local_view.model_ready and not local_view.profile.period_coverage.get("missing_or_rejected_years")
                        and self._healthy_accessions(subject, local, local_view) == {item.accession_number for item in local}):
                    cached_view = local_view
                    discovered = DiscoveryOutcome(DiscoveryState.RESOLVED_STALE, filings=local,
                        freshness="freshness_unverified", next_action="continue_local_validation",
                        diagnostics=("verified_local_cohort_reused_without_remote_refresh",))
        if discovered is None:
            discovered = self.discovery.discover(source, subject,
                annual_limit=request.annual_history_years, force=request.force)
        check_cancelled()
        if not discovered.filings:
            raise OfficialFinancialUnavailable(discovered.error_code or "NO_FILINGS_AVAILABLE",
                diagnostics=discovered.diagnostics)
        filings = tuple(replace(item) for item in discovered.filings)
        notify("filing-discovery", 1, 1)
        errors = []
        downloaded_accessions = ()
        stored_filings = self.storage.get_filings(storage_key)
        if request.download_filings:
            acquired = self.acquisition.acquire(source, filings,
                self.storage.filings_dir / storage_key.replace(":", "_"),
                stored_filings=stored_filings, cancel_check=cancelled,
                progress=lambda current, total: notify("filing-download", current, total))
            if acquired.cancelled:
                raise CancelledError("financial acquisition cancelled")
            acquired_by_accession = {item.accession_number: item for item in acquired.filings}
            downloaded_accessions = tuple(item.accession_number for item in acquired.downloaded)
            # Keep the requested official identities when byte acquisition
            # fails. An independently validated structured source can still
            # repair those periods; corrupt/missing bytes are not parsed.
            filings = tuple(acquired_by_accession.get(item.accession_number,
                replace(item, local_path="", content_hash="")) for item in filings)
            for filing, error in acquired.errors:
                code = FinancialRecoveryController.classify_error(error, stage="download")
                errors.append(f"{filing.accession_number}:download:{code}")
                if request.run_id:
                    self.storage.save_financial_recovery_case(
                        run_id=request.run_id, company_cik=storage_key,
                        accession_number=filing.accession_number, document_hash=filing.content_hash,
                        stage="filing-download", error_code=code, next_action="retry_download")
        else:
            stored = {item.accession_number: item for item in stored_filings}
            filings = tuple(self.acquisition.verified_cache(item, stored.get(item.accession_number))
                            or replace(item, local_path="", content_hash="") for item in filings)
        check_cancelled()
        if not filings:
            raise OfficialFinancialUnavailable("FILING_DOWNLOAD_FAILED")
        self.storage.save_filings(list(filings))
        notify("filing-acquisition-complete", len(filings), len(filings), filings)
        parse_targets = filings
        if request.recovery_targets - {item.accession_number for item in filings}:
            raise OfficialFinancialUnavailable("FILING_RECOVERY_TARGET_MISSING")
        if request.repair and not request.force:
            cached_view = cached_view or self._active_view(subject, filings, request, cancelled)
            healthy = self._healthy_accessions(subject, filings, cached_view)
            parse_targets = tuple(item for item in filings
                if item.accession_number in request.recovery_targets
                or item.accession_number not in healthy)
        target_accessions = {item.accession_number for item in parse_targets}
        reused_accessions = tuple(item.accession_number for item in filings
                                  if item.accession_number not in target_accessions)
        notify("filing-parse", 0, len(parse_targets))
        # A failed or empty structured endpoint never prevents native originals
        # from being collected. First research and force-rebuild use this exact
        # same candidate/compiler/admission interface and full annual window.
        view = self.pipeline.prepare(FinancialResearchSnapshot(
            subject, parse_targets, candidate_sources=candidate_sources,
            requested_annual_count=request.annual_history_years,
            research_as_of=request.research_as_of, cancel_check=cancelled, progress=notify,
            vision_fallback=request.vision_fallback, vision_config=request.vision_config,
            authorized_source_stages=request.authorized_source_stages,
        ), CompilationPolicy(reporting_currency=subject.reporting_currency)) if parse_targets else cached_view
        errors.extend(f"{failure.accession_number}:{failure.stage}:{failure.code}"
            for failure in view.canonical_dataset.source_failures if failure.required)
        parsing_observations = view.profile.parsing_observations
        if reused_accessions and parse_targets:
            parsed = view.canonical_dataset
            retained = cached_view.canonical_dataset
            retained_documents = {item.document_id for item in filings
                                  if item.accession_number in reused_accessions}
            view = self.pipeline.prepare(FinancialResearchSnapshot.from_facts(
                subject, filings,
                (*parsed.resolved_facts, *parsed.quarantined_facts,
                 *(fact for fact in retained.resolved_facts if fact.accession_number in reused_accessions)),
                evidence=(*parsed.evidence, *(ref for ref in retained.evidence
                                             if ref.document_id in retained_documents)),
                source_manifests=(*parsed.manifests, *retained.manifests),
                source_diagnostics=parsed.diagnostics,
                source_failures=parsed.source_failures,
                requested_annual_count=request.annual_history_years,
                research_as_of=request.research_as_of, cancel_check=cancelled,
                parsing_observations=parsing_observations,
            ), CompilationPolicy(reporting_currency=subject.reporting_currency))
        check_cancelled()
        canonical = view.canonical_dataset
        manifest_by_document = {item.document_id: item for item in canonical.manifests}
        corrected = []
        for filing in filings:
            manifest = manifest_by_document.get(filing.document_id)
            if manifest is not None:
                if manifest.content_hash != filing.content_hash:
                    raise OfficialFinancialUnavailable("FILING_CONTENT_HASH_MISMATCH")
                filing = replace(filing, form_type=manifest.form_type,
                    fiscal_period=manifest.fiscal_period, period_end=manifest.period_end,
                    revision=manifest.original_revision or filing.revision,
                    period_metadata_status=("conflict" if manifest.revision == "period_header_conflict"
                        else "source_confirmed" if manifest.period_end else "unresolved"),
                    period_metadata_diagnostic=("FILING_PERIOD_METADATA_CONFLICT"
                        if manifest.revision == "period_header_conflict" else ""),
                    supersedes_document_id=manifest.supersedes_document_id)
            corrected.append(filing)
        filings = tuple(corrected)
        currency, currency_ambiguous = disclosed_reporting_currency(subject, canonical, filings)
        if currency != subject.reporting_currency:
            subject = replace(subject, reporting_currency=currency)
            view = self.pipeline.prepare(FinancialResearchSnapshot.from_canonical(
                subject, filings, canonical, requested_annual_count=request.annual_history_years,
                research_as_of=request.research_as_of, cancel_check=cancelled,
                source_diagnostics=canonical.diagnostics,
                parsing_observations=parsing_observations,
            ), CompilationPolicy(reporting_currency=currency))
            canonical = view.canonical_dataset
        self.storage.save_company(subject)
        self.storage.save_filings(list(filings))
        current_filings = {item.accession_number:item for item in filings}
        # The source resolver may correct captured period metadata from the
        # same original. Bind its execution metrics to that confirmed identity.
        parsing_observations = tuple({**item,
            "period_end":current_filings[item["accession_number"]].period_end,
            "fiscal_period":current_filings[item["accession_number"]].fiscal_period}
            for item in parsing_observations
            if item.get("accession_number") in current_filings
            and item.get("source_hash") == current_filings[item["accession_number"]].content_hash)
        view = self._merge_incremental_view(
            subject, filings, stored_filings, view, cached_view, request, cancelled,
            parsing_observations, initial_generations,
        )
        canonical = view.canonical_dataset
        check_cancelled()
        accepted_ids = {fact.fact_id for fact in view.facts}
        audit_only = [fact for fact in canonical.resolved_facts
                      if fact.accession_number in target_accessions and fact.fact_id not in accepted_ids]
        publication = self.storage.replace_financial_ingestion(
            storage_key, sorted(target_accessions),
            [fact for fact in view.facts if fact.accession_number in target_accessions],
            [fact for fact in canonical.quarantined_facts if fact.accession_number in target_accessions],
            compiler_validation_groups(tuple(group for group in canonical.validations
                                             if group.identity[0] in target_accessions)),
            [ref for ref in canonical.evidence if ref.document_id in {
                item.document_id for item in parse_targets}], audit_only,
            generation_id=_financial_mapping_generation_id(),
            parser_version=",".join(sorted({fact.parser_version for fact in
                (*view.facts, *canonical.quarantined_facts, *audit_only) if fact.parser_version})),
            source_hashes={item.accession_number: item.content_hash for item in filings
                           if item.accession_number in target_accessions},
            complete_accessions=set(view.publishable_accessions) & target_accessions,
            candidate_diagnostics=list(canonical.diagnostics),
            source_failures=canonical.source_failures, cancel_check=cancelled,
            fact_validation_records=canonical.coverage.get("fact_validation_records", {}),
            parsing_observations=tuple(item for item in parsing_observations
                if item.get("accession_number") in target_accessions) or tuple({
                "accession_number": filing.accession_number, "period_end": filing.period_end,
                "fiscal_period": filing.fiscal_period, "source_hash": filing.content_hash,
            } for filing in filings if filing.accession_number in target_accessions and filing.content_hash
                and any((fact.accession_number, fact.end_date, fact.fiscal_period) == (
                    filing.accession_number, filing.period_end, filing.fiscal_period)
                    for fact in (*canonical.resolved_facts, *canonical.quarantined_facts))),
            expected_generations={item.accession_number: initial_generations.get(item.accession_number, "")
                                  for item in filings},
        ) if target_accessions else None
        if publication is not None and publication.get("state") == "stale_generation":
            raise OfficialFinancialUnavailable("FINANCIAL_GENERATION_CHANGED")
        # Publication may deliberately retain a previous generation. Material
        # for research must come from committed active rows, not uncommitted
        # candidates. Retained rows are independently revalidated as well.
        committed, committed_generations = self._active_material(
            subject, filings, request, cancelled, source_manifests=canonical.manifests)
        # Preserve candidate diagnostics and quarantine separately from the
        # active research values. A retained good cache is not proof of repair.
        committed_readiness = committed.readiness
        if view.incremental_accessions:
            committed_readiness = replace(committed_readiness,
                level=(ReadinessLevel.PARTIAL if committed_readiness.level is ReadinessLevel.FULL
                       else committed_readiness.level),
                reasons=tuple(dict.fromkeys((*committed_readiness.reasons, "source_page_plan_incomplete"))),
                actions=tuple(dict.fromkeys((*committed_readiness.actions, "source_page_plan_incomplete"))))
        committed = replace(committed,
            diagnostics=tuple(dict.fromkeys((*view.diagnostics, *committed.diagnostics))),
            quarantined_facts=tuple(canonical.quarantined_facts),
            publishable_accessions=view.publishable_accessions,
            incremental_accessions=view.incremental_accessions,
            readiness=committed_readiness,
            canonical_dataset=replace(committed.canonical_dataset,
                source_failures=tuple(dict.fromkeys((*canonical.source_failures,
                    *committed.canonical_dataset.source_failures))),
                diagnostics=tuple(dict.fromkeys((*canonical.diagnostics,
                    *committed.canonical_dataset.diagnostics)))),
        )
        if currency_ambiguous:
            committed = replace(committed,
                capabilities=tuple(sorted({**committed.capability_map,
                    "reporting_currency_ambiguous": True}.items())),
                diagnostics=(*committed.diagnostics, "FINANCIAL_CURRENCY_LANE_AMBIGUOUS"))
        notify("filing-validation", len(filings), len(filings))
        if request.run_id:
            from .financial_outcomes import unavailable_outcome
            from ..financial_source_errors import LOCAL_PARSER_AVAILABILITY_CODES
            by_accession = {item.accession_number: item for item in filings}
            for failure in canonical.source_failures:
                filing = by_accession.get(failure.accession_number)
                if filing is None:
                    continue
                failed_outcome = unavailable_outcome(failure.code, stage=failure.stage)
                self.storage.save_financial_recovery_case(run_id=request.run_id, company_cik=storage_key,
                    accession_number=filing.accession_number, document_hash=filing.content_hash,
                    stage='filing-parse' if failure.stage in {'parse', 'worker', 'index'} else failure.stage,
                    error_code=failure.code, next_action=next(iter(failed_outcome.next_actions), 'repair_required_official_evidence'))
            if publication is not None and publication.get("state") != "stale_generation":
                failed_accessions = {failure.accession_number for failure in canonical.source_failures}
                validation_accessions = {
                    group.identity[0] for group in canonical.validations
                    if group.identity and group.identity[0] in target_accessions
                }
                parser_failure_codes = LOCAL_PARSER_AVAILABILITY_CODES | {
                    "FILING_LOCAL_PARSE_FAILED", "PERIOD_DECLARATION_NOT_FOUND",
                }
                validation_outcome = unavailable_outcome(
                    "FILING_DATA_QUALITY_FAILED", stage="validation")
                for case in self.storage.get_financial_recovery_cases(request.run_id):
                    accession = case.get("accession_number")
                    if (case.get("status") != "open"
                            or case.get("stage") != "filing-parse"
                            or case.get("error_code") not in parser_failure_codes
                            or accession not in target_accessions
                            or accession in failed_accessions
                            or accession not in validation_accessions
                            or accession in view.publishable_accessions):
                        continue
                    filing = by_accession.get(accession)
                    if filing is None:
                        continue
                    self.storage.save_financial_recovery_case(
                        run_id=request.run_id, company_cik=storage_key,
                        accession_number=accession,
                        document_hash=filing.content_hash,
                        pages=case.get("pages", ()), fields=case.get("fields", ()),
                        stage="filing-validation", error_code="FILING_DATA_QUALITY_FAILED",
                        next_action=next(iter(validation_outcome.next_actions),
                            "repair_required_official_evidence"),
                        status="open", diagnostics=case.get("diagnostics", {}),
                    )
        outcome = preparation_outcome(committed, publication, errors, currency_ambiguous=currency_ambiguous)
        if outcome.state != RecoveryState.COMMITTED and not errors:
            errors = [outcome.diagnostic_code]
        return OfficialFinancialPreparation(subject, filings, committed, discovered, tuple(errors), publication,
            downloaded_accessions, currency_ambiguous, tuple(sorted(target_accessions)), reused_accessions,
            outcome, committed_generations)

    def _active_view(self, subject, filings, request, cancelled, *, source_manifests=()):
        return self._active_material(subject, filings, request, cancelled,
                                     source_manifests=source_manifests)[0]

    def _active_material(self, subject, filings, request, cancelled, *, source_manifests=()):
        selected = {item.accession_number for item in filings}
        key = subject.cik if subject.market == "US" else subject.security_id
        active = self.storage.get_financial_research_snapshot(key, include_evidence=True)
        facts = tuple(FinancialFact(**row) for row in (*active["facts"], *active.get("supplemental_facts", ()))
                      if row.get("accession_number") in selected)
        evidence = tuple(EvidenceRef(**row) for row in active["evidence"]
                         if row.get("document_id") in {item.document_id for item in filings})
        view = self.pipeline.prepare(FinancialResearchSnapshot.from_facts(
            subject, filings, facts, evidence=evidence, source_manifests=source_manifests,
            requested_annual_count=request.annual_history_years,
            research_as_of=request.research_as_of, cancel_check=cancelled,
            parsing_observations=tuple(active.get("parsing_observations", ())),
        ), CompilationPolicy(reporting_currency=subject.reporting_currency))
        # Capture pointers with the rows, not after validation or a UI callback.
        # A concurrent repair may advance active heads while these immutable
        # inputs are being validated; it must not relabel the captured material.
        generations = tuple(sorted((accession, generation)
            for accession, generation in active["generations"].items() if accession in selected))
        return view, generations

    def _merge_incremental_view(
        self, subject, filings, stored_filings, view, cached_view, request, cancelled,
        parsing_observations, initial_generations,
    ):
        """Merge verified fragments at the existing compiler/publication seam.

        Page completion cannot promote facts by itself. Older admitted values
        must survive the same compiler and a partial repair cannot choose a
        conflicting replacement, even if source priority would otherwise do so.
        """
        incremental = set(view.incremental_accessions)
        if not incremental:
            return view
        cached_view = cached_view or self._active_view(subject, filings, request, cancelled)
        previous = {item.accession_number: item for item in stored_filings}
        current = {item.accession_number: item for item in filings}
        replaced_sources = {accession for accession in incremental
            if accession in initial_generations and accession in previous and accession in current
            and previous[accession].content_hash.casefold() != current[accession].content_hash.casefold()}
        if replaced_sources:
            codes = tuple(f"{item}:incremental_source_changed_preserved_previous_generation"
                for item in sorted(replaced_sources))
            view = replace(view,
                publishable_accessions=tuple(item for item in view.publishable_accessions
                    if item not in replaced_sources),
                canonical_dataset=replace(view.canonical_dataset,
                    diagnostics=tuple(dict.fromkeys((*view.canonical_dataset.diagnostics, *codes)))),
                diagnostics=tuple(dict.fromkeys((*view.diagnostics, *codes))))
        same_sources = {accession for accession in incremental
            if accession in previous and accession in current
            and previous[accession].content_hash
            and previous[accession].content_hash.casefold() == current[accession].content_hash.casefold()
            and previous[accession].period_end == current[accession].period_end
            and previous[accession].fiscal_period == current[accession].fiscal_period}
        retained = tuple(fact for fact in cached_view.facts if fact.accession_number in same_sources)
        if not retained:
            return view
        old_values = {_publication_fact_key(fact): Decimal(str(fact.value)) for fact in retained}
        conflicting = {fact.accession_number for fact in view.facts
            if (key := _publication_fact_key(fact)) in old_values
            and Decimal(str(fact.value)) != old_values[key]}
        source = view.canonical_dataset
        manifests = {item.document_id: item for item in cached_view.canonical_dataset.manifests}
        manifests.update({item.document_id: item for item in source.manifests})
        merged = self.pipeline.prepare(FinancialResearchSnapshot.from_facts(
            subject, filings, (*retained, *source.resolved_facts, *source.quarantined_facts),
            evidence=(*cached_view.canonical_dataset.evidence, *source.evidence),
            source_manifests=tuple(manifests.values()),
            source_diagnostics=source.diagnostics, source_failures=source.source_failures,
            requested_annual_count=request.annual_history_years,
            research_as_of=request.research_as_of, cancel_check=cancelled,
            parsing_observations=parsing_observations,
        ), CompilationPolicy(reporting_currency=subject.reporting_currency))
        new_values = {(_publication_fact_key(fact), Decimal(str(fact.value))) for fact in merged.facts}
        blocked = replaced_sources | conflicting | {fact.accession_number for fact in retained
            if (_publication_fact_key(fact), Decimal(str(fact.value))) not in new_values}
        diagnostics = tuple(dict.fromkeys((*merged.diagnostics,
            *(f"{item}:incremental_publication_preserved_previous_generation" for item in sorted(blocked)))))
        return replace(merged,
            publishable_accessions=tuple(item for item in merged.publishable_accessions if item not in blocked),
            incremental_accessions=tuple(sorted(incremental)),
            canonical_dataset=replace(merged.canonical_dataset,
                diagnostics=tuple(dict.fromkeys((*merged.canonical_dataset.diagnostics,
                    *(f"{item}:incremental_publication_preserved_previous_generation" for item in sorted(blocked)))))),
            diagnostics=diagnostics)

    def _healthy_accessions(self, subject, filings, view):
        pending_mappings = set(self.pipeline.pending_local_mappings(subject, filings))
        # Parse-cache files are disposable. The active generation is the
        # durable fallback: if it predates the independent cost mapping and
        # its validated facts/source labels touch either cost family, reparse
        # only that accession even when its parse cache is absent or corrupt.
        pending_mappings.update(self._pending_persisted_cost_mappings(subject, filings))
        # Having all core fields does not prove that the remaining page plan
        # completed. Keep these sources eligible for checkpointed recovery.
        incomplete_pages = {str(item.get("accession_number", ""))
            for item in view.profile.parsing_observations
            if item.get("parse_strategy") == "positioned-page-checkpoint"
            and type(item.get("completed_page_count")) is int
            and type(item.get("candidate_page_count")) is int
            and item["completed_page_count"] < item["candidate_page_count"]}
        return {
            filing.accession_number for filing in filings
            if filing.accession_number not in pending_mappings
            and filing.accession_number not in incomplete_pages
            and self.acquisition.verified_cache(filing) is not None
            and any(group.identity[0] == filing.accession_number
                    and group.identity[1] == filing.period_end
                    and group.identity[2] == filing.fiscal_period
                    and group.identity[3] == "consolidated"
                    and group.identity[4] == subject.reporting_currency
                    and concepts_cover_profile((fact.concept for fact in group.accepted),
                                               CoveragePlanner().plan(subject).required_concepts)
                    for group in view.canonical_dataset.validations)
        }

    def _pending_persisted_cost_mappings(self, subject, filings):
        key = subject.cik if subject.market == "US" else subject.security_id
        snapshot = self.storage.get_financial_research_snapshot(key, include_evidence=True)
        generations = snapshot.get("generations", {})
        by_accession: dict[str, list[dict[str, Any]]] = {}
        for fact in (*snapshot.get("facts", ()), *snapshot.get("supplemental_facts", ())):
            if isinstance(fact, dict):
                by_accession.setdefault(str(fact.get("accession_number", "")), []).append(fact)
        evidence = tuple(item for item in snapshot.get("evidence", ()) if isinstance(item, dict))
        parsing_observations = tuple(
            item for item in snapshot.get("parsing_observations", ()) if isinstance(item, dict)
        )
        # Parsing observations only prove that a source/hash/period was parsed;
        # they intentionally carry no source-row semantics. The audit view
        # supplies isolated candidate facts whose amount evidence can be bound
        # back to the exact filing document/hash before triggering a remap.
        audit_facts = tuple(self.storage.get_facts_audit(key))
        pending = set()
        for filing in filings:
            generation = str(generations.get(filing.accession_number, ""))
            if generation.endswith(_FINANCIAL_MAPPING_GENERATION_MARKER):
                continue
            facts = by_accession.get(filing.accession_number, ())
            if any(
                fact.get("concept") in {"cost_of_revenue", "total_operating_cost"}
                or any(cost_row_mentions(fact.get(field, ""))
                       for field in ("reported_concept", "raw_text"))
                for fact in facts
            ):
                pending.add(filing.accession_number)
                continue

            matching_observation = any(
                item.get("accession_number") == filing.accession_number
                and item.get("period_end") == filing.period_end
                and item.get("fiscal_period") == filing.fiscal_period
                and item.get("source_hash") == filing.content_hash
                for item in parsing_observations
            )
            for fact in audit_facts:
                if (not isinstance(fact, dict)
                        or fact.get("accession_number") != filing.accession_number
                        or not (fact.get("concept") in {"cost_of_revenue", "total_operating_cost"}
                                or any(cost_row_mentions(fact.get(field, ""))
                                       for field in ("reported_concept", "raw_text")))):
                    continue
                validation_record = fact.get("fact_validation_record", {})
                components = validation_record.get("components", {}) if isinstance(validation_record, dict) else {}
                amount = components.get("amount", {}) if isinstance(components, dict) else {}
                amount_evidence_ids = set(amount.get("evidence_ids", ())) if isinstance(amount, dict) else set()
                fact_evidence_id = f"fact:{fact.get('fact_id', '')}"
                if any(
                    ref.get("document_id") == filing.document_id
                    and ref.get("content_hash") == filing.content_hash
                    and ref.get("excerpt") == fact.get("raw_text")
                    and (ref.get("evidence_id") in amount_evidence_ids
                         or ref.get("evidence_id") == fact_evidence_id)
                    for ref in evidence
                ):
                    pending.add(filing.accession_number)
                    break

            if filing.accession_number in pending:
                continue
            # An old failed mapping may retain only its original source row,
            # with no admitted fact or model observation. Bound evidence is
            # enough to invalidate that document, never enough to admit a value.
            if any(ref.get("document_id") == filing.document_id
                   and ref.get("content_hash") == filing.content_hash
                   and cost_row_mentions(ref.get("excerpt", "")) for ref in evidence):
                pending.add(filing.accession_number)
                continue
            if not matching_observation:
                # Do not use unbound historical candidate evidence to schedule
                # work for a source that is not the observed current document.
                continue
            # Unverified visual observations can be the only durable trace of a
            # cost row when admission isolated it before facts were committed.
            # Their document and source hash are checked against this exact
            # accession; unrelated observations never fan out across filings.
            if any(
                ref.get("evidence_id", "").startswith("observation:")
                and ref.get("document_id") == filing.document_id
                and ref.get("content_hash") == filing.content_hash
                and (cost_row_mentions(ref.get("title", ""))
                     or cost_row_mentions(ref.get("excerpt", "")))
                for ref in evidence
            ):
                pending.add(filing.accession_number)
        return pending


def compiler_validation_groups(validations):
    """Only serialization compatibility; no independent acceptance rules."""
    projected = []
    for item in validations:
        name = item.status
        status = (ValidationStatus.VERIFIED if name == ValidationStatus.VERIFIED.value
                  else ValidationStatus.REJECTED if name in {ValidationStatus.REJECTED.value, "CONFLICTED"}
                  else ValidationStatus.READY_WITH_WARNINGS)
        issues = tuple(item.issues)
        if name == "INCOMPLETE" and "compiler_incomplete_profile" not in issues:
            issues = (*issues, "compiler_incomplete_profile")
        projected.append(FinancialGroupValidation(tuple(item.identity), FinancialValidation(
            status, issues, frozenset(item.covered), tuple(item.accepted), tuple(item.quarantined)), item.role))
    return projected
