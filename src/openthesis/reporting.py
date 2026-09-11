from __future__ import annotations

from typing import Any

from .financials import (
    deterministic_summary,
    format_money,
    format_percent,
    reverse_dcf_disclaimer,
    reverse_dcf_status_text,
)
from .growth import (
    GROWTH_FIELD_LABELS,
    evidence_grade_label,
    format_evidence_summary,
    format_probability_range,
    growth_field_label,
    growth_opportunities_from_value,
    scenario_label,
)
from .i18n import EN, UI_HANT, ZH_HANT, normalize_language
from .report_projection import normalize_report_sections, project_report_value, report_display_value, report_field_label


SECTION_LABELS_ZH = {
    "executive_summary": "执行摘要",
    "claims": "主要结论",
    "business_model": "商业模式",
    "financial_quality": "财务质量",
    "balance_sheet": "资产负债表",
    "competitive_position": "竞争地位",
    "growth_opportunities": "增长机会",
    "counterarguments": "反方观点",
    "scenarios": "长期经营情景",
    "implied_expectations": "当前估值隐含预期",
    "thesis": "投资逻辑",
    "invalidation_conditions": "逻辑失效条件",
    "leading_indicators": "领先指标",
    "unresolved_questions": "未解决问题",
}

SECTION_LABELS_EN = {
    "executive_summary": "Executive Summary",
    "claims": "Key Claims",
    "business_model": "Business Model",
    "financial_quality": "Financial Quality",
    "balance_sheet": "Balance Sheet",
    "competitive_position": "Competitive Position",
    "growth_opportunities": "Growth Opportunities",
    "counterarguments": "Counterarguments",
    "scenarios": "Long-term Operating Scenarios",
    "implied_expectations": "Current Valuation Implied Expectations",
    "thesis": "Investment Thesis",
    "invalidation_conditions": "Thesis Invalidation Conditions",
    "leading_indicators": "Leading Indicators",
    "unresolved_questions": "Unresolved Questions",
}

# Backward-compatible export used by integrations.
SECTION_LABELS = SECTION_LABELS_ZH
SECTION_LABELS_HANT = {
    "executive_summary": "\u57f7\u884c\u6458\u8981", "claims": "\u4e3b\u8981\u7d50\u8ad6", "business_model": "\u5546\u696d\u6a21\u5f0f",
    "financial_quality": "\u8ca1\u52d9\u54c1\u8cea", "balance_sheet": "\u8cc7\u7522\u8ca0\u50b5\u8868", "competitive_position": "\u7af6\u722d\u5730\u4f4d",
    "growth_opportunities": "\u589e\u9577\u6a5f\u6703", "counterarguments": "\u53cd\u65b9\u89c0\u9ede", "scenarios": "\u9577\u671f\u7d93\u71df\u60c5\u5883",
    "implied_expectations": "\u4f30\u503c\u96b1\u542b\u9810\u671f", "thesis": "\u6295\u8cc7\u908f\u8f2f", "invalidation_conditions": "\u908f\u8f2f\u5931\u6548\u689d\u4ef6",
    "leading_indicators": "\u9818\u5148\u6307\u6a19", "unresolved_questions": "\u672a\u89e3\u6c7a\u554f\u984c",
}

ARTIFACT_LABELS = {
    "deterministic-financial-summary": (
        "确定性财务概览",
        "Deterministic Financial Overview",
    ),
    "deterministic-valuation": (
        "反向 DCF 隐含预期",
        "Reverse DCF Implied Expectations",
    ),
    "verified-research-dossier": (
        "经过验证的研究档案",
        "Verified Research Dossier",
    ),
    "growth-opportunities": ("增长机会", "Growth Opportunities"),
    "counter-analysis": ("反方审查", "Counter-analysis"),
    "forecast-scenarios": ("长期经营情景", "Long-term Operating Scenarios"),
    "research-report": ("长期研究报告", "Long-term Research Report"),
    "model-comparison": ("双模型研究分歧", "Two-model Research Differences"),
    "thesis-snapshot": ("投资逻辑快照", "Investment Thesis Snapshot"),
}


def _pick(language: str, chinese: str, english: str) -> str:
    locale = normalize_language(language)
    if locale == EN:
        return english
    if locale == ZH_HANT:
        prefix = chinese[: len(chinese) - len(chinese.lstrip("# >-"))]
        return prefix + UI_HANT.get(chinese[len(prefix):], chinese[len(prefix):])
    return chinese


def _locale_text(language: str, simplified: str, traditional: str, english: str) -> str:
    """Select program-authored copy without translating model-authored prose."""

    locale = normalize_language(language)
    if locale == EN:
        return english
    if locale == ZH_HANT:
        return traditional
    return simplified


