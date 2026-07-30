from __future__ import annotations

from typing import Any

from .financials import deterministic_summary
from .i18n import EN, normalize_language


SECTION_LABELS_ZH = {
    "executive_summary": "执行摘要",
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
    "claims": "主要结论",
}

SECTION_LABELS_EN = {
    "executive_summary": "Executive Summary",
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
    "claims": "Key Claims",
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
        return [value]
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
            label = labels.get(str(key), str(key).replace("_", " ").title() if english else str(key).replace("_", " "))
            rendered = _render_value(item, language, level + 1)
            if isinstance(item, (dict, list)):
                lines.append(f"**{label}**")
                lines.extend(rendered)
            else:
                separator = ": " if english else "："
                lines.append(f"- **{label}{separator}** {rendered[0]}")
        return lines
    return [str(value)]


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
    if deterministic:
        metrics = deterministic["content"].get("metrics")
        if company_name and isinstance(metrics, list):
            lines.extend(
                [
                    deterministic_summary(company_name, metrics, language),
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
                for key in section_labels:
                    if key not in report:
                        continue
                    lines.extend(
                        [
                            f"## {section_labels[key]}",
                            "",
                            *_render_value(report[key], language),
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
                issue_prefix = "- Issue: " if english else "- 问题："
                for issue in verification.get("issues", []):
                    lines.append(issue_prefix + str(issue))
                lines.append("")

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
