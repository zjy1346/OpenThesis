from __future__ import annotations

from typing import Any

from .financials import deterministic_summary, format_money, format_percent
from .growth import (
    GROWTH_FIELD_LABELS,
    evidence_grade_label,
    format_probability_range,
    growth_field_label,
    growth_opportunities_from_value,
    scenario_label,
)
from .i18n import EN, normalize_language
from .report_projection import project_report_value, report_display_value, report_field_label


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
    return english if normalize_language(language) == EN else chinese


def _render_value(value: Any, language: str = "zh-CN", level: int = 0) -> list[str]:
    english = normalize_language(language) == EN
    labels = SECTION_LABELS_EN if english else SECTION_LABELS_ZH
    if value is None:
        return ["Insufficient evidence or not provided." if english else "证据不足或尚未提供。"]
    if isinstance(value, str):
        return [report_display_value(value, language)]
    if isinstance(value, bool):
        return ["Yes" if value else "No"] if english else ["是" if value else "否"]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        if not value:
            return ["None." if english else "暂无。"]
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
            heading = "Confidence not provided" if english else "置信度未提供"
        elif confidence >= 0.8:
            heading = "High confidence" if english else "高置信度"
        elif confidence >= 0.55:
            heading = "Medium confidence" if english else "中等置信度"
        else:
            heading = "Low confidence" if english else "低置信度"
        score = "" if confidence is None else f" · {confidence:.2f}"
        count = (
            f" · {len(grouped[confidence])} items"
            if english
            else f" · {len(grouped[confidence])} 条"
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
) -> list[str]:
    english = normalize_language(language) == EN
    opportunities = growth_opportunities_from_value(value, language)
    if not opportunities:
        response_error = value.get("_response_error") if isinstance(value, dict) else None
        validation = value.get("_validation") if isinstance(value, dict) else None
        if response_error in {"empty_content", "invalid_json", "invalid_shape"}:
            return [
                (
                    "The growth-opportunity model returned no usable content. You can retry only this stage."
                    if english
                    else "增长机会模型未返回有效内容，可单独重试该阶段。"
                )
            ]
        if isinstance(validation, dict) and validation.get("passed") is False:
            return [
                (
                    "The growth-opportunity output did not pass structure or evidence validation. You can retry only this stage."
                    if english
                    else "增长机会输出未通过结构或证据校验，可单独重试该阶段。"
                )
            ]
        return [
            (
                "Current evidence is insufficient to present a growth opportunity."
                if english
                else "当前证据不足，未形成可展示的增长机会。"
            )
        ]
    lines: list[str] = []
    for opportunity in opportunities:
        title = opportunity.get("title") or (
            "Unnamed opportunity" if english else "未命名机会"
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
            (f"{horizon} years" if english else f"{horizon} 年")
            if horizon
            else ("Insufficient evidence" if english else "证据不足")
        )
        lines.extend(
            [
                (
                    f"- Probability: {probability}"
                    if english
                    else f"- 可能性：{probability}"
                ),
                (
                    f"- Time horizon: {horizon_text}"
                    if english
                    else f"- 时间跨度：{horizon_text}"
                ),
            ]
        )
        scenarios = [
            scenario_label(item, language)
            for item in opportunity.get("scenario_eligibility", [])
        ]
        if scenarios:
            separator = ", " if english else "、"
            lines.append(
                (
                    "- Eligible scenarios: "
                    if english
                    else "- 适用情景："
                )
                + separator.join(scenarios)
            )
        lines.extend(
            [
                "",
                "**Growth mechanism**" if english else "**增长机制**",
                "",
                str(opportunity.get("mechanism") or (
                    "Insufficient evidence." if english else "证据不足。"
                )),
                "",
            ]
        )
        supporting = opportunity.get("supporting_evidence_ids", [])
        contradicting = opportunity.get("contradicting_evidence_ids", [])
        lines.append(
            (
                f"Evidence: {len(supporting)} supporting · {len(contradicting)} contradicting"
                if english
                else f"证据：{len(supporting)} 条支持 · {len(contradicting)} 条相反"
            )
        )
        if opportunity.get("leading_indicators"):
            lines.extend(
                [
                    "",
                    "**Leading indicators**" if english else "**领先指标**",
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
                    (
                        "**Invalidation conditions**"
                        if english
                        else "**失效条件**"
                    ),
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
                    (
                        f"> Opportunity ID: `{opportunity.get('opportunity_id', '')}`"
                        if english
                        else f"> 机会 ID：`{opportunity.get('opportunity_id', '')}`"
                    ),
                    (
                        "> Supporting evidence IDs: "
                        if english
                        else "> 支持证据 ID："
                    )
                    + (
                        ", ".join(map(str, supporting))
                        if supporting
                        else "—"
                    ),
                    (
                        "> Contradicting evidence IDs: "
                        if english
                        else "> 相反证据 ID："
                    )
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
    section_labels = SECTION_LABELS_EN if english else SECTION_LABELS_ZH
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
            for artifact in artifacts
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
            snapshot = content.get("market_snapshot")
            if isinstance(snapshot, dict):
                values: list[str] = []
                if isinstance(snapshot.get("price"), (int, float)):
                    values.append(
                        ("手动价格" if language != EN else "Manual price")
                        + f": {snapshot.get('currency', '')} {snapshot['price']:,.2f}"
                    )
                if isinstance(snapshot.get("market_cap"), (int, float)):
                    values.append(
                        ("手动市值" if language != EN else "Manual market cap")
                        + f": {snapshot.get('currency', '')} {snapshot['market_cap']:,.0f}"
                    )
                if values:
                    lines.extend(
                        [
                            "> "
                            + "；".join(values)
                            + (
                                f"。来源：用户手动输入；日期：{snapshot.get('as_of', '—')}。"
                                if language != EN
                                else f". Source: user-supplied; as of {snapshot.get('as_of', '—')}."
                            ),
                            "",
                        ]
                    )
            if content.get("industry_support") == "financial_beta":
                lines.extend(
                    [
                        (
                            "> 金融机构研究为 Beta，标准自由现金流反向 DCF 不适用。"
                            if language != EN
                            else "> Financial-institution research is Beta; standard free-cash-flow reverse DCF is not applicable."
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
            lines.extend(
                (
                    [
                        f"- Current market-cap input: ${value['market_cap'] / 1_000_000_000:,.2f} billion",
                        f"- Latest free cash flow: ${value['base_free_cash_flow'] / 1_000_000_000:,.2f} billion",
                        f"- Implied free-cash-flow growth for the first five years: {value['implied_fcf_growth'] * 100:.1f}%",
                        f"- Discount rate: {value['discount_rate'] * 100:.1f}%",
                        f"- Terminal growth rate: {value['terminal_growth'] * 100:.1f}%",
                    ]
                    if english
                    else [
                        f"- 当前市值输入：{value['market_cap'] / 1_000_000_000:,.2f} 十亿美元",
                        f"- 最新自由现金流：{value['base_free_cash_flow'] / 1_000_000_000:,.2f} 十亿美元",
                        f"- 前五年隐含自由现金流增速：{value['implied_fcf_growth'] * 100:.1f}%",
                        f"- 折现率：{value['discount_rate'] * 100:.1f}%",
                        f"- 永续增长率：{value['terminal_growth'] * 100:.1f}%",
                    ]
                )
            )
        else:
            reason = {
                "insufficient_data": (
                    "没有足够的正自由现金流数据。",
                    "There is not enough positive free-cash-flow data.",
                ),
                "outside_search_range": (
                    "隐含增速超出当前搜索范围。",
                    "The implied growth rate is outside the current search range.",
                ),
            }.get(str(value.get("status")), ("无法计算。", "Unable to calculate."))
            lines.append(
                _pick(
                    language,
                    f"- 无法给出隐含增速：{reason[0]}",
                    f"- Implied growth could not be calculated: {reason[1]}",
                )
            )
        limitations = (
            [
                "Market capitalization approximates enterprise value without a separate net-cash or net-debt adjustment.",
                "The model assumes free cash flow grows at a constant rate for the first five years.",
                "This result explains market-implied expectations; it is not a price target.",
            ]
            if english
            else [
                "使用市值近似企业价值，未单独调整净现金或净债务。",
                "模型假设前五年自由现金流按固定速度增长。",
                "该结果用于解释市场隐含预期，不是目标价。",
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
            report = content.get("report", content)
            narrative = report.get("narrative") if isinstance(report, dict) else None
            if narrative:
                lines.extend(
                    [
                        _pick(language, "## 模型原始研究", "## Original Model Research"),
                        "",
                        str(narrative),
                        "",
                    ]
                )
            elif isinstance(report, dict):
                display_report = dict(report)
                business = display_report.get("business_model")
                if isinstance(business, dict):
                    business = dict(business)
                    legacy_claims = business.pop("claims", None)
                    if legacy_claims and not display_report.get("claims"):
                        display_report["claims"] = legacy_claims
                    display_report["business_model"] = business
                for key in section_labels:
                    if key not in display_report:
                        continue
                    projected = project_report_value(
                        display_report[key],
                        include_technical=include_technical,
                        section=key,
                    )
                    rendered = (
                        _render_growth_opportunities(
                            projected,
                            language,
                            include_technical=include_technical,
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
                            continue
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
        lines.extend(
            [
                _pick(language, "## 增长机会", "## Growth Opportunities"),
                "",
                *_render_growth_opportunities(
                    growth_artifact.get("content"),
                    language,
                    include_technical=include_technical,
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
                "assumptions, and unknowns. The user makes the final investment judgment.",
            ),
        ]
    )
    return "\n".join(lines)
