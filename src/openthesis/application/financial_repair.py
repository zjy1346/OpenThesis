"""A model-free financial repair: prepare, publish and refresh, never fabricate.

The request captures source/authorization collaborators at the application
boundary. The same preparer used by first research owns numeric admission;
this use case owns repair postconditions, audit and report refresh. It has no
model factory, settings lookup or market-specific parsing policy.
"""
from concurrent.futures import CancelledError
from dataclasses import dataclass, replace
import json
from typing import Any, Callable

from ..market_financials import ValidationStatus
from ..resilience.contracts import RecoveryOutcome, RecoveryState
from .financial_outcomes import unavailable_outcome
from .financials import OfficialFinancialRequest, OfficialFinancialUnavailable


@dataclass(frozen=True, slots=True)
class FinancialRetryResult:
    mode: str
    targets: tuple[str, ...] = ()
    downloaded: tuple[str, ...] = ()
    accepted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    status: str = "failed"
    error: str = ""
    updated_artifacts: tuple[str, ...] = ()
    terminal_state: str = ""
    diagnostics: tuple[str, ...] = ()
    freshness: str = "fresh"
    next_action: str = ""
    recovery_outcome: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "targets": list(self.targets), "downloaded": list(self.downloaded),
            "accepted": list(self.accepted), "rejected": list(self.rejected), "status": self.status,
            "error": self.error, "updated_artifacts": list(self.updated_artifacts),
            "terminal_state": self.terminal_state, "diagnostics": list(self.diagnostics),
            "freshness": self.freshness, "next_action": self.next_action,
            "recovery_outcome": json.loads(json.dumps(self.recovery_outcome, allow_nan=False))
                if self.recovery_outcome else None}


class FinancialRepairer:
    def __init__(self, storage, preparer, refresher):
        self.storage, self.preparer, self.refresher = storage, preparer, refresher

    def repair(self, request: OfficialFinancialRequest, *, progress: Callable[..., None] | None = None,
               cancel_check: Callable[[], bool] | None = None) -> FinancialRetryResult:
        if not isinstance(request, OfficialFinancialRequest) or not request.repair or not request.run_id:
            raise ValueError("CAPTURED_FINANCIAL_REPAIR_REQUEST_REQUIRED")
        stored = self.storage.get_run(request.run_id)
        if stored is None:
            raise KeyError("research run not found")
        payload = json.loads(stored["payload_json"])
        saved_company = payload.get("company") if isinstance(payload, dict) else None
        if not isinstance(saved_company, dict) or any(saved_company.get(field) != getattr(request.company, field)
                for field in ("cik", "security_id", "market")):
            raise ValueError("FINANCIAL_REPAIR_RUN_IDENTITY_MISMATCH")
        cancelled, notify = cancel_check or (lambda: False), progress or (lambda *_args: None)
        def check_cancelled():
            if cancelled():
                raise CancelledError("financial repair cancelled")
        check_cancelled()
        notify("filing-discovery", 0, 1)
        key = request.company.cik if request.company.market == "US" else request.company.security_id
        prepared = None
        errors, diagnostics = [], []
        targets, downloaded, processed, updated = (), (), (), ()
        freshness, terminal_state = "fresh", ""
        try:
            prepared = self.preparer.prepare(request, progress=notify, cancel_check=cancelled)
        except CancelledError:
            raise
        except OfficialFinancialUnavailable as exc:
            errors.append(exc.code)
            diagnostics.extend(exc.diagnostics)
            outcome = exc.outcome
        except Exception as exc:
            # Unclassified implementation exceptions are not transient source
            # failures. Preserve the last committed report and sanitized type.
            errors.append("FINANCIAL_REPAIR_INTERNAL_ERROR")
            diagnostics.append(f"financial_repair_exception:{type(exc).__name__}")
            outcome = RecoveryOutcome(RecoveryState.FAILED_INTERNAL,
                diagnostic_code="FINANCIAL_REPAIR_INTERNAL_ERROR",
                next_actions=("export_diagnostic_and_retry_with_new_plan",))
        else:
            outcome = prepared.recovery_outcome
            errors.extend(prepared.errors)
            targets, downloaded = prepared.target_accessions, prepared.downloaded_accessions
            pending = {accession for accession, state in (prepared.publication or {}).get("accessions", {}).items()
                       if state != "promoted"}
            processed = tuple(sorted(set(prepared.view.publishable_accessions) - pending)) if targets else ()
            diagnostics.extend((*prepared.discovery.diagnostics, *prepared.view.diagnostics))
            freshness = prepared.discovery.freshness
            terminal_state = outcome.state.value if errors else prepared.discovery.state.value
        check_cancelled()
        audit = [item for item in self.storage.get_facts_audit(key)
                 if item.get("accession_number") in processed]
        accepted = tuple(sorted(item["fact_id"] for item in audit
                         if str(item.get("validation_status", "")).upper() != ValidationStatus.REJECTED.value))
        rejected = tuple(sorted(item["fact_id"] for item in audit
                         if str(item.get("validation_status", "")).upper() == ValidationStatus.REJECTED.value))
        if not targets and not self.storage.get_facts(key) and not errors:
            errors.append("quality:no_financial_facts")
            outcome = unavailable_outcome("FILING_DATA_QUALITY_FAILED")
        if processed and (not errors or accepted):
            try:
                notify("artifact-rebuild", 0, 1)
                updated = self.refresher.refresh(request.run_id, prepared.company, cancel_check=cancelled)
                notify("artifact-rebuild", 1, 1)
            except CancelledError:
                raise
            except Exception as exc:
                code = str(exc) if str(exc) in {
                    "REPORT_REVISION_CONFLICT", "FINANCIAL_GENERATION_CONFLICT", "RESEARCH_RUN_SNAPSHOT_CONFLICT"
                } else "FILING_REPORT_REFRESH_FAILED"
                errors.append(f"artifact-rebuild:{code}")
                failure = unavailable_outcome(code)
                outcome = replace(outcome, state=failure.state, diagnostic_code=failure.diagnostic_code,
                    next_actions=failure.next_actions)
                terminal_state = outcome.state.value
        check_cancelled()
        if not errors and processed:
            self.storage.resolve_financial_recovery_cases(request.run_id, processed)
        status = "partial" if errors and (accepted or processed) else "failed" if errors else "succeeded"
        stage = ("filing-download" if any(":download:" in item for item in errors) else
                 "filing-parse" if any(":parse:" in item for item in errors) else
                 "filing-validation" if errors else "completed")
        error = "; ".join(dict.fromkeys(errors))[:800]
        self.storage.record_financial_retry_attempt(key, stage=stage, error=error)
        return FinancialRetryResult(mode="rebuild" if request.force else "retry", targets=tuple(sorted(targets)),
            downloaded=tuple(sorted(downloaded)), accepted=accepted, rejected=rejected, status=status,
            error=error, updated_artifacts=updated, terminal_state=terminal_state or outcome.state.value,
            diagnostics=tuple(sorted(set(diagnostics))), freshness=freshness,
            next_action=next(iter(outcome.next_actions), "") or (prepared.discovery.next_action if prepared else ""),
            recovery_outcome=outcome.to_dict())