def staged_context_capacity_notice(
    report: object,
    language: str,
) -> dict[str, str] | None:
    """Return safe, localized copy for a capacity-limited staged report."""

    if not isinstance(report, dict) or report.get("cross_section_synthesis_status") != "not_completed_context_capacity":
        return None

    budget_data = report.get("context_budget")
    budget_data = budget_data if isinstance(budget_data, dict) else {}
    required = budget_data.get("required_bytes")
    available = budget_data.get("available_bytes")

    def format_bytes(value: object) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _locale_text(language, "不可用", "無法取得", "unavailable")
        amount = int(value)
        kib = amount / 1024
        return f"{amount:,} bytes ({kib:,.1f} KiB)"

    budget = _locale_text(
        language,
        f"模型输入预算：需要 {format_bytes(required)}；可用 {format_bytes(available)}。",
        f"模型輸入預算：需要 {format_bytes(required)}；可用 {format_bytes(available)}。",
        f"Model input budget: required {format_bytes(required)}; available {format_bytes(available)}.",
    )
    mode = budget_data.get("counting_mode")
    if mode == "conservative_utf8_byte_upper_bound":
        counting = _locale_text(
            language,
            "输入大小按 UTF-8 字节保守上界估算，并非官方精确 token 计数。",
            "輸入大小按 UTF-8 位元組保守上界估算，並非官方精確 token 計數。",
            "Input size uses a conservative UTF-8 byte upper bound estimate, not an official exact token count.",
        )
    elif mode == "explicit_max_input_bytes":
        counting = _locale_text(
            language,
            "可用容量来自提供方明确的最大输入字节预算。",
            "可用容量來自提供方明確的最大輸入位元組預算。",
            "Available capacity comes from the provider's explicit maximum input-byte budget.",
        )
    else:
        counting = _locale_text(
            language,
            "当前模型输入容量不足，未对内容作截断或猜测。",
            "目前模型輸入容量不足，未對內容作截斷或猜測。",
            "The model input capacity was insufficient; no content was truncated or guessed.",
        )

    return {
        "title": _locale_text(language, "最终综合容量状态", "最終綜合容量狀態", "Final synthesis capacity"),
        "summary": _locale_text(
            language,
            "研究阶段数据已完整保留，但跨章节最终综合因当前模型容量不足未完成。",
            "研究階段資料已完整保留，但跨章節最終綜合因目前模型容量不足未完成。",
            "All completed research-stage data was preserved, but cross-section final synthesis was not completed because the model context capacity was insufficient.",
        ),
        "budget": budget,
        "counting": counting,
        "retry": _locale_text(
            language,
            "可选择支持更大上下文的模型，然后仅重试“综合”；无需重新下载财报或重跑其他研究阶段。",
            "可選擇支援更大上下文的模型，然後僅重試「綜合」；無需重新下載財報或重跑其他研究階段。",
            "You can choose a model with a larger context window and retry only synthesis; financial filings and other research stages do not need to run again.",
        ),
    }


def _render_value(value: Any, language: str = "zh-CN", level: int = 0) -> list[str]:
    english = normalize_language(language) == EN
    labels = SECTION_LABELS_EN if english else SECTION_LABELS_HANT if normalize_language(language) == ZH_HANT else SECTION_LABELS_ZH
    if value is None:
        return [_locale_text(language, "证据不足或尚未提供。", "證據不足或尚未提供。", "Insufficient evidence or not provided.")]
    if isinstance(value, str):
        return [report_display_value(value, language)]
    if isinstance(value, bool):
        return [_locale_text(language, "是" if value else "否", "是" if value else "否", "Yes" if value else "No")]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        if not value:
            return [_locale_text(language, "暂无。", "暫無。", "None.")]
        lines: list[str] = []
        for item in value:
            rendered = _render_value(item, language, level + 1)
            lines.append(f"- {rendered[0]}")
            lines.extend(f"  {line}" for line in rendered[1:])
        return lines
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if str(key).startswith("_"):
                continue
            label = labels.get(str(key))
            if label is None and str(key) in GROWTH_FIELD_LABELS:
                label = growth_field_label(str(key), language)
            if label is None:
                label = report_field_label(key, language)
            rendered = _render_value(item, language, level + 1)
            if isinstance(item, (dict, list)):
                lines.append(f"**{label}**")
                lines.extend(rendered)
            else:
                separator = ": " if english else "："
                lines.append(f"- **{label}{separator}** {rendered[0]}")
        return lines
    return [str(value)]


