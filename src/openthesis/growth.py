from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .i18n import EN, normalize_language


MAX_GROWTH_OPPORTUNITIES = 5

GROWTH_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "title": ("机会名称", "Opportunity"),
    "category": ("类别", "Category"),
    "mechanism": ("增长机制", "Growth mechanism"),
    "evidence_grade": ("证据等级", "Evidence grade"),
    "maturity_stage": ("成熟阶段", "Maturity stage"),
    "time_horizon_years": ("时间跨度", "Time horizon"),
    "probability_range": ("可能性区间", "Probability range"),
    "capital_requirements": ("资本要求", "Capital requirements"),
    "leading_indicators": ("领先指标", "Leading indicators"),
    "invalidation_conditions": ("失效条件", "Invalidation conditions"),
    "scenario_eligibility": ("适用情景", "Eligible scenarios"),
    "supporting_evidence_ids": ("支持证据", "Supporting evidence"),
    "contradicting_evidence_ids": ("相反证据", "Contradicting evidence"),
}

EVIDENCE_GRADE_LABELS: dict[str, tuple[str, str]] = {
    "A": ("A · 强直接证据", "A · Strong direct evidence"),
    "B": ("B · 较强证据", "B · Good evidence"),
    "C": ("C · 有限证据", "C · Limited evidence"),
    "D": ("D · 主要为推论", "D · Primarily inferred"),
    "E": ("E · 假设性机会", "E · Speculative opportunity"),
    "U": ("未评级", "Unrated"),
}

SCENARIO_LABELS: dict[str, tuple[str, str]] = {
    "bear": ("悲观情景", "Bear scenario"),
    "downside": ("悲观情景", "Downside scenario"),
    "base": ("基准情景", "Base scenario"),
    "bull": ("乐观情景", "Bull scenario"),
    "upside": ("乐观情景", "Upside scenario"),
}

_ALLOWED_SCENARIOS = frozenset(SCENARIO_LABELS)


@dataclass(slots=True)
class GrowthValidation:
    output: dict[str, Any]
    issues: list[str]

    @property
    def passed(self) -> bool:
        return not self.issues


def _pick(language: str, chinese: str, english: str) -> str:
    return english if normalize_language(language) == EN else chinese


def growth_field_label(field: str, language: str = "zh-CN") -> str:
    labels = GROWTH_FIELD_LABELS.get(field)
    if labels:
        return _pick(language, labels[0], labels[1])
    return field.replace("_", " ").title() if normalize_language(language) == EN else field.replace("_", " ")


def evidence_grade_label(grade: object, language: str = "zh-CN") -> str:
    normalized = str(grade or "U").strip().upper()
    labels = EVIDENCE_GRADE_LABELS.get(normalized, EVIDENCE_GRADE_LABELS["U"])
    return _pick(language, labels[0], labels[1])


def scenario_label(value: object, language: str = "zh-CN") -> str:
    normalized = str(value or "").strip().lower()
    labels = SCENARIO_LABELS.get(normalized)
    if labels:
        return _pick(language, labels[0], labels[1])
    return str(value or "")


