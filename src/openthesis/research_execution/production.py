"""Production runners for the official and declarative research stage graphs."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..domain import ResearchArtifact, ResearchRun
from ..growth import normalize_growth_output
from ..providers import ProviderError
from ..research_coverage import ResearchCoverageContract, obligation_production_plan
from ..research_materials import route_materials, material_capabilities, usable_disclosure
from ..research_assertions import validate_with_assertions
from ..research_content_flow import stage_flow
from ..report_readiness import assess_report_readiness
from ..resilience.contracts import digest as recovery_digest
from ..storage_research_publication import ResearchPublicationSnapshot
from ..valuation_claims import enforce_valuation_consistency
from .checkpoints import candidate_checkpoint, completed_checkpoint, checkpoint_artifact_id, read_checkpoints
from .durable import DurableStageVerifier
from .executor import StageExecutionCancelled, StageExecutor, StageResult, StageValidation, stage_input_digest
from .model import SynthesisContextLimitError
from .registry import SOURCE_ANALYSIS_STAGES, StageRegistry


@dataclass(frozen=True, slots=True)
class GraphBindings:
    """Research-layer functions and protocol versions used by graph production."""

    system_prompt: str
    coverage_version: str
    verification_version: str
    model_role_version: str
    required_synthesis_sections: frozenset[str]
    verify_agent_output: Callable[..., dict[str, Any]]
    trusted_stage_result: Callable[..., dict[str, Any]]
    gate_stage_output: Callable[..., tuple[dict[str, Any], dict[str, Any]]]
    skeptical_prior_artifacts: Callable[..., dict[str, Any]]
    validate_research_synthesis: Callable[..., dict[str, Any]]
    collect_stage_claims: Callable[..., list[dict[str, Any]]]
    presentation_stage_value: Callable[..., Any]
    cancelled_error: type[Exception]


def _save_stage_flow(workflow, run, result, sources, generation):
    with workflow.storage.connect() as db:
        rows = db.execute("""SELECT response_received,state FROM model_request_journal
            WHERE run_id=? AND stage_id=? AND generation=? AND purpose='generation'""",
            (run.run_id, result.stage_id, generation)).fetchall()
    workflow._save(run, "stage-content-flow", result.stage_id,
        stage_flow(result, sources, generation=generation, request_rows=rows), agent_id="content-observability")


def _local_journal_candidates(journal, registry, saved, *, on_read_issues=None):
    """Adapt durable received bodies only; never import old validation flags."""
    candidates = dict(saved)
    received = journal.saved_generation_candidates()
    if journal.candidate_read_issues and on_read_issues is not None:
        on_read_issues(journal.candidate_read_issues)
    for spec in registry.stages:
        row = received.get(spec.stage_id)
        if row is not None and spec.stage_id not in candidates:
            body = row["candidate"]
            candidates[spec.stage_id] = StageResult(spec.stage_id, spec.identity, row["input_digest"],
                body, StageValidation(False, state="needs_action"), recovery_digest(body),
                execution_fingerprint=row["configuration_fingerprint"], has_candidate=True)
    return candidates


class ProductionWorkflowHost(Protocol):
    """Narrow host seam for existing model, storage, and publication behavior."""

    stage_registry: StageRegistry
    pack: Any
    storage: Any
    report_language: str
    ui_language: str
    model_config: Any
    parallel_agents: bool
    cancel_check: Callable[[], bool]

    def _new_request_journal(self, run: ResearchRun) -> Any: ...
    def _set_agent_state(self, agent_id: str, state: str) -> None: ...
    def _progress_text(self, chinese: str, **params: object) -> str: ...
    def _check_cancelled(self) -> None: ...
    def _run_agent_adaptive(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def _run_synthesis_with_budget(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def _bind_request_journal(self, journal: Any) -> Any: ...
    def _save(self, *args: Any, **kwargs: Any) -> Any: ...
    def _artifact(self, *args: Any, **kwargs: Any) -> Any: ...
    def _report_text(self, chinese: str, english: str) -> str: ...
    def _publish_report_result(self, *args: Any, **kwargs: Any) -> Any: ...


def _source_business_inputs(inputs: dict[str, Any], context_json: str,
                            evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Same original snapshot for business, industry and source-risk reading.

    AST unit/title/classification and legal currency proofs are financial
    provenance, not new business disclosures. Reintroducing them here resets
    prompt identity after the early source task, causing duplicate generation.
    Keep the complete canonical filing bodies; never shrink them for cache fit.
    """
    source_evidence = []
    seen = set()
    for item in evidence:
        if (not isinstance(item, dict) or not usable_disclosure(item)
                or not (str(item.get("evidence_id", "")).startswith("filing:")
                        or item.get("kind") == "filing_text")):
            continue
        # Period/entity/scope metadata is part of disclosure attribution.
        # Keep it in actual prompts AND identity; only a compiler-generated
        # financial binding may be excluded from this original-reading lane.
        current = {key: deepcopy(value) for key, value in item.items()
                   if key != "compiled_disclosure_binding"}
        identity = recovery_digest(current)
        if identity not in seen:
            source_evidence.append(current)
            seen.add(identity)
    source_evidence.sort(key=lambda item: (str(item.get("evidence_id", "")), recovery_digest(item)))
    source_context = json.loads(context_json)
    for key in ("facts", "accepted_facts", "metrics", "latest_interim_metrics", "valuation", "market_snapshot",
                "financial_period_coverage", "financial_capabilities", "repair_observations", "issues",
                "compiled_disclosure_binding"):
        source_context.pop(key, None)
    source_context["evidence"] = source_evidence
    company = source_context.get("company", {})
    if isinstance(company, dict):
        company.pop("reporting_currency", None)
    capabilities = material_capabilities({"evidence": source_evidence})
    if isinstance(company, dict) and company.get("cik") and company.get("ticker"):
        capabilities.add("company_identity")
    return {**inputs, "context_json": json.dumps(source_context, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")), "available": sorted(item["evidence_id"] for item in source_evidence
        if item.get("evidence_id")), "evidence_records": {item["evidence_id"]: item for item in source_evidence
        if item.get("evidence_id")}, "capabilities": sorted(capabilities),
        "material_scope_policy": "source-independent-analysis-v4-canonical-originals"}


