"""Shared, model-free financial evidence recovery seams.

The controller deliberately does not parse documents or decide whether facts
are safe for model input.  It owns only recovery orchestration: retaining a
local filing snapshot when discovery is unavailable, identifying retryable
targets, and returning an explicit terminal state for callers and the UI.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .domain import Company, FilingDocument
from .financial_compiler import CoveragePlanner, concepts_cover_profile
from .financial_compatibility import CompatibilityPackRegistry
from .filing_selection import select_research_filings
from .storage import Storage


class RecoveryState(StrEnum):
    RESOLVED = "resolved"
    RESOLVED_STALE = "resolved_stale"
    NEEDS_CONSENT = "needs_consent"
    NEEDS_CONFIGURATION = "needs_configuration"
    RETRYABLE_EXTERNAL_FAILURE = "retryable_external_failure"
    BLOCKED_INTEGRITY = "blocked_integrity"
    CANCELLED = "cancelled"


CORE_FINANCIAL_FIELDS = (
    "revenue", "net_income", "operating_cash_flow",
    "assets", "liabilities", "equity",
)


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    state: RecoveryState
    filings: tuple[FilingDocument, ...] = ()
    targets: tuple[str, ...] = ()
    error_code: str = ""
    diagnostics: tuple[str, ...] = ()
    next_action: str = ""
    freshness: str = "fresh"
    missing_periods: tuple[str, ...] = ()
    failed_fields: tuple[str, ...] = ()
    failed_accessions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "targets": list(self.targets),
            "error_code": self.error_code,
            "diagnostics": list(self.diagnostics),
            "next_action": self.next_action,
            "freshness": self.freshness,
            "missing_periods": list(self.missing_periods),
            "failed_fields": list(self.failed_fields),
            "failed_accessions": list(self.failed_accessions),
        }


class FinancialRecoveryController:
    """Keep discovery, local evidence and repair decisions independent.

    ``discover`` always tries the supplied adapter first.  If it fails, a
    bounded local selection is returned when cached filings exist.  The
    caller still runs the normal parser/compiler quality gate; local fallback
    only removes an unnecessary network dependency.
    """

    def __init__(self, storage: Storage, *, app_version: str = __version__):
        self.storage = storage
        self.compatibility_packs = CompatibilityPackRegistry(storage.data_dir, app_version)

    def compatibility_summary(
        self, market: str, report_type: str
    ) -> dict[str, str] | None:
        """Return the active pack's safe identity for bounded diagnostics."""
        pack = self.compatibility_packs.active_for(market, report_type)
        return pack.summary() if pack is not None else None

    def compatibility_diagnostics(self, market: str, report_type: str) -> tuple[str, ...]:
        """Expose only pack identity, version, digest and trust state."""
        summary = self.compatibility_summary(market, report_type)
        if summary is None:
            return ("compatibility_pack:none",)
        return (
            f"compatibility_pack:{summary['pack_id']}@{summary['version']}",
            f"compatibility_pack_sha256:{summary['payload_sha256']}",
            f"compatibility_pack_trust:{summary['trust_status']}",
        )

    def compatibility_rules(self, company: Company, report_type: str):
        """Select immutable rules by exchange first, then broad market.

        Packs commonly target ``SSE``/``SZSE``/``HKEX`` while company.market
        intentionally remains the broader ``CN_A``/``HK`` identity.
        """
        candidates = tuple(dict.fromkeys(
            value for value in (company.exchange, company.market) if value
        ))
        for candidate in candidates:
            rules = self.compatibility_packs.rules_snapshot(candidate, report_type)
            if rules.active:
                return rules
        return self.compatibility_packs.rules_snapshot(company.market or company.exchange, report_type)

    def local_filings(self, company: Company, *, annual_limit: int) -> tuple[FilingDocument, ...]:
        # SEC rows are keyed by CIK; A/H rows by security id. Prefer a CIK
        # snapshot when one exists, otherwise use the listing key.
        stored = self.storage.get_filings(company.cik)
        if not stored and company.security_id != company.cik:
            stored = self.storage.get_filings(company.security_id)
        if not stored:
            return ()
        try:
            selected = tuple(select_research_filings(stored, annual_limit=annual_limit).documents)
            # The shared selector uses the A/H canonical form name.  SEC
            # adapters legitimately use 10-K/20-F/40-F, so do not collapse a
            # valid SEC snapshot into an empty result merely because its form
            # label is different.
            if not selected:
                selected = tuple(
                    item for item in stored
                    if str(item.form_type).upper() in {"10-K", "20-F", "40-F"}
                )[: max(1, int(annual_limit)) + 1]
            return selected
        except (TypeError, ValueError):
            return tuple(stored[: max(1, annual_limit)])

    def discover(
        self,
        adapter: Any,
        company: Company,
        *,
        annual_limit: int,
        force: bool = False,
    ) -> RecoveryOutcome:
        try:
            loader = getattr(adapter, "list_financial_filings", None)
            if loader is None:
                loader = getattr(adapter, "list_annual_filings", None)
            if loader is None:
                raise AttributeError("financial discovery adapter has no listing method")
            candidates = loader(company, limit=max(1, int(annual_limit)) + 3)
            if not candidates:
                return RecoveryOutcome(
                    RecoveryState.RETRYABLE_EXTERNAL_FAILURE,
                    error_code="NO_FILINGS_AVAILABLE",
                    diagnostics=("official_discovery_returned_no_filings",),
                    next_action="retry_discovery",
                )
            selected = tuple(
                select_research_filings(candidates, annual_limit=max(1, int(annual_limit))).documents
            )
            if not selected:
                selected = tuple(candidates)[: max(1, int(annual_limit)) + 1]
            return RecoveryOutcome(
                RecoveryState.RESOLVED,
                filings=selected,
                targets=tuple(item.accession_number for item in selected if item.accession_number),
                next_action="continue_local_validation",
            )
        except Exception as exc:
            code = self.classify_error(exc, stage="discovery")
            local = self.local_filings(company, annual_limit=annual_limit)
            if local:
                valid = tuple(item for item in local if self._cache_valid(item))
                targets = tuple(item.accession_number for item in local if item.accession_number)
                missing, failed_fields, failed_accessions = self._local_quality(
                    company, local, annual_limit=annual_limit
                )
                diagnostics = (
                    "official_discovery_failed_using_local_snapshot",
                    f"discovery_exception:{type(exc).__name__}",
                )
                if valid and len(valid) == len(local) and not missing and not failed_accessions and not failed_fields:
                    return RecoveryOutcome(
                        RecoveryState.RESOLVED_STALE,
                        filings=local,
                        targets=targets,
                        error_code=code,
                        diagnostics=diagnostics,
                        next_action="continue_local_validation",
                        freshness="freshness_unverified",
                        missing_periods=missing,
                        failed_fields=failed_fields,
                        failed_accessions=failed_accessions,
                    )
                return RecoveryOutcome(
                    RecoveryState.BLOCKED_INTEGRITY if code == "FILING_CONTENT_INTEGRITY_FAILED" else RecoveryState.RETRYABLE_EXTERNAL_FAILURE,
                    filings=local,
                    targets=targets,
                    error_code=code,
                    diagnostics=diagnostics + (
                        "local_filings_available_for_reparse" if valid else "local_filings_not_cache_valid",
                    ),
                    next_action="retry_local_parse" if valid else ("inspect_local_file" if code == "FILING_CONTENT_INTEGRITY_FAILED" else "retry_discovery"),
                    freshness="freshness_unverified" if valid else "unknown",
                    missing_periods=missing,
                    failed_fields=failed_fields,
                    failed_accessions=failed_accessions,
                )
            return RecoveryOutcome(
                RecoveryState.RETRYABLE_EXTERNAL_FAILURE,
                error_code=code,
                diagnostics=("official_discovery_failed_no_local_snapshot",),
                next_action="retry_discovery",
                freshness="unknown",
            )

    @staticmethod
    def _cache_valid(filing: FilingDocument) -> bool:
        path = Path(str(filing.local_path or ""))
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        expected = str(filing.content_hash or "").strip().lower()
        if not expected:
            return True
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return False
        return digest.hexdigest().lower() == expected

    def _local_quality(
        self, company: Company, filings: Sequence[FilingDocument], *, annual_limit: int
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Return window gaps, missing fields, and accessions needing repair."""
        key = company.cik if self.storage.get_filings(company.cik) else company.security_id
        groups = self.storage.get_validation_groups(key)
        facts = self.storage.get_facts(key)
        required = CoveragePlanner().plan(company).required_concepts
        failed_fields: set[str] = set()
        failed_accessions: set[str] = set()
        for filing in filings:
            accession = str(filing.accession_number)
            eligible_groups = [
                item for item in groups
                if str(item.get("accession_number", "")) == accession
                and str(item.get("status", "")).upper() == "VERIFIED"
                and str(item.get("consolidated_scope", "")).lower() == "consolidated"
                and str(item.get("currency", "")).upper() == str(company.reporting_currency).upper()
            ]
            covered: set[str] = set()
            for group in eligible_groups:
                covered.update(str(item) for item in group.get("covered_concepts", []))
            # The persisted group is a validation summary, not the complete
            # fact list. Merge only non-rejected, verified facts from the same
            # accession/scope/currency so total_equity can satisfy equity via
            # the compiler's declared alias.
            for fact in facts:
                if (
                    str(fact.get("accession_number", "")) == accession
                    and str(fact.get("validation_status", "")).upper() == "VERIFIED"
                    and str(fact.get("fiscal_period", "FY")).upper() == "FY"
                    and str(fact.get("consolidated_scope", fact.get("scope", ""))).lower() == "consolidated"
                    and str(fact.get("currency", "")).upper() == str(company.reporting_currency).upper()
                    and str(fact.get("end_date", "")) == str(filing.period_end)
                ):
                    covered.add(str(fact.get("concept", "")))
            if not eligible_groups:
                failed_accessions.add(accession)
            if not concepts_cover_profile(covered, required):
                missing = set(required)
                if "equity" in missing and "total_equity" in {item.lower() for item in covered}:
                    missing.discard("equity")
                failed_fields.update(missing)
            for group in groups:
                if str(group.get("accession_number", "")) != accession:
                    continue
                failed_fields.update(str(item) for item in group.get("issues", []))

        years = sorted({
            int(str(item.period_end)[:4]) for item in filings
            if str(item.period_end)[:4].isdigit()
        }, reverse=True)
        missing: set[str] = set()
        if years:
            expected = range(years[0], years[0] - max(1, int(annual_limit)), -1)
            present = set(years)
            missing.update(str(year) for year in expected if year not in present)
        return tuple(sorted(missing, reverse=True)), tuple(sorted(failed_fields)), tuple(sorted(failed_accessions))

    @staticmethod
    def classify_error(value: Any, *, stage: str = "") -> str:
        """Map provider/parser exceptions to a stable, user-facing catalog code.

        Exception class names and messages are intentionally never returned to
        the UI.  They may be retained by callers in bounded diagnostics only.
        """
        known = {
            "FILING_FETCH_FAILED", "FILING_DISCOVERY_FAILED", "FILING_STATUS_UNVERIFIED", "NO_FILINGS_AVAILABLE",
            "FILING_DOWNLOAD_FAILED", "FILING_LOCAL_PARSE_FAILED", "FILING_RULE_UNSUPPORTED",
            "FILING_CONTENT_INTEGRITY_FAILED", "FILING_CONTENT_UNSAFE",
            "VISION_CONSENT_REQUIRED", "VISION_MODEL_REQUIRED", "VISION_UNAUTHORIZED",
            "VISION_EXTERNAL_FAILED", "VISION_NETWORK_ERROR", "VISION_TIMEOUT",
            "VISION_RATE_LIMITED", "FILING_DATA_QUALITY_FAILED",
        }
        text = str(getattr(value, "code", value) or "").strip().upper()
        if text in known:
            if text in {"FILING_CONTENT_UNSAFE"}:
                return "FILING_CONTENT_INTEGRITY_FAILED"
            if text in {"VISION_NETWORK_ERROR", "VISION_TIMEOUT", "VISION_RATE_LIMITED"}:
                return "VISION_EXTERNAL_FAILED"
            return text
        message = str(value or "").upper()
        if any(token in message for token in ("HASH", "INTEGRITY", "CHECKSUM", "CONTENT_UNSAFE")):
            return "FILING_CONTENT_INTEGRITY_FAILED"
        if any(token in message for token in ("CONSENT", "APPROVAL")) and "VISION" in message:
            return "VISION_CONSENT_REQUIRED"
        if any(token in message for token in ("UNAUTHORIZED", "AUTHENTICATION", "CREDENTIAL")) and "VISION" in message:
            return "VISION_UNAUTHORIZED"
        if any(token in message for token in ("TIMEOUT", "TIMED OUT", "RATE LIMIT", "TOO MANY REQUEST", "NETWORK")):
            return "VISION_EXTERNAL_FAILED" if "VISION" in message else "FILING_FETCH_FAILED"
        return {
            "discovery": "FILING_FETCH_FAILED",
            "download": "FILING_DOWNLOAD_FAILED",
            "parse": "FILING_LOCAL_PARSE_FAILED",
        }.get(stage, "FILING_FETCH_FAILED")

    _catalog_error_code = classify_error
