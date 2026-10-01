"""Versioned immutable report document and its format-only renderers.

Artifacts are adapted once at the report boundary.  The resulting document
contains presentation-neutral block nodes and structured claim/source views;
the two renderers below only lay those blocks out and escape them.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
from html import escape
import re
from typing import Any, Sequence

from .i18n import EN, ZH_HANT, normalize_language
from .financials import (
    cash_conversion_definition,
    format_cash_conversion,
    format_growth,
    format_money,
    format_percent,
    reverse_dcf_disclaimer,
    reverse_dcf_status_text,
)


@dataclass(frozen=True, slots=True)
class MoneyFact:
    concept: str
    value: float
    currency: str
    unit_scale: float = 1.0
    unit_provenance: str = "normalized"
    scope: str = "consolidated"
    period: str = ""
    generation_id: str = ""
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportNode:
    kind: str
    text: str = ""
    rows: tuple[tuple[str, ...], ...] = ()
    level: int = 0
    confidence_tier: str = ""
    money_facts: tuple[MoneyFact, ...] = ()


@dataclass(frozen=True, slots=True)
class SectionBlock:
    section_id: str
    title: str
    body: str
    nodes: tuple[ReportNode, ...]
    claim_ids: tuple[str, ...] = ()
    assumption_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    state: str = "available"
    source_artifact_ids: tuple[str, ...] = ()
    verification_state: str = "needs_review"
    is_substantive: bool = False
    diagnostic_code: str = ""


@dataclass(frozen=True, slots=True)
class ClaimIdentity:
    company: str = ""
    concept: str = ""
    period: str = ""
    scope: str = ""
    currency: str = ""
    unit: str = ""


@dataclass(frozen=True, slots=True)
class ClaimView:
    claim_id: str
    text: str
    kind: str
    state: str
    identity: ClaimIdentity
    value: str | int | float | bool | None
    evidence_ids: tuple[str, ...] = ()
    assumption_id: str = ""


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_id: str
    document_id: str
    title: str
    url: str
    locators: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    evidence_locations: tuple[tuple[str, str], ...]
    document_only: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryDiagnostic:
    code: str
    severity: str
    location: str
    affected_ids: tuple[str, ...] = ()
    action: str = "Review the affected item; other report content remains available."


@dataclass(frozen=True, slots=True)
class RunDigest:
    run_id: str
    artifact_count: int
    final_report_available: bool
    continuity_state: str = "unknown"
    run_status: str = ""


@dataclass(frozen=True, slots=True)
class ReadinessView:
    state: str = "unknown"
    complete: bool = False
    substantive_sections: tuple[str, ...] = ()
    missing_sections: tuple[str, ...] = ()
    missing_stages: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    recovery_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "complete": self.complete,
            "substantive_sections": list(self.substantive_sections),
            "missing_sections": list(self.missing_sections),
            "missing_stages": list(self.missing_stages),
            "issues": list(self.issues),
            "recovery_action": self.recovery_action,
        }


@dataclass(frozen=True, slots=True)
class ReportDocument:
    schema_version: str
    run_id: str
    company_name: str
    language: str
    sections: tuple[SectionBlock, ...]
    claims: tuple[ClaimView, ...]
    sources: tuple[SourceRef, ...]
    readiness: ReadinessView
    delivery_diagnostics: tuple[DeliveryDiagnostic, ...]
    run_digest: RunDigest
    include_technical: bool = False


def _adapt_nested_expectation_narrative(artifacts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lift explicitly supported scenario analysis prose into a typed field.

    Some older payloads kept narrative one layer below ``analysis`` under
    ``revenue_path``/``evidence_basis``.  These named fields are safe prose,
    unlike arbitrary nested protocol strings; copy and normalize only these
    known paths for the read-only presentation adapter.
    """
    adapted = copy.deepcopy(list(artifacts))
    target = next((item for item in reversed(adapted) if item.get("artifact_type") == "research-report"), None)
    content = target.get("content") if isinstance(target, dict) else None
    report = content.get("report") if isinstance(content, dict) else None
    expectations = report.get("implied_expectations") if isinstance(report, dict) else None
    scenarios = expectations.get("scenarios") if isinstance(expectations, dict) else None
    if not isinstance(scenarios, dict):
        return adapted
    for scenario_name, scenario in scenarios.items():
        if scenario_name not in {"base", "bear", "bull"} or not isinstance(scenario, dict):
            continue
        analysis = scenario.get("analysis")
        if not isinstance(analysis, dict):
            continue
        narrative = " ".join(
            value.strip()
            for key in ("revenue_path", "evidence_basis")
            if isinstance((value := analysis.get(key)), str) and value.strip()
        )
        if narrative:
            scenario["analysis"] = {"summary": narrative}
    return adapted


def _value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def _slug(title: str, ordinal: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return slug or f"section-{ordinal}"


def _node_markdown(node: ReportNode) -> str:
    if node.kind in {"heading", "confidence_heading"}:
        level = 3 if node.kind == "confidence_heading" else min(6, max(1, node.level or 2))
        return f"{'#' * level} {node.text}"
    if node.kind == "quote":
        return f"> {node.text}"
    if node.kind == "list_item":
        return f"- {node.text}"
    if node.kind == "table" and node.rows:
        header, *body = node.rows
        separator = tuple("---" for _ in header)
        return "\n".join(
            "| " + " | ".join(row) + " |"
            for row in (header, separator, *body)
        )
    return node.text


def _nodes_markdown(nodes: Sequence[ReportNode]) -> str:
    return "\n\n".join(text for node in nodes if (text := _node_markdown(node)).strip())


def _localized_section_title(key: str, language: str) -> str:
    from .reporting import SECTION_LABELS_EN, SECTION_LABELS_HANT, SECTION_LABELS_ZH

    labels = SECTION_LABELS_EN if language == EN else SECTION_LABELS_HANT if language == ZH_HANT else SECTION_LABELS_ZH
    return labels.get(key, key)


def _scalar_text(value: Any, key: str | None, language: str) -> str:
    from .report_projection import format_report_semantic_value, report_display_value

    formatted = format_report_semantic_value(value, key)
    if formatted is not None:
        return formatted
    if isinstance(value, bool):
        return ("Yes" if value else "No") if language == EN else ("是" if value else "否")
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, str):
        return report_display_value(value, language)
    return str(value)


def _semantic_nodes(value: Any, language: str, *, depth: int = 0, field: str | None = None) -> list[ReportNode]:
    from .report_projection import report_field_label

    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [ReportNode("paragraph" if depth == 0 else "list_item", _scalar_text(value, field, language))]
    if isinstance(value, (int, float, bool)):
        return [ReportNode("list_item", _scalar_text(value, field, language))]
    if isinstance(value, dict):
        nodes: list[ReportNode] = []
        for key, child in value.items():
            if str(key).startswith("_") or child in (None, "", [], {}):
                continue
            if key == "evidence_count" and child == 0:
                continue
            label = report_field_label(key, language)
            if isinstance(child, dict):
                nodes.append(ReportNode("heading", label, level=min(5, 3 + depth)))
                nodes.extend(_semantic_nodes(child, language, depth=depth + 1, field=str(key)))
            elif isinstance(child, (list, tuple)):
                nodes.append(ReportNode("heading", label, level=min(5, 3 + depth)))
                nodes.extend(_semantic_nodes(child, language, depth=depth + 1, field=str(key)))
            else:
                nodes.append(ReportNode("list_item", f"**{label}:** {_scalar_text(child, str(key), language)}"))
        return nodes
    if isinstance(value, (list, tuple)):
        from .report_projection import format_report_semantic_value
        formatted = format_report_semantic_value(value, field)
        if formatted is not None:
            return [ReportNode("list_item", formatted)]
        nodes: list[ReportNode] = []
        for item in value:
            if isinstance(item, dict):
                item_nodes = _semantic_nodes(item, language, depth=depth + 1, field=field)
                if item_nodes:
                    nodes.extend(item_nodes)
            else:
                text = _scalar_text(item, field, language)
                if text:
                    nodes.append(ReportNode("list_item", text))
        return nodes
    return []


# Formal report sections use a declarative field order and explicit nested
# shapes.  `_semantic_nodes` remains for pre-contract staged artifacts only;
# model-controlled nested dictionaries must not define the report structure.
_SECTION_RENDER_ORDER: dict[str, tuple[str, ...]] = {
    "executive_summary": ("summary", "text", "body", "analysis", "conclusion"),
    "business_model": ("summary", "body", "analysis", "conclusion", "possible_moats", "strengths", "concerns", "risks", "unknowns"),
    "financial_quality": ("summary", "body", "analysis", "conclusion", "financial_analysis", "accounting_risk", "strengths", "concerns", "risks", "risk_flags", "benign_explanations", "follow_up_questions", "unknowns"),
    "balance_sheet": ("summary", "body", "analysis", "conclusion", "assets", "liabilities", "equity", "total_equity", "strengths", "concerns", "risks", "risk_flags", "benign_explanations", "follow_up_questions", "unknowns"),
    "competitive_position": ("summary", "body", "analysis", "conclusion", "possible_moats", "strengths", "concerns", "risks", "unknowns"),
    "counterarguments": ("title", "strongest_counterarguments", "counterargument", "argument", "text", "body", "unsupported_assumptions", "missing_evidence", "claims", "severity", "confidence"),
    "scenarios": ("summary", "body", "analysis", "conclusion", "base", "bear", "bull", "scenarios", "probability", "probability_range", "assumptions", "text"),
    "implied_expectations": ("summary", "body", "analysis", "conclusion", "current_price", "share_price", "market_cap", "enterprise_value", "equity_value", "currency", "as_of", "valuation", "valuation_range", "implied_growth", "implied_revenue_growth", "required_growth", "required_return", "expected_return", "revenue_cagr_range", "operating_margin_range", "probability", "probability_range", "discount_rate", "wacc", "terminal_growth_rate", "margin_of_safety", "formula", "source", "evidence", "metrics", "horizon_years", "market_assumption", "sensitivity_note", "assumptions", "scenarios", "confidence"),
    "thesis": ("summary", "body", "analysis", "conclusion", "text", "claims", "assumptions", "confidence"),
    "invalidation_conditions": ("title", "condition", "text", "body", "trigger", "confidence"),
    "leading_indicators": ("title", "indicator", "metric", "text", "body", "confidence"),
    "unresolved_questions": ("title", "question", "text", "body", "confidence"),
}