def stage_execution_plan(registry: StageRegistry, saved: dict[str, StageResult],
                         model_identity: str, invalidated: tuple[str, ...], *,
                         scoped_resume: bool, material_digest: str | dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Preserve independent stage identities, never mislabel a new model call.

    Downstream producers use the current configured model too. Their identity
    must therefore change with an explicitly resumed ancestor, even though a
    material-only retry can otherwise reuse identical validated dependencies.
    """
    affected = set(registry.descendants(invalidated))
    fingerprints = {}
    for spec in registry.stages:
        previous = saved.get(spec.stage_id)
        compatible = (previous is not None and previous.stage_identity == spec.identity
            and all(key in saved for key in spec.depends_on)
            and previous.input_digest == stage_input_digest(
                spec, material_digest.get(spec.stage_id, "") if isinstance(material_digest, dict) else material_digest,
                previous.execution_fingerprint, saved))
        fingerprints[spec.stage_id] = (
            previous.execution_fingerprint
            if scoped_resume and spec.stage_id not in affected and previous is not None
            and previous.execution_fingerprint and compatible else model_identity
        )
    return fingerprints, tuple(spec.stage_id for spec in registry.stages if spec.stage_id in affected)


def run_declarative_ot_workflow(
    workflow: "ProductionWorkflowHost",
    bindings: "GraphBindings",
    run: ResearchRun,
    context: Any,
    evidence: list[dict[str, Any]],
    notify: Callable[[str, int], None],
    publication: ResearchPublicationSnapshot,
) -> ResearchRun:
    """Execute a non-official .ot dependency graph without hidden fixed agents."""
    registry = StageRegistry.from_ot(workflow.pack.workflow)
    registry = registry.bind_prompts(
        {
            spec.prompt_path: workflow.pack.prompt(spec.prompt_path)
            for spec in registry.stages
        },
        shared_instructions=bindings.system_prompt,
    )
    raw_steps = {str(item["id"]).strip(): item for item in workflow.pack.workflow["steps"]}
    steps = {
        spec.stage_id: {**raw_steps[spec.stage_id], "id": spec.stage_id,
                        "prompt": spec.prompt_path, "role": spec.role,
                        "depends_on": list(spec.depends_on)}
        for spec in registry.stages
    }
    order = [spec.stage_id for spec in registry.stages]

    run.workflow_id = f"ot:{workflow.pack.pack_id}"
    run.research_configuration["ot_workflow"] = {
        "schema": workflow.pack.workflow.get("schema", ""),
        "step_ids": order,
        "roles": [steps[step_id]["role"] for step_id in order],
        "settings": workflow.pack.workflow.get("settings", {}),
        "stage_graph_hash": registry.graph_hash,
        "model_role_contract": bindings.model_role_version,
    }
    publication = workflow.storage.update_research_definition(publication,
        workflow_id=run.workflow_id, research_configuration=run.research_configuration)

    available = {item["evidence_id"] for item in evidence}
    evidence_records = {item["evidence_id"]: item for item in evidence}
    results: dict[str, dict[str, Any]] = {}
    verifications: dict[str, dict[str, Any]] = {}
    completed = 0

    def produce(spec, inputs, dependencies):
        return workflow._run_agent_adaptive(
            spec.stage_id, spec.prompt_path, inputs["context_json"],
            {"workflow_role": spec.role,
             "workflow_settings": inputs["workflow_settings"],
             "dependency_outputs": dependencies}, run=run,
            role_id=spec.role, output_schema=spec.output_contract,
        )

    def validate(spec, candidate, inputs):
        verification = bindings.verify_agent_output(
            candidate, set(inputs["available"]), workflow.report_language,
            inputs["evidence_records"],
        )
        trusted = bindings.trusted_stage_result(candidate, verification, inputs["evidence_records"], workflow.report_language)
        state = trusted.get("_verification_state") or (
            "completed_verified" if verification["passed"] else "failed_verification"
        )
        return StageValidation(
            state == "completed_verified",
            trusted if state in {"completed_verified", "completed_partial"} else {},
            state, tuple(verification["issues"]), verification,
        )

    def persist_ot_candidate(spec, input_digest, candidate):
        workflow._check_cancelled()
        workflow._save(run, "stage-candidate", spec.stage_id,
                   candidate_checkpoint(spec, input_digest, candidate,
                       execution_fingerprint=workflow.model_config.public_id),
                   agent_id="research-execution")

    def completed_stage(result):
        nonlocal completed
        workflow._check_cancelled()
        verification = dict(result.validation.record)
        if not verification:
            verification = {"passed": False, "issues": [result.error_code],
                            "claim_count": 0, "verified_claim_count": 0}
        results[result.stage_id] = result.validation.trusted_output or {
            "_verification_state": result.validation.state,
            "_stage_error_code": result.error_code,
        }
        verifications[result.stage_id] = verification
        completed += 1
        step = steps[result.stage_id]
        workflow._save(run, "ot-agent-analysis", str(step["role"]),
                   {"step_id": result.stage_id, "role": step["role"],
                    "depends_on": step["depends_on"],
                    "output_schema": step.get("output_schema", ""),
                    "result": result.candidate, "verification": verification,
                    "stage_identity": result.stage_identity,
                    "input_digest": result.input_digest,
                    "error_code": result.error_code,
                    "trusted_state": result.validation.state},
                   agent_id=result.stage_id)
        _save_stage_flow(workflow, run, result, context.evidence, request_generations[result.stage_id])
        notify(workflow._progress_text("正在执行 .ot 工作流：{completed}/{total}",
                                   completed=completed, total=len(order)),
               20 + round(completed * 68 / len(order)))

    graph_inputs = {
        "context_json": context.compact_json(), "available": sorted(available),
        "evidence_records": evidence_records,
        "workflow_settings": workflow.pack.workflow.get("settings", {}),
        "report_language": workflow.report_language,
        "research_coverage_contract": bindings.coverage_version,
        "research_verification_contract": bindings.verification_version,
        "model_role_contract": bindings.model_role_version,
    }
    request_journal = workflow._new_request_journal(run)
    received_candidates = (_local_journal_candidates(request_journal, registry, {},
        on_read_issues=lambda issues: workflow._save(run, "model-candidate-read-diagnostic", "Saved candidate integrity", {"issues": issues}, agent_id="local-revalidation"))
        if getattr(workflow, "local_revalidation", False) else {})
    request_generations = request_journal.prepare(registry, recovery_digest(graph_inputs),
        local_only=bool(getattr(workflow, "local_revalidation", False)))
    publication = workflow.storage.bind_research_publication_stages(publication, request_generations)
    produce_unbound = produce

    def produce(spec, frozen_inputs, dependencies):
        with workflow._bind_request_journal(request_journal), request_journal.bind(spec.stage_id, request_generations[spec.stage_id]):
            return produce_unbound(spec, frozen_inputs, dependencies)

    validate_candidate = validate

    def validate(spec, candidate, frozen_inputs):
        return validate_with_assertions(lambda: validate_candidate(spec, candidate, frozen_inputs),
            stage_id=spec.stage_id, journal=request_journal)

    def prepare_candidate(spec, candidate, frozen_inputs):
        with workflow._bind_request_journal(request_journal), request_journal.bind(spec.stage_id, request_generations[spec.stage_id]):
            factory = getattr(workflow, "_model_stage_executor", None)
            if callable(factory):
                validate_with_assertions(lambda: validate_candidate(spec, candidate, frozen_inputs),
                    stage_id=spec.stage_id, journal=request_journal,
                    model_executor=factory(), allow_network=True)

    saved = read_checkpoints(workflow.storage.get_artifacts(run.run_id), registry)

    executor = StageExecutor(
        {"model-analysis": produce}, {"research-evidence-v1": validate},
        checkpoint=completed_stage, candidate_checkpoint=persist_ot_candidate,
        candidate_verifier=DurableStageVerifier(workflow.storage, run.run_id, cancel_check=workflow.cancel_check,
                                               request_generations=request_generations),
        candidate_preparer=prepare_candidate,
        cancel_check=workflow.cancel_check, progress=workflow._set_agent_state,
    )
    try:
        executor.execute(registry, graph_inputs, max_workers=2 if workflow.parallel_agents else 1,
           execution_fingerprint=workflow.model_config.public_id, previous=saved.results,
           local_only=bool(getattr(workflow, "local_revalidation", False)),
           revalidation_candidates={**received_candidates, **saved.revalidation_candidates})
    except StageExecutionCancelled as exc:
        raise bindings.cancelled_error() from exc


    sink_ids = [
        step_id
        for step_id in order
        if not any(step_id in steps[other]["depends_on"] for other in order)
    ]
    preferred = next(
        (
            step_id
            for step_id in reversed(order)
            if "synth" in str(steps[step_id]["role"]).lower()
        ),
        sink_ids[-1] if sink_ids else order[-1],
    )
    final_output = results[preferred]
    claims = bindings.collect_stage_claims(*results.values())
    aggregate = bindings.verify_agent_output(
        {"claims": claims}, available, workflow.report_language, evidence_records
    )
    issues = list(aggregate["issues"])
    for step_id in order:
        for issue in verifications[step_id]["issues"]:
            labelled = f"{step_id}: {issue}"
            if labelled not in issues:
                issues.append(labelled)
    aggregate["issues"] = issues
    aggregate["passed"] = not issues and all(
        item["passed"] for item in verifications.values()
    )

    if bindings.required_synthesis_sections.issubset(final_output):
        report_payload = final_output
        synthesis_verification = bindings.validate_research_synthesis(
            final_output, available, workflow.report_language, evidence_records, coverage_context=context
        )
        synthesis_verification["issues"] = list(dict.fromkeys(
            [*aggregate["issues"], *synthesis_verification["issues"]]
        ))
        synthesis_verification["passed"] = (
            aggregate["passed"] and not synthesis_verification["issues"]
        )
        verification = synthesis_verification
    else:
        visible_results = {
            step_id: {
                "role": steps[step_id]["role"],
                "output": bindings.presentation_stage_value(results[step_id], remove_claims=False),
            }
            for step_id in order
        }
        report_payload = {
            "narrative": "\n\n".join(
                f"{step_id} · {item['role']}\n"
                + json.dumps(item["output"], ensure_ascii=False, indent=2)
                for step_id, item in visible_results.items()
            ),
            "workflow_results": visible_results,
            "claims": claims,
        }
        verification = aggregate

    coverage = ResearchCoverageContract().assess(
        report_payload, verification, evidence_records=evidence_records,
        metrics=context.metrics, valuation=context.valuation, company=context.company.to_dict(),
        financial_period_coverage=context.financial_period_coverage)
    readiness = assess_report_readiness(report_payload, verification, coverage=coverage)
    verification["report_readiness"] = readiness.to_dict()
    if not readiness.complete:
        verification["passed"] = False
    report_artifact = workflow._artifact(
        run,
        "research-report",
        workflow._report_text(".ot 工作流研究报告", ".ot Workflow Research Report"),
        {
            "mode": "ot-workflow",
            "research_coverage_contract": bindings.coverage_version,
            "report": report_payload,
            "verification": verification,
            "readiness": readiness.to_dict(),
            "retryable": False,
            "workflow": {
                "pack_id": workflow.pack.pack_id,
                "pack_version": workflow.pack.version,
                "content_identity": workflow.pack.content_hash,
                "selected_output_step": preferred,
                "steps": order,
            },
        },
        agent_id=preferred,
    )
    thesis_content = (
            {
                "thesis": report_payload.get("thesis"),
                "claims": report_payload.get("claims", claims),
                "invalidation_conditions": report_payload.get("invalidation_conditions", []),
                "leading_indicators": report_payload.get("leading_indicators", []),
                "unresolved_questions": report_payload.get("unresolved_questions", []),
            } if report_payload.get("thesis") else None)
    workflow._publish_report_result(run, publication, report_artifact,
        complete=verification["passed"], thesis_content=thesis_content)
    notify(workflow._progress_text(".ot 工作流研究完成"), 100)
    return run


def execute_official_model_graph(
    workflow: "ProductionWorkflowHost",
    bindings: "GraphBindings",
    context: Any,
    run: ResearchRun,
    artifacts: list[dict[str, Any]],
    *,
    publication: ResearchPublicationSnapshot,
    resume_target: str = "model-stages",
    notify: Callable[[str, int], None] | None = None,
    stage_targets: tuple[str, ...] | None = None,
) -> tuple[dict[str, StageResult], ResearchPublicationSnapshot]:
    """Execute or continue the official graph from durable checkpoints."""
    registry = workflow.stage_registry.bind_prompts(
        {
            spec.prompt_path: workflow.pack.prompt(spec.prompt_path)
            for spec in workflow.stage_registry.stages
        },
        shared_instructions=bindings.system_prompt,
    )
    workflow.stage_registry = registry
    base_ids = tuple(
        spec.stage_id for spec in registry.stages if not spec.depends_on
    )
    aliases = {
        "growth": "growth-opportunities",
        "synthesis": "final-synthesis",
    }
    target = aliases.get(resume_target, resume_target)
    if target == "base-agents":
        invalidated = base_ids
    elif target in {"model-stages", ""}:
        invalidated = ()
    elif target in {spec.stage_id for spec in registry.stages}:
        invalidated = (target,)
    else:
        # Unknown historical labels cannot authorize a partial reuse plan.
        invalidated = ()

    evidence = list(context.evidence)
    available = {
        str(item.get("evidence_id"))
        for item in evidence
        if isinstance(item, dict) and item.get("evidence_id")
    }
    evidence_records = {
        str(item.get("evidence_id")): item
        for item in evidence
        if isinstance(item, dict) and item.get("evidence_id")
    }
    inputs = {
        # Stored deterministic metrics are JSON-decoded with sorted object
        # keys. Canonicalize the exact prompt material here so replayed
        # stage inputs keep the same identity across that round-trip.
        "context_json": json.dumps(
            json.loads(context.compact_json()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "available": sorted(available),
        "evidence_records": evidence_records,
        "report_language": workflow.report_language,
        "ui_language": workflow.ui_language,
        "pack_content_identity": workflow.pack.content_hash,
        "research_coverage_contract": bindings.coverage_version,
        "research_verification_contract": bindings.verification_version,
        "capabilities": sorted([
            *material_capabilities(json.loads(context.compact_json())),
            *( ["company_identity"] if context.company.cik and context.company.ticker else []),
        ]),
    }
    # Source-independent business work must use source-independent ACTUAL
    # inputs, not merely a narrower cache digest over a larger prompt.
    source_inputs = _source_business_inputs(inputs, context.compact_json(), evidence)
    stage_inputs = {spec.stage_id: source_inputs if spec.stage_id in SOURCE_ANALYSIS_STAGES and not spec.depends_on else inputs
                    for spec in registry.stages}
    stage_material_digests = {key: recovery_digest(value) for key, value in stage_inputs.items()}
    saved_artifacts = artifacts or []
    checkpoint_read = read_checkpoints(saved_artifacts, registry)
    previous = checkpoint_read.results
    request_journal = workflow._new_request_journal(run)
    local_candidates = (_local_journal_candidates(request_journal, registry, checkpoint_read.revalidation_candidates,
        on_read_issues=lambda issues: workflow._save(run, "model-candidate-read-diagnostic", "Saved candidate integrity", {"issues": issues}, agent_id="local-revalidation"))
        if getattr(workflow, "local_revalidation", False) or target in {"model-stages", ""}
        else checkpoint_read.revalidation_candidates)
    if target in {"model-stages", ""} and saved_artifacts:
        # Explicit continuation changes only missing/unverified stages and
        # their actual descendants. A replacement model is not permission to
        # rerun independent successful base work with the same materials.
        invalidated = tuple(spec.stage_id for spec in registry.stages
            if spec.stage_id not in local_candidates and (spec.stage_id not in previous or previous[spec.stage_id].validation.state not in
            {"completed_verified", "completed_partial", "reused_verified"}))
    fingerprints, invalidated = stage_execution_plan(
        registry, previous, workflow.model_config.public_id, tuple(invalidated),
        scoped_resume=bool(invalidated) or bool(saved_artifacts), material_digest=stage_material_digests)
    request_generations = request_journal.prepare(
        registry, recovery_digest(inputs), invalidated, stage_configurations=fingerprints,
        local_only=bool(getattr(workflow, "local_revalidation", False)), stage_material_digests=stage_material_digests)
    publication = workflow.storage.bind_research_publication_stages(publication, request_generations)
    completed_base: set[str] = set()

    def progress(stage_id: str, state: str) -> None:
        role = registry.get(stage_id).role
        workflow._set_agent_state(stage_id if stage_id in SOURCE_ANALYSIS_STAGES else role, state)
        if stage_id not in base_ids:
            if notify is None:
                return
            running_messages = {
                "growth-opportunities": ("正在研究公司与行业增长机会", 55),
                "counter-analysis": ("正在进行反方审查与压力测试", 68),
                "forecast-scenarios": ("正在生成长期经营情景", 78),
                "final-synthesis": ("正在合成最终长期研究报告", 90),
            }
            completion_messages = {
                "growth-opportunities": ("增长机会研究完成", 65),
                "counter-analysis": ("反方审查完成", 75),
                "forecast-scenarios": ("长期情景完成", 88),
            }
            message = (
                running_messages.get(stage_id) if state == "running"
                else completion_messages.get(stage_id)
                if state in {"completed_verified", "completed_partial", "reused_verified"}
                else None
            )
            if message is not None:
                notify(workflow._progress_text(message[0]), message[1])
            return
        if stage_id in completed_base or state not in {
            "completed_verified", "completed_partial", "failed_verification",
            "needs_action", "reused_verified",
        }:
            return
        completed_base.add(stage_id)
        if notify is None:
            return
        if state in {"failed_verification", "needs_action"}:
            message = workflow._progress_text(
                "基础分析 Agent 未完成 {completed}/{total}：{agent_id}；继续保留其他阶段",
                completed=len(completed_base),
                total=len(base_ids),
                agent_id=role,
            )
        else:
            message = workflow._progress_text(
                "基础分析 Agent 已完成 {completed}/{total}：{agent_id}",
                completed=len(completed_base),
                total=len(base_ids),
                agent_id=role,
            )
        notify(message, 25 + round(len(completed_base) * 21 / len(base_ids)))

    def dossier_from(dependencies: dict[str, Any]) -> dict[str, Any]:
        analyses = {
            stage_id: dependencies[stage_id]
            for stage_id in base_ids
            if stage_id in dependencies
        }
        return {
            "company": context.company.to_dict(),
            "metrics": context.metrics[:5],
            "analyses": analyses,
            "verified_analyses": analyses,
            "source_analyses": {key: value for key, value in analyses.items()
                                if key in SOURCE_ANALYSIS_STAGES},
            "dependency_gaps": dependencies.get("_dependency_gaps", []),
            "unverified_supplement": [
                {
                    "agent_id": stage_id,
                    "state": value.get("_verification_state", "completed_verified"),
                    "claim_verifications": [],
                    "issues": [],
                }
                for stage_id, value in analyses.items()
                if value.get("_verification_state") not in (None, "completed_verified")
            ],
        }

    def produce_unbound(spec, frozen_inputs, dependencies):
        workflow._check_cancelled()
        if spec.stage_id in base_ids:
            assignment = {"assigned_research_obligations": obligation_production_plan(spec.role)}
            if spec.stage_id == "source-industry-analysis":
                assignment = {
                    "assigned_research_obligations": [item for item in obligation_production_plan(spec.role)
                        if item["obligation_id"] == "industry_value_allocation"],
                    "source_research_lane": spec.stage_id,
                    "scope_instructions": "Read the supplied original disclosures for industry structure, value allocation, competitive change and external constraints. This is an independent source-reading task, not final growth analysis. Do not invent accepted financial values, investment opportunities or absent industry benchmarks. Preserve sourced analysis and identify specific missing evidence; later growth analysis will use this result plus newly admitted financial facts.",
                }
            elif spec.stage_id == "source-risk-analysis":
                assignment = {
                    "source_research_lane": spec.stage_id,
                    "scope_instructions": "Independently examine the supplied originals for business-model vulnerabilities, competitive constraints, disclosure contradictions and governance risks. No growth thesis has been supplied yet: do not pretend to rebut one. Cite the original disclosures, separate interpretation from facts, and do not promote raw unadmitted amounts to financial facts. Later counter-analysis must challenge the actual growth hypotheses and newly admitted financial results, retaining this source-risk work without repeating it.",
                }
            return workflow._run_agent_adaptive(
                spec.role,
                spec.prompt_path,
                frozen_inputs["context_json"],
                assignment,
                run=run,
            )
        dossier = dossier_from(dependencies)
        if spec.stage_id == "growth-opportunities":
            return workflow._run_agent_adaptive(
                spec.role,
                spec.prompt_path,
                frozen_inputs["context_json"],
                {"research_dossier": dossier,
                 "assigned_research_obligations": obligation_production_plan(spec.role)},
                run=run,
            )
        growth = dependencies.get("growth-opportunities", {})
        if spec.stage_id == "counter-analysis":
            return workflow._run_agent_adaptive(
                spec.role,
                spec.prompt_path,
                frozen_inputs["context_json"],
                bindings.skeptical_prior_artifacts(context, dossier, growth),
                run=run,
            )
        skeptic = dependencies.get("counter-analysis", {})
        if spec.stage_id == "forecast-scenarios":
            return workflow._run_agent_adaptive(
                spec.role,
                spec.prompt_path,
                frozen_inputs["context_json"],
                {
                    "research_dossier": dossier,
                    "growth_opportunities": growth,
                    "counter_analysis": skeptic,
                },
                run=run,
            )
        forecast = dependencies.get("forecast-scenarios", {})
        try:
            return workflow._run_synthesis_with_budget(
                context, dossier, growth, skeptic, forecast, run=run
            )
        except SynthesisContextLimitError as exc:
            return {
                "claims": [],
                "_response_error": "context_budget_exceeded",
                "_context_sections": exc.sections,
                "_context_required_bytes": exc.required_bytes,
                "_context_available_bytes": exc.available_bytes,
                "_context_counting_mode": exc.counting_mode,
            }
        except ProviderError as exc:
            return {
                "claims": [],
                "_response_error": (
                    "context_budget_exceeded"
                    if exc.code == "MODEL_CONTEXT_CAPACITY"
                    else "provider_error"
                ),
                "_provider_error_code": exc.code,
                "_provider_retryable": exc.retryable,
                "_context_counting_mode": "provider_error_code",
            }

    def validate(spec, candidate, frozen_inputs):
        candidate_copy = deepcopy(candidate)
        if spec.stage_id in base_ids or spec.stage_id == "counter-analysis":
            verification = bindings.verify_agent_output(
                candidate_copy,
                set(frozen_inputs["available"]),
                frozen_inputs["report_language"],
                frozen_inputs["evidence_records"],
            )
            trusted = bindings.trusted_stage_result(candidate_copy, verification,
                frozen_inputs["evidence_records"], frozen_inputs["report_language"])
            state = trusted.get("_verification_state") or (
                "completed_verified" if verification.get("passed") else "failed_verification"
            )
            if state not in {"completed_verified", "completed_partial"}:
                trusted = {}
                state = "failed_verification"
            return StageValidation(
                state == "completed_verified",
                trusted,
                state,
                tuple(verification.get("issues", [])),
                {"verification": verification},
            )

        if spec.stage_id == "growth-opportunities":
            normalized = normalize_growth_output(
                candidate_copy,
                set(frozen_inputs["available"]),
                frozen_inputs["report_language"],
            )
            trusted, verification = bindings.gate_stage_output(
                normalized.output,
                set(frozen_inputs["available"]),
                frozen_inputs["evidence_records"],
                frozen_inputs["report_language"],
            )
            verification["normalization_issues"] = list(normalized.issues)
            state = trusted.get("_verification_state", "failed_verification")
            if state not in {"completed_verified", "completed_partial"}:
                trusted = {}
                state = "failed_verification"
            return StageValidation(
                state == "completed_verified",
                trusted,
                state,
                tuple(verification.get("issues", [])),
                {"verification": verification, "normalized": normalized.output},
            )

        if spec.stage_id == "final-synthesis":
            if candidate_copy.get("_response_error"):
                code = str(
                    candidate_copy.get("_provider_error_code")
                    or candidate_copy.get("_response_error")
                )
                return StageValidation(
                    False,
                    state="needs_action",
                    issues=(code,),
                    record={"response_error": candidate_copy},
                )
            checked, valuation_audit = enforce_valuation_consistency(
                candidate_copy, context.valuation, frozen_inputs["report_language"]
            )
            verification = bindings.validate_research_synthesis(
                checked,
                set(frozen_inputs["available"]),
                frozen_inputs["report_language"],
                frozen_inputs["evidence_records"],
                coverage_context=context,
            )
            verification["valuation_consistency"] = valuation_audit
            supported = verification.get("trusted_report", {})
            state = ("completed_verified" if verification.get("passed") else "completed_partial"
                     if any(key != "claims" for key in supported) or supported.get("claims")
                     else "failed_verification")
            return StageValidation(
                state == "completed_verified",
                supported if state in {"completed_verified", "completed_partial"} else {},
                state,
                tuple(verification.get("issues", [])),
                {"verification": verification, "valuation_consistency": valuation_audit},
            )

        trusted, verification = bindings.gate_stage_output(
            candidate_copy,
            set(frozen_inputs["available"]),
            frozen_inputs["evidence_records"],
            frozen_inputs["report_language"],
        )
        state = trusted.get("_verification_state", "failed_verification")
        if state not in {"completed_verified", "completed_partial"}:
            trusted = {}
            state = "failed_verification"
        return StageValidation(
            state == "completed_verified",
            trusted,
            state,
            tuple(verification.get("issues", [])),
            {"verification": verification},
        )

    compatibility_artifacts = {
        "growth-opportunities": ("growth-opportunities", "增长机会", "Growth Opportunities"),
        "counter-analysis": ("counter-analysis", "反方审查", "Counter-analysis"),
        "forecast-scenarios": ("forecast-scenarios", "长期经营情景", "Long-term Operating Scenarios"),
    }

    def json_checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
        # SQLite JSON round-tripping turns tuples into lists. Normalize
        # before artifact identity generation so replayed checkpoints are
        # byte/shape stable when an immutable record is written again.
        return json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def persist_candidate(spec, input_digest, candidate):
        workflow._check_cancelled()
        payload = candidate_checkpoint(
            spec,
            input_digest,
            candidate,
            execution_fingerprint=fingerprints[spec.stage_id],
        )
        workflow._save(
            run,
            "stage-candidate",
            spec.stage_id,
            json_checkpoint(payload),
            agent_id="research-execution",
        )

    def persist_result(result: StageResult) -> None:
        nonlocal publication
        workflow._check_cancelled()
        spec = registry.get(result.stage_id)
        payload = json_checkpoint(completed_checkpoint(result))
        # The durable verifier and executor publish the same immutable object.
        # A second artifact identity would duplicate proof/snapshot commits.
        workflow.storage.save_artifact(ResearchArtifact(
            checkpoint_artifact_id(run.run_id, payload), run.run_id, "stage-checkpoint", spec.stage_id,
            payload, model_id=result.execution_fingerprint, agent_id="research-execution"))
        _save_stage_flow(workflow, run, result, context.evidence, request_generations[result.stage_id])
        state = result.validation.state
        workflow._set_agent_state(spec.stage_id if spec.stage_id in SOURCE_ANALYSIS_STAGES else spec.role, state)
        if (
            result.stage_id == "final-synthesis"
            and isinstance(result.candidate, dict)
            and result.candidate.get("_provider_retryable") is True
        ):
            workflow._set_agent_state(spec.role, "waiting_retryable")
        record = result.validation.record
        verification = record.get("verification", record) if isinstance(record, dict) else {}
        if result.stage_id in base_ids:
            workflow._save(
                run,
                "agent-analysis",
                result.stage_id,
                {
                    "result": result.candidate,
                    "verification": verification,
                    "trusted_state": state,
                },
                agent_id=result.stage_id,
            )
        elif result.stage_id in compatibility_artifacts:
            artifact_type, chinese, english = compatibility_artifacts[result.stage_id]
            content = dict(result.validation.trusted_output)
            content["_audit"] = {
                "result": result.candidate,
                "verification": verification,
            }
            workflow._save(
                run,
                artifact_type,
                workflow._report_text(chinese, english),
                content,
                agent_id=spec.role,
            )
        if result.error_code or state in {"needs_action", "failed_verification"}:
            code = result.error_code or (
                result.validation.issues[0] if result.validation.issues else "MODEL_RESPONSE_INVALID"
            )
            retryable = result.retryable or state == "failed_verification"
            legacy_stage = {
                "growth-opportunities": "growth-opportunities",
                "counter-analysis": "counterarguments",
                "forecast-scenarios": "scenarios",
                "final-synthesis": "synthesis",
            }.get(result.stage_id, result.stage_id)
            workflow._save(
                run,
                "stage-outcome",
                workflow._report_text("研究阶段未完成", "Research stage incomplete"),
                {
                    "stage": legacy_stage,
                    "agent_id": spec.role,
                    "outcome": "waiting_retryable" if retryable else "needs_action",
                    "error_code": code,
                    "retryable": retryable,
                    "next_action": "resume_failed_model_stages",
                },
                agent_id="research-continuity",
            )
        from ..report_content import assemble_trusted_report_payload
        local_payload = assemble_trusted_report_payload(
            workflow.storage.get_artifacts(run.run_id), run_id=run.run_id, input_digest=publication.input_digest)
        if local_payload is not None and stage_targets is None:
            local_payload["research_coverage_contract"] = bindings.coverage_version
            local_report = workflow._artifact(run, "research-report",
                workflow._report_text("已验证的阶段研究", "Verified stage research"), local_payload,
                agent_id="local-report-assembly")
            publication = workflow.storage.publish_research_progress(
                publication, local_report, cancel_check=workflow.cancel_check)

    validate_candidate = validate

    def validate(spec, candidate, frozen_inputs):
        # DurableStageVerifier remains zero-provider. Reuse only source-bound
        # semantic observations already received through the request journal.
        return validate_with_assertions(lambda: validate_candidate(spec, candidate, frozen_inputs),
            stage_id=spec.stage_id, journal=request_journal)

    def produce(spec, frozen_inputs, dependencies):
        with workflow._bind_request_journal(request_journal), request_journal.bind(spec.stage_id, request_generations[spec.stage_id]):
            return produce_unbound(spec, frozen_inputs, dependencies)

    def prepare_candidate(spec, candidate, frozen_inputs):
        # Called only for fresh candidates, after their durable checkpoint.
        # Revalidation and history reads never enter this provider lane.
        with workflow._bind_request_journal(request_journal), request_journal.bind(spec.stage_id, request_generations[spec.stage_id]):
            factory = getattr(workflow, "_model_stage_executor", None)
            if callable(factory):
                validate_with_assertions(lambda: validate_candidate(spec, candidate, frozen_inputs),
                    stage_id=spec.stage_id, journal=request_journal,
                    model_executor=factory(), allow_network=True)

    produce_scoped = produce

    def produce(spec, frozen_inputs, dependencies):
        if fingerprints[spec.stage_id] != workflow.model_config.public_id:
            # Revalidation may reveal a changed dependency after planning.
            # Never charge a new provider under an old model/request identity.
            raise ProviderError("Stage configuration needs a fresh recovery plan",
                                code="MODEL_STAGE_CONFIGURATION_REPLAN_REQUIRED")
        return produce_scoped(spec, frozen_inputs, dependencies)

    executor = StageExecutor(
        {"model-analysis": produce},
        {
            "research-evidence-v1": validate,
            "growth-evidence-v1": validate,
            "scenario-evidence-v1": validate,
            "report-coverage-v1": validate,
        },
        checkpoint=persist_result,
        candidate_checkpoint=persist_candidate,
        candidate_preparer=prepare_candidate,
        candidate_verifier=DurableStageVerifier(workflow.storage, run.run_id, cancel_check=workflow.cancel_check,
                                               request_generations=request_generations),
        cancel_check=workflow.cancel_check,
        progress=progress,
    )
    try:
        graph = executor.execute(
            registry,
            inputs,
            previous=previous,
            invalidated=tuple(invalidated),
            max_workers=2 if workflow.parallel_agents else 1,
            execution_fingerprint=workflow.model_config.public_id,
            execution_fingerprints=fingerprints,
            propagate_invalidation=False,
            local_only=bool(getattr(workflow, "local_revalidation", False)),
            revalidation_candidates=(local_candidates
                if getattr(workflow, "local_revalidation", False) or target in {"model-stages", ""} else None),
            stage_inputs=stage_inputs,
            targets=stage_targets,
        )
        return graph, publication
    except StageExecutionCancelled as exc:
        raise bindings.cancelled_error() from exc


