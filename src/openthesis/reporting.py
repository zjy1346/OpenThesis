from __future__ import annotations

from typing import Any


SECTION_LABELS = {
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


def _render_value(value: Any, level: int = 0) -> list[str]:
    if value is None:
        return ["证据不足或尚未提供。"]
    if isinstance(value, str):
        return [value]
    if isinstance(value, bool):
        return ["是" if value else "否"]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        if not value:
            return ["暂无。"]
        lines: list[str] = []
        for item in value:
            rendered = _render_value(item, level + 1)
            lines.append(f"- {rendered[0]}")
            lines.extend(f"  {line}" for line in rendered[1:])
        return lines
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            label = SECTION_LABELS.get(str(key), str(key).replace("_", " "))
            rendered = _render_value(item, level + 1)
            if isinstance(item, (dict, list)):
                lines.append(f"**{label}**")
                lines.extend(rendered)
            else:
                lines.append(f"- **{label}：** {rendered[0]}")
        return lines
    return [str(value)]


def render_research_run(run_id: str, artifacts: list[dict[str, Any]]) -> str:
    lines = [
        "# OpenThesis 长期公司研究",
        "",
        f"研究运行：`{run_id}`",
        "",
        "> 本报告用于研究辅助，不构成投资建议或交易指令。",
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
        lines.extend(["## 反向 DCF 隐含预期", ""])
        if value.get("status") == "ok":
            lines.extend(
                [
                    f"- 当前市值输入：{value['market_cap'] / 1_000_000_000:,.2f} 十亿美元",
                    f"- 最新自由现金流：{value['base_free_cash_flow'] / 1_000_000_000:,.2f} 十亿美元",
                    f"- 前五年隐含自由现金流增速：{value['implied_fcf_growth'] * 100:.1f}%",
                    f"- 折现率：{value['discount_rate'] * 100:.1f}%",
                    f"- 永续增长率：{value['terminal_growth'] * 100:.1f}%",
                ]
            )
        else:
            lines.append(f"- 无法给出隐含增速：{value.get('reason', value['status'])}")
        for limitation in value.get("limitations", []):
            lines.append(f"- 限制：{limitation}")
        lines.append("")
    if final:
        content = final["content"]
        if content.get("mode") == "deterministic-only":
            notice = content.get("notice")
            if notice:
                lines.extend(["## AI 研究状态", "", str(notice), ""])
        else:
            report = content.get("report", content)
            narrative = report.get("narrative") if isinstance(report, dict) else None
            if narrative:
                lines.extend(["## 模型原始研究", "", str(narrative), ""])
            elif isinstance(report, dict):
                for key in SECTION_LABELS:
                    if key not in report:
                        continue
                    lines.extend(
                        [
                            f"## {SECTION_LABELS[key]}",
                            "",
                            *_render_value(report[key]),
                            "",
                        ]
                    )
            verification = content.get("verification")
            if verification:
                lines.extend(
                    [
                        "## 验证结果",
                        "",
                        f"- 是否通过：{'是' if verification.get('passed') else '否'}",
                        f"- 结论数量：{verification.get('claim_count', 0)}",
                        f"- 已验证结论：{verification.get('verified_claim_count', 0)}",
                        f"- 无证据事实：{verification.get('unsupported_fact_count', 0)}",
                    ]
                )
                for issue in verification.get("issues", []):
                    lines.append(f"- 问题：{issue}")
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
            [
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
        if value["common_claims"]:
            lines.append("### 共同结论")
            lines.extend(f"- {item}" for item in value["common_claims"])
            lines.append("")
        if value["primary_only_claims"]:
            lines.append("### 仅主模型提出")
            lines.extend(f"- {item}" for item in value["primary_only_claims"])
            lines.append("")
        if value["secondary_only_claims"]:
            lines.append("### 仅对比模型提出")
            lines.extend(f"- {item}" for item in value["secondary_only_claims"])
            lines.append("")
        different_sections = [
            item for item in value["section_comparison"] if not item["same"]
        ]
        lines.append(
            "存在结构化差异的章节："
            + (", ".join(item["section"] for item in different_sections) or "无")
        )
        lines.extend(["", f"> {value['method']}", ""])

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
        lines.extend(["## 证据来源", ""])
        for item in unique_sources[:40]:
            title = (
                item.get("title")
                or item.get("concept")
                or item.get("evidence_id")
                or "来源"
            )
            locator = item.get("locator")
            suffix = f"（{locator}）" if locator else ""
            lines.append(f"- {title}{suffix}")
            lines.append(f"  {item['source_url']}")
        lines.append("")

    lines.extend(["## 研究过程", ""])
    for artifact in artifacts:
        lines.append(
            f"- {artifact['title']} · `{artifact['agent_id']}` · `{artifact['model_id']}`"
        )
    lines.extend(
        [
            "",
            "## 方法说明",
            "",
            "财务数值来自结构化事实并由确定性程序计算。模型生成内容必须与证据、"
            "假设和未知项区分；最终投资判断由用户自行作出。",
        ]
    )
    return "\n".join(lines)
