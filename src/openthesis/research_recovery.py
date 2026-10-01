"""Pure dependency planner for model-stage recovery.

The planner does not call providers, mutate storage, or know about UI routes.
It converts append-only artifacts and continuity attempts into one minimal,
hashable plan that every compatibility retry endpoint must execute through.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable


BASE_AGENTS = (
    "financial-analyst",
    "business-analyst",
    "accounting-risk-analyst",
)


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    target: str
    stages: tuple[str, ...]
    reason: str
    input_artifact_ids: tuple[str, ...]

    @property
    def available(self) -> bool:
        return bool(self.stages)

    @property
    def plan_hash(self) -> str:
        encoded = json.dumps(
            {
                "target": self.target,
                "stages": self.stages,
                "reason": self.reason,
                "inputs": self.input_artifact_ids,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "stages": list(self.stages),
            "reason": self.reason,
            "input_artifact_ids": list(self.input_artifact_ids),
            "plan_hash": self.plan_hash,
        }


class ResearchRecoveryPlanner:
    """Derive the smallest supported recovery DAG from persisted results."""

    _STAGE_ARTIFACTS = {
        "growth-opportunities": "growth-opportunities",
        "counter-analysis": "counter-analysis",
        "forecast-scenarios": "forecast-scenarios",
        "synthesis": "research-report",
    }
    _STAGE_DOWNSTREAM = {
        "growth": ("growth-opportunities", "counter-analysis", "forecast-scenarios", "final-synthesis"),
        "counter-analysis": ("counter-analysis", "forecast-scenarios", "final-synthesis"),
        "forecast-scenarios": ("forecast-scenarios", "final-synthesis"),
        "synthesis": ("final-synthesis",),
    }
    _FAILURE_STAGE_ALIASES = {
        "base-agents": "model-stages",
        **{agent: "model-stages" for agent in BASE_AGENTS},
        "growth-opportunities": "growth",
        "growth": "growth",
        "counterarguments": "counter-analysis",
        "counter-analysis": "counter-analysis",
        "skeptical-analyst": "counter-analysis",
        "scenarios": "forecast-scenarios",
        "forecast-scenarios": "forecast-scenarios",
        "forecast-analyst": "forecast-scenarios",
        "synthesis": "synthesis",
        "research-synthesizer": "synthesis",
    }

    def describe(
        self,
        artifacts: Iterable[dict[str, Any]],
        continuity: dict[str, Any] | None = None,
        *,
        target: str = "auto",
    ) -> dict[str, Any]:
        """Return a stable, UI-ready recovery description from saved state.

        Recovery remains optional report metadata: callers may isolate failures
        from this method without coupling report rendering to planner internals.
        """
        items = tuple(item for item in artifacts if isinstance(item, dict))
        plan = self.plan(items, continuity, target=target)
        return {
            **plan.to_dict(),
            "available": plan.available,
            "error_code": self.failure_code(items, plan.target),
        }

    def failure_code(
        self, artifacts: Iterable[dict[str, Any]], target: str
    ) -> str:
        """Resolve a saved-stage error without exposing aliases to services."""
        items = tuple(item for item in artifacts if isinstance(item, dict))
        aliases = {
            "model-stages": {"base-agents", *BASE_AGENTS},
            "growth": {"growth", "growth-opportunities", "growth-opportunity-analyst"},
            "counter-analysis": {"counterarguments", "counter-analysis", "skeptical-analyst"},
            "forecast-scenarios": {"scenarios", "forecast-scenarios", "forecast-analyst"},
            "synthesis": {"synthesis", "research-synthesizer"},
        }
        wanted = aliases.get(target, set())
        for item in reversed(items):
            if item.get("artifact_type") != "stage-outcome":
                continue
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            if str(content.get("stage") or "") in wanted or str(content.get("agent_id") or "") in wanted:
                return str(content.get("error_code") or "")
        if target == "synthesis":
            final = next((item for item in reversed(items) if item.get("artifact_type") == "research-report"), None)
            content = final.get("content", {}) if isinstance(final, dict) else {}
            if isinstance(content, dict) and content.get("mode") == "synthesis-incomplete":
                diagnostics = content.get("diagnostics")
                diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                provider_code = str(diagnostics.get("provider_error_code") or "")
                if provider_code:
                    return provider_code[:80]
                if diagnostics.get("parse_error_class") == "context_budget_exceeded":
                    return "MODEL_CONTEXT_CAPACITY"
                if diagnostics.get("parse_error_class") == "provider_error":
                    return "MODEL_PROVIDER_ERROR"
                return "MODEL_RESPONSE_INVALID"
        artifact_type = {
            "growth": "growth-opportunities",
            "counter-analysis": "counter-analysis",
            "forecast-scenarios": "forecast-scenarios",
        }.get(target)
        if artifact_type:
            for item in reversed(items):
                if item.get("artifact_type") == artifact_type:
                    content = item.get("content") if isinstance(item.get("content"), dict) else {}
                    return str(content.get("_response_error") or "")
        return ""

    def plan(
        self,
        artifacts: Iterable[dict[str, Any]],
        continuity: dict[str, Any] | None = None,
        *,
        target: str = "auto",
    ) -> RecoveryPlan:
        items = tuple(item for item in artifacts if isinstance(item, dict))
        identities = tuple(sorted({
            str(item.get("artifact_id") or "") for item in items
            if item.get("artifact_id")
        }))
        types = {str(item.get("artifact_type") or "") for item in items}
        latest_agent_artifacts: dict[str, dict[str, Any]] = {}
        has_unbound_agent_result = False
        for item in items:
            if item.get("artifact_type") == "agent-analysis":
                agent_id = str(item.get("agent_id") or "")
                if agent_id in BASE_AGENTS:
                    latest_agent_artifacts[agent_id] = item
                elif not agent_id:
                    # Without a persisted role identity, recovery cannot know
                    # whether rerunning a base role would duplicate a prior
                    # model result. Prefer an explicit manual recovery over
                    # charging for an ambiguous replay.
                    has_unbound_agent_result = True
        successful_agents = {
            agent_id
            for agent_id, item in latest_agent_artifacts.items()
            if isinstance(item.get("content"), dict)
            and item["content"].get("trusted_state") in {
                "completed_verified", "completed_partial",
            }
        }
        missing_base = tuple(agent for agent in BASE_AGENTS if agent not in successful_agents)
        base_retryable = self._base_retryable(items, continuity)

        # Recovery re-enters the same workflow with persisted artifacts.  The
        # workflow seeds successful analyses and invokes only missing roles;
        # downstream stages are refreshed from the rebuilt dossier.
        if missing_base and base_retryable and target in {"auto", "model-stages"}:
            if has_unbound_agent_result:
                return RecoveryPlan(target, (), "unbound_agent_artifact_requires_review", identities)
            return RecoveryPlan(
                "model-stages",
                (*missing_base, "growth-opportunities", "counterarguments", "scenarios", "final-synthesis"),
                "retryable_base_stage_incomplete",
                identities,
            )

        stage_target = (
            self._latest_failed_model_stage(items)
            if target == "auto"
            else self._FAILURE_STAGE_ALIASES.get(target, "")
        )
        if stage_target in {"growth", "counter-analysis", "forecast-scenarios", "synthesis"}:
            if not self._stage_prerequisites_present(stage_target, items):
                return RecoveryPlan(stage_target, (), "stage_prerequisites_not_verified", identities)
            return RecoveryPlan(
                stage_target,
                self._STAGE_DOWNSTREAM[stage_target],
                f"{stage_target.replace('-', '_')}_failed_or_unverified",
                identities,
            )

        if target in {"auto", "growth"} and self._stage_prerequisites_present(
            "growth", items
        ):
            growth = self._latest(items, "growth-opportunities")
            if target == "growth" or self._growth_retryable(growth):
                downstream = ("final-synthesis",) if {
                    "counter-analysis", "forecast-scenarios"
                }.issubset(types) else ()
                return RecoveryPlan(
                    "growth",
                    ("growth-opportunities", *downstream),
                    "growth_stage_incomplete",
                    identities,
                )
        if target in {"auto", "synthesis"} and self._stage_prerequisites_present(
            "synthesis", items
        ):
            report = self._latest(items, "research-report")
            retryable = report is None or bool(
                (report.get("content") or {}).get("retryable")
                if isinstance(report.get("content"), dict) else False
            )
            if target == "synthesis" or retryable:
                return RecoveryPlan(
                    "synthesis", ("final-synthesis",),
                    "final_synthesis_incomplete", identities,
                )
        return RecoveryPlan(target, (), "no_retryable_stage", identities)

    @staticmethod
    def _stage_prerequisites_present(
        target: str, items: tuple[dict[str, Any], ...]
    ) -> bool:
        prerequisites = {
            "growth": {"deterministic-financial-summary", "verified-research-dossier"},
            "counter-analysis": {
                "deterministic-financial-summary", "verified-research-dossier",
                "growth-opportunities",
            },
            "forecast-scenarios": {
                "deterministic-financial-summary", "verified-research-dossier",
                "growth-opportunities", "counter-analysis",
            },
            "synthesis": {
                "deterministic-financial-summary", "verified-research-dossier",
                "growth-opportunities", "counter-analysis", "forecast-scenarios",
            },
        }
        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            artifact_type = str(item.get("artifact_type") or "")
            if artifact_type:
                latest[artifact_type] = item
        required = prerequisites.get(target, set())
        if not required.issubset(latest):
            return False
        for artifact_type in required:
            item = latest[artifact_type]
            content = item.get("content")
            if not isinstance(content, dict):
                return False
            if artifact_type == "verified-research-dossier":
                analyses = content.get("verified_analyses", content.get("analyses"))
                if not isinstance(analyses, dict) or not analyses:
                    return False
            elif artifact_type in {
                "growth-opportunities", "counter-analysis", "forecast-scenarios",
            } and not ResearchRecoveryPlanner._artifact_verified(item):
                return False
        return True

    def _latest_failed_model_stage(self, items: tuple[dict[str, Any], ...]) -> str:
        latest_failure: dict[str, int] = {}
        latest_success: dict[str, int] = {}
        for index, item in enumerate(items):
            artifact_type = str(item.get("artifact_type") or "")
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            if artifact_type == "stage-outcome":
                target = self._FAILURE_STAGE_ALIASES.get(str(content.get("stage") or ""))
                if not target:
                    target = self._FAILURE_STAGE_ALIASES.get(str(content.get("agent_id") or ""))
                if target and (
                    content.get("retryable")
                    or content.get("outcome") in {"waiting_retryable", "needs_action"}
                    or str(content.get("error_code") or "").startswith("MODEL_")
                ):
                    latest_failure[target] = index
            elif artifact_type in {"growth-opportunities", "counter-analysis", "forecast-scenarios"}:
                target = {
                    "growth-opportunities": "growth",
                    "counter-analysis": "counter-analysis",
                    "forecast-scenarios": "forecast-scenarios",
                }[artifact_type]
                if ResearchRecoveryPlanner._artifact_verified(item):
                    latest_success[target] = index
                else:
                    latest_failure[target] = index
            elif artifact_type == "research-report":
                if content.get("retryable") or content.get("mode") in {"synthesis-incomplete", "staged-fallback"}:
                    latest_failure["synthesis"] = index
                else:
                    latest_success["synthesis"] = index

        for target in ("growth", "counter-analysis", "forecast-scenarios", "synthesis"):
            failure_index = latest_failure.get(target)
            if failure_index is not None and failure_index > latest_success.get(target, -1):
                return target
        return ""

    @staticmethod
    def _artifact_verified(item: dict[str, Any]) -> bool:
        content = item.get("content")
        if not isinstance(content, dict):
            return False
        if item.get("artifact_type") == "growth-opportunities":
            validation = content.get("_validation")
            audit = content.get("_audit")
            audit = audit.get("verification") if isinstance(audit, dict) else None
            return bool(
                content.get("opportunities")
                and (
                    isinstance(validation, dict) and validation.get("passed") is True
                    or isinstance(audit, dict) and audit.get("passed") is True
                )
            )
        audit = content.get("_audit")
        audit = audit.get("verification") if isinstance(audit, dict) else None
        return isinstance(audit, dict) and audit.get("passed") is True

    @staticmethod
    def _latest(items: tuple[dict[str, Any], ...], artifact_type: str) -> dict[str, Any] | None:
        return next(
            (item for item in reversed(items) if item.get("artifact_type") == artifact_type),
            None,
        )

    @staticmethod
    def _growth_retryable(item: dict[str, Any] | None) -> bool:
        if not item or not isinstance(item.get("content"), dict):
            return True
        content = item["content"]
        if content.get("opportunities"):
            return False
        validation = content.get("_validation")
        return content.get("_response_error") in {
            "empty_content", "invalid_json", "invalid_shape",
        } or (isinstance(validation, dict) and validation.get("passed") is False)

    @staticmethod
    def _base_retryable(
        items: tuple[dict[str, Any], ...], continuity: dict[str, Any] | None,
    ) -> bool:
        for item in reversed(items):
            if item.get("artifact_type") != "stage-outcome":
                continue
            content = item.get("content")
            if not isinstance(content, dict):
                continue
            if (
                str(content.get("agent_id") or "") in BASE_AGENTS
                and bool(content.get("retryable"))
                and content.get("outcome") == "waiting_retryable"
            ):
                return True
            if (
                content.get("stage") == "base-agents"
                and int(content.get("completed", -1)) == 0
                and bool(content.get("retryable"))
            ):
                return True
        latest = continuity.get("latest") if isinstance(continuity, dict) else None
        return bool(
            isinstance(latest, dict)
            and latest.get("outcome") == "waiting_retryable"
            and str(latest.get("error_code") or "").startswith("MODEL_")
        )
