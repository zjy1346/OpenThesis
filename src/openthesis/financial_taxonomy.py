from __future__ import annotations

import re
from functools import lru_cache
from collections.abc import Sequence

from .text_normalization import canonical_search_text


# Kept independent from the PDF parser contract so the cache layer can remap
# only documents/windows whose saved rows were affected by a concept change.
FINANCIAL_MAPPING_VERSION = "financial-concept-mapping-v2"


FINANCIAL_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "interest_revenue": ("interest revenue", "interest income", "利息收入"),
    "finance_interest_income": ("interest income", "利息收入"),
    "operating_cash_flow": (
        "经营活动产生的现金流量净额", "经营活动所得现金净额",
        "经营活动产生的现金流",
        "net cash generated from operating activities", "net cash flows from operating activities",
        "net cash generated from/used in operating activities",
        "net cash generated from/(used in) operating activities",
        "net cash (used in)/generated from operating activities",
        "net cash flows generated from operating activities",
        "net cash flows (used in)/generated from operating activities",
        "net cash flows generated from/(used in) operating activities",
        "net cash flows generated from/used in operating activities",
        "net cash flow from operating activities",
        "net cash provided by operating activities",
    ),
    "profit_before_tax": (
        "利润总额", "profit before tax", "profit before income tax",
        "(Loss)/profit before income tax", "loss before tax", "loss before income tax",
    ),
    "net_income": (
        "净利润", "淨利潤", "profit for the year", "loss for the year",
        "profit after tax", "net profit", "net income", "net loss",
        "(Loss)/profit for the year", "profit/(loss) for the year",
    ),
    "parent_net_income": (
        "归属于母公司股东的净利润", "归属于母公司所有者的净利润",
        "归属于上市公司股东的净利润", "歸屬於母公司股東的淨利潤",
        "本公司权益持有人应占盈利", "本公司股东应占溢利",
        "net income attributable to owners", "net income attributable to shareholders",
        "net profit attributable to shareholders of the parent company",
        "net income attributable to alibaba group holding limited",
        "profit attributable to owners", "profit attributable to equity holders",
        "profit attributable to shareholders", "profit for the year attributable to owners",
        "equity holders of the company", "owners of the company",
    ),
    "minority_interest_profit": (
        "少数股东损益", "少數股東損益", "少数股东净利润", "非控股权益应占利润",
        "net income attributable to non-controlling interests",
        "profit attributable to non-controlling interests", "non-controlling interests",
        "noncontrolling interests", "minority interests",
    ),
    "ordinary_shareholder_net_income": (
        "net income attributable to ordinary shareholders",
        "profit attributable to ordinary shareholders", "归属于普通股股东的净利润",
    ),
    "equity": (
        "归属于母公司所有者权益合计", "归属于母公司所有者权益（或股东权益）合计",
        "归属于上市公司股东的所有者权益", "归属于母公司所有者权益",
        "归属于母公司股东权益合计", "equity attributable to owners",
        "equity attributable to equity holders of the company",
        "total equity attributable to the parent company",
        "total shareholders' equity attributable to the parent company",
        "total equity attributable to shareholders of the company",
    ),
    "total_equity": (
        "所有者权益合计", "所有者权益（或股东权益）合计", "股东权益合计",
        "total equity", "total shareholders' equity", "total owners' equity",
    ),
    "comprehensive_income": ("total comprehensive income for the year", "total comprehensive income", "综合收益总额"),
    "parent_comprehensive_income": ("owners of the company", "equity holders of the company", "归属于母公司所有者的综合收益"),
    "minority_interest_comprehensive_income": ("non-controlling interests", "noncontrolling interests", "归属于少数股东的综合收益"),
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
        "销售成本",
        "銷售成本",
        "cost of revenue",
        "cost of revenues",
        "cost of sales",
    ),
    "total_operating_cost": (
        "营业总成本",
        "營業總成本",
        "total operating cost",
        "total operating costs",
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


def financial_label_body(label: object) -> str:
    """Remove an anchored list ordinal, not words or source evidence."""
    return re.sub(
        r"^\s*(?:"
        r"[（(]\s*(?:[一二三四五六七八九十百0-9]+|[IVXLCDM]+)\s*[）)]|"
        r"[一二三四五六七八九十百0-9]+[、.．)）]|[IVXLCDM]+[.．]"
        r")\s*",
        "", str(label), flags=re.I,
    )


def is_profit_ownership_heading(label: object) -> bool:
    """Match only the formal ownership-classification table heading."""
    normalized = normalize_financial_label(financial_label_body(label))
    return normalized in {
        "classifiedbyownership", "按所有权分类", "按所有權分類",
    }


def _ownership_row_literal_proof(label: object, raw_row: object) -> bool:
    """Validate a literal numbered ownership heading, leaf and numeric row."""
    ordinal = (
        r"(?:[一二三四五六七八九十百0-9]+[、.．)）]|[IVXLCDM]+[.．]|"
        r"[（(]\s*(?:[一二三四五六七八九十百0-9]+|[IVXLCDM]+)\s*[）)])"
    )
    heading = re.match(
        rf"^\s*(?:{ordinal}\s*)?Classified\s+by\s+ownership(?=\s)(?P<tail>.*)$",
        str(raw_row), flags=re.I,
    )
    if heading is None:
        return False
    number = r"(?:\(\s*[+-]?\d[\d,]*(?:\.\d+)?\s*\)|[+-]?\d[\d,]*(?:\.\d+)?)"
    row = re.fullmatch(
        rf"\s*(?P<leaf>.+?)\s+(?P<values>{number}(?:\s+{number})*)\s*",
        heading.group("tail"), flags=re.I,
    )
    if row is None:
        return False
    leaf_norm = normalize_financial_label(financial_label_body(row.group("leaf")))
    reported_label = str(label)
    headed_label = re.match(
        rf"^\s*(?:{ordinal}\s*)?Classified\s+by\s+ownership(?=\s)(?P<tail>.*)$",
        reported_label, flags=re.I,
    )
    # The positioned fact may retain the formal heading alongside its leaf.
    # In that case, compare only the exact remainder after the anchored title;
    # do not accept prose or value text from the reported label.
    label_leaf = headed_label.group("tail") if headed_label is not None else reported_label
    label_shape = re.fullmatch(
        r"\s*(?P<leaf>.+?)\s*", label_leaf,
    )
    if label_shape is None:
        return False
    label_norm = normalize_financial_label(financial_label_body(label_shape.group("leaf")))
    if not label_norm or leaf_norm != label_norm:
        return False
    non_controlling = {
        "noncontrollinginterest", "noncontrollinginterests",
        "minorityinterest", "minorityinterests", "非控股权益", "少数股东", "少数股东权益",
    }
    owners = {
        "owners", "ownersofthecompany", "equityholdersofthecompany",
        "shareholdersofthecompany", "shareholdersoftheparentcompany",
        "parentcompanyowners", "母公司", "母公司所有者", "母公司股东", "上市公司股东",
    }
    return label_norm in non_controlling or label_norm in owners


def _is_attribution_heading(value: object) -> bool:
    """Keep cross-row attribution separate from prose in a full source row."""
    return normalize_financial_label(financial_label_body(value)) in {
        "attributableto", "归属于", "歸屬於", "应占", "應佔",
    }


def is_profit_sign_convention(label: object) -> bool:
    """Recognize an explanatory sign line, never an independent loss row."""
    source = financial_label_body(label).strip()
    return bool(re.fullmatch(
        r"[（(]\s*(?:net\s*loss\s*is\s*represented\s*by|净亏损以|淨虧損以)"
        r"[^\n]*[）)]?", source, flags=re.I,
    ))


def _profit_label_body(label: object) -> str:
    # A statement's sign convention can wrap onto a second line. It does not
    # assert that every amount in the row is a loss. Retain the original label
    # and row evidence; strip only this recognized annotation for classification.
    return re.sub(
        r"[（(]\s*(?:net\s*loss\s*is\s*represented\s*by|净亏损以|淨虧損以)"
        r"[^）)\n]*(?:[）)]|$)", "", financial_label_body(label), flags=re.I,
    )


def profit_row_concept(label: object, attribution: object = "") -> str | None:
    """Distinguish consolidated total profit from its allocation to owners."""
    source = _profit_label_body(label)
    normalized = normalize_financial_label(source)
    context = normalize_financial_label(financial_label_body(attribution))
    if re.search(r"\bEPS\b", f"{label} {attribution}", flags=re.I):
        return None
    if any(token in normalized + context for token in (
        "earningspershare", "pershare", "perads", "basic", "diluted",
        "每股", "comprehensive", "综合收益",
    )):
        return None
    ownership_context = (
        is_profit_ownership_heading(attribution)
        or _ownership_row_literal_proof(label, attribution)
    )
    attributed = (
        any(token in normalized for token in ("归属于", "歸屬於", "应占", "應佔", "attributableto"))
        or _is_attribution_heading(attribution)
    )
    if attributed and any(token in normalized for token in ("ordinaryshareholder", "普通股股东")):
        return "ordinary_shareholder_net_income"
    if any(token in normalized for token in ("少数股东损益", "少数股东净利润")):
        return "minority_interest_profit"
    if (attributed or ownership_context) and any(token in normalized for token in ("noncontrollinginterest", "minorityinterest", "非控股")):
        return "minority_interest_profit"
    if (attributed or ownership_context) and any(token in normalized for token in ("母公司", "上市公司股东", "本公司", "owners", "equityholder", "shareholder", "alibabagroup")):
        return "parent_net_income"
    if not attributed and any(normalized.startswith(normalize_financial_label(item)) for item in FINANCIAL_LABEL_ALIASES["net_income"]):
        return "net_income"
    return None


def normalize_financial_label(value: object) -> str:
    """Canonicalize label typography while leaving source text untouched."""
    text = str(value or "")
    # Row/alias matching revisits the same strings millions of times. Cache
    # this pure transform, while bounding both entry count and retained text.
    return _cached_canonical_financial_label(text) if len(text) <= 512 else canonical_search_text(text)


@lru_cache(maxsize=16_384)
def _cached_canonical_financial_label(text: str) -> str:
    return canonical_search_text(text)


_COST_ROW_QUALIFIERS = frozenset({
    "本期金额", "本期发生额", "本年金额", "本年发生额",
    "上期金额", "上期发生额", "上年金额", "上年发生额",
    "currentperiod", "currentperiodamount", "currentperiodincurred",
    "priorperiod", "priorperiodamount", "previousperiod", "previousperiodamount",
})
_COST_NON_AMOUNT_MARKERS = (
    "增长", "同比", "环比", "占比", "比例", "增长率", "率",
    "growth", "margin", "ratio", "percentage", "percent", "rate", "yield",
)


def cost_row_mentions(label: object) -> frozenset[str]:
    """Return fixed cost families named anywhere in an alias or source row."""

    source = _COST_ROW_NUMBER.sub(" ", financial_label_body(label))
    normalized = normalize_financial_label(source)
    for prefix in ("其中", "including", "ofwhich"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return frozenset(
        concept
        for concept in ("cost_of_revenue", "total_operating_cost")
        if any(normalize_financial_label(alias) in normalized
               for alias in FINANCIAL_LABEL_ALIASES[concept])
    )


def cost_row_is_non_amount(label: object) -> bool:
    """Identify cost growth/rate/margin labels that are not monetary rows."""

    raw = financial_label_body(label).casefold()
    normalized = normalize_financial_label(raw)
    return ("%" in raw or "％" in raw
            or any(normalize_financial_label(marker) in normalized
                   for marker in _COST_NON_AMOUNT_MARKERS))


def cost_row_source_is_non_amount(source_row: object) -> bool:
    """Check only the local text following a cost alias in a source-row excerpt.

    Vision excerpts may include a statement title, unit, period, or another
    table heading before the actual row. Those surrounding headers must not
    make a monetary cost row look like a ratio. Inspect the row-local suffix
    after the cost alias through the first displayed number instead.
    """

    source = financial_label_body(source_row)
    for aliases in (FINANCIAL_LABEL_ALIASES["cost_of_revenue"],
                    FINANCIAL_LABEL_ALIASES["total_operating_cost"]):
        for alias in aliases:
            pattern = re.escape(alias).replace(r"\ ", r"\s+")
            match = re.search(pattern, source, flags=re.IGNORECASE)
            if match is None:
                continue
            suffix = source[match.end():].partition("\n")[0]
            number = _COST_ROW_NUMBER.search(suffix)
            label_suffix = suffix[:number.start()] if number else suffix[:120]
            if cost_row_is_non_amount(alias + label_suffix):
                return True
            # A percent immediately attached to the displayed value is also
            # a ratio even if the model omitted the word "rate" from the label.
            if number is not None and re.match(r"\s*%", suffix[number.end():]):
                return True
    return False


def cost_row_alias_conflicts(label: object, concept: str) -> bool:
    """Protect fixed cost families when compatible packs add aliases."""

    mentions = cost_row_mentions(label)
    if not mentions:
        return False
    return bool(
        concept not in {"cost_of_revenue", "total_operating_cost"}
        or len(mentions) != 1
        or concept not in mentions
        or cost_row_is_non_amount(label)
        or cost_row_concept(label) != concept
    )


_COST_ROW_NUMBER = re.compile(
    r"(?:\(\s*[+-]?\d[\d,]*(?:\.\d+)?\s*\)|[+-]?\d[\d,]*(?:\.\d+)?)"
)


def cost_row_concept(label: object) -> str | None:
    """Classify only an explicit cost row; total operating cost is not COGS.

    This accepts a row label by itself or its source row with numeric cells,
    and removes only a leading component marker such as ``其中``.  It does not
    infer semantics from relative amounts or arbitrary substring matches.
    """

    source = financial_label_body(label)
    source = _COST_ROW_NUMBER.sub(" ", source)
    normalized = normalize_financial_label(source)
    for prefix in ("其中", "including", "ofwhich"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    if normalized in {
        normalize_financial_label(alias)
        for concept in ("cost_of_revenue", "total_operating_cost")
        for alias in FINANCIAL_LABEL_ALIASES[concept]
    }:
        for concept in ("cost_of_revenue", "total_operating_cost"):
            if normalized in {
                normalize_financial_label(alias)
                for alias in FINANCIAL_LABEL_ALIASES[concept]
            }:
                return concept

    # Only narrow statement-column qualifiers are allowed after a known row.
    # Arbitrary suffixes (notably growth/rate labels) are not cost amounts.
    for concept in ("cost_of_revenue", "total_operating_cost"):
        for alias in FINANCIAL_LABEL_ALIASES[concept]:
            needle = normalize_financial_label(alias)
            if not normalized.startswith(needle):
                continue
            tail = normalized[len(needle):]
            if tail in _COST_ROW_QUALIFIERS and not cost_row_is_non_amount(source):
                return concept

    # Vision's source-bound row excerpt also carries table headers and period
    # labels. Accept an embedded concept only when no non-numeric row label
    # follows it; this excludes e.g. "营业成本率".
    matches: list[tuple[int, str]] = []
    for concept in ("cost_of_revenue", "total_operating_cost"):
        for alias in FINANCIAL_LABEL_ALIASES[concept]:
            needle = normalize_financial_label(alias)
            start = normalized.find(needle)
            if start < 0:
                continue
            tail = normalized[start + len(needle):]
            if not tail:
                matches.append((len(needle), concept))
    if not matches:
        return None
    longest = max(length for length, _concept in matches)
    concepts = {concept for length, concept in matches if length == longest}
    return next(iter(concepts)) if len(concepts) == 1 else None


def interest_income_row_concept(label: object, heading: object = "") -> str | None:
    """Separate identically named rows by their explicit statement parent."""
    normalized = normalize_financial_label(financial_label_body(label))
    if normalized.removeprefix("其中").removeprefix("including") not in {"利息收入", "interestincome", "interestrevenue"}:
        return None
    context = normalize_financial_label(financial_label_body(heading))
    if context.startswith(("财务费用", "financecosts", "financeexpenses")):
        return "finance_interest_income"
    if context.startswith(("营业总收入", "totaloperatingrevenue")):
        return "interest_revenue"
    # English income tables may present interest as an explicit component of
    # Revenues. Accept an exact formal parent, or its bound literal leaf plus
    # numeric cells; never a paragraph merely mentioning revenue.
    for parent in ("revenue", "revenues", "operatingrevenue", "operatingrevenues", "totalrevenue"):
        if context == parent:
            return "interest_revenue"
        for leaf in ("interestincome", "interestrevenue"):
            for component in (leaf, "including" + leaf):
                prefix = parent + component
                if context.startswith(prefix) and context[len(prefix):].isdecimal():
                    return "interest_revenue"
    return None


def comprehensive_income_row_concept(label: object, attribution: object = "") -> str | None:
    """Classify allocations only inside an explicit comprehensive-income table."""
    normalized = normalize_financial_label(financial_label_body(label))
    combined = normalized + normalize_financial_label(attribution)
    if "attributableto" in combined or "归属于" in combined:
        if any(token in normalized for token in ("noncontrollinginterest", "少数股东")):
            return "minority_interest_comprehensive_income"
        if any(token in normalized for token in ("owners", "equityholder", "母公司")):
            return "parent_comprehensive_income"
    if normalized.startswith(("totalcomprehensiveincome", "综合收益总额")):
        return "comprehensive_income"
    return None


def equity_row_concept(
    label: object,
    attribution: object = "",
    *,
    following_labels: Sequence[str] = (),
) -> str | None:
    """Classify equity by source label and bounded same-table row context only."""
    source = re.sub(r"^\s*[一二三四五六七八九十百0-9]+[、.．)）]\s*", "", str(label))
    normalized = normalize_financial_label(source)
    context = normalize_financial_label(attribution)
    combined = normalized + context
    if any(token in combined for token in (
        "equityandliabilities", "liabilitiesandequity", "liabilitiesandshareholdersequity",
        "changesinequity", "statementofchangesinequity", "sharecapital", "capitalstock",
        "issuedsharecapital", "noncontrollinginterest", "minorityinterest", "少数股东权益",
        "少數股東權益", "实收资本", "實收資本", "股本",
    )):
        return None

    exact_shareholders_equity = normalized in {
        "totalshareholdersequity", "totalstockholdersequity",
    }
    if exact_shareholders_equity:
        nci_rows = {"noncontrollinginterest", "noncontrollinginterests", "minorityinterest", "minorityinterests"}
        future_rows = tuple(normalize_financial_label(row) for row in following_labels[:3])
        for index, future in enumerate(future_rows):
            if future in nci_rows and any(
                row == "totalequity" for row in future_rows[index + 1:]
            ):
                return "equity"

    has_attribution = any(token in combined for token in ("attributableto", "归属于", "應占", "应占"))
    has_owner = any(token in combined for token in (
        "owner", "equityholder", "shareholder", "parentcompany", "母公司",
        "上市公司股东", "股东", "股東", "所有者",
    ))
    describes_equity = any(token in normalized for token in ("equity", "权益", "權益"))
    if has_attribution and has_owner and describes_equity:
        return "equity"

    if any(normalized.startswith(normalize_financial_label(alias))
           for alias in FINANCIAL_LABEL_ALIASES["total_equity"]):
        return "total_equity"
    return None


def profit_loss_value_transform(label: object, concept: str) -> str:
    """A loss-only source label supplies sign, never magnitude or allocation.

    Mixed profit/(loss) headings defer to the numeric cell's own sign. A bare
    allocation label cannot borrow a loss sign from a neighbouring row.
    """
    if concept not in {
        "net_income", "parent_net_income", "minority_interest_profit",
        "ordinary_shareholder_net_income", "profit_before_tax", "operating_income",
    }:
        return "identity"
    normalized = normalize_financial_label(_profit_label_body(label))
    loss_positions = [position for marker in ("loss", "亏损", "損失", "损失")
                      if (position := normalized.find(marker)) >= 0]
    if not loss_positions:
        return "identity"
    first_loss = min(loss_positions)
    positive_before_loss = normalized[:first_loss]
    if any(marker in normalized for marker in ("profit", "盈利", "利润", "溢利")) or any(
        marker in positive_before_loss for marker in ("income", "profit", "盈利", "利润", "溢利")
    ):
        return "identity"
    return "negative_absolute_value"


def balance_component_row_concept(label: object) -> str | None:
    """Bind equity components to their own row, never a combined total."""
    normalized = normalize_financial_label(label)
    if normalized == "mezzanineequity":
        return "mezzanine_equity"
    if normalized in {
        "noncontrollinginterest", "noncontrollinginterests",
        "minorityinterest", "minorityinterests", "少数股东权益", "非控股权益",
    }:
        return "minority_interest_equity"
    return None


def liability_total_label_matches(label: object) -> bool:
    """Liabilities plus any equity class is the balance-sheet grand total."""
    normalized = normalize_financial_label(label)
    return not any(token in normalized for token in ("equity", "权益", "股东"))


def cash_flow_total_label_matches(label: object, aliases: tuple[str, ...]) -> bool:
    normalized = normalize_financial_label(re.sub(
        r"^\s*[一二三四五六七八九十百0-9]+[、.．)）]\s*", "", str(label)
    ))
    return any(normalized.startswith(normalize_financial_label(alias)) for alias in aliases)


def total_revenue_label_matches(label: object, aliases: tuple[str, ...]) -> bool:
    """A company's total cannot be inferred from a detail's revenue suffix."""
    source = re.sub(r"^\s*(?:[一二三四五六七八九十百0-9]+[、.．)）]|[IVX]+(?:\.|\s))\s*", "", str(label), flags=re.I)
    normalized = normalize_financial_label(source)
    # A statutory operating-revenue breakdown is a distinct line from total
    # revenues including interest/premiums, not an arbitrary segment detail.
    for prefix in ("其中", "ofwhich", "including"):
        if normalized.startswith(prefix) and normalized[len(prefix):].startswith(("营业收入", "operatingrevenue")):
            normalized = normalized[len(prefix):]
            break
    if any(token in normalized for token in ("增长", "growth", "margin", "收入占比")):
        return False
    return any(normalized.startswith(normalize_financial_label(alias)) for alias in aliases)


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


CAPITAL_ALLOCATION_PAYMENT_ALIASES: dict[str, tuple[str, ...]] = {
    # The source says liability payments, not a proven principal-only amount.
    "lease_liability_payment": ("payments of lease liabilities", "payment of lease liabilities"),
    "deferred_asset_acquisition_payment": tuple(
        f"{payment} of deferred consideration for acquisition of {asset}"
        for payment in ("payments", "payment")
        for asset in ("intangible assets", "property, plant and equipment")
    ),
}
_CAPITAL_ALLOCATION_PAYMENT_KEYS = {
    concept: frozenset(normalize_financial_label(alias) for alias in aliases)
    for concept, aliases in CAPITAL_ALLOCATION_PAYMENT_ALIASES.items()
}


def capital_allocation_payment_concept(label: object) -> str | None:
    """Only a complete, single payment label proves its literal asset scope."""
    normalized = normalize_financial_label(label)
    matched = [concept for concept, aliases in _CAPITAL_ALLOCATION_PAYMENT_KEYS.items()
               if normalized in aliases]
    return matched[0] if len(matched) == 1 else None


def has_distinct_capital_payment_label(label: object) -> bool:
    """Exclude even incomplete/combined financing labels from cash capex.

    Negative classification does not permit creating a typed fact. A deferred
    business acquisition stays a disclosure until independently supported.
    """
    normalized = normalize_financial_label(label)
    return any(marker in normalized for marker in (
        "paymentsofleaseliabilities", "paymentofleaseliabilities",
        "paymentsofdeferredconsiderationforacquisitionof",
        "paymentofdeferredconsiderationforacquisitionof",
    ))
