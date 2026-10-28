"""Shared, model-free financial evidence recovery seams.

The controller deliberately does not parse documents or decide whether facts
are safe for model input.  It owns only recovery orchestration: retaining a
local filing snapshot when discovery is unavailable, identifying retryable
targets, and returning an explicit terminal state for callers and the UI.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from concurrent.futures import CancelledError
from enum import StrEnum
from typing import Any, Sequence

from . import __version__
from .domain import Company, FilingDocument
from .financial_compatibility import CompatibilityPackRegistry
from .filing_selection import select_research_filings
from .financial_sources.downloads import filing_cache_valid
from .storage import Storage
from .resilience.contracts import IncidentScope, digest
from .financial_source_errors import SOURCE_ERROR_ACTIONS, TRANSIENT_SOURCE_CODES


class RecoveryState(StrEnum):
    RESOLVED = "resolved"
    RESOLVED_STALE = "resolved_stale"
    NEEDS_CONSENT = "needs_consent"
    NEEDS_CONFIGURATION = "needs_configuration"
    RETRYABLE_EXTERNAL_FAILURE = "retryable_external_failure"
    EXHAUSTED_SAME_INPUT = "exhausted_same_input"
    BLOCKED_INTEGRITY = "blocked_integrity"
    CANCELLED = "cancelled"


class _EmptyOfficialIndex(RuntimeError):
    code = "NO_FILINGS_AVAILABLE"


def financial_incident_scope(company: Company) -> IncidentScope:
    # Shared by source and page recovery in a captured research run. Changing
    # a provider, page representation or strategy cannot reset this incident.
    identity = {"issuer": company.issuer_id or company.cik, "security": company.security_id}
    return IncidentScope("financial-evidence:" + digest(identity), digest(identity))


CORE_FINANCIAL_FIELDS = (
    "revenue", "net_income", "operating_cash_flow",
    "assets", "liabilities", "equity",
)



def financial_input_fingerprint(
    company: Company,
    filings: Sequence[FilingDocument],
    *,
    parser_version: str = "",
    rules_version: str = "",
    structured_source_fingerprint: str = "",
    vision_policy_fingerprint: str = "",
) -> str:
    """Build a stable, secret-free identity for one recovery input.

    The fingerprint includes document bytes (when a content hash is known),
    parser/rules identities and external-source policy.  It deliberately does
    not include credentials or raw provider responses.
    """
    documents = []
    for filing in filings:
        documents.append({
            "document_id": str(filing.document_id),
            "accession_number": str(filing.accession_number),
            "content_hash": str(filing.content_hash or "").casefold(),
            "period_end": str(filing.period_end),
            "fiscal_period": str(filing.fiscal_period),
            "revision": str(filing.revision),
        })
    material = {
        "company": str(company.security_id or company.cik),
        "market": str(company.market),
        "exchange": str(company.exchange),
        "reporting_currency": str(company.reporting_currency),
        "parser_version": str(parser_version),
        "rules_version": str(rules_version),
        "structured_source_fingerprint": str(structured_source_fingerprint),
        "vision_policy_fingerprint": str(vision_policy_fingerprint),
        "documents": sorted(documents, key=lambda item: (item["accession_number"], item["document_id"])),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    input_fingerprint: str = ""

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
            "input_fingerprint": self.input_fingerprint,
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

    def input_fingerprint(
        self,
        company: Company,
        filings: Sequence[FilingDocument],
        *,
        parser_version: str = "",
        rules_version: str = "",
        structured_source_fingerprint: str = "",
        vision_policy_fingerprint: str = "",
    ) -> str:
        return financial_input_fingerprint(
            company,
            filings,
            parser_version=parser_version,
            rules_version=rules_version,
            structured_source_fingerprint=structured_source_fingerprint,
            vision_policy_fingerprint=vision_policy_fingerprint,
        )

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
            limit = max(1, int(annual_limit)) + 3
            candidates = list(loader(company, limit=limit))
            if not candidates:
                # Empty discovery is not proof that previously captured
                # originals disappeared. Use the same identity/hash checks
                # and subsequent compiler revalidation as a transport outage.
                raise _EmptyOfficialIndex()
            # A source limit counts announcements, not fiscal years. Interim
            # reports must not push requested annual originals (and the hidden
            # YoY companion) out of discovery. Expand metadata only when the
            # source actually filled its page; never download guessed years.
            discovery_diagnostics = []
            maximum = min(128, max(16, (max(1, int(annual_limit)) + 1) * 8))
            for _ in range(3):
                annual_years = {item.period_end[:4] or item.primary_document[:4] for item in candidates
                    if item.fiscal_period.upper() == "FY"
                    and item.form_type.upper() in {"ANNUAL_REPORT", "ANNUAL", "10-K", "20-F", "40-F"}
                    and (item.period_end[:4] or item.primary_document[:4]).isdigit()}
                if len(candidates) < limit or len(annual_years) >= max(1, int(annual_limit)) + 1 or limit >= maximum:
                    break
                next_limit = min(maximum, limit * 2)
                previous_ids = {item.document_id for item in candidates}
                try:
                    expanded = list(loader(company, limit=next_limit))
                except CancelledError:
                    raise
                except Exception as expansion_error:
                    discovery_diagnostics.append("official_discovery_window_expansion_failed:"
                        + self.classify_error(expansion_error, stage="discovery"))
                    break
                by_id = {item.document_id: item for item in candidates}
                by_id.update((item.document_id, item) for item in expanded)
                candidates = list(by_id.values())
                discovery_diagnostics.append(f"official_discovery_window_expanded:{limit}:{next_limit}")
                limit = next_limit
                if set(by_id) == previous_ids or len(expanded) < next_limit:
                    break
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
                diagnostics=tuple(discovery_diagnostics),
            )
        except CancelledError:
            raise
        except Exception as exc:
            code = self.classify_error(exc, stage="discovery")
            local = self.local_filings(company, annual_limit=annual_limit)
            if local:
                valid = tuple(item for item in local if self._cache_valid(item))
                targets = tuple(item.accession_number for item in local if item.accession_number)
                # Source availability is not numerical admission. The current
                # pure compiler in OfficialFinancialPreparer revalidates facts,
                # source evidence and window coverage after this operation.
                failed_accessions = tuple(item.accession_number for item in local if item not in valid)
                diagnostics = (
                    "official_discovery_failed_using_local_snapshot",
                    f"discovery_exception:{type(exc).__name__}",
                )
                if valid and len(valid) == len(local):
                    return RecoveryOutcome(
                        RecoveryState.RESOLVED_STALE,
                        filings=local,
                        targets=targets,
                        error_code=code,
                        diagnostics=diagnostics,
                        next_action="continue_local_validation",
                        freshness="freshness_unverified",
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
        return filing_cache_valid(filing)

    @staticmethod
    def raw_error_code(value: Any) -> str:
        """Return an error code without normalizing away its source spelling."""
        code = getattr(value, "code", None)
        if code is not None and str(code):
            return str(code)
        return value if isinstance(value, str) else ""

    @staticmethod
    def classify_error(value: Any, *, stage: str = "") -> str:
        """Map provider/parser exceptions to a stable, user-facing catalog code.

        Exception class names and messages are intentionally never returned to
        the UI.  They may be retained by callers in bounded diagnostics only.
        """
        known = set(SOURCE_ERROR_ACTIONS) | TRANSIENT_SOURCE_CODES | {
            "FILING_FETCH_FAILED", "FILING_DISCOVERY_FAILED", "FILING_STATUS_UNVERIFIED", "NO_FILINGS_AVAILABLE",
            "FILING_DOWNLOAD_FAILED", "FILING_LOCAL_PARSE_FAILED", "FILING_RULE_UNSUPPORTED",
            "FILING_CONTENT_INTEGRITY_FAILED", "FILING_CONTENT_UNSAFE",
            "VISION_CONSENT_REQUIRED", "VISION_MODEL_REQUIRED", "VISION_UNAUTHORIZED",
            "VISION_EXTERNAL_FAILED", "VISION_NETWORK_ERROR", "VISION_TIMEOUT",
            "VISION_RATE_LIMITED", "FILING_DATA_QUALITY_FAILED",
            "SEC_CONTACT_CONFIGURATION_REQUIRED", "FILING_CONTENT_HASH_MISMATCH",
            "FILING_SOURCE_IDENTITY_MISMATCH", "FILING_DOWNLOAD_INCOMPLETE",
            "OFFICIAL_LOCAL_IO_UNAVAILABLE", "RECOVERY_CANDIDATE_REJECTED",
            "RECOVERY_INCIDENT_BUDGET_EXHAUSTED", "RECOVERY_INCIDENT_TIME_EXHAUSTED",
            "RECOVERY_ACTION_USAGE_EXHAUSTED", "RECOVERY_PROVIDER_CIRCUIT_OPEN",
            "OFFICIAL_CAPTURE_INTEGRITY_FAILED", "OFFICIAL_CAPTURE_MISSING",
            "OFFICIAL_RECEIPT_IDENTITY_INVALID", "RECOVERY_EXECUTION_INTERRUPTED",
            "RECOVERY_CANCELLED", "RECOVERY_RUN_NOT_FOUND",
            "OFFICIAL_SOURCE_REDIRECT_UNSAFE",
            "FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED",
        }
        raw_code = FinancialRecoveryController.raw_error_code(value)
        text = str(raw_code or value or "").strip().upper()
        if text == "REMOTE_TRANSPORT_FAILED" or text in {
            "REMOTE_HTTP_408", "REMOTE_HTTP_425", "REMOTE_HTTP_429", "REMOTE_HTTP_500",
            "REMOTE_HTTP_502", "REMOTE_HTTP_503", "REMOTE_HTTP_504"}:
            return "FILING_DOWNLOAD_FAILED" if stage == "download" else "FILING_FETCH_FAILED"
        if text in {"REMOTE_HTTP_401", "REMOTE_HTTP_403"}:
            return "OFFICIAL_SOURCE_CONFIGURATION_REQUIRED"
        if text.startswith("RECOVERY_INTERNAL_"):
            return "OFFICIAL_SOURCE_INTERNAL_FAILED"
        if text in known:
            if text in {"FILING_CONTENT_UNSAFE"}:
                return "FILING_CONTENT_INTEGRITY_FAILED"
            if text in {"VISION_NETWORK_ERROR", "VISION_TIMEOUT", "VISION_RATE_LIMITED"}:
                return "VISION_EXTERNAL_FAILED"
            return text
        # An explicit but unknown code is meaningful diagnostic data, not
        # evidence that filing discovery or download failed.
        has_explicit_code = getattr(value, "code", None) not in (None, "")
        if isinstance(value, str):
            has_explicit_code = bool(text) and "_" in text and not any(char.isspace() for char in text)
        if has_explicit_code:
            return "UNCLASSIFIED_ERROR"
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
        }.get(stage, "UNCLASSIFIED_ERROR")

    _catalog_error_code = classify_error
