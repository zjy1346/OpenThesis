"""Authoritative admission boundary for financial research inputs.

Source adapters and the compiler retain rich audit material.  This module is
the only place that turns that material into the immutable view consumed by
deterministic reporting and model research.  Keeping the decision here avoids
slightly different currency, period, and capability gates in service, retry,
and UI paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any, Sequence

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .financial_ingestion import (
    _PDF_PARSER_VERSION,
    FinancialProfile,
    build_financial_profile,
)


FINANCIAL_ADMISSION_CONTRACT_VERSION = "financial-admission-v1"


class ReadinessLevel(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    ACTION_REQUIRED = "action_required"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ResearchReadiness:
    """Typed admission result; boolean compatibility is derived from this."""

    level: ReadinessLevel
    capabilities: tuple[tuple[str, str], ...]
    reasons: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    source_document_count: int = 0

    @property
    def can_continue(self) -> bool:
        return self.level is not ReadinessLevel.BLOCKED

    @property
    def capability_map(self) -> dict[str, str]:
        return dict(self.capabilities)


@dataclass(frozen=True, slots=True)
class FinancialResearchView:
    """Immutable, admitted financial view shared by every downstream layer."""

    facts: tuple[FinancialFact, ...]
    quarantined_facts: tuple[FinancialFact, ...]
    fact_dicts: tuple[dict[str, Any], ...]
    profile: FinancialProfile
    capabilities: tuple[tuple[str, bool], ...]
    filing_assessments: tuple[Any, ...]
    evidence: tuple[EvidenceRef, ...]
    reporting_currency: str
    diagnostics: tuple[str, ...]
    readiness: ResearchReadiness
    admission_contract_version: str = FINANCIAL_ADMISSION_CONTRACT_VERSION

    @property
    def capability_map(self) -> dict[str, bool]:
        return dict(self.capabilities)

    @property
    def model_ready(self) -> bool:
        return bool(
            self.readiness.can_continue
            and self.capability_map.get("model_financial_analysis")
            and (self.profile.metrics or self.profile.interim_metrics)
            and self.facts
        )

    @property
    def research_ready(self) -> bool:
        return bool(
            self.readiness.can_continue
            and (self.model_ready or self.readiness.source_document_count > 0)
        )


class FinancialAdmission:
    """Project one canonical compiler result into research-ready material."""

    def admit(
        self,
        subject: Company,
        canonical: Any,
        *,
        selected_filings: Sequence[FilingDocument] = (),
        manifests: Sequence[Any] = (),
        evidence: Sequence[EvidenceRef] = (),
        requested_annual_count: int | None = None,
        research_as_of: str | None = None,
    ) -> FinancialResearchView:
        facts = tuple(getattr(canonical, "research_facts", ()) or ())
        quarantined_facts = tuple(getattr(canonical, "quarantined_facts", ()) or ())
        stale_pdf_facts = tuple(
            fact for fact in facts
            if fact.parser_version.startswith("financial-ingestion-ast")
            and fact.parser_version != _PDF_PARSER_VERSION
        )
        if stale_pdf_facts:
            stale_ids = {fact.fact_id for fact in stale_pdf_facts}
            facts = tuple(fact for fact in facts if fact.fact_id not in stale_ids)
            quarantined_facts = (*quarantined_facts, *stale_pdf_facts)
        # The profile compatibility type carries the same period identity and
        # role semantics as compiler validations; it is produced once here.
        group_validations = tuple(getattr(canonical, "group_validations", ()) or ())
        canonical_manifests = tuple(manifests or getattr(canonical, "manifests", ()) or ())
        profile = build_financial_profile(
            facts,
            group_validations,
            subject.reporting_currency,
            selected_filings=selected_filings,
            manifests=canonical_manifests,
            requested_annual_count=requested_annual_count,
            research_as_of=research_as_of,
        )
        capabilities = dict(getattr(canonical, "capabilities", {}) or {})
        growth_fields = (
            "revenue_growth", "operating_income_growth", "net_income_growth",
            "operating_cash_flow_growth",
        )
        capabilities["financial_snapshot"] = bool(
            facts and (profile.metrics or profile.interim_metrics)
        )
        # Derive capabilities from the admitted values. The compiler's legacy
        # complete-profile flags remain useful to trigger deterministic repair,
        # but must not hide a trend already calculable from safe partial facts.
        capabilities["annual_trend"] = any(
            metric.get(field) is not None
            for metric in profile.metrics for field in growth_fields
        )
        capabilities["interim_comparison"] = any(
            metric.get(field) is not None
            for metric in profile.interim_metrics for field in growth_fields
        )
        # The compiler's historical allow_ai flag represented every desired
        # year/metric as one all-or-nothing gate.  Admission now derives the
        # safe model capability from the verified snapshot; missing optional
        # trend/interim capabilities remain visible as typed degradation.
        capabilities["model_financial_analysis"] = bool(
            capabilities["financial_snapshot"]
        )
        diagnostics = [
            str(item) for item in getattr(canonical, "diagnostics", ()) or ()
        ]
        diagnostics.extend(
            f"stale_financial_parser_contract:{fact.fact_id}"
            for fact in stale_pdf_facts
        )
        diagnostics = tuple(dict.fromkeys(diagnostics))
        source_document_count = sum(
            1 for item in selected_filings
            if getattr(item, "accession_number", "") and getattr(item, "source_url", "")
        )
        readiness = _research_readiness(
            capabilities, profile, facts, diagnostics, source_document_count,
            bool(quarantined_facts),
        )
        return FinancialResearchView(
            facts=facts,
            quarantined_facts=quarantined_facts,
            fact_dicts=tuple(item.to_dict() for item in facts),
            profile=profile,
            capabilities=tuple(sorted(capabilities.items())),
            filing_assessments=group_validations,
            evidence=tuple(evidence or getattr(canonical, "evidence", ()) or ()),
            reporting_currency=subject.reporting_currency,
            diagnostics=diagnostics,
            readiness=readiness,
            admission_contract_version=FINANCIAL_ADMISSION_CONTRACT_VERSION,
        )

    def admit_facts(
        self,
        subject: Company,
        facts: Sequence[FinancialFact],
        *,
        validation_groups: Sequence[Any] = (),
        selected_filings: Sequence[FilingDocument] = (),
        manifests: Sequence[Any] = (),
        requested_annual_count: int | None = None,
        research_as_of: str | None = None,
    ) -> FinancialResearchView:
        """Restore the same view from already-admitted persisted facts."""

        return self.admit(
            subject,
            SimpleNamespace(
                research_facts=tuple(facts),
                validations=(),
                group_validations=tuple(validation_groups),
                manifests=tuple(manifests),
                evidence=(),
                diagnostics=(),
                capabilities={
                    "financial_snapshot": bool(facts),
                    "annual_trend": bool(facts),
                    "interim_comparison": bool(facts),
                    "model_financial_analysis": bool(facts),
                    "qualitative_research": True,
                },
            ),
            selected_filings=selected_filings,
            manifests=manifests,
            requested_annual_count=requested_annual_count,
            research_as_of=research_as_of,
        )


_HARD_BLOCK_DIAGNOSTICS = (
    "identity_conflict", "currency_conflict", "content_hash_mismatch",
    "tampered", "accounting_identity_failed", "unresolved_unit",
    "unit_scale_continuity_failed", "same_filing_comparator_unit_mismatch",
    "same_filing_comparator_identity_mismatch", "same_filing_restatement_conflict",
    "stale_financial_parser_contract",
)
_GLOBAL_BLOCK_DIAGNOSTICS = ("identity_conflict", "content_hash_mismatch", "tampered")


def _research_readiness(
    capabilities: dict[str, bool], profile: FinancialProfile,
    facts: tuple[FinancialFact, ...], diagnostics: tuple[str, ...],
    source_document_count: int = 0, quarantined: bool = False,
) -> ResearchReadiness:
    capability_states = tuple(sorted(
        (name, "available" if available else "unavailable")
        for name, available in capabilities.items()
    ))
    hard = tuple(
        item for item in diagnostics
        if any(marker in item.casefold() for marker in _HARD_BLOCK_DIAGNOSTICS)
    )
    has_financial_view = bool(facts and (profile.metrics or profile.interim_metrics))
    global_hard = tuple(
        item for item in diagnostics
        if any(marker in item.casefold() for marker in _GLOBAL_BLOCK_DIAGNOSTICS)
    )
    if global_hard:
        return ResearchReadiness(
            ReadinessLevel.BLOCKED, capability_states, global_hard,
            ("repair_or_replace_conflicting_official_evidence",),
            source_document_count,
        )
    if not has_financial_view and not source_document_count:
        return ResearchReadiness(
            ReadinessLevel.ACTION_REQUIRED, capability_states,
            ("no_admitted_financial_view",),
            ("retry_deterministic_extraction_or_configure_visual_fallback",),
            source_document_count,
        )
    missing = tuple(name for name, available in capabilities.items() if not available)
    if missing or hard or quarantined or not has_financial_view:
        states = tuple(
            (name, "available" if available else "degraded")
            for name, available in sorted(capabilities.items())
        )
        reasons = tuple(f"capability_unavailable:{name}" for name in missing)
        if hard:
            reasons = (*reasons, *(f"isolated_financial_evidence:{item}" for item in hard))
        if quarantined:
            reasons = (*reasons, "some_financial_facts_quarantined")
        if not has_financial_view:
            reasons = (*reasons, "financial_snapshot_unavailable")
        actions = tuple(f"recover:{name}" for name in missing)
        if hard:
            actions = (*actions, "repair_or_replace_conflicting_official_evidence")
        if quarantined or not has_financial_view:
            actions = (*actions, "recover_financial_snapshot")
        return ResearchReadiness(
            ReadinessLevel.PARTIAL, states, reasons, actions,
            source_document_count,
        )
    return ResearchReadiness(
        ReadinessLevel.FULL, capability_states,
        source_document_count=source_document_count,
    )
