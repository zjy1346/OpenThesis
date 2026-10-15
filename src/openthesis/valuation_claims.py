"""Keep model valuation prose subordinate to deterministic valuation facts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any

from .i18n import EN, ZH_HANT, normalize_language
from .research_coverage import has_analysis
from .research_numeric_assertions import written_percentage_matches


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
_PERCENT = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*[%％]")
_RATE_LABELS = {
    "discount_rate": re.compile(r"\b(?:discount\s+rate|WACC)\b|折[现現]率", re.I),
    "terminal_growth": re.compile(r"\bterminal\s+growth(?:\s+rate)?\b|永[续續][增長长率]+|终[值期]增[长長]率|終[值期]增[长長]率", re.I),
    "implied_fcf_growth": re.compile(r"\b(?:implied\s+(?:annual\s+)?(?:FCF\s+)?growth|(?:annual\s+)?FCF\s+growth)\b|(?:自由[现現]金流)(?:年)?(?:增[长長]|增速)(?:率)?|(?:隐含|隱含)(?:年)?增[长長](?:率)?", re.I),
}
_METADATA = frozenset({"evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids",
    "claim_ids", "input_claim_ids", "claim_id", "source_ids", "source_url", "document_id",
    "title", "label", "name", "kind", "type", "confidence", "severity", "role"})
_ABSENT = object()


def _rate_field(text: str, amount: re.Match[str]) -> str | None:
    """Bind explicit labels to one quantity, never to any matching parameter.

    This only classifies captured prose; it never edits a sentence. Unlabelled
    or conflicting quantities stay unverified, not inferred from magnitude.
    """
    preceding = []
    following = []
    for field, pattern in _RATE_LABELS.items():
        for label in pattern.finditer(text):
            if label.end() <= amount.start():
                between = text[label.end():amount.start()]
                if len(between) <= 80 and not _PERCENT.search(between) and not re.search(r"[;；。!?]|\.(?=\s|$)", between):
                    preceding.append((label.end(), field))
            elif label.start() >= amount.end():
                between = text[amount.end():label.start()]
                if len(between) <= 25 and not re.search(r"[,;；。.!?]|\band\b|以及|及|和|而", between, re.I):
                    following.append((label.start(), field))
    before = max(preceding, default=None)
    after = min(following, default=None)
    if after is not None:
        # In "discount rate 10% annual FCF growth", both labels could govern
        # one token; do not choose whichever number makes the claim pass.
        if before is not None and before[1] != after[1]:
            return None
        return after[1]
    if before is not None:
        return before[1]
    prefix = text[:amount.start()]
    cue = list(_VALUATION_ASSERTION.finditer(prefix))
    if cue and not _PERCENT.search(prefix[cue[-1].end():]) and re.search(
        r"implies?|implied|隐含|隱含", cue[-1].group(), re.I):
        return "implied_fcf_growth"
    return None
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


def _assertion_context(text: str, cue: re.Match[str]) -> tuple[str, int]:
    """Read the governing sentence for classification, never render a slice."""
    boundaries = [match.end() for match in re.finditer(r"[。！？!?；;\n]|\.(?=\s|$)", text)]
    left = max((position for position in boundaries if position <= cue.start()), default=0)
    right = min((position for position in boundaries if position >= cue.end()), default=len(text))
    return text[left:right], left


def _is_unsupported_assertion(sentence: str, valuation: dict[str, Any] | None) -> bool:
    for margin in _MARGIN_OF_SAFETY.finditer(sentence):
        # Reverse DCF alone is not an intrinsic-value estimate. Until a typed,
        # source-backed margin-of-safety field exists, the model may not assert
        # either a wide or narrow margin as though the engine computed it.
        context, _ = _assertion_context(sentence, margin)
        if not _MARGIN_LIMITATION.search(context):
            return True
    for cue in _VALUATION_ASSERTION.finditer(sentence):
        context, offset = _assertion_context(sentence, cue)
        local_cue = _VALUATION_ASSERTION.search(context, cue.start() - offset)
        if _sentence_is_negated(context, local_cue):
            continue
        percentages = list(_PERCENT.finditer(context))
        money = _MONEY.search(context)
        if not _valuation_is_usable(valuation):
            if percentages or money or "dcf" in cue.group(0).casefold():
                return True
        elif percentages:
            # The public-facing deterministic calculation is the only source
            # for a market-implied growth rate. Do not accept model-created
            # ranges or alternative point estimates as if independently solved.
            if any((field := _rate_field(context, item)) is None
                   or not written_percentage_matches(item.group(1), valuation.get(field))
                   for item in percentages):
                return True
        elif money:
            return True

    # No exchange-rate/source contract currently travels with a deterministic
    # valuation, so quantified cross-currency conversions are never asserted.
    if _CURRENCY_CONVERSION.search(sentence) and _CURRENCY_MARKER.search(sentence):
        return bool(re.search(r"\d", sentence))
    return False


def _sanitize_value(value: Any, valuation: dict[str, Any] | None,
                    *, path: str, rejected: list[dict[str, str]]) -> tuple[Any, int]:
    if isinstance(value, str):
        if _is_unsupported_assertion(value, valuation):
            rejected.append({"path": path, "reason": "unsupported_valuation_assertion",
                             "content_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()})
            return _ABSENT, 1
        return value, 0
    if isinstance(value, list):
        sanitized: list[Any] = []
        removed = 0
        for index, item in enumerate(value):
            safe_item, count = _sanitize_value(item, valuation, path=f"{path}[{index}]", rejected=rejected)
            removed += count
            if isinstance(item, dict) and any(
                field in item for field in ("text", "conclusion", "argument")
            ) and isinstance(safe_item, dict) and not any(
                isinstance(safe_item.get(field), str) and safe_item.get(field).strip()
                for field in ("text", "conclusion", "argument")
            ):
                continue
            if safe_item is not _ABSENT and safe_item not in (None, "", [], {}):
                sanitized.append(safe_item)
        return sanitized, removed
    if isinstance(value, tuple):
        safe, removed = _sanitize_value(list(value), valuation, path=path, rejected=rejected)
        return tuple(safe), removed
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        removed = 0
        for key, item in value.items():
            if key in _METADATA:
                sanitized[key] = deepcopy(item)
                continue
            safe_item, count = _sanitize_value(item, valuation, path=f"{path}.{key}" if path else str(key), rejected=rejected)
            removed += count
            if safe_item is not _ABSENT and safe_item not in (None, "", [], {}):
                sanitized[key] = safe_item
        return sanitized, removed
    return value, 0


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
    try:
        encoded = json.dumps(sanitized, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return {}, {"schema": "valuation-content-proof-v2-whole-leaves", "input_sha256": None,
                    "valuation_available": False, "valuation_status": "unavailable",
                    "quarantined_count": 1, "quarantined_paths": ["$"],
                    "quarantined_nodes": [{"path": "$", "reason": "invalid_json_value"}]}
    rejected: list[dict[str, str]] = []
    valuation_value = valuation if isinstance(valuation, dict) else None
    usable = _valuation_is_usable(valuation_value)
    original_section = sanitized.get("implied_expectations")
    source_analysis, _ = _sanitize_value(original_section,
        valuation_value if usable else None, path="implied_expectations", rejected=rejected)
    # Preserve source-backed prose and links. Quantitative valuation fields
    # have one owner: the deterministic engine, not the model's copy.
    section = _valuation_analysis_only(source_analysis, path="implied_expectations", rejected=rejected)
    had_analysis = has_analysis(section)
    if isinstance(section.get("summary"), str):
        section["analysis"] = section.pop("summary")
    section.update({"summary": _localized_summary(
        usable, language,
        str(valuation_value.get("status", "")) if usable and valuation_value else "",
    ), "role": "verified_analysis" if had_analysis else "notice"})
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

    sensitivity_section = sanitized.get("valuation_and_sensitivity")
    if sensitivity_section is None and isinstance(original_section, dict):
        sensitivity_section = original_section.get("valuation_and_sensitivity")
    if sensitivity_section is not None:
        safe_sensitivity, _ = _sanitize_value(
            sensitivity_section, valuation_value if usable else None,
            path="valuation_and_sensitivity", rejected=rejected,
        )
        projected = _valuation_analysis_only(safe_sensitivity, path="valuation_and_sensitivity", rejected=rejected)
        projected["role"] = "verified_analysis" if has_analysis(projected) else "notice"
        if usable and isinstance(valuation_value.get("sensitivity"), list):
            projected["sensitivity"] = deepcopy(valuation_value["sensitivity"])
            if valuation_value.get("currency"):
                projected["currency"] = valuation_value["currency"]
        sanitized["valuation_and_sensitivity"] = projected
    for key, value in list(sanitized.items()):
        if key in {"implied_expectations", "valuation_and_sensitivity"}:
            continue
        safe_value, _ = _sanitize_value(value, valuation_value if usable else None,
                                           path=str(key), rejected=rejected)
        if safe_value is _ABSENT:
            del sanitized[key]
        else:
            sanitized[key] = safe_value
    return sanitized, {
        "valuation_available": usable,
        "valuation_status": str(valuation_value.get("status", "unavailable")) if valuation_value else "unavailable",
        "schema": "valuation-content-proof-v2-whole-leaves",
        "input_sha256": hashlib.sha256(encoded).hexdigest(),
        "quarantined_count": len(rejected),
        "quarantined_paths": list(dict.fromkeys(item["path"] for item in rejected)),
        "quarantined_nodes": rejected,
    }


def _valuation_analysis_only(value: Any, *, path: str, rejected: list[dict[str, str]]) -> dict[str, Any]:
    """Keep explanatory structure and citations, never model-computed amounts."""
    quantitative = frozenset({
        "implied_growth", "implied_fcf_growth", "required_growth", "implied_revenue_growth",
        "current_price", "share_price", "equity_market_value", "equity_value", "market_cap",
        "enterprise_value", "valuation", "valuation_range", "discount_rate", "wacc",
        "terminal_growth", "terminal_growth_rate", "horizon_years", "currency", "as_of",
        "sensitivity", "valuation_and_sensitivity", "fcf_growth", "base_free_cash_flow",
    })

    def retain(item: Any, location: str) -> Any:
        if isinstance(item, dict):
            kept = {}
            for key, child in item.items():
                if str(key).casefold() in quantitative:
                    if child not in (None, "", [], {}):
                        rejected.append({"path": f"{location}.{key}", "reason": "engine_owned_valuation_field"})
                elif not str(key).startswith("_"):
                    kept[key] = retain(child, f"{location}.{key}")
            return kept
        if isinstance(item, (list, tuple)):
            return [retain(child, f"{location}[{index}]") for index, child in enumerate(item)]
        return deepcopy(item)

    kept = retain(value, path) if value is not _ABSENT else None
    return kept if isinstance(kept, dict) else {"analysis": kept} if kept else {}
