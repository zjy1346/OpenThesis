"""Pure projections of financial postconditions onto the recovery contract.

Availability, safe admitted content and completion are distinct. A transport
error is never evidence that deterministic input has exhausted its strategies.
This module cannot accept facts, perform I/O, or schedule another attempt.
"""
from ..resilience.contracts import RecoveryOutcome, RecoveryState
from ..financial_source_errors import LOCAL_PARSER_AVAILABILITY_CODES, SOURCE_ERROR_ACTIONS
import re


_INTEGRITY = frozenset({
    "FILING_CONTENT_HASH_MISMATCH", "FILING_CONTENT_INTEGRITY_FAILED",
    "FILING_SOURCE_IDENTITY_MISMATCH", "FILING_CONTENT_UNSAFE",
    "RECOVERY_CANDIDATE_REJECTED", "OFFICIAL_CAPTURE_INTEGRITY_FAILED",
    "OFFICIAL_CAPTURE_MISSING", "OFFICIAL_RECEIPT_IDENTITY_INVALID",
    "OFFICIAL_SOURCE_REDIRECT_UNSAFE",
})
_CONFIGURATION = frozenset({
    "SEC_CONTACT_CONFIGURATION_REQUIRED", "VISION_MODEL_REQUIRED",
    "VISION_UNAUTHORIZED", "VISION_CONFIG_VALIDATION_REQUIRED", "VISION_ADAPTER_UNAVAILABLE",
    "OFFICIAL_SOURCE_CONFIGURATION_REQUIRED",
    "FILING_ACCESS_DENIED",
})
_EXTERNAL = frozenset({
    "FILING_FETCH_FAILED", "FILING_DISCOVERY_FAILED", "FILING_DOWNLOAD_FAILED",
    "FILING_DOWNLOAD_INCOMPLETE", "FILING_TIMEOUT", "RATE_LIMITED",
    "VISION_EXTERNAL_FAILED", "VISION_NETWORK_ERROR", "VISION_TIMEOUT", "VISION_RATE_LIMITED",
})


def continuity_financial_outcome(continuity):
    """Read the persisted source terminal result, never infer it from prose.

    A later model-stage outcome must not reactivate a past financial error.
    Malformed optional/legacy metadata remains absent, not a rendering error.
    """
    if not isinstance(continuity, dict):
        return None
    latest = continuity.get("latest")
    if not isinstance(latest, dict) or latest.get("stage") != "data-unavailable":
        return None
    diagnostics = latest.get("diagnostics")
    value = diagnostics.get("recovery_outcome") if isinstance(diagnostics, dict) else None
    if not isinstance(value, dict):
        return None
    actions, code, state = value.get("next_actions"), value.get("diagnostic_code"), value.get("state")
    if (not isinstance(state, str) or state not in {RecoveryState.WAITING_EXTERNAL, RecoveryState.NEEDS_ACTION,
            RecoveryState.BLOCKED_INTEGRITY, RecoveryState.EXHAUSTED, RecoveryState.FAILED_INTERNAL}
        or not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code)
        or not isinstance(actions, (list, tuple)) or not 1 <= len(actions) <= 16
        or any(not isinstance(action, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", action)
               for action in actions)):
        return None
    return {"state": state, "diagnostic_code": code, "next_actions": list(actions)}


def unavailable_outcome(code: str, *, stage: str = "discovery") -> RecoveryOutcome:
    if code in _INTEGRITY:
        return RecoveryOutcome(RecoveryState.BLOCKED_INTEGRITY,
            next_actions=("repair_or_replace_conflicting_official_evidence",), diagnostic_code=code)
    if code in _CONFIGURATION:
        action = SOURCE_ERROR_ACTIONS.get(code) or (
            "configure_sec_contact" if code.startswith("SEC_") else
            "configure_official_source" if code.startswith("OFFICIAL_") else "configure_cloud")
        return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
            next_actions=(action,),
            diagnostic_code=code)
    if code in SOURCE_ERROR_ACTIONS:
        return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
            next_actions=(SOURCE_ERROR_ACTIONS[code],), diagnostic_code=code)
    if code == "VISION_CONSENT_REQUIRED":
        return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
            next_actions=("approve_provider_pages_and_budget",), diagnostic_code=code)
    if code in {"FINANCIAL_GENERATION_CHANGED", "FILING_RECOVERY_TARGET_MISSING",
                "REPORT_REVISION_CONFLICT", "FINANCIAL_GENERATION_CONFLICT", "RESEARCH_RUN_SNAPSHOT_CONFLICT"}:
        return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
            next_actions=("refresh_recovery_plan",), diagnostic_code=code)
    if code in {"RECOVERY_INCIDENT_BUDGET_EXHAUSTED", "RECOVERY_INCIDENT_TIME_EXHAUSTED",
                "RECOVERY_ACTION_USAGE_EXHAUSTED"}:
        return RecoveryOutcome(RecoveryState.EXHAUSTED,
            next_actions=("extend_recovery_budget",), diagnostic_code=code)
    if code in _EXTERNAL:
        return RecoveryOutcome(RecoveryState.WAITING_EXTERNAL,
            next_actions=("retry_download" if stage == "download" else "retry_discovery",), diagnostic_code=code)
    if code == "NO_FILINGS_AVAILABLE":
        return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
            next_actions=("provide_official_source_or_retry_discovery",), diagnostic_code=code)
    if code in {"OFFICIAL_SOURCE_INTERNAL_FAILED", "FILING_REPORT_REFRESH_FAILED", "FINANCIAL_REPAIR_INTERNAL_ERROR"}:
        return RecoveryOutcome(RecoveryState.FAILED_INTERNAL,
            next_actions=("export_diagnostic_and_retry_with_new_plan",), diagnostic_code=code)
    if code == "OFFICIAL_LOCAL_IO_UNAVAILABLE":
        return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
            next_actions=("repair_local_data_directory_access",), diagnostic_code=code)
    return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
        next_actions=("repair_required_official_evidence",), diagnostic_code=code)


