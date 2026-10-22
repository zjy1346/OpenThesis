"""Pure checks of numbers in prose against the cited source, not model fields.

This is not an accounting or semantic inference engine. It understands explicit
display scales, rounding, currency codes, ratios and source-bound dates only.
An unrecognised or conflicting assertion remains unverified.
"""
from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Any, Mapping, Sequence


_CURRENCIES = {"usd": "USD", "us$": "USD", "$": "USD", "美元": "USD",
               "hkd": "HKD", "hk$": "HKD", "港元": "HKD", "港币": "HKD", "港幣": "HKD",
               "cny": "CNY", "rmb": "CNY", "¥": "CNY", "￥": "CNY",
               "人民币": "CNY", "人民幣": "CNY"}
_SCALES = {"k": "1000", "thousand": "1000", "thousands": "1000", "千": "1000",
           "万": "10000", "萬": "10000", "百万": "1000000", "百萬": "1000000",
           "m": "1000000", "million": "1000000", "millions": "1000000",
           "亿": "100000000", "億": "100000000", "十亿": "1000000000", "十億": "1000000000",
           "b": "1000000000", "billion": "1000000000", "billions": "1000000000",
           "t": "1000000000000", "trillion": "1000000000000", "trillions": "1000000000000"}
_CURRENCY_PATTERN = r"USD|US\$|HKD|HK\$|CNY|RMB|人民币|人民幣|美元|港元|港币|港幣"
_NUMBER_PATTERN = r"[-+]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
_QUANTITY = re.compile(
    rf"(?<![A-Za-z0-9_.])(?P<prefix>{_CURRENCY_PATTERN}|[$¥￥])?\s*"
    rf"(?P<number>{_NUMBER_PATTERN})\s*"
    r"(?P<scale>trillions?|billions?|millions?|thousands?|十亿|十億|百万|百萬|万|萬|亿|億|千|[KMBT](?![a-z]))?\s*"
    rf"(?P<suffix>percentage\s+points?|percent|%|％|{_CURRENCY_PATTERN}|元)?(?!\d|,\d|\.\d)",
    re.IGNORECASE,
)
_TYPOGRAPHY = str.maketrans("０１２３４５６７８９，．％－＋（）", "0123456789,.%-+()")


@dataclass(frozen=True, slots=True)
class ProseQuantity:
    number: Decimal
    scale: Decimal
    currency: str
    kind: str
    decimals: int
    text: str
    fiscal_year: str = ""


