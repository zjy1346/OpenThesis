from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Any

from .i18n import EN, ZH_HANT, normalize_language


_ANNUAL_PERIODS = frozenset({"", "FY", "CY", "ANNUAL"})
_INTERIM_PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q2": 2, "Q3": 3, "9M": 3, "Q": 3}


_REVERSE_DCF_STATUS_TEXT: dict[str, tuple[str, str, str]] = {
    "insufficient_data": (
        "没有足够的正自由现金流数据。",
        "沒有足夠的正自由現金流資料。",
        "There is not enough positive free-cash-flow data.",
    ),
    "outside_search_range": (
        "隐含增速超出当前搜索范围。",
        "隱含增速超出目前搜尋範圍。",
        "The implied growth rate is outside the current search range.",
    ),
    "extreme_assumption": (
        "可求得市场隐含增速，但结果处于极端假设区间，请谨慎解释。",
        "可求得市場隱含增速，但結果處於極端假設區間，請審慎解讀。",
        "A market-implied growth rate is solvable, but it requires an extreme assumption.",
    ),
    "no_economic_solution": (
        "在保持现金流与折现模型经济含义的范围内无可用解。",
        "在維持現金流與折現模型經濟含義的範圍內無可用解。",
        "No solution exists within economically meaningful cash-flow assumptions.",
    ),
    "market_snapshot_unavailable": (
        "行情快照不可用，无法计算市场隐含增速。",
        "行情快照無法使用，無法計算市場隱含增速。",
        "The market snapshot is unavailable, so implied growth cannot be calculated.",
    ),
    "currency_mismatch": (
        "行情与报告币种不一致，无法可靠计算市场隐含增速。",
        "行情與報告幣別不一致，無法可靠計算市場隱含增速。",
        "The quote and reporting currencies do not match, so implied growth cannot be calculated reliably.",
    ),
    "valuation_unit_mismatch": (
        "市场价值或自由现金流缺少可验证的金额单位，无法可靠计算市场隐含增速。",
        "市場價值或自由現金流缺少可驗證的金額單位，無法可靠計算市場隱含增速。",
        "The market value or free cash flow has no verifiable monetary unit, so implied growth cannot be calculated reliably.",
    ),
    "valuation_currency_mismatch": (
        "市场价值与财报币种不一致，无法可靠计算市场隐含增速。",
        "市場價值與財報幣別不一致，無法可靠計算市場隱含增速。",
        "The market value and financial statements use different currencies, so implied growth cannot be calculated reliably.",
    ),
    "not_applicable": (
        "当前公司类型不适用标准自由现金流反向 DCF。",
        "目前公司類型不適用標準自由現金流反向 DCF。",
        "Standard free-cash-flow reverse DCF is not applicable to this company type.",
    ),
    "invalid_parameters": (
        "估值参数无效，请检查折现率、永续增长率与预测年限。",
        "估值參數無效，請檢查折現率、永續增長率與預測年限。",
        "The valuation parameters are invalid; check the discount rate, terminal growth, and forecast horizon.",
    ),
}

_REVERSE_DCF_DEFAULT_TEXT = (
    "当前数据不足，无法可靠计算市场隐含增速。",
    "目前資料不足，無法可靠計算市場隱含增速。",
    "Current data is insufficient to calculate market-implied growth reliably.",
)

_REVERSE_DCF_DISCLAIMER = (
    "该结果用于解释市场隐含预期，不是目标价或交易建议。",
    "該結果用於解釋市場隱含預期，不是目標價或交易建議。",
    "This result explains market-implied expectations; it is not a price target or trading recommendation.",
)


@dataclass(frozen=True, slots=True)
class NormalizedMoney:
    """A monetary amount with an explicit, one-time normalization contract.

    ``value`` is the displayed/raw amount when created with :meth:`from_raw`
    and is already in base currency when created with :meth:`from_normalized`.
    Callers cannot silently infer a scale from a bare float at the valuation
    boundary; the legacy float path remains supported as an already-normalized
    compatibility value.
    """

    value: float
    currency: str
    unit_scale: float = 1.0
    unit_provenance: str = "normalized"
    source_id: str = ""
    as_of: str = ""

    @classmethod
    def from_raw(
        cls, value: float, currency: str, unit_scale: float,
        unit_provenance: str = "declared",
    ) -> "NormalizedMoney":
        if unit_scale <= 0 or not unit_provenance or unit_provenance == "unknown":
            raise ValueError("money_unit_provenance_required")
        return cls(float(value), str(currency).upper(), float(unit_scale), unit_provenance)

    @classmethod
    def from_normalized(
        cls, value: float, currency: str, *, source_id: str = "", as_of: str = ""
    ) -> "NormalizedMoney":
        return cls(float(value), str(currency).upper(), 1.0, "normalized", source_id, as_of)

    @property
    def normalized_value(self) -> float:
        if self.unit_provenance == "normalized":
            return float(self.value)
        try:
            return float(Decimal(str(self.value)) * Decimal(str(self.unit_scale)))
        except (InvalidOperation, ValueError):
            raise ValueError("money_value_invalid") from None


@dataclass(frozen=True, slots=True)
class FinancialTension:
    """A source-backed divergence between earnings growth and cash-flow growth."""

    period: int
    net_income_growth: float
    operating_cash_flow_growth: float
    currency: str
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "type": "profit_cash_flow_growth_divergence",
            "observations": {
                "net_income_growth": self.net_income_growth,
                "operating_cash_flow_growth": self.operating_cash_flow_growth,
                "currency": self.currency,
            },
            "explanation_state": "not_established_from_verified_inputs",
            "evidence_ids": list(self.evidence_ids),
        }


def _fact_money_metadata(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "currency": str(fact.get("currency") or fact.get("unit") or "").upper(),
        "unit_scale": float(fact.get("unit_scale") or 1.0),
        "unit_provenance": str(fact.get("unit_provenance") or "unknown"),
        "fact_id": str(fact.get("fact_id", "")),
        "accession_number": str(fact.get("accession_number", "")),
        "scope": str(fact.get("consolidated_scope") or fact.get("scope") or ""),
        "generation_id": str(fact.get("generation_id", "")),
        "parser_version": str(fact.get("parser_version", "")),
        "source_document": str(fact.get("source_document", "")),
        "period": _period(fact.get("fiscal_period")),
        "period_end": str(fact.get("end_date", "")),
    }