def format_probability_range(
    value: object,
    language: str = "zh-CN",
) -> str:
    pair = _probability_pair(value)
    if pair is None:
        return _pick(language, "证据不足", "Insufficient evidence")
    low, high = pair
    return f"{low * 100:.0f}%–{high * 100:.0f}%"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _probability_pair(value: object) -> tuple[float, float] | None:
    candidates: Iterable[object]
    if isinstance(value, dict):
        candidates = (value.get("low"), value.get("high"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        candidates = value
    else:
        return None
    try:
        low, high = (float(item) for item in candidates)
    except (TypeError, ValueError):
        return None
    if not (0 <= low <= high <= 1):
        return None
    return low, high


def _opportunity_items(value: object) -> list[object]:
    if isinstance(value, dict):
        if isinstance(value.get("opportunities"), list):
            return list(value["opportunities"])
        if "title" in value or "mechanism" in value:
            return [value]
        return []
    if isinstance(value, list):
        return list(value)
    return []


def normalize_growth_output(
    output: object,
    available_evidence: set[str] | None = None,
    language: str = "zh-CN",
) -> GrowthValidation:
    english = normalize_language(language) == EN
    issues: list[str] = []
    source = dict(output) if isinstance(output, dict) else {}
    if not isinstance(output, dict):
        issues.append(
            "Growth output must be a JSON object."
            if english
            else "增长机会输出必须是 JSON 对象。"
        )
    raw_items = _opportunity_items(output)
    if isinstance(output, dict) and "opportunities" not in output:
        issues.append(
            "Growth output is missing the opportunities array."
            if english
            else "增长机会输出缺少 opportunities 数组。"
        )
    if len(raw_items) > MAX_GROWTH_OPPORTUNITIES:
        issues.append(
            f"Only the first {MAX_GROWTH_OPPORTUNITIES} growth opportunities were retained."
            if english
            else f"增长机会最多保留前 {MAX_GROWTH_OPPORTUNITIES} 项。"
        )

    normalized_items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items[:MAX_GROWTH_OPPORTUNITIES], start=1):
        if not isinstance(raw, dict):
            issues.append(
                f"Growth opportunity {index} is not a JSON object."
                if english
                else f"第 {index} 个增长机会不是 JSON 对象。"
            )
            continue

        title = str(raw.get("title", "")).strip()
        mechanism = str(raw.get("mechanism", "")).strip()
        if not title:
            issues.append(
                f"Growth opportunity {index} has no title."
                if english
                else f"第 {index} 个增长机会缺少标题。"
            )
            title = (
                f"Unnamed opportunity {index}"
                if english
                else f"未命名机会 {index}"
            )
        if not mechanism:
            issues.append(
                f"Growth opportunity {index} has no growth mechanism."
                if english
                else f"第 {index} 个增长机会缺少增长机制。"
            )

        grade = str(raw.get("evidence_grade", "U")).strip().upper()
        if grade not in EVIDENCE_GRADE_LABELS:
            issues.append(
                f"Growth opportunity {index} has an invalid evidence grade."
                if english
                else f"第 {index} 个增长机会的证据等级无效。"
            )
            grade = "U"

        probability = _probability_pair(raw.get("probability_range"))
        if probability is None:
            issues.append(
                f"Growth opportunity {index} has an invalid probability range."
                if english
                else f"第 {index} 个增长机会的可能性区间无效。"
            )

        horizon: int | None
        try:
            horizon = int(raw.get("time_horizon_years"))
        except (TypeError, ValueError):
            horizon = None
        if horizon is None or not 1 <= horizon <= 20:
            issues.append(
                f"Growth opportunity {index} has an invalid time horizon."
                if english
                else f"第 {index} 个增长机会的时间跨度无效。"
            )
            horizon = None

        supporting = _string_list(raw.get("supporting_evidence_ids"))
        contradicting = _string_list(raw.get("contradicting_evidence_ids"))
        unknown_evidence: list[str] = []
        if available_evidence is not None:
            unknown_evidence = [
                evidence_id
                for evidence_id in supporting + contradicting
                if evidence_id not in available_evidence
            ]
            if unknown_evidence:
                issues.append(
                    (
                        f"Growth opportunity {index} references unknown evidence: "
                        + ", ".join(unknown_evidence)
                    )
                    if english
                    else (
                        f"第 {index} 个增长机会引用了不存在的证据："
                        + "、".join(unknown_evidence)
                    )
                )

        scenarios = [
            item.lower()
            for item in _string_list(raw.get("scenario_eligibility"))
            if item.lower() in _ALLOWED_SCENARIOS
        ]
        invalid_scenarios = [
            item
            for item in _string_list(raw.get("scenario_eligibility"))
            if item.lower() not in _ALLOWED_SCENARIOS
        ]
        if invalid_scenarios:
            issues.append(
                f"Growth opportunity {index} contains unknown scenario labels."
                if english
                else f"第 {index} 个增长机会包含未知情景标签。"
            )

        normalized_items.append(
            {
                "opportunity_id": str(
                    raw.get("opportunity_id") or f"growth-{index}"
                ),
                "title": title,
                "category": str(raw.get("category", "")).strip(),
                "mechanism": mechanism,
                "evidence_grade": grade,
                "maturity_stage": str(raw.get("maturity_stage", "")).strip(),
                "time_horizon_years": horizon,
                "probability_range": (
                    [probability[0], probability[1]] if probability else []
                ),
                "supporting_evidence_ids": supporting,
                "contradicting_evidence_ids": contradicting,
                "unknown_evidence_ids": unknown_evidence,
                "capital_requirements": str(
                    raw.get("capital_requirements", "")
                ).strip(),
                "leading_indicators": _string_list(
                    raw.get("leading_indicators")
                ),
                "invalidation_conditions": _string_list(
                    raw.get("invalidation_conditions")
                ),
                "scenario_eligibility": scenarios,
            }
        )

    normalized = source
    normalized["opportunities"] = normalized_items
    normalized["_validation"] = {
        "passed": not issues,
        "issues": list(issues),
    }
    return GrowthValidation(output=normalized, issues=issues)


def growth_opportunities_from_value(
    value: object,
    language: str = "zh-CN",
) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("opportunities"), list):
        source: object = value
    else:
        source = {"opportunities": _opportunity_items(value)}
    return normalize_growth_output(source, None, language).output["opportunities"]
