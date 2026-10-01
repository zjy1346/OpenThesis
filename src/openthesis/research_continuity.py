from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .domain import ResearchArtifact, ResearchRun, RunStatus, utc_now_iso


class StageOutcome(StrEnum):
    """Typed stage result; failures are data, not control-flow for the whole run."""

    COMPLETE = "complete"
    DEGRADED = "degraded"
    WAITING_RETRYABLE = "waiting_retryable"
    NEEDS_ACTION = "needs_action"
    BLOCKED_INTEGRITY = "blocked_integrity"
    CANCELLED = "cancelled"


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
        }


class ContinuityStore(Protocol):
    def save_run(self, run: ResearchRun) -> None: ...
    def append_stage_attempt(self, attempt: StageAttempt) -> None: ...
    def save_artifact(self, artifact: ResearchArtifact) -> None: ...


class ResearchContinuityCoordinator:
    """Small public seam for creating, checkpointing and snapshotting a run.

    The coordinator deliberately knows nothing about SEC/HKEX/A-share adapters or
    model providers.  Those systems report typed outcomes here, which keeps one
    durable run alive across recoverable failures.
    """

    def __init__(self, store: ContinuityStore):
        self.store = store

    def start(self, run: ResearchRun) -> ResearchRun:
        run.status = RunStatus.RUNNING
        self.store.save_run(run)
        self.checkpoint(run.run_id, "created", StageOutcome.COMPLETE)
        return run

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
    ) -> StageAttempt:
        record = StageAttempt(
            run_id=run_id,
            stage=stage,
            outcome=outcome,
            attempt=max(1, int(attempt)),
            error_code=error_code,
            message=message,
            diagnostics=diagnostics or {},
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
        self.store.save_artifact(artifact)
        self.checkpoint(
            run.run_id,
            stage,
            outcome,
            error_code=error_code,
            message=message,
            diagnostics={"retryable": retryable, "artifact_sha256": digest},
        )
        run.status = RunStatus.PARTIAL
        run.completed_at = utc_now_iso()
        if message and message not in run.errors:
            run.errors.append(message)
        self.store.save_run(run)
        return artifact

    def snapshot(self, run_id: str) -> dict[str, Any]:
        getter = getattr(self.store, "continuity_snapshot")
        return getter(run_id)
