from __future__ import annotations

from collections import defaultdict
from typing import Any

from .i18n import EN, normalize_language


_ANNUAL_PERIODS = frozenset({"", "FY", "CY", "ANNUAL"})
_INTERIM_PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q2": 2, "Q3": 3, "9M": 3, "Q": 3}


def _period(value: object) -> str:
    return str(value or "").strip().upper()


def latest_by_year(facts: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    """Return annual facts only.

    Quarterly and interim flows are not comparable with full-year values.  Older
    imported fixtures with no period remain annual for backwards compatibility.
    """

    matrix: dict[int, dict[str, float]] = defaultdict(dict)
    filed: dict[tuple[int, str], str] = {}
    for fact in facts:
        if _period(fact.get("fiscal_period")) not in _ANNUAL_PERIODS:
            continue
        year = int(fact["fiscal_year"])
        concept = str(fact["concept"])
        key = (year, concept)
        filing_date = str(fact["filed_at"])
        if key not in filed or filing_date >= filed[key]:
            matrix[year][concept] = float(fact["value"])
            filed[key] = filing_date
    return dict(sorted(matrix.items(), reverse=True))


def _latest_by_interim_period(
    facts: list[dict[str, Any]],
) -> tuple[dict[tuple[int, str], dict[str, float]], dict[tuple[int, str], str]]:
    matrix: dict[tuple[int, str], dict[str, float]] = defaultdict(dict)
    filed: dict[tuple[int, str, str], str] = {}
    period_ends: dict[tuple[int, str], str] = {}
    for fact in facts:
        period = _period(fact.get("fiscal_period"))
        if period in _ANNUAL_PERIODS:
            continue
        year = int(fact["fiscal_year"])
        concept = str(fact["concept"])
        key = (year, period)
        filing_date = str(fact.get("filed_at", ""))
        concept_key = (year, period, concept)
        if concept_key not in filed or filing_date >= filed[concept_key]:
            matrix[key][concept] = float(fact["value"])
            filed[concept_key] = filing_date
        period_ends[key] = max(period_ends.get(key, ""), str(fact.get("end_date", "")))
    return dict(matrix), period_ends


def safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def growth_rate(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return current / previous - 1


def calculate_metrics(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matrix = latest_by_year(facts)
    years = sorted(matrix, reverse=True)
    results: list[dict[str, Any]] = []
    for index, year in enumerate(years):
        values = matrix[year]
        previous = matrix.get(years[index + 1]) if index + 1 < len(years) else {}
        revenue = values.get("revenue")
        operating_income = values.get("operating_income")
        net_income = values.get("net_income")
        operating_cash_flow = values.get("operating_cash_flow")
        capex = values.get("capital_expenditure")
        assets = values.get("assets")
        liabilities = values.get("liabilities")
        equity = values.get("equity")
        reported_roe = values.get("reported_roe")
        free_cash_flow = (
            operating_cash_flow - capex
            if operating_cash_flow is not None and capex is not None
            else None
        )
        results.append(
            {
                "year": year,
                **values,
                "revenue_growth": growth_rate(revenue, previous.get("revenue")),
                "operating_margin": safe_divide(operating_income, revenue),
                "net_margin": safe_divide(net_income, revenue),
                "cash_conversion": safe_divide(operating_cash_flow, net_income),
                "free_cash_flow": free_cash_flow,
                "debt_to_assets": safe_divide(liabilities, assets),
                # Prefer the issuer's disclosed weighted-average ROE. The
                # fallback uses ending attributable equity and is explicitly a
                # less precise approximation when average equity is unavailable.
                "return_on_equity": reported_roe if reported_roe is not None else safe_divide(net_income, equity),
            }
        )
    return results


def calculate_interim_metrics(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calculate period-aware interim snapshots and like-for-like growth.

    Revenue growth is computed only against the same fiscal period in the prior
    year (Q1 vs Q1, H1 vs H1, and so on).  No interim value is annualized.
    """

    matrix, period_ends = _latest_by_interim_period(facts)
    keys = sorted(
        matrix,
        key=lambda item: (item[0], _INTERIM_PERIOD_ORDER.get(item[1], 0), item[1]),
        reverse=True,
    )
    results: list[dict[str, Any]] = []
    for year, period in keys:
        values = matrix[(year, period)]
        previous = matrix.get((year - 1, period), {})
        revenue = values.get("revenue")
        operating_income = values.get("operating_income")
        net_income = values.get("net_income")
        operating_cash_flow = values.get("operating_cash_flow")
        capex = values.get("capital_expenditure")
        assets = values.get("assets")
        liabilities = values.get("liabilities")
        equity = values.get("equity")
        reported_roe = values.get("reported_roe")
        results.append(
            {
                "year": year,
                "period": period,
                "period_end": period_ends.get((year, period), ""),
                "comparison_period": f"{year - 1} {period}" if previous else None,
                **values,
                "revenue_growth": growth_rate(revenue, previous.get("revenue")),
                "operating_margin": safe_divide(operating_income, revenue),
                "net_margin": safe_divide(net_income, revenue),
                "cash_conversion": safe_divide(operating_cash_flow, net_income),
                "free_cash_flow": (
                    operating_cash_flow - capex
                    if operating_cash_flow is not None and capex is not None
                    else None
                ),
                "debt_to_assets": safe_divide(liabilities, assets),
                "return_on_equity": (
                    reported_roe
                    if reported_roe is not None
                    else safe_divide(net_income, equity)
                ),
            }
        )
    return results


def format_money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "—"
    prefix = {"USD": "$", "CNY": "¥", "HKD": "HK$"}.get(
        str(currency).upper(),
        f"{str(currency).upper()} ",
    )
    absolute = abs(value)
    if absolute >= 1_000_000_000_000:
        return f"{prefix}{value / 1_000_000_000_000:,.2f}T"
    if absolute >= 1_000_000_000:
        return f"{prefix}{value / 1_000_000_000:,.2f}B"
    if absolute >= 1_000_000:
        return f"{prefix}{value / 1_000_000:,.2f}M"
    return f"{prefix}{value:,.0f}"


def format_percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def discounted_cash_flow_value(
    base_free_cash_flow: float,
    annual_growth: float,
    discount_rate: float,
    terminal_growth: float,
    horizon_years: int = 5,
) -> float:
    if base_free_cash_flow <= 0:
        raise ValueError("自由现金流必须为正数")
    if horizon_years < 1:
        raise ValueError("预测年限必须大于零")
    if discount_rate <= terminal_growth:
        raise ValueError("折现率必须高于永续增长率")
    present_value = 0.0
    cash_flow = base_free_cash_flow
    for year in range(1, horizon_years + 1):
        cash_flow *= 1 + annual_growth
        present_value += cash_flow / ((1 + discount_rate) ** year)
    terminal_value = cash_flow * (1 + terminal_growth) / (
        discount_rate - terminal_growth
    )
    return present_value + terminal_value / ((1 + discount_rate) ** horizon_years)


def implied_fcf_growth(
    market_cap: float,
    base_free_cash_flow: float,
    discount_rate: float = 0.10,
    terminal_growth: float = 0.03,
    horizon_years: int = 5,
) -> float | None:
    if market_cap <= 0 or base_free_cash_flow <= 0:
        return None
    low, high = -0.60, 1.50
    low_value = discounted_cash_flow_value(
        base_free_cash_flow, low, discount_rate, terminal_growth, horizon_years
    )
    high_value = discounted_cash_flow_value(
        base_free_cash_flow, high, discount_rate, terminal_growth, horizon_years
    )
    if market_cap < low_value or market_cap > high_value:
        return None
    for _ in range(100):
        middle = (low + high) / 2
        value = discounted_cash_flow_value(
            base_free_cash_flow,
            middle,
            discount_rate,
            terminal_growth,
            horizon_years,
        )
        if value < market_cap:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def reverse_dcf_analysis(
    metrics: list[dict[str, Any]],
    market_cap: float,
    discount_rate: float = 0.10,
    terminal_growth: float = 0.03,
    horizon_years: int = 5,
) -> dict[str, Any]:
    if not metrics:
        return {"status": "insufficient_data", "reason": "没有财务指标"}
    base_fcf = metrics[0].get("free_cash_flow")
    if not isinstance(base_fcf, (int, float)) or base_fcf <= 0:
        return {
            "status": "insufficient_data",
            "reason": "最新财年自由现金流不是正数，无法使用标准反向 DCF",
        }
    implied = implied_fcf_growth(
        market_cap,
        float(base_fcf),
        discount_rate,
        terminal_growth,
        horizon_years,
    )
    sensitivity = [
        {
            "fcf_growth": growth,
            "enterprise_value": discounted_cash_flow_value(
                float(base_fcf),
                growth,
                discount_rate,
                terminal_growth,
                horizon_years,
            ),
        }
        for growth in (-0.05, 0.0, 0.05, 0.10, 0.15, 0.20, 0.30)
    ]
    return {
        "status": "ok" if implied is not None else "outside_search_range",
        "market_cap": market_cap,
        "base_free_cash_flow": float(base_fcf),
        "discount_rate": discount_rate,
        "terminal_growth": terminal_growth,
        "horizon_years": horizon_years,
        "implied_fcf_growth": implied,
        "sensitivity": sensitivity,
        "limitations": [
            "使用市值近似企业价值，未单独调整净现金或净债务。",
            "模型假设前五年自由现金流按固定速度增长。",
            "该结果用于解释市场隐含预期，不是目标价。",
        ],
    }


def deterministic_summary(
    company_name: str,
    metrics: list[dict[str, Any]],
    language: str = "zh-CN",
    currency: str = "USD",
) -> str:
    english = normalize_language(language) == EN
    lines = (
        [
            f"# {company_name} Financial Overview",
            "",
            "The following content was generated by the deterministic financial engine.",
            "",
        ]
        if english
        else [f"# {company_name} 财务概览", "", "以下内容由确定性财务引擎生成。", ""]
    )
    if not metrics:
        return "\n".join(
            lines
            + [
                (
                    "No normalized annual financial data is available."
                    if english
                    else "没有可用的标准化年度财务数据。"
                )
            ]
        )
    lines.extend(
        (
            [
                "| Fiscal year | Revenue | Revenue growth | Operating margin | Net income | Operating cash flow | Free cash flow |",
                "|---:|---:|---:|---:|---:|---:|---:|",
            ]
            if english
            else [
                "| 财年 | 营业收入 | 收入增长 | 营业利润率 | 净利润 | 经营现金流 | 自由现金流 |",
                "|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
    )
    for row in metrics[:5]:
        lines.append(
            "| {year} | {revenue} | {growth} | {op_margin} | {net_income} | {ocf} | {fcf} |".format(
                year=row["year"],
                revenue=format_money(row.get("revenue"), currency),
                growth=format_percent(row.get("revenue_growth")),
                op_margin=format_percent(row.get("operating_margin")),
                net_income=format_money(row.get("net_income"), currency),
                ocf=format_money(row.get("operating_cash_flow"), currency),
                fcf=format_money(row.get("free_cash_flow"), currency),
            )
        )
    latest = metrics[0]
    lines.extend(
        (
            [
                "",
                "## Latest Fiscal-Year Deterministic Metrics",
                "",
                f"- Cash conversion: {format_percent(latest.get('cash_conversion'))}",
                f"- Debt to assets: {format_percent(latest.get('debt_to_assets'))}",
                f"- Return on equity: {format_percent(latest.get('return_on_equity'))}",
                "",
                "> These metrics are research inputs, not investment advice.",
            ]
            if english
            else [
                "",
                "## 最新财年确定性指标",
                "",
                f"- 现金利润转化率：{format_percent(latest.get('cash_conversion'))}",
                f"- 资产负债率：{format_percent(latest.get('debt_to_assets'))}",
                f"- 净资产收益率：{format_percent(latest.get('return_on_equity'))}",
                "",
                "> 这些指标只是研究输入，不构成投资建议。",
            ]
        )
    )
    return "\n".join(lines)
