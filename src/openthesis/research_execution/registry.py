"""Validated stage DAGs. Pack and OT metadata compile to the same contract."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from ..resilience.contracts import digest

SOURCE_ANALYSIS_STAGES = frozenset({
    "business-analyst", "source-industry-analysis", "source-risk-analysis",
})


@dataclass(frozen=True, slots=True)
class StageSpec:
    stage_id: str
    version: str
    handler_id: str
    validator_id: str
    depends_on: tuple[str, ...] = ()
    input_contracts: tuple[str, ...] = ()
    output_contract: str = ""
    role: str = ""
    prompt_path: str = ""
    configuration_scope: str = "model"
    prompt_sha256: str = ""
    optional_dependencies: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    any_capabilities: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        from dataclasses import asdict
        return digest(asdict(self))

    def missing_capabilities(self, capabilities: Iterable[str]) -> tuple[str, ...]:
        """One material eligibility rule for first execution and continuation."""
        present = set(capabilities)
        missing = [key for key in self.required_capabilities if key not in present]
        if self.any_capabilities and not present.intersection(self.any_capabilities):
            missing.append("any:" + "|".join(self.any_capabilities))
        return tuple(missing)


class StageRegistry:
    def __init__(self, stages: Iterable[StageSpec]):
        self._stages: dict[str, StageSpec] = {}
        for spec in stages:
            if not spec.stage_id or not spec.version or not spec.handler_id or not spec.validator_id or not spec.output_contract:
                raise ValueError("STAGE_CONTRACT_INCOMPLETE")
            if spec.prompt_sha256 and (len(spec.prompt_sha256) != 64
                                      or any(char not in "0123456789abcdef" for char in spec.prompt_sha256)):
                raise ValueError("STAGE_PROMPT_DIGEST_INVALID")
            if spec.stage_id in self._stages or len(set(spec.depends_on)) != len(spec.depends_on):
                raise ValueError("STAGE_ID_OR_DEPENDENCY_DUPLICATE")
            if (not set(spec.optional_dependencies) <= set(spec.depends_on)
                    or len(set(spec.optional_dependencies)) != len(spec.optional_dependencies)
                    or any(not isinstance(item, str) or not item for item in
                           (*spec.required_capabilities, *spec.any_capabilities))):
                raise ValueError("STAGE_CAPABILITY_CONTRACT_INVALID")
            self._stages[spec.stage_id] = spec
        if not self._stages or len(self._stages) > 512:
            raise ValueError("STAGE_GRAPH_SIZE_INVALID")
        for spec in self._stages.values():
            if any(item not in self._stages for item in spec.depends_on):
                raise ValueError("STAGE_DEPENDENCY_MISSING")
        self._order = self._topological_order()

    def _topological_order(self) -> tuple[str, ...]:
        order: list[str] = []
        remaining = set(self._stages)
        while remaining:
            ready = [stage_id for stage_id, spec in self._stages.items()
                     if stage_id in remaining and not (set(spec.depends_on) & remaining)]
            if not ready:
                raise ValueError("STAGE_GRAPH_CYCLE")
            order.extend(ready)
            remaining.difference_update(ready)
        return tuple(order)

    @property
    def stages(self) -> tuple[StageSpec, ...]:
        return tuple(self._stages[stage_id] for stage_id in self._order)

    @property
    def graph_hash(self) -> str:
        return digest([spec.identity for spec in self.stages])

    def get(self, stage_id: str) -> StageSpec:
        try:
            return self._stages[stage_id]
        except KeyError as exc:
            raise ValueError("STAGE_NOT_REGISTERED") from exc

    def bind_prompts(self, prompt_text_by_path: Mapping[str, str], *,
                     shared_instructions: str = "") -> StageRegistry:
        """Freeze exact prompt contents without coupling the graph to file I/O.

        A path/version alone cannot identify changed instructions. Callers
        provide the already-loaded text; healthy unrelated prompt identities
        remain stable when only one stage's instructions change.
        """
        if not isinstance(shared_instructions, str):
            raise ValueError("STAGE_PROMPT_TEXT_INVALID")
        specs = []
        for spec in self.stages:
            if not spec.prompt_path:
                specs.append(spec)
                continue
            prompt = prompt_text_by_path.get(spec.prompt_path)
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("STAGE_PROMPT_TEXT_REQUIRED")
            specs.append(replace(spec, prompt_sha256=digest({
                "prompt": prompt, "shared_instructions": shared_instructions,
            })))
        return StageRegistry(specs)

    def ancestors(self, targets: Iterable[str]) -> tuple[str, ...]:
        selected = set(targets)
        for stage_id in tuple(selected):
            self.get(stage_id)
        for stage_id in reversed(self._order):
            if stage_id in selected:
                selected.update(self._stages[stage_id].depends_on)
        return tuple(stage_id for stage_id in self._order if stage_id in selected)

    def descendants(self, invalidated: Iterable[str], *, include_targets: bool = True) -> tuple[str, ...]:
        original = set(invalidated)
        selected = set(original)
        for stage_id in original:
            self.get(stage_id)
        for stage_id in self._order:
            if set(self._stages[stage_id].depends_on) & selected:
                selected.add(stage_id)
        if not include_targets:
            selected.difference_update(original)
        return tuple(stage_id for stage_id in self._order if stage_id in selected)

    @classmethod
    def from_ot(cls, workflow: dict[str, Any]) -> StageRegistry:
        """OT may select registered handlers, never Python or validator code."""
        raw_steps = workflow.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 64:
            raise ValueError("OT_STAGE_GRAPH_SIZE_INVALID")
        specs: list[StageSpec] = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                raise ValueError("OT_STAGE_CONTRACT_INVALID")
            dependencies = raw.get("depends_on", [])
            if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
                raise ValueError("OT_STAGE_CONTRACT_INVALID")
            stage_id, role, prompt = (raw.get(key) for key in ("id", "role", "prompt"))
            if any(not isinstance(value, str) or not value.strip() for value in (stage_id, role, prompt)):
                raise ValueError("OT_STAGE_CONTRACT_INVALID")
            specs.append(StageSpec(stage_id.strip(), "ot-stage-v1", "model-analysis", "research-evidence-v1",
                                   tuple(dependencies), ("research-context-v1",),
                                   str(raw.get("output_schema") or "research-analysis-v1"), role.strip(), prompt.strip()))
        return cls(specs)


def standard_model_registry() -> StageRegistry:
    """Existing official Pack stages, without duplicated recovery DAG lists."""
    specs = [StageSpec(agent, "research-stage-v1", "model-analysis", "research-evidence-v1",
                       output_contract="verified-analysis-v1", role=agent,
                       prompt_path=f"prompts/{agent}.md")
             for agent in ("financial-analyst", "business-analyst", "accounting-risk-analyst")]
    # Independent reading tasks, not replacements for the later growth and
    # financial counter-analysis. They consume originals only and can finish
    # while deterministic financial preparation is still in flight.
    specs.extend((
        StageSpec("source-industry-analysis", "source-analysis-v1", "model-analysis", "research-evidence-v1",
                  output_contract="source-industry-analysis-v1", role="growth-opportunity-analyst",
                  prompt_path="prompts/growth-opportunity-analyst.md"),
        StageSpec("source-risk-analysis", "source-analysis-v1", "model-analysis", "research-evidence-v1",
                  output_contract="source-risk-analysis-v1", role="skeptical-analyst",
                  prompt_path="prompts/skeptical-analyst.md"),
    ))
    bases = tuple(spec.stage_id for spec in specs)
    specs.extend((
        StageSpec("growth-opportunities", "research-stage-v1", "model-analysis", "growth-evidence-v1", bases,
                  output_contract="growth-opportunities-v1", role="growth-opportunity-analyst",
                  prompt_path="prompts/growth-opportunity-analyst.md"),
        StageSpec("counter-analysis", "research-stage-v1", "model-analysis", "research-evidence-v1", (*bases, "growth-opportunities"),
                  output_contract="counter-analysis-v1", role="skeptical-analyst", prompt_path="prompts/skeptical-analyst.md"),
        StageSpec("forecast-scenarios", "research-stage-v1", "model-analysis", "scenario-evidence-v1", (*bases, "growth-opportunities", "counter-analysis"),
                  output_contract="forecast-scenarios-v1", role="forecast-analyst", prompt_path="prompts/forecast-analyst.md"),
        StageSpec("final-synthesis", "research-stage-v1", "model-analysis", "report-coverage-v1", (*bases, "growth-opportunities", "counter-analysis", "forecast-scenarios"),
                  output_contract="research-report-v1", role="research-synthesizer", prompt_path="prompts/research-synthesizer.md"),
    ))
    requirements = {
        "financial-analyst": (),
    }
    qualitative = {*SOURCE_ANALYSIS_STAGES, "growth-opportunities", "counter-analysis"}
    material = ("quantitative_facts", "sourced_disclosures")
    return StageRegistry(replace(spec, version="research-stage-v4-source-interpretation",
        optional_dependencies=spec.depends_on,
        required_capabilities=("company_identity", *requirements.get(spec.stage_id, ())),
        any_capabilities=("quantitative_facts", "sourced_financial_observations") if spec.stage_id == "financial-analyst" else
            ("sourced_disclosures",) if spec.stage_id in qualitative else
            material if spec.stage_id in {"accounting-risk-analyst", "forecast-scenarios", "final-synthesis"} else ())
        for spec in specs)
