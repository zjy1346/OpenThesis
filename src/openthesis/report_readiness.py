"""Shared, evidence-aware readiness contract for generated research reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Iterable

from .i18n import EN, ZH_HANT, normalize_language


REQUIRED_REPORT_SECTIONS = (
    "executive_summary",
    "claims",
    "business_model",
    "financial_quality",
    "balance_sheet",
    "competitive_position",
    "growth_opportunities",
    "counterarguments",
    "scenarios",
    # This section must contain either a deterministic reverse-DCF result or
    # an explicit statement that the data/method is not applicable.  Omitting
    # valuation silently would violate the report's minimum investment logic.
    "implied_expectations",
    "thesis",
    "invalidation_conditions",
    "leading_indicators",
    "unresolved_questions",
)
REQUIRED_RESEARCH_ARTIFACTS = (
    "deterministic-financial-summary",
    "verified-research-dossier",
    "growth-opportunities",
    "counter-analysis",
    "forecast-scenarios",
)

_NON_CONTENT_KEYS = frozenset({
    "evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids",
    "unknown_evidence_ids", "fact_id", "claim_id", "source_id", "accession",
    "confidence", "probability", "evidence_count", "supporting_evidence_count",
    "contradicting_evidence_count", "verification", "verified", "kind",
    "status", "mode", "unit", "currency", "fiscal_year", "fiscal_period",
    "end_date", "period_end", "page", "pages", "url", "source_url",
})

_PLACEHOLDER_TEXT = frozenset({
    "",
    "暂无",
    "暂无。",
    "暂无可显示内容。",
    "证据不足或尚未提供。",
    "证據不足或尚未提供。",
    "当前研究阶段未返回可验证的此章节内容。",
    "本研究階段未返回可驗證的此章節內容。",
    "this section was not returned with verifiable content in the current research stage.",
    "insufficient evidence or not provided.",
    "no displayable content.",
    "none.",
    "not available.",
})


def _normalized_text(value: str) -> str:
    text = re.sub(r"[`*_>#\[\](){}]", "", value)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def _is_content_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = _normalized_text(value)
    if normalized in _PLACEHOLDER_TEXT:
        return False
    if normalized.startswith((
        "insufficient evidence or not provided",
        "this section was not returned with verifiable content",
        "当前研究阶段未返回可验证",
        "本研究階段未返回可驗證",
    )):
        return False
    return bool(normalized)


def _has_semantic_content(value: Any, *, parent_key: str = "") -> bool:
    if isinstance(value, str):
        return _is_content_text(value)
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key.startswith("_") or normalized_key in _NON_CONTENT_KEYS:
                continue
            if _has_semantic_content(child, parent_key=normalized_key):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_has_semantic_content(item, parent_key=parent_key) for item in value)
    # Raw numbers, booleans, and identifiers are not by themselves a research
    # explanation.  They need a semantic field with human-readable meaning.
    return False


def section_has_substance(section: str, value: Any) -> bool:
    """Whether a section carries user-readable meaning instead of a shell.

    Claims have a stricter contract: metadata, numeric values, and IDs cannot
    make the section substantive without at least one actual string body.
    """

    if section == "claims":
        if not isinstance(value, (list, tuple)):
            return _is_content_text(value)
        return any(
            isinstance(item, dict)
            and any(
                _is_content_text(item.get(field))
                for field in ("text", "conclusion", "argument")
            )
            for item in value
        )
    return _has_semantic_content(value, parent_key=section)


@dataclass(frozen=True, slots=True)
class ReportReadiness:
    state: str
    complete: bool
    substantive_sections: tuple[str, ...]
    missing_sections: tuple[str, ...]
    missing_stages: tuple[str, ...]
    issues: tuple[str, ...]
    recovery_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_report_readiness(
    report: Any,
    verification: Any,
    *,
    missing_stages: Iterable[str] = (),
    financial_evidence_count: int | None = None,
    allow_complete: bool = True,
    failed: bool = False,
) -> ReportReadiness:
    """Assess report substance, verification, and prerequisite-stage coverage.

    This is deliberately a semantic completeness check, not a count threshold:
    each named required section must contain human-readable analysis, and the
    report must have at least one verifiable claim and no unresolved fact error.
    """

    value = report if isinstance(report, dict) else {}
    verification_value = verification if isinstance(verification, dict) else {}
    substantive = tuple(
        key for key in REQUIRED_REPORT_SECTIONS
        if section_has_substance(key, value.get(key))
    )
    missing_sections = tuple(
        key for key in REQUIRED_REPORT_SECTIONS if key not in substantive
    )
    missing_stage_values = tuple(sorted({str(item) for item in missing_stages if str(item)}))
    issues: list[str] = []
    if missing_sections:
        issues.append("missing_substantive_sections:" + ",".join(missing_sections))
    if missing_stage_values:
        issues.append("missing_research_stages:" + ",".join(missing_stage_values))
    if int(verification_value.get("verified_claim_count", 0) or 0) <= 0:
        issues.append("no_verifiable_claims")
    if int(verification_value.get("unsupported_fact_count", 0) or 0) > 0:
        issues.append("unsupported_quantitative_facts")
    if not bool(verification_value.get("passed")):
        issues.append("verification_not_passed")
    if financial_evidence_count is not None and financial_evidence_count <= 0:
        issues.append("no_verified_financial_source_evidence")

    if failed:
        state = "failed"
        recovery = "review_run_diagnostics"
    elif not substantive:
        state = "action_required"
        recovery = "resume_or_repair_missing_research_stages"
    elif not issues and allow_complete:
        state = "complete"
        recovery = ""
    else:
        state = "substantive_partial"
        recovery = "complete_missing_or_unverified_sections"
    return ReportReadiness(
        state=state,
        complete=state == "complete",
        substantive_sections=substantive,
        missing_sections=missing_sections,
        missing_stages=missing_stage_values,
        issues=tuple(issues),
        recovery_action=recovery,
    )


def missing_research_stages(artifacts: Iterable[Any]) -> tuple[str, ...]:
    """Return prerequisite stage artifacts that are absent or empty."""

    available: set[str] = set()
    for artifact in artifacts:
        if isinstance(artifact, dict):
            artifact_type = str(artifact.get("artifact_type", ""))
            content = artifact.get("content")
        else:
            artifact_type = str(getattr(artifact, "artifact_type", ""))
            content = getattr(artifact, "content", None)
        if artifact_type in REQUIRED_RESEARCH_ARTIFACTS and isinstance(content, dict) and content:
            available.add(artifact_type)
    return tuple(item for item in REQUIRED_RESEARCH_ARTIFACTS if item not in available)


def financial_source_evidence_count(artifacts: Iterable[Any]) -> int:
    """Count distinct accepted-fact evidence entries in the financial artifact."""

    for artifact in artifacts:
        if isinstance(artifact, dict):
            artifact_type = str(artifact.get("artifact_type", ""))
            content = artifact.get("content")
        else:
            artifact_type = str(getattr(artifact, "artifact_type", ""))
            content = getattr(artifact, "content", None)
        if artifact_type != "deterministic-financial-summary" or not isinstance(content, dict):
            continue
        evidence = content.get("evidence")
        if not isinstance(evidence, list):
            return 0
        return len({
            str(item.get("evidence_id") or item.get("fact_id"))
            for item in evidence
            if isinstance(item, dict)
            and item.get("kind") == "financial_fact"
            and (item.get("evidence_id") or item.get("fact_id"))
        })
    return 0


def report_readiness_notice(readiness: Any, language: str) -> tuple[str, str] | None:
    """Localized, non-technical status copy shared by report renderers."""

    if not isinstance(readiness, dict):
        return None
    state = str(readiness.get("state", ""))
    if state == "complete":
        return None
    locale = normalize_language(language)
    if state == "failed":
        simplified = ("研究未能完成", "本次研究遇到无法自动恢复的问题。已保留可用阶段结果，请根据研究状态和恢复提示处理。")
        traditional = ("研究未能完成", "本次研究遇到無法自動恢復的問題。已保留可用階段結果，請依據研究狀態和恢復提示處理。")
        english = ("Research could not be completed", "This run encountered an issue that could not be recovered automatically. Available stage results were preserved; review the recovery guidance before continuing.")
    elif state == "action_required":
        simplified = ("研究尚未形成可用报告", "目前没有足够的实质研究内容可作为报告。请继续研究或根据恢复提示修复未完成阶段。")
        traditional = ("研究尚未形成可用報告", "目前沒有足夠的實質研究內容可作為報告。請繼續研究或依據恢復提示修復未完成階段。")
        english = ("No usable report is available yet", "There is not enough substantive research to present a report. Continue the research or repair the incomplete stages using the recovery guidance.")
    else:
        simplified = ("阶段性研究，尚未达到完整报告条件", "已保留有实质内容的研究结果；部分必要章节、来源或验证仍未完成。请查看章节提示并继续补充或验证。")
        traditional = ("階段性研究，尚未達到完整報告條件", "已保留具實質內容的研究結果；部分必要章節、來源或驗證仍未完成。請查看章節提示並繼續補充或驗證。")
        english = ("Substantive partial research", "Substantive results have been preserved, but some required sections, sources, or verification remain incomplete. Review the section notices and continue the missing work.")
    return english if locale == EN else traditional if locale == ZH_HANT else simplified


def no_final_report_notice(
    language: str, *, has_stage_materials: bool = True
) -> tuple[str, str]:
    """Explain that saved stage outputs are not yet a synthesized report."""
    locale = normalize_language(language)
    if has_stage_materials:
        simplified = (
            "尚未生成最终综合报告",
            "本次运行尚未生成最终综合报告。已保存的财务或研究阶段材料仍可查看；请完成或继续未完成阶段后再导出完整报告。",
        )
        traditional = (
            "尚未產生最終綜合報告",
            "本次執行尚未產生最終綜合報告。已儲存的財務或研究階段材料仍可查看；請完成或繼續未完成階段後再匯出完整報告。",
        )
        english = (
            "Final synthesis has not been generated",
            "This run has no final synthesized report yet. Saved financial or research-stage material remains available; finish or resume the incomplete stages before exporting a complete report.",
        )
    else:
        simplified = (
            "研究尚未形成可用报告",
            "本次运行没有保存可用的研究材料，因此不能导出完整报告。请根据运行诊断修复失败环节后重新继续研究。",
        )
        traditional = (
            "研究尚未形成可用報告",
            "本次執行沒有儲存可用的研究材料，因此無法匯出完整報告。請依據執行診斷修復失敗環節後重新繼續研究。",
        )
        english = (
            "No usable research report is available",
            "This run preserved no usable research material, so a complete report cannot be exported. Review the run diagnostics, recover the failed stage, and then continue the research.",
        )
    return english if locale == EN else traditional if locale == ZH_HANT else simplified


def readiness_for_report_artifact(
    artifact: Any,
    artifacts: Iterable[Any],
    *,
    failed: bool = False,
    run_status: str = "",
) -> dict[str, Any] | None:
    """Use the persisted shared result, or conservatively assess legacy data."""
    if not isinstance(artifact, dict):
        return None
    content = artifact.get("content")
    if not isinstance(content, dict):
        content = {}
    readiness = content.get("readiness")
    if isinstance(readiness, dict):
        readiness = dict(readiness)
    else:
        verification = content.get("verification")
        if not isinstance(verification, dict):
            verification = {}
        readiness = verification.get("report_readiness")
    if isinstance(readiness, dict):
        readiness = dict(readiness)
        status = str(run_status).casefold()
        if failed or status == "failed":
            readiness["state"] = "failed"
            readiness["complete"] = False
            readiness["recovery_action"] = "review_run_diagnostics"
            issues = readiness.get("issues", ())
            issues = list(issues) if isinstance(issues, (list, tuple)) else []
            if "research_run_failed" not in issues:
                issues.append("research_run_failed")
            readiness["issues"] = issues
        elif status and status != "completed" and (
            readiness.get("complete") is True or readiness.get("state") == "complete"
        ):
            readiness["state"] = "substantive_partial"
            readiness["complete"] = False
            readiness["recovery_action"] = "resume_or_repair_missing_research_stages"
            issues = readiness.get("issues", ())
            issues = list(issues) if isinstance(issues, (list, tuple)) else []
            if "research_run_not_completed" not in issues:
                issues.append("research_run_not_completed")
            readiness["issues"] = issues
        return readiness
    mode = str(content.get("mode", ""))
    report = content.get("report", content)
    return assess_report_readiness(
        report,
        verification,
        missing_stages=(
            () if mode == "ot-workflow" else missing_research_stages(artifacts)
        ),
        financial_evidence_count=financial_source_evidence_count(artifacts),
        allow_complete=(mode in {"synthesized", "ot-workflow"})
        and (not run_status or str(run_status).casefold() == "completed"),
        failed=failed or str(run_status).casefold() == "failed",
    ).to_dict()