def _same_financial_generation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Match the active persisted generation, or a pre-persistence PDF cohort.

    Direct ingestion and fixture consumers can calculate before storage assigns
    the canonical generation id. In that narrow case, require both facts to
    come from the same filing, parser version, and source document; never infer
    identity from period or matching values alone.
    """
    left_generation = str(left.get("generation_id") or "")
    right_generation = str(right.get("generation_id") or "")
    if left_generation or right_generation:
        return bool(left_generation and left_generation == right_generation)
    return bool(
        left.get("accession_number")
        and left.get("accession_number") == right.get("accession_number")
        and left.get("parser_version")
        and left.get("parser_version") == right.get("parser_version")
        and left.get("source_document")
        and left.get("source_document") == right.get("source_document")
    )


def _compatible_annual_money_inputs(
    left: dict[str, Any] | None, right: dict[str, Any] | None,
) -> bool:
    if not left or not right:
        return False
    return bool(
        left.get("currency")
        and left.get("currency") == right.get("currency")
        and left.get("scope")
        and left.get("scope") == right.get("scope")
        and _same_financial_generation(left, right)
        and left.get("period") in _ANNUAL_PERIODS
        and left.get("period") == right.get("period")
        and left.get("period_end")
        and left.get("period_end") == right.get("period_end")
        and left.get("unit_provenance") not in (None, "", "unknown")
        and right.get("unit_provenance") not in (None, "", "unknown")
        and left.get("fact_id")
        and right.get("fact_id")
    )


def _free_cash_flow_gap_reason(
    operating_cash_flow: Any,
    capital_expenditure: Any,
    operating_metadata: dict[str, Any] | None,
    capex_metadata: dict[str, Any] | None,
) -> str | None:
    """Preserve the exact missing or incompatible inputs behind an FCF gap."""
    if operating_cash_flow is None:
        return "missing_operating_cash_flow"
    if capital_expenditure is None:
        return "missing_capital_expenditure"
    if not operating_metadata or not capex_metadata:
        return "missing_source_metadata"
    for key, reason in (
        ("currency", "currency_mismatch"),
        ("scope", "scope_mismatch"),
        ("period", "period_mismatch"),
        ("period_end", "period_end_mismatch"),
    ):
        left, right = operating_metadata.get(key), capex_metadata.get(key)
        if not left or not right:
            return f"missing_{key}"
        if left != right:
            return reason
    if not _same_financial_generation(operating_metadata, capex_metadata):
        return "generation_mismatch"
    if operating_metadata.get("unit_provenance") in (None, "", "unknown") or capex_metadata.get("unit_provenance") in (None, "", "unknown"):
        return "unit_unverified"
    if not operating_metadata.get("fact_id") or not capex_metadata.get("fact_id"):
        return "missing_source_fact"
    return "inputs_not_comparable"


def _financial_tension(
    year: int,
    net_income_growth: float | None,
    operating_cash_flow_growth: float | None,
    current_metadata: dict[tuple[int, str], dict[str, Any]],
    prior_facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if (
        net_income_growth is None
        or operating_cash_flow_growth is None
        or net_income_growth * operating_cash_flow_growth >= 0
    ):
        return None
    prior_metadata = {
        str(fact.get("concept") or ""): _fact_money_metadata(fact)
        for fact in prior_facts
        if fact.get("concept") in {"net_income", "operating_cash_flow"}
    }
    current_net = current_metadata.get((year, "net_income"))
    current_cash = current_metadata.get((year, "operating_cash_flow"))
    prior_net = prior_metadata.get("net_income")
    prior_cash = prior_metadata.get("operating_cash_flow")
    bases = (current_net, current_cash, prior_net, prior_cash)
    if any(not item for item in bases):
        return None
    typed_bases = [item for item in bases if item is not None]
    if (
        len({item.get("currency") for item in typed_bases}) != 1
        or len({item.get("scope") for item in typed_bases}) != 1
        or not all(item.get("currency") and item.get("scope") for item in typed_bases)
        or not _compatible_annual_money_inputs(current_net, current_cash)
        or not _compatible_annual_money_inputs(prior_net, prior_cash)
    ):
        return None
    evidence_ids = tuple(
        f"fact:{item['fact_id']}"
        for item in typed_bases
        if item.get("fact_id")
    )
    if len(evidence_ids) != 4:
        return None
    return FinancialTension(
        year,
        float(net_income_growth),
        float(operating_cash_flow_growth),
        str(typed_bases[0]["currency"]),
        evidence_ids,
    ).to_dict()


def _coerce_money(value: object, *, fallback_currency: str = "") -> NormalizedMoney | None:
    if isinstance(value, NormalizedMoney):
        return value
    if isinstance(value, dict):
        try:
            provenance = str(value.get("unit_provenance", value.get("provenance", "")))
            scale = float(value.get("unit_scale", 1.0))
            amount = float(value["value"])
            currency = str(value.get("currency", fallback_currency))
            if provenance == "normalized" or scale == 1.0:
                if not provenance:
                    provenance = "normalized"
                return NormalizedMoney(
                    amount, currency.upper(), scale, provenance,
                    str(value.get("source_id", "")), str(value.get("as_of", "")),
                )
            return NormalizedMoney.from_raw(amount, currency, scale, provenance)
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return NormalizedMoney.from_normalized(float(value), fallback_currency)
    return None


def _has_explicit_normalized_money(value: object) -> bool:
    """Require an explicit normalized provenance marker at strict seams."""
    if isinstance(value, NormalizedMoney):
        return value.unit_provenance == "normalized" and value.unit_scale == 1.0
    if not isinstance(value, dict):
        return False
    provenance = str(value.get("unit_provenance", value.get("provenance", ""))).strip().casefold()
    try:
        scale = float(value.get("unit_scale", 0))
    except (TypeError, ValueError):
        return False
    return provenance == "normalized" and scale == 1.0


def reverse_dcf_status_text(status: object, language: str = "zh-CN") -> str:
    """Return a stable localized reason without exposing provider narrative."""
    locale = normalize_language(language)
    index = 2 if locale == EN else 1 if locale == ZH_HANT else 0
    return _REVERSE_DCF_STATUS_TEXT.get(str(status), _REVERSE_DCF_DEFAULT_TEXT)[index]


def reverse_dcf_disclaimer(language: str = "zh-CN") -> str:
    locale = normalize_language(language)
    index = 2 if locale == EN else 1 if locale == ZH_HANT else 0
    return _REVERSE_DCF_DISCLAIMER[index]


def _period(value: object) -> str:
    return str(value or "").strip().upper()


def latest_by_year(facts: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    """Return annual facts only.

    Quarterly and interim flows are not comparable with full-year values.  Older
    imported fixtures with no period remain annual for backwards compatibility.
    """

    matrix: dict[int, dict[str, float]] = defaultdict(dict)
    filed: dict[tuple[int, str], tuple[int, str, str]] = {}
    for fact in facts:
        if _period(fact.get("fiscal_period")) not in _ANNUAL_PERIODS:
            continue
        year = int(fact["fiscal_year"])
        concept = str(fact["concept"])
        key = (year, concept)
        filing_date = str(fact.get("filed_at", ""))
        priority = 0 if str(fact.get("usage_status", "")).casefold() == "comparator" else 1
        rank = (priority, filing_date, str(fact.get("fact_id", "")))
        if key not in filed or rank >= filed[key]:
            matrix[year][concept] = float(fact["value"])
            filed[key] = rank
    return dict(sorted(matrix.items(), reverse=True))


def _latest_by_interim_period(
    facts: list[dict[str, Any]],
) -> tuple[dict[tuple[int, str], dict[str, float]], dict[tuple[int, str], str]]:
    matrix: dict[tuple[int, str], dict[str, float]] = defaultdict(dict)
    filed: dict[tuple[int, str, str], tuple[int, str, str]] = {}
    period_ends: dict[tuple[int, str], str] = {}
    for fact in facts:
        period = _period(fact.get("fiscal_period"))
        if period in _ANNUAL_PERIODS:
            continue
        year = int(fact["fiscal_year"])
        concept = str(fact["concept"])
        key = (year, period)
        filing_date = str(fact.get("filed_at", ""))
        priority = 0 if str(fact.get("usage_status", "")).casefold() == "comparator" else 1
        rank = (priority, filing_date, str(fact.get("fact_id", "")))
        concept_key = (year, period, concept)
        if concept_key not in filed or rank >= filed[concept_key]:
            matrix[key][concept] = float(fact["value"])
            filed[concept_key] = rank
        period_ends[key] = max(period_ends.get(key, ""), str(fact.get("end_date", "")))
    return dict(matrix), period_ends


def safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _selected_period_fact(
    facts: list[dict[str, Any]], concept: str, year: int, period: str | None
) -> dict[str, Any] | None:
    candidates = []
    for fact in facts:
        if str(fact.get("concept", "")) != concept:
            continue
        if str(fact.get("fiscal_year", "")) != str(year):
            continue
        fact_period = _period(fact.get("fiscal_period"))
        if period is None:
            if fact_period not in _ANNUAL_PERIODS:
                continue
        elif fact_period != period:
            continue
        if str(fact.get("usage_status", "")).casefold() == "comparator":
            continue
        if fact.get("value") is None:
            continue
        candidates.append(fact)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (str(item.get("filed_at", "")), str(item.get("fact_id", ""))),
    )


def _normalized_scope(fact: dict[str, Any] | None) -> str:
    if not fact:
        return ""
    value = fact.get("scope") or fact.get("consolidated_scope") or ""
    if isinstance(value, bool):
        return "consolidated" if value else "parent"
    normalized = str(value).strip().casefold().replace("_", " ")
    if normalized in {"consolidated", "group", "合并", "合併", "綜合"}:
        return "consolidated"
    if normalized in {"parent", "parent company", "issuer", "母公司", "母公司口径", "母公司口徑"}:
        return "parent"
    return ""


def _cash_conversion_metric(
    facts: list[dict[str, Any]],
    *,
    year: int,
    period: str | None,
    net_income: float | None,
    operating_cash_flow: float | None,
) -> tuple[float | None, str, str]:
    """Calculate OCF/net income only for meaningful, compatible inputs."""
    net_fact = _selected_period_fact(facts, "net_income", year, period)
    cash_fact = _selected_period_fact(facts, "operating_cash_flow", year, period)
    if net_income is None or net_fact is None:
        return None, "net_income_unavailable", ""
    if net_income <= 0:
        return None, "not_applicable_nonpositive_earnings", ""
    if operating_cash_flow is None or cash_fact is None:
        return None, "operating_cash_flow_unavailable", ""
    net_currency = str(net_fact.get("currency") or net_fact.get("unit") or "").strip().upper()
    cash_currency = str(cash_fact.get("currency") or cash_fact.get("unit") or "").strip().upper()
    if not net_currency or not cash_currency:
        return None, "currency_unverified", ""
    if net_currency != cash_currency:
        return None, "currency_mismatch", ""
    net_end = str(net_fact.get("end_date") or "").strip()
    cash_end = str(cash_fact.get("end_date") or "").strip()
    if net_end and cash_end and net_end != cash_end:
        return None, "period_mismatch", ""
    net_scope = _normalized_scope(net_fact)
    cash_scope = _normalized_scope(cash_fact)
    if net_scope and net_scope == cash_scope:
        return operating_cash_flow / net_income, "calculated", net_scope
    return operating_cash_flow / net_income, "approximate_scope", ""


def _format_cash_conversion(row: dict[str, Any], language: str) -> str:
    value = row.get("cash_conversion")
    status = str(row.get("cash_conversion_status", ""))
    if value is not None:
        formatted = format_percent(value)
        if status == "approximate_scope":
            return (
                f"Approximate (scope differs or is unverified): {formatted}"
                if normalize_language(language) == EN
                else f"近似值（口径不一致或未核实）：{formatted}"
            )
        return formatted
    messages = {
        "not_applicable_nonpositive_earnings": (
            "Not applicable (net income is not positive; see the original net income and operating cash flow above.)",
            "不适用（净利润不为正；净利润与经营现金流原值见上表。）",
            "不適用（淨利潤不為正；淨利潤與經營現金流原值見上表。）",
        ),
        "currency_unverified": (
            "Not calculated (input currency is unverified.)",
            "未计算（输入币种未核实。）",
            "未計算（輸入幣別未核實。）",
        ),
        "currency_mismatch": (
            "Not calculated (input currencies differ.)",
            "未计算（输入币种不一致。）",
            "未計算（輸入幣別不一致。）",
        ),
        "period_mismatch": (
            "Not calculated (input period end dates differ.)",
            "未计算（输入期间截止日期不一致。）",
            "未計算（輸入期間截止日期不一致。）",
        ),
        "net_income_unavailable": (
            "Not available (net income source is missing.)",
            "暂无（缺少净利润来源。）",
            "暫無（缺少淨利潤來源。）",
        ),
        "operating_cash_flow_unavailable": (
            "Not available (operating cash-flow source is missing.)",
            "暂无（缺少经营现金流来源。）",
            "暫無（缺少經營現金流來源。）",
        ),
    }
    fallback = ("Not available.", "暂无。", "暫無。")
    index = 2 if normalize_language(language) == ZH_HANT else 0 if normalize_language(language) == EN else 1
    return messages.get(status, fallback)[index]


def _cash_conversion_definition(row: dict[str, Any], language: str) -> str:
    locale = normalize_language(language)
    scope = str(row.get("cash_conversion_scope", ""))
    status = str(row.get("cash_conversion_status", ""))
    if status == "approximate_scope":
        label = (
            "scope differs or is unverified"
            if locale == EN
            else "口徑不一致或未核實"
            if locale == ZH_HANT
            else "口径不一致或未核实"
        )
    elif scope == "consolidated":
        label = "consolidated" if locale == EN else "合併口徑" if locale == ZH_HANT else "合并口径"
    elif scope == "parent":
        label = "parent-company scope" if locale == EN else "母公司口徑" if locale == ZH_HANT else "母公司口径"
    else:
        label = "scope unverified" if locale == EN else "口徑未核實" if locale == ZH_HANT else "口径未核实"
    return (
        f"Operating cash flow ÷ net income; {label}."
        if locale == EN
        else f"經營現金流 ÷ 淨利潤；{label}。"
        if locale == ZH_HANT
        else f"经营现金流 ÷ 净利润；{label}。"
    )


def format_cash_conversion(row: dict[str, Any], language: str = "zh-CN") -> str:
    """Localized display of the cash-conversion ratio or its explicit gap."""
    return _format_cash_conversion(row, language)


def cash_conversion_definition(row: dict[str, Any], language: str = "zh-CN") -> str:
    """Localized definition and scope for the cash-conversion metric."""
    return _cash_conversion_definition(row, language)


def growth_rate(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0 or current < 0:
        return None
    return current / previous - 1


def growth_status(current: float | None, previous: float | None) -> str:
    """Describe comparison semantics without inventing percentages for losses."""
    if current is None or previous is None:
        return "comparison_unavailable"
    if previous == 0:
        return "zero_base"
    if previous > 0 and current < 0:
        return "turned_to_loss"
    if previous < 0 and current >= 0:
        return "turnaround"
    if previous < 0 and current < 0:
        if abs(current) < abs(previous):
            return "loss_narrowed"
        if abs(current) > abs(previous):
            return "loss_widened"
        return "loss_unchanged"
    return "rate"


_GROWTH_STATUS_TEXT: dict[str, tuple[str, str, str]] = {
    "turned_to_loss": ("转为亏损", "轉為虧損", "Turned to loss"),
    "turnaround": ("扭亏为盈", "轉虧為盈", "Turned profitable"),
    "loss_narrowed": ("亏损收窄", "虧損收窄", "Loss narrowed"),
    "loss_widened": ("亏损扩大", "虧損擴大", "Loss widened"),
    "loss_unchanged": ("亏损持平", "虧損持平", "Loss unchanged"),
    "zero_base": ("基数为零，不适用", "基數為零，不適用", "Not meaningful on a zero base"),
    "comparison_unavailable": ("缺少可比数据", "缺少可比資料", "Comparable data unavailable"),
}


def format_growth(value: float | None, status: object, language: str = "zh-CN") -> str:
    """Render a growth comparison without turning loss transitions into rates."""
    if value is not None:
        return format_percent(value)
    locale = normalize_language(language)
    index = 2 if locale == EN else 1 if locale == ZH_HANT else 0
    return _GROWTH_STATUS_TEXT.get(str(status), ("—", "—", "—"))[index]


def _comparison_baseline(
    previous: dict[str, float], records: list[dict[str, Any]],
) -> tuple[dict[str, float], list[dict[str, Any]], str | None, bool]:
    """Prefer issuer-stated comparators independently for every concept.

    A later filing can restate revenue, profit and cash-flow rows together.  A
    revenue-only override produces internally inconsistent growth metrics, so
    selection happens per concept and keeps every selected fact id auditable.
    """
    by_concept: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in records:
        concept = str(fact.get("concept") or "")
        if concept and fact.get("value") is not None:
            by_concept[concept].append(fact)
    selected: list[dict[str, Any]] = []
    has_comparator = False
    restatement_available = False
    for concept, candidates in by_concept.items():
        comparators = [
            item for item in candidates
            if str(item.get("usage_status", "")).casefold() == "comparator"
        ]
        preferred = comparators or [
            item for item in candidates
            if str(item.get("usage_status", "")).casefold() != "comparator"
        ]
        if not preferred:
            continue
        chosen = max(
            preferred,
            key=lambda item: (str(item.get("filed_at", "")), str(item.get("fact_id", ""))),
        )
        selected.append(chosen)
        if comparators:
            has_comparator = True
            standalone = [item for item in candidates if item not in comparators]
            restatement_available = restatement_available or bool(
                standalone
                and any(float(item["value"]) != float(chosen["value"]) for item in standalone)
            )
    baseline = {
        **previous,
        **{str(item["concept"]): float(item["value"]) for item in selected},
    }
    source = (
        "same_filing_comparator" if has_comparator
        else "independent_historical" if selected
        else None
    )
    return baseline, selected, source, restatement_available


def _equity_value(values: dict[str, float]) -> float | None:
    """Normalize parser/source aliases without losing the reported concept."""
    equity = values.get("equity")
    return equity if equity is not None else values.get("total_equity")


def _annual_roe_details(
    values: dict[str, float], previous: dict[str, float]
) -> dict[str, Any]:
    net_income = values.get("net_income")
    closing_equity = _equity_value(values)
    opening_equity = _equity_value(previous)
    reported_roe = values.get("reported_roe")
    inputs = {
        "net_income": net_income,
        "opening_equity": opening_equity,
        "closing_equity": closing_equity,
    }
    average_equity = (
        (opening_equity + closing_equity) / 2
        if opening_equity is not None and closing_equity is not None
        else None
    )
    if closing_equity is not None and closing_equity <= 0:
        gap = "non_positive_equity"
    elif average_equity is not None and average_equity <= 0:
        gap = "non_positive_equity"
    elif reported_roe is not None:
        return {
            "return_on_equity": reported_roe,
            "return_on_equity_basis": "reported_weighted_average",
            "return_on_equity_formula": "reported_roe",
            "return_on_equity_inputs": inputs,
            "return_on_equity_gap": None,
        }
    elif net_income is None:
        gap = "missing_net_income"
    elif closing_equity in (None, 0):
        gap = "missing_equity"
    elif opening_equity not in (None, 0):
        return {
            "return_on_equity": net_income / average_equity,
            "return_on_equity_basis": "average_equity",
            "return_on_equity_formula": "net_income / average(opening_equity, closing_equity)",
            "return_on_equity_inputs": inputs,
            "return_on_equity_gap": None,
        }
    else:
        return {
            "return_on_equity": net_income / closing_equity,
            "return_on_equity_basis": "ending_equity_proxy",
            "return_on_equity_formula": "net_income / closing_equity",
            "return_on_equity_inputs": inputs,
            "return_on_equity_gap": None,
        }
    return {
        "return_on_equity": None,
        "return_on_equity_basis": None,
        "return_on_equity_formula": None,
        "return_on_equity_inputs": inputs,
        "return_on_equity_gap": gap,
    }


def calculate_metrics(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matrix = latest_by_year(facts)
    metadata: dict[int, tuple[str, str]] = {}
    monetary_metadata: dict[tuple[int, str], tuple[str, str]] = {}
    monetary_rank: dict[tuple[int, str], tuple[int, str, str]] = {}
    money_fact_metadata: dict[tuple[int, str], dict[str, Any]] = {}
    for fact in facts:
        if _period(fact.get("fiscal_period")) not in _ANNUAL_PERIODS:
            continue
        try:
            year = int(fact["fiscal_year"])
        except (TypeError, ValueError):
            continue
        filed_at = str(fact.get("filed_at", ""))
        period_end = str(fact.get("end_date", ""))
        concept = str(fact.get("concept", ""))
        previous = metadata.get(year, ("", ""))
        metadata[year] = (max(previous[0], filed_at), max(previous[1], period_end))
        currency = str(fact.get("currency") or fact.get("unit") or "").upper()
        provenance = str(fact.get("unit_provenance") or "")
        money_key = (year, concept)
        priority = 0 if str(fact.get("usage_status", "")).casefold() == "comparator" else 1
        rank = (priority, filed_at, str(fact.get("fact_id", "")))
        if rank >= monetary_rank.get(money_key, (-1, "", "")):
            money_fact_metadata[money_key] = _fact_money_metadata(fact)
            monetary_rank[money_key] = rank
        if currency and provenance and provenance != "unknown":
            monetary_metadata[money_key] = (currency, provenance)
    visible_years = {
        int(fact["fiscal_year"])
        for fact in facts
        if _period(fact.get("fiscal_period")) in _ANNUAL_PERIODS
        and str(fact.get("usage_status", "")).casefold() != "comparator"
        and str(fact.get("fiscal_year", "")).isdigit()
    }
    # Comparative columns remain available in ``matrix`` as a calculation
    # baseline, but never become standalone report rows.
    years = sorted((year for year in matrix if year in visible_years), reverse=True)
    results: list[dict[str, Any]] = []
    for index, year in enumerate(years):
        values = matrix[year]
        comparison_year = year - 1 if year - 1 in matrix else None
        previous = matrix.get(comparison_year, {}) if comparison_year is not None else {}
        revenue = values.get("revenue")
        gross_profit = values.get("gross_profit")
        cost_of_revenue = values.get("cost_of_revenue")
        if (
            gross_profit is None
            and revenue is not None
            and revenue >= 0
            and cost_of_revenue is not None
            and cost_of_revenue >= 0
        ):
            gross_profit = revenue - cost_of_revenue
        operating_income = values.get("operating_income")
        net_income = values.get("net_income")
        operating_cash_flow = values.get("operating_cash_flow")
        capex = values.get("capital_expenditure")
        assets = values.get("assets")
        liabilities = values.get("liabilities")
        roe_details = _annual_roe_details(values, previous)
        operating_meta = money_fact_metadata.get((year, "operating_cash_flow"))
        capex_meta = money_fact_metadata.get((year, "capital_expenditure"))
        fcf_inputs_comparable = _compatible_annual_money_inputs(operating_meta, capex_meta)
        candidate_free_cash_flow = (
            operating_cash_flow - capex
            if operating_cash_flow is not None and capex is not None
            else None
        )
        free_cash_flow = candidate_free_cash_flow if fcf_inputs_comparable else None
        free_cash_flow_gap = None if free_cash_flow is not None else _free_cash_flow_gap_reason(
            operating_cash_flow, capex, operating_meta, capex_meta,
        )
        cash_conversion, cash_conversion_status, cash_conversion_scope = (
            _cash_conversion_metric(
                facts,
                year=year,
                period=None,
                net_income=net_income,
                operating_cash_flow=operating_cash_flow,
            )
        )
        comparison_records = [
            fact for fact in facts
            if str(fact.get("fiscal_period", "")).upper() in _ANNUAL_PERIODS
            and fact.get("fiscal_year") == comparison_year
        ] if comparison_year is not None else []
        previous, comparison_selected, comparison_source, restatement_available = (
            _comparison_baseline(previous, comparison_records)
        )
        net_income_growth = growth_rate(net_income, previous.get("net_income"))
        operating_cash_flow_growth = growth_rate(
            operating_cash_flow, previous.get("operating_cash_flow")
        )
        tension = _financial_tension(
            year,
            net_income_growth,
            operating_cash_flow_growth,
            money_fact_metadata,
            comparison_selected,
        )
        # Recompute ROE inputs after selecting an issuer-stated comparative
        # column, while keeping that column out of visible rows.
        roe_details = _annual_roe_details(values, previous)
        typed_money_metadata: dict[str, dict[str, Any]] = {}
        for concept in (
            "revenue", "net_income", "operating_cash_flow", "assets",
            "liabilities", "equity", "total_equity",
        ):
            fact_metadata = money_fact_metadata.get((year, concept))
            if values.get(concept) is None or fact_metadata is None:
                continue
            typed_money_metadata[concept] = {
                **fact_metadata,
                "evidence_ids": [f"fact:{fact_metadata['fact_id']}"]
                if fact_metadata.get("fact_id") else [],
            }
        if free_cash_flow is not None and operating_meta and capex_meta:
            if fcf_inputs_comparable:
                typed_money_metadata["free_cash_flow"] = {
                    "currency": operating_meta["currency"],
                    "unit_scale": 1.0,
                    "unit_provenance": "derived_from_normalized_inputs",
                    "scope": operating_meta["scope"],
                    "accession_number": operating_meta.get("accession_number", ""),
                    "generation_id": operating_meta.get("generation_id", ""),
                    "period": operating_meta.get("period", ""),
                    "period_end": operating_meta.get("period_end", ""),
                    "fact_id": "",
                    "input_fact_ids": [operating_meta["fact_id"], capex_meta["fact_id"]],
                    "evidence_ids": list(dict.fromkeys(
                        [
                            f"fact:{operating_meta['fact_id']}"
                            for _ in (0,) if operating_meta.get("fact_id")
                        ] + [
                            f"fact:{capex_meta['fact_id']}"
                            for _ in (0,) if capex_meta.get("fact_id")
                        ]
                    )),
                }
        results.append(
            {
                "year": year,
                "filed_at": metadata.get(year, ("", ""))[0],
                "period_end": metadata.get(year, ("", ""))[1],
                **values,
                "revenue_growth": growth_rate(revenue, previous.get("revenue")),
                "revenue_growth_status": growth_status(revenue, previous.get("revenue")),
                "comparison_year": comparison_year,
                "comparison_gap": (
                    None
                    if previous.get("revenue") is not None
                    else "prior_revenue_unavailable"
                    if comparison_year is not None
                    else f"missing_{year - 1}"
                ),
                "comparison_source": comparison_source,
                "comparison_basis": comparison_source,
                "comparison_fact_ids": [
                    str(item.get("fact_id")) for item in comparison_selected
                    if item.get("fact_id")
                ],
                "restatement_available": restatement_available,
                "operating_margin": safe_divide(operating_income, revenue),
                "gross_margin": safe_divide(gross_profit, revenue),
                "gross_profit_basis": (
                    "reported"
                    if values.get("gross_profit") is not None
                    else "revenue_minus_cost_of_revenue"
                    if gross_profit is not None
                    else None
                ),
                "operating_income_growth": growth_rate(operating_income, previous.get("operating_income")),
                "operating_income_growth_status": growth_status(operating_income, previous.get("operating_income")),
                "net_income_growth": net_income_growth,
                "net_income_growth_status": growth_status(net_income, previous.get("net_income")),
                "operating_cash_flow_growth": operating_cash_flow_growth,
                "operating_cash_flow_growth_status": growth_status(operating_cash_flow, previous.get("operating_cash_flow")),
                "net_margin": safe_divide(net_income, revenue),
                "cash_conversion": cash_conversion,
                "cash_conversion_status": cash_conversion_status,
                "cash_conversion_scope": cash_conversion_scope,
                "cash_conversion_formula": "operating_cash_flow / net_income",
                "free_cash_flow": free_cash_flow,
                "free_cash_flow_gap": free_cash_flow_gap,
                "financial_tensions": [tension] if tension is not None else [],
                **(
                    {
                        "free_cash_flow_money": {
                            "value": free_cash_flow,
                            "currency": operating_meta["currency"],
                            "unit_scale": 1.0,
                            "unit_provenance": "normalized",
                            "source_fact_ids": [operating_meta["fact_id"], capex_meta["fact_id"]],
                            "evidence_ids": [
                                f"fact:{operating_meta['fact_id']}",
                                f"fact:{capex_meta['fact_id']}",
                            ],
                        }
                    }
                    if free_cash_flow is not None
                    and fcf_inputs_comparable
                    else {}
                ),
                "debt_to_assets": safe_divide(liabilities, assets),
                **roe_details,
                "money_metadata": typed_money_metadata,
            }
        )
    return results


def calculate_interim_metrics(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calculate period-aware interim snapshots and like-for-like growth.

    Revenue growth is computed only against the same fiscal period in the prior
    year (Q1 vs Q1, H1 vs H1, and so on).  No interim value is annualized.
    """

    matrix, period_ends = _latest_by_interim_period(facts)
    visible_keys = {
        (int(fact["fiscal_year"]), _period(fact.get("fiscal_period")))
        for fact in facts
        if _period(fact.get("fiscal_period")) not in _ANNUAL_PERIODS
        and str(fact.get("usage_status", "")).casefold() != "comparator"
        and str(fact.get("fiscal_year", "")).isdigit()
    }
    # Keep comparator cohorts in the matrix as a calculation baseline only.
    keys = sorted(
        (key for key in matrix if key in visible_keys),
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
        equity = _equity_value(values)
        reported_roe = values.get("reported_roe")
        cash_conversion, cash_conversion_status, cash_conversion_scope = (
            _cash_conversion_metric(
                facts,
                year=year,
                period=period,
                net_income=net_income,
                operating_cash_flow=operating_cash_flow,
            )
        )
        comparison_records = [
            fact for fact in facts
            if fact.get("fiscal_year") == year - 1
            and _period(fact.get("fiscal_period")) == period
        ]
        previous, comparison_selected, comparison_source, restatement_available = (
            _comparison_baseline(previous, comparison_records)
        )
        results.append(
            {
                "year": year,
                "period": period,
                "period_end": period_ends.get((year, period), ""),
                "comparison_period": f"{year - 1} {period}" if previous else None,
                "comparison_gap": (
                    None
                    if previous.get("revenue") is not None
                    else "prior_revenue_unavailable"
                    if previous
                    else "prior_period_unavailable"
                ),
                "comparison_source": comparison_source,
                "comparison_basis": comparison_source,
                "comparison_fact_ids": [
                    str(item.get("fact_id")) for item in comparison_selected
                    if item.get("fact_id")
                ],
                "restatement_available": restatement_available,
                **values,
                "revenue_growth": growth_rate(revenue, previous.get("revenue")),
                "revenue_growth_status": growth_status(revenue, previous.get("revenue")),
                "operating_income_growth": growth_rate(operating_income, previous.get("operating_income")),
                "operating_income_growth_status": growth_status(operating_income, previous.get("operating_income")),
                "net_income_growth": growth_rate(net_income, previous.get("net_income")),
                "net_income_growth_status": growth_status(net_income, previous.get("net_income")),
                "operating_cash_flow_growth": growth_rate(operating_cash_flow, previous.get("operating_cash_flow")),
                "operating_cash_flow_growth_status": growth_status(operating_cash_flow, previous.get("operating_cash_flow")),
                "operating_margin": safe_divide(operating_income, revenue),
                "net_margin": safe_divide(net_income, revenue),
                "cash_conversion": cash_conversion,
                "cash_conversion_status": cash_conversion_status,
                "cash_conversion_scope": cash_conversion_scope,
                "cash_conversion_formula": "operating_cash_flow / net_income",
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


def _format_roe(metric: dict[str, Any], language: str) -> str:
    value = metric.get("return_on_equity")
    if value is not None:
        suffix = {
            "en": " (ending-equity proxy)",
            "zh-CN": "（期末权益近似）",
            "zh-Hant": "（期末權益近似）",
        }.get(language, "") if metric.get("return_on_equity_basis") == "ending_equity_proxy" else ""
        return format_percent(value) + suffix
    gap = str(metric.get("return_on_equity_gap") or "")
    reasons = {
        "missing_net_income": {
            "en": "net income is missing",
            "zh-CN": "缺少净利润数据",
            "zh-Hant": "缺少淨利潤資料",
        },
        "missing_equity": {
            "en": "equity data is missing",
            "zh-CN": "缺少权益数据",
            "zh-Hant": "缺少權益資料",
        },
        "non_positive_equity": {
            "en": "equity is zero or negative; not applicable",
            "zh-CN": "权益为零或负数，不适用",
            "zh-Hant": "權益為零或負數，不適用",
        },
    }
    reason = reasons.get(gap, {}).get(language, "")
    if not reason:
        return "—"
    return f"— ({reason})" if language == "en" else f"—（{reason}）"


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
    # Start with an ordinary range, then expand monotonically only as far as
    # the DCF remains economically interpretable.  A fixed [-60%, 150%] gate
    # incorrectly classified valid high-growth cases as missing data.
    low, high = -0.60, 0.50
    low_value = discounted_cash_flow_value(
        base_free_cash_flow, low, discount_rate, terminal_growth, horizon_years
    )
    high_value = discounted_cash_flow_value(
        base_free_cash_flow, high, discount_rate, terminal_growth, horizon_years
    )
    while market_cap < low_value and low > -0.99:
        low = max(-0.99, low - max(0.10, (low + 1.0) / 2.0))
        low_value = discounted_cash_flow_value(
            base_free_cash_flow, low, discount_rate, terminal_growth, horizon_years
        )
    while market_cap > high_value and high < 10.0:
        high = min(10.0, high * 1.75 + 0.10)
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
    market_cap: float | NormalizedMoney | dict[str, Any],
    discount_rate: float = 0.10,
    terminal_growth: float = 0.03,
    horizon_years: int = 5,
    *,
    market_as_of: str = "",
    currency: str = "",
    policy_version: str = "reverse-dcf-policy-v1",
    strict: bool = False,
    require_typed: bool | None = None,
) -> dict[str, Any]:
    # ``strict`` is the semantic switch; ``require_typed`` is the explicit
    # name used by the authoritative research boundary.  Keep both keyword
    # spellings so compatibility callers can opt in without changing the
    # legacy positional API.
    strict_mode = bool(strict or require_typed)
    invalid_fields: list[str] = []
    numeric_parameters = {
        "discount_rate": discount_rate,
        "terminal_growth": terminal_growth,
    }
    for name, value in numeric_parameters.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            invalid_fields.append(name)
    if (
        isinstance(horizon_years, bool)
        or not isinstance(horizon_years, int)
        or horizon_years < 1
    ):
        invalid_fields.append("horizon_years")
    if not invalid_fields and float(discount_rate) <= float(terminal_growth):
        invalid_fields.extend(("discount_rate", "terminal_growth"))
    if invalid_fields:
        return {
            "status": "invalid_parameters",
            "reason": "invalid_reverse_dcf_parameters",
            "invalid_fields": list(dict.fromkeys(invalid_fields)),
            "policy_version": policy_version,
        }
    if not metrics:
        return {"status": "insufficient_data", "reason": "没有财务指标"}
    market_money = _coerce_money(market_cap, fallback_currency=currency)
    if strict_mode and not _has_explicit_normalized_money(market_cap):
        return {
            "status": "valuation_unit_mismatch",
            "reason": "市场价值必须是带可验证归一化来源的金额",
            "currency": currency,
            "policy_version": policy_version,
        }
    if market_money is None or market_money.normalized_value <= 0:
        return {
            "status": "valuation_unit_mismatch",
            "reason": "市场价值缺少可验证的金额单位或归一化来源",
            "currency": currency,
            "policy_version": policy_version,
        }
    market_currency = market_money.currency or str(currency or "").upper()
    # Keep only complete fiscal-year rows for the FCFE proxy.  Intermediate
    # Q1/H1/Q3 and YTD rows remain available to other analysis, but must not
    # be mistaken for a full-year base simply because they sort first.
    eligible = [
        row for row in metrics
        if _reverse_dcf_is_complete_fy(row)
        and (not market_as_of or not row.get("filed_at") or str(row.get("filed_at", ""))[:10] <= market_as_of)
    ]
    eligible.sort(key=lambda row: (
        str(row.get("filed_at") or "")[:10],
        str(row.get("end_date") or ""),
        int(row.get("year") or 0),
    ), reverse=True)
    base_metric = eligible[0] if eligible else None
    base_fcf_value = base_metric.get("free_cash_flow") if base_metric else None
    base_money = _coerce_money(
        base_metric.get("free_cash_flow_money") if base_metric else None,
        fallback_currency=str(base_metric.get("free_cash_flow_currency", currency)) if base_metric else currency,
    ) if base_metric and base_metric.get("free_cash_flow_money") is not None else None
    if strict_mode and (
        not _has_explicit_normalized_money(
            base_metric.get("free_cash_flow_money") if base_metric else None
        )
        or
        base_metric is None
        or base_money is None
        or base_money.unit_provenance != "normalized"
        or base_money.unit_scale != 1.0
    ):
        return {
            "status": "valuation_unit_mismatch",
            "reason": "自由现金流必须来自已验证的归一化金额对象",
            "currency": market_currency,
            "policy_version": policy_version,
        }
    # A metric carrying a table scale but no explicit normalized-money object
    # is ambiguous at this boundary.  Reject it instead of guessing whether
    # the parser already applied the scale.
    if base_metric and base_money is None and base_metric.get("free_cash_flow_unit_scale") not in (None, 1, 1.0):
        return {
            "status": "valuation_unit_mismatch",
            "reason": "自由现金流同时携带裸值与未声明的归一化边界",
            "currency": market_currency,
            "policy_version": policy_version,
        }
    if base_money is None:
        base_money = _coerce_money(base_fcf_value, fallback_currency=str(currency or market_currency))
    if base_money is None or base_money.normalized_value <= 0:
        return {
            "status": "insufficient_data",
            "reason": "行情日之前没有正的 FCFE proxy，无法使用权益反向 DCF",
            "cash_flow_basis": "FCFE proxy = operating cash flow - capital expenditure",
            "market_as_of": market_as_of,
            "policy_version": policy_version,
        }
    base_fcf = base_money.normalized_value
    normalized_market_cap = market_money.normalized_value
    if base_money.currency and market_currency and base_money.currency.upper() != market_currency.upper():
        return {
            "status": "valuation_currency_mismatch",
            "reason": "市场价值与自由现金流币种不一致",
            "currency": market_currency,
            "policy_version": policy_version,
        }
    implied = implied_fcf_growth(
        normalized_market_cap,
        base_fcf,
        discount_rate,
        terminal_growth,
        horizon_years,
    )
    sensitivity = [
        {
            "fcf_growth": growth,
            "equity_value": discounted_cash_flow_value(
                base_fcf,
                growth,
                discount_rate,
                terminal_growth,
                horizon_years,
            ),
        }
        for growth in (-0.05, 0.0, 0.05, 0.10, 0.15, 0.20, 0.30)
    ]
    status = (
        "no_economic_solution" if implied is None else
        "extreme_assumption" if implied < -0.60 or implied > 1.50 else
        "ok"
    )
    return {
        "status": status,
        # Keep market_cap as a wire-compatibility alias.  The semantic target
        # is equity market value, never enterprise value.
        "market_cap": normalized_market_cap,
        "equity_market_value": normalized_market_cap,
        "base_free_cash_flow": base_fcf,
        "base_fcf_period": (
            f"{base_metric.get('year')} {base_metric.get('period')}".strip()
            if base_metric and base_metric.get("period") else str(base_metric.get("year", "")) if base_metric else ""
        ),
        "base_fcf_filed_at": str(base_metric.get("filed_at", "")) if base_metric else "",
        "cash_flow_basis": "FCFE proxy = operating cash flow - capital expenditure",
        "market_as_of": market_as_of,
        "currency": market_currency,
        "market_value_source_id": market_money.source_id,
        "market_value_source_as_of": market_money.as_of,
        "policy_version": policy_version,
        "discount_rate": discount_rate,
        "terminal_growth": terminal_growth,
        "horizon_years": horizon_years,
        "implied_fcf_growth": implied,
        "sensitivity": sensitivity,
        "limitations": [
            "权益市值与 FCFE proxy 同口径；该 proxy 未替代完整的股东现金流建模。",
            "模型假设显性预测期内 FCFE proxy 按固定速度增长。",
            "该结果用于解释市场隐含预期，不是目标价。",
        ],
    }


def _reverse_dcf_is_complete_fy(row: dict[str, Any]) -> bool:
    period = str(row.get("period") or row.get("fiscal_period") or "").strip().upper()
    if not period:
        return True
    return period in {"FY", "ANNUAL", "12M", "FULL_YEAR", "FULL-YEAR"}


def deterministic_summary(
    company_name: str,
    metrics: list[dict[str, Any]],
    language: str = "zh-CN",
    currency: str = "USD",
) -> str:
    """Render deterministic metrics from one explicit, coverage-aware schema."""
    english = normalize_language(language) == EN
    traditional = normalize_language(language) == ZH_HANT
    heading = (
        f"# {company_name} Financial Overview"
        if english and company_name
        else "# Financial Overview"
        if english
        else f"# {company_name} {'財務概覽' if traditional else '财务概览'}"
        if company_name
        else "# 財務概覽"
        if traditional
        else "# 财务概览"
    )
    lines = (
        [
            heading,
            "",
            "The following content was generated by the deterministic financial engine.",
            "",
        ]
        if english
        else [heading, "", "\u4ee5\u4e0b\u5167\u5bb9\u7531\u78ba\u5b9a\u6027\u8ca1\u52d9\u5f15\u64ce\u751f\u6210\u3002" if traditional else "以下内容由确定性财务引擎生成。", ""]
    )
    if not metrics:
        lines.append(
            "No normalized annual financial data is available."
            if english
            else "沒有可用的標準化年度財務資料。"
            if traditional
            else "没有可用的标准化年度财务数据。"
        )
        return "\n".join(lines)

    has_operating_margin = any(row.get("operating_margin") is not None for row in metrics)
    has_free_cash_flow = any(row.get("free_cash_flow") is not None for row in metrics)
    columns: list[tuple[str, str, str]] = [
        ("year", "Fiscal year", "\u8d22\u5e74" if not traditional else "\u8ca1\u653f\u5e74\u5ea6"),
        ("revenue", "Revenue", "\u8425\u4e1a\u6536\u5165" if not traditional else "\u71df\u696d\u6536\u5165"),
        ("revenue_growth", "Revenue growth", "\u6536\u5165\u589e\u9577" if traditional else "\u6536\u5165\u589e\u957f"),
    ]
    if has_operating_margin:
        columns.append(("operating_margin", "Operating margin", "\u71df\u696d\u5229\u6f64\u7387" if traditional else "\u8425\u4e1a\u5229\u6da6\u7387"))
    columns.extend([
        ("net_income", "Net income", "\u6de8\u5229\u6f64" if traditional else "\u51c0\u5229\u6da6"),
        ("operating_cash_flow", "Operating cash flow", "\u7d93\u71df\u73fe\u91d1\u6d41" if traditional else "\u7ecf\u8425\u73b0\u91d1\u6d41"),
    ])
    if has_free_cash_flow:
        columns.append(("free_cash_flow", "Free cash flow", "\u81ea\u7531\u73fe\u91d1\u6d41" if traditional else "\u81ea\u7531\u73b0\u91d1\u6d41"))
    labels = [column[1] if english else column[2] for column in columns]
    lines.append("| " + " | ".join(labels) + " |")
    lines.append("|" + "|".join("---:" for _ in columns) + "|")
    for row in metrics[:5]:
        values: list[str] = []
        for key, _, _ in columns:
            value = row.get(key)
            if key == "year":
                values.append(str(value))
            elif key in {"revenue", "net_income", "operating_cash_flow", "free_cash_flow"}:
                values.append(format_money(value, currency))
            elif key.endswith("_growth"):
                values.append(format_growth(value, row.get(f"{key}_status"), language))
            else:
                values.append(format_percent(value))
        lines.append("| " + " | ".join(values) + " |")

    latest = metrics[0]
    metric_details = (
        [
            ("Gross margin", "毛利率", "毛利率", format_percent(latest.get("gross_margin"))),
            ("Operating income growth", "營業利潤增長", "营业利润增长", format_growth(latest.get("operating_income_growth"), latest.get("operating_income_growth_status"), language)),
            ("Net income growth", "淨利潤增長", "净利润增长", format_growth(latest.get("net_income_growth"), latest.get("net_income_growth_status"), language)),
            ("Operating cash-flow growth", "經營現金流增長", "经营现金流增长", format_growth(latest.get("operating_cash_flow_growth"), latest.get("operating_cash_flow_growth_status"), language)),
        ]
    )
    visible_details = [
        item for item in metric_details
        if item[3] != "—" or item[0] == "Gross margin" and latest.get("gross_margin") is not None
    ]
    if visible_details:
        lines.extend(["", "## " + ("Growth and Quality Metrics" if english else "成長與品質指標" if traditional else "增长与质量指标"), ""])
        for en_label, hant_label, zh_label, value in visible_details:
            label = en_label if english else hant_label if traditional else zh_label
            lines.append(f"- {label}: {value}" if english else f"- {label}：{value}")
    cash_conversion_text = format_cash_conversion(latest, language)
    cash_conversion_formula = cash_conversion_definition(latest, language)
    if english:
        lines.extend([
            "",
            "## Latest Fiscal-Year Deterministic Metrics",
            "",
            f"- Cash conversion: {cash_conversion_text}",
            f"- Definition: {cash_conversion_formula}",
            f"- Debt to assets: {format_percent(latest.get('debt_to_assets'))}",
            f"- Return on equity: {_format_roe(latest, 'en')}",
            "",
            "> These metrics are research inputs, not investment advice.",
        ])
    elif traditional:
        lines.extend([
            "", "## \u6700\u65b0\u8ca1\u5e74\u78ba\u5b9a\u6027\u6307\u6a19", "",
            f"- \u73fe\u91d1\u5229\u6f64\u8f49\u5316\u7387：{cash_conversion_text}",
            f"- \u8a08\u7b97\u65b9\u5f0f：{cash_conversion_formula}",
            f"- \u8cc7\u7522\u8ca0\u50b5\u7387：{format_percent(latest.get('debt_to_assets'))}",
            f"- \u6de8\u8cc7\u7522\u5831\u916c\u7387：{_format_roe(latest, 'zh-Hant')}", "",
            "> \u9019\u4e9b\u6307\u6a19\u50c5\u4f5c\u70ba\u7814\u7a76\u8f38\u5165\uff0c\u4e0d\u69cb\u6210\u6295\u8cc7\u5efa\u8b70\u3002",
        ])
    else:
        lines.extend([
            "",
            "## 最新财年确定性指标",
            "",
            f"- 现金利润转化率：{cash_conversion_text}",
            f"- 计算方式：{cash_conversion_formula}",
            f"- 资产负债率：{format_percent(latest.get('debt_to_assets'))}",
            f"- 净资产收益率：{_format_roe(latest, 'zh-CN')}",
            "",
            "> 这些指标仅作为研究输入，不构成投资建议。",
        ])
    return "\n".join(lines)