def _render_claims(value: object, language: str) -> list[str]:
    english = normalize_language(language) == EN
    claims = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    if not claims:
        return _render_value(value, language)
    grouped: dict[float | None, list[dict[str, Any]]] = {}
    for claim in claims:
        raw_confidence = claim.get("confidence")
        if isinstance(raw_confidence, bool):
            confidence = None
        else:
            try:
                confidence = max(0.0, min(1.0, float(raw_confidence)))
            except (TypeError, ValueError):
                confidence = None
        grouped.setdefault(confidence, []).append(claim)
    lines: list[str] = []
    for confidence in sorted(
        grouped,
        key=lambda item: (item is None, 0.0 if item is None else -item),
    ):
        if confidence is None:
            heading = _locale_text(language, "置信度未提供", "信心程度未提供", "Confidence not provided")
        elif confidence >= 0.8:
            heading = _locale_text(language, "高置信度", "高信心程度", "High confidence")
        elif confidence >= 0.55:
            heading = _locale_text(language, "中等置信度", "中等信心程度", "Medium confidence")
        else:
            heading = _locale_text(language, "低置信度", "低信心程度", "Low confidence")
        score = "" if confidence is None else f" · {confidence:.2f}"
        count = _locale_text(
            language,
            f" · {len(grouped[confidence])} 条",
            f" · {len(grouped[confidence])} 條",
            f" · {len(grouped[confidence])} items",
        )
        lines.extend([f"### {heading}{score}{count}", ""])
        for claim in grouped[confidence]:
            text = claim.get("text") or claim.get("conclusion") or claim.get("argument")
            if not text:
                continue
            kind = report_display_value(str(claim.get("kind", "inference")), language)
            lines.append(f"- **{kind}** · {text}")
        lines.append("")
    return lines


def _render_growth_opportunities(
    value: object,
    language: str,
    *,
    include_technical: bool = False,
    available_evidence: set[str] | None = None,
    counts_projected: bool = False,
) -> list[str]:
    english = normalize_language(language) == EN
    if isinstance(value, dict) and isinstance(value.get("opportunities"), list):
        opportunities = list(value["opportunities"])
    elif isinstance(value, list):
        opportunities = list(value)
    else:
        opportunities = growth_opportunities_from_value(value, language)
    if not opportunities:
        response_error = value.get("_response_error") if isinstance(value, dict) else None
        validation = value.get("_validation") if isinstance(value, dict) else None
        if response_error in {"empty_content", "invalid_json", "invalid_shape"}:
            return [
                _locale_text(
                    language,
                    "增长机会模型未返回有效内容，可单独重试该阶段。",
                    "增長機會模型未返回有效內容，可單獨重試該階段。",
                    "The growth-opportunity model returned no usable content. You can retry only this stage.",
                )
            ]
        if isinstance(validation, dict) and validation.get("passed") is False:
            return [
                _locale_text(
                    language,
                    "增长机会输出未通过结构或证据校验，可单独重试该阶段。",
                    "增長機會輸出未通過結構或證據校驗，可單獨重試該階段。",
                    "The growth-opportunity output did not pass structure or evidence validation. You can retry only this stage.",
                )
            ]
        return [
            _locale_text(
                language,
                "当前证据不足，未形成可展示的增长机会。",
                "目前證據不足，未形成可展示的增長機會。",
                "Current evidence is insufficient to present a growth opportunity.",
            )
        ]
    lines: list[str] = []
    for opportunity in opportunities:
        title = opportunity.get("title") or _locale_text(
            language, "未命名机会", "未命名機會", "Unnamed opportunity"
        )
        lines.extend([f"### {title}", ""])
        badges = [
            evidence_grade_label(opportunity.get("evidence_grade"), language)
        ]
        if opportunity.get("category"):
            badges.append(str(opportunity["category"]))
        if opportunity.get("maturity_stage"):
            badges.append(str(opportunity["maturity_stage"]))
        lines.extend([" · ".join(f"`{item}`" for item in badges), ""])
        probability = format_probability_range(
            opportunity.get("probability_range"), language
        )
        horizon = opportunity.get("time_horizon_years")
        horizon_text = (
            _locale_text(language, f"{horizon} 年", f"{horizon} 年", f"{horizon} years")
            if horizon
            else _locale_text(language, "证据不足", "證據不足", "Insufficient evidence")
        )
        lines.extend(
            [
                _locale_text(language, f"- 可能性：{probability}", f"- 可能性：{probability}", f"- Probability: {probability}"),
                _locale_text(language, f"- 时间跨度：{horizon_text}", f"- 時間跨度：{horizon_text}", f"- Time horizon: {horizon_text}"),
            ]
        )
        scenarios = [
            scenario_label(item, language)
            for item in opportunity.get("scenario_eligibility", [])
        ]
        if scenarios:
            separator = ", " if english else "、"
            lines.append(
                _locale_text(language, "- 适用情景：", "- 適用情境：", "- Eligible scenarios: ")
                + separator.join(scenarios)
            )
        lines.extend(
            [
                "",
                _locale_text(language, "**增长机制**", "**增長機制**", "**Growth mechanism**"),
                "",
                str(opportunity.get("mechanism") or _locale_text(
                    language, "证据不足。", "證據不足。", "Insufficient evidence."
                )),
                "",
            ]
        )
        supporting = opportunity.get("supporting_evidence_ids", [])
        contradicting = opportunity.get("contradicting_evidence_ids", [])
        if available_evidence is not None and (
            "supporting_evidence_ids" in opportunity
            or "contradicting_evidence_ids" in opportunity
        ):
            supporting_count = len(
                {str(item).strip() for item in supporting if str(item).strip() in available_evidence}
            )
            contradicting_count = len(
                {str(item).strip() for item in contradicting if str(item).strip() in available_evidence}
            )
        elif available_evidence is not None:
            supporting_count = opportunity.get("supporting_evidence_count", 0) if counts_projected else 0
            contradicting_count = opportunity.get("contradicting_evidence_count", 0) if counts_projected else 0
        else:
            supporting_count = opportunity.get("supporting_evidence_count", 0)
            contradicting_count = opportunity.get("contradicting_evidence_count", 0)
            supporting_count = supporting_count if isinstance(supporting_count, int) else 0
            contradicting_count = contradicting_count if isinstance(contradicting_count, int) else 0
        lines.append(
            (
                format_evidence_summary(
                    supporting_count, contradicting_count, language
                )
            )
        )
        if opportunity.get("leading_indicators"):
            lines.extend(
                [
                    "",
                    _locale_text(language, "**领先指标**", "**領先指標**", "**Leading indicators**"),
                    *[
                        f"- {item}"
                        for item in opportunity["leading_indicators"]
                    ],
                ]
            )
        if opportunity.get("invalidation_conditions"):
            lines.extend(
                [
                    "",
                    _locale_text(language, "**失效条件**", "**失效條件**", "**Invalidation conditions**"),
                    *[
                        f"- {item}"
                        for item in opportunity["invalidation_conditions"]
                    ],
                ]
            )
        if include_technical:
            lines.extend(
                [
                    "",
                    _locale_text(
                        language,
                        f"> 机会 ID：`{opportunity.get('opportunity_id', '')}`",
                        f"> 機會 ID：`{opportunity.get('opportunity_id', '')}`",
                        f"> Opportunity ID: `{opportunity.get('opportunity_id', '')}`",
                    ),
                    _locale_text(language, "> 支持证据 ID：", "> 支援證據 ID：", "> Supporting evidence IDs: ")
                    + (
                        ", ".join(map(str, supporting))
                        if supporting
                        else "—"
                    ),
                    _locale_text(language, "> 相反证据 ID：", "> 相反證據 ID：", "> Contradicting evidence IDs: ")
                    + (
                        ", ".join(map(str, contradicting))
                        if contradicting
                        else "—"
                    ),
                ]
            )
        lines.append("")
    return lines


