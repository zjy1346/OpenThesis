from __future__ import annotations

import logging
import math
import re
from typing import Any

from .i18n import EN, ZH_HANT, normalize_language


_LOGGER = logging.getLogger(__name__)
_EXTENSIBLE_PROSE_LIMIT = 128
_EXTENSIBLE_PROSE_LENGTH = 8_000


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
        "text_format_warning",
        "risks", "unknowns", "possible_moats", "assumptions", "assumption",
        "financial_analysis", "accounting_risk", "information_gaps",
        "mechanism", "category", "maturity_stage", "time_horizon_years",
        "condition", "indicator", "metric", "trigger", "question",
        "probability", "probability_range", "scenario_eligibility",
        "capital_requirements", "leading_indicators", "invalidation_conditions",
        "evidence_grade", "supporting_evidence_count", "contradicting_evidence_count",
        "evidence_count",
        "opportunities", "evidence", "evidence_count", "current_price", "share_price",
        "market_cap", "enterprise_value", "equity_value", "as_of", "valuation",
        "valuation_range", "implied_growth", "implied_revenue_growth", "required_growth",
        "required_return", "expected_return", "discount_rate", "wacc",
        "terminal_growth_rate", "margin_of_safety", "formula", "source",
        "strongest_counterarguments", "unsupported_assumptions", "missing_evidence",
        "risk_flags", "benign_explanations", "follow_up_questions",
        "description", "durability",
        "name", "cagr", "market_assumption", "sensitivity_note",
        "revenue_path", "evidence_basis", "value", "current", "required", "unit",
    }
)

_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "summary": ("摘要", "Summary"),
    "analysis": ("分析", "Analysis"),
    "claims": ("主要结论", "Key Conclusions"),
    "unknowns": ("信息缺口", "Information Gaps"),
    "possible_moats": ("潜在护城河", "Potential Moats"),
    "description": ("说明", "Description"),
    "durability": ("持续性", "Durability"),
    "name": ("名称", "Name"),
    "cagr": ("复合增速", "CAGR"),
    "market_assumption": ("市场假设", "Market Assumption"),
    "sensitivity_note": ("敏感性说明", "Sensitivity Note"),
    "revenue_path": ("收入路径", "Revenue Path"),
    "evidence_basis": ("证据基础", "Evidence Basis"),
    "value": ("数值", "Value"),
    "current": ("当前值", "Current"),
    "required": ("要求值", "Required"),
    "unit": ("单位", "Unit"),
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
    "evidence_count": ("有效引用数", "Valid Evidence References"),
    "supporting_evidence_count": ("支持证据数", "Supporting Evidence"),
    "contradicting_evidence_count": ("反证数", "Contradicting Evidence"),
    "assets": ("资产总额", "Total Assets"),
    "liabilities": ("负债总额", "Total Liabilities"),
    "equity": ("归属于母公司股东权益", "Equity Attributable to Owners"),
    "total_equity": ("所有者权益合计", "Total Equity"),
    "text": ("结论", "Conclusion"),
    "kind": ("类型", "Type"),
    "base": ("基准情景", "Base Case"),
    "bear": ("悲观情景", "Bear Case"),
    "bull": ("乐观情景", "Bull Case"),
    "probability": ("可能性", "Probability"),
    "probability_range": ("可能性区间", "Probability Range"),
    "condition": ("失效条件", "Condition"),
    "indicator": ("指标", "Indicator"),
    "metric": ("指标", "Metric"),
    "question": ("待解问题", "Question"),
    "unknowns": ("信息缺口", "Information Gaps"),
    "revenue_cagr_range": ("营收复合增速区间", "Revenue CAGR Range"),
    "operating_margin_range": ("营业利润率区间", "Operating Margin Range"),
}
_FIELD_LABELS_HANT = {
    "summary": "摘要", "analysis": "分析", "claims": "主要結論", "unknowns": "資訊缺口",
    "possible_moats": "潛在護城河", "risks": "主要風險", "conclusion": "結論",
    "description": "說明", "durability": "持續性",
    "name": "名稱", "cagr": "複合增速", "market_assumption": "市場假設",
    "sensitivity_note": "敏感性說明", "revenue_path": "收入路徑",
    "evidence_basis": "證據基礎", "value": "數值", "current": "當前值",
    "required": "要求值", "unit": "單位",
    "strengths": "優勢", "concerns": "關注事項", "opportunities": "增長機會",
    "leading_indicators": "領先指標", "invalidation_conditions": "失效條件",
    "counterargument": "反方觀點", "severity": "嚴重程度", "title": "標題",
    "confidence": "信心程度", "text": "結論", "kind": "類型",
    "evidence_count": "有效引用數",
    "supporting_evidence_count": "支持證據數",
    "contradicting_evidence_count": "反證數",
    "assets": "資產總額", "liabilities": "負債總額",
    "equity": "歸屬於母公司股東權益", "total_equity": "所有者權益合計",
    "financial_analysis": "財務分析", "accounting_risk": "會計風險",
    "information_gaps": "資訊缺口",
    "risk_flags": "風險信號", "benign_explanations": "可能的合理解釋",
    "follow_up_questions": "後續核查問題", "strongest_counterarguments": "核心反方觀點",
    "unsupported_assumptions": "證據不足的假設", "missing_evidence": "缺失證據",
    "scenarios": "情境", "capital_requirements": "資本需求",
    "maturity_stage": "成熟階段", "category": "類別", "mechanism": "增長機制",
    "time_horizon_years": "時間跨度（年）", "argument": "反方論點",
    "assumption": "假設", "assumptions": "關鍵假設", "challenge": "質疑",
    "base": "基準情境", "bear": "悲觀情境", "bull": "樂觀情境",
    "probability": "可能性", "probability_range": "可能性區間",
    "condition": "失效條件", "indicator": "指標", "metric": "指標", "question": "待解問題",
    "revenue_cagr_range": "營收複合增速區間",
    "operating_margin_range": "營業利潤率區間",
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
    "cost_advantage": "成本优势",
    "network_effects": "网络效应",
    "switching_costs": "转换成本",
    "intangible_assets": "无形资产",
    "efficient_scale": "有效规模",
    "brand": "品牌",
    "distribution": "渠道优势",
    "data_advantage": "数据优势",
    "regulatory_advantage": "监管优势",
    "other": "其他",
    "base": "基准",
    "bear": "悲观",
    "bull": "乐观",
}

