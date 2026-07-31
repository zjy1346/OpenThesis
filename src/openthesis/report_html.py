from __future__ import annotations

import html
from typing import Any

from .financials import format_money, format_percent
from .growth import (
    evidence_grade_label,
    format_probability_range,
    growth_opportunities_from_value,
    scenario_label,
)
from .i18n import EN, normalize_language
from .reporting import (
    ARTIFACT_LABELS,
    SECTION_LABELS_EN,
    SECTION_LABELS_ZH,
)


REPORT_CSS = """
html, body {
  margin: 0;
  padding: 0;
  background: #f5f7fb;
  color: #172033;
  font-family: "Segoe UI", "Microsoft YaHei UI", Arial, sans-serif;
  font-size: 15px;
  line-height: 1.62;
}
body { padding: 22px; }
.page { max-width: 1120px; margin: 0 auto; }
.hero {
  background: #ffffff;
  border: 1px solid #dfe6ef;
  border-left: 5px solid #2563eb;
  padding: 26px 30px;
  margin-bottom: 18px;
}
.eyebrow {
  color: #2563eb;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
}
h1 {
  color: #0f172a;
  font-size: 30px;
  line-height: 1.25;
  margin: 8px 0 6px;
}
h2 {
  color: #0f172a;
  font-size: 21px;
  line-height: 1.3;
  margin: 0 0 14px;
}
h3 {
  color: #172033;
  font-size: 17px;
  line-height: 1.35;
  margin: 0 0 8px;
}
p { margin: 7px 0 12px; }
.muted { color: #64748b; }
.meta { color: #64748b; font-size: 13px; }
.notice {
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  color: #1e3a8a;
  padding: 11px 14px;
  margin-top: 16px;
}
.section {
  background: #ffffff;
  border: 1px solid #dfe6ef;
  padding: 22px 24px;
  margin-bottom: 16px;
}
.section-heading {
  border-bottom: 1px solid #e8edf4;
  padding-bottom: 10px;
  margin-bottom: 16px;
}
.badge {
  display: inline-block;
  border: 1px solid #cbd5e1;
  background: #f8fafc;
  color: #475569;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  margin: 2px 5px 2px 0;
}
.badge-blue { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
.badge-green { background: #ecfdf5; border-color: #a7f3d0; color: #047857; }
.badge-amber { background: #fffbeb; border-color: #fde68a; color: #92400e; }
.badge-red { background: #fef2f2; border-color: #fecaca; color: #b91c1c; }
.kpi-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 8px;
  margin: 0 0 14px;
}
.kpi {
  width: 25%;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  padding: 13px 14px;
  vertical-align: top;
}
.kpi-label { color: #64748b; font-size: 12px; font-weight: 600; }
.kpi-value { color: #0f172a; font-size: 19px; font-weight: 700; margin-top: 4px; }
.data-table {
  width: 100%;
  border-collapse: collapse;
  margin: 8px 0 4px;
  font-size: 13px;
}
.data-table th {
  background: #f1f5f9;
  color: #334155;
  border: 1px solid #dbe3ec;
  padding: 9px 8px;
  text-align: right;
  white-space: nowrap;
}
.data-table th:first-child { text-align: left; }
.data-table td {
  border: 1px solid #e2e8f0;
  padding: 9px 8px;
  text-align: right;
  white-space: nowrap;
}
.data-table td:first-child { text-align: left; font-weight: 600; }
.data-table tr:nth-child(even) td { background: #fafcff; }
.missing { color: #94a3b8; }
.opportunity {
  background: #fbfdff;
  border: 1px solid #dbe4ef;
  border-left: 4px solid #3b82f6;
  padding: 18px 20px;
  margin: 12px 0;
}
.opportunity-meta {
  color: #475569;
  font-size: 13px;
  margin: 8px 0 12px;
}
.label { color: #64748b; font-size: 12px; font-weight: 700; }
.value { color: #1e293b; }
.list { margin: 7px 0 12px 20px; padding: 0; }
.list li { margin: 4px 0; }
.callout {
  background: #f8fafc;
  border-left: 4px solid #94a3b8;
  padding: 12px 15px;
  margin: 10px 0;
}
.callout-success { background: #ecfdf5; border-left-color: #10b981; }
.callout-warning { background: #fffbeb; border-left-color: #f59e0b; }
.callout-danger { background: #fef2f2; border-left-color: #ef4444; }
.technical {
  background: #f8fafc;
  border: 1px dashed #94a3b8;
  padding: 12px 14px;
  margin-top: 12px;
  font-family: Consolas, monospace;
  font-size: 12px;
  color: #475569;
}
.source {
  border-bottom: 1px solid #edf1f6;
  padding: 9px 0;
}
a { color: #1d4ed8; text-decoration: none; }
code {
  background: #eef2f7;
  color: #334155;
  padding: 1px 4px;
  font-family: Consolas, monospace;
  font-size: 12px;
}
.footer {
  color: #64748b;
  font-size: 12px;
  text-align: center;
  padding: 10px 18px 24px;
}
"""