def _artifact_title(artifact: dict[str, Any], language: str) -> str:
    labels = ARTIFACT_LABELS.get(str(artifact.get("artifact_type", "")))
    if labels:
        return _pick(language, labels[0], labels[1])
    return str(artifact.get("title", ""))


def render_research_run(
    run_id: str,
    artifacts: list[dict[str, Any]],
    language: str = "zh-CN",
    *,
    company_name: str = "",
    include_technical: bool = False,
) -> str:
    language = normalize_language(language)
    english = language == EN
    traditional = language == ZH_HANT
    section_labels = SECTION_LABELS_EN if english else SECTION_LABELS_HANT if language == ZH_HANT else SECTION_LABELS_ZH
    lines = [
        _pick(language, "# OpenThesis 长期公司研究", "# OpenThesis Long-term Company Research"),
        "",
        _pick(language, f"研究运行：`{run_id}`", f"Research run: `{run_id}`"),
        "",
        _pick(
            language,
            "> 本报告用于研究辅助，不构成投资建议或交易指令。",
            "> This report is research assistance, not investment advice or a trade instruction.",
        ),
        "",
    ]
    deterministic = next(
        (
            artifact
            for artifact in reversed(artifacts)
            if artifact["artifact_type"] == "deterministic-financial-summary"
        ),
        None,
    )
    final = next(
        (
            artifact
            for artifact in reversed(artifacts)
            if artifact["artifact_type"] == "research-report"
        ),
        None,
    )
    growth_artifact = next(
        (
            artifact
            for artifact in reversed(artifacts)
            if artifact["artifact_type"] == "growth-opportunities"
        ),
        None,
    )
    growth_rendered = False
    available_evidence = {
        str(item.get("evidence_id"))
        for item in (
            deterministic.get("content", {}).get("evidence", [])
            if deterministic else []
        )
        if isinstance(item, dict) and item.get("evidence_id")
    }
    if deterministic:
        metrics = deterministic["content"].get("metrics")
        if company_name and isinstance(metrics, list):
            lines.extend(
                [
                    deterministic_summary(
                        company_name,
                        metrics,
                        language,
                        str(deterministic["content"].get("currency", "USD")),
                    ),
                    "",
                ]
            )
            content = deterministic.get("content", {})
            interim_metrics = content.get("interim_metrics")
            latest_interim = (
                next((item for item in interim_metrics if isinstance(item, dict)), None)
                if isinstance(interim_metrics, list)
                else None
            )
            if latest_interim:
                period = f"{latest_interim.get('year', '')} {latest_interim.get('period', '')}".strip()
                comparison = str(latest_interim.get("comparison_period") or "")
                comparison_gap = str(latest_interim.get("comparison_gap") or "")
                currency = str(content.get("currency", "USD"))
                lines.extend(
                    [
                        _pick(language, "## 最新季度及中期数据", "## Latest Interim Results"),
                        "",
                        _pick(
                            language,
                            f"> {period} 为累计期间数据，不与完整财年混算。",
                            f"> {period} is a cumulative interim period and is not mixed with full-year totals.",
                        ),
                        "",
                        _pick(
                            language,
                            f"- 营业收入：{format_money(latest_interim.get('revenue'), currency)}",
                            f"- Revenue: {format_money(latest_interim.get('revenue'), currency)}",
                        ),
                        _pick(
                            language,
                            (
                                f"- 同期收入增长：{format_percent(latest_interim.get('revenue_growth'))}"
                                + (f"（对比 {comparison}）" if comparison else "")
                                if not comparison_gap
                                else "- 同期收入增长：缺少或未通过校验的上年同期披露"
                            ),
                            (
                                f"- Revenue growth: {format_percent(latest_interim.get('revenue_growth'))}"
                                + (f" (versus {comparison})" if comparison else "")
                                if not comparison_gap
                                else "- Revenue growth: prior-year comparable disclosure is missing or did not pass validation"
                            ),
                        ),
                        _pick(
                            language,
                            f"- 净利润：{format_money(latest_interim.get('net_income'), currency)}",
                            f"- Net income: {format_money(latest_interim.get('net_income'), currency)}",
                        ),
                        _pick(
                            language,
                            f"- 经营现金流：{format_money(latest_interim.get('operating_cash_flow'), currency)}",
                            f"- Operating cash flow: {format_money(latest_interim.get('operating_cash_flow'), currency)}",
                        ),
                        "",
                    ]
                )
            quality = content.get("financial_quality")
            rejected = quality.get("rejected_periods", []) if isinstance(quality, dict) else []
            continuity = quality.get("period_continuity", []) if isinstance(quality, dict) else []
            has_continuity_gap = bool(rejected) or any(
                isinstance(item, dict) and item.get("status") != "accepted"
                for item in continuity
            )
            if has_continuity_gap:
                lines.extend([
                    _pick(language, "## 年度数据连续性", "## Annual Data Continuity"),
                    "",
                    _pick(
                        language,
                        "部分年度数据校验未通过，已隔离且未用于计算或模型分析。",
                        "Some annual data failed validation and was quarantined; it was not used for metrics or model analysis.",
                    ),
                    "",
                ])
            coverage = quality.get("period_coverage") if isinstance(quality, dict) else None
            if isinstance(coverage, dict):
                available_years = [
                    str(year) for year in coverage.get("displayed_annual_years", [])
                    if year is not None
                ]
                hidden_years = [
                    str(year) for year in coverage.get("hidden_comparator_years", [])
                    if year is not None
                ]
                missing = coverage.get("missing_or_rejected_years", [])
                missing_years = [
                    str(item.get("year")) for item in missing
                    if isinstance(item, dict) and item.get("year") is not None
                ]
                missing_reason_code = str(
                    coverage.get("missing_reason_code") or ""
                ).strip()
                coverage_title = (
                    "年度披露範圍" if traditional
                    else "Annual disclosure coverage" if english
                    else "年度披露范围"
                )
                coverage_lines = [f"## {coverage_title}", ""]
                requested_range = coverage.get("requested_annual_range")
                if isinstance(requested_range, (list, tuple)) and requested_range:
                    coverage_lines.append(
                        (
                            "- 要求的年度範圍：" if traditional
                            else "- Requested annual range: " if english
                            else "- 要求的年度范围："
                        ) + " – ".join(str(item) for item in requested_range)
                    )
                if available_years:
                    coverage_lines.append(
                        (
                            "- 實際採用的年度：" if traditional
                            else "- Annual years actually used: " if english
                            else "- 实际采用的年度："
                        ) + ", ".join(available_years)
                    )
                if hidden_years:
                    coverage_lines.append(
                        (
                            "- 隱藏比較年度：" if traditional
                            else "- Hidden comparator years: " if english
                            else "- 隐藏比较年度："
                        ) + ", ".join(hidden_years)
                    )
                latest_fy = coverage.get("latest_official_fy")
                if latest_fy is not None:
                    coverage_lines.append(
                        (
                            "- 最新有效官方財年：" if traditional
                            else "- Latest available official FY: " if english
                            else "- 最新有效官方财年："
                        ) + str(latest_fy)
                    )
                interim = [str(item) for item in coverage.get("interim_periods", []) if item]
                if interim:
                    coverage_lines.append(
                        (
                            "- 中期期間（不替代完整財年）：" if traditional
                            else "- Interim periods (not a substitute for FY): " if english
                            else "- 中期期间（不替代完整财年）："
                        ) + ", ".join(interim)
                    )
                if missing_reason_code or missing:
                    localized_missing = (
                        "截至研究日未取得有效官方年報"
                        if traditional
                        else "No valid official annual report was available as of the research date."
                        if english
                        else "截至研究日未获取到有效官方年报"
                    )
                    if missing_years:
                        localized_missing += (
                            ("（年度：" if traditional else " (years: " if english else "（年度：")
                            + ", ".join(missing_years)
                            + (")" if english else "）")
                        )
                    coverage_lines.append(
                        "- " + localized_missing
                    )
                if coverage.get("research_as_of"):
                    coverage_lines.append(
                        (
                            "- 研究截至：" if traditional
                            else "- Research as of: " if english
                            else "- 研究截至："
                        ) + str(coverage["research_as_of"])
                    )
                lines.extend(coverage_lines + [""])
            snapshot = content.get("market_snapshot")
            if isinstance(snapshot, dict):
                values: list[str] = []
                quote_currency = str(snapshot.get("quote_currency") or snapshot.get("currency", ""))
                valuation_currency = str(snapshot.get("valuation_currency") or snapshot.get("reporting_currency") or quote_currency)
                manual_snapshot = str(snapshot.get("source", "")).casefold() == "manual" or str(snapshot.get("status", "")).upper() == "MANUAL"
                price_label = ("Manual price" if manual_snapshot else "Price") if english else ("手動價格" if traditional and manual_snapshot else "價格" if traditional else "手动价格" if manual_snapshot else "价格")
                cap_label = ("Manual market cap" if manual_snapshot else "Market cap") if english else ("手動市值" if traditional and manual_snapshot else "市值" if traditional else "手动市值" if manual_snapshot else "市值")
                if isinstance(snapshot.get("price"), (int, float)):
                    values.append(
                        price_label
                        + f": {quote_currency} {snapshot['price']:,.2f}"
                    )
                if isinstance(snapshot.get("market_cap"), (int, float)):
                    values.append(
                        cap_label
                        + f": {quote_currency} {snapshot['market_cap']:,.0f}"
                    )
                if values:
                    source = str(snapshot.get("source") or snapshot.get("provider") or "—")
                    lines.extend(
                        [
                            "> "
                            + "；".join(values)
                            + (
                                f"。來源：{source}；日期：{snapshot.get('as_of', '—')}。"
                                if language == ZH_HANT
                                else f". Source: {source}; as of {snapshot.get('as_of', '—')}."
                                if language == EN
                                else f"。来源：{source}；日期：{snapshot.get('as_of', '—')}；估值币种：{valuation_currency}。"
                            ),
                            "",
                        ]
                    )
            if content.get("industry_support") == "financial_beta":
                lines.extend(
                    [
                        (
                            "> 金融機構研究為 Beta，標準自由現金流反向 DCF 不適用。"
                            if language == ZH_HANT
                            else "> Financial-institution research is Beta; standard free-cash-flow reverse DCF is not applicable."
                            if language == EN
                            else "> 金融机构研究为 Beta，标准自由现金流反向 DCF 不适用。"
                        ),
                        "",
                    ]
                )
        else:
            markdown = deterministic["content"].get("markdown")
            if markdown:
                lines.extend([str(markdown), ""])

    valuation = next(
        (
            artifact
            for artifact in artifacts
            if artifact["artifact_type"] == "deterministic-valuation"
        ),
        None,
    )
    if valuation:
        value = valuation["content"]
        lines.extend(
            [
                _pick(
                    language,
                    "## 反向 DCF 隐含预期",
                    "## Reverse DCF Implied Expectations",
                ),
                "",
            ]
        )
        if value.get("status") == "ok":
            currency = str(value.get("currency", "USD"))
            snapshot = value.get("market_snapshot") if isinstance(value.get("market_snapshot"), dict) else {}
            source = str(snapshot.get("source") or value.get("source") or "—")
            as_of = str(snapshot.get("as_of") or value.get("market_as_of") or "—")
            period = str(value.get("base_fcf_period") or "—")
            horizon = int(value.get("horizon_years", 5))
            lines.extend(
                (
                    [
                        f"- Equity market value ({currency}): {format_money(value['equity_market_value'], currency)}",
                        f"- FCFE proxy ({period}): {format_money(value['base_free_cash_flow'], currency)}",
                        f"- Implied FCFE growth for the first {horizon} years: {value['implied_fcf_growth'] * 100:.1f}%",
                        f"- Discount rate: {value['discount_rate'] * 100:.1f}%",
                        f"- Terminal growth rate: {value['terminal_growth'] * 100:.1f}%",
                        f"- Market snapshot: {source}; as of {as_of}",
                    ]
                    if english
                    else [
                        f"- {'權益市值' if traditional else '权益市值'}（{currency}）：{format_money(value['equity_market_value'], currency)}",
                        f"- {'FCFE 近似' if traditional else 'FCFE 近似'}（{period}）：{format_money(value['base_free_cash_flow'], currency)}",
                        f"- {'前' if traditional else '前'} {horizon} {'年隱含 FCFE 增速' if traditional else '年隐含 FCFE 增速'}：{value['implied_fcf_growth'] * 100:.1f}%",
                        f"- {'折現率' if traditional else '折现率'}：{value['discount_rate'] * 100:.1f}%",
                        f"- {'永續增長率' if traditional else '永续增长率'}：{value['terminal_growth'] * 100:.1f}%",
                        f"- {'行情快照' if traditional else '行情快照'}：{source}；{'截至' if traditional else '截至'} {as_of}",
                    ]
                )
            )
        else:
            status_reason = reverse_dcf_status_text(value.get("status"), language)
            prefix = (
                "- Implied growth could not be calculated: "
                if english
                else "- 無法給出隱含增速："
                if traditional
                else "- 无法给出隐含增速："
            )
            lines.append(prefix + status_reason)
        limitations = (
            [
                "Equity market value is matched to an FCFE proxy; this is not an enterprise-value model.",
                "The model assumes the FCFE proxy grows at a constant rate during the explicit forecast period.",
                "This result explains market-implied expectations; it is not a price target.",
            ]
            if english
            else [
                "權益市值與 FCFE 近似口徑一致；這不是企業價值模型。" if traditional else "权益市值与 FCFE 近似口径一致；这不是企业价值模型。",
                "模型假設顯性預測期內 FCFE 近似按固定速度增長。" if traditional else "模型假设显性预测期内 FCFE 近似按固定速度增长。",
                reverse_dcf_disclaimer(language),
            ]
        )
        prefix = "- Limitation: " if english else "- 限制："
        lines.extend(prefix + item for item in limitations)
        lines.append("")

    if final:
        content = final["content"]
        if content.get("mode") == "deterministic-only":
            notice = _pick(
                language,
                "未配置模型，因此没有生成定性研究、增长机会和长期情景。",
                "No model was configured, so qualitative research, growth "
                "opportunities, and long-term scenarios were not generated.",
            )
            lines.extend(
                [
                    _pick(language, "## AI 研究状态", "## AI Research Status"),
                    "",
                    notice,
                    "",
                ]
            )
        else:
            if content.get("mode") == "financial-refresh":
                lines.extend([
                    _pick(language, "## 财务刷新状态", "## Financial Refresh Status"),
                    "",
                    _pick(
                        language,
                        "> 财务表格与确定性指标已在不调用模型的情况下更新；下方定性研究保留自原综合结果，若关键数字发生变化，请重新生成综合报告。",
                        "> Financial tables and deterministic metrics were refreshed without a model call. Qualitative research below is retained from the prior synthesis; regenerate synthesis if material figures changed.",
                    ),
                    "",
                ])
            if content.get("mode") == "staged-fallback":
                capacity_notice = staged_context_capacity_notice(content.get("report"), language)
                if capacity_notice:
                    lines.extend(
                        [
                            f"## {capacity_notice['title']}",
                            "",
                            f"> {capacity_notice['summary']}",
                            f"> {capacity_notice['budget']}",
                            f"> {capacity_notice['counting']}",
                            f"> {capacity_notice['retry']}",
                            "",
                        ]
                    )
            report = content.get("report", content)
            if isinstance(report, dict):
                display_report = normalize_report_sections(report, language)
                for key in section_labels:
                    if key not in display_report:
                        continue
                    projected = project_report_value(
                        display_report[key],
                        include_technical=include_technical,
                        section=key,
                        available_evidence=available_evidence,
                    )
                    rendered = (
                        _render_growth_opportunities(
                            projected,
                            language,
                            include_technical=include_technical,
                            available_evidence=available_evidence,
                            counts_projected=True,
                        )
                        if key == "growth_opportunities"
                        else _render_claims(
                            projected,
                            language,
                        )
                        if key == "claims"
                        else _render_value(
                            projected,
                            language,
                        )
                    )
                    if key == "growth_opportunities":
                        if not growth_opportunities_from_value(projected, language):
                            rendered = _render_value(projected, language)
                        else:
                            growth_rendered = True
                    lines.extend(
                        [
                            f"## {section_labels[key]}",
                            "",
                            *rendered,
                            "",
                        ]
                    )
            verification = content.get("verification")
            if verification:
                passed = bool(verification.get("passed"))
                lines.extend(
                    (
                        [
                            "## Verification Results",
                            "",
                            f"- Passed: {'Yes' if passed else 'No'}",
                            f"- Claims: {verification.get('claim_count', 0)}",
                            f"- Verified claims: {verification.get('verified_claim_count', 0)}",
                            f"- Unsupported facts: {verification.get('unsupported_fact_count', 0)}",
                        ]
                        if english
                        else [
                            "## 验证结果",
                            "",
                            f"- 是否通过：{'是' if passed else '否'}",
                            f"- 结论数量：{verification.get('claim_count', 0)}",
                            f"- 已验证结论：{verification.get('verified_claim_count', 0)}",
                            f"- 无证据事实：{verification.get('unsupported_fact_count', 0)}",
                        ]
                    )
                )
                if include_technical:
                    issue_prefix = "- Issue: " if english else "- 问题："
                    for issue in verification.get("issues", []):
                        lines.append(issue_prefix + str(issue))
                elif not passed:
                    lines.append(
                        "- Some conclusions need more evidence or a new synthesis."
                        if english
                        else "- 部分结论仍需补充证据或重新生成综合报告。"
                    )
                lines.append("")

    if growth_artifact and not growth_rendered:
        growth_content = growth_artifact.get("content")
        growth_value = project_report_value(
            growth_content,
            include_technical=include_technical,
            section="growth_opportunities",
            available_evidence=available_evidence,
        )
        # Keep machine-readable failure metadata available to the typed
        # renderer without projecting private protocol keys into the report.
        if isinstance(growth_content, dict) and isinstance(growth_value, dict):
            for metadata_key in ("_response_error", "_validation"):
                if metadata_key in growth_content:
                    growth_value[metadata_key] = growth_content[metadata_key]
        lines.extend(
            [
                _pick(language, "## 增长机会", "## Growth Opportunities"),
                "",
                *_render_growth_opportunities(
                    growth_value,
                    language,
                    include_technical=include_technical,
                    available_evidence=available_evidence,
                    counts_projected=True,
                ),
                "",
            ]
        )

    comparison = next(
        (
            artifact
            for artifact in artifacts
            if artifact["artifact_type"] == "model-comparison"
        ),
        None,
    )
    if comparison:
        value = comparison["content"]
        primary = value["primary"]
        secondary = value["secondary"]
        lines.extend(
            (
                [
                    "## Two-model Comparison",
                    "",
                    f"- Primary model: `{primary['provider']}:{primary['model']}`",
                    f"- Comparison model: `{secondary['provider']}:{secondary['model']}`",
                    f"- Identical claims: {len(value['common_claims'])}",
                    f"- Primary-model-only claims: {len(value['primary_only_claims'])}",
                    f"- Comparison-model-only claims: {len(value['secondary_only_claims'])}",
                    "",
                ]
                if english
                else [
                    "## 双模型比较",
                    "",
                    f"- 主模型：`{primary['provider']}:{primary['model']}`",
                    f"- 对比模型：`{secondary['provider']}:{secondary['model']}`",
                    f"- 完全相同的结论：{len(value['common_claims'])}",
                    f"- 仅主模型提出：{len(value['primary_only_claims'])}",
                    f"- 仅对比模型提出：{len(value['secondary_only_claims'])}",
                    "",
                ]
            )
        )
        groups = (
            (
                "### Common Claims",
                value["common_claims"],
            ),
            (
                "### Primary Model Only",
                value["primary_only_claims"],
            ),
            (
                "### Comparison Model Only",
                value["secondary_only_claims"],
            ),
        ) if english else (
            ("### 共同结论", value["common_claims"]),
            ("### 仅主模型提出", value["primary_only_claims"]),
            ("### 仅对比模型提出", value["secondary_only_claims"]),
        )
        for heading, items in groups:
            if items:
                lines.append(heading)
                lines.extend(f"- {item}" for item in items)
                lines.append("")
        different_sections = [
            item for item in value["section_comparison"] if not item["same"]
        ]
        section_text = ", ".join(item["section"] for item in different_sections)
        lines.append(
            (
                "Sections with structured differences: " + (section_text or "None")
                if english
                else "存在结构化差异的章节：" + (section_text or "无")
            )
        )
        method = _pick(
            language,
            "确定性结构比较；相同文本不代表事实正确，文本不同也不自动代表实质冲突。",
            "Deterministic structural comparison: identical text does not prove "
            "factual correctness, and different text does not automatically "
            "indicate a substantive conflict.",
        )
        lines.extend(["", f"> {method}", ""])

    evidence = (
        deterministic.get("content", {}).get("evidence", []) if deterministic else []
    )
    unique_sources: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str]] = set()
    for item in evidence:
        url = str(item.get("source_url", "")).strip()
        if not url:
            continue
        evidence_id = str(item.get("evidence_id", ""))
        identity = (evidence_id, url)
        if identity in seen_sources:
            continue
        seen_sources.add(identity)
        unique_sources.append(item)
    if unique_sources:
        lines.extend(
            [
                _pick(language, "## 证据来源", "## Evidence Sources"),
                "",
            ]
        )
        for item in unique_sources[:40]:
            title = (
                item.get("title")
                or item.get("concept")
                or item.get("evidence_id")
                or ("Source" if english else "来源")
            )
            locator = item.get("locator")
            suffix = f" ({locator})" if locator and english else (f"（{locator}）" if locator else "")
            lines.append(f"- {title}{suffix}")
            lines.append(f"  {item['source_url']}")
        lines.append("")

    lines.extend([_pick(language, "## 研究过程", "## Research Process"), ""])
    for artifact in artifacts:
        lines.append(
            f"- {_artifact_title(artifact, language)} · "
            f"`{artifact['agent_id']}` · `{artifact['model_id']}`"
        )
    lines.extend(
        [
            "",
            _pick(language, "## 方法说明", "## Methodology"),
            "",
            _pick(
                language,
                "财务数值来自结构化事实并由确定性程序计算。模型生成内容必须与证据、"
                "假设和未知项区分；最终投资判断由用户自行作出。",
                "Financial values come from structured facts and deterministic "
                "calculations. Model-generated content must distinguish evidence, "
                "assumptions, and information gaps. The user makes the final investment judgment.",
            ),
        ]
    )
    return "\n".join(lines)
