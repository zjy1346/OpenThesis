from __future__ import annotations

from .text_normalization import canonical_search_text


FINANCIAL_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "operating_income": (
        "营业利润",
        "營業利潤",
        "经营利润",
        "經營利潤",
        "operating income",
        "operating loss",
        "operating profit",
        "profit from operations",
        "operating income/(loss)",
    ),
    "gross_profit": (
        "毛利",
        "毛利润",
        "毛利潤",
        "营业毛利",
        "營業毛利",
        "gross profit",
        "gross profit/(loss)",
    ),
    "cost_of_revenue": (
        "营业成本",
        "營業成本",
        "营业总成本",
        "營業總成本",
        "销售成本",
        "銷售成本",
        "cost of revenue",
        "cost of revenues",
        "cost of sales",
    ),
    "capital_expenditure": (
        "购建固定资产、无形资产和其他长期资产支付的现金",
        "购建固定资产、无形资产和其他长期资产所支付的现金",
        "购建固定资产、无形资产及其他长期资产支付的现金",
        "购建固定资产支付的现金",
        "購建固定資產、無形資產和其他長期資產支付的現金",
        "購置物業、廠房及設備款項",
        "購置物業、機器及設備",
        "购置物业、厂房及设备款项",
        "購置無形資產",
        "购置无形资产",
        "资本性支出",
        "資本性支出",
        "capital expenditure",
        "capital expenditures",
        "purchase of property, plant and equipment",
        "purchases of property, plant and equipment and intangible assets",
        "purchases and prepayments of property, plant and equipment and intangible assets",
        "purchase of/prepayments for property, plant and equipment, construction in progress",
        "purchase of/prepayments for intangible assets",
        "purchase of / prepayments for property, plant and equipment, construction in progress",
        "purchase of / prepayments for intangible assets",
    ),
}


def normalize_financial_label(value: object) -> str:
    """Canonicalize label typography while leaving source text untouched."""

    return canonical_search_text(value)


def label_matches(text: object, label: object) -> bool:
    candidate = normalize_financial_label(text)
    wanted = normalize_financial_label(label)
    return bool(wanted and wanted in candidate)


def capex_component_kind(label: object) -> str:
    """Classify a capex row so totals and components are never double counted."""

    normalized = normalize_financial_label(label)
    if any(token in normalized for token in ("无形资产", "intangibleasset")):
        if any(token in normalized for token in ("固定资产", "propertyplantandequipment")):
            return "total"
        return "intangibles"
    if any(token in normalized for token in ("物业厂房及设备", "固定资产", "propertyplantandequipment")):
        return "ppe"
    return "total"