def _quantities(text: str, records: Sequence[Mapping[str, Any]], *, fiscal_year: Any = None) -> tuple[ProseQuantity, ...]:
    normalized = str(text).translate(_TYPOGRAPHY)
    # Host-known inline source IDs are locators, not reported quantities.
    # Mask only exact registered IDs (not arbitrary hex-looking numbers), so
    # an invented amount/unknown locator still fails the ordinary checks.
    for record in records:
        identity = record.get("evidence_id")
        if isinstance(identity, str) and identity.startswith(("fact:", "filing:", "disclosure:", "table:", "observation:")):
            normalized = re.sub(r"(?<![A-Za-z0-9:._-])" + re.escape(identity) + r"(?![A-Za-z0-9:_-])",
                lambda match: " " * len(match.group()), normalized)
    normalized = re.sub(r"(?<![\w])\((\d+(?:,\d{3})*(?:\.\d+)?)\)",
                        r"-\1", normalized)
    year_markers = []
    fiscal_year_spans = []
    # Unicode ``\b`` does not mark the boundary between ASCII digits and
    # Chinese characters: ``FY2025合并`` therefore used to leave 2025 for the
    # malformed-number scan. Match and retain the actual fiscal-year span so
    # only that identity token is exempted; nearby amounts remain checkable.
    year_patterns = (
        re.compile(r"(?<![A-Za-z0-9_])FY\s*(?P<year>(?:19|20|21)\d{2})(?!\d)(?!\s*(?:元|人民币|人民幣|美元|USD|RMB|CNY))", re.I),
        re.compile(r"(?<!\d)(?P<year>(?:19|20|21)\d{2})(?:年度|财年|財年|会计年度|會計年度|年)", re.I),
        re.compile(r"(?:(?:\bin|\bduring|\bfor|\byear|\bfiscal\s+year)\s*"
                    r"(?P<year>(?:19|20|21)\d{2})(?!\d)(?!-\d{2}-\d{2}))", re.I),
    )
    for pattern in year_patterns:
        for marker in pattern.finditer(normalized):
            year = marker.group("year")
            marker_span = (marker.start(), marker.end())
            year_span = marker.span("year")
            year_markers.append((*marker_span, year))
            fiscal_year_spans.append(year_span)
    boundaries = [match.end() for match in re.finditer(r"[;；。!?]|,(?!\d)|\.(?=\s|$)", normalized)]
    # Dates and fiscal labels are identities, not amounts. They may be
    # omitted from amount checks only when explicitly carried by a citation.
    dates = {str(record.get(key) or "") for record in records
             for key in ("start_date", "end_date", "filed_at", "period_end")}
    for date in sorted(dates, key=len, reverse=True):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", date):
            normalized = normalized.replace(date[:10], " " * 10)
    years = {str(record.get("fiscal_year")) for record in records if record.get("fiscal_year")}
    # Keep the original text/offsets intact. The recorded spans below prevent
    # fiscal-year digits from becoming amounts without masking a genuine
    # amount that happens to repeat the same four digits elsewhere.
    periods = {str(record.get("fiscal_period") or "") for record in records}
    pairs = {(str(record.get("fiscal_year") or str(record.get("end_date") or "")[:4]), str(record.get("fiscal_period") or "")) for record in records}
    interim_labels = re.compile(r"(?<![A-Za-z0-9_])(?:(?P<year>(?:19|20|21)\d{2})\s*(?P<period>H[12]|Q[1-4])|(?P<before>H[12]|Q[1-4])\s*(?P<after>(?:19|20|21)\d{2}))(?![A-Za-z0-9_])", re.I)
    for label in list(interim_labels.finditer(normalized)):
        year = label.group("year") or label.group("after")
        period = (label.group("period") or label.group("before")).upper()
        prefix = normalized[:label.start()].rstrip()
        suffix = normalized[label.end():].lstrip()
        if ((year, period) not in pairs or re.search(rf"(?:{_CURRENCY_PATTERN}|[$¥￥])$", prefix, re.I)
                or re.match(rf"(?:{_CURRENCY_PATTERN}|元|[%％]|[亿億万萬千])", suffix, re.I)):
            continue
        year_markers.append((label.start(), label.end(), year))
        normalized = normalized[:label.start()] + " " * len(label.group()) + normalized[label.end():]
    year_markers.sort()
    for period in periods:
        if period in {"Q1", "Q2", "Q3", "Q4", "H1", "H2"}:
            normalized = re.sub(rf"(?<![A-Za-z0-9_]){period}(?![A-Za-z0-9_])", " " * len(period), normalized, flags=re.I)
    quantities = []
    matches = list(_QUANTITY.finditer(normalized))
    number_starts = [match.start("number") for match in matches]
    # A malformed number must not become two independently matching amounts.
    # Check that every numeric token is consumed in full, including a leading
    # decimal point. Dates/fiscal labels above are source-bound exceptions.
    for run in re.finditer(r"(?<!\d)\.?\d[\d,.]*\d|(?<!\d)\d", normalized):
        if any(start <= run.start() and run.end() <= end for start, end in fiscal_year_spans):
            continue
        position = bisect_right(number_starts, run.start()) - 1
        if position < 0 or matches[position].end("number") < run.end():
            quantities.append(ProseQuantity(Decimal("NaN"), Decimal(1), "INVALID", "invalid", 0, run.group()))
    for match in matches:
        raw = match.group("number")
        prefix, suffix = match.group("prefix") or "", match.group("suffix") or ""
        scale_name = (match.group("scale") or "").casefold()
        prefix_context = normalized[:match.start()].rstrip()
        suffix_context = normalized[match.end():].lstrip()
        fiscal_label = suffix_context.startswith("年") or bool(re.search(
            r"(?:\b(?:in|during|for|FY|year)|\bfiscal\s+year)\s*$", prefix_context, re.I))
        if not (prefix or suffix or scale_name) and raw in years and fiscal_label:
            continue
        currency = _CURRENCIES.get(prefix.casefold(), _CURRENCIES.get(suffix.casefold(), ""))
        kind = "ratio" if suffix.casefold() in {"%", "％", "percent"} else "money" if prefix or suffix or scale_name else "number"
        if suffix.casefold().startswith("percentage"):
            kind = "percentage_points"
        try:
            number = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        position = bisect_right(boundaries, match.start())
        left = boundaries[position - 1] if position else 0
        right = boundaries[position] if position < len(boundaries) else len(normalized)
        local_years = [item for item in year_markers if left <= item[0] < right]
        preceding = [item for item in local_years if item[1] <= match.start("number")]
        following = [item for item in local_years if item[0] >= match.end("number")]
        year = (preceding[-1][2] if preceding else following[0][2] if following
                else str(fiscal_year) if fiscal_year not in (None, "") else "")
        quantities.append(ProseQuantity(number, Decimal(_SCALES.get(scale_name, "1")),
            currency, kind, len(raw.partition(".")[2]), match.group().strip(), year))
    return tuple(quantities)


