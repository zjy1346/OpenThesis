from __future__ import annotations

import hashlib
import json
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
from .growth import normalize_growth_output
from .i18n import EN, OUTPUT_LANGUAGE_INSTRUCTIONS, normalize_language, translate
from .packs import ResearchPack
from .providers import ModelConfig, ModelProvider
from .storage import Storage


ProgressCallback = Callable[[str, int], None]
CancelCheck = Callable[[], bool]
AgentProgressCallback = Callable[[str, str], None]


class ResearchCancelled(RuntimeError):
    def __init__(self, message: str = "研究已由用户取消", run_id: str = ""):
        super().__init__(message)
        self.run_id = run_id


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
    output: dict[str, Any],
    available_evidence: set[str],
    language: str = "zh-CN",
) -> dict[str, Any]:
    english = normalize_language(language) == EN
    issues: list[str] = []
    claims = output.get("claims", [])
    if claims is not None and not isinstance(claims, list):
        issues.append("claims must be an array" if english else "claims 必须是数组")
        claims = []
    verified_count = 0
    unsupported_count = 0
    for claim in claims or []:
        if not isinstance(claim, dict):
            issues.append(
                "A claim is not a JSON object" if english else "存在非对象 claim"
            )
            continue
        evidence_ids = claim.get("evidence_ids", [])
        if not isinstance(evidence_ids, list):
            issues.append(
                "claim.evidence_ids must be an array"
                if english
                else "claim.evidence_ids 必须是数组"
            )
            continue
        missing = [item for item in evidence_ids if item not in available_evidence]
        if missing:
            prefix = "Unknown evidence reference: " if english else "引用不存在："
            issues.append(prefix + ", ".join(map(str, missing)))
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
        cancel_check: CancelCheck | None = None,
        report_language: str = "zh-CN",
        ui_language: str = "zh-CN",
        parallel_agents: bool = True,
        agent_progress: AgentProgressCallback | None = None,
    ):
        self.storage = storage
        self.pack = research_pack
        self.provider = provider
        self.model_config = model_config
        self.cancel_check = cancel_check or (lambda: False)
        self.report_language = normalize_language(report_language)
        self.ui_language = normalize_language(ui_language)
        self.parallel_agents = parallel_agents
        self.agent_progress = agent_progress or (lambda _agent_id, _state: None)

    def _report_text(self, chinese: str, english: str) -> str:
        return english if self.report_language == EN else chinese

    def _progress_text(self, chinese: str, **params: object) -> str:
        return translate(chinese, self.ui_language, **params)

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise ResearchCancelled()

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
            report_language=self.report_language,
        )
        self.storage.save_run(run)
        try:
            self._check_cancelled()
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
            self._check_cancelled()
            notify(self._progress_text("已完成确定性财务计算"), 15)
            summary = deterministic_summary(
                company.name, metrics, self.report_language
            )
            self._save(
                run,
                "deterministic-financial-summary",
                self._report_text(
                    "确定性财务概览", "Deterministic Financial Overview"
                ),
                {
                    "markdown": summary,
                    "metrics": metrics,
                    "evidence": evidence,
                },
                agent_id="calculation-engine",
            )
            if valuation is not None:
                self._save(
                    run,
                    "deterministic-valuation",
                    self._report_text(
                        "反向 DCF 隐含预期", "Reverse DCF Implied Expectations"
                    ),
                    valuation,
                    agent_id="valuation-engine",
                )
            if self.provider is None:
                self._save(
                    run,
                    "research-report",
                    self._report_text("基础研究报告", "Basic Research Report"),
                    {
                        "mode": "deterministic-only",
                        "summary": summary,
                        "notice": self._report_text(
                            "未配置模型，因此没有生成定性研究、增长机会和长期情景。",
                            "No model was configured, so qualitative research, "
                            "growth opportunities, and long-term scenarios were "
                            "not generated.",
                        ),
                    },
                    agent_id="deterministic-fallback",
                )
                run.status = RunStatus.PARTIAL
                run.completed_at = utc_now_iso()
                self.storage.save_run(run)
                notify(
                    self._progress_text(
                        "基础财务分析完成；配置模型后可运行完整研究"
                    ),
                    100,
                )
                return run

            available = {item["evidence_id"] for item in evidence}
            stage_one = {
                "financial-analyst": "prompts/financial-analyst.md",
                "business-analyst": "prompts/business-analyst.md",
                "accounting-risk-analyst": "prompts/accounting-risk-analyst.md",
            }
            stage_results: dict[str, dict[str, Any]] = {}
            notify(
                self._progress_text(
                    "正在并行运行财务、商业与会计风险 Agent（0/3）"
                ),
                25,
            )
            notify(
                (
                    "Running base agents in parallel (0/3)"
                    if self.ui_language == EN and self.parallel_agents
                    else "Running base agents sequentially (0/3)"
                    if self.ui_language == EN
                    else "正在并行运行基础 Agent（0/3）"
                    if self.parallel_agents
                    else "正在按顺序运行基础 Agent（0/3）"
                ),
                25,
            )
            for agent_id in stage_one:
                self._set_agent_state(agent_id, "queued")
            if self.parallel_agents:
                executor = ThreadPoolExecutor(max_workers=3)
                futures = {}
                try:
                    for agent_id, prompt_path in stage_one.items():
                        self._set_agent_state(agent_id, "running")
                        futures[executor.submit(
                            self._run_agent, agent_id, prompt_path, context.compact_json(), {}
                        )] = agent_id
                    pending = set(futures)
                    completed_agents = 0
                    while pending:
                        self._check_cancelled()
                        done, pending = wait(
                            pending, timeout=0.05, return_when=FIRST_COMPLETED
                        )
                        for future in done:
                            agent_id = futures[future]
                            try:
                                result = future.result()
                            except ResearchCancelled:
                                self._set_agent_state(agent_id, "cancelled")
                                raise
                            except Exception:
                                self._set_agent_state(agent_id, "failed")
                                raise
                            self._check_cancelled()
                            stage_results[agent_id] = result
                            verification = verify_agent_output(
                                result, available, self.report_language
                            )
                            self._save(
                                run,
                                "agent-analysis",
                                agent_id,
                                {"result": result, "verification": verification},
                                agent_id=agent_id,
                            )
                            self._set_agent_state(agent_id, "completed")
                            completed_agents += 1
                            notify(
                                self._progress_text(
                                    "基础分析 Agent 已完成 {completed}/3：{agent_id}",
                                    completed=completed_agents,
                                    agent_id=agent_id,
                                ),
                                25 + completed_agents * 7,
                            )
                finally:
                    executor.shutdown(
                        wait=not self.cancel_check(),
                        cancel_futures=True,
                    )
            else:
                completed_agents = 0
                for agent_id, prompt_path in stage_one.items():
                    self._check_cancelled()
                    self._set_agent_state(agent_id, "running")
                    try:
                        result = self._run_agent(
                            agent_id, prompt_path, context.compact_json(), {}
                        )
                    except ResearchCancelled:
                        self._set_agent_state(agent_id, "cancelled")
                        raise
                    except Exception:
                        self._set_agent_state(agent_id, "failed")
                        raise
                    self._check_cancelled()
                    stage_results[agent_id] = result
                    verification = verify_agent_output(
                        result, available, self.report_language
                    )
                    self._save(
                        run,
                        "agent-analysis",
                        agent_id,
                        {"result": result, "verification": verification},
                        agent_id=agent_id,
                    )
                    self._set_agent_state(agent_id, "completed")
                    completed_agents += 1
                    notify(
                        self._progress_text(
                            "基础分析 Agent 已完成 {completed}/3：{agent_id}",
                            completed=completed_agents,
                            agent_id=agent_id,
                        ),
                        25 + completed_agents * 7,
                    )
            if False:
                futures = {
                    executor.submit(
                        self._run_agent, agent_id, prompt_path, context.compact_json(), {}
                    ): agent_id
                    for agent_id, prompt_path in stage_one.items()
                }
                completed_agents = 0
                for future in as_completed(futures):
                    agent_id = futures[future]
                    result = future.result()
                    self._check_cancelled()
                    stage_results[agent_id] = result
                    verification = verify_agent_output(
                        result, available, self.report_language
                    )
                    self._save(
                        run,
                        "agent-analysis",
                        agent_id,
                        {"result": result, "verification": verification},
                        agent_id=agent_id,
                    )
                    completed_agents += 1
                    notify(
                        self._progress_text(
                            "基础分析 Agent 已完成 {completed}/3：{agent_id}",
                            completed=completed_agents,
                            agent_id=agent_id,
                        ),
                        25 + completed_agents * 7,
                    )

            dossier = {
                "company": company.to_dict(),
                "metrics": metrics[:5],
                "analyses": stage_results,
            }
            self._save(
                run,
                "verified-research-dossier",
                self._report_text(
                    "经过验证的研究档案", "Verified Research Dossier"
                ),
                dossier,
                agent_id="evidence-verifier",
            )
            notify(self._progress_text("基础研究档案完成"), 50)

            notify(self._progress_text("正在研究公司与行业增长机会"), 52)
            growth_raw = self._run_agent(
                "growth-opportunity-analyst",
                "prompts/growth-opportunity-analyst.md",
                context.compact_json(),
                {"research_dossier": dossier},
            )
            growth_validation = normalize_growth_output(
                growth_raw,
                available,
                self.report_language,
            )
            growth = growth_validation.output
            self._save(
                run,
                "growth-opportunities",
                self._report_text("增长机会", "Growth Opportunities"),
                growth,
                agent_id="growth-opportunity-analyst",
            )
            notify(self._progress_text("增长机会研究完成"), 65)

            notify(self._progress_text("正在进行反方审查与压力测试"), 67)
            skeptic = self._run_agent(
                "skeptical-analyst",
                "prompts/skeptical-analyst.md",
                context.compact_json(),
                {"research_dossier": dossier, "growth_opportunities": growth},
            )
            self._save(
                run,
                "counter-analysis",
                self._report_text("反方审查", "Counter-analysis"),
                skeptic,
                agent_id="skeptical-analyst",
            )
            notify(self._progress_text("反方审查完成"), 75)

            notify(self._progress_text("正在生成长期经营情景"), 77)
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
                self._report_text("长期经营情景", "Long-term Operating Scenarios"),
                forecast,
                agent_id="forecast-analyst",
            )
            notify(self._progress_text("长期情景完成"), 88)

            notify(self._progress_text("正在合成最终长期研究报告"), 90)
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
            verification = verify_agent_output(
                synthesis, available, self.report_language
            )
            self._save(
                run,
                "research-report",
                self._report_text(
                    "完整长期研究报告", "Complete Long-term Research Report"
                ),
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
                self._report_text(
                    f"投资逻辑 v{thesis_version['version']}",
                    f"Investment Thesis v{thesis_version['version']}",
                ),
                thesis_version,
                agent_id="thesis-versioning",
            )
            run.status = RunStatus.COMPLETED if verification["passed"] else RunStatus.PARTIAL
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            notify(self._progress_text("研究完成"), 100)
            return run
        except ResearchCancelled as exc:
            exc.run_id = run.run_id
            run.errors.append(str(exc))
            run.status = RunStatus.CANCELLED
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            raise
        except Exception as exc:
            run.errors.append(str(exc))
            run.status = RunStatus.FAILED
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            raise

    def _set_agent_state(self, agent_id: str, state: str) -> None:
        self.agent_progress(agent_id, state)

    def _generate_with_cancellation(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        """Run a provider call without allowing a cancelled UI job to hang.

        Provider SDKs generally expose a blocking request. Running that call in
        a daemon worker lets the workflow acknowledge cancellation immediately;
        the late result is deliberately discarded by the cancelled workflow.
        """
        result: dict[str, Any] | None = None
        error: BaseException | None = None

        def invoke() -> None:
            nonlocal result, error
            try:
                result = self.provider.generate(  # type: ignore[union-attr]
                    system_prompt,
                    user_prompt,
                    json_mode=json_mode,
                )
            except Exception as exc:
                error = exc

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.05)
            if self.cancel_check():
                raise ResearchCancelled()
        if error is not None:
            raise error
        if result is None:
            raise RuntimeError("model provider returned no result")
        return result
        if self.provider is None:
            raise RuntimeError("模型 Provider 未配置")
        role_prompt = self.pack.prompt(prompt_path)
        language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS[self.report_language]
        user_prompt = json.dumps(
            {
                "agent": agent_id,
                "task_instructions": role_prompt,
                "output_language": self.report_language,
                "output_language_instruction": language_instruction,
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
        try:
            result = self.provider.generate(
                CORE_SYSTEM_PROMPT + "\n" + language_instruction,
                user_prompt,
                json_mode=True,
            )
        except Exception:
            self._check_cancelled()
            raise
        self._check_cancelled()
        return result

    def _run_agent(
        self,
        agent_id: str,
        prompt_path: str,
        context_json: str,
        prior_artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        self._check_cancelled()
        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        role_prompt = self.pack.prompt(prompt_path)
        language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS[self.report_language]
        user_prompt = json.dumps(
            {
                "agent": agent_id,
                "task_instructions": role_prompt,
                "output_language": self.report_language,
                "output_language_instruction": language_instruction,
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
        try:
            result = self._generate_with_cancellation(
                CORE_SYSTEM_PROMPT + "\n" + language_instruction,
                user_prompt,
                json_mode=True,
            )
        except Exception:
            self._check_cancelled()
            raise
        self._check_cancelled()
        return result

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