def preparation_outcome(view, publication, errors, *, currency_ambiguous=False) -> RecoveryOutcome:
    """Inspect independent compiler/admission and atomic-publication results."""
    artifact_ids = ((publication["generation_id"],) if publication and publication.get("generation_id") else ())
    pending = tuple(accession for accession, state in (publication or {}).get("accessions", {}).items()
                    if state != "promoted")
    target_groups = tuple(group for group in view.canonical_dataset.validations if group.role == "target")
    complete = bool(view.model_ready and target_groups
        and all(group.status == "VERIFIED" for group in target_groups)
        and not view.profile.period_coverage.get("missing_or_rejected_years")
        and not pending and not currency_ambiguous and not errors)
    if complete:
        # Safe numeric rows do not replace a missing original's qualitative
        # disclosures. Completion requires the selected source cohort as well.
        return RecoveryOutcome(RecoveryState.COMMITTED, candidate_artifact_ids=artifact_ids)
    # An unavailable parser process is not proof of a bad financial number.
    # Actual rejected facts/currency conflicts retain their integrity priority.
    error_codes = {error.rsplit(":", 1)[-1] for error in errors}
    local_unavailable = sorted(error_codes & LOCAL_PARSER_AVAILABILITY_CODES)
    if (local_unavailable and not error_codes & _INTEGRITY
            and not currency_ambiguous and not view.quarantined_facts):
        outcome = unavailable_outcome(local_unavailable[0])
        return RecoveryOutcome(outcome.state, candidate_artifact_ids=artifact_ids,
            next_actions=outcome.next_actions, diagnostic_code=outcome.diagnostic_code)
    if currency_ambiguous or view.readiness.level.value == "blocked":
        return RecoveryOutcome(RecoveryState.BLOCKED_INTEGRITY,
            candidate_artifact_ids=artifact_ids,
            next_actions=("repair_or_replace_conflicting_official_evidence",),
            diagnostic_code="FINANCIAL_CURRENCY_LANE_AMBIGUOUS" if currency_ambiguous else "FILING_DATA_QUALITY_FAILED")
    # Classify catalog codes only, never free-form provider/parser prose.
    ordered_errors = sorted(errors, key=lambda error: (
        0 if error.rsplit(":", 1)[-1] in _INTEGRITY else 1))
    for error in ordered_errors:
        code = error.rsplit(":", 1)[-1]
        outcome = unavailable_outcome(code, stage="download" if ":download:" in error else "discovery")
        if (code in _EXTERNAL | _INTEGRITY | _CONFIGURATION or code in SOURCE_ERROR_ACTIONS or code == "VISION_CONSENT_REQUIRED"
                or code in {"OFFICIAL_SOURCE_INTERNAL_FAILED", "OFFICIAL_LOCAL_IO_UNAVAILABLE",
                    "RECOVERY_INCIDENT_BUDGET_EXHAUSTED", "RECOVERY_INCIDENT_TIME_EXHAUSTED",
                    "RECOVERY_ACTION_USAGE_EXHAUSTED"}):
            return RecoveryOutcome(outcome.state, candidate_artifact_ids=artifact_ids,
                next_actions=outcome.next_actions, diagnostic_code=outcome.diagnostic_code)
    for diagnostic in view.diagnostics:
        if diagnostic in _EXTERNAL | _CONFIGURATION or diagnostic in SOURCE_ERROR_ACTIONS or diagnostic == "VISION_CONSENT_REQUIRED":
            outcome = unavailable_outcome(diagnostic)
            return RecoveryOutcome(outcome.state, candidate_artifact_ids=artifact_ids,
                next_actions=outcome.next_actions, diagnostic_code=outcome.diagnostic_code)
    return RecoveryOutcome(RecoveryState.NEEDS_ACTION,
        candidate_artifact_ids=artifact_ids, next_actions=tuple(view.readiness.actions)
        or ("repair_required_official_evidence",), diagnostic_code="FILING_DATA_QUALITY_FAILED")