def _display_matches(quantity: ProseQuantity, value: Any) -> bool:
    try:
        expected = Decimal(str(value))
        if not expected.is_finite():
            return False
        scale = Decimal("0.01") if quantity.kind == "ratio" else quantity.scale
        with localcontext() as context:
            context.prec = max(50, len(expected.as_tuple().digits) + 30)
            displayed = expected / scale
            # Rounding is to the written precision of this exact quantity,
            # never a global relative tolerance across financial values.
            precision = Decimal(1).scaleb(-quantity.decimals)
            return displayed.quantize(precision, rounding=ROUND_HALF_UP) == quantity.number
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return False


def written_percentage_matches(written: str, value: Any) -> bool:
    """Match an engine ratio at this percentage's explicitly written precision."""
    try:
        number = Decimal(written)
    except (InvalidOperation, ValueError, TypeError):
        return False
    if not number.is_finite():
        return False
    return _display_matches(ProseQuantity(number, Decimal(1), "", "ratio",
                            len(written.partition(".")[2]), written), value)


def disclosure_quantity_conflicted(text: str, records: Sequence[Mapping[str, Any]]) -> bool:
    """Prove a changed quantity in an otherwise identical cited disclosure.

    A missing amount anywhere on a page is insufficient. The complete clause
    skeleton, explicit period, issuer and document must match, and every
    eligible version of that exact clause must disagree. Semantic paraphrases
    remain pending for the assertion verifier.
    """
    def skeleton(body: str) -> str:
        return re.sub(r"\s+", " ", _QUANTITY.sub(" <quantity> ", body.translate(_TYPOGRAPHY))).strip().casefold()

    if not _quantities(text, records):
        return False
    clauses = []
    years = set(re.findall(r"(?:19|20)\d{2}", text))
    for record in records:
        if (record.get("kind") not in {"filing_text", "disclosure_text"}
                or not record.get("document_id") or not record.get("company_cik")
                or not record.get("end_date")):
            continue
        source = str(record.get("raw_text") or record.get("excerpt") or record.get("text") or "")
        source_years = set(re.findall(r"(?:19|20)\d{2}", source))
        if years != source_years or skeleton(text) != skeleton(source):
            continue
        clauses.append(record)
    identities = {(row.get("company_cik"), row.get("end_date"), row.get("fiscal_period")) for row in clauses}
    return bool(clauses) and len(identities) == 1 and not numeric_prose_supported(text, clauses)


def numeric_prose_supported(text: str, records: Sequence[Mapping[str, Any]], *, fiscal_year: Any = None) -> bool:
    """Require every amount/ratio in prose to be present in its cited source.

    A matching deterministic JSON value cannot override contradictory prose.
    For filing disclosures, compare the same explicit quantity and currency;
    numbers merely occurring somewhere in a different citation are not enough.
    """
    assertions = _quantities(text, records, fiscal_year=fiscal_year)
    for quantity in assertions:
        if quantity.kind == "invalid":
            return False
        supported = False
        for record in records:
            if record.get("kind") == "financial_fact":
                if quantity.fiscal_year and str(record.get("fiscal_year") or "") != quantity.fiscal_year:
                    continue
                currency = str(record.get("currency") or record.get("unit") or "").upper()
                is_money = bool(re.fullmatch(r"[A-Z]{3}", currency))
                point_unit = str(record.get("unit") or "").casefold() in {
                    "percentage_point", "percentage_points", "pp",
                }
                # A percentage level is not a percentage-point change. Such
                # a change needs its own typed amount/derivation, not a ratio
                # record whose unscaled number happens to match the prose.
                if quantity.kind == "percentage_points" and not point_unit:
                    continue
                if quantity.kind == "ratio" and point_unit:
                    continue
                if quantity.currency and quantity.currency != currency:
                    continue
                if quantity.kind == "money" and not is_money:
                    continue
                if quantity.kind in {"ratio", "percentage_points"} and is_money:
                    continue
                if _display_matches(quantity, record.get("value")):
                    supported = True
                    break
            elif str(record.get("kind", "")).casefold() in {"filing_text", "disclosure_text"} or str(record.get("evidence_id", "")).startswith("filing:"):
                source = str(record.get("raw_text") or record.get("excerpt") or record.get("text") or "")
                for original in _quantities(source, (record,)):
                    if (quantity.kind == original.kind and quantity.currency == original.currency
                            and quantity.scale == original.scale and quantity.number == original.number
                            and (not quantity.fiscal_year or quantity.fiscal_year == original.fiscal_year
                                 or quantity.fiscal_year == str(record.get("fiscal_year") or ""))):
                        supported = True
                        break
                if supported:
                    break
        if not supported:
            return False
    return True