_DISPLAY_VALUES_EN = {
    "calculation": "Calculation",
    "assumption": "Assumption",
    "forecast": "Forecast",
    "risk": "Risk",
    "inference": "Inference",
    "fact": "Fact",
    "opinion": "Opinion",
    "unknown": "Unknown",
    "cost_advantage": "Cost advantage",
    "network_effects": "Network effects",
    "switching_costs": "Switching costs",
    "intangible_assets": "Intangible assets",
    "efficient_scale": "Efficient scale",
    "brand": "Brand",
    "distribution": "Distribution advantage",
    "data_advantage": "Data advantage",
    "regulatory_advantage": "Regulatory advantage",
    "other": "Other",
    "base": "Base",
    "bear": "Bear",
    "bull": "Bull",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


_DISPLAY_VALUES_ZH.update({"high": "高", "medium": "中", "low": "低"})

_DISPLAY_VALUES_HANT = {
    **_DISPLAY_VALUES_ZH,
    "calculation": "計算",
    "assumption": "假設",
    "forecast": "預測",
    "risk": "風險",
    "inference": "推論",
    "fact": "事實",
    "opinion": "觀點",
    "unknown": "未知",
    "cost_advantage": "成本優勢",
    "network_effects": "網絡效應",
    "switching_costs": "轉換成本",
    "intangible_assets": "無形資產",
    "efficient_scale": "有效規模",
    "brand": "品牌",
    "distribution": "渠道優勢",
    "data_advantage": "數據優勢",
    "regulatory_advantage": "監管優勢",
    "other": "其他",
    "base": "基準",
    "bear": "悲觀",
    "bull": "樂觀",
    "high": "高",
    "medium": "中",
    "low": "低",
}


_INTERNAL_ID_RE = re.compile(
    r"(?i)(?:fact|evidence|filing|artifact|run):[A-Za-z0-9_.:/-]+|"
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b"
)
_INTERNAL_ID_TOKEN_RE = re.compile(
    r"(?i)(?:fact|evidence|filing|artifact|run):[A-Za-z0-9_.:/-]+|"
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b"
)
_PROTOCOL_VALUE_FIELDS = {
    "kind": {"fact", "calculation", "inference", "assumption", "forecast", "risk", "unknown", "opinion"},
    "severity": {"low", "medium", "high", "critical"},
    "mode": {"synthesized", "staged-fallback", "deterministic-only"},
}
_PATH_VALUE_FIELDS = {
    ("possible_moats", "kind"): {
        "cost_advantage", "network_effects", "switching_costs",
        "intangible_assets", "efficient_scale", "brand", "distribution",
        "data_advantage", "regulatory_advantage", "unknown", "other",
    },
    ("possible_moats", "durability"): {"low", "medium", "high", "unknown"},
}
_NESTED_TYPED_FIELDS = {
    "possible_moats": {"kind", "description", "durability", "summary", "text", "title"},
}
_TYPED_SECTION_FIELDS = {
    "executive_summary": {"summary", "text", "body", "analysis", "conclusion"},
    "claims": {"text", "conclusion", "argument", "kind", "confidence", "title", "text_format_warning"},
    "business_model": {"summary", "body", "analysis", "conclusion", "possible_moats", "risks", "unknowns", "strengths", "concerns"},
    "financial_quality": {"summary", "body", "analysis", "conclusion", "financial_analysis", "accounting_risk", "strengths", "concerns", "risks", "unknowns", "risk_flags", "benign_explanations", "follow_up_questions"},
    "balance_sheet": {"summary", "body", "analysis", "conclusion", "strengths", "concerns", "risks", "unknowns", "risk_flags", "benign_explanations", "follow_up_questions", "assets", "liabilities", "equity", "total_equity"},
    "competitive_position": {"summary", "body", "analysis", "conclusion", "possible_moats", "strengths", "concerns", "risks", "unknowns"},
    "counterarguments": {"title", "counterargument", "argument", "text", "body", "severity", "confidence", "strongest_counterarguments", "unsupported_assumptions", "missing_evidence", "claims"},
    "invalidation_conditions": {"title", "condition", "text", "body", "trigger", "confidence"},
    "leading_indicators": {"title", "indicator", "metric", "text", "body", "confidence"},
    "unresolved_questions": {"title", "question", "text", "body", "confidence"},
    "scenarios": {"summary", "body", "analysis", "conclusion", "base", "bear", "bull", "name", "cagr", "probability", "probability_range", "revenue_cagr_range", "operating_margin_range", "assumptions", "text"},
    "thesis": {"summary", "body", "analysis", "conclusion", "text", "claims", "assumptions", "confidence"},
    "implied_expectations": {
        "summary", "body", "analysis", "conclusion", "text", "assumptions", "scenarios",
        "current_price", "share_price", "market_cap", "enterprise_value", "equity_value",
        "currency", "as_of", "valuation", "valuation_range", "implied_growth",
        "implied_revenue_growth", "required_growth", "required_return", "expected_return",
        "revenue_cagr_range", "operating_margin_range", "probability", "probability_range",
        "discount_rate", "wacc", "terminal_growth_rate", "margin_of_safety", "formula",
        "source", "evidence", "confidence", "metrics",
        "horizon_years", "market_assumption", "sensitivity_note", "unit", "value", "current", "required",
    },
}


def typed_section_fields(section: str) -> frozenset[str]:
    """Return the declared top-level fields for a report section."""

    return frozenset(_TYPED_SECTION_FIELDS.get(section, ()))
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


def resolve_report_identity(
    artifacts: list[dict[str, Any]],
    *,
    explicit_name: str = "",
) -> dict[str, str]:
    """Resolve one canonical company identity for every report renderer."""

    identity: dict[str, str] = {
        "name": str(explicit_name or "").strip(),
        "ticker": "",
        "market": "",
        "exchange": "",
        "cik": "",
    }
    candidates: list[dict[str, Any]] = []
    for artifact in reversed(artifacts):
        if not isinstance(artifact, dict):
            continue
        content = artifact.get("content")
        if not isinstance(content, dict):
            continue
        company = content.get("company")
        if isinstance(company, dict):
            candidates.append(company)
        if artifact.get("artifact_type") == "verified-research-dossier":
            legacy = content.get("issuer")
            if isinstance(legacy, dict):
                candidates.append(legacy)
    for candidate in candidates:
        for key in identity:
            if key == "name" and identity[key]:
                continue
            value = str(candidate.get(key) or "").strip()
            if value and not identity[key]:
                identity[key] = value
        if identity["name"] and identity["ticker"]:
            break
    return identity


def report_identity_title(
    artifacts: list[dict[str, Any]],
    language: str,
    *,
    explicit_name: str = "",
) -> str:
    identity = resolve_report_identity(artifacts, explicit_name=explicit_name)
    name = identity["name"]
    ticker = identity["ticker"]
    if name:
        return f"{name} · {ticker}" if ticker and ticker not in name else name
    locale = normalize_language(language)
    return (
        "Company Research Report"
        if locale == EN
        else "公司研究報告"
        if locale == ZH_HANT
        else "公司研究报告"
    )


def _project_scalar(
    value: Any,
    *,
    parent_key: str | None,
    container_key: str | None = None,
) -> Any:
    if isinstance(value, str):
        allowed = _PATH_VALUE_FIELDS.get(
            (container_key or "", parent_key or ""),
            _PROTOCOL_VALUE_FIELDS.get(parent_key or ""),
        )
        if allowed is not None and value.casefold().strip() not in allowed:
            if container_key in _NESTED_TYPED_FIELDS and parent_key in {"kind", "durability"}:
                return "unknown"
            return _MISSING
        def remove_id_group(match: re.Match[str]) -> str:
            parts = [part.strip() for part in match.group(1).split(",")]
            return "" if parts and all(_INTERNAL_ID_TOKEN_RE.fullmatch(part) for part in parts) else match.group(0)

        cleaned = re.sub(r"\[([^\[\]]+)\]", remove_id_group, value)
        cleaned = _INTERNAL_ID_RE.sub("", cleaned)
        cleaned = re.sub(r"\[\s*(?:,\s*)+\]", "", cleaned)
        # Removing a private citation token can leave only its punctuation
        # inside the surrounding parentheses, e.g. ``(filing:id, )``.
        cleaned = re.sub(r"[\(（]\s*[,，;；、]*\s*[\)）]", "", cleaned)
        return cleaned or _MISSING
    return value


def claim_body_text(value: Any) -> tuple[str | None, bool]:
    """Return only contract-valid claim text; never guess from malformed data."""

    if isinstance(value, str):
        text = value.strip()
        return (text or None), False
    return None, value is not None


def claim_text_format_notice(language: str) -> str:
    locale = normalize_language(language)
    return (
        "One or more conclusion text fields had an invalid type and were not displayed; retry synthesis to regenerate them."
        if locale == EN
        else "部分主要結論的正文欄位類型錯誤，內容未顯示；請重試綜合階段以重新產生。"
        if locale == ZH_HANT
        else "部分主要结论的正文文本字段类型错误，内容未显示；请重试综合阶段重新生成。"
    )


def _project_value(
    value: Any,
    *,
    parent_key: str | None = None,
    container_key: str | None = None,
    root_section: str | None = None,
    available_evidence: set[str] | None = None,
) -> Any:
    root_section = root_section or parent_key
    if isinstance(value, dict):
        # Identity/provenance metadata on a claim must never be inferred as
        # prose.  The previous extensible-field fallback appended unknown
        # strings (company, concept, period, unit) to ``text`` and changed a
        # valid string into a list that the strict renderer then rejected.
        if root_section == "claims" and not isinstance(value.get("text"), str):
            body_text, malformed_body = claim_body_text(value.get("body"))
            if body_text:
                value = {**value, "text": body_text}
            elif malformed_body:
                value = {**value, "text_format_warning": True}
        if root_section == "claims" and isinstance(value.get("text"), (list, tuple)):
            normalized_claim = dict(value)
            body_text, malformed = claim_body_text(value.get("text"))
            if body_text:
                normalized_claim["text"] = body_text
            else:
                normalized_claim.pop("text", None)
            if malformed:
                normalized_claim["text_format_warning"] = True
                _LOGGER.warning("report_projection_malformed_claim_text")
            value = normalized_claim
        allowed = _NESTED_TYPED_FIELDS.get(
            parent_key or "", _TYPED_SECTION_FIELDS.get(parent_key or "")
        )
        projected: dict[str, Any] = {}
        extensible_nested_expectations = root_section == "implied_expectations"
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
                        and available_evidence is not None
                        and str(item).strip() in available_evidence
                    }
                )
        evidence_ids = value.get("evidence_ids")
        if isinstance(evidence_ids, list) and available_evidence is not None:
            projected["evidence_count"] = len(
                {
                    str(item).strip()
                    for item in evidence_ids
                    if str(item).strip() in available_evidence
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
                or (
                    name not in _REPORT_FIELDS.union(_FIELD_LABELS)
                    and not (allowed is not None and name in allowed)
                    and not (
                        extensible_nested_expectations
                        and isinstance(child, (dict, list, tuple))
                    )
                )
                or (
                    allowed is not None
                    and name not in allowed
                    and not (
                        extensible_nested_expectations
                        and isinstance(child, (dict, list, tuple))
                    )
                )
            ):
                continue
            item = _project_value(
                child,
                parent_key=name,
                container_key=parent_key,
                root_section=root_section,
                available_evidence=available_evidence,
            )
            if item is not _MISSING:
                projected[name] = item
        return projected
    if isinstance(value, list):
        return [
            item
            for item in (
                _project_value(
                    item, parent_key=parent_key, container_key=container_key,
                    root_section=root_section,
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
                    item, parent_key=parent_key, container_key=container_key,
                    root_section=root_section,
                    available_evidence=available_evidence,
                )
                for item in value
            )
            if item is not _MISSING
        )
    return _project_scalar(
        value, parent_key=parent_key, container_key=container_key
    )


def project_report_value(
    value: Any,
    *,
    include_technical: bool,
    section: str | None = None,
    available_evidence: set[str] | None = None,
) -> Any:
    """Return a presentation-safe view without mutating stored research artifacts."""

    if not include_technical:
        drift = project_report_diagnostics(value)
        if drift:
            _LOGGER.warning(
                "report_projection_schema_drift",
                extra={"unknown_paths": [item[:160] for item in drift[:128]], "path_count": len(drift)},
            )

    if include_technical:
        return value
    if section and isinstance(value, dict) and section in value:
        return {
            section: _project_value(
                value[section],
                parent_key=section,
                root_section=section,
                available_evidence=available_evidence,
            )
        }
    return _project_value(
        value, parent_key=section, root_section=section,
        available_evidence=available_evidence
    )


_PERCENT_FIELDS = frozenset(
    {
        "probability", "growth_rate", "revenue_growth", "revenue_cagr",
        "revenue_cagr_rate", "operating_margin", "operating_margin_rate",
        "discount_rate", "wacc", "terminal_growth_rate", "implied_growth",
        "implied_revenue_growth", "required_growth", "required_return",
        "expected_return", "margin_of_safety", "gross_margin", "net_margin",
        "reported_roe", "return_on_equity", "roe",
        "cagr",
    }
)
_PERCENT_RANGE_FIELDS = frozenset(
    {"probability_range", "revenue_cagr_range", "operating_margin_range"}
)


def format_report_semantic_value(value: Any, field: str | None) -> str | None:
    """Format declared fractional rates/ranges without exposing raw decimals."""
    normalized_field = str(field or "").casefold()

    def percentage(item: Any) -> str | None:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        numeric = float(item)
        if not math.isfinite(numeric):
            return None
        rendered = f"{numeric * 100:.1f}".rstrip("0").rstrip(".")
        return f"{rendered}%"

    if normalized_field in _PERCENT_RANGE_FIELDS and isinstance(value, (list, tuple)):
        if len(value) == 2:
            left, right = percentage(value[0]), percentage(value[1])
            if left is not None and right is not None:
                return f"{left}–{right}"
    if normalized_field in _PERCENT_FIELDS:
        return percentage(value)
    return None


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
    # Agent versions have used both a direct list and a typed object for the
    # opposing-views section. Canonicalise aliases once at the projection seam
    # so Markdown, HTML and the desktop renderer cannot silently disagree.
    counter = report.get("counterarguments")
    if isinstance(counter, dict):
        counter = dict(counter)
        aliases = {
            "counterarguments": "strongest_counterarguments",
            "strongest_arguments_against": "strongest_counterarguments",
            "unsupported": "unsupported_assumptions",
            "evidence_gaps": "missing_evidence",
        }
        for alias, canonical in aliases.items():
            if counter.get(canonical) in (None, "", [], {}) and counter.get(alias) not in (None, "", [], {}):
                counter[canonical] = counter[alias]
            counter.pop(alias, None)
        report["counterarguments"] = counter
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

    def walk(
        item: Any,
        path: str,
        *,
        root_section: str = "",
        section_depth: int = 0,
    ) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                name = str(key)
                child_path = f"{path}.{name}" if path else name
                section = root_section or (name if name in _TYPED_SECTION_FIELDS else "")
                depth = (
                    0 if not root_section and section
                    else section_depth + 1 if root_section
                    else section_depth
                )
                section_field_is_unknown = bool(
                    root_section
                    and section_depth == 0
                    and root_section != "claims"
                    and root_section in _TYPED_SECTION_FIELDS
                    and name not in _TYPED_SECTION_FIELDS[root_section]
                )
                if (
                    name not in _INTERNAL_FIELDS
                    and not name.startswith("_")
                    and (
                        name not in _FIELD_LABELS
                        and name not in _REPORT_FIELDS
                        or section_field_is_unknown
                    )
                ):
                    unknown.append(child_path)
                walk(child, child_path, root_section=section, section_depth=depth)
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(
                    child,
                    f"{path}[{index}]",
                    root_section=root_section,
                    section_depth=section_depth,
                )

    walk(value, "")
    return tuple(unknown)


def report_display_value(value: str, language: str) -> str:
    locale = normalize_language(language)
    if locale == EN:
        return _DISPLAY_VALUES_EN.get(value.casefold().strip(), value)
    if locale == ZH_HANT:
        return _DISPLAY_VALUES_HANT.get(value.casefold().strip(), value)
    return _DISPLAY_VALUES_ZH.get(value.casefold().strip(), value)
