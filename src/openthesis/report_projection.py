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
