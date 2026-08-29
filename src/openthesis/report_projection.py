from __future__ import annotations

import re
from typing import Any

from .i18n import EN, ZH_HANT, normalize_language


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
        "financial_analysis", "accounting_risk", "information_gaps",
        "mechanism", "category", "maturity_stage", "time_horizon_years",
        "condition", "indicator", "metric", "trigger", "question",
        "probability", "probability_range", "scenario_eligibility",
        "capital_requirements", "leading_indicators", "invalidation_conditions",
        "evidence_grade", "supporting_evidence_count", "contradicting_evidence_count",
        "opportunities",
    }
)

_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "summary": ("摘要", "Summary"),
    "analysis": ("分析", "Analysis"),
    "claims": ("主要结论", "Key Conclusions"),
    "unknowns": ("信息缺口", "Information Gaps"),
    "possible_moats": ("潜在护城河", "Potential Moats"),
    "financial_analysis": ("财务分析", "Financial Analysis"),
    "accounting_risk": ("会计风险", "Accounting Risk"),
    "information_gaps": ("信息缺口", "Information Gaps"),
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
_FIELD_LABELS_HANT = {
    "summary": "摘要", "analysis": "分析", "claims": "主要結論", "unknowns": "資訊缺口",
    "possible_moats": "潛在護城河", "risks": "主要風險", "conclusion": "結論",
    "strengths": "優勢", "concerns": "關注事項", "opportunities": "增長機會",
    "leading_indicators": "領先指標", "invalidation_conditions": "失效條件",
    "counterargument": "反方觀點", "severity": "嚴重程度", "title": "標題",
    "confidence": "信心程度", "text": "結論", "kind": "類型",
    "financial_analysis": "財務分析", "accounting_risk": "會計風險",
    "information_gaps": "資訊缺口",
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

_DISPLAY_VALUES_HANT = {
    **_DISPLAY_VALUES_ZH,
    "calculation": "計算",
    "forecast": "預測",
    "inference": "推論",
    "opinion": "觀點",
    "high": "高",
    "medium": "中",
    "low": "低",
}


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
    "business_model": {"summary", "analysis", "conclusion", "possible_moats", "risks", "unknowns", "strengths", "concerns"},
    "financial_quality": {"summary", "analysis", "conclusion", "financial_analysis", "accounting_risk", "strengths", "concerns", "risks", "unknowns"},
    "balance_sheet": {"summary", "analysis", "conclusion", "strengths", "concerns", "risks", "unknowns", "assets", "liabilities", "equity", "total_equity"},
    "competitive_position": {"summary", "analysis", "conclusion", "possible_moats", "strengths", "concerns", "risks", "unknowns"},
    "counterarguments": {"title", "counterargument", "argument", "text", "severity", "confidence"},
    "invalidation_conditions": {"title", "condition", "text", "trigger", "confidence"},
    "leading_indicators": {"title", "indicator", "metric", "text", "confidence"},
    "unresolved_questions": {"title", "question", "text", "confidence"},
    "scenarios": {"summary", "analysis", "conclusion", "base", "bear", "bull", "probability", "probability_range", "assumptions", "text"},
    "thesis": {"summary", "analysis", "conclusion", "text", "claims", "assumptions", "confidence"},
}
_REQUIRED_QUALITATIVE_SECTIONS = (
    "executive_summary",
    "claims",
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
)
_MISSING = object()


def _project_scalar(value: Any, *, parent_key: str | None) -> Any:
    if isinstance(value, str):
        allowed = _PROTOCOL_VALUE_FIELDS.get(parent_key or "")
        if allowed is not None and value.casefold().strip() not in allowed:
            return _MISSING
        cleaned = _INTERNAL_ID_RE.sub("", value).strip()
        return cleaned or _MISSING
    return value


