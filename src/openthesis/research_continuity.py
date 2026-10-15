from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, Protocol

from .domain import Company, ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from .resilience.contracts import RecoveryOutcome, RecoveryState


class StageOutcome(StrEnum):
    """Typed stage result; failures are data, not control-flow for the whole run."""

    COMPLETE = "complete"
    DEGRADED = "degraded"
    WAITING_RETRYABLE = "waiting_retryable"
    WAITING_EXTERNAL = "waiting_external"
    NEEDS_ACTION = "needs_action"
    BLOCKED_INTEGRITY = "blocked_integrity"
    EXHAUSTED = "exhausted"
    FAILED_INTERNAL = "failed_internal"
    CANCELLED = "cancelled"

    @classmethod
    def from_recovery(cls, outcome: RecoveryOutcome) -> StageOutcome:
        """Keep canonical terminal distinctions in the legacy stage ledger."""
        return {
            RecoveryState.COMMITTED: cls.COMPLETE,
            RecoveryState.WAITING_EXTERNAL: cls.WAITING_EXTERNAL,
            RecoveryState.NEEDS_ACTION: cls.NEEDS_ACTION,
            RecoveryState.BLOCKED_INTEGRITY: cls.BLOCKED_INTEGRITY,
            RecoveryState.EXHAUSTED: cls.EXHAUSTED,
            RecoveryState.CANCELLED: cls.CANCELLED,
            RecoveryState.FAILED_INTERNAL: cls.FAILED_INTERNAL,
        }[outcome.state]


# Stable lifecycle vocabulary shared by persistence, recovery policy and chaos
# tests.  Adapter-specific sub-stages (for example ``comparison:2``) may extend
# this list, but the core pipeline must not silently lose coverage when a new
# top-level stage is introduced.
RESEARCH_STAGE_KINDS: tuple[str, ...] = (
    "company-identity",
    "disclosure-discovery",
    "disclosure-coverage",
    "filing-download",
    "deterministic-parse",
    "canonical-validation",
    "financial-projection",
    "research-materials",
    "base-agents",
    "growth-opportunities",
    "counterarguments",
    "scenarios",
    "final-synthesis",
    "report-rendering",
)


@dataclass(slots=True, frozen=True)
class StageAttempt:
    run_id: str
    stage: str
    outcome: StageOutcome
    attempt: int = 1
    error_code: str = ""
    message: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    action_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stage": self.stage,
            "outcome": self.outcome.value,
            "attempt": self.attempt,
            "error_code": self.error_code,
            "message": self.message,
            "diagnostics": dict(self.diagnostics),
            "created_at": self.created_at,
            "action_id": self.action_id,
        }


class ContinuityStore(Protocol):
    def save_run_start(self, run: ResearchRun, attempt: StageAttempt) -> None: ...
    def save_run_stage_result(self, run_id: str, attempt: StageAttempt, *,
                              snapshot_updates: dict[str, Any] | None = None,
                              status: RunStatus | None = None, error: str = "",
                              completed_at: str | None = None) -> dict[str, Any]: ...
    def append_stage_attempt(self, attempt: StageAttempt) -> None: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def save_continuity_result(self, run_id: str, artifact: ResearchArtifact,
                               attempt: StageAttempt, *, message: str,
                               completed_at: str) -> dict[str, Any]: ...


