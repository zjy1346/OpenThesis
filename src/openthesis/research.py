from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from .domain import (
    Company,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)
from .financials import calculate_metrics, deterministic_summary, reverse_dcf_analysis
from .packs import ResearchPack
from .providers import ModelConfig, ModelProvider
from .storage import Storage


ProgressCallback = Callable[[str, int], None]


CORE_SYSTEM_PROMPT = """\
You are a careful long-term company research analyst inside OpenThesis.
Use only the evidence supplied in the task. Never invent financial values,
citations, customers, products, events, or management statements.
Keep facts, calculations, inferences, assumptions, forecasts, risks, and
unknowns separate. If evidence is insufficient, say so explicitly.
Return one valid JSON object and no markdown wrapper. This is research
assistance, not personalized investment advice and never a trade instruction.
"""


@dataclass(slots=True)
class ResearchContext:
    company: Company
    facts: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    valuation: dict[str, Any] | None = None

    def compact_json(self) -> str:
        return json.dumps(
            {
                "company": self.company.to_dict(),
                "metrics": self.metrics[:5],
                "evidence": self.evidence[:30],
                "valuation": self.valuation,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def build_fact_evidence(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for fact in facts:
        evidence.append(
            {
                "evidence_id": f"fact:{fact['fact_id']}",
                "kind": "financial_fact",
                "concept": fact["concept"],
                "value": fact["value"],
                "unit": fact["unit"],
                "fiscal_year": fact["fiscal_year"],
                "filed_at": fact["filed_at"],
                "source_url": fact["source_url"],
            }
        )
    return evidence


def verify_agent_output(
    output: dict[str, Any], available_evidence: set[str]
) -> dict[str, Any]:
    issues: list[str] = []
    claims = output.get("claims", [])
    if claims is not None and not isinstance(claims, list):
        issues.append("claims 必须是数组")
        claims = []
    verified_count = 0
    unsupported_count = 0
    for claim in claims or []:
        if not isinstance(claim, dict):
            issues.append("存在非对象 claim")
            continue
        evidence_ids = claim.get("evidence_ids", [])
        if not isinstance(evidence_ids, list):
            issues.append("claim.evidence_ids 必须是数组")
            continue
        missing = [item for item in evidence_ids if item not in available_evidence]
        if missing:
            issues.append(f"引用不存在：{', '.join(map(str, missing))}")
        if claim.get("kind") == "fact" and not evidence_ids:
            unsupported_count += 1
        elif not missing:
            verified_count += 1
    return {
        "structured_output_valid": bool(output.get("structured_output_valid", True)),
        "claim_count": len(claims or []),
        "verified_claim_count": verified_count,
        "unsupported_fact_count": unsupported_count,
        "issues": issues,
        "passed": not issues and unsupported_count == 0,
    }


class ResearchWorkflow:
    def __init__(
        self,
        storage: Storage,
        research_pack: ResearchPack,
        provider: ModelProvider | None,
        model_config: ModelConfig,
    ):
        self.storage = storage
        self.pack = research_pack
        self.provider = provider
        self.model_config = model_config

    def run(
        self,
        company: Company,
        facts: list[dict[str, Any]],
        filing_evidence: list[dict[str, Any]] | None = None,
        valuation_inputs: dict[str, float] | None = None,
        progress: ProgressCallback | None = None,
    ) -> ResearchRun:
        notify = progress or (lambda _message, _percent: None)
        run = ResearchRun(
            run_id=uuid.uuid4().hex,
            company=company,
            workflow_id="complete-fundamental-research",
            research_pack_id=self.pack.pack_id,
            research_pack_version=self.pack.version,
            provider_id=self.model_config.provider,
            model_id=self.model_config.model,
            data_as_of=utc_now_iso(),
            status=RunStatus.RUNNING,
        )
        self.storage.save_run(run)
        try:
            metrics = calculate_metrics(facts)
            evidence = build_fact_evidence(facts)
            evidence.extend(filing_evidence or [])
            valuation = None
            if valuation_inputs and valuation_inputs.get("market_cap", 0) > 0:
                valuation = reverse_dcf_analysis(
                    metrics,
                    valuation_inputs["market_cap"],
                    valuation_inputs.get("discount_rate", 0.10),
                    valuation_inputs.get("terminal_growth", 0.03),
                )
            context = ResearchContext(company, facts, metrics, evidence, valuation)
            notify("已完成确定性财务计算", 15)
            self._save(
                run,
                "deterministic-financial-summary",
                "确定性财务概览",
                {
                    "markdown": deterministic_summary(company.name, metrics),
                    "metrics": metrics,
                    "evidence": evidence,
                },
                agent_id="calculation-engine",
            )
            if valuation is not None:
                self._save(
                    run,
                    "deterministic-valuation",
                    "反向 DCF 隐含预期",
                    valuation,
                    agent_id="valuation-engine",
                )
            if self.provider is None:
                self._save(
                    run,
                    "research-report",
                    "基础研究报告",
                    {
                        "mode": "deterministic-only",
                        "summary": deterministic_summary(company.name, metrics),
                        "notice": "未配置模型，因此没有生成定性研究、增长机会和长期情景。",
                    },
                    agent_id="deterministic-fallback",
                )
                run.status = RunStatus.PARTIAL
                run.completed_at = utc_now_iso()
                self.storage.save_run(run)
                notify("基础财务分析完成；配置模型后可运行完整研究", 100)
                return run

            available = {item["evidence_id"] for item in evidence}
            stage_one = {
                "financial-analyst": "prompts/financial-analyst.md",
                "business-analyst": "prompts/business-analyst.md",
                "accounting-risk-analyst": "prompts/accounting-risk-analyst.md",
            }
            stage_results: dict[str, dict[str, Any]] = {}
            notify("正在并行运行财务、商业与会计风险 Agent", 25)
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(
                        self._run_agent, agent_id, prompt_path, context.compact_json(), {}
                    ): agent_id
                    for agent_id, prompt_path in stage_one.items()
                }
                for future in as_completed(futures):
                    agent_id = futures[future]
                    result = future.result()
                    stage_results[agent_id] = result
                    verification = verify_agent_output(result, available)
                    self._save(
                        run,
                        "agent-analysis",
                        agent_id,
                        {"result": result, "verification": verification},
                        agent_id=agent_id,
                    )

            dossier = {
                "company": company.to_dict(),
                "metrics": metrics[:5],
                "analyses": stage_results,
            }
            self._save(
                run,
                "verified-research-dossier",
                "经过验证的研究档案",
                dossier,
                agent_id="evidence-verifier",
            )
            notify("基础研究档案完成", 50)

            growth = self._run_agent(
                "growth-opportunity-analyst",
                "prompts/growth-opportunity-analyst.md",
                context.compact_json(),
                {"research_dossier": dossier},
            )
            self._save(
                run,
                "growth-opportunities",
                "增长机会",
                growth,
                agent_id="growth-opportunity-analyst",
            )
            notify("增长机会研究完成", 65)

            skeptic = self._run_agent(
                "skeptical-analyst",
                "prompts/skeptical-analyst.md",
                context.compact_json(),
                {"research_dossier": dossier, "growth_opportunities": growth},
            )
            self._save(
                run,
                "counter-analysis",
                "反方审查",
                skeptic,
                agent_id="skeptical-analyst",
            )
            notify("反方审查完成", 75)

            forecast = self._run_agent(
                "forecast-analyst",
                "prompts/forecast-analyst.md",
                context.compact_json(),
                {
                    "research_dossier": dossier,
                    "growth_opportunities": growth,
                    "counter_analysis": skeptic,
                },
            )
            self._save(
                run,
                "forecast-scenarios",
                "长期经营情景",
                forecast,
                agent_id="forecast-analyst",
            )
            notify("长期情景完成", 88)

            synthesis = self._run_agent(
                "research-synthesizer",
                "prompts/research-synthesizer.md",
                context.compact_json(),
                {
                    "research_dossier": dossier,
                    "growth_opportunities": growth,
                    "counter_analysis": skeptic,
                    "forecast": forecast,
                },
            )
            verification = verify_agent_output(synthesis, available)
            self._save(
                run,
                "research-report",
                "完整长期研究报告",
                {"report": synthesis, "verification": verification},
                agent_id="research-synthesizer",
            )
            thesis_content = {
                "thesis": synthesis.get("thesis"),
                "claims": synthesis.get("claims", []),
                "invalidation_conditions": synthesis.get(
                    "invalidation_conditions", []
                ),
                "leading_indicators": synthesis.get("leading_indicators", []),
                "unresolved_questions": synthesis.get("unresolved_questions", []),
                "growth_opportunities": growth.get("opportunities", growth),
                "scenarios": forecast.get("scenarios", forecast),
            }
            thesis_version = self.storage.save_thesis_version(
                company.cik,
                thesis_content,
                run_id=run.run_id,
                created_by=self.model_config.public_id,
                created_at=utc_now_iso(),
            )
            self._save(
                run,
                "thesis-snapshot",
                f"投资逻辑 v{thesis_version['version']}",
                thesis_version,
                agent_id="thesis-versioning",
            )
            run.status = RunStatus.COMPLETED if verification["passed"] else RunStatus.PARTIAL
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            notify("研究完成", 100)
            return run
        except Exception as exc:
            run.errors.append(str(exc))
            run.status = RunStatus.FAILED
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            raise

    def _run_agent(
        self,
        agent_id: str,
        prompt_path: str,
        context_json: str,
        prior_artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        if self.provider is None:
            raise RuntimeError("模型 Provider 未配置")
        role_prompt = self.pack.prompt(prompt_path)
        user_prompt = json.dumps(
            {
                "agent": agent_id,
                "task_instructions": role_prompt,
                "research_context": json.loads(context_json),
                "prior_artifacts": prior_artifacts,
                "required_claim_shape": {
                    "text": "string",
                    "kind": "fact|calculation|inference|assumption|forecast|risk|unknown",
                    "confidence": "0..1 or null",
                    "evidence_ids": ["fact:<id>"],
                },
            },
            ensure_ascii=False,
        )
        return self.provider.generate(CORE_SYSTEM_PROMPT, user_prompt, json_mode=True)

    def _save(
        self,
        run: ResearchRun,
        artifact_type: str,
        title: str,
        content: dict[str, Any],
        *,
        agent_id: str,
    ) -> ResearchArtifact:
        raw = json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        identity = agent_id.encode() + b"|" + raw
        suffix = hashlib.sha256(identity).hexdigest()[:10]
        artifact = ResearchArtifact(
            artifact_id=f"{run.run_id}:{artifact_type}:{suffix}",
            run_id=run.run_id,
            artifact_type=artifact_type,
            title=title,
            content=content,
            model_id=self.model_config.public_id,
            agent_id=agent_id,
        )
        self.storage.save_artifact(artifact)
        return artifact
