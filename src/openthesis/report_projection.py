from __future__ import annotations

import re
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

# Stable report protocol keys.  Unknown keys are retained only in technical
# mode; non-technical projections must not turn an accidental JSON field into
# a visible English implementation detail.
_REPORT_FIELDS = frozenset(
    {
        "executive_summary", "business_model", "financial_quality", "balance_sheet",
        "competitive_position", "growth_opportunities", "counterarguments",
        "scenarios", "implied_expectations", "thesis", "invalidation_conditions",
        "leading_indicators", "unresolved_questions", "report", "verification",
        "narrative", "mode", "metrics", "currency", "interim_metrics",
        "market_snapshot", "industry_support", "claims", "text", "kind",
        "confidence", "title", "argument", "counterargument", "severity",
        "summary", "analysis", "conclusion", "strengths", "concerns",
        "risks", "unknowns", "possible_moats", "assumptions", "assumption",
        "mechanism", "category", "maturity_stage", "time_horizon_years",
        "condition", "indicator", "metric", "trigger", "question",
        "probability", "probability_range", "scenario_eligibility",
        "capital_requirements", "leading_indicators", "invalidation_conditions",
        "evidence_grade",
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
    "counterargument": ("反方观点", "Counterargument"),
    "severity": ("严重程度", "Severity"),
    "title": ("标题", "Title"),
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


_DISPLAY_VALUES_ZH.update({"high": "高", "medium": "中", "low": "低"})


_INTERNAL_ID_RE = re.compile(
    r"(?i)(?:fact|evidence|filing|artifact|run):[A-Za-z0-9_.:/-]+|"
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b"
)
_PROTOCOL_VALUE_FIELDS = {
    "kind": {"fact", "calculation", "inference", "assumption", "forecast", "risk", "unknown", "opinion"},
    "severity": {"low", "medium", "high", "critical"},
    "mode": {"synthesized", "staged-fallback", "deterministic-only"},
}
_TYPED_SECTION_FIELDS = {
    "executive_summary": {"summary", "text", "analysis", "conclusion"},
    "claims": {"text", "conclusion", "argument", "kind", "confidence", "title"},
    "counterarguments": {"title", "counterargument", "argument", "text", "severity", "confidence"},
    "invalidation_conditions": {"title", "condition", "text", "trigger", "confidence"},
    "leading_indicators": {"title", "indicator", "metric", "text", "confidence"},
    "unresolved_questions": {"title", "question", "text", "confidence"},
}
_MISSING = object()


def _project_scalar(value: Any, *, parent_key: str | None) -> Any:
    if isinstance(value, str):
        allowed = _PROTOCOL_VALUE_FIELDS.get(parent_key or "")
        if allowed is not None and value.casefold().strip() not in allowed:
            return _MISSING
        cleaned = _INTERNAL_ID_RE.sub("", value).strip()
        return cleaned or _MISSING
    return value


def _project_value(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        allowed = _TYPED_SECTION_FIELDS.get(parent_key or "")
        projected: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if (
                name in _INTERNAL_FIELDS
                or name.startswith("_")
                or name not in _REPORT_FIELDS.union(_FIELD_LABELS)
                or (allowed is not None and name not in allowed)
            ):
                continue
            item = _project_value(child, parent_key=name)
            if item is not _MISSING:
                projected[name] = item
        return projected
    if isinstance(value, list):
        return [
            item
            for item in (_project_value(item, parent_key=parent_key) for item in value)
            if item is not _MISSING
        ]
    if isinstance(value, tuple):
        return tuple(
            item
            for item in (_project_value(item, parent_key=parent_key) for item in value)
            if item is not _MISSING
        )
    return _project_scalar(value, parent_key=parent_key)


def project_report_value(
    value: Any,
    *,
    include_technical: bool,
    section: str | None = None,
) -> Any:
    """Return a presentation-safe view without mutating stored research artifacts."""

    if include_technical:
        return value
    return _project_value(value, parent_key=section)


def report_field_label(key: object, language: str) -> str:
    normalized = str(key)
    label = _FIELD_LABELS.get(normalized)
    if label:
        return label[1] if normalize_language(language) == EN else label[0]
    # Never expose an unrecognized protocol key (for example ``severity`` in
    # an older payload) as a raw identifier in user-facing reports.  The
    # diagnostics helper below retains the path for technical inspection.
    return "Other" if normalize_language(language) == EN else "其他"


def project_report_diagnostics(value: Any, *, include_technical: bool = False) -> tuple[str, ...]:
    """Return unknown presentation-key paths without leaking them to users."""

    if include_technical:
        return ()
    unknown: list[str] = []

    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                name = str(key)
                child_path = f"{path}.{name}" if path else name
                if (
                    name not in _INTERNAL_FIELDS
                    and not name.startswith("_")
                    and name not in _FIELD_LABELS
                    and name not in _REPORT_FIELDS
                ):
                    unknown.append(child_path)
                walk(child, child_path)
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")

    walk(value, "")
    return tuple(unknown)


def report_display_value(value: str, language: str) -> str:
    if normalize_language(language) == EN:
        return value
    return _DISPLAY_VALUES_ZH.get(value.casefold().strip(), value)
