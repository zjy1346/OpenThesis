"""Keep model valuation prose subordinate to deterministic valuation facts."""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

from .i18n import EN, ZH_HANT, normalize_language


_VALID_VALUATION_STATES = frozenset({"ok", "extreme_assumption"})
_VALUATION_ASSERTION = re.compile(
    r"(?:market[- ]implied|current (?:share )?price\s+(?:implies?|reflects?)|"
    r"price\s+(?:implies?|reflects?)|implied (?:fcf )?(?:growth|expectations)|"
    r"reverse\s+dcf|market expectations|"
    r"市场.{0,8}(?:隐含|反映)|市場.{0,8}(?:隱含|隐含|反映)|當前價格.{0,8}(?:隱含|隐含|反映)|"
    r"当前价格.{0,8}(?:隱含|隐含|反映)|股價.{0,8}(?:隱含|隐含|反映)|"
    r"股价.{0,8}(?:隱含|隐含|反映)|隱含(?:的)?(?:自由現金流)?(?:增長|預期)|"
    r"隐含(?:的)?(?:自由现金流)?(?:增长|预期)|反向\s*DCF)",
    re.IGNORECASE,
)
_MARGIN_OF_SAFETY = re.compile(r"(?:margin of safety|安全边际|安全邊際)", re.IGNORECASE)
_MARGIN_LIMITATION = re.compile(
    r"(?:cannot|can't|unable to|not possible to|not assessed|not established|"
    r"cannot be inferred|not infer|无从|无法|未能|不能|不宜|不推断|未评估|未確認|未确认|"
    r"無法|未能|不能|不宜|不推斷|未評估|未確認)",
    re.IGNORECASE,
)
_CURRENCY_CONVERSION = re.compile(
    r"(?:折合|折算|換算|换算|converted?\s+(?:to|into)|equivalent\s+to|"
    r"translated\s+(?:to|into))",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"(?:無法|无法|不能|不應|不应|未能|並未|并未|不再|尚未|沒有|没有|"
    r"does\s+not|doesn't|cannot|can't|not\s+(?:available|supported|reliable)|"
    r"no\s+(?:reliable|verified|valid))",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
_MONEY = re.compile(
    r"(?:[$¥￥]|\b(?:HKD|CNY|RMB|USD|EUR|GBP|JPY)\b|"
    r"港幣|港币|人民幣|人民币|美元|歐元|欧元)\s*[-+]?\d[\d,.]*(?:\s*(?:億|亿|萬|万|B|M|K|T))?",
    re.IGNORECASE,
)
_CURRENCY_MARKER = re.compile(
    r"(?:[$¥￥]|\b(?:HKD|CNY|RMB|USD|EUR|GBP|JPY)\b|"
    r"港幣|港币|人民幣|人民币|美元|歐元|欧元)",
    re.IGNORECASE,
)


def _valuation_is_usable(value: Any) -> bool:
    if not isinstance(value, dict) or str(value.get("status", "")).casefold() not in _VALID_VALUATION_STATES:
        return False
    growth = value.get("implied_fcf_growth")
    equity_value = value.get("equity_market_value", value.get("market_cap"))
    base_fcf = value.get("base_free_cash_flow")
    try:
        return (
            not isinstance(growth, bool)
            and math.isfinite(float(growth))
            and not isinstance(equity_value, bool)
            and math.isfinite(float(equity_value))
            and float(equity_value) > 0
            and not isinstance(base_fcf, bool)
            and math.isfinite(float(base_fcf))
            and float(base_fcf) > 0
            and bool(str(value.get("currency", "")).strip())
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _sentence_is_negated(sentence: str, cue: re.Match[str]) -> bool:
    # Only consider negation immediately governing the valuation assertion;
    # an unrelated disclaimer elsewhere in a long paragraph must not bless it.
    before = sentence[max(0, cue.start() - 40):cue.start()]
    return bool(_NEGATED.search(before))


def _is_unsupported_assertion(sentence: str, valuation: dict[str, Any] | None) -> bool:
    if _MARGIN_OF_SAFETY.search(sentence) and not _MARGIN_LIMITATION.search(sentence):
        # Reverse DCF alone is not an intrinsic-value estimate. Until a typed,
        # source-backed margin-of-safety field exists, the model may not assert
        # either a wide or narrow margin as though the engine computed it.
        return True
    cue = _VALUATION_ASSERTION.search(sentence)
    if cue and not _sentence_is_negated(sentence, cue):
        percentages = [float(match.group(1)) / 100 for match in _PERCENT.finditer(sentence)]
        money = _MONEY.search(sentence)
        if not _valuation_is_usable(valuation):
            if percentages or money or "dcf" in cue.group(0).casefold():
                return True
        elif percentages:
            expected = float(valuation["implied_fcf_growth"])
            # The public-facing deterministic calculation is the only source
            # for a market-implied growth rate. Do not accept model-created
            # ranges or alternative point estimates as if independently solved.
            if any(abs(item - expected) > 0.015 for item in percentages):
                return True
        elif money:
            return True

    # No exchange-rate/source contract currently travels with a deterministic
    # valuation, so quantified cross-currency conversions are never asserted.
    if _CURRENCY_CONVERSION.search(sentence) and _CURRENCY_MARKER.search(sentence):
        return bool(re.search(r"\d", sentence))
    return False


def _unsupported_clause_start(
    text: str, valuation: dict[str, Any] | None
) -> int | None:
    """Locate a removable valuation clause without discarding prior prose.

    Punctuation is not a reliable sentence boundary in model output.  When a
    high-risk assertion follows supported analysis without a period, quarantine
    from the assertion cue onward rather than deleting the whole paragraph.
    """
    cue = _VALUATION_ASSERTION.search(text)
    if cue and not _sentence_is_negated(text, cue):
        percentages = [float(match.group(1)) / 100 for match in _PERCENT.finditer(text)]
        money = _MONEY.search(text)
        unsupported = False
        if not _valuation_is_usable(valuation):
            unsupported = bool(percentages or money or "dcf" in cue.group(0).casefold())
        elif percentages:
            expected = float(valuation["implied_fcf_growth"])
            unsupported = any(abs(item - expected) > 0.015 for item in percentages)
        elif money:
            unsupported = True
        if unsupported:
            return cue.start()

    conversion = _CURRENCY_CONVERSION.search(text)
    if (
        conversion
        and _CURRENCY_MARKER.search(text)
        and re.search(r"\d", text[conversion.start():])
    ):
        return conversion.start()
    return None


def _sanitize_text(value: str, valuation: dict[str, Any] | None) -> tuple[str, int]:
    pieces = re.split(r"(?<=[。！？!?；;\n])|(?<=[.!?])(?=\s+[A-Z])", value)
    kept: list[str] = []
    removed = 0
    for piece in pieces:
        if _MARGIN_OF_SAFETY.search(piece) and not _MARGIN_LIMITATION.search(piece):
            removed += 1
            continue
        unsafe_start = _unsupported_clause_start(piece, valuation)
        if piece and unsafe_start is not None:
            safe_prefix = piece[:unsafe_start]
            if safe_prefix:
                kept.append(safe_prefix)
            removed += 1
        else:
            kept.append(piece)
    return "".join(kept), removed


def _sanitize_value(value: Any, valuation: dict[str, Any] | None) -> tuple[Any, int]:
    if isinstance(value, str):
        return _sanitize_text(value, valuation)
    if isinstance(value, list):
        sanitized: list[Any] = []
        removed = 0
        for item in value:
            safe_item, count = _sanitize_value(item, valuation)
            removed += count
            if isinstance(item, dict) and any(
                field in item for field in ("text", "conclusion", "argument")
            ) and isinstance(safe_item, dict) and not any(
                isinstance(safe_item.get(field), str) and safe_item.get(field).strip()
                for field in ("text", "conclusion", "argument")
            ):
                continue
            if safe_item not in (None, "", [], {}):
                sanitized.append(safe_item)
        return sanitized, removed
    if isinstance(value, tuple):
        safe, removed = _sanitize_value(list(value), valuation)
        return tuple(safe), removed
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        removed = 0
        for key, item in value.items():
            safe_item, count = _sanitize_value(item, valuation)
            removed += count
            if safe_item not in (None, "", [], {}):
                sanitized[key] = safe_item
        return sanitized, removed
    return value, 0


def _section_needs_quarantine(value: Any, valuation: dict[str, Any] | None) -> bool:
    if isinstance(value, str):
        pieces = re.split(r"(?<=[。！？!?；;\n])|(?<=[.!?])(?=\s+[A-Z])", value)
        return any(_is_unsupported_assertion(piece, valuation) for piece in pieces)
    if isinstance(value, dict):
        numeric_market_fields = {
            "implied_growth", "implied_fcf_growth", "required_growth",
            "implied_revenue_growth", "current_price", "share_price",
            "equity_market_value", "market_cap", "enterprise_value",
        }
        if any(
            str(key).casefold() in numeric_market_fields
            and item not in (None, "", [], {})
            for key, item in value.items()
        ):
            return True
        return any(_section_needs_quarantine(item, valuation) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_section_needs_quarantine(item, valuation) for item in value)
    return False


def _localized_summary(available: bool, language: str, status: str = "") -> str:
    locale = normalize_language(language)
    if available:
        if status == "extreme_assumption":
            simplified = "确定性反向 DCF 得到极端增长假设；数值、币种与参数以同报告的确定性估值结果为准，不采用模型自行改写的估值数字。"
            traditional = "確定性反向 DCF 得到極端增長假設；數值、幣別與參數以同報告的確定性估值結果為準，不採用模型自行改寫的估值數字。"
            english = "The deterministic reverse DCF produces an extreme growth assumption. Use its reported values, currency, and parameters; model-rewritten valuation figures are omitted."
        else:
            simplified = "确定性反向 DCF 已提供可复核结果；本节数字以同报告的确定性估值结果为准，未经该计算支持的市场隐含预期不予呈现。"
            traditional = "確定性反向 DCF 已提供可複核結果；本節數字以同報告的確定性估值結果為準，未經該計算支持的市場隱含預期不予呈現。"
            english = "A deterministic reverse DCF result is available. This section follows that calculation; market-implied expectations not supported by it are omitted."
    else:
        simplified = "缺少有效的同币种市场权益价值或正的已验证全年自由现金流，未执行确定性反向 DCF；因此不推断当前价格隐含预期。"
        traditional = "缺少有效的同幣別市場權益價值或正的已驗證全年自由現金流，未執行確定性反向 DCF；因此不推斷目前價格隱含預期。"
        english = "A valid same-currency equity value or positive verified full-year free cash flow is unavailable, so no deterministic reverse DCF was run and no market-implied expectation is inferred."
    return english if locale == EN else traditional if locale == ZH_HANT else simplified


def enforce_valuation_consistency(
    report: Any, valuation: Any, language: str = "zh-CN"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Quarantine unsupported valuation prose and project only computed values.

    Ordinary business and risk analysis is retained. The dedicated valuation
    section is replaced with either deterministic fields or an explicit
    limitation; this does not invent facts or make a failed study complete.
    """

    sanitized = deepcopy(report) if isinstance(report, dict) else {}
    valuation_value = valuation if isinstance(valuation, dict) else None
    usable = _valuation_is_usable(valuation_value)
    original_section = sanitized.get("implied_expectations")
    had_section_content = original_section not in (None, "", [], {})
    section: dict[str, Any] = {"summary": _localized_summary(
        usable, language,
        str(valuation_value.get("status", "")) if usable and valuation_value else "",
    )}
    if usable and valuation_value:
        fields = {
            "implied_growth": valuation_value.get("implied_fcf_growth"),
            "equity_market_value": valuation_value.get("equity_market_value", valuation_value.get("market_cap")),
            "discount_rate": valuation_value.get("discount_rate"),
            "terminal_growth_rate": valuation_value.get("terminal_growth"),
            "horizon_years": valuation_value.get("horizon_years"),
            "currency": valuation_value.get("currency"),
            "as_of": valuation_value.get("market_as_of"),
        }
        section.update({key: item for key, item in fields.items() if item not in (None, "")})
    sanitized["implied_expectations"] = section

    removed_paths: list[str] = []
    removed_count = int(_section_needs_quarantine(original_section, valuation_value if usable else None))
    for key, value in list(sanitized.items()):
        if key == "implied_expectations":
            continue
        safe_value, count = _sanitize_value(value, valuation_value if usable else None)
        if count:
            removed_paths.append(str(key))
            removed_count += count
        sanitized[key] = safe_value
    return sanitized, {
        "valuation_available": usable,
        "valuation_status": str(valuation_value.get("status", "unavailable")) if valuation_value else "unavailable",
        "quarantined_count": removed_count,
        "quarantined_paths": removed_paths + (["implied_expectations"] if removed_count else []),
    }