_NESTED_RENDER_FIELDS: dict[str, tuple[str, ...]] = {
    "possible_moats": ("title", "kind", "description", "durability", "summary", "text"),
    "risks": ("title", "text", "body", "analysis", "conclusion", "description", "severity", "condition", "confidence", "evidence_count"),
    "risk_flags": ("title", "text", "body", "description", "severity", "confidence"),
    "unknowns": ("title", "question", "text", "body", "description", "confidence"),
    "strengths": ("title", "text", "body", "analysis", "description", "confidence"),
    "concerns": ("title", "text", "body", "analysis", "description", "severity", "confidence"),
    "benign_explanations": ("title", "text", "body", "description", "confidence"),
    "follow_up_questions": ("title", "question", "text", "body", "indicator", "metric"),
    "financial_analysis": ("summary", "text", "body", "analysis", "conclusion", "risks", "strengths", "concerns", "unknowns"),
    "accounting_risk": ("summary", "text", "body", "analysis", "conclusion", "risks", "risk_flags", "unknowns"),
    "strongest_counterarguments": ("title", "text", "body", "argument", "counterargument", "severity", "confidence", "evidence_count"),
    "unsupported_assumptions": ("title", "text", "body", "assumption", "assumptions", "confidence"),
    "missing_evidence": ("title", "text", "body", "question", "description"),
    "claims": ("title", "text", "conclusion", "argument", "kind", "confidence"),
    "assumptions": ("title", "text", "body", "assumption", "description", "confidence"),
    "base": ("summary", "text", "body", "analysis", "conclusion", "probability", "probability_range", "revenue_cagr_range", "operating_margin_range", "assumptions", "invalidation_conditions", "leading_indicators"),
    "bear": ("summary", "text", "body", "analysis", "conclusion", "probability", "probability_range", "revenue_cagr_range", "operating_margin_range", "assumptions", "invalidation_conditions", "leading_indicators"),
    "bull": ("summary", "text", "body", "analysis", "conclusion", "probability", "probability_range", "revenue_cagr_range", "operating_margin_range", "assumptions", "invalidation_conditions", "leading_indicators"),
    "scenarios": ("title", "name", "base", "bear", "bull", "summary", "text", "body", "analysis", "conclusion", "probability", "probability_range", "cagr", "revenue_cagr_range", "operating_margin_range", "assumptions", "invalidation_conditions", "leading_indicators"),
    "analysis": ("revenue_path", "evidence_basis", "summary", "text", "body", "conclusion"),
    "metrics": ("title", "metric", "summary", "text", "value", "current", "required", "unit", "source", "as_of", "confidence"),
    "evidence": ("title", "text", "description", "source", "as_of", "confidence"),
    "invalidation_conditions": ("title", "condition", "text", "body", "trigger", "confidence"),
    "leading_indicators": ("title", "indicator", "metric", "text", "body", "confidence"),
}

_UNLABELED_PROSE_FIELDS = frozenset({"summary", "body", "analysis", "conclusion", "text"})


def _typed_section_nodes(section: str, value: Any, language: str) -> tuple[ReportNode, ...]:
    """Render a declared report section without recursively trusting its shape."""
    from .report_projection import report_field_label, typed_section_fields

    allowed = typed_section_fields(section)
    order = _SECTION_RENDER_ORDER.get(section, ())
    if not allowed:
        return ()

    def ordered_keys(item: dict[str, Any], fields: tuple[str, ...] | frozenset[str]) -> tuple[str, ...]:
        preferred = order if fields is allowed else fields
        return tuple(
            key for key in dict.fromkeys((*preferred, *sorted(fields)))
            if key in fields and key in item
        )

    def scalar_node(item: Any, field: str, depth: int) -> list[ReportNode]:
        if item is None or item == "":
            return []
        label = report_field_label(field, language)
        if isinstance(item, (list, tuple)):
            from .report_projection import format_report_semantic_value
            formatted = format_report_semantic_value(item, field)
            if formatted is not None:
                return [ReportNode("list_item", formatted)]
        if isinstance(item, (dict, list, tuple)):
            child_fields = _NESTED_RENDER_FIELDS.get(field)
            if child_fields is None:
                return []
            nested = render_value(item, field, child_fields, depth + 1)
            if not nested:
                return []
            return [ReportNode("heading", label, level=min(5, 3 + depth)), *nested]
        rendered = _scalar_text(item, field, language)
        if field == "title":
            return [ReportNode("heading", rendered, level=min(5, 3 + depth))]
        if depth == 0 and field in _UNLABELED_PROSE_FIELDS:
            return [ReportNode("paragraph", rendered)]
        if depth > 0 and field in _UNLABELED_PROSE_FIELDS:
            return [ReportNode("list_item", rendered)]
        separator = ":" if language == EN else "："
        return [ReportNode("list_item", f"**{label}{separator}** {rendered}")]

    def render_mapping(item: dict[str, Any], fields: tuple[str, ...] | frozenset[str], depth: int) -> list[ReportNode]:
        nodes: list[ReportNode] = []
        for key in ordered_keys(item, fields):
            nodes.extend(scalar_node(item[key], key, depth))
        return nodes

    def render_value(item: Any, field: str, fields: tuple[str, ...], depth: int) -> list[ReportNode]:
        if isinstance(item, dict):
            return render_mapping(item, fields, depth)
        if isinstance(item, (list, tuple)):
            from .report_projection import format_report_semantic_value
            formatted = format_report_semantic_value(item, field)
            if formatted is not None:
                return [ReportNode("list_item", formatted)]
            nodes: list[ReportNode] = []
            for child in item:
                if isinstance(child, dict):
                    child_nodes = render_mapping(child, fields, depth)
                    if child_nodes:
                        nodes.extend(child_nodes)
                elif child not in (None, ""):
                    nodes.append(ReportNode("list_item", _scalar_text(child, field, language)))
            return nodes
        return scalar_node(item, field, depth)

    if isinstance(value, dict):
        return tuple(render_mapping(value, allowed, 0))
    if isinstance(value, (list, tuple)):
        return tuple(render_value(value, section, order or tuple(allowed), 0))
    if value in (None, ""):
        return ()
    return (ReportNode("paragraph", _scalar_text(value, section, language)),)


def _claim_nodes(claims: Sequence[ClaimView], raw_claims: Any, language: str) -> tuple[ReportNode, ...]:
    from .report_projection import report_display_value

    raw_by_id = {
        str(item.get("claim_id", f"claim-{index + 1}")): item
        for index, item in enumerate(raw_claims if isinstance(raw_claims, list) else ())
        if isinstance(item, dict)
    }
    grouped: dict[str, dict[str, list[ClaimView]]] = {"high": {}, "medium": {}, "low": {}, "unknown": {}}
    for claim in claims:
        try:
            confidence = float(raw_by_id.get(claim.claim_id, {}).get("confidence"))
        except (TypeError, ValueError):
            confidence = -1.0
        tier = "high" if confidence >= 0.8 else "medium" if confidence >= 0.55 else "low" if confidence >= 0 else "unknown"
        confidence_key = f"{confidence:.2f}" if confidence >= 0 else ""
        grouped[tier].setdefault(confidence_key, []).append(claim)
    labels = {
        "high": ("High confidence", "高置信度", "高信心程度"),
        "medium": ("Medium confidence", "中等置信度", "中等信心程度"),
        "low": ("Low confidence", "低置信度", "低信心程度"),
        "unknown": ("Confidence not provided", "置信度未提供", "信心程度未提供"),
    }
    nodes: list[ReportNode] = []
    for tier in ("high", "medium", "low", "unknown"):
        confidence_groups = grouped[tier]
        ordered_confidences = sorted(confidence_groups, key=lambda item: float(item) if item else -1, reverse=True)
        for confidence_key in ordered_confidences:
            items = confidence_groups[confidence_key]
            if tier != "unknown":
                title = labels[tier][0] if language == EN else labels[tier][2] if language == ZH_HANT else labels[tier][1]
                if language == EN:
                    heading = f"{title} · {confidence_key} · {len(items)} conclusions" if confidence_key else f"{title} · {len(items)}"
                else:
                    count_unit = "條" if language == ZH_HANT else "条"
                    heading = f"{title} · {confidence_key} · {len(items)} {count_unit}" if confidence_key else f"{title} · {len(items)} {count_unit}"
                nodes.append(ReportNode("confidence_heading", heading, confidence_tier=tier))
            for claim in items:
                if not claim.text:
                    warning = (
                        "Claim text has an invalid structure and was not displayed."
                        if language == EN else
                        "結論正文欄位格式錯誤，內容未顯示。"
                        if language == ZH_HANT else
                        "正文文本字段类型错误，内容未显示。"
                    )
                    nodes.append(ReportNode("list_item", warning, confidence_tier=tier))
                    continue
                raw = raw_by_id.get(claim.claim_id, {})
                kind = report_display_value(claim.kind, language)
                line = f"**{kind}** · {claim.text}"
                evidence_count = raw.get("evidence_count")
                if isinstance(evidence_count, int) and evidence_count > 0:
                    note = f" · {evidence_count} valid references" if language == EN else f" · {evidence_count} 條有效引用" if language == ZH_HANT else f" · {evidence_count} 条有效引用"
                    line += note
                nodes.append(ReportNode("list_item", line, confidence_tier=tier))
    return tuple(nodes)


def _section(
    ordinal: int,
    section_id: str,
    title: str,
    nodes: Sequence[ReportNode],
    *,
    claim_ids: tuple[str, ...] = (),
    assumption_ids: tuple[str, ...] = (),
    evidence_ids: tuple[str, ...] = (),
    source_artifact_ids: tuple[str, ...] = (),
    verification_state: str = "needs_review",
    diagnostic_code: str = "",
    is_substantive: bool | None = None,
) -> SectionBlock:
    immutable_nodes = tuple(nodes)
    return SectionBlock(
        section_id=section_id or _slug(title, ordinal),
        title=title,
        body=_nodes_markdown(immutable_nodes),
        nodes=immutable_nodes,
        claim_ids=claim_ids,
        assumption_ids=assumption_ids,
        evidence_ids=evidence_ids,
        state="available" if any(node.text or node.rows for node in immutable_nodes) else "needs_review",
        source_artifact_ids=source_artifact_ids,
        verification_state=verification_state,
        is_substantive=(
            _section_is_substantive(section_id, immutable_nodes)
            if is_substantive is None else is_substantive
        ),
        diagnostic_code=diagnostic_code,
    )


_NON_SUBSTANTIVE_SECTION_IDS = {
    "report-identity", "research-status", "financial-refresh-status",
    "staged-report-status", "context-capacity-status", "ai-research-status",
    "financial-data-quality", "verification-results", "evidence-sources",
    "research-process", "methodology", "technical-details",
}