def _project_value(
    value: Any,
    *,
    parent_key: str | None = None,
    available_evidence: set[str] | None = None,
) -> Any:
    if isinstance(value, dict):
        allowed = _TYPED_SECTION_FIELDS.get(parent_key or "")
        projected: dict[str, Any] = {}
        unknown_evidence = {
            str(item).strip()
            for item in value.get("unknown_evidence_ids", [])
            if str(item).strip()
        } if isinstance(value.get("unknown_evidence_ids"), list) else set()
        for evidence_field, count_field in (
            ("supporting_evidence_ids", "supporting_evidence_count"),
            ("contradicting_evidence_ids", "contradicting_evidence_count"),
        ):
            evidence_ids = value.get(evidence_field)
            if isinstance(evidence_ids, list):
                projected[count_field] = len(
                    {
                        str(item).strip()
                        for item in evidence_ids
                        if str(item).strip()
                        and str(item).strip() not in unknown_evidence
                        and (
                            available_evidence is None
                            or str(item).strip() in available_evidence
                        )
                    }
                )
        for key, child in value.items():
            name = str(key)
            # Evidence counts are derived above from registered evidence IDs;
            # never copy model-supplied count values back over that result.
            if name in {"supporting_evidence_count", "contradicting_evidence_count"}:
                continue
            if (
                name in _INTERNAL_FIELDS
                or name.startswith("_")
                or name not in _REPORT_FIELDS.union(_FIELD_LABELS)
                or (allowed is not None and name not in allowed)
            ):
                continue
            item = _project_value(
                child, parent_key=name, available_evidence=available_evidence
            )
            if item is not _MISSING:
                projected[name] = item
        return projected
    if isinstance(value, list):
        return [
            item
            for item in (
                _project_value(
                    item, parent_key=parent_key,
                    available_evidence=available_evidence,
                )
                for item in value
            )
            if item is not _MISSING
        ]
    if isinstance(value, tuple):
        return tuple(
            item
            for item in (
                _project_value(
                    item, parent_key=parent_key,
                    available_evidence=available_evidence,
                )
                for item in value
            )
            if item is not _MISSING
        )
    return _project_scalar(value, parent_key=parent_key)


def project_report_value(
    value: Any,
    *,
    include_technical: bool,
    section: str | None = None,
    available_evidence: set[str] | None = None,
) -> Any:
    """Return a presentation-safe view without mutating stored research artifacts."""

    if include_technical:
        return value
    return _project_value(
        value, parent_key=section, available_evidence=available_evidence
    )


def normalize_report_sections(value: Any, language: str) -> dict[str, Any]:
    """Normalize legacy/partial synthesis into one section-aware view.

    This function does not invent qualitative conclusions. Missing sections
    receive an explicit localized capability/evidence notice so a partially
    valid synthesis cannot silently erase whole report areas.
    """

    report = dict(value) if isinstance(value, dict) else {}
    if value not in (None, "") and not isinstance(value, dict):
        report["executive_summary"] = value
    narrative = report.pop("narrative", None)
    if narrative and not report.get("executive_summary"):
        report["executive_summary"] = narrative
    business = report.get("business_model")
    if isinstance(business, dict):
        business = dict(business)
        embedded_claims = business.pop("claims", None)
        if embedded_claims and not report.get("claims"):
            report["claims"] = embedded_claims
        report["business_model"] = business
    if report.get("unknowns") and not report.get("unresolved_questions"):
        report["unresolved_questions"] = report.get("unknowns")
    locale = normalize_language(language)
    missing = (
        "This section was not returned with verifiable content in the current research stage."
        if locale == EN
        else "本研究階段未返回可驗證的此章節內容。"
        if locale == ZH_HANT
        else "当前研究阶段未返回可验证的此章节内容。"
    )
    for key in _REQUIRED_QUALITATIVE_SECTIONS:
        if report.get(key) in (None, "", [], {}):
            report[key] = missing
    return report


def report_field_label(key: object, language: str) -> str:
    normalized = str(key)
    label = _FIELD_LABELS.get(normalized)
    if label:
        locale = normalize_language(language)
        return label[1] if locale == EN else _FIELD_LABELS_HANT.get(normalized, label[0]) if locale == ZH_HANT else label[0]
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
    locale = normalize_language(language)
    if locale == EN:
        return value
    if locale == ZH_HANT:
        return _DISPLAY_VALUES_HANT.get(value.casefold().strip(), value)
    return _DISPLAY_VALUES_ZH.get(value.casefold().strip(), value)
