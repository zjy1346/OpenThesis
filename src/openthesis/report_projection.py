from __future__ import annotations

from typing import Any

from .i18n import EN, normalize_language


_INTERNAL_FIELDS = frozenset(
    {
        "agent",
        "evidence_ids",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "unknown_evidence_ids",
        "target_opportunity_ids",
        "opportunity_id",
        "structured_output_valid",
        "_response_meta",
    }
)

_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "summary": ("摘要", "Summary"),
    "analysis": ("分析", "Analysis"),
    "claims": ("主要结论", "Key Conclusions"),
    "unknowns": ("信息缺口", "Information Gaps"),
    "possible_moats": ("潜在护城河", "Potential Moats"),
    "risks": ("主要风险", "Key Risks"),
    "conclusion": ("结论", "Conclusion"),
    "strengths": ("优势", "Strengths"),
    "concerns": ("关注事项", "Concerns"),
    "risk_flags": ("风险信号", "Risk Flags"),
    "benign_explanations": ("可能的合理解释", "Possible Benign Explanations"),
    "follow_up_questions": ("后续核查问题", "Follow-up Questions"),
    "strongest_counterarguments": ("核心反方观点", "Strongest Counterarguments"),
    "unsupported_assumptions": ("证据不足的假设", "Unsupported Assumptions"),
    "missing_evidence": ("缺失证据", "Missing Evidence"),
    "opportunities": ("增长机会", "Growth Opportunities"),
    "scenarios": ("情景", "Scenarios"),
    "capital_requirements": ("资本需求", "Capital Requirements"),
    "maturity_stage": ("成熟阶段", "Maturity Stage"),
    "category": ("类别", "Category"),
    "mechanism": ("增长机制", "Growth Mechanism"),
    "time_horizon_years": ("时间跨度（年）", "Time Horizon (Years)"),
    "leading_indicators": ("领先指标", "Leading Indicators"),
    "invalidation_conditions": ("失效条件", "Invalidation Conditions"),
    "argument": ("反方论点", "Argument"),
    "assumption": ("假设", "Assumption"),
    "assumptions": ("关键假设", "Key Assumptions"),
    "challenge": ("质疑", "Challenge"),
    "confidence": ("置信度", "Confidence"),
    "text": ("结论", "Conclusion"),
    "kind": ("类型", "Type"),
    "base": ("基准情景", "Base Case"),
    "bear": ("悲观情景", "Bear Case"),
    "bull": ("乐观情景", "Bull Case"),
    "probability": ("可能性", "Probability"),
    "probability_range": ("可能性区间", "Probability Range"),
    "revenue_cagr_range": ("营收复合增速区间", "Revenue CAGR Range"),
    "operating_margin_range": ("营业利润率区间", "Operating Margin Range"),
}

_DISPLAY_VALUES_ZH = {
    "calculation": "计算",
    "assumption": "假设",
    "forecast": "预测",
    "risk": "风险",
    "inference": "推论",
    "fact": "事实",
    "opinion": "观点",
    "unknown": "未知",
    "base": "基准",
    "bear": "悲观",
    "bull": "乐观",
}


def project_report_value(value: Any, *, include_technical: bool) -> Any:
    """Return a presentation-safe view without mutating stored research artifacts."""

    if include_technical:
        return value
    if isinstance(value, dict):
        return {
            key: project_report_value(item, include_technical=False)
            for key, item in value.items()
            if str(key) not in _INTERNAL_FIELDS and not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [project_report_value(item, include_technical=False) for item in value]
    if isinstance(value, tuple):
        return tuple(project_report_value(item, include_technical=False) for item in value)
    return value


def report_field_label(key: object, language: str) -> str:
    normalized = str(key)
    label = _FIELD_LABELS.get(normalized)
    if label:
        return label[1] if normalize_language(language) == EN else label[0]
    return (
        normalized.replace("_", " ").title()
        if normalize_language(language) == EN
        else normalized.replace("_", " ")
    )


def report_display_value(value: str, language: str) -> str:
    if normalize_language(language) == EN:
        return value
    return _DISPLAY_VALUES_ZH.get(value.casefold().strip(), value)
