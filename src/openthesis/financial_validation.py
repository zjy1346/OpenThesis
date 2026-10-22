"""Pure financial invariants shared by candidate compilation and legacy reads."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Sequence

from .domain import FinancialFact, EvidenceRef
from .financial_contracts import (
    PDF_PARSER_VERSION as _PDF_PARSER_VERSION,
    STATEMENT_FOR as _STATEMENT_FOR,
    FactValidationRecord,
    FinancialGroupValidation,
    FinancialValidation,
    SourceComponentObservation,
    ValidationStatus,
    exact_decimal_arithmetic,
    fact_row_evidence_ids,
    missing_profile_concepts,
)
from .financial_taxonomy import (
    FINANCIAL_LABEL_ALIASES, balance_component_row_concept, equity_row_concept, normalize_financial_label,
    cash_flow_total_label_matches, liability_total_label_matches, comprehensive_income_row_concept,
    profit_loss_value_transform, profit_row_concept,
    interest_income_row_concept,
    CAPITAL_ALLOCATION_PAYMENT_ALIASES, capital_allocation_payment_concept,
    has_distinct_capital_payment_label,
)
from .financial_statement_identity import classify_statement_title, statement_title_evidence_id
from .financial_unit_declarations import literal_money_unit, combined_money_unit, physical_unit_context
from .financial_statutory_currency import bound_money_unit
from .financial_inline_xbrl import inline_source_issue
from .financial_xbrl_semantics import INLINE_PARSER_VERSION
from .financial_structured_roles import companyfacts_source_issue
from .financial_cloud_proof import cloud_table_source_issue
from .financial_gateway_proof import GATEWAY_FACT_VERSION, gateway_source_issue
from .financial_cell_repair import cell_repair_issue
from .financial_sources.amount_disclosures import DISCLOSURE_VERSION, disclosure_source_issue, explicit_money_disclosures
from .financial_sources.note_disclosures import NOTE_DISCLOSURE_VERSIONS, note_source_issue
from .financial_sources.statement_periods import column_period_years

_NUM = re.compile(r"(?:\(\s*[+-]?\d[\d,]*(?:\.\d+)?\s*\)|[+-]?\d[\d,]*(?:\.\d+)?)")
_CORE = {"revenue", "net_income", "assets", "liabilities", "equity", "operating_cash_flow"}
_COVERAGE_WARNING_ISSUES = frozenset({
    "income_statement_core_missing",
    "cash_flow_core_missing",
    "balance_sheet_core_missing",
    "core_coverage_insufficient",
})

def _parse_number(text: str) -> float | None:
    cleaned = text.strip().replace("−", "-").replace("％", "%")
    is_percent = cleaned.endswith("%")
    if is_percent:
        cleaned = cleaned[:-1].strip()
    match = _NUM.fullmatch(cleaned)
    if not match or cleaned in {"-", "–", "—"}:
        return None
    token = match.group().replace(",", "").replace(" ", "")
    negative = token.startswith("(") or token.startswith("-")
    try:
        value = float(token.strip("()-"))
    except ValueError:
        return None
    value = -value if negative else value
    return value / 100.0 if is_percent else value

def _label_compact(text: str) -> str:
    return normalize_financial_label(text)


def _source_row_contains_label(label: str, text: str) -> bool:
    # Positioned rows may print values between two wrapped label fragments.
    # Strip only numeric tokens from the bound, verbatim evidence row before
    # matching its label. The row/page/box binding remains independently checked.
    normalized = _label_compact(label)
    return bool(normalized and normalized in _label_compact(_NUM.sub("", text)))


def _statement_source_issue(
    fact: FinancialFact, row: EvidenceRef | None, evidence: dict[str, EvidenceRef],
) -> str | None:
    """Prove ambiguous allocations from the original table, not metadata.

    ``classification_evidence_ids`` are role-prefixed: statement-title
    evidence is distinct from the following-row equity hierarchy proof.
    """
    if fact.parser_version != _PDF_PARSER_VERSION:
        return None
    title_ids = tuple(key for key in fact.classification_evidence_ids if key.startswith("statement:"))
    label = normalize_financial_label(fact.reported_concept)
    ambiguous_profit = fact.concept in {"parent_net_income", "minority_interest_profit", "ordinary_shareholder_net_income"} and not any(
        marker in label for marker in ("profit", "income", "loss", "利润", "損益", "损益", "溢利", "盈利", "亏损")
    )
    required = fact.statement == "comprehensive_income" or ambiguous_profit
    if not title_ids:
        return "statement_source_concept_mismatch" if required else None
    if len(title_ids) != 1 or row is None:
        return "statement_source_concept_mismatch"
    title = evidence.get(title_ids[0])
    if title is None or title.bbox is None or fact.source_bbox is None or fact.source_page is None:
        return "statement_source_concept_mismatch"
    page = re.fullmatch(r"page:(\d+)", title.locator)
    try:
        geometry = title.bbox + fact.source_bbox
        valid_geometry = len(geometry) == 8 and all(math.isfinite(value) for value in geometry)
        valid_geometry = valid_geometry and title.bbox[0] < title.bbox[2] and title.bbox[1] < title.bbox[3]
    except (TypeError, ValueError, OverflowError):
        valid_geometry = False
    if (
        page is None or not valid_geometry
        or title.document_id != row.document_id or title.source_url != row.source_url
        or not title.content_hash or title.content_hash != row.content_hash
        or not 0 <= fact.source_page - int(page[1]) <= 3
        or (int(page[1]) == fact.source_page and title.bbox[3] > fact.source_bbox[1])
        or statement_title_evidence_id(title.document_id, int(page[1]), title.bbox, title.excerpt) != title.evidence_id
        or classify_statement_title(title.excerpt) != (fact.statement, fact.consolidated_scope)
    ):
        return "statement_source_concept_mismatch"
    return None


def _profit_before_tax_row_issue(
    fact: FinancialFact, ref: EvidenceRef | None
) -> str | None:
    """Require a PDF profit-before-tax value to remain bound to its row/column.

    The parser stores the row text and box together with the selected period
    column.  These checks make that association part of admission, rather
    than trusting a canonical value whose label, numeric cell, or period has
    drifted apart in a cached or transformed candidate.
    """
    reported_label = _label_compact(fact.reported_concept)
    if not reported_label or not _source_row_contains_label(fact.reported_concept, fact.raw_text):
        return "profit_before_tax_row_label_mismatch"
    if (
        ref is None
        or fact.source_page is None
        or fact.source_bbox is None
        or ref.bbox != fact.source_bbox
        or ref.locator != f"page:{fact.source_page}"
        or ref.excerpt != fact.raw_text
    ):
        return "profit_before_tax_evidence_binding_mismatch"
    if fact.source_column:
        years = column_period_years(fact.source_column)
        if years and fact.fiscal_year not in years:
            return "profit_before_tax_period_column_mismatch"
    if not math.isfinite(float(fact.unit_scale)) or fact.unit_scale <= 0:
        return "profit_before_tax_value_scale_invalid"
    displayed_value = float(fact.value) / float(fact.unit_scale)
    row_values = (
        _parse_number(match.group())
        for match in _NUM.finditer(fact.raw_text.replace("−", "-"))
    )
    if fact.normalization_lineage is None and not any(
        value is not None
        and math.isclose(value, displayed_value, rel_tol=1e-10, abs_tol=0.005)
        for value in row_values
    ):
        return "profit_before_tax_value_not_in_evidence_row"
    return None

class FinancialValidator:
    def validate_group(
        self,
        facts: list[FinancialFact],
        identity: tuple[str, str, str, str, str],
        evidence_map: dict[str, EvidenceRef] | None = None,
        required_concepts: set[str] | frozenset[str] | None = None,
        expected_company_id: str | None = None,
        source_evidence: Sequence[EvidenceRef] = (),
    ) -> FinancialGroupValidation:
        issues: list[str] = []
        fact_issues: dict[str, list[str]] = {}

        def isolate(fact: FinancialFact, issue: str) -> None:
            fact_issues.setdefault(fact.fact_id, []).append(issue)
            issues.append(issue)

        source_evidence_by_id = {
            ref.evidence_id: ref for ref in source_evidence
            if isinstance(ref, EvidenceRef) and ref.evidence_id
        }

        def normalization_issue(fact: FinancialFact, row_ref: EvidenceRef | None) -> str | None:
            lineage = fact.normalization_lineage
            if lineage is None:
                return "normalization_lineage_missing"
            if row_ref is None:
                return "normalization_source_row_missing"

            def validate_node(node) -> Decimal:
                if node.inputs:
                    if node.quantity_kind != "money" or node.currency != fact.currency:
                        raise ValueError("derived_lineage_identity_mismatch")
                    child_values = [validate_node(child) for child in node.inputs]
                    if (
                        node.method == "sum-absolute-components-v1"
                        and node.value_transform == "sum"
                        and fact.concept == "capital_expenditure"
                        and fact.parser_version == _PDF_PARSER_VERSION
                    ):
                        normalized = exact_decimal_arithmetic(child_values)
                    elif (
                        node.method == "sec-xbrl-arithmetic-v1"
                        and node.value_transform in {"sum", "subtract"}
                        and fact.concept == "liabilities"
                        and fact.parser_version in {"sec-companyfacts-v3", "sec-companyfacts-v4"}
                    ):
                        normalized = exact_decimal_arithmetic(child_values, node.value_transform)
                    else:
                        raise ValueError("unsupported_derived_lineage")
                    if Decimal(node.normalized_value) != normalized:
                        raise ValueError("derived_lineage_total_mismatch")
                    expected_evidence_ids = tuple(dict.fromkeys(
                        evidence_id
                        for child in node.inputs
                        for evidence_id in child.unit_evidence_ids
                    ))
                    if tuple(node.unit_evidence_ids) != expected_evidence_ids:
                        raise ValueError("derived_lineage_evidence_mismatch")
                    return normalized

                from .financial_contracts import QuantityKind, SourceAmount

                expected_kind = (
                    QuantityKind.RATIO
                    if fact.concept in {"reported_roe", "reported_roe_excluding_nonrecurring"}
                    else QuantityKind.SHARES
                    if fact.concept == "shares_outstanding"
                    else QuantityKind.MONEY
                )
                if node.quantity_kind != expected_kind.value:
                    raise ValueError("quantity_kind_mismatch")
                if expected_kind is QuantityKind.MONEY and node.currency != fact.currency:
                    raise ValueError("currency_mismatch")
                if expected_kind is QuantityKind.RATIO and node.currency:
                    raise ValueError("ratio_currency_mismatch")
                if node.value_transform == "absolute_value" and fact.concept not in {
                    "capital_expenditure", "capital_expenditure_ppe", "capital_expenditure_intangible",
                    "share_repurchase", "shareholder_dividends_paid",
                    "lease_liability_payment", "deferred_asset_acquisition_payment",
                }:
                    raise ValueError("unsupported_fact_sign_transform")
                if node.value_transform == "negative_absolute_value":
                    label = fact.reported_concept
                    source_concept_matches = profit_row_concept(label, fact.raw_text) == fact.concept
                    if fact.concept in {"profit_before_tax", "operating_income"}:
                        source_concept_matches = any(
                            _label_compact(alias) in _label_compact(label)
                            for alias in FINANCIAL_LABEL_ALIASES[fact.concept]
                        )
                    if (
                        profit_loss_value_transform(label, fact.concept) != "negative_absolute_value"
                        or not source_concept_matches
                        or not _label_compact(label)
                        or not _source_row_contains_label(label, row_ref.excerpt)
                        or row_ref.excerpt != fact.raw_text
                    ):
                        raise ValueError("reported_loss_sign_unproven")
                source_amount = SourceAmount(
                    node.raw_value, node.scale, node.quantity_kind, node.currency,
                    tuple(node.unit_evidence_ids), node.value_transform,
                )
                expected = source_amount.normalize()
                if (
                    node.normalized_value != expected.normalized_value
                    or node.method != expected.method
                ):
                    raise ValueError("normalized_amount_mismatch")
                return Decimal(expected.normalized_value)

            try:
                normalized = validate_node(lineage)
                if fact.parser_version == _PDF_PARSER_VERSION:
                    if (
                        fact.source_page is None or fact.source_bbox is None
                        or row_ref.bbox != fact.source_bbox
                        or row_ref.locator != f"page:{fact.source_page}"
                        or row_ref.excerpt != fact.raw_text
                    ):
                        return "normalization_source_row_binding_mismatch"
                    observed = set()
                    for match in _NUM.finditer(row_ref.excerpt.replace("−", "-")):
                        token = match.group().replace(",", "").replace(" ", "")
                        if token.startswith("(") and token.endswith(")"):
                            token = "-" + token[1:-1]
                        observed.add(Decimal(token))
                    def source_leaves(node):
                        if node.inputs:
                            for child in node.inputs:
                                yield from source_leaves(child)
                        else:
                            yield node

                    for leaf in source_leaves(lineage):
                        raw_token = leaf.raw_value.strip().replace("−", "-").replace(",", "")
                        if raw_token.startswith("(") and raw_token.endswith(")"):
                            raw_token = "-" + raw_token[1:-1].strip()
                        if Decimal(raw_token) not in observed:
                            return "normalization_raw_value_not_in_source_row"
                        if Decimal(leaf.scale) != Decimal(lineage.scale):
                            return "normalization_scale_does_not_match_fact"
                    column_years = column_period_years(fact.source_column)
                    if column_years and fact.fiscal_year not in column_years:
                        return "normalization_period_column_mismatch"
                if float(normalized) != float(fact.value):
                    return "normalized_amount_does_not_match_fact"
                if fact.concept not in {"reported_roe", "reported_roe_excluding_nonrecurring"} and float(Decimal(lineage.scale)) != float(fact.unit_scale):
                    return "normalization_scale_does_not_match_fact"
                for evidence_id in lineage.unit_evidence_ids:
                    ref = source_evidence_by_id.get(evidence_id)
                    locator_is_bound = bool(
                        ref is not None
                        and (
                            re.fullmatch(r"page:\d+", ref.locator)
                            or re.fullmatch(r"xbrl:[^\s]{1,300}", ref.locator)
                        )
                    )
                    if (
                        ref is None
                        or ref.document_id != row_ref.document_id
                        or ref.source_url != row_ref.source_url
                        or (row_ref.content_hash and ref.content_hash != row_ref.content_hash)
                        or not locator_is_bound
                        or not ref.excerpt.strip()
                    ):
                        return "normalization_unit_evidence_unbound"
                if not lineage.unit_evidence_ids:
                    return "normalization_unit_evidence_missing"
                if fact.parser_version == _PDF_PARSER_VERSION:
                    for leaf in source_leaves(lineage):
                        if leaf.quantity_kind == "money":
                            unit = bound_money_unit(fact, tuple(source_evidence_by_id[key]
                                                       for key in leaf.unit_evidence_ids), source_evidence_by_id)
                            if not unit.proves(leaf.currency, leaf.scale):
                                return "normalization_unit_declaration_mismatch"
            except (InvalidOperation, TypeError, ValueError):
                return "normalization_lineage_invalid"
            return None

        required = set(required_concepts or {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"})
        # A normalized structured fact may legitimately use scale=1 while a
        # PDF fact from the same filing retains the table's displayed unit.
        # The same report may show an MDA summary in millions and a formal
        # statement in thousands. Compare scales only within the SAME bound
        # unit declaration. Never use per-fact evidence IDs as the context:
        # facts inherit distinct IDs for the same physical header.
        def scale_context(fact: FinancialFact) -> tuple[object, ...]:
            context = physical_unit_context(fact, source_evidence_by_id)
            return (fact.source_document, fact.parser_version, fact.statement, context)

        statement_scales: dict[tuple[object, ...], set[float]] = {}
        for fact in facts:
            try:
                finite_amount = math.isfinite(float(fact.value)) and math.isfinite(float(fact.unit_scale))
            except (TypeError, ValueError, OverflowError):
                finite_amount = False
            if not finite_amount:
                isolate(fact, "non_finite_fact_value_or_scale")
                continue
            if (
                fact.currency
                and fact.concept not in {"reported_roe", "reported_roe_excluding_nonrecurring"}
                and fact.statement in {"income_statement", "balance_sheet", "cash_flow"}
                and fact.source_document
                and fact.parser_version
            ):
                scale_key = scale_context(fact)
                statement_scales.setdefault(scale_key, set()).add(float(fact.unit_scale))
        for scale_key, scales in statement_scales.items():
            if len(scales) <= 1:
                continue
            issues.append("statement_unit_scale_inconsistent")
            for fact in facts:
                if scale_context(fact) == scale_key:
                    isolate(fact, "statement_unit_scale_inconsistent")
        expected_identity = identity[1:]
        for fact in facts:
            if expected_company_id is not None and str(fact.company_cik or "").strip() != expected_company_id:
                isolate(fact, "issuer_identity_mismatch")
            fact_identity = (
                fact.end_date,
                (fact.fiscal_period or "FY").upper(),
                fact.consolidated_scope or fact.scope or "unknown",
                fact.currency or "",
            )
            if fact_identity != expected_identity:
                isolate(fact, "group_identity_inconsistent")
            if not fact.accession_number or not fact.source_url or not fact.raw_text:
                isolate(fact, "provenance_missing")
            statement_expected = _STATEMENT_FOR.get(fact.concept)
            if statement_expected and fact.statement != statement_expected and fact.concept not in {"reported_roe", "reported_roe_excluding_nonrecurring"}:
                isolate(fact, "statement_mismatch")
            if (
                fact.concept in {"reported_roe", "reported_roe_excluding_nonrecurring"}
                and fact.unit_provenance not in {"explicit_percent", "explicit_ratio"}
            ):
                # A reported ROE is a ratio, but its source display may be a
                # percentage or a decimal. Keep the field out of trusted
                # research until the source explicitly establishes which.
                isolate(fact, "ratio_unit_unknown")
            if fact.parser_version == _PDF_PARSER_VERSION and fact.concept in {
                "reported_roe", "reported_roe_excluding_nonrecurring"
            }:
                ratio_label = normalize_financial_label(fact.reported_concept)
                adjusted = any(term in ratio_label for term in (
                    "扣非", "扣除非经常", "扣除非經常", "excludingnonrecurring"))
                expected_ratio = "reported_roe_excluding_nonrecurring" if adjusted else "reported_roe"
                if (expected_ratio != fact.concept
                    or not any(term in ratio_label for term in ("净资产收益", "淨資產收益", "returnonequity"))
                    or any(term in ratio_label for term in ("每股收益", "earningspershare"))):
                    isolate(fact, "ratio_source_concept_mismatch")
            ref = (evidence_map or {}).get(fact.fact_id)
            inline_issue = inline_source_issue(fact, ref, source_evidence_by_id)
            if inline_issue:
                isolate(fact, inline_issue)
            if fact.dimensions and fact.parser_version != INLINE_PARSER_VERSION:
                isolate(fact, "dimension_source_proof_unsupported")
            structured_issue = companyfacts_source_issue(fact, ref)
            if structured_issue:
                isolate(fact, structured_issue)
            cloud_issue = cloud_table_source_issue(fact, ref, source_evidence_by_id)
            if cloud_issue:
                isolate(fact, cloud_issue)
            gateway_issue = gateway_source_issue(fact, ref, source_evidence_by_id)
            if gateway_issue:
                isolate(fact, gateway_issue)
            mapping_issue = cell_repair_issue(fact, ref, source_evidence_by_id)
            if mapping_issue:
                isolate(fact, mapping_issue)
            disclosure_issue = disclosure_source_issue(fact, ref, source_evidence_by_id)
            if disclosure_issue:
                isolate(fact, disclosure_issue)
            note_issue = note_source_issue(fact, ref, source_evidence_by_id)
            if note_issue:
                isolate(fact, note_issue)
            statement_issue = _statement_source_issue(fact, ref, source_evidence_by_id)
            if statement_issue:
                isolate(fact, statement_issue)
            if fact.parser_version.startswith("financial-ingestion-ast") and (ref is None or ref.bbox is None):
                isolate(fact, "pdf_evidence_bbox_missing")
            if (
                fact.parser_version == _PDF_PARSER_VERSION
                and fact.concept in {"net_income", "parent_net_income", "minority_interest_profit", "ordinary_shareholder_net_income"}
                and profit_row_concept(fact.reported_concept, fact.raw_text) != fact.concept
            ):
                isolate(fact, "profit_source_concept_mismatch")
            if (
                fact.parser_version == _PDF_PARSER_VERSION
                and fact.concept in {"interest_revenue", "finance_interest_income"}
                and interest_income_row_concept(fact.reported_concept, fact.raw_text) != fact.concept
            ):
                isolate(fact, "interest_source_hierarchy_mismatch")
            if fact.parser_version == _PDF_PARSER_VERSION and fact.concept in {"equity", "total_equity"}:
                context = [source_evidence_by_id.get(item) for item in fact.classification_evidence_ids
                           if not item.startswith(("statement:", "cell-reread:"))]
                prior_top = fact.source_bbox[3] if fact.source_bbox else float("inf")
                bound = True
                for context_ref in context:
                    if (
                        context_ref is None or ref is None or context_ref.bbox is None
                        or context_ref.document_id != ref.document_id
                        or context_ref.source_url != ref.source_url
                        or context_ref.content_hash != ref.content_hash
                        or context_ref.locator != ref.locator
                        or context_ref.bbox[1] < prior_top
                        or context_ref.bbox[1] - prior_top > 80
                    ):
                        bound = False
                        break
                    prior_top = context_ref.bbox[3]
                classification = equity_row_concept(
                    fact.reported_concept,
                    following_labels=tuple(_NUM.sub("", item.excerpt).strip() for item in context if item is not None),
                ) if bound else None
                if (
                    classification != fact.concept
                    or not _source_row_contains_label(fact.reported_concept, fact.raw_text)
                ):
                    # Legacy unlabeled subtotals have their explicit heading
                    # in the same bound row; never accept a generic owner label
                    # merely because its amount resembles assets-liabilities.
                    isolate(fact, "equity_source_concept_mismatch")
            if fact.parser_version == _PDF_PARSER_VERSION and fact.statement == "comprehensive_income":
                if (comprehensive_income_row_concept(fact.reported_concept) != fact.concept
                        or not _source_row_contains_label(fact.reported_concept, fact.raw_text)):
                    isolate(fact, "comprehensive_income_source_concept_mismatch")
            if fact.parser_version == _PDF_PARSER_VERSION and fact.concept in {"mezzanine_equity", "minority_interest_equity"}:
                if (
                    balance_component_row_concept(fact.reported_concept) != fact.concept
                    or not _source_row_contains_label(fact.reported_concept, fact.raw_text)
                ):
                    isolate(fact, "balance_component_source_concept_mismatch")
            if (fact.parser_version == _PDF_PARSER_VERSION
                    and (fact.concept == "capital_expenditure" or fact.concept in CAPITAL_ALLOCATION_PAYMENT_ALIASES)):
                allocation_concept = capital_allocation_payment_concept(fact.reported_concept)
                source_row = re.sub(r"\s*\[column:(?:19|20)\d{2}\]\s*$", "", ref.excerpt) if ref is not None else ""
                source_label = _NUM.sub("", source_row)
                source_allocation = capital_allocation_payment_concept(source_label)
                if (
                    (fact.concept == "capital_expenditure" and (
                        has_distinct_capital_payment_label(fact.reported_concept)
                        or has_distinct_capital_payment_label(source_label)))
                    or (fact.concept in CAPITAL_ALLOCATION_PAYMENT_ALIASES
                        and (allocation_concept != fact.concept
                             or source_allocation != fact.concept
                             or not _source_row_contains_label(fact.reported_concept, fact.raw_text)))
                ):
                    isolate(fact, "capital_allocation_source_concept_mismatch")
            if fact.parser_version == _PDF_PARSER_VERSION and fact.concept == "liabilities":
                if not liability_total_label_matches(fact.reported_concept) or not liability_total_label_matches(fact.raw_text):
                    isolate(fact, "liability_source_concept_mismatch")
            if fact.parser_version == _PDF_PARSER_VERSION and fact.concept == "operating_cash_flow":
                aliases = FINANCIAL_LABEL_ALIASES["operating_cash_flow"]
                if not cash_flow_total_label_matches(fact.reported_concept, aliases) or not cash_flow_total_label_matches(fact.raw_text, aliases):
                    isolate(fact, "cash_flow_source_concept_mismatch")
            if (
                fact.parser_version.startswith("financial-ingestion-ast")
                and fact.parser_version != _PDF_PARSER_VERSION
            ):
                isolate(fact, "stale_financial_parser_contract")
            if (
                fact.parser_version in {_PDF_PARSER_VERSION, "sec-companyfacts-v3", "sec-companyfacts-v4"}
                or fact.parser_version in {GATEWAY_FACT_VERSION, "vision-json-v3", "vision-markdown-v3"}
                or fact.normalization_lineage is not None
            ):
                issue = normalization_issue(fact, ref)
                if issue:
                    isolate(fact, issue)
            if (
                fact.concept == "profit_before_tax"
                and fact.parser_version.startswith("financial-ingestion-ast")
            ):
                row_issue = _profit_before_tax_row_issue(fact, ref)
                if row_issue:
                    isolate(fact, row_issue)
            if fact.parser_version.startswith("vision-"):
                if fact.parser_version == "vision-markdown-v3":
                    isolate(fact, "stale_cloud_table_contract")
                expected_vision_statement = _STATEMENT_FOR.get(fact.concept)
                if (
                    expected_vision_statement is None
                    or fact.statement != expected_vision_statement
                    or fact.unit_provenance != "vision_explicit"
                    or fact.consolidated_scope != "consolidated"
                    or fact.unit_scale not in {1.0, 1_000.0, 10_000.0, 100_000.0, 1_000_000.0, 100_000_000.0, 1_000_000_000.0}
                    or not fact.raw_text
                ):
                    isolate(fact, "vision_fact_contract_invalid")
            if (
                fact.parser_version.startswith("financial-ingestion-ast")
                and fact.concept in _STATEMENT_FOR
                and fact.concept not in {"reported_roe", "reported_roe_excluding_nonrecurring"}
                and fact.unit_provenance == "unknown"
            ):
                # Optional money fields (notably total equity) are no safer
                # than required ones when their displayed unit is unknown.
                # Keep unit safety separate from the current coverage profile.
                isolate(fact, "unit_provenance_missing")
            if fact.parser_version.startswith("vision-"):
                if ref is None or not ref.content_hash or not ref.locator.startswith("page:") or str(fact.source_page or 0) != ref.locator.split(":", 1)[1] or ref.evidence_id != f"fact:{fact.fact_id}":
                    isolate(fact, "vision_evidence_provenance_missing")
        safe_facts = [fact for fact in facts if fact.fact_id not in fact_issues]
        safe_values = {fact.concept: fact.value for fact in safe_facts}
        if safe_values.get("revenue") is not None and safe_values["revenue"] < 0:
            for fact in safe_facts:
                if fact.concept == "revenue":
                    isolate(fact, "negative_revenue")
            safe_facts = [fact for fact in safe_facts if fact.concept != "revenue"]
            safe_values.pop("revenue", None)
        # Economic ratios are not parser-integrity proofs: banks can have
        # unusual leverage and issuers can report negative or thin equity.
        # Check the accounting identity only when total equity (rather than
        # parent-attributable equity) is explicitly available.
        assets = safe_values.get("assets")
        liabilities = safe_values.get("liabilities")
        total_equity = safe_values.get("total_equity")
        invalid_concepts: set[str] = set()
        if assets is not None and liabilities is not None and total_equity is not None:
            if abs(assets - liabilities - total_equity) / max(abs(assets), 1.0) > 0.08:
                issues.append("balance_sheet_imbalance")
                invalid_concepts.update({"assets", "liabilities", "total_equity"})
        for fact in safe_facts:
            if fact.concept in invalid_concepts:
                isolate(fact, "balance_sheet_imbalance")
        accepted = tuple(fact for fact in safe_facts if fact.concept not in invalid_concepts)
        accepted_ids = {fact.fact_id for fact in accepted}
        quarantined = tuple(fact for fact in facts if fact.fact_id not in accepted_ids)

        def bound_row_for(fact: FinancialFact) -> EvidenceRef | None:
            row = (evidence_map or {}).get(fact.fact_id)
            if row is None or row.evidence_id not in fact_row_evidence_ids(fact):
                return None
            captured = source_evidence_by_id.get(row.evidence_id)
            if captured is None or (
                captured.document_id != row.document_id
                or captured.source_url != row.source_url
                or captured.content_hash != row.content_hash
                or captured.locator != row.locator
                or captured.excerpt != row.excerpt
                or captured.bbox != row.bbox
            ):
                return None
            if (
                not row.document_id
                or row.source_url != fact.source_url
                or not row.content_hash
                or not row.excerpt.strip()
            ):
                return None
            if fact.parser_version == _PDF_PARSER_VERSION and (
                fact.source_page is None
                or fact.source_bbox is None
                or row.locator != f"page:{fact.source_page}"
                or row.bbox != fact.source_bbox
                or row.excerpt != fact.raw_text
            ):
                return None
            return row

        def bound_unit_refs(fact: FinancialFact, row: EvidenceRef | None) -> tuple[EvidenceRef, ...]:
            lineage = fact.normalization_lineage
            if row is None or lineage is None or not lineage.unit_evidence_ids:
                return ()
            refs: list[EvidenceRef] = []
            for evidence_id in lineage.unit_evidence_ids:
                ref = source_evidence_by_id.get(evidence_id)
                if (
                    ref is None
                    or ref.document_id != row.document_id
                    or ref.source_url != row.source_url
                    or ref.content_hash != row.content_hash
                    or not ref.excerpt.strip()
                    or not isinstance(ref.locator, str)
                    or not re.fullmatch(r"(?:page:\d+|xbrl:[^\s]{1,300})", ref.locator)
                ):
                    return ()
                refs.append(ref)
            return tuple(refs)

        def component_record(fact: FinancialFact) -> FactValidationRecord:
            lineage = fact.normalization_lineage
            row = bound_row_for(fact)
            row_ids = (row.evidence_id,) if row is not None else ()
            unit_refs = bound_unit_refs(fact, row)
            unit_ids = tuple(ref.evidence_id for ref in unit_refs)
            unit = bound_money_unit(fact, unit_refs, source_evidence_by_id) if unit_refs else None

            amount_candidate = lineage.raw_value if lineage is not None else fact.value
            amount_state = "unknown"
            if (lineage is not None and row is not None and not lineage.inputs
                    and isinstance(lineage.raw_value, str)):
                raw_token = lineage.raw_value.strip().replace("−", "-").replace(",", "")
                if raw_token.startswith("(") and raw_token.endswith(")"):
                    raw_token = "-" + raw_token[1:-1].strip()
                try:
                    row_values = {
                        Decimal(match.group().replace("−", "-").replace(",", "").replace(" ", "").strip("()"))
                        * (-1 if match.group().strip().startswith("(") else 1)
                        for match in _NUM.finditer(row.excerpt)
                    }
                    amount_state = "observed" if Decimal(raw_token) in row_values else "conflict"
                except (InvalidOperation, ValueError):
                    amount_state = "conflict"
            amount = SourceComponentObservation(amount_state, amount_candidate, row_ids)

            scale_candidate = str(lineage.scale) if lineage is not None else str(fact.unit_scale)
            scale_state = "unknown"
            scale_value: object = scale_candidate
            if lineage is not None:
                try:
                    if Decimal(lineage.scale) != Decimal(str(fact.unit_scale)):
                        scale_state = "conflict"
                except (InvalidOperation, TypeError, ValueError):
                    scale_state = "conflict"
            if unit is not None and unit.scales:
                if len(unit.scales) != 1:
                    scale_state = "conflict"
                else:
                    source_scale = next(iter(unit.scales))
                    try:
                        scale_state = (
                            "observed"
                            if lineage is not None
                            and Decimal(source_scale) == Decimal(lineage.scale)
                            and Decimal(str(fact.unit_scale)) == Decimal(lineage.scale)
                            else "conflict"
                        )
                    except (InvalidOperation, ValueError):
                        scale_state = "conflict"
                    if scale_state == "observed":
                        scale_value = source_scale
            scale = SourceComponentObservation(scale_state, scale_value, unit_ids)

            currency_candidates = tuple(dict.fromkeys(
                    str(value).strip().upper()
                for value in (
                    lineage.currency if lineage is not None else "",
                    fact.currency,
                )
                if value and value.strip()
            ))
            currency_candidate = currency_candidates[0] if currency_candidates else ""
            currency_state = "conflict" if len(currency_candidates) > 1 else "unknown"
            currency_value: object = currency_candidate or None
            if unit is not None and unit.currencies:
                if len(unit.currencies) != 1:
                    currency_state = "conflict"
                else:
                    source_currency = next(iter(unit.currencies))
                    if any(candidate != source_currency for candidate in currency_candidates):
                        currency_state = "conflict"
                    else:
                        currency_state = "observed"
                        currency_value = source_currency
            currency = SourceComponentObservation(currency_state, currency_value, unit_ids)

            source_column = str(fact.source_column or "")
            period_candidate = {
                "source_column": source_column or None,
                "fiscal_year": fact.fiscal_year,
                "fiscal_period": fact.fiscal_period,
                "period_start": fact.period_start or fact.start_date,
                "period_end": fact.end_date,
            }
            period_state = "unknown"
            period_year_proof = False
            if row is not None and source_column:
                years = {
                    int(value) for value in re.findall(
                        r"(?<!\d)(?:19|20)\d{2}(?!\d)", source_column
                    )
                }
                bound_column_markers = re.findall(
                    r"\[column:((?:19|20)\d{2})\]", row.excerpt
                )
                marker_conflict = bool(
                    bound_column_markers
                    and any(int(marker) != fact.fiscal_year for marker in bound_column_markers)
                )
                period_year_proof = bool(years and fact.fiscal_year in years)
                period_state = (
                    "observed"
                    if not marker_conflict and (not years or fact.fiscal_year in years)
                    else "conflict"
                )
            period = SourceComponentObservation(period_state, period_candidate, row_ids)

            classification_refs: list[EvidenceRef] = []
            if row is not None:
                for evidence_id in fact.classification_evidence_ids:
                    ref = source_evidence_by_id.get(evidence_id)
                    is_bound = bool(
                        ref is not None
                        and isinstance(ref.locator, str)
                        and ref.document_id == row.document_id
                        and ref.source_url == row.source_url
                        and ref.content_hash == row.content_hash
                        and ref.locator.startswith("page:")
                        and ref.excerpt.strip()
                    )
                    if is_bound and evidence_id.startswith("statement:"):
                        page_match = re.fullmatch(r"page:(\d+)", ref.locator)
                        bbox = ref.bbox
                        valid_bbox = bool(
                            isinstance(bbox, tuple)
                            and len(bbox) == 4
                            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox)
                        )
                        is_bound = bool(
                            page_match
                            and valid_bbox
                            and statement_title_evidence_id(
                                ref.document_id, int(page_match.group(1)), bbox, ref.excerpt
                            ) == evidence_id
                        )
                    if is_bound:
                        classification_refs.append(ref)
            scope_candidate = fact.consolidated_scope or fact.scope or None
            scope_state = "unknown"
            scope_value: object = {
                "candidate": scope_candidate,
                "source_text": [],
            }
            scope_refs = [
                ref for ref in classification_refs
                if ref.evidence_id.startswith("statement:")
            ]
            if scope_refs:
                classifications = [classify_statement_title(ref.excerpt) for ref in scope_refs]
                matching = [
                    ref for ref, (_statement, scope) in zip(scope_refs, classifications)
                    if scope == scope_candidate
                ]
                scope_value = {
                    "candidate": scope_candidate,
                    "source_text": [ref.excerpt for ref in scope_refs],
                }
                scope_state = "observed" if matching and len(matching) == len(scope_refs) else "conflict"
            if fact.parser_version == DISCLOSURE_VERSION and row is not None:
                disclosed = [item for ref in classification_refs
                    for item in explicit_money_disclosures(ref.excerpt, fact.end_date)
                    if item.excerpt == row.excerpt]
                if len(disclosed) == 1:
                    source_item = disclosed[0]
                    scope_state = "observed" if source_item.scope == scope_candidate else "conflict"
                    scope_value = {"candidate": scope_candidate, "source_text": [source_item.excerpt]}
                    period_year_proof = source_item.period_end == fact.end_date and fact.fiscal_year == int(source_item.period_end[:4])
                    period = SourceComponentObservation("observed" if period_year_proof else "conflict",
                        period_candidate, tuple(ref.evidence_id for ref in classification_refs))
            elif fact.parser_version in NOTE_DISCLOSURE_VERSIONS and row is not None:
                note_scopes = [ref for ref in classification_refs if ref.evidence_id.startswith("note-scope:")]
                note_periods = [ref for ref in classification_refs if ref.evidence_id.startswith("note-period:")]
                if len(note_scopes) == len(note_periods) == 1:
                    source_scope = "parent" if "母公司" in note_scopes[0].excerpt else "consolidated"
                    scope_state = "observed" if source_scope == scope_candidate else "conflict"
                    scope_value = {"candidate": scope_candidate, "source_text": [note_scopes[0].excerpt]}
                    period_year_proof = (str(fact.fiscal_year) in row.excerpt
                        or any(str(fact.fiscal_year) in ref.excerpt for ref in classification_refs
                               if ref.evidence_id.startswith("note-context:")))
                    if period_year_proof:
                        period = SourceComponentObservation("observed", period_candidate,
                            tuple(ref.evidence_id for ref in classification_refs))
            scope = SourceComponentObservation(
                scope_state,
                scope_value,
                tuple(ref.evidence_id for ref in classification_refs),
            )

            # Verification is a projection of the existing admission proof,
            # never a second path to admission. Legacy/synthetic accepted
            # fixtures without a bound row and literal units remain observed
            # or unknown rather than being promoted.
            source_proof_succeeded = bool(
                fact.fact_id in accepted_ids
                and fact.fact_id not in fact_issues
                and lineage is not None
                and row is not None
                and unit is not None
                and unit.proves(lineage.currency, lineage.scale)
                and amount.state == "observed"
            )
            components = (
                ("amount", amount),
                ("scale", scale),
                ("currency", currency),
                ("period", period),
                ("scope", scope),
            )
            if source_proof_succeeded:
                components = tuple(
                    (name, SourceComponentObservation("verified", observation.value, observation.evidence_ids))
                    if observation.state == "observed" and (name != "period" or period_year_proof)
                    else (name, observation)
                    for name, observation in components
                )
            component_dependencies = (
                ("amount", row_ids),
                ("scale", unit_ids),
                ("currency", unit_ids),
                ("period", row_ids),
                ("scope", tuple(ref.evidence_id for ref in classification_refs)),
            )
            return FactValidationRecord(
                fact.fact_id,
                fact.fact_id in accepted_ids,
                tuple(dict.fromkeys(fact_issues.get(fact.fact_id, ()))),
                components,
                component_dependencies,
            )

        records_by_id: dict[str, FactValidationRecord] = {}
        for fact in facts:
            record = component_record(fact)
            existing = records_by_id.get(record.fact_id)
            if existing is None:
                records_by_id[record.fact_id] = record
            else:
                records_by_id[record.fact_id] = FactValidationRecord(
                    record.fact_id,
                    existing.accepted and record.accepted,
                    tuple(dict.fromkeys((*existing.issues, *record.issues))),
                    record.components,
                    tuple((name, tuple(dict.fromkeys((*dict(existing.dependencies).get(name, ()), *ids))))
                          for name, ids in record.dependencies),
                )
        accepted_concepts = {fact.concept for fact in accepted}
        covered = accepted_concepts & _CORE
        missing_required = missing_profile_concepts(accepted_concepts, required)
        if {"revenue", "net_income"}.issubset(required) and {"revenue", "net_income"} & missing_required:
            issues.append("income_statement_core_missing")
        if "operating_cash_flow" in required and "operating_cash_flow" not in covered:
            issues.append("cash_flow_core_missing")
        balance_required = {"assets", "liabilities"} & required
        equity_required = bool({"equity", "total_equity"} & required)
        if (balance_required - covered) or (equity_required and not ({"equity", "total_equity"} & accepted_concepts)):
            issues.append("balance_sheet_core_missing")
        if missing_required or (equity_required and not ({"equity", "total_equity"} & accepted_concepts)):
            issues.append("core_coverage_insufficient")
        fatal_issues = [issue for issue in issues if issue not in _COVERAGE_WARNING_ISSUES]
        status = (
            ValidationStatus.REJECTED
            if not accepted and (fatal_issues or facts)
            else ValidationStatus.READY_WITH_WARNINGS
            if issues or quarantined
            else ValidationStatus.VERIFIED
        )
        validation = FinancialValidation(
            status,
            tuple(dict.fromkeys(issues)),
            frozenset(covered),
            accepted,
            quarantined,
            tuple(records_by_id.values()),
        )
        return FinancialGroupValidation(identity, validation)
