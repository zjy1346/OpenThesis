"""Controlled strategy registration, with an independent named verifier."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .contracts import RecoveryAction, RecoveryPlan, Usage, VerificationResult


class ActionContext(Protocol):
    plan: RecoveryPlan
    action: RecoveryAction
    prior_candidate: dict[str, Any] | None
    remaining_issue_ids: tuple[str, ...]
    external_task_id: str
    def checkpoint(self) -> None: ...
    def consume(self, usage: Usage) -> None: ...
    def wait(self, seconds: float) -> None: ...
    def record_external_intent(self) -> None: ...
    def record_external_task(self, task_id: str) -> None: ...
    def record_external_rejection(self, status: int, *, reason: str = "") -> None: ...


@dataclass(frozen=True, slots=True)
class CandidateResult:
    payload: dict[str, Any]
    artifact_ids: tuple[str, ...] = ()


class RecoveryError(RuntimeError):
    def __init__(self, code: str, *, transient: bool = False, retry_after: float = 0.0,
                 waiting_external: bool = False, needs_action: str = ""):
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.retry_after = max(0.0, retry_after)
        self.waiting_external = waiting_external
        self.needs_action = needs_action


@dataclass(frozen=True, slots=True)
class Strategy:
    strategy_id: str
    version: str
    verifier_id: str
    produce: Callable[[ActionContext], CandidateResult]
    verify: Callable[[CandidateResult, RecoveryPlan], VerificationResult]
    applicable: Callable[[RecoveryPlan, RecoveryAction], bool]
    # Registered observers may query a known task, never resubmit it. Their
    # separately bounded usage is charged to the original incident.
    resume_external: Callable[[ActionContext, str], CandidateResult] | None = None
    observation_usage: Usage = Usage()
    auto_observe: bool = False
    # A versioned, locally registered single-wire adapter may extend its call
    # reservation before a known-rejected format fallback. This is NOT another
    # allowance: each extension debits the same persisted incident budget.
    max_remote_calls: int | None = None


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[tuple[str, str], Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        key = (strategy.strategy_id, strategy.version)
        if (key in self._strategies or not all(key) or not strategy.verifier_id
                or not isinstance(strategy.observation_usage, Usage)
                or not isinstance(strategy.auto_observe, bool)
                or (strategy.max_remote_calls is not None
                    and (not isinstance(strategy.max_remote_calls, int)
                         or isinstance(strategy.max_remote_calls, bool)
                         or not 1 <= strategy.max_remote_calls <= 128))
                or (strategy.auto_observe and strategy.resume_external is None)
                or (strategy.resume_external is not None
                    and (not callable(strategy.resume_external) or strategy.observation_usage.calls < 1
                         or strategy.observation_usage.pages))):
            raise ValueError("RECOVERY_STRATEGY_REGISTRATION_INVALID")
        self._strategies[key] = strategy

    def resolve(self, action: RecoveryAction) -> Strategy:
        strategy = self._strategies.get((action.strategy_id, action.strategy_version))
        if strategy is None or strategy.verifier_id != action.verifier_id:
            raise ValueError("RECOVERY_STRATEGY_NOT_REGISTERED")
        if (strategy.max_remote_calls is not None
                and strategy.max_remote_calls < action.estimated_usage.calls):
            raise ValueError("RECOVERY_STRATEGY_CALL_POLICY_INVALID")
        return strategy