def _section_is_substantive(section_id: str, nodes: Sequence[ReportNode]) -> bool:
    """Count domain content, not headings, notices, or presentation metadata."""
    if section_id in _NON_SUBSTANTIVE_SECTION_IDS:
        return False
    if section_id not in {
        "financial-overview", "deterministic-balance-sheet", "deterministic-valuation",
        "executive_summary", "claims", "business_model", "financial_quality",
        "balance_sheet", "competitive_position", "growth_opportunities",
        "counterarguments", "scenarios", "thesis", "invalidation_conditions",
        "leading_indicators", "unresolved_questions", "opposing-views",
    }:
        return False
    for node in nodes:
        if node.kind == "table" and len(node.rows) > 1:
            if any(
                str(cell).strip() not in {
                    "", "—", "-", "n/a", "N/A", "待核验", "待核實",
                    "Pending verification", "Unit pending verification",
                    "单位待核实", "單位待核實",
                }
                for row in node.rows[1:]
                for cell in row[1:]
            ):
                return True
        elif node.kind in {"paragraph", "list_item", "quote"}:
            text = re.sub(r"[\s*_`]+", "", str(node.text or ""))
            if len(text) >= 12 and text not in {"—", "-", "未知", "Unknown", "待核验", "待核實"}:
                return True
    return False


def _money_cell(
    concept: str,
    value: Any,
    metadata: Any,
    currency: str,
    period: str,
    language: str,
    generations: dict[str, str],
) -> tuple[str, MoneyFact | None]:
    if value is None:
        return "—", None
    meta = metadata if isinstance(metadata, dict) else {}
    stated_currency = str(meta.get("currency") or currency or "").upper()
    provenance = str(meta.get("unit_provenance") or "")
    if not provenance and stated_currency:
        # Historical deterministic-summary artifacts predate per-value
        # provenance. Their values were produced by the normalized metrics
        # engine; preserve that compatibility contract explicitly.
        provenance = "legacy_deterministic_summary_contract"
    if not stated_currency or provenance == "unknown":
        missing = (
            "Unit pending verification" if language == EN else
            "單位待核實" if language == ZH_HANT else
            "单位待核实"
        )
        fact = MoneyFact(
            concept=concept, value=float(value), currency=stated_currency,
            unit_scale=1.0, unit_provenance="unknown",
            scope=str(meta.get("scope") or "unknown"), period=period,
            generation_id=str(meta.get("generation_id") or generations.get(str(meta.get("accession_number", "")), "")),
            evidence_ids=tuple(str(item) for item in meta.get("evidence_ids", ()) if item),
        )
        return missing, fact
    fact = MoneyFact(
        concept=concept,
        value=float(value),
        currency=stated_currency,
        unit_scale=1.0,
        unit_provenance=provenance,
        scope=str(meta.get("scope") or "consolidated"),
        period=period,
        generation_id=str(meta.get("generation_id") or generations.get(str(meta.get("accession_number", "")), "")),
        evidence_ids=tuple(str(item) for item in meta.get("evidence_ids", ()) if item),
    )
    return format_money(float(value), stated_currency), fact


def _free_cash_flow_gap_label(reason: Any, language: str) -> str:
    english = {
        "missing_operating_cash_flow": "operating cash flow is missing",
        "missing_capital_expenditure": "capital expenditure is missing",
        "missing_source_metadata": "source currency, scope, period, or provenance is missing",
        "currency_mismatch": "input currencies differ",
        "scope_mismatch": "input consolidation scopes differ",
        "generation_mismatch": "inputs come from different accepted data generations",
        "period_mismatch": "inputs do not cover the same annual period",
        "period_end_mismatch": "input fiscal year-end dates differ",
        "missing_currency": "input currency metadata is missing",
        "missing_scope": "input consolidation scope is missing",
        "missing_generation_id": "accepted data-generation identity is missing",
        "missing_period": "annual-period metadata is missing",
        "missing_period_end": "fiscal year-end metadata is missing",
        "unit_unverified": "one or more input units are unverified",
        "missing_source_fact": "source fact identity is missing",
        "inputs_not_comparable": "inputs could not be verified as comparable",
    }
    simplified = {
        "missing_operating_cash_flow": "缺少已验证的经营现金流",
        "missing_capital_expenditure": "缺少已验证的资本开支",
        "missing_source_metadata": "缺少币种、合并口径、期间或来源属性",
        "currency_mismatch": "输入币种不一致",
        "scope_mismatch": "输入合并口径不一致",
        "generation_mismatch": "输入来自不同的有效数据代次",
        "period_mismatch": "输入不属于同一完整财年",
        "period_end_mismatch": "财年截止日期不一致",
        "missing_currency": "缺少输入币种",
        "missing_scope": "缺少输入合并口径",
        "missing_generation_id": "缺少有效数据代次标识",
        "missing_period": "缺少年度期间信息",
        "missing_period_end": "缺少财年截止日期",
        "unit_unverified": "至少一项输入的单位尚未验证",
        "missing_source_fact": "缺少来源事实标识",
        "inputs_not_comparable": "无法确认输入数据具备可比性",
    }
    traditional = {
        "missing_operating_cash_flow": "缺少已驗證的經營現金流",
        "missing_capital_expenditure": "缺少已驗證的資本開支",
        "missing_source_metadata": "缺少幣別、合併範圍、期間或來源屬性",
        "currency_mismatch": "輸入幣別不一致",
        "scope_mismatch": "輸入合併範圍不一致",
        "generation_mismatch": "輸入來自不同的有效資料代次",
        "period_mismatch": "輸入不屬於同一完整財年",
        "period_end_mismatch": "財年截止日期不一致",
        "missing_currency": "缺少輸入幣別",
        "missing_scope": "缺少輸入合併範圍",
        "missing_generation_id": "缺少有效資料代次標識",
        "missing_period": "缺少年度期間資訊",
        "missing_period_end": "缺少財年截止日期",
        "unit_unverified": "至少一項輸入的單位尚未驗證",
        "missing_source_fact": "缺少來源事實標識",
        "inputs_not_comparable": "無法確認輸入資料具備可比性",
    }
    mapping = english if language == EN else traditional if language == ZH_HANT else simplified
    return mapping.get(str(reason or ""), mapping["inputs_not_comparable"])


