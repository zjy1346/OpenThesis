from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

from .domain import (
    Company,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)
from .financials import (
    calculate_interim_metrics,
    calculate_metrics,
    deterministic_summary,
    reverse_dcf_analysis,
)
from .financial_ingestion import FinancialProfile
from .growth import normalize_growth_output
from .i18n import EN, OUTPUT_LANGUAGE_INSTRUCTIONS, UI_HANT, ZH_HANT, normalize_language, translate
from .packs import ResearchPack
from .providers import ModelConfig, ModelProvider, ProviderError
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
Read company.market, exchange, reporting_currency, accounting_standard, and
industry_support before analysis. For CN_A listings, explicitly examine
controlling-shareholder and related-party exposure, pledges, subsidies,
regulatory/delisting risks, audit opinions, and CAS reporting scope; never treat
cumulative quarterly income or cash flow as a standalone quarter. For HK
listings, explicitly examine listing structure, controlling shareholders,
connected transactions, VIE exposure where evidenced, HKEX compliance, reporting
standard, and currency differences. For financial_beta issuers, do not apply an
ordinary-company free-cash-flow valuation framework.
Always distinguish fiscal_period values: FY is a full year, while Q1, H1, and Q3
are cumulative interim periods. Cover the latest supplied interim period, compare
it only with the same prior-year period, and never combine it with FY totals.
"""


@dataclass(slots=True)
class ResearchContext:
    company: Company
    facts: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    interim_metrics: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    valuation: dict[str, Any] | None = None
    market_snapshot: dict[str, Any] | None = None

    def compact_json(self) -> str:
        return json.dumps(
            {
                "company": self.company.to_dict(),
                "metrics": self.metrics[:5],
                "latest_interim_metrics": self.interim_metrics[:3],
                "evidence": self.evidence[:30],
                "valuation": self.valuation,
                "market_snapshot": self.market_snapshot,
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
                "fiscal_period": fact.get("fiscal_period", ""),
                "form_type": fact.get("form_type", ""),
                "start_date": fact.get("start_date", ""),
                "end_date": fact.get("end_date", ""),
                "filed_at": fact["filed_at"],
                "source_url": fact["source_url"],
            }
        )
    return evidence


def verify_agent_output(
    output: dict[str, Any],
    available_evidence: set[str],
    language: str = "zh-CN",
    evidence_records: dict[str, dict[str, Any]] | None = None,
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
        asserted = {
            key: claim.get(key)
            for key in ("concept", "value", "unit", "fiscal_year", "fiscal_period", "end_date")
            if claim.get(key) not in (None, "")
        }
        if asserted and evidence_records and not missing:
            financial_records = [
                evidence_records[item]
                for item in evidence_ids
                if item in evidence_records
                and evidence_records[item].get("kind") == "financial_fact"
            ]
            if financial_records and not any(
                _claim_matches_financial_evidence(asserted, record)
                for record in financial_records
            ):
                issues.append(
                    "Claim period/value does not match its cited financial evidence"
                    if english
                    else "结论中的期间或数值与引用的财务证据不一致"
                )
    structured_output_valid = bool(output.get("structured_output_valid", True))
    if not structured_output_valid:
        issues.append(
            "Model output was not valid structured JSON"
            if english
            else "模型输出不是有效的结构化 JSON"
        )
    return {
        "structured_output_valid": structured_output_valid,
        "claim_count": len(claims or []),
        "verified_claim_count": verified_count,
        "unsupported_fact_count": unsupported_count,
        "issues": issues,
        "passed": structured_output_valid and not issues and unsupported_count == 0,
    }


def _claim_matches_financial_evidence(
    asserted: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    for key, expected in asserted.items():
        actual = evidence.get(key)
        if key == "value":
            try:
                left = float(expected)
                right = float(actual)
            except (TypeError, ValueError):
                return False
            tolerance = max(1e-6, abs(right) * 1e-9)
            if abs(left - right) > tolerance:
                return False
        elif str(expected).strip().casefold() != str(actual).strip().casefold():
            return False
    return True


_REQUIRED_SYNTHESIS_SECTIONS = frozenset(
    {
        "executive_summary",
        "business_model",
        "financial_quality",
        "balance_sheet",
        "competitive_position",
        "growth_opportunities",
        "counterarguments",
        "scenarios",
        "thesis",
        "invalidation_conditions",
        "leading_indicators",
        "unresolved_questions",
        "claims",
    }
)


def validate_research_synthesis(
    output: dict[str, Any],
    available_evidence: set[str],
    language: str = "zh-CN",
    evidence_records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    verification = verify_agent_output(
        output, available_evidence, language, evidence_records
    )
    english = normalize_language(language) == EN
    missing = sorted(
        key
        for key in _REQUIRED_SYNTHESIS_SECTIONS
        if key not in output or output.get(key) in (None, "", [])
    )
    if missing:
        prefix = "Missing required report sections: " if english else "缺少必要报告章节："
        verification["issues"].append(prefix + ", ".join(missing))
    if verification["claim_count"] == 0:
        verification["issues"].append(
            "The final report contains no verifiable claims"
            if english
            else "最终报告没有可验证的主要结论"
        )
    verification["passed"] = not verification["issues"] and verification["unsupported_fact_count"] == 0
    return verification


def _presentation_stage_value(value: Any, *, remove_claims: bool = True) -> Any:
    if isinstance(value, dict):
        return {
            key: _presentation_stage_value(item, remove_claims=remove_claims)
            for key, item in value.items()
            if not str(key).startswith("_")
            and key != "structured_output_valid"
            and (not remove_claims or key != "claims")
        }
    if isinstance(value, list):
        return [
            _presentation_stage_value(item, remove_claims=remove_claims)
            for item in value
        ]
    return value


def _collect_stage_claims(*values: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            claims = value.get("claims")
            if isinstance(claims, list):
                found.extend(item for item in claims if isinstance(item, dict))
            for key, item in value.items():
                if key != "claims" and not str(key).startswith("_"):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in found:
        text = str(
            claim.get("text")
            or claim.get("conclusion")
            or claim.get("argument")
            or ""
        ).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(_presentation_stage_value(claim, remove_claims=False))
    return unique


def _collect_strings(value: Any, *keys: str) -> list[str]:
    found: list[str] = []
    internal_id = re.compile(
        r"(?i)(?:fact|evidence|filing|artifact|run):[A-Za-z0-9_.:/-]+|"
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b"
    )

    def visit(item: Any) -> None:
        if isinstance(item, str):
            text = internal_id.sub("", item).strip()
            if text and text not in found:
                found.append(text)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for key in keys:
                if key in item:
                    visit(item[key])
            for child in item.values():
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(value)
    return found


def _synthesis_prior_artifacts(
    dossier: Any,
    growth: Any,
    skeptic: Any,
    forecast: Any,
) -> dict[str, Any]:
    """Keep synthesis input complete without repeating company metrics and evidence."""

    base_analyses = dossier.get("analyses", dossier) if isinstance(dossier, dict) else dossier
    return _presentation_stage_value(
        {
            "base_analyses": base_analyses,
            "growth_opportunities": growth,
            "counter_analysis": skeptic,
            "forecast": forecast,
        },
        remove_claims=False,
    )


def _response_diagnostics(payload: Any) -> dict[str, Any]:
    """Persist bounded provider diagnostics without response text or secrets."""

    if not isinstance(payload, dict):
        return {"finish_reason": None, "content_length": 0, "parse_error_class": "invalid_payload"}
    meta = payload.get("_response_meta")
    meta = meta if isinstance(meta, dict) else {}
    error = payload.get("_response_error")
    known_errors = {"empty_content", "invalid_json", "invalid_shape", "provider_error"}
    parse_error_class = error if isinstance(error, str) and error in known_errors else (
        "unknown_parse_error"
        if error or payload.get("structured_output_valid") is False
        else None
    )
    try:
        content_length = int(meta.get("content_length") or 0)
    except (TypeError, ValueError):
        content_length = 0
    return {
        "finish_reason": meta.get("finish_reason"),
        "content_length": content_length,
        "parse_error_class": parse_error_class,
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

    def _report_text(
        self, chinese: str, english: str, traditional: str | None = None
    ) -> str:
        if self.report_language == EN:
            return english
        if self.report_language == ZH_HANT:
            prefix = chinese[: len(chinese) - len(chinese.lstrip("# >-"))]
            return prefix + (
                traditional
                if traditional is not None
                else UI_HANT.get(chinese[len(prefix):], chinese[len(prefix):])
            )
        return chinese

    def _progress_text(self, chinese: str, **params: object) -> str:
        return translate(chinese, self.ui_language, **params)

    def _build_staged_fallback(
        self,
        stage_results: dict[str, dict[str, Any]],
        growth: dict[str, Any],
        skeptic: dict[str, Any],
        forecast: dict[str, Any],
        financial_metrics: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        business_raw = stage_results.get("business-analyst", {})
        financial_raw = stage_results.get("financial-analyst", {})
        accounting_raw = stage_results.get("accounting-risk-analyst", {})
        business = _presentation_stage_value(business_raw)
        claims = _collect_stage_claims(
            *stage_results.values(), growth, skeptic, forecast
        )
        possible_moats = (
            business.get("possible_moats", []) if isinstance(business, dict) else []
        )
        unknowns = _collect_strings(
            [business_raw, skeptic], "unknowns", "missing_evidence", "unresolved_questions"
        )
        invalidation = _collect_strings(
            skeptic, "unsupported_assumptions", "strongest_counterarguments"
        )
        leading_indicators = _collect_strings(growth, "leading_indicators")
        metric_rows = [
            metric
            for metric in (financial_metrics or [])
            if isinstance(metric, dict) and metric.get("year") is not None
        ]
        latest_metric = max(
            metric_rows,
            key=lambda metric: int(metric.get("year", 0)),
            default=None,
        )
        metric_values: dict[str, Any] = {}
        if latest_metric is not None:
            metric_values["year"] = latest_metric.get("year")
            for key in ("assets", "liabilities", "equity", "total_equity"):
                value = latest_metric.get(key)
                if value is not None:
                    metric_values[key] = value
        if len(metric_values) > 1:
            balance_sheet: Any = {
                "summary": self._report_text(
                    "以下资产负债表摘要仅使用通过校验的确定性财务数据。",
                    "This balance-sheet summary uses only deterministic financial data that passed validation.",
                    "以下資產負債表摘要僅使用通過驗證的確定性財務數據。",
                ),
                **metric_values,
            }
        else:
            balance_sheet = self._report_text(
                "缺少已验证的资产负债表数据，暂不能判断资产、负债与权益结构。",
                "Verified balance-sheet data is unavailable, so the asset, liability, and equity structure cannot be assessed.",
                "缺少已驗證的資產負債表數據，暫不能判斷資產、負債與權益結構。",
            )
        return {
            "executive_summary": self._report_text(
                "最终综合未完整生成。以下内容由已完成的研究阶段确定性整理。",
                "Final synthesis was incomplete. The content below deterministically preserves completed research stages.",
            ),
            "business_model": business,
            "financial_quality": {
                "financial_analysis": _presentation_stage_value(financial_raw),
                "accounting_risk": _presentation_stage_value(accounting_raw),
            },
            "balance_sheet": balance_sheet,
            "competitive_position": possible_moats
            or self._report_text(
                "竞争地位尚待最终综合；请结合商业模式与信息缺口复核。",
                "Competitive position awaits final synthesis; review the business model and information gaps.",
            ),
            "growth_opportunities": growth.get("opportunities", growth),
            "counterarguments": skeptic.get("strongest_counterarguments", skeptic),
            "scenarios": forecast.get("scenarios", forecast),
            "thesis": self._report_text(
                "尚未形成最终长期结论；可重试最终综合，阶段性结论已保留。",
                "No final long-term thesis was formed; retry synthesis while preserving stage conclusions.",
            ),
            "invalidation_conditions": invalidation
            or self._report_text("尚待最终综合。", "Awaiting final synthesis."),
            "leading_indicators": leading_indicators
            or self._report_text("尚待最终综合。", "Awaiting final synthesis."),
            "unresolved_questions": unknowns
            or self._report_text(
                "最终综合输出需要重新生成。",
                "The final synthesis output needs to be regenerated.",
            ),
            "claims": claims,
        }

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise ResearchCancelled()

    def _run_declarative_ot_workflow(
        self,
        run: ResearchRun,
        context: ResearchContext,
        evidence: list[dict[str, Any]],
        notify: ProgressCallback,
    ) -> ResearchRun:
        """Execute a non-official .ot dependency graph without hidden fixed agents."""
        raw_steps = self.pack.workflow.get("steps", [])
        if not isinstance(raw_steps, list) or not raw_steps:
            raise RuntimeError("OT workflow has no executable steps")
        steps: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                raise RuntimeError("OT workflow step is invalid")
            step_id = str(raw_step.get("id", "")).strip()
            prompt_path = str(raw_step.get("prompt", "")).strip()
            role = str(raw_step.get("role", "")).strip()
            dependencies = raw_step.get("depends_on", [])
            if (
                not step_id
                or step_id in steps
                or not prompt_path
                or not role
                or not isinstance(dependencies, list)
                or any(not isinstance(item, str) for item in dependencies)
            ):
                raise RuntimeError("OT workflow step contract is invalid")
            steps[step_id] = {
                **raw_step,
                "id": step_id,
                "prompt": prompt_path,
                "role": role,
                "depends_on": list(dependencies),
            }
            order.append(step_id)
        if len(steps) > 64:
            raise RuntimeError("OT workflow exceeds the executable step limit")

        run.workflow_id = f"ot:{self.pack.pack_id}"
        run.research_configuration["ot_workflow"] = {
            "schema": self.pack.workflow.get("schema", ""),
            "step_ids": order,
            "roles": [steps[step_id]["role"] for step_id in order],
            "settings": self.pack.workflow.get("settings", {}),
        }
        self.storage.save_run(run)

        available = {item["evidence_id"] for item in evidence}
        evidence_records = {item["evidence_id"]: item for item in evidence}
        remaining = set(order)
        results: dict[str, dict[str, Any]] = {}
        verifications: dict[str, dict[str, Any]] = {}
        total = len(order)
        completed = 0

        def execute_step(step_id: str) -> tuple[str, dict[str, Any]]:
            step = steps[step_id]
            self._check_cancelled()
            self._set_agent_state(step_id, "running")
            prior = {
                dependency: results[dependency]
                for dependency in step["depends_on"]
            }
            output = self._run_agent(
                step_id,
                step["prompt"],
                context.compact_json(),
                {
                    "workflow_role": step["role"],
                    "workflow_settings": self.pack.workflow.get("settings", {}),
                    "dependency_outputs": prior,
                },
            )
            return step_id, output

        while remaining:
            ready = [
                step_id
                for step_id in order
                if step_id in remaining
                and all(dependency in results for dependency in steps[step_id]["depends_on"])
            ]
            if not ready:
                raise RuntimeError("OT workflow dependencies cannot be resolved")
            batch = ready[:2] if self.parallel_agents else ready[:1]
            notify(
                self._progress_text(
                    "正在执行 .ot 工作流：{completed}/{total}",
                    completed=completed,
                    total=total,
                ),
                20 + round(completed * 68 / total),
            )
            for step_id in batch:
                self._set_agent_state(step_id, "queued")
            if len(batch) > 1:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {executor.submit(execute_step, step_id): step_id for step_id in batch}
                    completed_batch = []
                    for future in futures:
                        try:
                            completed_batch.append(future.result())
                        except ResearchCancelled:
                            self._set_agent_state(futures[future], "cancelled")
                            raise
                        except Exception:
                            self._set_agent_state(futures[future], "failed")
                            raise
            else:
                completed_batch = [execute_step(batch[0])]

            for step_id, output in completed_batch:
                verification = verify_agent_output(
                    output, available, self.report_language, evidence_records
                )
                results[step_id] = output
                verifications[step_id] = verification
                remaining.remove(step_id)
                completed += 1
                self._save(
                    run,
                    "ot-agent-analysis",
                    str(steps[step_id]["role"]),
                    {
                        "step_id": step_id,
                        "role": steps[step_id]["role"],
                        "depends_on": steps[step_id]["depends_on"],
                        "output_schema": steps[step_id].get("output_schema", ""),
                        "result": output,
                        "verification": verification,
                    },
                    agent_id=step_id,
                )
                self._set_agent_state(step_id, "completed")

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
        claims = _collect_stage_claims(*results.values())
        aggregate = verify_agent_output(
            {"claims": claims}, available, self.report_language, evidence_records
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

        if _REQUIRED_SYNTHESIS_SECTIONS.issubset(final_output):
            report_payload = final_output
            synthesis_verification = validate_research_synthesis(
                final_output, available, self.report_language, evidence_records
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
                    "output": _presentation_stage_value(results[step_id], remove_claims=False),
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

        self._save(
            run,
            "research-report",
            self._report_text(".ot 工作流研究报告", ".ot Workflow Research Report"),
            {
                "mode": "ot-workflow",
                "report": report_payload,
                "verification": verification,
                "retryable": False,
                "workflow": {
                    "pack_id": self.pack.pack_id,
                    "pack_version": self.pack.version,
                    "content_identity": self.pack.content_hash,
                    "selected_output_step": preferred,
                    "steps": order,
                },
            },
            agent_id=preferred,
        )
        if report_payload.get("thesis"):
            thesis_version = self.storage.save_thesis_version(
                run.company.cik,
                {
                    "thesis": report_payload.get("thesis"),
                    "claims": report_payload.get("claims", claims),
                    "invalidation_conditions": report_payload.get("invalidation_conditions", []),
                    "leading_indicators": report_payload.get("leading_indicators", []),
                    "unresolved_questions": report_payload.get("unresolved_questions", []),
                },
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
        notify(self._progress_text(".ot 工作流研究完成"), 100)
        return run
    def run(
        self,
        company: Company,
        facts: list[dict[str, Any]] | FinancialProfile,
        filing_evidence: list[dict[str, Any]] | None = None,
        valuation_inputs: dict[str, float] | None = None,
        market_snapshot: dict[str, Any] | None = None,
        progress: ProgressCallback | None = None,
        reproducibility: dict[str, Any] | None = None,
    ) -> ResearchRun:
        notify = progress or (lambda _message, _percent: None)
        reproducibility = reproducibility or {}
        profile = facts if isinstance(facts, FinancialProfile) else None
        if profile is not None:
            facts = list(profile.fact_dicts)
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
            market_snapshot=market_snapshot,
            model_configuration={
                "configured_model_id": self.model_config.configured_model_id,
                "configuration_version": self.model_config.configuration_version,
                "role": self.model_config.role,
            },
            research_configuration={
                **dict(reproducibility.get("research_configuration", {})),
                "report_language": self.report_language,
                "parallel_agents": self.parallel_agents,
                "research_pack_id": self.pack.pack_id,
                "research_pack_version": self.pack.version,
                "research_pack_content_identity": self.pack.content_hash,
            },
            data_snapshot=dict(reproducibility.get("data_snapshot", {})),
        )
        self.storage.save_run(run)
        try:
            self._check_cancelled()
            metrics = calculate_metrics(facts)
            interim_metrics = calculate_interim_metrics(facts)
            evidence = build_fact_evidence(facts)
            evidence.extend(filing_evidence or [])
            valuation = None
            snapshot_currency = str((market_snapshot or {}).get("currency", ""))
            if company.industry_support == "financial_beta" and valuation_inputs:
                valuation = {
                    "status": "not_applicable",
                    "reason": self._report_text(
                        "金融机构 Beta 暂不使用标准自由现金流反向 DCF。",
                        "Financials Beta does not apply the standard free-cash-flow reverse DCF.",
                    ),
                    "currency": company.reporting_currency,
                }
            elif (
                valuation_inputs
                and snapshot_currency
                and snapshot_currency != company.reporting_currency
            ):
                valuation = {
                    "status": "currency_mismatch",
                    "reason": self._report_text(
                        "手动市值币种与财报币种不同；未提供汇率，因此不执行反向 DCF。",
                        "The manual market-cap currency differs from the reporting currency; reverse DCF was skipped because no FX rate was supplied.",
                    ),
                    "currency": snapshot_currency,
                    "reporting_currency": company.reporting_currency,
                }
            elif valuation_inputs and valuation_inputs.get("market_cap", 0) > 0:
                valuation = reverse_dcf_analysis(
                    metrics,
                    valuation_inputs["market_cap"],
                    valuation_inputs.get("discount_rate", 0.10),
                    valuation_inputs.get("terminal_growth", 0.03),
                )
                valuation["currency"] = company.reporting_currency
            context = ResearchContext(
                company,
                facts,
                metrics,
                interim_metrics,
                evidence,
                valuation,
                market_snapshot,
            )
            self._check_cancelled()
            notify(self._progress_text("已完成确定性财务计算"), 15)
            summary = deterministic_summary(
                company.name,
                metrics,
                self.report_language,
                company.reporting_currency,
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
                    "interim_metrics": interim_metrics,
                    "evidence": evidence,
                    "currency": company.reporting_currency,
                    "accounting_standard": company.accounting_standard,
                    "industry_support": company.industry_support,
                    "market_snapshot": market_snapshot,
                    "financial_quality": {
                        "status": profile.status.value,
                        "rejected_periods": list(profile.rejected_periods),
                        "period_continuity": list(profile.period_continuity),
                    } if profile is not None else None,
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

            if self.pack.pack_id != "official.long-term-fundamentals":
                return self._run_declarative_ot_workflow(run, context, evidence, notify)

            available = {item["evidence_id"] for item in evidence}
            evidence_records = {item["evidence_id"]: item for item in evidence}
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
                    else "正在並行執行基礎 Agent（0/3）"
                    if self.ui_language == ZH_HANT and self.parallel_agents
                    else "正在依序執行基礎 Agent（0/3）"
                    if self.ui_language == ZH_HANT
                    else "正在并行运行基础 Agent（0/3）"
                    if self.parallel_agents
                    else "正在按顺序运行基础 Agent（0/3）"
                ),
                25,
            )
            for agent_id in stage_one:
                self._set_agent_state(agent_id, "queued")

            completed_agents = 0

            def record_stage_result(agent_id: str, result: dict[str, Any]) -> None:
                nonlocal completed_agents
                self._check_cancelled()
                stage_results[agent_id] = result
                verification = verify_agent_output(
                    result, available, self.report_language, evidence_records
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

            if self.parallel_agents:
                executor = ThreadPoolExecutor(max_workers=2)
                futures: dict[Future[dict[str, Any]], str] = {}
                failures: dict[str, BaseException] = {}
                try:
                    queued = iter(stage_one.items())

                    def submit_next() -> Future[dict[str, Any]] | None:
                        try:
                            agent_id, prompt_path = next(queued)
                        except StopIteration:
                            return None
                        self._set_agent_state(agent_id, "running")
                        future = executor.submit(
                            self._run_agent,
                            agent_id,
                            prompt_path,
                            context.compact_json(),
                            {},
                        )
                        futures[future] = agent_id
                        return future

                    pending = {
                        future
                        for future in (submit_next(), submit_next())
                        if future is not None
                    }
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
                            except Exception as exc:
                                self._set_agent_state(agent_id, "failed")
                                failures[agent_id] = exc
                            else:
                                record_stage_result(agent_id, result)
                            next_future = submit_next()
                            if next_future is not None:
                                pending.add(next_future)
                finally:
                    executor.shutdown(
                        wait=not self.cancel_check(),
                        cancel_futures=True,
                    )

                for agent_id, error in failures.items():
                    if not (
                        isinstance(error, ProviderError) and error.retryable
                    ):
                        raise error
                    self._check_cancelled()
                    self._set_agent_state(agent_id, "retrying")
                    notify(
                        self._progress_text(
                            "{agent_id} 暂时失败，正在单独重试",
                            agent_id=agent_id,
                        ),
                        25 + completed_agents * 7,
                    )
                    try:
                        result = self._run_agent(
                            agent_id,
                            stage_one[agent_id],
                            context.compact_json(),
                            {},
                        )
                    except ResearchCancelled:
                        self._set_agent_state(agent_id, "cancelled")
                        raise
                    except Exception:
                        self._set_agent_state(agent_id, "failed")
                        raise
                    record_stage_result(agent_id, result)
            else:
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
                    record_stage_result(agent_id, result)

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
            if not growth_validation.passed or not growth_validation.output.get("opportunities"):
                notify(self._progress_text("增长机会输出不完整，正在进行一次修复"), 58)
                growth_raw = self._run_agent(
                    "growth-opportunity-analyst",
                    "prompts/growth-opportunity-analyst.md",
                    context.compact_json(),
                    {
                        "research_dossier": dossier,
                        "repair_instruction": (
                            "Return one complete growth-opportunity JSON object with a non-empty "
                            "opportunities array. Repair only this stage and do not invent evidence."
                        ),
                    },
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
                _synthesis_prior_artifacts(dossier, growth, skeptic, forecast),
            )
            verification = validate_research_synthesis(
                synthesis, available, self.report_language, evidence_records
            )
            report_payload: dict[str, Any] = synthesis
            report_mode = "synthesized"
            repair_diagnostics: dict[str, Any] | None = None
            if not verification["passed"]:
                # A malformed final payload gets exactly one bounded repair
                # call.  The prior agents are already persisted and are never
                # rerun; authentication, rate-limit and provider exceptions
                # remain terminal rather than becoming hidden retries.
                repair_input = _synthesis_prior_artifacts(dossier, growth, skeptic, forecast)
                repair_input["invalid_synthesis"] = _presentation_stage_value(synthesis, remove_claims=False)
                repair_input["repair_instruction"] = (
                    "Return one complete JSON object matching the required report schema. "
                    "Repair only the final synthesis; do not invent evidence."
                )
                try:
                    repaired = self._run_agent(
                        "research-synthesizer-repair",
                        "prompts/research-synthesizer.md",
                        context.compact_json(),
                        repair_input,
                    )
                except ProviderError:
                    # Authentication, rate-limit, and quota failures are not
                    # retried. Preserve completed stages as a partial report
                    # instead of turning a final-only failure into FAILED.
                    repair_diagnostics = _response_diagnostics(
                        {"_response_error": "provider_error"}
                    )
                    verification["issues"].append(
                        "Final synthesis repair was unavailable; completed stages were preserved."
                    )
                else:
                    repaired_verification = validate_research_synthesis(
                        repaired, available, self.report_language, evidence_records
                    )
                    repair_diagnostics = _response_diagnostics(repaired)
                    if not repaired_verification["passed"] and not repair_diagnostics.get("parse_error_class"):
                        repair_diagnostics["parse_error_class"] = "invalid_schema"
                    if repaired_verification["passed"]:
                        synthesis = repaired
                        verification = repaired_verification
                        report_payload = repaired
                    else:
                        verification["issues"].extend(
                            issue for issue in repaired_verification["issues"]
                            if issue not in verification["issues"]
                        )
                report_mode = "staged-fallback"
                if not verification["passed"]:
                    report_payload = self._build_staged_fallback(
                        stage_results, growth, skeptic, forecast, context.metrics
                    )
                else:
                    report_mode = "synthesized"
            diagnostics = _response_diagnostics(synthesis)
            if repair_diagnostics is not None:
                diagnostics = {
                    **diagnostics,
                    "initial": diagnostics,
                    "repair": repair_diagnostics,
                    **repair_diagnostics,
                }
            self._save(
                run,
                "research-report",
                self._report_text(
                    "完整长期研究报告", "Complete Long-term Research Report"
                ),
                {
                    "mode": report_mode,
                    "report": report_payload,
                    "verification": verification,
                    "retryable": not verification["passed"],
                    "diagnostics": diagnostics,
                },
                agent_id="research-synthesizer",
            )
            if not verification["passed"]:
                run.status = RunStatus.PARTIAL
                run.completed_at = utc_now_iso()
                self.storage.save_run(run)
                notify(
                    self._progress_text(
                        "综合报告生成不完整；已保留阶段研究结果，可稍后重试综合"
                    ),
                    100,
                )
                return run
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

    def retry_synthesis(
        self,
        run: ResearchRun,
        artifacts: list[dict[str, Any]],
        facts: list[dict[str, Any]],
    ) -> ResearchRun:
        """Regenerate only the final synthesis from persisted stage artifacts."""

        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        deterministic = _latest_artifact(artifacts, "deterministic-financial-summary")
        dossier_artifact = _latest_artifact(artifacts, "verified-research-dossier")
        growth_artifact = _latest_artifact(artifacts, "growth-opportunities")
        skeptic_artifact = _latest_artifact(artifacts, "counter-analysis")
        forecast_artifact = _latest_artifact(artifacts, "forecast-scenarios")
        required = (deterministic, dossier_artifact, growth_artifact, skeptic_artifact, forecast_artifact)
        if any(item is None for item in required):
            raise RuntimeError("saved research stages are incomplete")

        deterministic_content = deterministic["content"]
        evidence = deterministic_content.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        context = ResearchContext(
            run.company,
            facts,
            deterministic_content.get("metrics", []),
            deterministic_content.get("interim_metrics", []),
            evidence,
            (_latest_artifact(artifacts, "deterministic-valuation") or {}).get("content"),
            deterministic_content.get("market_snapshot"),
        )
        dossier = dossier_artifact["content"]
        growth = growth_artifact["content"]
        skeptic = skeptic_artifact["content"]
        forecast = forecast_artifact["content"]
        synthesis = self._run_agent(
            "research-synthesizer",
            "prompts/research-synthesizer.md",
            context.compact_json(),
            _synthesis_prior_artifacts(dossier, growth, skeptic, forecast),
        )
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
        verification = validate_research_synthesis(
            synthesis, available, self.report_language, evidence_records
        )
        if verification["passed"]:
            report_payload = synthesis
            mode = "synthesized"
        else:
            previous_report = _latest_artifact(artifacts, "research-report")
            previous_content = previous_report.get("content", {}) if previous_report else {}
            report_payload = previous_content.get("report", {})
            mode = "staged-fallback"
        self._save(
            run,
            "research-report",
            self._report_text("完整长期研究报告", "Complete Long-term Research Report"),
            {
                "mode": mode,
                "report": report_payload,
                "verification": verification,
                "retryable": not verification["passed"],
                "diagnostics": _response_diagnostics(synthesis),
            },
            agent_id="research-synthesizer",
        )
        if verification["passed"]:
            thesis_content = {
                "thesis": synthesis.get("thesis"),
                "claims": synthesis.get("claims", []),
                "invalidation_conditions": synthesis.get("invalidation_conditions", []),
                "leading_indicators": synthesis.get("leading_indicators", []),
                "unresolved_questions": synthesis.get("unresolved_questions", []),
                "growth_opportunities": growth.get("opportunities", growth),
                "scenarios": forecast.get("scenarios", forecast),
            }
            thesis_version = self.storage.save_thesis_version(
                run.company.cik,
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
        return run

    def retry_growth(
        self,
        run: ResearchRun,
        artifacts: list[dict[str, Any]],
        facts: list[dict[str, Any]],
    ) -> ResearchRun:
        """Regenerate only growth opportunities, then refresh final synthesis."""

        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        deterministic = _latest_artifact(artifacts, "deterministic-financial-summary")
        dossier_artifact = _latest_artifact(artifacts, "verified-research-dossier")
        if deterministic is None or dossier_artifact is None:
            raise RuntimeError("saved research stages are incomplete")

        deterministic_content = deterministic["content"]
        evidence = deterministic_content.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        available = {
            str(item.get("evidence_id"))
            for item in evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
        context = ResearchContext(
            run.company,
            facts,
            deterministic_content.get("metrics", []),
            deterministic_content.get("interim_metrics", []),
            evidence,
            (_latest_artifact(artifacts, "deterministic-valuation") or {}).get("content"),
            deterministic_content.get("market_snapshot"),
        )
        growth_raw = self._run_agent(
            "growth-opportunity-analyst",
            "prompts/growth-opportunity-analyst.md",
            context.compact_json(),
            {
                "research_dossier": dossier_artifact["content"],
                "retry_instruction": (
                    "The previous growth stage returned no usable opportunities. Regenerate only "
                    "this stage using the persisted evidence; do not rerun or assume other stages."
                ),
            },
        )
        validation = normalize_growth_output(
            growth_raw,
            available,
            self.report_language,
        )
        self._save(
            run,
            "growth-opportunities",
            self._report_text("增长机会", "Growth Opportunities"),
            validation.output,
            agent_id="growth-opportunity-analyst",
        )
        if not validation.passed or not validation.output.get("opportunities"):
            raise RuntimeError("growth-opportunity model returned no usable content")
        return self.retry_synthesis(
            run,
            self.storage.get_artifacts(run.run_id),
            facts,
        )

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


def _latest_artifact(
    artifacts: list[dict[str, Any]], artifact_type: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in reversed(artifacts)
            if item.get("artifact_type") == artifact_type
        ),
        None,
    )