class ResearchContinuityCoordinator:
    """Small public seam for creating, checkpointing and snapshotting a run.

    The coordinator deliberately knows nothing about SEC/HKEX/A-share adapters or
    model providers.  Those systems report typed outcomes here, which keeps one
    durable run alive across recoverable failures.
    """

    def __init__(self, store: ContinuityStore):
        self.store = store

    def start(self, run: ResearchRun) -> ResearchRun:
        captured = deepcopy(run)
        captured.status = RunStatus.RUNNING
        self.store.save_run_start(captured, StageAttempt(run.run_id, "created", StageOutcome.COMPLETE))
        run.status = captured.status
        return run

    @staticmethod
    def _apply_lifecycle_result(run: ResearchRun, committed: dict[str, Any]) -> None:
        run.status = RunStatus(committed["status"])
        run.completed_at = committed.get("completed_at")
        run.errors = deepcopy(committed["errors"])
        run.data_snapshot = deepcopy(committed.get("data_snapshot", {}))

    def cancel(self, run: ResearchRun, *, stage: str, message: str) -> ResearchRun:
        attempt = StageAttempt(run.run_id, stage or "cancelled", StageOutcome.CANCELLED,
                               error_code="RESEARCH_CANCELLED", message=message)
        committed = self.store.save_run_stage_result(
            run.run_id, attempt, status=RunStatus.CANCELLED,
            completed_at=utc_now_iso(), error="research cancelled")
        self._apply_lifecycle_result(run, committed)
        return run

    def record_disclosure_coverage(self, run: ResearchRun, coverage: Any) -> None:
        captured = deepcopy(coverage.to_dict())
        attempt = StageAttempt(run.run_id, "disclosure-coverage",
            StageOutcome.COMPLETE if coverage.complete else StageOutcome.DEGRADED,
            diagnostics=captured,
            message="" if coverage.complete else "Official disclosure history is incomplete")
        committed = self.store.save_run_stage_result(
            run.run_id, attempt, snapshot_updates={"disclosure_coverage": captured})
        self._apply_lifecycle_result(run, committed)

    def record_comparison_result(self, run: ResearchRun, *, comparison_index: int,
                                 error_code: str, model_id: str = "", retryable: bool = False,
                                 cancelled: bool = False) -> None:
        """A failed optional peer cannot degrade committed primary research.

        The diagnostic and its stage attempt are committed together against the
        latest stored payload. Never replace it with the peer's stale run copy.
        """
        if (isinstance(comparison_index, bool) or not isinstance(comparison_index, int)
                or comparison_index < 1):
            raise ValueError("RESEARCH_COMPARISON_INDEX_INVALID")
        stage = f"comparison:{comparison_index}"
        attempt = StageAttempt(run.run_id, stage,
            StageOutcome.CANCELLED if cancelled else StageOutcome.WAITING_RETRYABLE
                if retryable else StageOutcome.NEEDS_ACTION,
            error_code=error_code, message=error_code,
            diagnostics={"comparison_index": comparison_index, "model_id": model_id})
        committed = self.store.save_run_stage_result(
            run.run_id, attempt, error=f"{stage}:{error_code}")
        self._apply_lifecycle_result(run, committed)

    def checkpoint(
        self,
        run_id: str,
        stage: str,
        outcome: StageOutcome,
        *,
        attempt: int = 1,
        error_code: str = "",
        message: str = "",
        diagnostics: dict[str, Any] | None = None,
        action_id: str = "",
    ) -> StageAttempt:
        record = StageAttempt(
            run_id=run_id,
            stage=stage,
            outcome=outcome,
            attempt=max(1, int(attempt)),
            error_code=error_code,
            message=message,
            diagnostics=diagnostics or {},
            action_id=action_id,
        )
        self.store.append_stage_attempt(record)
        return record

    def preserve_visible_result(
        self,
        run: ResearchRun,
        *,
        stage: str,
        outcome: StageOutcome,
        error_code: str,
        message: str,
        retryable: bool,
        recovery_outcome: RecoveryOutcome | None = None,
    ) -> ResearchArtifact:
        """Append a recovery notice without replacing the authoritative report.

        An operational error is not investment research.  Keeping it in a
        distinct artifact lane lets report projection continue to show the
        last valid report (or deterministic stage outputs) while the UI reads
        the continuity ledger for recovery state.
        """

        content = {
            "research_complete": False,
            "continuity": {
                "stage": stage,
                "outcome": outcome.value,
                "error_code": error_code,
                "message": message,
                "retryable": retryable,
            },
        }
        if recovery_outcome is not None:
            content["continuity"]["recovery_outcome"] = recovery_outcome.to_dict()
        raw = json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        artifact = ResearchArtifact(
            artifact_id=f"{run.run_id}:continuity-notice:{digest[:16]}",
            run_id=run.run_id,
            artifact_type="continuity-notice",
            title="Research recovery notice",
            content=content,
            agent_id="research-continuity",
        )
        diagnostics = {"retryable": retryable, "artifact_sha256": digest}
        if recovery_outcome is not None:
            diagnostics["recovery_outcome"] = recovery_outcome.to_dict()
        attempt = StageAttempt(
            run_id=run.run_id,
            stage=stage,
            outcome=outcome,
            error_code=error_code,
            message=message,
            diagnostics=diagnostics,
        )
        committed = self.store.save_continuity_result(
            run.run_id, artifact, attempt, message=message, completed_at=utc_now_iso())
        # Do not mutate the caller's in-memory state until all three durable
        # writes succeed. Its captured inputs must never replace newer inputs.
        self._apply_lifecycle_result(run, committed)
        return artifact

    def preserve_interruption(
        self, run_id: str, *, run: ResearchRun | None = None, stage: str,
        outcome: StageOutcome, error_code: str, message: str,
        recovery_outcome: RecoveryOutcome | None = None,
    ) -> ResearchRun | None:
        """Load interrupted state and persist its notice behind one use case.

        Transport jobs need not reconstruct domain records or decide which
        metadata survives. Unknown legacy fields stay in storage; known optional
        fields (including the market snapshot) are retained in the domain view.
        """
        if run is not None and run_id and run.run_id != run_id:
            raise ValueError("CONTINUITY_RUN_IDENTITY_CONFLICT")
        if run is None and run_id:
            record = self.store.get_run(run_id)
            if record is None:
                return None
            payload = json.loads(record["payload_json"])
            if not isinstance(payload, dict) or not isinstance(payload.get("company"), dict):
                raise ValueError("CONTINUITY_RUN_PAYLOAD_INVALID")
            values = {key: deepcopy(value) for key, value in payload.items()
                      if key in {item.name for item in fields(ResearchRun)}}
            values["run_id"] = run_id
            values["company"] = Company(**{key: value for key, value in payload["company"].items()
                                           if key in {item.name for item in fields(Company)}})
            values["status"] = RunStatus(record["status"])
            run = ResearchRun(**values)
        if run is None:
            return None
        self.preserve_visible_result(
            run, stage=stage, outcome=outcome, error_code=error_code, message=message,
            retryable=outcome in {StageOutcome.WAITING_RETRYABLE, StageOutcome.WAITING_EXTERNAL},
            recovery_outcome=recovery_outcome)
        return run

    def snapshot(self, run_id: str) -> dict[str, Any]:
        getter = getattr(self.store, "continuity_snapshot")
        return getter(run_id)
