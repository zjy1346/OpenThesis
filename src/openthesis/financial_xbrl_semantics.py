"""Pure, bounded Inline XBRL semantics; not a general DTS validator.

Only registered namespace/concept/transform combinations are numeric facts.
Unsupported taxonomy extensions and transforms remain source observations;
they never inherit a standard tag merely because their local name matches.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import re
import xml.etree.ElementTree as ET

XBRLI = "http://www.xbrl.org/2003/instance"
IX = "http://www.xbrl.org/2013/inlineXBRL"
ISO4217 = "http://www.xbrl.org/2003/iso4217"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
INLINE_PARSER_VERSION = "inline-xbrl-v3-typed-dimensions"
TRANSFORM_NAMESPACES = frozenset({
    "http://www.xbrl.org/inlineXBRL/transformation/2010-04-20",
    "http://www.xbrl.org/inlineXBRL/transformation/2011-07-31",
    "http://www.xbrl.org/inlineXBRL/transformation/2015-02-26",
    "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12",
    "http://www.xbrl.org/inlineXBRL/transformation/2022-02-16",
})
_MODERN_TRANSFORMS = frozenset({"fixed-zero", "num-dot-decimal", "num-comma-decimal"})
_TRANSFORMS_BY_NAMESPACE = {
    "http://www.xbrl.org/inlineXBRL/transformation/2010-04-20": frozenset({"numcommadot", "numdotcomma", "numdash"}),
    "http://www.xbrl.org/inlineXBRL/transformation/2011-07-31": frozenset({"numdotdecimal", "numcommadecimal", "zerodash"}),
    "http://www.xbrl.org/inlineXBRL/transformation/2015-02-26": frozenset({"numdotdecimal", "numcommadecimal", "zerodash"}),
    "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12": _MODERN_TRANSFORMS,
    "http://www.xbrl.org/inlineXBRL/transformation/2022-02-16": _MODERN_TRANSFORMS | {"num-dot-decimal-apos", "num-comma-decimal-apos"},
}

# Parent and total earnings/equity are deliberately distinct. A standard
# namespace is necessary; an issuer extension with this name is not equivalent.
CONCEPTS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": ("revenue", "income_statement", "duration"),
    "RevenueFromContractWithCustomerIncludingAssessedTax": ("revenue_including_assessed_tax", "income_statement", "duration"),
    "Revenues": ("revenue", "income_statement", "duration"),
    "SalesRevenueNet": ("revenue", "income_statement", "duration"),
    "NetIncomeLoss": ("parent_net_income", "income_statement", "duration"),
    "ProfitLoss": ("net_income", "income_statement", "duration"),
    "NetIncomeLossAvailableToCommonStockholdersBasic": ("ordinary_shareholder_net_income", "income_statement", "duration"),
    "NetIncomeLossAttributableToNoncontrollingInterest": ("minority_interest_profit", "income_statement", "duration"),
    "OperatingIncomeLoss": ("operating_income", "income_statement", "duration"),
    "GrossProfit": ("gross_profit", "income_statement", "duration"),
    "CostOfRevenue": ("cost_of_revenue", "income_statement", "duration"),
    "Assets": ("assets", "balance_sheet", "instant"),
    "Liabilities": ("liabilities", "balance_sheet", "instant"),
    "StockholdersEquity": ("equity", "balance_sheet", "instant"),
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": ("total_equity", "balance_sheet", "instant"),
    "MinorityInterest": ("minority_interest_equity", "balance_sheet", "instant"),
    "NetCashProvidedByUsedInOperatingActivities": ("operating_cash_flow", "cash_flow", "duration"),
    "PaymentsToAcquirePropertyPlantAndEquipment": ("capital_expenditure_ppe", "cash_flow", "duration"),
    "PaymentsToAcquireIntangibleAssets": ("capital_expenditure_intangible", "cash_flow", "duration"),
    "PaymentsToAcquireProductiveAssets": ("capital_expenditure", "cash_flow", "duration"),
    "PaymentsForRepurchaseOfCommonStock": ("share_repurchase", "cash_flow", "duration"),
    "PaymentsOfDividends": ("shareholder_dividends_paid", "cash_flow", "duration"),
    "CommonStockSharesOutstanding": ("shares_outstanding", "equity_disclosure", "instant"),
}


def standard_concept(expanded_name: str) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"\{https?://fasb\.org/us-gaap/(20\d{2})(?:-\d{2}-\d{2})?\}([^{}]+)", expanded_name)
    return CONCEPTS.get(match[2]) if match else None


def expanded_qname(value: str, namespaces: dict[str, str]) -> str:
    if not re.fullmatch(r"[A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*", value):
        raise ValueError("XBRL_QNAME_INVALID")
    prefix, name = value.split(":", 1)
    if prefix not in namespaces:
        raise ValueError("XBRL_QNAME_NAMESPACE_MISSING")
    return f"{{{namespaces[prefix]}}}{name}"


def numeric_token(text: str, transform: str, sign: str, scale: str) -> tuple[str, str]:
    """Apply an explicitly registered display transform, sign, then 10^scale.

    ``decimals`` is intentionally absent: accuracy must not change magnitude.
    Large exponents and arbitrary transform names are not executed.
    """
    if not re.fullmatch(r"-?\d{1,2}", scale) or not -18 <= int(scale) <= 18:
        raise ValueError("XBRL_SCALE_UNSUPPORTED")
    if sign not in {"", "-"}:
        raise ValueError("XBRL_SIGN_INVALID")
    token = text.strip().replace("\u00a0", " ").replace("\u202f", " ")
    if transform:
        match = re.fullmatch(r"\{([^{}]+)\}([^{}]+)", transform)
        if match is None or match[1] not in TRANSFORM_NAMESPACES:
            raise ValueError("XBRL_TRANSFORM_UNSUPPORTED")
        name = match[2]
        if name not in _TRANSFORMS_BY_NAMESPACE[match[1]]:
            raise ValueError("XBRL_TRANSFORM_UNSUPPORTED")
        if name in {"fixed-zero", "zerodash", "numdash"}:
            if name != "fixed-zero" and token not in {"-", "–", "—"}:
                raise ValueError("XBRL_TRANSFORM_VALUE_INVALID")
            token = "0"
        elif name in {"num-dot-decimal", "num-dot-decimal-apos", "numdotdecimal", "numcommadot"}:
            if name.endswith("-apos"):
                token = re.sub("['`´’′]", " ", token)
            if not re.fullmatch(r"(?:(?:\d+|\d{1,3}(?:[, ]\d{3})+)(?:\.\d*)?|\.\d+)", token):
                raise ValueError("XBRL_TRANSFORM_VALUE_INVALID")
            token = token.replace(",", "").replace(" ", "")
        elif name in {"num-comma-decimal", "num-comma-decimal-apos", "numcommadecimal", "numdotcomma"}:
            if name.endswith("-apos"):
                token = re.sub("['`´’′]", " ", token)
            if not re.fullmatch(r"(?:(?:\d+|\d{1,3}(?:[. ]\d{3})+)(?:,\d*)?|,\d+)", token):
                raise ValueError("XBRL_TRANSFORM_VALUE_INVALID")
            token = token.replace(".", "").replace(" ", "").replace(",", ".")
        else:
            raise ValueError("XBRL_TRANSFORM_UNSUPPORTED")
    elif not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", token):
        raise ValueError("XBRL_DECIMAL_VALUE_INVALID")
    value = Decimal(token)
    if sign:
        if value < 0:
            raise ValueError("XBRL_DOUBLE_SIGN_INVALID")
        value = value.copy_negate()
    return format(value, "f"), format(Decimal(1).scaleb(int(scale)), "f")


def context_identity(element: ET.Element, namespaces: dict[str, str], *, namespace_at=None) -> dict:
    ns = {"x": XBRLI}
    identifiers = element.findall("x:entity/x:identifier", ns)
    if len(identifiers) != 1 or identifiers[0].get("scheme") != "http://www.sec.gov/CIK":
        raise ValueError("XBRL_ENTITY_SCHEME_UNSUPPORTED")
    identifier = (identifiers[0].text or "").strip()
    if not re.fullmatch(r"\d{1,10}", identifier):
        raise ValueError("XBRL_ENTITY_IDENTIFIER_INVALID")
    period = element.find("x:period", ns)
    if period is None:
        raise ValueError("XBRL_PERIOD_MISSING")
    instant = period.find("x:instant", ns)
    start = period.find("x:startDate", ns)
    end = period.find("x:endDate", ns)
    if instant is not None and len(period) == 1:
        start_date, end_date, period_type = None, (instant.text or "").strip(), "instant"
    elif start is not None and end is not None and len(period) == 2:
        start_date, end_date, period_type = (start.text or "").strip(), (end.text or "").strip(), "duration"
    else:
        raise ValueError("XBRL_PERIOD_UNSUPPORTED")
    parsed_end = date.fromisoformat(end_date)
    if start_date is not None and date.fromisoformat(start_date) > parsed_end:
        raise ValueError("XBRL_PERIOD_INVALID")
    dimensions = []
    for container in (element.find("x:entity/x:segment", ns), element.find("x:scenario", ns)):
        if container is None:
            continue
        for member in container:
            if member.tag == f"{{{XBRLDI}}}explicitMember":
                if len(member) or set(member.attrib) != {"dimension"}:
                    raise ValueError("XBRL_EXPLICIT_DIMENSION_UNSUPPORTED")
                member_namespaces = namespace_at(member) if namespace_at else namespaces
                dimensions.append((expanded_qname(member.get("dimension", ""), member_namespaces),
                                   expanded_qname((member.text or "").strip(), member_namespaces), "explicit"))
            elif member.tag == f"{{{XBRLDI}}}typedMember":
                # A bounded source-identity lane, not a DTS type validator.
                # Complex domains, QName-valued domains and attributes require
                # a taxonomy-aware adapter; do not silently flatten their XML.
                if (len(member) != 1 or set(member.attrib) != {"dimension"}
                        or (member.text or "").strip()):
                    raise ValueError("XBRL_TYPED_DIMENSION_UNSUPPORTED")
                domain = member[0]
                value = domain.text or ""
                if (len(domain) or domain.attrib or (domain.tail or "").strip()
                        or not domain.tag.startswith("{") or not value.strip()
                        or len(value) > 1024
                        or re.fullmatch(r"[A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*", value.strip())):
                    raise ValueError("XBRL_TYPED_DIMENSION_UNSUPPORTED")
                member_namespaces = namespace_at(member) if namespace_at else namespaces
                dimensions.append((expanded_qname(member.get("dimension", ""), member_namespaces),
                                   json.dumps([domain.tag, value], ensure_ascii=False, separators=(",", ":")),
                                   "typed"))
            else:
                raise ValueError("XBRL_CONTEXT_CONTENT_UNSUPPORTED")
    if len(dimensions) > 32:
        raise ValueError("XBRL_DIMENSION_LIMIT")
    if len({item[0] for item in dimensions}) != len(dimensions):
        raise ValueError("XBRL_DIMENSION_DUPLICATE")
    return {"cik": identifier.zfill(10), "start": start_date, "end": end_date,
            "period_type": period_type, "dimensions": sorted(dimensions)}


def unit_identity(element: ET.Element, namespaces: dict[str, str], *, namespace_at=None) -> tuple[str, str]:
    if len(element) != 1 or element[0].tag != f"{{{XBRLI}}}measure":
        raise ValueError("XBRL_UNIT_UNSUPPORTED")
    measure = expanded_qname((element[0].text or "").strip(),
                             namespace_at(element[0]) if namespace_at else namespaces)
    match = re.fullmatch(r"\{" + re.escape(ISO4217) + r"\}([A-Z]{3})", measure)
    if match:
        return "money", match[1]
    if measure == f"{{{XBRLI}}}shares":
        return "shares", ""
    raise ValueError("XBRL_UNIT_UNSUPPORTED")