def _pick(language: str, chinese: str, english: str) -> str:
    return english if normalize_language(language) == EN else chinese


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _paragraphs(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "".join(
        f"<p>{_escape(part).replace(chr(10), '<br>')}</p>"
        for part in text.split("\n\n")
        if part.strip()
    )


def _document(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{_escape(title)}</title><style>{REPORT_CSS}</style></head>"
        f"<body><div class=\"page\">{body}</div></body></html>"
    )


def render_message_html(
    text: str,
    language: str = "zh-CN",
    *,
    title: str | None = None,
    tone: str = "neutral",
) -> str:
    language = normalize_language(language)
    heading = title or _pick(language, "OpenThesis", "OpenThesis")
    callout_class = {
        "success": "callout callout-success",
        "warning": "callout callout-warning",
        "danger": "callout callout-danger",
    }.get(tone, "callout")
    body = (
        "<div class=\"hero\">"
        f"<div class=\"eyebrow\">OpenThesis</div><h1>{_escape(heading)}</h1>"
        f"<div class=\"{callout_class}\">{_paragraphs(text)}</div>"
        "</div>"
    )
    return _document(heading, body)


def _artifact(
    artifacts: list[dict[str, Any]],
    artifact_type: str,
    *,
    reverse: bool = False,
) -> dict[str, Any] | None:
    values = reversed(artifacts) if reverse else artifacts
    return next(
        (
            artifact
            for artifact in values
            if artifact.get("artifact_type") == artifact_type
        ),
        None,
    )


def _section(title: str, content: str, *, section_id: str = "") -> str:
    identity = f" id=\"{_escape(section_id)}\"" if section_id else ""
    return (
        f"<div class=\"section\"{identity}>"
        f"<div class=\"section-heading\"><h2>{_escape(title)}</h2></div>"
        f"{content}</div>"
    )


def _metric_cell(label: str, value: str) -> str:
    value_class = "kpi-value missing" if value == "—" else "kpi-value"
    return (
        "<td class=\"kpi\">"
        f"<div class=\"kpi-label\">{_escape(label)}</div>"
        f"<div class=\"{value_class}\">{_escape(value)}</div>"
        "</td>"
    )


def _financial_section(
    metrics: object,
    language: str,
) -> str:
    english = language == EN
    rows = metrics if isinstance(metrics, list) else []
    if not rows:
        return _section(
            _pick(language, "财务概览", "Financial Overview"),
            f"<div class=\"callout callout-warning\">{_escape(_pick(language, '没有可用的标准化年度财务数据。', 'No normalized annual financial data is available.'))}</div>",
            section_id="financials",
        )

    latest = rows[0] if isinstance(rows[0], dict) else {}
    kpis = (
        (
            ("营业收入", "Revenue", format_money(latest.get("revenue"))),
            ("营业利润率", "Operating margin", format_percent(latest.get("operating_margin"))),
            ("净利润", "Net income", format_money(latest.get("net_income"))),
            ("自由现金流", "Free cash flow", format_money(latest.get("free_cash_flow"))),
        )
    )
    kpi_html = "<table class=\"kpi-table\"><tr>" + "".join(
        _metric_cell(english_label if english else chinese_label, value)
        for chinese_label, english_label, value in kpis
    ) + "</tr></table>"

    headers = (
        (
            "Fiscal year",
            "Revenue",
            "Revenue growth",
            "Operating margin",
            "Net income",
            "Operating cash flow",
            "Free cash flow",
        )
        if english
        else (
            "财年",
            "营业收入",
            "收入增长",
            "营业利润率",
            "净利润",
            "经营现金流",
            "自由现金流",
        )
    )
    table = (
        "<table class=\"data-table\"><thead><tr>"
        + "".join(f"<th>{_escape(label)}</th>" for label in headers)
        + "</tr></thead><tbody>"
    )
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        values = (
            str(row.get("year", "—")),
            format_money(row.get("revenue")),
            format_percent(row.get("revenue_growth")),
            format_percent(row.get("operating_margin")),
            format_money(row.get("net_income")),
            format_money(row.get("operating_cash_flow")),
            format_money(row.get("free_cash_flow")),
        )
        table += "<tr>" + "".join(
            f"<td class=\"{'missing' if value == '—' else ''}\">{_escape(value)}</td>"
            for value in values
        ) + "</tr>"
    table += "</tbody></table>"

    missing_count = sum(
        1
        for row in rows[:5]
        if isinstance(row, dict)
        for key in (
            "revenue",
            "operating_income",
            "net_income",
            "operating_cash_flow",
            "capital_expenditure",
        )
        if row.get(key) is None
    )
    note = ""
    if missing_count:
        note = (
            "<div class=\"callout callout-warning\">"
            + _escape(
                _pick(
                    language,
                    f"近五年存在 {missing_count} 个标准化指标缺口；缺失值显示为“—”，不会由模型补写。",
                    f"There are {missing_count} normalized metric gaps in the five-year view. Missing values are shown as “—” and are never filled by the model.",
                )
            )
            + "</div>"
        )
    return _section(
        _pick(language, "确定性财务概览", "Deterministic Financial Overview"),
        kpi_html + table + note,
        section_id="financials",
    )


def _valuation_section(value: object, language: str) -> str:
    if not isinstance(value, dict):
        return ""
    english = language == EN
    if value.get("status") == "ok":
        items = (
            (
                "Current market-cap input",
                f"${value['market_cap'] / 1_000_000_000:,.2f}B",
            ),
            (
                "Latest free cash flow",
                f"${value['base_free_cash_flow'] / 1_000_000_000:,.2f}B",
            ),
            (
                "Five-year implied FCF growth",
                f"{value['implied_fcf_growth'] * 100:.1f}%",
            ),
            ("Discount rate", f"{value['discount_rate'] * 100:.1f}%"),
            ("Terminal growth", f"{value['terminal_growth'] * 100:.1f}%"),
        ) if english else (
            ("当前市值输入", f"${value['market_cap'] / 1_000_000_000:,.2f}B"),
            ("最新自由现金流", f"${value['base_free_cash_flow'] / 1_000_000_000:,.2f}B"),
            ("前五年隐含 FCF 增速", f"{value['implied_fcf_growth'] * 100:.1f}%"),
            ("折现率", f"{value['discount_rate'] * 100:.1f}%"),
            ("永续增长率", f"{value['terminal_growth'] * 100:.1f}%"),
        )
        rows = "".join(
            f"<tr><td>{_escape(label)}</td><td>{_escape(result)}</td></tr>"
            for label, result in items
        )
        content = f"<table class=\"data-table\"><tbody>{rows}</tbody></table>"
    else:
        content = (
            "<div class=\"callout callout-warning\">"
            + _escape(
                _pick(
                    language,
                    "当前数据不足，无法可靠计算市场隐含增速。",
                    "Current data is insufficient to calculate market-implied growth reliably.",
                )
            )
            + "</div>"
        )
    content += (
        "<div class=\"callout\">"
        + _escape(
            _pick(
                language,
                "该结果用于解释市场隐含预期，不是目标价或交易建议。",
                "This result explains market-implied expectations; it is not a price target or trading recommendation.",
            )
        )
        + "</div>"
    )
    return _section(
        _pick(language, "反向 DCF 隐含预期", "Reverse DCF Implied Expectations"),
        content,
        section_id="valuation",
    )


def _simple_list(items: object) -> str:
    if not isinstance(items, list) or not items:
        return ""
    return "<ul class=\"list\">" + "".join(
        f"<li>{_escape(item)}</li>" for item in items
    ) + "</ul>"


def _growth_section(
    value: object,
    language: str,
    include_technical: bool,
) -> str:
    opportunities = growth_opportunities_from_value(value, language)
    if not opportunities:
        return _section(
            _pick(language, "增长机会", "Growth Opportunities"),
            f"<div class=\"callout\">{_escape(_pick(language, '当前证据不足，未形成可展示的增长机会。', 'Current evidence is insufficient to present a growth opportunity.'))}</div>",
            section_id="growth",
        )

    cards: list[str] = []
    for opportunity in opportunities:
        grade = str(opportunity.get("evidence_grade", "U"))
        grade_class = (
            "badge-green"
            if grade in {"A", "B"}
            else "badge-amber"
            if grade in {"C", "D"}
            else "badge-red"
        )
        horizon = opportunity.get("time_horizon_years")
        horizon_text = (
            _pick(language, f"{horizon} 年", f"{horizon} years")
            if horizon
            else _pick(language, "证据不足", "Insufficient evidence")
        )
        scenarios = [
            scenario_label(item, language)
            for item in opportunity.get("scenario_eligibility", [])
        ]
        supporting = opportunity.get("supporting_evidence_ids", [])
        contradicting = opportunity.get("contradicting_evidence_ids", [])
        title = opportunity.get("title") or _pick(
            language, "未命名机会", "Unnamed opportunity"
        )
        category = opportunity.get("category")
        maturity = opportunity.get("maturity_stage")
        badges = (
            f"<span class=\"badge {grade_class}\">{_escape(evidence_grade_label(grade, language))}</span>"
        )
        if category:
            badges += f"<span class=\"badge badge-blue\">{_escape(category)}</span>"
        if maturity:
            badges += f"<span class=\"badge\">{_escape(maturity)}</span>"

        meta = _pick(
            language,
            (
                f"可能性：{format_probability_range(opportunity.get('probability_range'), language)}"
                f"　·　时间跨度：{horizon_text}"
            ),
            (
                f"Probability: {format_probability_range(opportunity.get('probability_range'), language)}"
                f" · Time horizon: {horizon_text}"
            ),
        )
        evidence_summary = _pick(
            language,
            f"{len(supporting)} 条支持证据 · {len(contradicting)} 条相反证据",
            f"{len(supporting)} supporting · {len(contradicting)} contradicting evidence items",
        )
        scenario_text = "、".join(scenarios) if language != EN else ", ".join(scenarios)
        if not scenario_text:
            scenario_text = _pick(language, "尚未确定", "Not determined")

        detail_parts = [
            f"<div class=\"label\">{_escape(_pick(language, '增长机制', 'Growth mechanism'))}</div>",
            _paragraphs(opportunity.get("mechanism")),
            f"<div class=\"opportunity-meta\">{_escape(evidence_summary)}<br>{_escape(_pick(language, '适用情景：', 'Eligible scenarios: '))}{_escape(scenario_text)}</div>",
        ]
        capital = opportunity.get("capital_requirements")
        if capital:
            detail_parts.extend(
                [
                    f"<div class=\"label\">{_escape(_pick(language, '资本要求', 'Capital requirements'))}</div>",
                    _paragraphs(capital),
                ]
            )
        indicators = opportunity.get("leading_indicators")
        if indicators:
            detail_parts.extend(
                [
                    f"<div class=\"label\">{_escape(_pick(language, '领先指标', 'Leading indicators'))}</div>",
                    _simple_list(indicators),
                ]
            )
        invalidation = opportunity.get("invalidation_conditions")
        if invalidation:
            detail_parts.extend(
                [
                    f"<div class=\"label\">{_escape(_pick(language, '失效条件', 'Invalidation conditions'))}</div>",
                    _simple_list(invalidation),
                ]
            )
        if include_technical:
            technical_lines = [
                _pick(language, "机会 ID", "Opportunity ID")
                + ": "
                + str(opportunity.get("opportunity_id", "")),
                _pick(language, "支持证据 ID", "Supporting evidence IDs")
                + ": "
                + (", ".join(map(str, supporting)) or "—"),
                _pick(language, "相反证据 ID", "Contradicting evidence IDs")
                + ": "
                + (", ".join(map(str, contradicting)) or "—"),
            ]
            detail_parts.append(
                "<div class=\"technical\">"
                + "<br>".join(_escape(line) for line in technical_lines)
                + "</div>"
            )

        cards.append(
            "<div class=\"opportunity\">"
            f"<h3>{_escape(title)}</h3><div>{badges}</div>"
            f"<div class=\"opportunity-meta\">{_escape(meta)}</div>"
            + "".join(detail_parts)
            + "</div>"
        )
    return _section(
        _pick(language, "增长机会", "Growth Opportunities"),
        "".join(cards),
        section_id="growth",
    )


def _render_generic(value: object, language: str, level: int = 0) -> str:
    if value is None:
        return f"<span class=\"muted\">{_escape(_pick(language, '证据不足或尚未提供。', 'Insufficient evidence or not provided.'))}</span>"
    if isinstance(value, str):
        return _paragraphs(value)
    if isinstance(value, bool):
        return _escape(_pick(language, "是" if value else "否", "Yes" if value else "No"))
    if isinstance(value, (int, float)):
        return _escape(value)
    if isinstance(value, list):
        if not value:
            return f"<span class=\"muted\">{_escape(_pick(language, '暂无。', 'None.'))}</span>"
        return "<ul class=\"list\">" + "".join(
            f"<li>{_render_generic(item, language, level + 1)}</li>"
            for item in value
        ) + "</ul>"
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if str(key).startswith("_"):
                continue
            label = (
                str(key).replace("_", " ").title()
                if language == EN
                else str(key).replace("_", " ")
            )
            parts.append(
                f"<div class=\"label\">{_escape(label)}</div>"
                f"{_render_generic(item, language, level + 1)}"
            )
        return "".join(parts)
    return _escape(value)


def _report_sections(
    report: object,
    language: str,
    include_technical: bool,
) -> str:
    labels = SECTION_LABELS_EN if language == EN else SECTION_LABELS_ZH
    if not isinstance(report, dict):
        return _section(
            _pick(language, "模型研究", "Model Research"),
            _paragraphs(report),
            section_id="research",
        )
    parts: list[str] = []
    for key, title in labels.items():
        if key not in report:
            continue
        if key == "growth_opportunities":
            parts.append(_growth_section(report[key], language, include_technical))
            continue
        parts.append(
            _section(
                title,
                _render_generic(report[key], language),
                section_id=key.replace("_", "-"),
            )
        )
    return "".join(parts)


def _verification_section(value: object, language: str) -> str:
    if not isinstance(value, dict):
        return ""
    passed = bool(value.get("passed"))
    css_class = "callout callout-success" if passed else "callout callout-warning"
    headline = _pick(
        language,
        "证据验证通过" if passed else "存在需要复核的内容",
        "Evidence verification passed" if passed else "Some content needs review",
    )
    summary = _pick(
        language,
        (
            f"结论 {value.get('claim_count', 0)} 条 · "
            f"已验证 {value.get('verified_claim_count', 0)} 条 · "
            f"无证据事实 {value.get('unsupported_fact_count', 0)} 条"
        ),
        (
            f"{value.get('claim_count', 0)} claims · "
            f"{value.get('verified_claim_count', 0)} verified · "
            f"{value.get('unsupported_fact_count', 0)} unsupported facts"
        ),
    )
    issues = _simple_list(value.get("issues", []))
    return _section(
        _pick(language, "验证结果", "Verification Results"),
        f"<div class=\"{css_class}\"><strong>{_escape(headline)}</strong><br>{_escape(summary)}</div>{issues}",
        section_id="verification",
    )


def _sources_section(
    evidence: object,
    language: str,
) -> str:
    if not isinstance(evidence, list):
        return ""
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        url = str(item.get("source_url", "")).strip()
        identity = (str(item.get("evidence_id", "")), url)
        if not url or identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    if not unique:
        return ""
    rows: list[str] = []
    for item in unique[:40]:
        title = (
            item.get("title")
            or item.get("concept")
            or _pick(language, "SEC 来源", "SEC source")
        )
        locator = str(item.get("locator", "")).strip()
        suffix = f" · {locator}" if locator else ""
        rows.append(
            "<div class=\"source\">"
            f"<a href=\"{_escape(item['source_url'])}\">{_escape(title)}</a>"
            f"<div class=\"muted\">{_escape(suffix.lstrip(' ·'))}</div>"
            "</div>"
        )
    return _section(
        _pick(language, "证据来源", "Evidence Sources"),
        "".join(rows),
        section_id="sources",
    )


def _technical_process(
    artifacts: list[dict[str, Any]],
    language: str,
) -> str:
    rows: list[str] = []
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type", ""))
        labels = ARTIFACT_LABELS.get(artifact_type)
        title = (
            labels[1] if language == EN else labels[0]
        ) if labels else str(artifact.get("title", artifact_type))
        rows.append(
            "<tr>"
            f"<td>{_escape(title)}</td>"
            f"<td><code>{_escape(artifact.get('agent_id', ''))}</code></td>"
            f"<td><code>{_escape(artifact.get('model_id', ''))}</code></td>"
            "</tr>"
        )
    headers = (
        ("Artifact", "Agent", "Model")
        if language == EN
        else ("研究产物", "Agent", "模型")
    )
    table = (
        "<table class=\"data-table\"><thead><tr>"
        + "".join(f"<th>{_escape(item)}</th>" for item in headers)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    return _section(
        _pick(language, "技术详情", "Technical Details"),
        table,
        section_id="technical",
    )


def render_research_html(
    run_id: str,
    artifacts: list[dict[str, Any]],
    language: str = "zh-CN",
    *,
    company_name: str = "",
    include_technical: bool = False,
) -> str:
    language = normalize_language(language)
    title = company_name or _pick(language, "公司研究报告", "Company Research Report")
    hero = (
        "<div class=\"hero\">"
        f"<div class=\"eyebrow\">{_escape(_pick(language, '长期公司研究', 'Long-term company research'))}</div>"
        f"<h1>{_escape(title)}</h1>"
        f"<div class=\"meta\">{_escape(_pick(language, '研究运行', 'Research run'))}: <code>{_escape(run_id)}</code></div>"
        "<div class=\"notice\">"
        + _escape(
            _pick(
                language,
                "本报告用于研究辅助，不构成投资建议或交易指令。",
                "This report is research assistance, not investment advice or a trade instruction.",
            )
        )
        + "</div></div>"
    )

    deterministic = _artifact(artifacts, "deterministic-financial-summary")
    valuation = _artifact(artifacts, "deterministic-valuation")
    growth_artifact = _artifact(artifacts, "growth-opportunities", reverse=True)
    final = _artifact(artifacts, "research-report", reverse=True)
    growth_rendered = False

    body = [hero]
    if deterministic:
        body.append(
            _financial_section(
                deterministic.get("content", {}).get("metrics"),
                language,
            )
        )
    if valuation:
        body.append(_valuation_section(valuation.get("content"), language))
    if final:
        content = final.get("content", {})
        if content.get("mode") == "deterministic-only":
            body.append(
                _section(
                    _pick(language, "AI 研究状态", "AI Research Status"),
                    f"<div class=\"callout\">{_escape(_pick(language, '未配置模型，因此没有生成定性研究、增长机会和长期情景。', 'No model was configured, so qualitative research, growth opportunities, and long-term scenarios were not generated.'))}</div>",
                )
            )
        else:
            report = content.get("report", content)
            if isinstance(report, dict) and report.get("narrative"):
                body.append(
                    _section(
                        _pick(language, "模型研究", "Model Research"),
                        _paragraphs(report["narrative"]),
                        section_id="research",
                    )
                )
            else:
                growth_rendered = (
                    isinstance(report, dict)
                    and "growth_opportunities" in report
                )
                body.append(
                    _report_sections(
                        report,
                        language,
                        include_technical,
                    )
                )
            body.append(_verification_section(content.get("verification"), language))
    if growth_artifact and not growth_rendered:
        body.append(
            _growth_section(
                growth_artifact.get("content"),
                language,
                include_technical,
            )
        )

    evidence = (
        deterministic.get("content", {}).get("evidence", [])
        if deterministic
        else []
    )
    body.append(_sources_section(evidence, language))
    if include_technical:
        body.append(_technical_process(artifacts, language))
    body.append(
        "<div class=\"footer\">"
        + _escape(
            _pick(
                language,
                "财务数据来自结构化事实与确定性计算；最终投资判断由用户自行作出。",
                "Financial data comes from structured facts and deterministic calculations. The user makes the final investment judgment.",
            )
        )
        + "</div>"
    )
    return _document(title, "".join(part for part in body if part))
