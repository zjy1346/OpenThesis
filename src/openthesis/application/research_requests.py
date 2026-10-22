"""Captured application inputs, without transport, source or provider logic."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from ..domain import Company, ResearchRun
from ..providers import ModelConfig


MODEL_RESUME_TARGETS = frozenset({
    "model-stages", "growth", "counter-analysis", "forecast-scenarios", "synthesis",
})


def captured_models(models: tuple[ModelConfig, ...]) -> tuple[ModelConfig, ...]:
    models = tuple(models)
    if not models or len(models) > 5:
        raise ValueError("RESEARCH_MODEL_COUNT_INVALID")
    if any(not isinstance(model, ModelConfig) for model in models):
        raise ValueError("RESEARCH_MODEL_CONFIGURATION_INVALID")
    if len({model.public_id for model in models}) != len(models):
        raise ValueError("RESEARCH_MODELS_NOT_UNIQUE")
    return tuple(replace(model) for model in models)


@dataclass(frozen=True, slots=True)
class ResearchStartRequest:
    company: Company
    financials: Any
    selected_pack: Any
    models: tuple[ModelConfig, ...]
    filing_evidence: tuple[dict[str, Any], ...] = ()
    valuation_inputs: dict[str, Any] | None = None
    market_snapshot: dict[str, Any] | None = None
    reproducibility: dict[str, Any] = field(default_factory=dict)
    existing_run: ResearchRun | None = None
    report_language: str = "zh-CN"
    ui_language: str = "zh-CN"
    parallel_agents: bool = False
    recovery_session_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", captured_models(self.models))
        object.__setattr__(self, "company", replace(self.company))
        for key in ("financials", "filing_evidence", "valuation_inputs", "market_snapshot", "reproducibility"):
            object.__setattr__(self, key, deepcopy(getattr(self, key)))

    def workflow_inputs(self) -> dict[str, Any]:
        # Each comparison receives its own copy of one captured material set.
        # A provider/workflow mutating primary inputs cannot change its peer's
        # context, and no comparison repeats discovery or financial parsing.
        return deepcopy({
            "company": self.company, "facts": self.financials,
            "filing_evidence": list(self.filing_evidence),
            "valuation_inputs": self.valuation_inputs,
            "market_snapshot": self.market_snapshot,
            "reproducibility": self.reproducibility,
        })


@dataclass(frozen=True, slots=True)
class ResearchResumeRequest:
    run: ResearchRun
    selected_pack: Any
    model: ModelConfig
    facts: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    filing_evidence: tuple[dict[str, Any], ...] = ()
    target: str = "model-stages"
    ui_language: str = "zh-CN"
    parallel_agents: bool = False
    manual_authorization_id: str = ""
    local_only: bool = False
    budget_only: bool = False

    def __post_init__(self) -> None:
        if self.target not in MODEL_RESUME_TARGETS:
            raise ValueError("MODEL_RECOVERY_TARGET_INVALID")
        if type(self.local_only) is not bool:
            raise ValueError("RESEARCH_LOCAL_REVALIDATION_INVALID")
        if type(self.budget_only) is not bool or self.budget_only and (self.local_only or self.manual_authorization_id):
            raise ValueError("RESEARCH_BUDGET_RESUME_INVALID")
        if not self.model.enabled and not self.local_only:
            raise ValueError("an enabled model is required")
        if not self.run.run_id:
            raise ValueError("RESEARCH_RUN_ID_REQUIRED")
        for key in ("facts", "artifacts", "filing_evidence"):
            object.__setattr__(self, key, deepcopy(tuple(getattr(self, key))))


@dataclass(frozen=True, slots=True)
class ResearchExecutionProgress:
    message: str
    percent: float
    model_index: int
    comparison_count: int
    model_percent: int


@dataclass(frozen=True, slots=True)
class ResearchComparisonFailure:
    comparison_index: int
    model_id: str
    error_code: str
    retryable: bool