def _financial_nodes(
    content: dict[str, Any], language: str,
) -> tuple[tuple[ReportNode, ...], tuple[ReportNode, ...]]:
    metrics = content.get("metrics")
    if not isinstance(metrics, list):
        return (), ()
    currency = str(content.get("currency", "") or "").upper()
    generations = content.get("financial_generations", {})
    if not isinstance(generations, dict):
        generations = {}
    generations = {str(key): str(value) for key, value in generations.items()}
    english = language == EN
    traditional = language == ZH_HANT
    column_labels = {
        "revenue": ("Revenue", "營業收入", "营业收入"),
        "revenue_growth": ("Revenue growth", "收入增長", "收入增长"),
        "operating_margin": ("Operating margin", "營業利潤率", "营业利润率"),
        "net_income": ("Net income", "淨利潤", "净利润"),
        "operating_cash_flow": ("Operating cash flow", "經營現金流", "经营现金流"),
        "free_cash_flow": ("Free cash flow", "自由現金流", "自由现金流"),
    }
    annual_concepts = ["revenue"]
    annual_concepts.append("revenue_growth")
    for concept in ("operating_margin", "net_income", "operating_cash_flow", "free_cash_flow"):
        if any(
            isinstance(item, dict)
            and (item.get(concept) is not None or item.get(f"{concept}_status"))
            for item in metrics
        ):
            annual_concepts.append(concept)
    year_label = "Fiscal year" if english else "財年" if traditional else "财年"
    headers = (year_label, *(column_labels[key][0 if english else 1 if traditional else 2] for key in annual_concepts))
    rows: list[tuple[str, ...]] = []
    money_facts: list[MoneyFact] = []
    interim_money_facts: list[MoneyFact] = []
    balance_rows: list[tuple[str, ...]] = []
    balance_facts: list[MoneyFact] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        year = metric.get("year")
        if year is None:
            continue
        period = f"FY{year}"
        row = [str(year)]
        for concept in annual_concepts:
            value = metric.get(concept)
            if concept == "revenue_growth":
                row.append(format_growth(value, metric.get("revenue_growth_status"), language))
            elif concept == "operating_margin":
                row.append(format_percent(value))
            elif concept == "free_cash_flow" and metric.get("free_cash_flow_gap"):
                row.append(_free_cash_flow_gap_label(metric.get("free_cash_flow_gap"), language))
            else:
                cell, fact = _money_cell(
                    concept, value,
                    metric.get("money_metadata", {}).get(concept)
                    if isinstance(metric.get("money_metadata"), dict) else None,
                    currency, period, language, generations,
                )
                row.append(cell)
                if fact is not None:
                    money_facts.append(MoneyFact(
                        fact.concept, fact.value, fact.currency, fact.unit_scale,
                        fact.unit_provenance, fact.scope, fact.period,
                        fact.generation_id, fact.evidence_ids,
                    ))
        rows.append(tuple(row))
        balance_row = [str(year)]
        for concept in ("assets", "liabilities", "equity", "total_equity"):
            value = metric.get(concept)
            metadata = metric.get("money_metadata", {})
            cell, fact = _money_cell(
                concept, value,
                metadata.get(concept) if isinstance(metadata, dict) else None,
                currency, period, language, generations,
            )
            balance_row.append(cell)
            if fact is not None:
                balance_facts.append(fact)
        if any(value != "—" for value in balance_row[1:]):
            balance_rows.append(tuple(balance_row))

    visible_annual_indexes = [0] + [
        index for index in range(1, len(headers))
        if any(row[index] not in {"", "—"} for row in rows)
    ]
    headers = tuple(headers[index] for index in visible_annual_indexes)
    rows = [tuple(row[index] for index in visible_annual_indexes) for row in rows]
    visible_annual_concepts = {
        annual_concepts[index - 1]
        for index in visible_annual_indexes if index > 0
    }
    money_facts = [fact for fact in money_facts if fact.concept in visible_annual_concepts]
    nodes = (
        [
            ReportNode("paragraph", "以下为通过确定性财务引擎整理的年度指标。" if not english else "Annual metrics below are assembled by the deterministic financial engine." if not traditional else "以下為由確定性財務引擎整理的年度指標。"),
            ReportNode("table", rows=(headers, *rows), money_facts=tuple(money_facts)),
        ]
        if rows and len(headers) > 1 else []
    )
    interim = content.get("interim_metrics")
    if isinstance(interim, list):
        interim_rows: list[tuple[str, ...]] = []
        for item in interim:
            if not isinstance(item, dict):
                continue
            period = f"{item.get('year', '')} {item.get('period', '')}".strip()
            growth = item.get("revenue_growth")
            interim_cells: list[str] = []
            for concept in ("revenue", "net_income", "operating_cash_flow"):
                cell, fact = _money_cell(
                    concept, item.get(concept), None, currency, period,
                    language, generations,
                )
                interim_cells.append(cell)
                if fact is not None:
                    interim_money_facts.append(fact)
            interim_rows.append((
                period,
                interim_cells[0],
                format_percent(growth) if not item.get("comparison_gap") else ("Unavailable" if english else "未提供" if not traditional else "未提供"),
                interim_cells[1],
                interim_cells[2],
            ))
        if interim_rows:
            interim_title = "Latest interim and quarterly data" if english else "最新季度及中期數據" if traditional else "最新季度及中期数据"
            interim_headers = ("Period", "Revenue", "Comparable growth", "Net income", "Operating cash flow") if english else ("期間", "營業收入", "同期增長", "淨利潤", "經營現金流") if traditional else ("期间", "营业收入", "同期增长", "净利润", "经营现金流")
            nodes.append(ReportNode("heading", interim_title, level=3))
            interim_period = next((f"{item.get('year', '')} {item.get('period', '')}".strip() for item in interim if isinstance(item, dict)), "")
            nodes.append(ReportNode("quote", f"{interim_period} is cumulative period data and is not mixed with full fiscal years." if english else f"{interim_period} 為累計期間資料，不與完整財年混算。" if traditional else f"{interim_period} 为累计期间数据，不与完整财年混算。"))
            comparison = next((item.get("comparison_period") for item in interim if isinstance(item, dict) and item.get("comparison_period")), None)
            comparison_gap = next((item.get("comparison_gap") for item in interim if isinstance(item, dict) and item.get("comparison_gap")), None)
            if comparison:
                comparison_note = (
                    f"Compared with {comparison}." if english else
                    f"對比 {comparison}。" if traditional else
                    f"对比 {comparison}。"
                )
                nodes.append(ReportNode("paragraph", comparison_note))
            elif comparison_gap:
                nodes.append(ReportNode("paragraph", "Revenue growth: prior-year comparable disclosure is missing or did not pass validation." if english else "同期收入增長：缺少或未通過校驗的上年同期披露。" if traditional else "同期收入增长：缺少或未通过校验的上年同期披露。"))
            nodes.append(ReportNode("table", rows=(interim_headers, *interim_rows), money_facts=tuple(interim_money_facts)))

    latest = next((item for item in metrics if isinstance(item, dict)), None)
    if latest is not None:
        detail_items: list[tuple[str, str, bool]] = [
            (("Cash conversion" if english else "現金利潤轉化率" if traditional else "现金利润转化率"), format_cash_conversion(latest, language), "cash_conversion_status" in latest),
            (("Definition" if english else "計算口徑" if traditional else "计算口径"), cash_conversion_definition(latest, language), "cash_conversion_status" in latest),
            (("Gross margin" if english else "毛利率"), format_percent(latest.get("gross_margin")), latest.get("gross_margin") is not None),
            (("Net income growth" if english else "淨利潤增長" if traditional else "净利润增长"), format_growth(latest.get("net_income_growth"), latest.get("net_income_growth_status"), language), latest.get("net_income_growth") is not None or latest.get("net_income_growth_status") is not None),
            (("Operating cash-flow growth" if english else "經營現金流增長" if traditional else "经营现金流增长"), format_growth(latest.get("operating_cash_flow_growth"), latest.get("operating_cash_flow_growth_status"), language), latest.get("operating_cash_flow_growth") is not None or latest.get("operating_cash_flow_growth_status") is not None),
        ]
        visible_details = [(label, value) for label, value, visible in detail_items if visible]
        if visible_details:
            nodes.extend((
                ReportNode("heading", "Growth and quality metrics" if english else "增長與品質指標" if traditional else "增长与质量指标", level=3),
                *(ReportNode("list_item", f"**{label}:** {value}" if english else f"**{label}：** {value}") for label, value in visible_details),
            ))
        tensions = [
            tension
            for metric in metrics
            if isinstance(metric, dict)
            for tension in metric.get("financial_tensions", [])
            if isinstance(tension, dict)
            and isinstance(tension.get("observations"), dict)
        ]
        if tensions:
            nodes.append(ReportNode(
                "heading",
                "Earnings and cash-flow divergence" if english else "盈利與現金流背離" if traditional else "盈利与现金流背离",
                level=3,
            ))
            for tension in tensions:
                observations = tension["observations"]
                year = tension.get("period", "")
                net_growth = format_percent(observations.get("net_income_growth"))
                cash_growth = format_percent(observations.get("operating_cash_flow_growth"))
                nodes.append(ReportNode(
                    "quote",
                    f"FY{year}: net income grew {net_growth}, while operating cash flow changed {cash_growth}." if english else
                    f"{year} 財年：淨利潤增長 {net_growth}，經營現金流變動 {cash_growth}。" if traditional else
                    f"{year} 财年：净利润增长 {net_growth}，经营现金流变动 {cash_growth}。",
                ))
                nodes.append(ReportNode(
                    "paragraph",
                    "The cause is not established by the verified inputs used in this report; no causal explanation is inferred." if english else
                    "本報告已核驗的輸入尚未確認背離原因，因此不推斷具體成因。" if traditional else
                    "本报告已验证的输入尚未确认背离原因，因此不推断具体成因。",
                ))
        gap_notes: list[str] = []
        for metric in metrics[:5]:
            if not isinstance(metric, dict) or metric.get("revenue_growth") is not None:
                continue
            gap = str(metric.get("comparison_gap") or "")
            if gap.startswith("missing_"):
                previous_year = gap.removeprefix("missing_")
                gap_notes.append(
                    f"Fiscal {metric.get('year')} revenue growth is missing verified fiscal {previous_year} revenue." if english else
                    f"{metric.get('year')} 財年收入增長缺少 {previous_year} 財年已驗證收入" if traditional else
                    f"{metric.get('year')} 财年收入增长缺少 {previous_year} 财年已验证收入"
                )
        for metric in metrics[:5]:
            if not isinstance(metric, dict) or not metric.get("free_cash_flow_gap"):
                continue
            period = f"FY{metric.get('year')}" if english else f"{metric.get('year')} 財年" if traditional else f"{metric.get('year')} 财年"
            gap_notes.append(
                f"{period} free cash flow was not calculated: {_free_cash_flow_gap_label(metric['free_cash_flow_gap'], language)}"
                if english else
                f"{period} 自由現金流未計算：{_free_cash_flow_gap_label(metric['free_cash_flow_gap'], language)}"
                if traditional else
                f"{period} 自由现金流未计算：{_free_cash_flow_gap_label(metric['free_cash_flow_gap'], language)}"
            )
        if latest.get("return_on_equity") is None:
            reasons = {
                "missing_net_income": ("net income is missing", "缺少淨利潤資料", "缺少净利润数据"),
                "missing_equity": ("equity data is missing", "缺少權益資料", "缺少权益数据"),
                "non_positive_equity": ("equity is zero or negative; not applicable", "權益為零或負數，不適用", "权益为零或负数，不适用"),
            }
            reason = reasons.get(str(latest.get("return_on_equity_gap") or ""))
            if reason:
                gap_notes.append(
                    f"Return on equity could not be calculated: {reason[0]}" if english else
                    f"淨資產收益率無法計算：{reason[1]}" if traditional else
                    f"净资产收益率无法计算：{reason[2]}"
                )
        if gap_notes:
            nodes.append(ReportNode("heading", "Financial data gaps" if english else "財務數據缺口" if traditional else "财务数据缺口", level=3))
            nodes.extend(ReportNode("list_item", note) for note in gap_notes)
    overview_nodes = tuple(
        ReportNode(node.kind, node.text, node.rows, node.level, node.confidence_tier,
                   tuple(money_facts) if node.kind == "table" and node.rows and node.rows[0] == headers else node.money_facts)
        for node in nodes
    )
    balance_concepts = ["assets", "liabilities", "equity", "total_equity"]
    visible_balance_indexes = [
        index for index, concept in enumerate(balance_concepts, 1)
        if any(row[index] != "—" for row in balance_rows)
    ]
    # Equity attributable to parent and total equity are distinct concepts;
    # collapse them only when every observed value and provenance record is
    # literally identical, which indicates a duplicated source projection.
    equity_duplicate = bool(metrics) and all(
        not isinstance(metric, dict)
        or metric.get("equity") is None and metric.get("total_equity") is None
        or metric.get("equity") == metric.get("total_equity")
        and (metric.get("money_metadata", {}).get("equity") if isinstance(metric.get("money_metadata"), dict) else None)
        == (metric.get("money_metadata", {}).get("total_equity") if isinstance(metric.get("money_metadata"), dict) else None)
        for metric in metrics
    )
    if equity_duplicate and 4 in visible_balance_indexes:
        visible_balance_indexes.remove(4)
        balance_facts = [fact for fact in balance_facts if fact.concept != "total_equity"]
    visible_balance_concepts = {
        balance_concepts[index - 1] for index in visible_balance_indexes
    }
    balance_facts = [fact for fact in balance_facts if fact.concept in visible_balance_concepts]
    balance_rows = [
        tuple(row[index] for index in [0, *visible_balance_indexes])
        for row in balance_rows
    ]
    balance_nodes: list[ReportNode] = []
    if balance_rows:
        balance_title = "Balance-sheet data" if english else "資產負債表數據" if traditional else "资产负债表数据"
        all_balance_headers = (
            ("Fiscal year", "Assets", "Liabilities", "Equity attributable to parent", "Total equity")
            if english else
            ("財年", "資產", "負債", "歸屬母公司權益", "權益合計")
            if traditional else
            ("财年", "资产", "负债", "归属于母公司权益", "所有者权益合计")
        )
        balance_headers = tuple(all_balance_headers[index] for index in [0, *visible_balance_indexes])
        balance_nodes.extend((
            ReportNode("heading", balance_title, level=3),
            ReportNode("table", rows=(balance_headers, *balance_rows), money_facts=tuple(balance_facts)),
        ))
    return overview_nodes, tuple(balance_nodes)


def _artifact_label(artifact: dict[str, Any], language: str) -> str:
    from .reporting import ARTIFACT_LABELS

    labels = ARTIFACT_LABELS.get(str(artifact.get("artifact_type", "")), ("研究资料", "Research material"))
    return labels[1] if language == EN else labels[0]


_REPORT_SECTION_KEYS = (
    "executive_summary", "claims", "business_model", "financial_quality",
    "balance_sheet", "competitive_position", "growth_opportunities",
    "counterarguments", "scenarios", "implied_expectations", "thesis",
    "invalidation_conditions", "leading_indicators", "unresolved_questions",
)


def _stage_section_fallbacks(
    artifacts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Project known completed-stage payloads when final synthesis is absent/incomplete.

    This adapter deliberately routes only named report fields. Stage artifacts
    remain typed inputs; arbitrary protocol keys are still filtered later by
    ``project_report_value``.
    """
    collected: dict[str, list[Any]] = {}

    def add(section: str, value: Any) -> None:
        if section not in _REPORT_SECTION_KEYS or value in (None, "", [], {}):
            return
        collected.setdefault(section, []).append(value)

    for artifact in artifacts:
        content = artifact.get("content")
        if not isinstance(content, dict):
            continue
        artifact_type = artifact.get("artifact_type")
        if artifact_type == "verified-research-dossier":
            # The dossier is an evidence boundary: do not render compatibility
            # payloads that were never recorded as passing verification.
            analyses = content.get("verified_analyses", {})
            if isinstance(analyses, dict):
                for agent_id, analysis in analyses.items():
                    if not isinstance(analysis, dict):
                        continue
                    for key in _REPORT_SECTION_KEYS:
                        value = analysis.get(key)
                        if value in (None, "", [], {}):
                            continue
                        if key == "claims" and isinstance(value, list):
                            normalized_claims = []
                            for index, claim in enumerate(value):
                                if not isinstance(claim, dict):
                                    continue
                                normalized = dict(claim)
                                normalized.setdefault("claim_id", f"{agent_id}-claim-{index + 1}")
                                normalized_claims.append(normalized)
                            add(key, normalized_claims)
                        else:
                            add(key, value)
        elif artifact_type == "growth-opportunities":
            add("growth_opportunities", content)
        elif artifact_type == "counter-analysis":
            if content.get("counterarguments") not in (None, "", [], {}):
                add("counterarguments", content["counterarguments"])
            elif any(content.get(key) not in (None, "", [], {}) for key in (
                "strongest_counterarguments", "unsupported_assumptions", "missing_evidence"
            )):
                add("counterarguments", content)
        elif artifact_type == "forecast-scenarios":
            add("scenarios", content.get("scenarios", content))
        elif artifact_type == "deterministic-valuation":
            add("implied_expectations", content.get("implied_expectations", content))
        elif artifact_type == "thesis-snapshot":
            snapshot = content.get("content", content)
            if isinstance(snapshot, dict):
                for key in _REPORT_SECTION_KEYS:
                    add(key, snapshot.get(key))

    result: dict[str, Any] = {}
    for key, values in collected.items():
        if key in {"claims", "counterarguments"}:
            flattened: list[Any] = []
            for value in values:
                flattened.extend(value if isinstance(value, list) else [value])
            result[key] = flattened
        elif len(values) == 1:
            result[key] = values[0]
        else:
            result[key] = values
    return result


def _growth_status_nodes(content: Any, language: str) -> tuple[ReportNode, ...]:
    if isinstance(content, dict):
        if content.get("opportunities"):
            return ()
        response_error = content.get("_response_error")
        validation = content.get("_validation")
        if response_error in {"empty_content", "invalid_json", "invalid_shape"}:
            text = (
                "The growth-opportunity model returned no usable content. You can retry only this stage."
                if language == EN else
                "增長機會模型未返回有效內容，可單獨重試該階段。"
                if language == ZH_HANT else
                "增长机会模型未返回有效内容，可单独重试该阶段。"
            )
            return (ReportNode("paragraph", text),)
        if isinstance(validation, dict) and validation.get("passed") is False:
            text = (
                "The growth-opportunity output did not pass structure or evidence validation. You can retry only this stage."
                if language == EN else
                "增長機會輸出未通過結構或證據校驗，可單獨重試該階段。"
                if language == ZH_HANT else
                "增长机会输出未通过结构或证据校验，可单独重试该阶段。"
            )
            return (ReportNode("paragraph", text),)
    text = (
        "Current evidence is insufficient to present a growth opportunity."
        if language == EN else
        "目前證據不足，未形成可展示的增長機會。"
        if language == ZH_HANT else
        "当前证据不足，未形成可展示的增长机会。"
    )
    return (ReportNode("paragraph", text),)


def assemble_report_document(
    run_id: str,
    artifacts: Sequence[Any],
    continuity: Any = None,
    language: str = "zh-CN",
    *,
    company_name: str = "",
    include_technical: bool = False,
    run_status: str = "",
) -> ReportDocument:
    """Adapt persisted artifacts into the immutable v1 document contract."""
    from .growth import (
        evidence_grade_label,
        format_evidence_summary,
        format_probability_range,
        growth_field_label,
        normalize_growth_output,
        scenario_label,
    )
    from .report_projection import (
        normalize_report_sections,
        project_report_diagnostics,
    )
    # Keep the public projection seam shared with the legacy entry module so
    # adapters remain independently testable and malformed sections isolate.
    from .reporting import project_report_value
    from .report_readiness import readiness_for_report_artifact
    from .report_revisions import resolve_report_artifact

    adapted_artifacts = _adapt_nested_expectation_narrative(
        [item for item in artifacts if isinstance(item, dict)]
    )
    safe_artifacts = tuple(adapted_artifacts)
    locale = normalize_language(language)
    from .report_projection import resolve_report_identity
    identity = resolve_report_identity(list(safe_artifacts), explicit_name=company_name)
    company_name = identity["name"]
    stage_fallback_error = False
    try:
        stage_fallbacks = _stage_section_fallbacks(safe_artifacts)
    except Exception:
        stage_fallbacks = {}
        stage_fallback_error = True
    final = resolve_report_artifact(list(safe_artifacts))
    raw_report: Any = {}
    if isinstance(final, dict):
        content = final.get("content")
        if isinstance(content, dict):
            raw_report = content.get("report", content)
    latest_artifacts = {
        str(item.get("artifact_type") or ""): item
        for item in safe_artifacts
        if item.get("artifact_type")
    }

    def artifact_ids_for_section(section_id: str) -> tuple[str, ...]:
        artifact_types = {
            "financial-overview": ("deterministic-financial-summary",),
            "deterministic-balance-sheet": ("deterministic-financial-summary",),
            "financial-data-quality": ("deterministic-financial-summary",),
            "deterministic-valuation": ("deterministic-valuation",),
            "growth_opportunities": ("growth-opportunities",),
            "counterarguments": ("counter-analysis",),
            "opposing-views": ("counter-analysis",),
            "scenarios": ("forecast-scenarios",),
            "claims": ("research-report", "verified-research-dossier"),
        }.get(section_id, ("research-report", "verified-research-dossier"))
        ids = []
        for artifact_type in artifact_types:
            artifact = latest_artifacts.get(artifact_type)
            artifact_id = str(artifact.get("artifact_id") or "") if isinstance(artifact, dict) else ""
            if artifact_id and artifact_id not in ids:
                ids.append(artifact_id)
        return tuple(ids)

    def verification_for_section(section_id: str) -> str:
        artifact_types = {
            "financial-overview": ("deterministic-financial-summary",),
            "deterministic-balance-sheet": ("deterministic-financial-summary",),
            "financial-data-quality": ("deterministic-financial-summary",),
            "deterministic-valuation": ("deterministic-valuation",),
            "growth_opportunities": ("growth-opportunities",),
            "counterarguments": ("counter-analysis",),
            "opposing-views": ("counter-analysis",),
            "scenarios": ("forecast-scenarios",),
            "claims": ("research-report", "verified-research-dossier"),
        }.get(section_id, ("research-report", "verified-research-dossier"))
        for artifact_type in artifact_types:
            artifact = latest_artifacts.get(artifact_type)
            content = artifact.get("content") if isinstance(artifact, dict) else None
            if not isinstance(content, dict):
                continue
            if artifact_type == "research-report":
                verification = content.get("verification")
                if isinstance(verification, dict):
                    return "verified" if verification.get("passed") is True else "partial"
            elif artifact_type == "verified-research-dossier":
                analyses = content.get("verified_analyses")
                if isinstance(analyses, dict) and analyses:
                    return "partial"
            elif artifact_type == "deterministic-financial-summary":
                verification = content.get("verification")
                if isinstance(verification, dict):
                    return "verified" if verification.get("passed") is True else "partial"
                if str(content.get("verification_state") or "").upper() == "VERIFIED":
                    return "verified"
            else:
                validation = content.get("_validation")
                audit = content.get("_audit")
                audit_verification = audit.get("verification") if isinstance(audit, dict) else None
                if (
                    isinstance(validation, dict) and validation.get("passed") is True
                    or isinstance(audit_verification, dict) and audit_verification.get("passed") is True
                    or artifact_type == "deterministic-valuation" and content.get("status") == "ok"
                ):
                    return "verified"
                return "partial"
        return "needs_review"
    diagnostics: list[DeliveryDiagnostic] = []
    if stage_fallback_error:
        diagnostics.append(DeliveryDiagnostic("section_projection_failed", "presentation", "stage-fallbacks"))
    for item in safe_artifacts:
        if item.get("artifact_type") != "verified-research-dossier":
            continue
        content = item.get("content")
        if isinstance(content, dict) and isinstance(content.get("analyses"), dict) and not isinstance(content.get("verified_analyses"), dict):
            diagnostics.append(DeliveryDiagnostic(
                "legacy_dossier_unverified", "local_unverified", "verified-research-dossier.analyses",
                action="Legacy analyses without a verification projection were withheld; re-verify before relying on them.",
            ))
    if isinstance(raw_report, dict):
        known_nested_narratives = (".revenue_path", ".evidence_basis")
        diagnostics.extend(
            DeliveryDiagnostic("unknown_protocol_field", "presentation", path)
            for path in project_report_diagnostics(raw_report, include_technical=include_technical)
            if not (path.startswith("implied_expectations.scenarios.") and path.endswith(known_nested_narratives))
        )
    try:
        display_report = normalize_report_sections(raw_report, locale)
    except Exception:
        display_report = {}
        diagnostics.append(DeliveryDiagnostic("section_projection_failed", "presentation", "research-report"))
    financial = next((item for item in reversed(safe_artifacts) if item.get("artifact_type") == "deterministic-financial-summary"), None)
    financial_content = financial.get("content", {}) if isinstance(financial, dict) and isinstance(financial.get("content"), dict) else {}
    evidence = financial_content.get("evidence", [])
    available_evidence = {
        str(item.get("evidence_id")) for item in evidence
        if isinstance(item, dict) and item.get("evidence_id")
    } if isinstance(evidence, list) else set()
    claims: list[ClaimView] = []
    raw_claims = raw_report.get("claims", []) if isinstance(raw_report, dict) else []
    if raw_claims in (None, "", [], {}):
        raw_claims = display_report.get("claims") or stage_fallbacks.get("claims", [])
    source_claims = raw_claims
    safe_claims = project_report_value(
        raw_claims,
        include_technical=include_technical,
        section="claims",
        available_evidence=available_evidence,
    )
    if isinstance(source_claims, list) and isinstance(safe_claims, list):
        # Presentation projection intentionally strips identifiers and
        # identity fields. Restore those only into the typed audit object;
        # renderers consume the separately sanitized prose and never display
        # these metadata fields as claim text.
        raw_claims = []
        for index, original in enumerate(source_claims):
            if not isinstance(original, dict):
                raw_claims.append(original)
                continue
            projected = safe_claims[index] if index < len(safe_claims) and isinstance(safe_claims[index], dict) else {}
            restored = dict(projected)
            for field in (
                "claim_id", "company", "concept", "period", "scope", "currency",
                "unit", "evidence_ids", "assumption_id", "value", "state",
            ):
                if field in original:
                    restored[field] = original[field]
            raw_claims.append(restored)
    else:
        raw_claims = safe_claims
    if isinstance(raw_claims, list):
        seen: set[tuple[Any, ...]] = set()
        for index, raw in enumerate(raw_claims):
            if not isinstance(raw, dict):
                diagnostics.append(DeliveryDiagnostic("claim_not_object", "local_unverified", f"claims[{index}]"))
                continue
            text = raw.get("text")
            valid = isinstance(text, str)
            claim_identity = ClaimIdentity(
                company=str(raw.get("company", "") or ""), concept=str(raw.get("concept", "") or ""),
                period=str(raw.get("period", "") or ""), scope=str(raw.get("scope", "") or ""),
                currency=str(raw.get("currency", "") or ""), unit=str(raw.get("unit", "") or ""),
            )
            evidence_ids = tuple(dict.fromkeys(str(item) for item in raw.get("evidence_ids", []) if isinstance(item, (str, int)))) if isinstance(raw.get("evidence_ids", []), list) else ()
            claim_id = str(raw.get("claim_id", f"claim-{index + 1}"))
            state = str(raw.get("state", "unverified")) if valid else "rejected"
            value = _value(raw.get("value"))
            key = (claim_identity, value, evidence_ids, text if valid else "")
            if key in seen:
                continue
            seen.add(key)
            claims.append(ClaimView(claim_id, text if valid else "", str(raw.get("kind", "claim")), state, claim_identity, value, evidence_ids, str(raw.get("assumption_id", "") or "")))
            if not valid:
                diagnostics.append(DeliveryDiagnostic("claim_text_not_string", "presentation", f"claims[{index}].text", (claim_id,), "This claim was isolated; repair its text field before relying on it."))

    # Only the current deterministic artifact defines the displayed source set,
    # matching the report's financial provenance rather than stale overlays.
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(evidence, list):
        for raw in evidence:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("source_url", raw.get("url", "")) or "").strip()
            if not url:
                continue
            document_id = str(raw.get("document_id", "") or "")
            title = str(raw.get("title", raw.get("concept", "Source")) or "Source")
            key = (document_id or url, url)
            group = grouped.setdefault(key, {"title": title, "document_id": document_id or url, "locators": [], "evidence_ids": [], "evidence_locations": []})
            locator = str(raw.get("locator", "") or "").strip()
            evidence_id = str(raw.get("evidence_id", "") or "").strip()
            if locator and locator not in group["locators"]:
                group["locators"].append(locator)
            if evidence_id and evidence_id not in group["evidence_ids"]:
                group["evidence_ids"].append(evidence_id)
            if (evidence_id, locator) not in group["evidence_locations"]:
                group["evidence_locations"].append((evidence_id, locator))
    sources = tuple(SourceRef(
        f"source-{index}", value["document_id"], value["title"], key[1], tuple(value["locators"]),
        tuple(value["evidence_ids"]), tuple(value["evidence_locations"]),
        not bool(value["locators"]),
    ) for index, (key, value) in enumerate(grouped.items(), 1))

    try:
        readiness_value = readiness_for_report_artifact(
            final, safe_artifacts, run_status=str(run_status)
        ) if final else None
    except Exception:
        readiness_value = None
        diagnostics.append(DeliveryDiagnostic("section_projection_failed", "presentation", "report-readiness"))
    readiness = ReadinessView(
        state=str(readiness_value.get("state", "unknown")),
        complete=readiness_value.get("complete") is True,
        substantive_sections=tuple(str(item) for item in readiness_value.get("substantive_sections", []) if isinstance(item, str)) if isinstance(readiness_value.get("substantive_sections", []), (list, tuple)) else (),
        missing_sections=tuple(str(item) for item in readiness_value.get("missing_sections", []) if isinstance(item, str)) if isinstance(readiness_value.get("missing_sections", []), (list, tuple)) else (),
        missing_stages=tuple(str(item) for item in readiness_value.get("missing_stages", []) if isinstance(item, str)) if isinstance(readiness_value.get("missing_stages", []), (list, tuple)) else (),
        issues=tuple(str(item) for item in readiness_value.get("issues", []) if isinstance(item, str)) if isinstance(readiness_value.get("issues", []), (list, tuple)) else (),
        recovery_action=str(readiness_value.get("recovery_action", "")),
    ) if isinstance(readiness_value, dict) else ReadinessView(
        state="action_required",
        issues=("research_report_artifact_missing",),
        recovery_action="resume_or_repair_missing_research_stages",
    )

    # All report sections are assembled from the versioned artifact contract.
    # Format renderers consume these immutable nodes; Markdown is no longer
    # rendered and parsed back to recover section meaning or evidence links.
    blocks: list[SectionBlock] = []
    ordinal = 0

    def add_section(
        section_id: str,
        title: str,
        nodes: Sequence[ReportNode],
        *,
        section_claim_ids: tuple[str, ...] = (),
        assumption_ids: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
        diagnostic_code: str = "",
        is_substantive: bool | None = None,
    ) -> None:
        nonlocal ordinal
        ordinal += 1
        blocks.append(_section(
            ordinal, section_id, title,
            (ReportNode("heading", title, level=2), *nodes),
            claim_ids=section_claim_ids,
            assumption_ids=assumption_ids,
            evidence_ids=evidence_ids,
            source_artifact_ids=artifact_ids_for_section(section_id),
            verification_state=verification_for_section(section_id),
            diagnostic_code=diagnostic_code,
            is_substantive=is_substantive,
        ))

    complete_title = final is not None and readiness.complete
    brand_title = (
        ("OpenThesis Long-term Company Research" if complete_title else "OpenThesis Staged Research") if locale == EN
        else ("OpenThesis 長期公司研究" if complete_title else "OpenThesis 階段性研究") if locale == ZH_HANT
        else ("OpenThesis 长期公司研究" if complete_title else "OpenThesis 阶段性研究")
    )
    disclaimer = (
        "Research assistance only; not investment advice or a trade instruction."
        if locale == EN
        else "本報告用於研究輔助，不構成投資建議或交易指令。"
        if locale == ZH_HANT
        else "本报告用于研究辅助，不构成投资建议或交易指令。"
    )
    identity_nodes = [
        ReportNode("paragraph", f"{company_name} · {identity['ticker']}" if identity.get("ticker") and identity["ticker"] not in company_name else company_name)
        if company_name else ReportNode("paragraph", str(run_id)),
        ReportNode("paragraph", disclaimer),
    ]
    if identity.get("market"):
        identity_nodes.append(ReportNode("paragraph", identity["market"]))
    add_section(
        "report-identity",
        brand_title,
        identity_nodes,
    )

    if not safe_artifacts:
        message = (
            "No usable research report is available. Start a company research run to build one."
            if locale == EN else
            "研究尚未形成可用报告。请先发起公司研究。"
            if locale != ZH_HANT else
            "尚未形成研究報告。請先發起公司研究。"
        )
        add_section("research-status", "Research status" if locale == EN else "研究狀態" if locale == ZH_HANT else "研究状态", (ReportNode("paragraph", message),))
    elif final is None or not readiness.complete:
        message = (
            "No final integrated report has been generated. Completed research stages are preserved below."
            if locale == EN else
            "尚未生成最终综合报告，以下保留已完成的研究阶段。"
            if locale == ZH_HANT else
            "尚未生成最终综合报告，以下保留已完成的研究阶段。"
        )
        add_section("research-status", "Staged research" if locale == EN else "階段性研究" if locale == ZH_HANT else "阶段性研究", (ReportNode("quote", message),))

    if isinstance(final, dict):
        final_content = final.get("content") if isinstance(final.get("content"), dict) else {}
        mode = str(final_content.get("mode") or "")
        if mode == "financial-refresh":
            add_section(
                "financial-refresh-status",
                "Financial Refresh Status" if locale == EN else "財務刷新狀態" if locale == ZH_HANT else "财务刷新状态",
                (ReportNode("quote", "Financial tables and deterministic metrics were refreshed without a model call. Qualitative research below is retained from the prior synthesis; regenerate synthesis if material figures changed." if locale == EN else "財務表格與確定性指標已在不呼叫模型的情況下更新；下方定性研究保留自原綜合結果，若關鍵數字發生變化，請重新生成綜合報告。" if locale == ZH_HANT else "财务表格与确定性指标已在不调用模型的情况下更新；下方定性研究保留自原综合结果，若关键数字发生变化，请重新生成综合报告。"),),
            )
        if mode == "staged-fallback":
            add_section(
                "staged-report-status",
                "Staged Research Report" if locale == EN else "階段性研究報告" if locale == ZH_HANT else "阶段性研究报告",
                (ReportNode("quote", "Final synthesis was incomplete. The content below preserves completed research stages and can be synthesized again." if locale == EN else "最終綜合未完整生成，以下內容保留已完成的研究階段，可重新嘗試綜合。" if locale == ZH_HANT else "最终综合未完整生成，以下内容保留已完成的研究阶段，可重新尝试综合。"),),
            )
            from .reporting import staged_context_capacity_notice
            notice = staged_context_capacity_notice(final_content.get("report"), locale)
            if notice:
                add_section(
                    "context-capacity-status", notice["title"],
                    tuple(ReportNode("paragraph", notice[key]) for key in ("summary", "budget", "counting", "retry")),
                )
        if mode == "deterministic-only":
            add_section(
                "ai-research-status",
                "AI Research Status" if locale == EN else "AI 研究狀態" if locale == ZH_HANT else "AI 研究状态",
                (ReportNode("quote", "No model was configured, so qualitative research, growth opportunities, and long-term scenarios were not generated." if locale == EN else "未配置模型，因此沒有生成定性研究、增長機會和長期情境。" if locale == ZH_HANT else "未配置模型，因此没有生成定性研究、增长机会和长期情景。"),),
            )

    if isinstance(financial, dict):
        content = financial.get("content") if isinstance(financial.get("content"), dict) else {}
        financial_title = (
            f"{company_name} Financial Overview" if locale == EN and company_name
            else f"{company_name} 財務概覽" if locale == ZH_HANT and company_name
            else f"{company_name} 财务概览" if company_name
            else "Deterministic Financial Overview" if locale == EN
            else "確定性財務概覽" if locale == ZH_HANT
            else "确定性财务概览"
        )
        if not content.get("currency"):
            # Historical deterministic summaries use USD when they omit the
            # currency field; preserve that established input contract.
            content = {**content, "currency": "USD"}
        try:
            metric_nodes, balance_nodes = _financial_nodes(content, locale)
        except Exception:
            metric_nodes, balance_nodes = (), ()
            diagnostics.append(DeliveryDiagnostic("section_projection_failed", "presentation", "financial-overview"))
        if metric_nodes:
            all_financial_ids = tuple(dict.fromkeys(
                str(item.get("evidence_id")) for item in evidence
                if isinstance(item, dict) and item.get("evidence_id")
            )) if isinstance(evidence, list) else ()
            add_section("financial-overview", financial_title, metric_nodes, evidence_ids=all_financial_ids)
        if balance_nodes:
            balance_ids = tuple(dict.fromkeys(
                evidence_id
                for node in balance_nodes
                for fact in node.money_facts
                for evidence_id in fact.evidence_ids
            ))
            add_section(
                "deterministic-balance-sheet",
                "Balance sheet data" if locale == EN else "資產負債表數據" if locale == ZH_HANT else "资产负债表数据",
                balance_nodes,
                evidence_ids=balance_ids,
            )
        financial_quality = content.get("financial_quality")
        if isinstance(financial_quality, dict):
            projected_quality = project_report_value(
                financial_quality, include_technical=include_technical,
                available_evidence=set(all_financial_ids if metric_nodes else ()),
            )
            quality_nodes = _semantic_nodes(projected_quality, locale)
            rejected = financial_quality.get("rejected_periods", [])
            continuity = financial_quality.get("period_continuity", [])
            if rejected or (
                isinstance(continuity, list)
                and any(isinstance(item, dict) and item.get("status") != "accepted" for item in continuity)
            ):
                quality_nodes.insert(0, ReportNode(
                    "paragraph",
                    "Some annual data failed validation and was quarantined; it was not used for metrics or model analysis." if locale == EN else
                    "部分年度資料未通過校驗，已隔離且未用於計算或模型分析。" if locale == ZH_HANT else
                    "部分年度数据校验未通过，已隔离且未用于计算或模型分析。",
                ))
            coverage = financial_quality.get("period_coverage")
            if isinstance(coverage, dict):
                actual_years = coverage.get("displayed_annual_years", [])
                missing_years = coverage.get("missing_or_rejected_years", [])
                hidden_years = coverage.get("hidden_comparator_years", [])
                coverage_lines = []
                if actual_years:
                    coverage_lines.append(("Annual years actually used: " if locale == EN else "實際採用年度：" if locale == ZH_HANT else "实际采用年度：") + ", ".join(map(str, actual_years)))
                if hidden_years:
                    coverage_lines.append(("Hidden comparator years: " if locale == EN else "隱藏比較年度：" if locale == ZH_HANT else "隐藏比较年度：") + ", ".join(map(str, hidden_years)))
                if coverage.get("latest_official_fy") is not None:
                    coverage_lines.append(("Latest valid official fiscal year: " if locale == EN else "最新有效官方財年：" if locale == ZH_HANT else "最新有效官方财年：") + str(coverage["latest_official_fy"]))
                if coverage.get("interim_periods"):
                    coverage_lines.append(("Interim periods (not a substitute for FY): " if locale == EN else "中期期間（不替代完整財年）：" if locale == ZH_HANT else "中期期间（不替代完整财年）：") + ", ".join(map(str, coverage["interim_periods"])))
                if missing_years:
                    missing_label = "No valid official annual report was available" if locale == EN else "截至研究日未取得有效官方年報" if locale == ZH_HANT else "截至研究日未获取到有效官方年报"
                    years = ", ".join(str(item.get("year")) for item in missing_years if isinstance(item, dict) and item.get("year") is not None)
                    coverage_lines.append(missing_label + (f" (years: {years})" if years and locale == EN else f"（年度：{years}）" if years else "." if locale == EN else "。"))
                if coverage_lines:
                    quality_nodes.extend((
                        ReportNode("heading", "Annual disclosure coverage" if locale == EN else "年度披露範圍" if locale == ZH_HANT else "年度披露范围", level=3),
                        *(ReportNode("list_item", line) for line in coverage_lines),
                    ))
            if quality_nodes:
                add_section(
                    "financial-data-quality",
                    "Annual Data Continuity" if locale == EN and (rejected or continuity) else "Financial data quality" if locale == EN else "年度數據連續性" if locale == ZH_HANT and (rejected or continuity) else "財務資料品質" if locale == ZH_HANT else "年度数据连续性" if rejected or continuity else "财务数据质量",
                    quality_nodes,
                    evidence_ids=all_financial_ids,
                )

    valuation = next((item for item in reversed(safe_artifacts) if item.get("artifact_type") == "deterministic-valuation"), None)
    if isinstance(valuation, dict) and isinstance(valuation.get("content"), dict):
        value = valuation["content"]
        valuation_nodes: list[ReportNode] = []
        if value.get("status") == "ok":
            currency = str(value.get("currency", "USD"))
            snapshot = value.get("market_snapshot") if isinstance(value.get("market_snapshot"), dict) else {}
            period = str(value.get("base_fcf_period") or "—")
            horizon = int(value.get("horizon_years", 5))
            items = (
                ("Equity market value", format_money(value.get("equity_market_value", value.get("market_cap")), currency)),
                (f"FCFE proxy ({period})", format_money(value.get("base_free_cash_flow"), currency)),
                (f"{horizon}-year implied FCFE growth", format_percent(value.get("implied_fcf_growth"))),
                ("Discount rate", format_percent(value.get("discount_rate"))),
                ("Terminal growth", format_percent(value.get("terminal_growth"))),
                ("Market snapshot", f"{snapshot.get('source') or value.get('source') or '—'} · {snapshot.get('as_of') or value.get('market_as_of') or '—'}"),
            ) if locale == EN else (
                ("权益市值", format_money(value.get("equity_market_value", value.get("market_cap")), currency)),
                (f"FCFE 近似（{period}）", format_money(value.get("base_free_cash_flow"), currency)),
                (f"前 {horizon} 年隐含 FCFE 增速", format_percent(value.get("implied_fcf_growth"))),
                ("折现率", format_percent(value.get("discount_rate"))),
                ("永续增长率", format_percent(value.get("terminal_growth"))),
                ("行情快照", f"{snapshot.get('source') or value.get('source') or '—'}；截至 {snapshot.get('as_of') or value.get('market_as_of') or '—'}"),
            )
            valuation_nodes.extend(ReportNode("list_item", f"**{label}:** {rendered}" if locale == EN else f"**{label}：** {rendered}") for label, rendered in items)
        else:
            valuation_nodes.append(ReportNode(
                "paragraph",
                ("Implied growth could not be calculated: " if locale == EN else "無法給出隱含增速：" if locale == ZH_HANT else "无法给出隐含增速：") + reverse_dcf_status_text(value.get("status"), locale),
            ))
        valuation_nodes.extend(ReportNode("list_item", ("Limitation: " if locale == EN else "限制：") + reverse_dcf_disclaimer(locale) if locale == EN else reverse_dcf_disclaimer(locale)) for _ in range(1))
        add_section("deterministic-valuation", "Reverse DCF Implied Expectations" if locale == EN else "反向 DCF 隱含預期" if locale == ZH_HANT else "反向 DCF 隐含预期", valuation_nodes)

    # Render growth from the registry-backed normalizer, not model-provided
    # evidence counts. Identifier strings stay in the document metadata and
    # optional technical view; ordinary prose gets only verified counts.
    def growth_nodes(value: Any) -> tuple[ReportNode, ...]:
        normalized = normalize_growth_output(value, available_evidence, locale).output
        opportunities = normalized.get("opportunities", [])
        nodes: list[ReportNode] = []
        for opportunity in opportunities if isinstance(opportunities, list) else ():
            if not isinstance(opportunity, dict):
                continue
            title = str(opportunity.get("title") or "")
            nodes.append(ReportNode("heading", title or ("Growth opportunity" if locale == EN else "增长机会"), level=3))
            nodes.append(ReportNode("list_item", format_evidence_summary(
                int(opportunity.get("supporting_evidence_count", 0) or 0),
                int(opportunity.get("contradicting_evidence_count", 0) or 0), locale,
            )))
            fields = (
                ("category", opportunity.get("category")),
                ("mechanism", opportunity.get("mechanism")),
                ("evidence_grade", evidence_grade_label(opportunity.get("evidence_grade"), locale)),
                ("maturity_stage", opportunity.get("maturity_stage")),
                ("time_horizon_years", opportunity.get("time_horizon_years")),
                ("probability_range", format_probability_range(opportunity.get("probability_range"), locale)),
                ("capital_requirements", opportunity.get("capital_requirements")),
                ("leading_indicators", opportunity.get("leading_indicators")),
                ("invalidation_conditions", opportunity.get("invalidation_conditions")),
                ("scenario_eligibility", opportunity.get("scenario_eligibility")),
            )
            for key, item in fields:
                if item in (None, "", [], {}):
                    continue
                label = growth_field_label(key, locale)
                if key == "scenario_eligibility" and isinstance(item, list):
                    rendered = ", ".join(scenario_label(part, locale) for part in item)
                elif isinstance(item, list):
                    rendered = ", ".join(str(part) for part in item)
                else:
                    rendered = str(item)
                nodes.append(ReportNode("list_item", f"**{label}:** {rendered}" if locale == EN else f"**{label}：** {rendered}"))
        return tuple(nodes)

    for key in (_REPORT_SECTION_KEYS if safe_artifacts else ()):
        raw_value = raw_report.get(key) if isinstance(raw_report, dict) else None
        if raw_value in (None, "", [], {}) and key in stage_fallbacks:
            raw_value = stage_fallbacks[key]
        else:
            raw_value = display_report.get(key, raw_value)
        try:
            projected = project_report_value(
                raw_value,
                include_technical=include_technical,
                section=key,
                available_evidence=available_evidence,
            )
            claim_ids: tuple[str, ...] = ()
            assumptions: tuple[str, ...] = ()
            if key == "claims":
                nodes = _claim_nodes(tuple(claims), projected, locale)
                claim_ids = tuple(item.claim_id for item in claims)
                assumptions = tuple(item.assumption_id for item in claims if item.assumption_id)
            elif key == "growth_opportunities":
                nodes = growth_nodes(raw_value)
            else:
                nodes = _typed_section_nodes(key, projected, locale)
            if key == "growth_opportunities" and not nodes:
                nodes = _growth_status_nodes(raw_value, locale)
            if not nodes:
                continue
            section_evidence = tuple(dict.fromkeys(
                evidence_id for claim in claims for evidence_id in claim.evidence_ids
            )) if key in {"claims", "growth_opportunities", "counterarguments"} else ()
            add_section(
                "opposing-views" if key == "counterarguments" else key,
                _localized_section_title(key, locale),
                nodes,
                section_claim_ids=claim_ids,
                assumption_ids=assumptions,
                evidence_ids=section_evidence,
            )
        except Exception:
            diagnostics.append(DeliveryDiagnostic("section_projection_failed", "presentation", key))
            error_text = "This section could not be displayed; the other report sections remain available." if locale == EN else "本章節目前無法顯示，其他報告章節仍可查看。" if locale == ZH_HANT else "本章节暂时无法显示，其他报告章节仍可查看。"
            add_section(
                "opposing-views" if key == "counterarguments" else key,
                _localized_section_title(key, locale),
                (ReportNode("paragraph", error_text),),
                diagnostic_code="section_projection_failed",
                is_substantive=False,
            )

    if isinstance(raw_report, dict):
        verification = final.get("content", {}).get("verification") if isinstance(final, dict) and isinstance(final.get("content"), dict) else None
        if isinstance(verification, dict):
            verified = verification.get("passed") is True
            yes = "Yes" if locale == EN and verified else "No" if locale == EN else "是" if verified else "否"
            if locale == EN:
                title = "Verification Results"
                nodes = [ReportNode("list_item", f"Passed: {yes}")]
                nodes.extend(ReportNode("list_item", f"{label}: {verification.get(field, 0)}") for field, label in (("claim_count", "Claims"), ("verified_claim_count", "Verified claims"), ("unsupported_fact_count", "Unsupported facts")))
            else:
                title = "驗證結果" if locale == ZH_HANT else "验证结果"
                nodes = [ReportNode("list_item", f"是否通過：{yes}" if locale == ZH_HANT else f"是否通过：{yes}")]
                nodes.extend(ReportNode("list_item", f"{label}：{verification.get(field, 0)}") for field, label in ((("claim_count", "結論數量"), ("verified_claim_count", "已驗證結論"), ("unsupported_fact_count", "無證據事實")) if locale == ZH_HANT else (("claim_count", "结论数量"), ("verified_claim_count", "已验证结论"), ("unsupported_fact_count", "无证据事实"))))
            if include_technical and isinstance(verification.get("issues"), list):
                nodes.extend(ReportNode("list_item", str(issue)) for issue in verification["issues"])
            add_section("verification-results", title, nodes)

    if sources:
        title = "Evidence Sources" if locale == EN else "證據來源" if locale == ZH_HANT else "证据来源"
        source_nodes: list[ReportNode] = []
        for source in sources:
            locators = "; ".join(source.locators)
            label = source.title + (f" ({locators})" if locators and locale == EN else f"（{locators}）" if locators else "")
            if source.url.startswith(("https://", "http://")):
                label = f"[{label}]({source.url})"
            source_nodes.append(ReportNode("list_item", label))
            if not source.url.startswith(("https://", "http://")):
                source_nodes.append(ReportNode("paragraph", source.url))
        add_section("evidence-sources", title, source_nodes, evidence_ids=tuple(dict.fromkeys(item for source in sources for item in source.evidence_ids)))

    if safe_artifacts:
        from .reporting import _artifact_title
        process_title = "Research Process" if locale == EN else "研究過程" if locale == ZH_HANT else "研究过程"
        process_nodes = [
            ReportNode(
                "list_item",
                f"{_artifact_label(item, locale)} · `{item.get('agent_id', '')}` · `{item.get('model_id', '')}`" if include_technical
                else f"{_artifact_title(item, locale)} · {'Preserved' if locale == EN else '已保留'}",
            )
            for item in safe_artifacts
        ]
        add_section("research-process", process_title, process_nodes)
        method_title = "Methodology" if locale == EN else "方法說明" if locale == ZH_HANT else "方法说明"
        method = (
            "Financial values come from structured facts and deterministic calculations. Model-generated content distinguishes evidence, assumptions, and information gaps; the user makes the final investment judgment."
            if locale == EN else
            "財務數值來自結構化事實並由確定性程式計算。模型內容須區分證據、假設與未知事項；最終投資判斷由使用者自行作出。"
            if locale == ZH_HANT else
            "财务数值来自结构化事实并由确定性程序计算。模型生成内容应区分证据、假设和未知项；最终投资判断由用户自行作出。"
        )
        add_section("methodology", method_title, (ReportNode("paragraph", method),))
        if include_technical:
            details = [
                ReportNode("list_item", f"{_artifact_label(item, locale)} · `{item.get('agent_id', '')}` · `{item.get('model_id', '')}`")
                for item in safe_artifacts
            ]
            for item in safe_artifacts:
                if item.get("artifact_type") != "growth-opportunities":
                    continue
                content = item.get("content")
                opportunities = content.get("opportunities", []) if isinstance(content, dict) else []
                if not isinstance(opportunities, list):
                    continue
                for index, opportunity in enumerate(opportunities, 1):
                    if not isinstance(opportunity, dict):
                        continue
                    for field, label in (
                        ("supporting_evidence_ids", "supporting evidence IDs"),
                        ("contradicting_evidence_ids", "contradicting evidence IDs"),
                    ):
                        identifiers = opportunity.get(field)
                        if isinstance(identifiers, list) and identifiers:
                            details.append(ReportNode(
                                "list_item",
                                f"Growth opportunity {index} {label}: "
                                + ", ".join(str(value) for value in identifiers),
                            ))
            add_section("technical-details", "Technical Details" if locale == EN else "技術詳情" if locale == ZH_HANT else "技术详情", details)

    readiness = ReadinessView(
        state=str(readiness_value.get("state", "unknown")),
        complete=readiness_value.get("complete") is True,
        substantive_sections=tuple(str(item) for item in readiness_value.get("substantive_sections", []) if isinstance(item, str)) if isinstance(readiness_value.get("substantive_sections", []), (list, tuple)) else (),
        missing_sections=tuple(str(item) for item in readiness_value.get("missing_sections", []) if isinstance(item, str)) if isinstance(readiness_value.get("missing_sections", []), (list, tuple)) else (),
        missing_stages=tuple(str(item) for item in readiness_value.get("missing_stages", []) if isinstance(item, str)) if isinstance(readiness_value.get("missing_stages", []), (list, tuple)) else (),
        issues=tuple(str(item) for item in readiness_value.get("issues", []) if isinstance(item, str)) if isinstance(readiness_value.get("issues", []), (list, tuple)) else (),
        recovery_action=str(readiness_value.get("recovery_action", "")),
    ) if isinstance(readiness_value, dict) else ReadinessView(
        state="action_required",
        issues=("research_report_artifact_missing",),
        recovery_action="resume_or_repair_missing_research_stages",
    )
    continuity_state = str(continuity.get("state", "unknown")) if isinstance(continuity, dict) else "unknown"
    from .report_projection import resolve_report_identity

    identity = resolve_report_identity(list(safe_artifacts), explicit_name=company_name)["name"]
    return ReportDocument(
        schema_version="1",
        run_id=str(run_id),
        company_name=identity,
        language=locale,
        sections=blocks,
        claims=tuple(claims),
        sources=sources,
        readiness=readiness,
        delivery_diagnostics=tuple(diagnostics),
        run_digest=RunDigest(str(run_id), len(safe_artifacts), final is not None, continuity_state, str(run_status)),
        include_technical=include_technical,
    )


def render_markdown(document: ReportDocument) -> str:
    """Lay out semantic section blocks as Markdown without revisiting artifacts."""
    return "\n\n".join(
        section.body.rstrip()
        for section in document.sections
        if section.body.strip()
    ).rstrip() + "\n"


def _inline(text: str) -> str:
    escaped = escape(text, quote=True)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def _render_blocks(nodes: tuple[ReportNode, ...]) -> str:
    html: list[str] = []
    items: list[str] = []
    index = 0
    while index < len(nodes):
        node = nodes[index]
        if node.kind == "confidence_heading":
            if items:
                html.append("<ul class=\"list\">" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
                items = []
            end = index + 1
            while end < len(nodes) and nodes[end].kind != "confidence_heading":
                end += 1
            contents = _render_blocks(nodes[index + 1:end])
            html.append(
                f'<div class="confidence-group confidence-{node.confidence_tier}">'
                f'<div class="confidence-heading">{_inline(node.text)}</div>{contents}</div>'
            )
            index = end
            continue
        if node.kind != "list_item" and items:
            html.append("<ul class=\"list\">" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
            items = []
        if node.kind == "heading":
            level = min(6, max(2, node.level))
            html.append(f"<h{level}>{_inline(node.text)}</h{level}>")
        elif node.kind == "paragraph":
            html.append(f"<p>{_inline(node.text)}</p>")
        elif node.kind == "quote":
            html.append(f"<blockquote>{_inline(node.text)}</blockquote>")
        elif node.kind == "list_item":
            items.append(_inline(node.text))
        elif node.kind == "table":
            if node.rows:
                header, *body = node.rows
                html.append('<table class="data-table"><thead><tr>' + "".join(f"<th>{_inline(cell)}</th>" for cell in header) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>" for row in body) + "</tbody></table>")
        index += 1
    if items:
        html.append("<ul class=\"list\">" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
    return "".join(html)


def render_html(document: ReportDocument) -> str:
    """Lay out the same immutable section nodes as an escaped HTML report."""
    from .report_html import _document

    sections = "".join(
        f'<section id="{escape(section.section_id, quote=True)}">{_render_blocks(section.nodes)}</section>'
        for section in document.sections
    )
    title = document.company_name or ("OpenThesis Research Report" if document.language == EN else "OpenThesis 研究报告")
    status = document.run_digest.run_status.casefold()
    status_allows_complete = not status or status == "completed"
    complete = (
        document.readiness.complete
        and document.run_digest.final_report_available
        and status_allows_complete
    )
    status = (
        "Long-term company research" if complete
        else "Staged research" if document.run_digest.artifact_count
        else "No usable research report"
    ) if document.language == EN else (
        "长期公司研究" if complete
        else "阶段性研究" if document.run_digest.artifact_count
        else "研究尚未形成可用报告"
    )
    disclaimer = (
        "This report is research assistance, not investment advice or a trade instruction."
        if document.language == EN
        else "本报告用于研究辅助，不构成投资建议或交易指令。"
    )
    header = (
        '<header class="hero">'
        f'<div class="eyebrow">{escape(status)}</div>'
        f'<h1>{escape(title)}</h1>'
        f'<div class="meta">{escape("Research run" if document.language == EN else "研究运行")}: '
        f'<code>{escape(document.run_id)}</code></div>'
        f'<div class="notice">{escape(disclaimer)}</div>'
        '</header>'
    )
    return _document(title, header + f"<main>{sections}</main>", document.language)

