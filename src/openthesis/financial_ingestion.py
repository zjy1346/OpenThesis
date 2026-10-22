"""Structured-first financial ingestion with auditable PDF table parsing.

The public seam in this module deliberately separates source adapters from the
normalisation/validation policy.  A PDF is parsed as positioned words grouped
into page/table/row/cell nodes; numbers are never selected from an unscoped
page-wide regular-expression match.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace, asdict
from copy import deepcopy
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
import hashlib
import inspect
import json
import calendar
import math
import multiprocessing as mp
import os
import queue
import re
import tempfile
import threading
import time
import types


_PDF_TAXONOMY_VERSION = "canonical-taxonomy-v1"
_PDF_CACHE_POLICY_VERSION = "parse-cache-v5-source-label-boundaries"


@dataclass
class _PdfParseFlight:
    """In-process single-flight state for one content-addressed parse."""

    event: threading.Event
    result: tuple[list[FinancialFact], list[EvidenceRef], str | None] | None = None
    participants: dict[object, tuple[Callable[[], bool] | None, float]] = field(default_factory=dict)
    retiring: bool = False
    progress_sequence: int = 0
    latest_progress: tuple = ()


_PDF_FLIGHT_LOCK = threading.Lock()
_PDF_FLIGHTS: dict[str, _PdfParseFlight] = {}
_PDF_PRODUCERS = ThreadPoolExecutor(max_workers=3, thread_name_prefix="financial-shared-producer")

from .domain import (
    Company, EvidenceRef, FilingDocument, FinancialFact,
    FinancialNormalizationLineage,
)
from .financial_contracts import (
    CandidateBatch, CompilationPolicy, FactCandidate, FactIdentity, FinancialCandidateCollection,
    FinancialDataset as CanonicalFinancialDataset,
    GapStageKind, FinancialGroupValidation,
    FinancialValidation, PDF_PARSER_VERSION as _PDF_PARSER_VERSION,
    ROE_ROW_MAPPING_VERSION as _ROE_ROW_MAPPING_VERSION,
    QuantityKind, SourceAmount, STATEMENT_FOR as _STATEMENT_FOR, ValidationStatus,
    exact_decimal_arithmetic,
)
from .financial_profile import (
    FinancialProfile, _fact_period_identity, _validation_group_role,
    build_financial_profile,
)
from .financial_validation import FinancialValidator
from .financial_unit_declarations import literal_money_unit
from .disclosure_index import cached_page_texts, remember_page_texts
from .financial_sources.amount_disclosures import (
    DISCLOSURE_VERSION, disclosure_page_needed, extract_disclosure_candidates,
)
from .financial_sources.note_disclosures import NOTE_DISCLOSURE_VERSION, NOTE_DISCLOSURE_VERSIONS, extract_note_candidates

_SOURCE_DISCLOSURE_VERSION = DISCLOSURE_VERSION + "/" + NOTE_DISCLOSURE_VERSION
from .disclosure_identity import DisclosureIdentityResolver, _title_report_year
from .financial_compatibility import FinancialRulesSnapshot
from .financial_statement_identity import STATEMENT_TITLE_MARKERS, classify_statement_title, is_narrative_title_tail, statement_title_evidence_id
from .financial_taxonomy import (
    FINANCIAL_LABEL_ALIASES,
    balance_component_row_concept,
    cash_flow_total_label_matches, comprehensive_income_row_concept,
    liability_total_label_matches,
    capex_component_kind,
    CAPITAL_ALLOCATION_PAYMENT_ALIASES, capital_allocation_payment_concept,
    has_distinct_capital_payment_label,
    normalize_financial_label,
    profit_row_concept,
    profit_loss_value_transform,
    is_profit_sign_convention,
    total_revenue_label_matches,
    equity_row_concept,
    interest_income_row_concept,
    financial_label_body,
    is_profit_ownership_heading,
)
from .financial_sources.document_units import DocumentUnitResolution, discover_document_units
from .financial_sources.pdf_document_units import read_document_context, read_document_units
from .financial_sources.statement_periods import StatementPeriodDeclaration, statement_period_declarations, statement_period_gaps, period_gap_evidence
from .vision_financials import (
    VISION_MAX_PAGES,
    VisionFallbackConfig,
    VisionFinancialSourceAdapter,
    VisionPageRequest,
    _run_pdfium_isolated,
)


class FinancialExtractionError(RuntimeError):
    """A filing could not be converted into trustworthy structured facts."""


@dataclass(frozen=True, slots=True)
class OfficialSource:
    entity: str
    market: str
    statement: str
    concept: str
    fiscal_period: str
    period_start: str | None
    period_end: str
    consolidated_scope: str
    currency: str
    unit: str
    revision: str
    source_url: str
    document: str = ""
    page: int | None = None
    raw_text: str = ""
    parser_version: str = ""
    validation_status: str = ValidationStatus.READY_WITH_WARNINGS.value


class FinancialSourceAdapter(Protocol):
    """Provider seam. Implementations may be XBRL, API, or local fixtures."""

    def fetch(
        self, company: Company, filing: FilingDocument
    ) -> tuple[list[FinancialFact], list[EvidenceRef], str | None]: ...


StructuredFinancialProvider = FinancialSourceAdapter


@dataclass(frozen=True, slots=True)
class InMemoryFinancialSource:
    facts_by_document: dict[str, tuple[FinancialFact, ...]]
    evidence_by_document: dict[str, tuple[EvidenceRef, ...]] | None = None
    failure_by_document: dict[str, str] | None = None

    def fetch(self, company: Company, filing: FilingDocument):
        failure = (self.failure_by_document or {}).get(filing.document_id)
        if failure:
            return [], [], failure
        facts = [
            replace(fact, unit_provenance="structured_normalized")
            if fact.unit_provenance == "unknown" and fact.concept != "reported_roe"
            else fact
            for fact in self.facts_by_document.get(filing.document_id, ())
        ]
        refs = list((self.evidence_by_document or {}).get(filing.document_id, ()))
        return facts, refs, None if facts else "structured_source_empty"


@dataclass(frozen=True, slots=True)
class PdfCellAST:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float


def _reading_order_cells(cells: Sequence[PdfCellAST]) -> tuple[PdfCellAST, ...]:
    """Order one visual line by x before proceeding to a wrapped line.

    Fonts on the same baseline can differ by hundredths of a point. Sorting
    by exact top before x reverses words in an otherwise intact source row.
    Use the same 2.5-point line band as the positioned-word collector.
    """
    bands: list[list[PdfCellAST]] = []
    for cell in sorted(cells, key=lambda item: (item.top, item.x0)):
        if bands and cell.top - bands[-1][0].top <= 2.5:
            bands[-1].append(cell)
        else:
            bands.append([cell])
    return tuple(cell for band in bands for cell in sorted(band, key=lambda item: item.x0))


@dataclass(frozen=True, slots=True)
class PdfRowAST:
    cells: tuple[PdfCellAST, ...]
    top: float
    bbox: tuple[float, float, float, float]

    @property
    def text(self) -> str:
        return " ".join(cell.text for cell in _reading_order_cells(self.cells))


def _positioned_pdf_rows(words) -> tuple[PdfRowAST, ...]:
    """Shared positioned row reader for initial extraction and local repair."""
    bands: list[list[PdfCellAST]] = []
    for word in words:
        cell = PdfCellAST(str(word.get("text", "")).strip(), float(word.get("x0", 0)),
            float(word.get("top", 0)), float(word.get("x1", 0)), float(word.get("bottom", 0)))
        if cell.text:
            if bands and abs(cell.top - bands[-1][0].top) <= 2.5:
                bands[-1].append(cell)
            else:
                bands.append([cell])
    return tuple(PdfRowAST(tuple(sorted(cells, key=lambda cell: cell.x0)), min(cell.top for cell in cells),
        (min(cell.x0 for cell in cells), min(cell.top for cell in cells),
         max(cell.x1 for cell in cells), max(cell.bottom for cell in cells))) for cells in bands if cells)


def _ratio_unit_source(row, cell, table_rows, *, summary=False):
    """Bind a ratio to its cell/label, never a different numeric column."""
    if _parse_decimal(cell.text) and _parse_decimal(cell.text)[1]:
        return "explicit_percent", Decimal("0.01"), cell.text
    labels = " ".join(item.text for item in row.cells if _parse_number(item.text) is None)
    declarations = [labels]
    if summary:
        declarations.extend(item.text for item in table_rows[:12]
            if re.search(r"(?:单位|unit)\s*[:：]", item.text, re.I))
    for text in declarations:
        if any(marker in text for marker in ("%", "％", "百分比")):
            return "explicit_percent", Decimal("0.01"), text
        if re.search(r"[（(]\s*(?:比率|比例|ratio|decimal\s+ratio)\s*[）)]|(?:单位|unit)\s*[:：]\s*(?:比率|ratio)\b", text, re.I):
            return "explicit_ratio", Decimal("1"), text
    return "unknown", Decimal("1"), ""


@dataclass(frozen=True, slots=True)
class PdfTableAST:
    page: int
    statement: str
    scope: str
    currency: str
    unit_scale: float
    periods: tuple[str, ...]
    rows: tuple[PdfRowAST, ...]
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class PdfTableContext:
    """Page-scoped identity for a statement table.

    ``inherited_pages`` is deliberately bounded.  A PDF continuation header is
    useful for one or two adjacent pages, but carrying it through an appendix
    is a common source of silently mis-scoped facts.
    """

    statement: str
    scope: str
    multiplier: float
    currency: str
    unit_explicit: bool
    periods: tuple["_PeriodColumn", ...]
    last_page: int
    inherited_pages: int = 0
    unit_provenance: str = "unknown"
    unit_evidence: str = ""
    unit_evidence_page: int | None = None
    statement_evidence: str = ""
    statement_evidence_page: int | None = None
    statement_evidence_bbox: tuple[float, float, float, float] | None = None


def _checkpoint_context_to_dict(context: PdfTableContext | None) -> dict[str, Any]:
    if context is None:
        return {}
    return {
        "statement": context.statement,
        "scope": context.scope,
        "multiplier": context.multiplier,
        "currency": context.currency,
        "unit_explicit": context.unit_explicit,
        "periods": [
            {
                "year": item.year,
                "center": item.center,
                "left": item.left,
                "right": item.right,
                "currency": item.currency,
                "unit_scale": item.unit_scale,
                "header": item.header,
                "unit_excerpt": item.unit_excerpt,
                "unit_bbox": item.unit_bbox,
                "unit_page": item.unit_page,
                "relative_period": item.relative_period,
                "period_start": item.period_start,
                "period_end": item.period_end,
                "period_basis": item.period_basis,
            }
            for item in context.periods
        ],
        "last_page": context.last_page,
        "inherited_pages": context.inherited_pages,
        "unit_provenance": context.unit_provenance,
        "unit_evidence": context.unit_evidence,
        "unit_evidence_page": context.unit_evidence_page,
        "statement_evidence": context.statement_evidence,
        "statement_evidence_page": context.statement_evidence_page,
        "statement_evidence_bbox": context.statement_evidence_bbox,
    }


def _checkpoint_context_from_dict(value: dict[str, Any] | None) -> PdfTableContext | None:
    if not isinstance(value, dict) or not value.get("statement"):
        return None
    try:
        periods = tuple(
            _PeriodColumn(
                int(item["year"]), float(item["center"]), float(item["left"]),
                float(item["right"]), str(item.get("currency", "")),
                float(item.get("unit_scale", 1.0)),
                str(item.get("header", "")),
                str(item.get("unit_excerpt", "")),
                tuple(float(part) for part in item["unit_bbox"]) if item.get("unit_bbox") is not None else None,
                int(item["unit_page"]) if item.get("unit_page") is not None else None,
                str(item.get("relative_period", "")),
                str(item.get("period_start", "")),
                str(item.get("period_end", "")),
                str(item.get("period_basis", "")),
            )
            for item in value.get("periods", ())
        )
        return PdfTableContext(
            str(value["statement"]), str(value.get("scope", "consolidated")),
            float(value.get("multiplier", 1.0)), str(value.get("currency", "")),
            bool(value.get("unit_explicit", False)), periods,
            int(value.get("last_page", 0)), int(value.get("inherited_pages", 0)),
            str(value.get("unit_provenance", "unknown")),
            str(value.get("unit_evidence", "")),
            int(value["unit_evidence_page"])
            if value.get("unit_evidence_page") is not None else None,
            str(value.get("statement_evidence", "")),
            int(value["statement_evidence_page"])
            if value.get("statement_evidence_page") is not None else None,
            tuple(float(item) for item in value["statement_evidence_bbox"])
            if value.get("statement_evidence_bbox") is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PdfPageSection:
    context: PdfTableContext
    rows: tuple[PdfRowAST, ...]
    summary: bool = False
    inherited: bool = False


@dataclass(frozen=True, slots=True)
class FilingManifest:
    document_id: str
    accession_number: str
    source_url: str
    primary_document: str
    form_type: str
    fiscal_period: str
    period_end: str
    revision: str
    supersedes_document_id: str
    content_hash: str
    # Discovery/provider identity is retained when a parser later promotes a
    # provisional date from statement evidence.
    original_period_end: str = ""
    original_fiscal_period: str = ""
    original_revision: str = ""


@dataclass(frozen=True, slots=True)
class _CheckpointPdfWindowWorker:
    """Pickle-safe worker for the durable PDF-window scheduler."""

    local_path: str
    company: Company
    filing: FilingDocument
    manifest: FilingManifest
    compatibility_rules: FinancialRulesSnapshot
    document_units: DocumentUnitResolution = DocumentUnitResolution()

    def __call__(self, window: Any, initial_context: dict[str, Any] | None = None):
        engine = FinancialIngestionEngine(
            max_workers=1,
            compatibility_rules=self.compatibility_rules,
        )
        facts, refs = engine._parse_pdf_ast(
            self.local_path,
            self.company,
            self.filing,
            self.manifest,
            candidate_pages=frozenset(window.pages),
            index_precomputed=True,
            compatibility_rules=self.compatibility_rules,
            initial_context=initial_context,
            document_units=self.document_units,
        )
        return facts, refs, _checkpoint_context_to_dict(engine._last_pdf_context)


@dataclass(frozen=True, slots=True)
class FinancialDataset:
    accepted_facts: tuple[FinancialFact, ...]
    evidence: tuple[EvidenceRef, ...]
    manifest: tuple[FilingManifest, ...]
    validation: FinancialValidation
    diagnostics: tuple[str, ...] = ()
    group_validations: tuple[FinancialGroupValidation, ...] = ()

    @property
    def status(self) -> ValidationStatus:
        return self.validation.status


_LABELS: dict[str, tuple[str, ...]] = {
    "comprehensive_income": FINANCIAL_LABEL_ALIASES["comprehensive_income"],
    "parent_comprehensive_income": FINANCIAL_LABEL_ALIASES["parent_comprehensive_income"],
    "minority_interest_comprehensive_income": FINANCIAL_LABEL_ALIASES["minority_interest_comprehensive_income"],
    "revenue": ("营业收入", "营业收入合计", "revenue", "revenues", "total revenue", "operating revenue"),
    "total_operating_revenue": ("营业总收入", "營業總收入", "total operating revenue"),
    "interest_revenue": FINANCIAL_LABEL_ALIASES["interest_revenue"],
    "finance_interest_income": FINANCIAL_LABEL_ALIASES["finance_interest_income"],
    "net_income": FINANCIAL_LABEL_ALIASES["net_income"],
    "parent_net_income": FINANCIAL_LABEL_ALIASES["parent_net_income"],
    "minority_interest_profit": FINANCIAL_LABEL_ALIASES["minority_interest_profit"],
    "ordinary_shareholder_net_income": FINANCIAL_LABEL_ALIASES["ordinary_shareholder_net_income"],
    "operating_cash_flow": FINANCIAL_LABEL_ALIASES["operating_cash_flow"],
    "operating_cash_flow_before_tax": (
        "cash flows from (used in) operations",
        "cash generated from operations",
        "cash (used in)/generated from operations",
        "cash generated from/(used in) operations",
        "经营活动现金流量（税前）",
        "经营活动所得现金（税前）", "经营活动所用现金（税前）",
        "經營活動所得現金（稅前）", "經營活動所用現金（稅前）",
    ),
    "assets": ("资产总计", "资产合计", "total assets"),
    "liabilities": ("负债合计", "负债总计", "total liabilities"),
    "equity": (
        "归属于母公司所有者权益合计", "归属于母公司所有者权益（或股东权益）合计", "归属于上市公司股东的所有者权益",
        "归属于母公司所有者权益",
        "归属于母公司股东权益合计", "equity attributable to owners",
        "equity attributable to equity holders of the company",
        "total equity attributable to the parent company",
        "total shareholders' equity attributable to the parent company",
        "total equity attributable to shareholders of the company",
    ),
    "total_equity": FINANCIAL_LABEL_ALIASES["total_equity"],
    "mezzanine_equity": ("mezzanine equity",),
    "minority_interest_equity": (
        "noncontrolling interests", "non-controlling interests", "minority interests",
        "少数股东权益", "少數股東權益", "非控股权益", "非控股權益",
    ),
    "reported_roe": ("加权平均净资产收益率", "weighted average return on equity"),
    "profit_before_tax": FINANCIAL_LABEL_ALIASES["profit_before_tax"],
    "profit_after_tax": ("净利润", "profit after tax", "net profit"),
    "operating_income": FINANCIAL_LABEL_ALIASES["operating_income"],
    "capital_expenditure_ppe": (
        "purchase of property, plant and equipment",
        "purchases of property, plant and equipment",
        "payments to acquire property, plant and equipment",
        "payments for additions to property, plant and equipment",
        "purchase of/prepayments for property, plant and equipment, construction in progress",
        "purchase of / prepayments for property, plant and equipment, construction in progress",
        "购建固定资产支付的现金", "购建固定资产所支付的现金",
        "購建固定資產支付的現金", "购置物业、厂房及设备款项",
        "購置物業、廠房及設備款項", "購置物業、機器及設備",
    ),
    "capital_expenditure_intangible": (
        "purchase of intangible assets", "purchases of intangible assets",
        "payments to acquire intangible assets",
        "purchase of/prepayments for intangible assets",
        "purchase of / prepayments for intangible assets",
        "购建无形资产支付的现金", "购建无形资产所支付的现金",
        "購建無形資產支付的現金", "购置无形资产", "購置無形資產",
    ),
    "capital_expenditure": tuple(
        label for label in FINANCIAL_LABEL_ALIASES["capital_expenditure"]
        if label not in {
            "购建固定资产支付的现金",
            "購置物業、廠房及設備款項", "購置物業、機器及設備",
            "购置物业、厂房及设备款项", "購置無形資產", "购置无形资产",
            "purchase of property, plant and equipment",
            "purchase of/prepayments for property, plant and equipment, construction in progress",
            "purchase of/prepayments for intangible assets",
            "purchase of / prepayments for property, plant and equipment, construction in progress",
            "purchase of / prepayments for intangible assets",
        }
    ),
    **CAPITAL_ALLOCATION_PAYMENT_ALIASES,
    # Optional cash-return facts. These are deliberately outside _CORE: their
    # absence must not invalidate an otherwise usable financial statement.
    "share_repurchase": (
        "repurchase of ordinary shares", "repurchase of own shares",
        "purchase of treasury shares", "share buyback",
        "回购本公司股份", "回购股份", "购回股份", "购买库存股",
    ),
    "shareholder_dividends_paid": (
        "dividends paid to shareholders", "dividends paid to equity holders",
        "dividends paid to owners", "向股东支付股利", "向股东支付股息",
        "向股东支付分红", "向所有者支付股利",
    ),
    "gross_profit": FINANCIAL_LABEL_ALIASES["gross_profit"],
    "cost_of_revenue": FINANCIAL_LABEL_ALIASES["cost_of_revenue"],
}
_CORE = {"revenue", "net_income", "assets", "liabilities", "equity", "operating_cash_flow"}
_STATEMENT_MARKERS = STATEMENT_TITLE_MARKERS
_NUM = re.compile(r"(?:\(\s*[+-]?[\d,]+(?:\.\d+)?\s*\)|[+-]?[\d,]+(?:\.\d+)?)")


def _rules_aliases(rules: FinancialRulesSnapshot | None, kind: str, key: str) -> tuple[str, ...]:
    return rules.aliases(kind, key) if rules is not None else ()


def _labels_for_rules(rules: FinancialRulesSnapshot | None) -> dict[str, tuple[str, ...]]:
    labels = {key: tuple(values) for key, values in _LABELS.items()}
    if rules is None:
        return labels
    for canonical, aliases in rules.taxonomy_aliases:
        key = canonical.casefold()
        if key not in labels:
            key = next((name for name, values in labels.items() if any(_label_compact(canonical) == _label_compact(value) for value in values)), "")
        if key and key in labels:
            labels[key] = tuple(dict.fromkeys((*labels[key], *aliases)))
    return labels


def _statement_markers_for_rules(rules: FinancialRulesSnapshot | None) -> dict[str, tuple[str, ...]]:
    markers = {key: tuple(values) for key, values in _STATEMENT_MARKERS.items()}
    if rules is None:
        return markers
    aliases = {"consolidated_income": "income_statement", "income": "income_statement",
               "consolidated_balance": "balance_sheet", "balance": "balance_sheet",
               "consolidated_cash_flow": "cash_flow", "cashflow": "cash_flow"}
    for canonical, values in rules.title_aliases:
        key = aliases.get(canonical.casefold(), canonical.casefold())
        if key in markers:
            markers[key] = tuple(dict.fromkeys((*markers[key], *values)))
    return markers


def _scope_from_text(text: str, rules: FinancialRulesSnapshot | None = None) -> str:
    folded = text.casefold()
    consolidated = ("合并", "consolidated", *_rules_aliases(rules, "scope_aliases", "consolidated"))
    parent = ("母公司", "parent", "company only", *_rules_aliases(rules, "scope_aliases", "parent"))
    if any(token.casefold() in folded for token in consolidated):
        return "consolidated"
    if any(token.casefold() in folded for token in parent):
        return "parent"
    return "parent"


def _manifest_for(filing: FilingDocument) -> FilingManifest | None:
    title = filing.primary_document or ""
    folded = title.casefold()
    original_period_end = str(filing.period_end or "")
    original_fiscal_period = str(filing.fiscal_period or "")
    original_revision = str(filing.revision or "")
    if original_revision in {"period_end_provisional", "period_end_verified"}:
        original_revision = "original"
    # H1 must precede annual because some issuers use 年度报告 in a long title.
    if any(x in title for x in ("半年度报告", "中期报告")) or "interim report" in folded:
        period, form, end_suffix = "H1", "INTERIM_REPORT", "06-30"
    elif any(x in title for x in ("第一季度报告",)) or "first quarter" in folded:
        period, form, end_suffix = "Q1", "QUARTERLY_REPORT", "03-31"
    elif any(x in title for x in ("第三季度报告",)) or "third quarter" in folded:
        period, form, end_suffix = "Q3", "QUARTERLY_REPORT", "09-30"
    elif any(x in title for x in ("年度报告", "年报")) or "annual report" in folded:
        period, form, end_suffix = "FY", "ANNUAL_REPORT", "12-31"
    else:
        period = (filing.fiscal_period or "").upper()
        if period not in {"FY", "H1", "Q1", "Q3"}:
            return None
        form = filing.form_type or ("ANNUAL_REPORT" if period == "FY" else "INTERIM_REPORT")
        end_suffix = filing.period_end[5:10] if len(filing.period_end) >= 10 else "12-31"
    year = filing.period_end[:4]
    # Filing discovery may provide a legitimate non-calendar fiscal end
    # (e.g. Alibaba FY2026 ended 2026-03-31). Preserve it rather than forcing
    # every annual title to December 31.
    try:
        supplied_end = date.fromisoformat(filing.period_end[:10]).isoformat()
    except (TypeError, ValueError):
        supplied_end = ""
    period_end = supplied_end or f"{year}-{end_suffix}"
    identity = DisclosureIdentityResolver().resolve(
        title=filing.primary_document,
        filed_at=filing.filed_at,
        revision=original_revision,
        provider_metadata={
            "period_end": filing.period_end,
            "fiscal_period": filing.fiscal_period,
            "revision": filing.revision,
        },
    )
    if identity.fiscal_period:
        period = identity.fiscal_period
    if identity.period_end and not identity.provisional:
        period_end = identity.period_end
    revision = identity.revision
    # PRC A-share annual reports use the statutory calendar accounting year.
    # When the official discovery record supplies 31 December for the same
    # explicit report year in the title, those independent signals resolve the
    # disclosure period before PDF/vision extraction starts.  Keep this rule
    # market-scoped: HK/US and other non-calendar issuers must still prove their
    # period from an explicit date in the source document.
    title_year_match = re.search(
        r"\b(20\d{2})\b|(?<!\d)(20\d{2})年", title,
    )
    title_year = (
        next((group for group in title_year_match.groups() if group), "")
        if title_year_match is not None else ""
    )
    filing_identity = str(filing.company_cik or "").upper()
    calendar_year_a_share = (
        identity.provisional
        and period == "FY"
        and supplied_end.endswith("-12-31")
        and title_year == supplied_end[:4]
        and (filing_identity == "CN_A" or filing_identity.startswith("CN_A:"))
    )
    if calendar_year_a_share:
        period_end = supplied_end
        # Revision describes the filing edition, not how period identity was
        # established.  Do not make a normal annual report look amended.
        revision = original_revision or identity.revision or "original"
    elif identity.provisional:
        period_end = ""
        revision = "period_end_provisional"
    supplied_filed = str(filing.filed_at or "")[:10]
    supplied_period_end = str(filing.period_end or "")[:10]
    if (
        len(supplied_filed) == 10
        and len(supplied_period_end) == 10
        and supplied_period_end > supplied_filed
    ):
        # Discovery metadata can contain an announcement year or an
        # unobserved 12/31 placeholder.  It is unsafe as a parser/compiler
        # period until the statement itself supplies a date.
        period_end = ""
        revision = "period_end_provisional"
    return FilingManifest(
        filing.document_id, filing.accession_number, filing.source_url,
        filing.primary_document, form, period, period_end,
        revision, filing.supersedes_document_id, filing.content_hash,
        original_period_end, original_fiscal_period, original_revision,
    )


def _manifest_from_observed_pages(
    manifest: FilingManifest,
    filing: FilingDocument,
    page_texts: Sequence[str],
    *,
    declarations: Sequence[StatementPeriodDeclaration] | None = None,
) -> FilingManifest:
    """Resolve a provisional metadata date from bounded statement page text.

    Discovery is allowed to retain an annual-looking placeholder (for example
    an H1 filing carrying ``12/31``).  The parser must not turn that placeholder
    into a fact, but it can safely resolve it from dates observed on the
    candidate pages.  Dates remain constrained by ``filed_at`` in the shared
    resolver; absent a source-supported date the manifest stays provisional.
    """
    if manifest.revision == "period_header_conflict":
        return manifest
    expected_months = {"FY": 12, "H1": 6, "Q1": 3, "Q3": 9}.get(manifest.fiscal_period)
    observed = [item.period_end for item in (
        declarations if declarations is not None else statement_period_declarations(page_texts)
    ) if item.duration_months is None or item.duration_months == expected_months]
    if not observed:
        return manifest
    report_year = _title_report_year(filing.primary_document)
    current_dates = {value for value in observed if report_year is None or int(value[:4]) == report_year}
    if report_year is None and current_dates:
        latest_year = max(int(value[:4]) for value in current_dates)
        current_dates = {value for value in current_dates if int(value[:4]) == latest_year}
    if len(current_dates) > 1:
        # Multiple formal current-period dates are a source conflict, not a
        # licence to choose the latest release/audit/statement date.
        return replace(manifest, period_end="", revision="period_header_conflict")
    title_identity = DisclosureIdentityResolver().resolve(
        title=filing.primary_document, filed_at=filing.filed_at,
        provider_metadata={"fiscal_period": manifest.fiscal_period},
    )
    if title_identity.period_end and current_dates and title_identity.period_end not in current_dates:
        return replace(manifest, period_end="", revision="period_header_conflict")
    identity = DisclosureIdentityResolver().resolve(
        title=filing.primary_document,
        filed_at=filing.filed_at,
        provider_metadata={
            "period_end": "",
            "fiscal_period": manifest.fiscal_period,
            "revision": manifest.revision,
        },
        observed_statement_dates=tuple(observed),
    )
    if identity.period_end is None or identity.provisional:
        return manifest
    # The provisional marker belongs to parsing, not the physical edition.
    # Restore only the edition actually captured from discovery; never guess
    # an old historical marker into an original or a corrected edition.
    resolved_revision = manifest.original_revision or manifest.revision
    return replace(
        manifest,
        fiscal_period=identity.fiscal_period or manifest.fiscal_period,
        period_end=identity.period_end,
        revision=resolved_revision,
    )


def _unit_scale(text: str, rules: FinancialRulesSnapshot | None = None) -> tuple[float, str]:
    """Parse explicit currency and unit markers from one table context."""
    normalized = re.sub(r"\s+", "", text.casefold()).translate(str.maketrans({"’": "'", "‘": "'", "′": "'", "＇": "'", "ʼ": "'"}))
    custom_currency = ""
    custom_scale = 1.0
    scale_names = {"yuan": 1.0, "元": 1.0, "thousand": 1_000.0, "千元": 1_000.0,
                   "million": 1_000_000.0, "百万元": 1_000_000.0, "ten_thousand": 10_000.0, "万元": 10_000.0}
    if rules is not None:
        for canonical, aliases in rules.unit_aliases:
            if any(str(alias).casefold().replace(" ", "") in normalized for alias in aliases):
                key = canonical.casefold().replace(" ", "")
                if key in {"cny", "rmb", "人民币"}: custom_currency = "CNY"
                elif key in {"usd", "美元"}: custom_currency = "USD"
                elif key in {"hkd", "港元", "港币"}: custom_currency = "HKD"
                for name, scale in scale_names.items():
                    if key == name:
                        custom_scale = scale
    if any(token in normalized for token in ("hk$", "hk£", "hkd", "港元", "港币")):
        currency = "HKD"
    elif any(token in normalized for token in ("usd", "us$", "美元")):
        currency = "USD"
    elif any(token in normalized for token in ("cny", "rmb", "人民币", "元")):
        currency = "CNY"
    else:
        currency = ""
    currency = custom_currency or currency
    if custom_scale != 1.0:
        return custom_scale, currency or "CNY"
    # A report can describe the document broadly as "thousand yuan" while a
    # following formal statement declares its own displayed unit as yuan.  A
    # table-header declaration is the narrowest and most authoritative unit
    # context; use the last such declaration rather than a page-wide token.
    unit_headers = list(re.finditer(
        r"(?:单位|unit)[:：]?"
        r"(?:人民币|rmb|cny|美元|usd|港元|hkd|港币|港幣)?"
        r"(百万元|千元|万元|元|rmb'000|rmbmillion|million|thousand)",
        normalized,
    ))
    if unit_headers:
        header = unit_headers[-1]
        token = header.group(1)
        header_text = header.group(0)
        header_currency = (
            "HKD" if any(value in header_text for value in ("港元", "港币", "港幣", "hkd"))
            else "USD" if any(value in header_text for value in ("美元", "usd"))
            else "CNY" if any(value in header_text for value in ("人民币", "rmb", "cny"))
            else currency
        )
        return {
            "元": 1.0,
            "千元": 1_000.0,
            "万元": 10_000.0,
            "百万元": 1_000_000.0,
            "rmb'000": 1_000.0,
            "rmbmillion": 1_000_000.0,
            "million": 1_000_000.0,
            "thousand": 1_000.0,
        }[token], header_currency
    if any(token in normalized for token in ("千元", "千人民币", "rmb'000", "inthousands", "thousand")):
        return 1_000.0, currency or "CNY"
    if any(token in normalized for token in ("百万元", "rmbmillion", "inmillions", "million")):
        return 1_000_000.0, currency
    if any(token in normalized for token in ("万元", "万人民币", "rmbten-thousand")):
        return 10_000.0, currency or "CNY"
    # Older PDF text extraction can expose GBK mojibake. Keep fallbacks
    # bounded to explicit Chinese unit markers; never infer a scale from
    # unrelated prose or a bare English ``million`` heading.
    if not currency and any(token in normalized for token in ("千元", "千人民币")):
        return 1_000.0, "CNY"
    if not currency and any(token in normalized for token in ("万元", "万人民币")):
        return 10_000.0, "CNY"
    return 1.0, currency


def _explicit_currencies(text: str, rules: FinancialRulesSnapshot | None = None) -> frozenset[str]:
    """Return currencies explicitly named in a bounded table context.

    Official HKEX statements commonly append a current-year US-dollar
    convenience-translation column to RMB accounts.  Treating the whole page
    as USD in that case splits one audited statement into incompatible
    currency groups.
    """
    normalized = re.sub(r"\s+", "", text.casefold()).translate(
        str.maketrans({"’": "'", "‘": "'", "′": "'", "＇": "'", "ʼ": "'"})
    )
    currencies: set[str] = set()
    if any(token in normalized for token in ("hk$", "hkd", "港元", "港币", "港幣")):
        currencies.add("HKD")
    if any(token in normalized for token in ("usd", "us$", "美元")):
        currencies.add("USD")
    if any(token in normalized for token in ("cny", "rmb", "人民币", "人民幣")):
        currencies.add("CNY")
    if rules is not None:
        for canonical, aliases in rules.unit_aliases:
            if any(str(alias).casefold().replace(" ", "") in normalized for alias in aliases):
                key = canonical.casefold()
                if key in {"cny", "rmb", "人民币"}: currencies.add("CNY")
                elif key in {"usd", "美元"}: currencies.add("USD")
                elif key in {"hkd", "港元", "港币"}: currencies.add("HKD")
    return frozenset(currencies)


def _statement_context(text: str, rules: FinancialRulesSnapshot | None = None) -> tuple[str, str] | None:
    return classify_statement_title(
        text, markers=_statement_markers_for_rules(rules),
        consolidated_aliases=_rules_aliases(rules, "scope_aliases", "consolidated"),
        parent_aliases=_rules_aliases(rules, "scope_aliases", "parent"),
    )


def _period_start(manifest: FilingManifest) -> str | None:
    if manifest.fiscal_period == "FY":
        try:
            end = date.fromisoformat(manifest.period_end[:10])
            try:
                previous = end.replace(year=end.year - 1)
            except ValueError:
                # A leap-day year-end has no same-day predecessor; retaining
                # the fiscal boundary at Feb 28 keeps the following period
                # start deterministic rather than falling back to Jan 1.
                previous = end.replace(year=end.year - 1, day=28)
            return (previous + timedelta(days=1)).isoformat()
        except ValueError:
            return f"{manifest.period_end[:4]}-01-01"
    if manifest.fiscal_period == "H1":
        return f"{manifest.period_end[:4]}-01-01"
    if manifest.fiscal_period == "Q1":
        return f"{manifest.period_end[:4]}-01-01"
    if manifest.fiscal_period == "Q3":
        return f"{manifest.period_end[:4]}-01-01"
    return None


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


def _period_headers(rows: Sequence[PdfRowAST]) -> tuple[str, ...]:
    for row in rows:
        text = row.text
        years = tuple(re.findall(r"20\d{2}(?:年度|年)?", text))
        if years:
            return years
    return ()


@dataclass(frozen=True, slots=True)
class _PeriodColumn:
    year: int
    center: float
    left: float
    right: float
    currency: str = ""
    unit_scale: float = 1.0
    header: str = ""
    unit_excerpt: str = ""
    unit_bbox: tuple[float, float, float, float] | None = None
    unit_page: int | None = None
    relative_period: str = ""
    period_start: str = ""
    period_end: str = ""
    period_basis: str = ""


@dataclass(frozen=True, slots=True)
class _ComparisonPeriodBinding:
    fiscal_year: int
    fiscal_period: str
    period_start: str | None
    period_end: str
    basis: str
    resolved: bool


_ENGLISH_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _period_header_bounds(text: str) -> tuple[str, str, str]:
    """Return exact header interval/end dates without borrowing manifest years."""
    value = re.sub(r"\s+", " ", str(text).strip())

    def iso(year, month, day) -> str:
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except (TypeError, ValueError):
            return ""

    # Explicit Chinese start/end ranges, including a repeated year at the end.
    range_pattern = re.compile(
        r"(?P<sy>20\d{2})年(?P<sm>\d{1,2})月(?P<sd>\d{1,2})日?\s*(?:至|到|[-—–])\s*"
        r"(?:(?P<ey>20\d{2})年)?(?P<em>\d{1,2})月(?P<ed>\d{1,2})日?"
    )
    match = range_pattern.search(value)
    if match:
        start = iso(match.group("sy"), match.group("sm"), match.group("sd"))
        end = iso(match.group("ey") or match.group("sy"), match.group("em"), match.group("ed"))
        if start and end and start <= end:
            return start, end, "explicit_header_interval"

    # Common compact interim header, for example 2025年1-6月 / 2025年1—9月.
    month_range = re.search(
        r"(?P<year>20\d{2})年(?P<start>\d{1,2})\s*[-—–至到]\s*(?P<end>\d{1,2})月",
        value,
    )
    if month_range:
        year, first_month, last_month = map(int, month_range.groups())
        try:
            start = date(year, first_month, 1).isoformat()
            end_day = calendar.monthrange(year, last_month)[1]
            end = date(year, last_month, end_day).isoformat()
        except (ValueError, TypeError):
            pass
        else:
            if first_month <= last_month:
                return start, end, "explicit_header_interval"

    dates: list[str] = []
    for pattern in (
        re.compile(r"(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"),
        re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"),
    ):
        for item in pattern.finditer(value):
            parsed = iso(item.group("year"), item.group("month"), item.group("day"))
            if parsed:
                dates.append(parsed)
    english = re.compile(
        r"(?:(?P<day>\d{1,2})\s+(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s*,?\s*(?P<year>20\d{2})|"
        r"(?P<month2>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<day2>\d{1,2}),?\s*(?P<year2>20\d{2}))",
        re.I,
    )
    for item in english.finditer(value):
        month_name = item.group("month") or item.group("month2")
        day = item.group("day") or item.group("day2")
        year = item.group("year") or item.group("year2")
        parsed = iso(year, _ENGLISH_MONTHS[month_name.casefold()], day)
        if parsed:
            dates.append(parsed)
    if len(dates) >= 2:
        return min(dates), max(dates), "explicit_header_interval"

    # Numeric day/month/year headers are accepted only in a full date shape.
    numeric = re.search(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})(?!\d)", value)
    if numeric:
        day, month, year = numeric.groups()
        parsed = iso(year, month, day)
        if parsed:
            dates.append(parsed)
    if dates:
        return "", dates[-1], "explicit_header_end_date"
    return "", "", ""


def _effective_period_year(text: str) -> int | None:
    """Map a balance-sheet date header to its fiscal column year.

    Opening-date headers (January 1) belong to the preceding fiscal year,
    while a year-end date belongs to the named year.  When a cell contains a
    range, the chronologically latest date is the period end.  This helper is
    intentionally limited to complete dates so plain ``2025``/``2024`` and
    one-year statement titles retain the existing header rules.
    """
    normalized = re.sub(r"\s+", " ", text.casefold())
    dates: list[tuple[int, int, int]] = []
    chinese = re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?")
    for match in chinese.finditer(normalized):
        dates.append((int(match.group("year")), int(match.group("month")), int(match.group("day"))))
    day_month = re.compile(
        r"(?P<day>\d{1,2})\s+(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s*,?\s*(?P<year>20\d{2})",
        re.IGNORECASE,
    )
    for match in day_month.finditer(normalized):
        dates.append((int(match.group("year")), _ENGLISH_MONTHS[match.group("month").casefold()], int(match.group("day"))))
    month_day = re.compile(
        r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(?P<day>\d{1,2}),?\s*(?P<year>20\d{2})",
        re.IGNORECASE,
    )
    for match in month_day.finditer(normalized):
        dates.append((int(match.group("year")), _ENGLISH_MONTHS[match.group("month").casefold()], int(match.group("day"))))
    iso_date = re.fullmatch(r"(?:as\s+at\s+)?(20\d{2})-(\d{1,2})-(\d{1,2})", normalized)
    if iso_date:
        year, month, day = map(int, iso_date.groups())
        try:
            date(year, month, day)
        except ValueError:
            return None
        dates.append((year, month, day))
    numeric_header = re.fullmatch(r"(?:as\s+at\s+)?(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})", normalized)
    if numeric_header:
        day, month, year = map(int, numeric_header.groups())
        try:
            date(year, month, day)
        except ValueError:
            return None
        dates.append((year, month, day))
    if not dates:
        return None
    year, month, day = max(dates)
    return year - 1 if len(dates) == 1 and month == 1 and day == 1 else year


def _period_header_cells(cells: Sequence[PdfCellAST]) -> tuple[PdfCellAST, ...]:
    """Bind a complete printed English date before reducing it to a year.

    pdfplumber returns one cell per word. Reducing those cells independently
    loses the January-1 opening-date semantics and merges two 2023 columns.
    Only adjacent fragments forming one exact date are joined; bare year
    lists, note numbers and amounts remain separate cells.
    """
    ordered = sorted(cells, key=lambda cell: cell.x0)
    month = "(?:" + "|".join(_ENGLISH_MONTHS) + ")"
    date_pattern = re.compile(
        rf"(?:\d{{1,2}}\s+{month}\s*,?\s*20\d{{2}}|{month}\s+\d{{1,2}},?\s+20\d{{2}})",
        re.I,
    )
    result: list[PdfCellAST] = []
    index = 0
    while index < len(ordered):
        matched = False
        for size in range(min(4, len(ordered) - index), 0, -1):
            parts = ordered[index:index + size]
            text = " ".join(part.text.strip() for part in parts)
            if not date_pattern.fullmatch(text) or any(
                right.x0 - left.x1 > 18 for left, right in zip(parts, parts[1:])
            ):
                continue
            result.append(PdfCellAST(
                text, parts[0].x0, min(part.top for part in parts),
                max(part.x1 for part in parts), max(part.bottom for part in parts),
            ))
            index += size
            matched = True
            break
        if not matched:
            result.append(ordered[index])
            index += 1
    return tuple(result)


def _period_columns(
    rows: Sequence[PdfRowAST], preferred_currency: str = "", rules: FinancialRulesSnapshot | None = None,
    *, source_page: int | None = None,
) -> tuple[_PeriodColumn, ...]:
    """Choose visual year columns before any one-year title/date row."""
    dual: list[tuple[int, float]] | None = None
    fallback: list[tuple[int, float]] | None = None
    headers_by_year: dict[int, str] = {}
    header_rank_by_year: dict[int, int] = {}
    period_bounds_by_year: dict[int, tuple[str, str, str]] = {}
    all_text = " ".join(row.text for row in rows)
    global_scale, _ = _unit_scale(all_text, rules)
    unit_cells: list[tuple[float, str, PdfCellAST]] = []
    relative_headers: dict[str, tuple[float, str]] = {}
    relative_names = {
        'current': {'本期发生额', '本期金额', '本期', '本年', '本年发生额', '期末余额', 'currentperiod', 'currentyear'},
        'prior': {'上期发生额', '上期金额', '上期', '上年', '上年发生额', '期初余额', '上年年末余额', 'previousperiod', 'previousyear'},
    }
    for row_index, row in enumerate(rows):
        if _is_narrative_date_header(row.text):
            continue
        for cell in row.cells:
            normalized = re.sub(r"\s+", "", cell.text.casefold()).translate(str.maketrans({"’": "'", "‘": "'", "＇": "'"}))
            for role, names in relative_names.items():
                if normalized in names:
                    relative_headers.setdefault(role, ((cell.x0 + cell.x1) / 2, cell.text.strip()))
            if normalized in {"rmb", "cny", "rmb'000", "rmbmillion", "us$", "usd", "hkd", "hk$"}:
                _, currency = _unit_scale(cell.text, rules)
                if currency:
                    unit_cells.append(((cell.x0 + cell.x1) / 2, currency, cell))
        by_year: dict[int, tuple[int, float, str]] = {}
        row_period_bounds: dict[int, tuple[str, str, str]] = {}
        for cell in _period_header_cells(row.cells):
            period_bounds = _period_header_bounds(cell.text)
            effective_year = _effective_period_year(cell.text)
            if effective_year is not None:
                rank = _period_revision_rank(cell.text)
                candidate = (rank, (cell.x0 + cell.x1) / 2, cell.text.strip())
                if effective_year not in by_year or candidate[0] > by_year[effective_year][0]:
                    by_year[effective_year] = candidate
                    row_period_bounds[effective_year] = period_bounds
                continue
            # A prose sentence containing a year is not a column header, even
            # when it occurs later on the same page as a genuine year table.
            match = re.fullmatch(
                r"\s*(20\d{2})(?:年(?:度|半年度|半年度报告|中期|第?[一二三四1234]季度|"
                r"[1-9]\d?\s*[-—–至]\s*[1-9]\d?\s*月)?)?\s*"
                r"(?:[（(](?:调整[前后]|重述[前后]|經重列|经重列|未重列|"
                r"restated|adjusted|revised|before\s*adjustment|unadjusted)[）)])?\s*",
                cell.text, flags=re.I,
            )
            if match:
                year = int(match[1])
                rank = _period_revision_rank(cell.text)
                candidate = (rank, (cell.x0 + cell.x1) / 2, cell.text.strip())
                if year not in by_year or candidate[0] > by_year[year][0]:
                    by_year[year] = candidate
                    row_period_bounds[year] = period_bounds
        for year, (rank, _center, header) in by_year.items():
            current_bounds = period_bounds_by_year.get(year, ("", "", ""))
            candidate_bounds = row_period_bounds.get(year, ("", "", ""))
            if (rank > header_rank_by_year.get(year, -1)
                    or (rank == header_rank_by_year.get(year, -1)
                        and not current_bounds[0] and not current_bounds[1]
                        and (candidate_bounds[0] or candidate_bounds[1]))):
                header_rank_by_year[year] = rank
                headers_by_year[year] = header
                period_bounds_by_year[year] = candidate_bounds
        candidates = sorted(
            ((year, value[1]) for year, value in by_year.items()),
            key=lambda item: item[1],
        )
        if len(candidates) >= 2 and dual is None:
            dual = candidates
            continue
        if not candidates:
            normalized_row = re.sub(r"\s+", "", row.text.casefold())
            current_markers = ("期末", "本期", "本年", "currentperiod", "currentyear")
            previous_markers = ("期初", "上期", "上年", "previousperiod", "previousyear")
            # Require a current/previous header pair.  A bare substring such as
            # ``Current assets`` is a line item, not a period column header.
            if (
                any(marker in normalized_row for marker in current_markers)
                and any(marker in normalized_row for marker in previous_markers)
                and not any(_parse_number(cell.text) is not None for cell in row.cells)
            ):
                candidates = [
                    (0, (cell.x0 + cell.x1) / 2)
                    for cell in row.cells
                    if re.sub(r"\s+", "", cell.text.casefold())
                    in {"期末", "期末余额", "本期", "本期金额", "本年", "current", "currentperiod", "currentyear"}
                ]
        if len(candidates) == 1 and fallback is None:
            normalized_row = row.text.casefold()
            if not any(marker in normalized_row for marker in ("for the year ended", "year ended", "as at", "年度", "年末")):
                if any(marker in normalized_row for marker in ("current", "期末", "本期", "本年")):
                    fallback = candidates
                else:
                    standalone_year = any(re.fullmatch(r"20\d{2}", cell.text.strip()) for cell in row.cells)
                    prior_text = rows[row_index - 1].text.casefold() if row_index else ""
                    header_anchor = any(marker in prior_text for marker in ("statement", "balance", "income", "cash", "financial position", "for the year ended", "year ended"))
                    if standalone_year and header_anchor:
                        fallback = candidates
    candidates = dual or fallback
    if dual is None and set(relative_headers) == {'current', 'prior'}:
        candidates = [(0, center) for _role, (center, _header) in
                      sorted(relative_headers.items(), key=lambda item: item[1][0])]
    header_text = re.sub(
        r"\s+", "", " ".join(row.text for row in rows[:12]).casefold()
    )
    split_semantic_header = (
        ("currentperiod" in header_text and "previousperiod" in header_text)
        or ("closingbalance" in header_text and "openingbalance" in header_text)
        or ("balanceattheend" in header_text and "beginning" in header_text and "oftheperiod" in header_text)
        or (
            header_text.count("balanceatthe") >= 2
            and "endoftheperiod" in header_text
            and "beginningoftheyear" in header_text
        )
        or (any(marker in header_text for marker in ("期末", "本期", "本年"))
            and any(marker in header_text for marker in ("期初", "上期", "上年")))
    )
    if not candidates and split_semantic_header:
        # Some official bilingual statements split semantic headers over two or
        # three visual rows (e.g. ``Balance at the end`` / ``of the period``).
        # Infer value columns only from repeated numeric-cell geometry inside
        # the already identified formal statement section.  The rightmost two
        # stable clusters are the current and comparison columns; note numbers
        # remain to their left and therefore cannot become values.
        clusters: list[list[float]] = []
        for row in rows:
            for cell in row.cells:
                if _parse_number(cell.text) is None:
                    continue
                center = (cell.x0 + cell.x1) / 2
                cluster = next(
                    (item for item in clusters if abs(sum(item) / len(item) - center) <= 24),
                    None,
                )
                if cluster is None:
                    clusters.append([center])
                else:
                    cluster.append(center)
        stable = sorted(
            ((len(item), sum(item) / len(item)) for item in clusters if len(item) >= 2),
            key=lambda item: item[1],
        )
        if len(stable) >= 2:
            candidates = [(0, center) for _, center in stable[-2:]]
    if not candidates:
        return ()
    centers = [center for _, center in candidates]
    result: list[_PeriodColumn] = []
    for index, (year, center) in enumerate(candidates):
        left = (centers[index - 1] + center) / 2 if index else center - 90
        right = (center + centers[index + 1]) / 2 if index + 1 < len(centers) else center + 90
        currency = ""
        unit_cell = None
        value_center = center
        in_interval = [item for item in unit_cells if left <= item[0] <= right]
        preferred = [item for item in in_interval if item[1] == preferred_currency]
        candidates_for_currency = preferred or in_interval or unit_cells
        if candidates_for_currency:
            unit_center, currency, unit_cell = min(
                candidates_for_currency, key=lambda item: abs(item[0] - center)
            )
            # A unit marker inside this period interval can disambiguate a
            # reporting-currency value from a convenience translation. A
            # table-wide marker outside the interval supplies only scale and
            # currency; it must not replace the year-header geometry.
            if preferred or in_interval:
                value_center = unit_center
        result.append(
            _PeriodColumn(
                year,
                value_center,
                left,
                right,
                currency,
                global_scale if currency else 1.0,
                headers_by_year.get(year, "") if year else next((header for position, header in
                    relative_headers.values() if abs(position - center) < 0.01), ""),
                unit_cell.text if unit_cell else "",
                (unit_cell.x0, unit_cell.top, unit_cell.x1, unit_cell.bottom) if unit_cell else None,
                source_page if unit_cell else None,
                next((role for role, (position, _header) in relative_headers.items()
                    if not year and abs(position - center) < 0.01), ""),
                period_bounds_by_year.get(year, ("", "", ""))[0],
                period_bounds_by_year.get(year, ("", "", ""))[1],
                period_bounds_by_year.get(year, ("", "", ""))[2],
            )
        )
    return tuple(result)


def _summary_roe_period_columns(
    rows: Sequence[PdfRowAST], manifest: FilingManifest,
) -> tuple[_PeriodColumn, ...]:
    """Bind summary ROE only when its current, prior, and change headers are explicit."""
    try:
        current_year = int(manifest.period_end[:4])
    except (TypeError, ValueError):
        return ()
    if current_year <= 1 or not manifest.fiscal_period or not _period_start(manifest):
        return ()

    headers: dict[str, list[PdfCellAST]] = {"current": [], "prior": [], "change": []}
    for row in rows[:12]:
        for cell in row.cells:
            normalized = re.sub(r"\s+", "", cell.text.casefold())
            if normalized in {"本报告期", "本期"}:
                headers["current"].append(cell)
            elif normalized == "上年同期":
                headers["prior"].append(cell)
            elif any(marker in normalized for marker in ("增减", "变动", "变化", "change", "increase")):
                headers["change"].append(cell)

    def header_center(cells: Sequence[PdfCellAST]) -> float | None:
        if not cells:
            return None
        centers = sorted((cell.x0 + cell.x1) / 2 for cell in cells)
        clusters: list[list[float]] = []
        for center in centers:
            if clusters and center - sum(clusters[-1]) / len(clusters[-1]) <= 18:
                clusters[-1].append(center)
            else:
                clusters.append([center])
        return sum(clusters[0]) / len(clusters[0]) if len(clusters) == 1 else None

    current_header = headers["current"][0] if len(headers["current"]) == 1 else None
    prior_header = headers["prior"][0] if len(headers["prior"]) == 1 else None
    current_center = header_center(headers["current"])
    prior_center = header_center(headers["prior"])
    change_center = header_center(headers["change"])
    if (
        current_header is None or prior_header is None
        or current_center is None or prior_center is None or change_center is None
        or not current_center < prior_center < change_center
    ):
        return ()

    split_prior_left = (current_center + prior_center) / 2
    split_prior_right = (prior_center + change_center) / 2
    return (
        _PeriodColumn(
            current_year, current_center, current_header.x0, split_prior_left,
            header=current_header.text.strip(), relative_period="current",
        ),
        _PeriodColumn(
            current_year - 1, prior_center, split_prior_left, split_prior_right,
            header=prior_header.text.strip(), relative_period="prior",
        ),
    )


def _period_revision_rank(header: str) -> int:
    """Prefer the issuer-designated restated/adjusted comparative column."""

    compact = re.sub(r"\s+", "", str(header).casefold())
    if any(marker in compact for marker in (
        "调整后", "重述后", "經重列", "经重列", "restated", "adjusted", "revised",
    )):
        return 3
    if any(marker in compact for marker in (
        "调整前", "重述前", "未重列", "beforeadjustment", "unadjusted",
    )):
        return 0
    return 1


def _select_period_cell(
    row: PdfRowAST, label_end: float, columns: Sequence[_PeriodColumn], target_year: int
) -> PdfCellAST | None:
    numeric = [cell for cell in row.cells if cell.x0 >= label_end and _parse_number(cell.text) is not None]
    if not numeric:
        return None
    target = next((column for column in columns if column.year == target_year), None)
    if target is None:
        target = next((column for column in columns if column.year == 0), None)
    if target is None:
        return None
    in_column = [cell for cell in numeric if target.left <= (cell.x0 + cell.x1) / 2 <= target.right]
    if not in_column:
        return None
    return min(in_column, key=lambda cell: abs((cell.x0 + cell.x1) / 2 - target.center))


def _explicit_unit_info(text: str, rules: FinancialRulesSnapshot | None = None) -> tuple[float, str, bool]:
    """Return ``(scale, currency, explicit)`` for a page/table heading.

    ``_unit_scale`` intentionally defaults to one for compatibility.  The
    parser must nevertheless distinguish an absent heading from an explicit
    ``元``/``RMB`` heading so an empty continuation page cannot reset a
    thousand-yuan context to a unit scale of one.
    """
    scale, currency = _unit_scale(text, rules)
    if len(_explicit_currencies(text, rules)) > 1:
        # A page with both the reporting currency and a convenience translation
        # has no single page-wide currency.  The caller supplies the issuer's
        # reporting currency while period columns retain their explicit units.
        currency = ""
    compact = re.sub(r"\s+", "", text.casefold())
    markers = (
        "hk$", "hkd", "hk拢", "港元", "港幣", "usd", "us$", "美元",
        "cny", "rmb", "人民币", "千元", "千人民币", "万元",
        "万人民币", "百万元", "rmb'000", "rmbmillion", "inthousands",
        "inmillions", "thousand", "million",
    )
    # A bare ``元`` is common in EPS labels and explanatory prose.  It is not
    # a table-wide unit declaration.  Accept it only when attached to an
    # explicit unit header; other currency/scale markers remain compatible
    # with existing statement headings.
    explicit_unit_header = bool(re.search(
        r"(?:单位|unit)[:：]?(?:人民币|rmb|cny|美元|usd|港元|hkd)?"
        r"(?:元|yuan|千元|万元|百万元|million|thousand)",
        compact,
    ))
    custom_marker = bool(
        rules is not None
        and any(
            str(alias).casefold().replace(" ", "") in compact
            for _canonical, aliases in rules.unit_aliases
            for alias in aliases
        )
    )
    return scale, currency, (
        any(marker.casefold() in compact for marker in markers)
        or explicit_unit_header
        or custom_marker
    )


def _unit_evidence_excerpt(text: str, rules: FinancialRulesSnapshot | None = None) -> str:
    """Keep the narrow source line that explicitly declares amount units.

    The evidence locator remains the source page; this excerpt is only the
    exact displayed unit text, never a reconstructed label from issuer data.
    """
    patterns = (
        r"(?:单位|unit)\s*[:：]?\s*[^\n]{1,100}",
        r"(?:RMB|CNY|USD|HKD|US\$|HK\$)\s*(?:'\s*000|thousand|million|billion)?",
        r"(?:人民币|港元|港幣|港币|美元)\s*(?:千元|万元|百万元|亿元|元)?",
        r"(?:千元|万元|百万元|亿元|百分比|%|％)",
        r"\bin\s+(?:thousands|millions|billions)\b",
    )
    matches = []
    for line in (part.strip() for part in str(text or "").splitlines()):
        if not line or len(line) > 240:
            continue
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns):
            if _explicit_unit_info(line, rules)[2] or "%" in line or "％" in line or "百分比" in line:
                matches.append(line)
    # An exact table declaration has priority over prose about separate note
    # tables. This agrees with _unit_scale's narrower explicit-header rule;
    # the displayed multiplier and its proof must identify the same header.
    direct = [line for line in matches if re.match(r"^(?:单位|unit)\s*[:：]", line, re.I)
              and literal_money_unit(line).scale_explicit]
    if direct:
        # Multiple direct headers within one section remain together for the
        # independent validator. Conflicts cannot disappear by page order.
        return "\n".join(dict.fromkeys(direct))
    # A currency-only line cannot replace the multiplier printed below it.
    return next((line for line in matches if literal_money_unit(line).scale_explicit),
                matches[0] if matches else "")


def _extra_money_unit_evidence(
    fact_id: str, filing: FilingDocument, *, excerpt: str,
    column: _PeriodColumn | None, page: int, currency: str,
    scale: Decimal, scope: str, document_units: DocumentUnitResolution | None,
) -> tuple[EvidenceRef, ...]:
    """Bind separate currency and multiplier declarations to their source.

    A selected column can provide currency while a statement heading provides
    scale. A verbatim statement-wide currency-only clause can prove currency
    independently of a local display-unit override. A full default-unit basis
    may supply currency only when its scale agrees. Conflicts stay visible.
    """
    refs: list[EvidenceRef] = []
    primary = literal_money_unit(excerpt)
    currencies = primary.currencies
    if column is not None and column.unit_excerpt and column.unit_bbox:
        captured = literal_money_unit(column.unit_excerpt)
        if captured.currencies and (
            captured.currencies != currencies
            or (captured.scale_explicit and captured.scales != primary.scales)
        ):
            refs.append(EvidenceRef(
                f"unit:column:{fact_id}", filing.document_id, filing.source_url,
                "literal statement column currency", f"page:{column.unit_page or page}",
                column.unit_excerpt, filing.filed_at, filing.content_hash, column.unit_bbox,
            ))
            currencies |= captured.currencies
    presentation = document_units.currency_for_scope(scope) if document_units is not None else None
    declaration = document_units.for_scope(scope) if document_units is not None else None
    if not currencies and presentation is not None and presentation.currency == currency:
        refs.append(EvidenceRef(
            f"unit:presentation:{fact_id}", filing.document_id, filing.source_url,
            "explicit statement-wide presentation currency", f"page:{presentation.page}",
            presentation.excerpt, filing.filed_at, filing.content_hash,
        ))
    elif (not currencies and declaration is not None
            and declaration.currency == currency
            and Decimal(declaration.scale) == scale):
        refs.append(EvidenceRef(
            f"unit:document:{fact_id}", filing.document_id, filing.source_url,
            "explicit statement-wide presentation basis", f"page:{declaration.page}",
            declaration.excerpt, filing.filed_at, filing.content_hash,
        ))
    elif (not currencies and document_units is not None
            and scope in {"consolidated", "parent"} and currency == "CNY"
            and primary.scale_explicit and primary.scales == frozenset({"1"})):
        refs.extend(ref for ref in document_units.statutory_presentation
                    if ref.evidence_id.startswith("unit:statutory:"))
    return tuple(refs)


def _parse_decimal(text: str) -> tuple[Decimal, bool] | None:
    """Parse the source token exactly and report whether it states percent.

    The returned Decimal is the displayed number; percent scaling is retained
    separately so the caller can place it in a source lineage.
    """
    cleaned = str(text or "").strip().replace("−", "-").replace("％", "%")
    is_percent = cleaned.endswith("%")
    if is_percent:
        cleaned = cleaned[:-1].strip()
    match = _NUM.fullmatch(cleaned)
    if not match or cleaned in {"-", "–", "—"}:
        return None
    token = match.group().replace(",", "").replace(" ", "")
    negative = token.startswith("(") or token.startswith("-")
    try:
        value = Decimal(token.strip("()-"))
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    if negative:
        value = -value
    return value, is_percent


def _raw_decimal_token(text: str) -> str:
    cleaned = str(text or "").strip().replace("−", "-").replace("％", "%")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1].strip()
    match = _NUM.fullmatch(cleaned)
    return match.group() if match else ""


def _row_title_context(row: PdfRowAST, rules: FinancialRulesSnapshot | None = None) -> tuple[str, str] | None:
    """Identify a formal statement title from positioned row text."""
    # Use the same anchored title contract as discovery. Substring matching
    # admitted note references and reconciliations as fresh annual statements.
    return _statement_context(row.text.strip(), rules)


def _is_statement_reconciliation_heading(text: str) -> bool:
    return bool(re.match(
        r"^\s*(?:reconciliation\s+(?:of|between)\b.*(?:income\s+statement|balance\s+sheet|cash\s+flow|gaap|ifrs)"
        r"|(?:.*财务报表|.*利润表|.*资产负债表).*调节(?:表)?)[^\n]*$",
        text, re.IGNORECASE,
    ))


def _known_numeric_row(rows: Sequence[PdfRowAST]) -> bool:
    return any(
        _known_label(_row_label_text(_merge_visual_rows(rows, index)))
        and any(_parse_number(cell.text) is not None for cell in _merge_visual_rows(rows, index).cells)
        for index in range(len(rows))
    )


def _revenue_group_total_rows(
    rows: Sequence[PdfRowAST],
    columns: Sequence[_PeriodColumn],
    target_year: int,
) -> dict[tuple[float, float, float, float], PdfRowAST]:
    """Find a bounded revenue group total after its detail rows.

    English IFRS statements commonly render ``Revenues`` as a group heading,
    followed by business-line rows and an unlabeled total row whose only text
    is a note number.  Only accept that row when at least two adjacent detail
    rows have values and their selected current-period values sum to the
    candidate total.  This prevents arbitrary unlabeled numbers from becoming
    revenue facts.
    """
    totals: dict[tuple[float, float, float, float], PdfRowAST] = {}
    for index, row in enumerate(rows):
        label = re.sub(r"\s+", "", _row_label_text(row)).casefold()
        if label not in {"revenue", "revenues", "totalrevenue"}:
            continue
        if any(_parse_number(cell.text) is not None for cell in row.cells):
            continue
        detail_values: list[float] = []
        for candidate in rows[index + 1 : index + 10]:
            candidate_label = _row_label_text(candidate)
            numeric = [cell for cell in candidate.cells if _parse_number(cell.text) is not None]
            if not numeric:
                continue
            if not candidate_label:
                selected = _select_period_cell(candidate, 0.0, columns, target_year)
                total = _parse_number(selected.text) if selected is not None else None
                if total is None or len(detail_values) < 2:
                    break
                if abs(sum(detail_values) - total) <= max(1.0, abs(total) * 0.01):
                    totals[candidate.bbox] = candidate
                break
            selected = _select_period_cell(candidate, min((cell.x1 for cell in candidate.cells if _parse_number(cell.text) is None), default=0.0), columns, target_year)
            value = _parse_number(selected.text) if selected is not None else None
            if value is not None:
                detail_values.append(value)
        if totals:
            continue
    return totals


def _equity_group_total_rows(
    rows: Sequence[PdfRowAST],
    columns: Sequence[_PeriodColumn],
    target_year: int,
) -> dict[tuple[float, float, float, float], PdfRowAST]:
    """Bind an unlabeled subtotal to its explicit owner-equity hierarchy.

    The source heading, components and following NCI/group-total boundary,
    not closeness to a computed amount, establish the row's concept.
    """
    totals: dict[tuple[float, float, float, float], PdfRowAST] = {}
    for index, row in enumerate(rows):
        label = _row_label_text(row)
        if equity_row_concept(label) != "equity" or any(_parse_number(cell.text) is not None for cell in row.cells):
            continue
        component_count = 0
        for offset, candidate in enumerate(rows[index + 1 : index + 12], index + 1):
            candidate_label = _row_label_text(candidate)
            numeric = [cell for cell in candidate.cells if _parse_number(cell.text) is not None]
            if not numeric:
                continue
            if not candidate_label:
                selected = _select_period_cell(candidate, 0.0, columns, target_year)
                next_labels = tuple(_label_compact(_row_label_text(item)) for item in rows[offset + 1:offset + 3])
                has_group_boundary = any(item in {"noncontrollinginterests", "minorityinterests", "totalequity"} for item in next_labels)
                if selected is not None and component_count >= 2 and has_group_boundary:
                    totals[candidate.bbox] = PdfRowAST(
                        tuple((*row.cells, *candidate.cells)), candidate.top,
                        (min(row.bbox[0], candidate.bbox[0]), min(row.bbox[1], candidate.bbox[1]),
                         max(row.bbox[2], candidate.bbox[2]), max(row.bbox[3], candidate.bbox[3])),
                    )
                break
            selected = _select_period_cell(candidate, min((cell.x1 for cell in candidate.cells if _parse_number(cell.text) is None), default=0.0), columns, target_year)
            value = _parse_number(selected.text) if selected is not None else None
            if value is not None:
                component_count += 1
    return totals


def _continuation_compatible(context: PdfTableContext, rows: Sequence[PdfRowAST], rules: FinancialRulesSnapshot | None = None) -> bool:
    """Require a labelled target row and a value in a known period column."""
    if not rows:
        return False
    periods = context.periods or _period_columns(rows, rules=rules)
    if not periods:
        return False
    for index in range(len(rows)):
        merged = _merge_visual_rows(rows, index, rules=rules)
        if not any(_parse_number(cell.text) is not None for cell in merged.cells):
            # Some statement renderers use a hanging indent for the final
            # fragment of a wrapped label, so the ordinary same-column merge
            # deliberately refuses it.  At the continuation gate, accept only
            # a very small adjacent fragment when the combined label is a
            # known financial concept and the fragment row itself carries a
            # value in a known period column.  This admits split rows such as
            # ``经营活动产生的现金流`` + ``量净额 <current> <prior>`` without
            # turning narrative paragraphs or the next line item into a table
            # continuation.
            head = rows[index]
            head_label = _row_label_text(head)
            if head_label and not any(
                _parse_number(cell.text) is not None for cell in head.cells
            ):
                for tail in rows[index + 1:index + 3]:
                    if tail.top - head.bbox[3] > 6.5:
                        break
                    tail_label = _row_label_text(tail)
                    if (
                        not tail_label
                        or len(tail_label) > 12
                        or _known_label(tail_label, rules=rules)
                    ):
                        break
                    joined = PdfRowAST(
                        tuple((*head.cells, *tail.cells)),
                        head.top,
                        (
                            min(head.bbox[0], tail.bbox[0]),
                            min(head.bbox[1], tail.bbox[1]),
                            max(head.bbox[2], tail.bbox[2]),
                            max(head.bbox[3], tail.bbox[3]),
                        ),
                    )
                    head_compact = _label_compact(head_label)
                    combined_compact = _label_compact(_row_label_text(joined))
                    extension_completes_label = any(
                        _label_compact(label) in combined_compact
                        and _label_compact(label) not in head_compact
                        for labels in _labels_for_rules(rules).values()
                        for label in labels
                    )
                    if extension_completes_label:
                        merged = joined
                        break
                    if any(_parse_number(cell.text) is not None for cell in tail.cells):
                        break
        if not _known_label(_row_label_text(merged), rules=rules):
            continue
        label_end = max(
            (cell.x1 for cell in merged.cells if _parse_number(cell.text) is None),
            default=0.0,
        )
        for cell in merged.cells:
            if _parse_number(cell.text) is None or cell.x0 < label_end:
                continue
            center = (cell.x0 + cell.x1) / 2
            if any(column.left <= center <= column.right for column in periods):
                return True
    return False


def _page_sections(
    previous: PdfTableContext | None,
    page_text: str,
    rows: Sequence[PdfRowAST],
    page_number: int,
    default_currency: str,
    rules: FinancialRulesSnapshot | None = None,
    chapter_unit: PdfTableContext | None = None,
    document_units: DocumentUnitResolution | None = None,
) -> tuple[PdfPageSection, ...]:
    """Pure page-context state transition used by the PDF adapter.

    Summary pages never inherit.  Formal titles create a new context; pages
    without a title may inherit only two adjacent pages and only when their
    coordinates still look like the same table.  Multiple titles on one page
    become separate sections, which prevents a parent table header from
    reclassifying the consolidated rows above it.
    """
    rows_tuple = tuple(rows)
    # A statement page may be followed immediately by segment-note tables.
    # Those tables often reuse labels such as "revenue" and have numeric
    # columns, so ordinary continuation heuristics can otherwise inherit the
    # consolidated income-statement context. Segment values need a typed
    # segment identity before they can safely enter FinancialFact; until that
    # contract exists, truncate at a detectable segment heading or isolate an
    # explicit segment page when the table boundary cannot be located.
    segment_start = next(
        (index for index, row in enumerate(rows_tuple)
         if _is_segment_disclosure_heading(row.text) or _is_statement_reconciliation_heading(row.text)),
        None,
    )
    if segment_start is not None:
        prefix = rows_tuple[:segment_start]
        if _statement_context("\n".join(row.text for row in prefix), rules) is None:
            return ()
        rows_tuple = prefix
        # Keep unit/currency inference scoped to the statement side of the
        # boundary too; note tables can use a different display scale.
        page_text = "\n".join(row.text for row in prefix)
    elif _is_explicit_segment_disclosure(page_text):
        return ()
    scale, currency, explicit = _explicit_unit_info(page_text, rules)
    unit_excerpt = _unit_evidence_excerpt(page_text, rules)
    unit_page = page_number if unit_excerpt else None
    if _is_summary_page(page_text, rows_tuple, rules=rules):
        return (PdfPageSection(
            PdfTableContext("summary", "consolidated", scale, currency or default_currency,
                            explicit, _period_columns(rows_tuple, default_currency, rules, source_page=page_number), page_number, 0,
                            "explicit" if explicit else "unknown", unit_excerpt, unit_page),
            rows_tuple, True, False,
        ),)

    titles = [
        (index, _row_title_context(row, rules))
        for index, row in enumerate(rows_tuple)
        if not is_narrative_title_tail(row.text, rows_tuple[index - 1].text if index else "")
    ]
    titles = [(index, context) for index, context in titles if context is not None]
    titled_indices = {index for index, _context in titles}
    # PDF word extraction keeps each visual line as a separate AST row.  Join
    # only adjacent short rows when neither row is already a title so wrapped
    # formal identities such as ``CONSOLIDATED INCOME`` / ``STATEMENT`` still
    # establish a section without admitting narrative note mentions.
    for index in range(len(rows_tuple) - 1):
        if index in titled_indices or index + 1 in titled_indices:
            continue
        left, right = rows_tuple[index], rows_tuple[index + 1]
        if len(left.text.strip()) > 36 or len(right.text.strip()) > 36:
            continue
        joined_context = _statement_context(f"{left.text} {right.text}", rules)
        if joined_context is not None:
            titles.append((index, joined_context))
    titles.sort(key=lambda item: item[0])
    sections: list[PdfPageSection] = []
    if titles:
        # A title below a continuation's rows is a table boundary.  Keep the
        # previous table only for rows before the first title when they still
        # contain a compatible labelled value.
        first_index = titles[0][0]
        # A formal statement may end with a short continuation page immediately
        # before a parent-company table starts (e.g. the consolidated equity
        # totals split across three pages).  Retain one additional *titled*
        # continuation when its rows still carry a known labelled value.  The
        # untitled path below remains capped at two pages to avoid narrative
        # leakage.
        if first_index and previous and previous.statement != "summary" and previous.last_page + 1 == page_number and previous.inherited_pages < 3:
            prefix = rows_tuple[:first_index]
            if _continuation_compatible(previous, prefix, rules):
                sections.append(PdfPageSection(
                    PdfTableContext(previous.statement, previous.scope, previous.multiplier,
                                    previous.currency, previous.unit_explicit,
                                    previous.periods, page_number, previous.inherited_pages + 1,
                                    previous.unit_provenance,
                                    previous.unit_evidence, previous.unit_evidence_page,
                                    previous.statement_evidence, previous.statement_evidence_page,
                                    previous.statement_evidence_bbox),
                    prefix, False, True,
                ))
        for position, (title_index, (statement, scope)) in enumerate(titles):
            end = titles[position + 1][0] if position + 1 < len(titles) else len(rows_tuple)
            section_rows = rows_tuple[title_index:end]
            title_rows = ()
            for count in range(1, min(3, len(section_rows)) + 1):
                if _statement_context("\n".join(row.text for row in section_rows[:count]), rules) == (statement, scope):
                    title_rows = section_rows[:count]
                    break
            statement_excerpt = "\n".join(row.text for row in title_rows)
            statement_bbox = (
                min(row.bbox[0] for row in title_rows), min(row.bbox[1] for row in title_rows),
                max(row.bbox[2] for row in title_rows), max(row.bbox[3] for row in title_rows),
            ) if title_rows else None
            # Bind each formal table to its own rows. A second parent table
            # can use another unit on the same physical page; page-wide 'last
            # unit wins' would silently rescale the group statement above it.
            section_text = "\n".join(row.text for row in section_rows)
            local_unit_excerpt = _unit_evidence_excerpt(section_text, rules)
            if not local_unit_excerpt and position == 0:
                local_unit_excerpt = _unit_evidence_excerpt(
                    "\n".join(row.text for row in rows_tuple[:title_index]), rules)
            local_scale, local_currency, section_explicit = _explicit_unit_info(
                local_unit_excerpt or section_text, rules)
            section_scale = local_scale if section_explicit else 1.0
            section_currency = local_currency or default_currency
            periods = _period_columns(section_rows, default_currency, rules, source_page=page_number)
            title_text = section_rows[0].text.casefold() if section_rows else ""
            continuation_title = bool(re.search(
                r"续|continued|continuation|cont[\s.'’_-]*d", title_text
            ))
            can_inherit_unit = bool(
                previous
                and previous.statement == statement
                and previous.scope == scope
                and previous.last_page + 1 == page_number
                and not section_explicit
                and previous.unit_explicit
                and continuation_title
            )
            can_inherit_chapter_unit = bool(
                not can_inherit_unit
                and not section_explicit
                and chapter_unit
                and chapter_unit.unit_explicit
                and 0 < page_number - chapter_unit.last_page <= 12
                and (
                    not chapter_unit.currency
                    or not default_currency
                    or chapter_unit.currency.upper() == default_currency.upper()
                )
                and scope == "consolidated"
            )
            document_unit = document_units.for_scope(scope) if document_units is not None else None
            can_use_document_unit = bool(
                not section_explicit and not can_inherit_unit and not can_inherit_chapter_unit
                and document_unit is not None
            )
            if can_inherit_unit:
                section_scale = previous.multiplier
                section_currency = previous.currency or default_currency
            elif can_inherit_chapter_unit and chapter_unit is not None:
                section_scale = chapter_unit.multiplier
                section_currency = chapter_unit.currency or default_currency
            elif can_use_document_unit and document_unit is not None:
                section_scale = float(Decimal(document_unit.scale))
                section_currency = document_unit.currency
            section_unit_provenance = (
                "inherited" if can_inherit_unit else
                "chapter_inherited" if can_inherit_chapter_unit else
                "document_explicit" if can_use_document_unit else
                "explicit" if section_explicit else "unknown"
            )
            inherited_context = (
                previous if can_inherit_unit else
                chapter_unit if can_inherit_chapter_unit else None
            )
            section_unit_excerpt = local_unit_excerpt or (
                inherited_context.unit_evidence if inherited_context else ""
            )
            section_unit_page = (page_number if local_unit_excerpt else None) or (
                inherited_context.unit_evidence_page if inherited_context else None
            )
            if can_use_document_unit and document_unit is not None:
                section_unit_excerpt = document_unit.excerpt
                section_unit_page = document_unit.page
            if not periods and previous and previous.statement == statement and previous.scope == scope:
                periods = previous.periods
            sections.append(PdfPageSection(
                PdfTableContext(statement, scope, section_scale, section_currency,
                                section_explicit or can_inherit_unit or can_inherit_chapter_unit or can_use_document_unit, periods, page_number,
                                previous.inherited_pages + 1 if can_inherit_unit else 0,
                                section_unit_provenance, section_unit_excerpt, section_unit_page,
                                statement_excerpt, page_number if title_rows else None,
                                statement_bbox),
                section_rows, False, can_inherit_unit,
            ))
        return tuple(sections)

    if (
        previous
        and previous.statement != "summary"
        and previous.last_page + 1 == page_number
        and previous.inherited_pages < 2
        and _continuation_compatible(previous, rows_tuple, rules)
    ):
        inherited_scale = previous.multiplier if not explicit else scale
        inherited_currency = currency or previous.currency or default_currency
        inherited_periods = previous.periods or _period_columns(rows_tuple, default_currency, rules, source_page=page_number)
        inherited_unit_provenance = "explicit" if explicit else "inherited"
        continuation_unit_excerpt = unit_excerpt or previous.unit_evidence
        continuation_unit_page = unit_page or previous.unit_evidence_page
        return (PdfPageSection(
            PdfTableContext(previous.statement, previous.scope, inherited_scale, inherited_currency,
                            previous.unit_explicit or explicit, inherited_periods, page_number,
                            previous.inherited_pages + 1, inherited_unit_provenance,
                            continuation_unit_excerpt, continuation_unit_page,
                            previous.statement_evidence, previous.statement_evidence_page,
                            previous.statement_evidence_bbox),
            rows_tuple, False, True,
        ),)
    return ()


def _row_label_source_text(row: PdfRowAST) -> str:
    """Return label fragments while excluding period/value cells.

    Some Chinese annual reports wrap a single label over two or three visual
    lines and place the period values between those fragments.  Coordinates,
    rather than cell order, identify the label column: non-numeric cells to
    the left of the first numeric cell are retained, including a wrapped tail
    that appears after the values in reading order.
    """
    cells = _reading_order_cells(row.cells)
    numeric = [cell for cell in cells if _parse_number(cell.text) is not None]
    first_value_x = min((cell.x0 for cell in numeric), default=None)
    fragments = [
        cell.text
        for cell in cells
        if _parse_number(cell.text) is None
        and (first_value_x is None or cell.x1 <= first_value_x + 4)
    ]
    return " ".join(fragments)


def _row_label_text(row: PdfRowAST) -> str:
    """Compact matching adapter; verbatim source fragments remain available."""
    return re.sub(r"\s+", "", _row_label_source_text(row))


def _known_label(text: str, rules: FinancialRulesSnapshot | None = None) -> bool:
    if is_profit_sign_convention(text):
        return False
    compact = _label_compact(text)
    return (
        any(_label_compact(label) in compact for labels in _labels_for_rules(rules).values() for label in labels)
        or _is_capex_cash_outflow_label(text)
        or _capital_return_label(text) is not None
    )


@lru_cache(maxsize=8192)
def _label_compact(text: str) -> str:
    return normalize_financial_label(text)


@lru_cache(maxsize=32)
def _exact_label_keys(rules: FinancialRulesSnapshot | None) -> frozenset[str]:
    """Bounded immutable lookup; the full frozen rules value is the key."""
    return frozenset(_label_compact(alias) for labels in _labels_for_rules(rules).values() for alias in labels)


def _capex_cash_outflow_label(text: str) -> str:
    """Return a normalized capex label, or empty for unrelated rows.

    HKEX IFRS statements phrase the same cash outflow in several ways, often
    wrapping the asset classes onto a second visual line.  Require both an
    acquisition/payment verb and an explicit PPE or intangible-asset class;
    proceeds/disposal rows are deliberately excluded.  Fact creation remains
    restricted to the parser's formal cash-flow statement context.
    """

    normalized = _label_compact(text)
    if not normalized:
        return ""
    if any(token in normalized for token in (
        "proceed", "disposal", "disposed", "saleof", "saleproceeds",
        "出售", "处置", "處置", "收回",
    )):
        return ""
    outflow = any(token in normalized for token in (
        "purchase", "purchases", "prepayment", "prepayments",
        "acquisition", "acquisitions", "paymentfor", "paymentsfor",
        "购置", "購置", "购建", "購建", "购入", "購入", "支付",
    ))
    ppe = any(token in normalized for token in (
        "propertyplantandequipment", "fixedassets", "物业厂房及设备",
        "固定资产",
    ))
    intangible = any(token in normalized for token in ("无形资产", "intangibleasset"))
    if not outflow or not (ppe or intangible):
        return ""
    if ppe and intangible:
        return "purchases of property, plant and equipment and intangible assets"
    if intangible:
        return "purchase of intangible assets"
    return "purchase of property, plant and equipment"


def _is_capex_cash_outflow_label(text: str) -> bool:
    """Recognize capital-asset purchase rows beyond a finite alias list."""

    return bool(_capex_cash_outflow_label(text))


def _capital_return_label(text: str) -> tuple[str, str] | None:
    """Classify only explicit cash paid for issuer share buybacks/dividends.

    The caller already requires a formal cash-flow statement context. This
    helper stays conservative about the recipient/instrument: investment
    purchases, subsidiary/associate acquisitions, share disposals and proceeds
    are not issuer capital returns. Original row text is retained separately
    on every emitted fact as evidence.
    """

    normalized = _label_compact(text)
    if not normalized:
        return None
    excluded = (
        "proceed", "proceeds", "disposal", "disposed", "saleof", "saleproceeds",
        "receivedfrom", "receiptfrom", "exerciseofshareoptions", "sharebased",
        "equitysecurities", "investmentin", "associate", "subsidiar",
        "noncontrolling", "出售", "处置", "處置", "所得", "收到",
        "联营", "聯營", "子公司", "附属公司", "附屬公司", "非控股",
    )
    if any(token in normalized for token in excluded):
        return None

    share_words = ("share", "shares", "stock", "股份", "股票", "库存股", "庫存股")
    if any(token in normalized for token in ("buyback", "repurchase", "回购", "回購", "购回", "購回")):
        if any(token in normalized for token in share_words):
            return "share_repurchase", "repurchase of own shares"
    explicit_own_purchase = any(token in normalized for token in (
        "treasuryshare", "ownshare", "ownshares", "companysshare", "companyshares",
        "ordinaryshare", "ordinaryshares", "commonshare", "commonshares",
    ))
    if (
        "purchase" in normalized
        and explicit_own_purchase
        and any(token in normalized for token in share_words)
    ):
        return "share_repurchase", "purchase of own shares"

    dividend = any(token in normalized for token in ("dividend", "股利", "股息", "分红", "分紅"))
    paid = any(token in normalized for token in (
        "paid", "payment", "payments", "payable", "支付", "派付", "已付",
    ))
    recipient = any(token in normalized for token in (
        "shareholder", "shareholders", "equityholder", "equityholders",
        "ownersofthecompany", "ownersofparent", "股东", "股東", "所有者",
    ))
    if dividend and paid and recipient:
        return "shareholder_dividends_paid", "dividends paid to shareholders"
    return None


def _is_explicit_segment_disclosure(text: str) -> bool:
    """Identify explicit segment-note context without guessing a segment ID."""

    return any(_is_segment_disclosure_heading(line) for line in text.splitlines())


def _is_segment_disclosure_heading(text: str) -> bool:
    markers = (
        "segmentinformation", "operatingsegments", "reportablesegments",
        "businesssegments", "分部资料", "分部資料", "分部信息",
        "业务分部信息", "業務分部資料", "经营分部", "經營分部",
    )
    line_compact = _label_compact(text)
    if not line_compact or len(line_compact) > 100:
        return False
    # Match short heading-like lines only. A narrative sentence that merely
    # mentions segments must not suppress an otherwise valid statement page.
    return any(marker in line_compact for marker in markers) and (
        len(line_compact) <= 46 or line_compact.startswith(("note", "附注", "附註"))
    )


def _merge_capex_facts(
    existing: FinancialFact,
    candidate: FinancialFact,
) -> FinancialFact | None:
    """Aggregate distinct capex components only when provenance is identical."""

    existing_kind = capex_component_kind(existing.reported_concept)
    candidate_kind = capex_component_kind(candidate.reported_concept)
    if existing.reported_concept == "capital_expenditure_components":
        existing_kind = "aggregate"
    if candidate_kind == "total":
        if existing_kind == "total":
            # Two totals are observations for the compiler, not a parser
            # ranking decision. Even a lower-ranked row may be more precise.
            return None
        return candidate
    if existing_kind in {"total", "aggregate"} or existing_kind == candidate_kind:
        return None
    same_context = (
        existing.accession_number == candidate.accession_number
        and existing.end_date == candidate.end_date
        and existing.fiscal_period == candidate.fiscal_period
        and existing.consolidated_scope == candidate.consolidated_scope
        and existing.currency == candidate.currency
        and existing.unit_scale == candidate.unit_scale
        and existing.statement == candidate.statement == "cash_flow"
        and existing.source_page == candidate.source_page
    )
    if not same_context:
        return None
    boxes = tuple(box for box in (existing.source_bbox, candidate.source_bbox) if box)
    bbox = (
        (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        if boxes
        else None
    )
    identity = (
        f"{existing.accession_number}|capital_expenditure|{existing.end_date}|"
        f"{existing.source_page}|{existing.fact_id}|{candidate.fact_id}"
    )
    lineage = None
    exact_value = exact_decimal_arithmetic((
        Decimal(str(abs(existing.value))), Decimal(str(abs(candidate.value))),
    ))
    if (
        existing.normalization_lineage is not None
        and candidate.normalization_lineage is not None
    ):
        left_lineage = existing.normalization_lineage
        right_lineage = candidate.normalization_lineage
        if (
            left_lineage.quantity_kind == right_lineage.quantity_kind == QuantityKind.MONEY.value
            and left_lineage.currency == right_lineage.currency == existing.currency
        ):
            exact_value = exact_decimal_arithmetic((
                Decimal(left_lineage.normalized_value),
                Decimal(right_lineage.normalized_value),
            ))
            lineage = FinancialNormalizationLineage(
                raw_value="",
                scale=format(Decimal(str(existing.unit_scale)).normalize(), "f"),
                normalized_value=format(exact_value, "f"),
                quantity_kind=QuantityKind.MONEY.value,
                currency=existing.currency,
                unit_evidence_ids=tuple(dict.fromkeys(
                    (*left_lineage.unit_evidence_ids, *right_lineage.unit_evidence_ids)
                )),
                method="sum-absolute-components-v1",
                value_transform="sum",
                inputs=(left_lineage, right_lineage),
            )
    return replace(
        existing,
        fact_id=f"ingest:{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
        reported_concept="capital_expenditure_components",
        value=float(exact_value),
        normalization_lineage=lineage,
        source_bbox=bbox,
        raw_text=f"{existing.raw_text}\n{candidate.raw_text}",
    )


def _net_income_candidate_allowed(compact: str) -> bool:
    """Require attributable context for the short IFRS equity-holder label."""
    normalized = re.sub(r"\s+", "", compact).casefold()
    if "equityholdersofthecompany" not in normalized:
        return True
    if any(token in normalized for token in ("earningspershare", "basic", "diluted")):
        return False
    return "attributableto" in normalized


_GENERIC_NET_INCOME_LABELS = frozenset({
    "净利润", "profit for the year", "profit after tax", "net profit",
})


def _is_narrative_date_header(text: str) -> bool:
    """Reject prose date ranges before they can become period/value cells.

    Bilingual IFRS notes often contain sentences such as ``results for the
    years ended December 31, 2025 and 2024 are as follows``.  Their numbers
    are dates, not financial values.  Requiring both a date-range phrase and
    two distinct years keeps ordinary ``Revenue | 2025 | 2024`` headers valid.
    """
    folded = re.sub(r"\s+", " ", text.casefold()).strip()
    years = set(re.findall(r"(?:19|20)\d{2}", folded))
    if len(years) < 2:
        return False
    date_phrase = (
        "year ended", "years ended", "for the years", "as at",
        "截至", "年度", "年末",
    )
    narrative_tail = ("as follows", "如下", "information", "results", "资料")
    return any(marker in folded for marker in date_phrase) and any(
        marker in folded for marker in narrative_tail
    )


def _ownership_classification_heading_row(
    rows: Sequence[PdfRowAST], start: int,
) -> PdfRowAST | None:
    """Find a formal same-table ownership heading within eight prior rows."""
    if start <= 0 or start >= len(rows):
        return None
    current_labels = [cell for cell in rows[start].cells if _parse_number(cell.text) is None]
    anchor = min((cell.x0 for cell in current_labels), default=None)
    for row in reversed(rows[max(0, start - 8):start]):
        label = _row_label_text(row)
        if not label:
            continue
        if is_profit_ownership_heading(label):
            if any(_parse_number(cell.text) is not None for cell in row.cells):
                return None
            row_labels = [cell for cell in row.cells if _parse_number(cell.text) is None]
            if anchor is not None and row_labels and min(
                abs(cell.x0 - anchor) for cell in row_labels
            ) > 80:
                return None
            return row
        compact = _label_compact(financial_label_body(label))
        if _ownership_context_boundary(row, compact):
            return None
    return None


def _ownership_context_boundary(row: PdfRowAST, compact_label: str) -> bool:
    """Return whether a nearer row makes an older ownership heading unsafe."""
    source_text = _row_label_source_text(row)
    if classify_statement_title(source_text) is not None:
        return True
    if re.search(r"\bEPS\b", source_text, flags=re.I):
        return True
    if any(token in compact_label for token in (
        "comprehensive", "comprehensiveincome", "综合收益", "全面收益",
        "earningspershare", "pershare", "perads", "每股",
    )):
        return True
    if compact_label in {"basic", "diluted"}:
        return True
    if compact_label.startswith("classifiedby"):
        return True
    if compact_label.startswith(("按", "依")) and compact_label.endswith(("分类", "分類")):
        return True
    has_attribution = (
        "attributableto" in compact_label or "归属于" in compact_label or "歸屬於" in compact_label
    )
    if not has_attribution:
        return False
    has_values = any(_parse_number(cell.text) is not None for cell in row.cells)
    if has_values and profit_row_concept(source_text) == "parent_net_income":
        return False
    label_body = financial_label_body(source_text).strip()
    has_formal_ordinal = label_body.casefold() != source_text.strip().casefold()
    if has_formal_ordinal and _label_compact(label_body) == "netprofitattributableto":
        return False
    return True


def _attribution_context(rows: Sequence[PdfRowAST], start: int) -> str:
    """Join a bounded attribution heading with its following visual row.

    IFRS income tables commonly render ``Attributable to:`` and the equity
    holder label on separate rows.  Only the preceding two rows in the same
    AST table and aligned label column are eligible; this cannot pull context
    from an EPS note or another statement.
    """
    if start <= 0:
        return ""
    current_labels = [cell for cell in rows[start].cells if _parse_number(cell.text) is None]
    anchor = min((cell.x0 for cell in current_labels), default=None)
    for row in rows[max(0, start - 2):start]:
        label = _row_label_text(row)
        compact = _label_compact(label)
        if "attributableto" not in compact and "归属于" not in label:
            continue
        label_body = financial_label_body(label).strip()
        has_formal_ordinal = label_body.casefold() != label.strip().casefold()
        if has_formal_ordinal and _label_compact(label_body) == "netprofitattributableto":
            # This is the beginning of a numbered parent allocation row, not
            # a reusable ``Attributable to:`` heading for the following NCI
            # leaf. Let ownership-heading lookup below supply that context.
            continue
        if any(_parse_number(cell.text) is not None for cell in row.cells):
            continue
        row_labels = [cell for cell in row.cells if _parse_number(cell.text) is None]
        if anchor is not None and row_labels and min(abs(cell.x0 - anchor) for cell in row_labels) > 24:
            continue
        return label
    ownership_heading = _ownership_classification_heading_row(rows, start)
    return _row_label_text(ownership_heading) if ownership_heading is not None else ""


def _append_allocation_heading_proof(
    rows: Sequence[PdfRowAST], start: int, row: PdfRowAST, attribution: str,
) -> PdfRowAST:
    """Include only the literal, valueless heading used to classify a row."""
    if not attribution:
        return row
    if is_profit_ownership_heading(attribution):
        heading = _ownership_classification_heading_row(rows, start)
    else:
        heading = next((
            candidate for candidate in reversed(rows[max(0, start - 2):start])
            if _row_label_text(candidate) == attribution
            and not any(_parse_number(cell.text) is not None for cell in candidate.cells)
        ), None)
    if heading is None:
        return row
    heading_cells = tuple(
        cell for cell in heading.cells if _parse_number(cell.text) is None
    )
    if not heading_cells:
        return row
    return PdfRowAST(
        tuple((*heading_cells, *row.cells)), row.top,
        (
            min(row.bbox[0], *(cell.x0 for cell in heading_cells)),
            min(row.bbox[1], *(cell.top for cell in heading_cells)),
            max(row.bbox[2], *(cell.x1 for cell in heading_cells)),
            max(row.bbox[3], *(cell.bottom for cell in heading_cells)),
        ),
    )


def _interest_parent_row(rows: Sequence[PdfRowAST], start: int) -> PdfRowAST | None:
    """Find the explicit parent within this table, not a page-wide keyword."""
    for row in reversed(rows[max(0, start - 8):start]):
        label = _row_label_text(row)
        if interest_income_row_concept("利息收入", label) is not None:
            return row
        if _label_compact(financial_label_body(label)).startswith((
            "营业总成本", "营业利润", "投资收益", "公允价值变动收益",
            "otherincome", "othergains", "investmentincome",
            "costofrevenue", "costofsales", "operatingexpenses", "operatingprofit",
        )):
            break
    return None


def _summary_revision_boundary(rows: Sequence[PdfRowAST]) -> int | None:
    """Keep preliminary/final columns out of the unrelated annual year grid."""
    return next((index for index, row in enumerate(rows)
                 if "业绩快报" in row.text and "年度报告" in row.text
                 and not any(_parse_number(cell.text) is not None for cell in row.cells)), None)


def _completes_known_label(prefix: str, suffix: str, rules: FinancialRulesSnapshot | None = None) -> bool:
    """Return whether a short, unrecognized tail completes a known label.

    Some Chinese statements place a wrapped label fragment after the numeric
    cells for that same row. Only permit the horizontal offset when the
    concatenated fragments match the prefix and suffix of one registered
    canonical label; arbitrary nearby text must remain a separate row.
    """
    prefix_compact = _label_compact(prefix)
    suffix_compact = _label_compact(suffix)
    if (
        not prefix_compact
        or not suffix_compact
        or len(suffix_compact) > 12
        or _known_label(prefix, rules)
        or _known_label(suffix, rules)
    ):
        return False
    return any(
        normalized.startswith(prefix_compact)
        and normalized.endswith(suffix_compact)
        and len(normalized) >= len(prefix_compact) + len(suffix_compact)
        for labels in _labels_for_rules(rules).values()
        for label in labels
        if (normalized := _label_compact(label))
    )


def _merge_visual_rows(rows: Sequence[PdfRowAST], start: int, rules: FinancialRulesSnapshot | None = None) -> PdfRowAST:
    """Merge at most three tightly-spaced visual rows from one table row.

    The merge is deliberately conservative.  Rows must overlap or be within
    one normal line height, share the left label x-coordinate, and either
    contain no text (a values-only line) or not introduce a new complete
    financial label.  This prevents adjacent independent metrics from being
    swallowed while repairing labels such as ``加权平均净资产收益`` + values +
    ``率``.
    """
    initial_label = _label_compact(_row_label_text(rows[start]))
    initial_source_label = re.sub(r"\s+", "", _row_label_source_text(rows[start]))
    if (
        not any(_parse_number(cell.text) is not None for cell in rows[start].cells)
        and re.fullmatch(r"[（(]?(?:元|人民币)[/／](?:股|每股)[）)]?", initial_source_label)
    ):
        # A stand-alone unit continuation has no row identity of its own and
        # must not start a second merge into the complete following metric.
        return rows[start]
    if (initial_label.removeprefix("其中").removeprefix("including") in {"利息收入", "interestincome", "interestrevenue"}
            and any(_parse_number(cell.text) is not None for cell in rows[start].cells)):
        # An already complete valued leaf must not absorb the next blank
        # insurance/commission line just because that label is unsupported.
        return rows[start]
    activity_headings = {
        "cashflowsfromoperatingactivities", "cashflowsfrominvestingactivities",
        "cashflowsfromfinancingactivities",
    }
    if initial_label in activity_headings:
        return rows[start]
    if initial_label in {"item", "items", "项目", "项目附注", "itemnote", "itemnotes"} and sum(
        _effective_period_year(cell.text) is not None for cell in rows[start].cells
    ) >= 2:
        # Explicit multi-period column headers are not amounts belonging to
        # the first wrapped cash-purchase row. Do not apply this to unlabeled
        # balance subtotals whose numeric amounts happen to resemble years.
        return rows[start]
    variants = [rows[start]]
    anchor = min(
        (cell.x0 for cell in rows[start].cells if _parse_number(cell.text) is None),
        default=min((cell.x0 for cell in rows[start].cells), default=0.0),
    )
    for offset in range(1, 3):
        index = start + offset
        if index >= len(rows):
            break
        previous = variants[-1]
        candidate = rows[index]
        vertical_gap = candidate.top - previous.bbox[3]
        if vertical_gap > 6.5 or candidate.top < previous.top:
            break
        candidate_labels = [
            cell for cell in candidate.cells if _parse_number(cell.text) is None
        ]
        current_label = _row_label_text(
            PdfRowAST(
                tuple(cell for row in variants for cell in row.cells),
                variants[0].top,
                variants[0].bbox,
            )
        )
        candidate_label = _row_label_text(candidate)
        if _label_compact(candidate_label) in activity_headings:
            break
        wrapped_completion = _completes_known_label(
            current_label, candidate_label, rules
        )
        sign_annotation = (
            is_profit_sign_convention(candidate_label)
            and profit_row_concept(current_label) is not None
            and not any(_parse_number(cell.text) is not None for cell in variants[-1].cells)
        )
        aligned = any(abs(cell.x0 - anchor) <= 14 for cell in candidate_labels)
        aligned = aligned or wrapped_completion or sign_annotation
        if candidate_labels and not aligned:
            break
        combined_label = current_label + candidate_label
        current_has_values = any(
            _parse_number(cell.text) is not None
            for variant in variants for cell in variant.cells
        )
        candidate_has_values = any(_parse_number(cell.text) is not None for cell in candidate.cells)
        if candidate_has_values and candidate_label:
            # An ownership allocation leaf with its own values is a new row,
            # even when the preceding parent label is still a valueless
            # wrapped fragment. Do not merge that fragment into the next NCI
            # row; the bounded context lookup also respects table/title/EPS
            # boundaries.
            candidate_attribution = _attribution_context(rows, index)
            candidate_profit_concept = profit_row_concept(
                candidate_label, candidate_attribution,
            ) if candidate_attribution else None
            if candidate_profit_concept in {
                "parent_net_income", "minority_interest_profit",
                "ordinary_shareholder_net_income",
            }:
                break
        if candidate_has_values and candidate_label:
            exact_labels = _exact_label_keys(rules)
            if (_label_compact(current_label) in exact_labels
                    and _label_compact(combined_label) not in exact_labels
                    and not sign_annotation):
                # A complete heading introduces a component block, not a
                # wrapped row. Never borrow the first child's value.
                break
        # A colon-terminated section heading is not the trailing fragment of
        # the preceding amount row. Font encoding can leave that heading
        # unrecognized by the label registry; its geometric boundary still
        # holds. Explicit registered wrapped-label completions remain allowed.
        if current_has_values and candidate_label.endswith((":", "：")) and not wrapped_completion:
            break
        if current_has_values and not wrapped_completion and _label_compact(candidate_label).startswith((
            "payment", "purchase", "proceeds", "repayment", "distribution",
        )):
            # A new payment/acquisition label may itself wrap onto the next
            # line. It must not borrow the preceding lease/payment amount.
            break
        # A heading such as ``Revenues`` introduces a detail block; do not
        # swallow the first detail row into the heading.  Likewise, two
        # adjacent numeric rows are independent line items (the previous
        # implementation merged all English detail rows into one candidate).
        if candidate_has_values and current_label.casefold() in {"revenue", "revenues", "totalrevenue"}:
            break
        if current_has_values and candidate_has_values and candidate_label:
            break
        # A complete financial label after a numeric row starts a new line
        # item, even when concatenating both labels still contains a known
        # alias.  Incomplete wrapped tails (for example ``率``) remain
        # mergeable because they are not independently recognized labels.
        if current_has_values and candidate_label and _known_label(candidate_label, rules):
            break
        if candidate_label and current_label and _known_label(candidate_label, rules) and (
            "现金流" in current_label
            or "资产总计" in candidate_label
            or "负债合计" in candidate_label
            or "资产合计" in candidate_label and ("流动资产" in current_label or "非流动资产" in current_label)
        ):
            break
        # A separate complete row label is never a continuation fragment.
        if candidate_label and _known_label(candidate_label, rules) and not _known_label(combined_label, rules):
            break
        variants.append(candidate)
        merged_probe = PdfRowAST(
            tuple(cell for row in variants for cell in row.cells),
            variants[0].top,
            variants[0].bbox,
        )
        if _known_label(_row_label_text(merged_probe), rules) and any(
            _parse_number(cell.text) is not None
            for row in variants
            for cell in row.cells
        ):
            break
    cells = tuple(cell for row in variants for cell in row.cells)
    return PdfRowAST(
        tuple(sorted(cells, key=lambda cell: (cell.top, cell.x0))),
        min(row.top for row in variants),
        (
            min(cell.x0 for cell in cells),
            min(cell.top for cell in cells),
            max(cell.x1 for cell in cells),
            max(cell.bottom for cell in cells),
        ),
    )


def _is_summary_page(page_text: str, rows: Sequence[PdfRowAST], rules: FinancialRulesSnapshot | None = None) -> bool:
    """Detect the metrics page even when PDF text wraps its labels."""
    compact = re.sub(r"\s+", "", page_text).casefold()
    labels = _labels_for_rules(rules)
    if any(label.casefold() in compact for label in labels["reported_roe"]):
        return True
    return any(
        any(label.casefold() in _row_label_text(_merge_visual_rows(rows, index, rules)).casefold() for label in labels["reported_roe"])
        for index in range(len(rows))
    )


def _fact_rank(fact: FinancialFact) -> int:
    """Rank a candidate by statement identity, not by document page order."""
    scope_rank = 1 if fact.consolidated_scope == "consolidated" else 0
    if fact.concept == "reported_roe":
        return 10 + scope_rank if fact.statement == "summary" else 2 + scope_rank
    if fact.statement in {"income_statement", "balance_sheet", "cash_flow"}:
        return 10 + scope_rank
    if fact.statement == "summary":
        return 1
    return 0


def _reconcile_candidates(
    local: Sequence[FinancialFact], local_refs: Sequence[EvidenceRef],
    vision: Sequence[FinancialFact], vision_refs: Sequence[EvidenceRef], *, prefer_vision: bool,
    validate: Callable[[list[FinancialFact], dict[str, EvidenceRef]], FinancialGroupValidation] | None = None,
) -> tuple[list[FinancialFact], list[EvidenceRef], list[FinancialFact]]:
    """Enumerate bounded local/vision combinations through the real quality gate."""
    refs = {ref.evidence_id.removeprefix("fact:"): ref for ref in tuple(local_refs) + tuple(vision_refs)}
    local_by: dict[str, FinancialFact] = {}
    vision_by: dict[str, FinancialFact] = {}
    for fact in local:
        if fact.concept in local_by:
            return list(local), list(local_refs), list(vision)
        local_by[fact.concept] = fact
    for fact in vision:
        if fact.concept in vision_by:
            return list(local), list(local_refs), list(vision)
        vision_by[fact.concept] = fact
    concepts = sorted(set(local_by) | set(vision_by))
    if len(concepts) > 7 or validate is None:
        return list(local), list(local_refs), list(vision)
    options: list[tuple[str, ...]] = []
    for concept in concepts:
        choices = []
        if concept in local_by:
            choices.append("local")
        if concept in vision_by:
            choices.append("vision")
        options.append(tuple(choices))
    combos: list[tuple[list[FinancialFact], list[EvidenceRef], int]] = []
    def walk(index: int, facts: list[FinancialFact], selected_refs: list[EvidenceRef], replacements: int) -> None:
        if len(combos) >= 128:
            return
        if index == len(concepts):
            evidence_map = {ref.evidence_id.removeprefix("fact:"): ref for ref in selected_refs}
            result = validate(facts, evidence_map)
            if result.validation.status in {ValidationStatus.VERIFIED, ValidationStatus.READY_WITH_WARNINGS} and result.validation.accepted:
                combos.append((list(facts), list(selected_refs), replacements))
            return
        concept = concepts[index]
        for source in options[index]:
            fact = local_by[concept] if source == "local" else vision_by[concept]
            ref = refs.get(fact.fact_id)
            if ref is None:
                continue
            walk(index + 1, facts + [fact], selected_refs + [ref], replacements + (source == "vision"))
    walk(0, [], [], 0)
    if not combos:
        return list(local), list(local_refs), list(vision)
    best = min(item[2] for item in combos)
    winners = [item for item in combos if item[2] == best]
    if len(winners) != 1:
        return list(local), list(local_refs), list(vision)
    return winners[0][0], winners[0][1], []


def _vision_page_preparation_worker(filing, config, validation_issues, existing_facts, period_header_pages,
                                    parser_failed_pages=()):
    """Pickle-safe page selection/slicing behind a killable deadline."""
    return _vision_failed_pages(filing, config, validation_issues, existing_facts,
                                period_header_pages=period_header_pages, parser_failed_pages=parser_failed_pages)


_VISION_PAGE_WORKER_ERROR_CODES = frozenset({
    "VISION_CANCELLED",
    "VISION_IMAGE_RENDER_FAILED",
    "VISION_IMAGE_RENDER_TIMEOUT",
    "VISION_IMAGE_REPRESENTATION_FAILED",
    "VISION_IMAGE_SIZE_LIMIT",
    "VISION_SIZE_LIMIT",
    "VISION_SOURCE_HASH_MISMATCH",
    "VISION_TIMEOUT",
})


def _vision_failed_pages(
    filing: FilingDocument, config: VisionFallbackConfig,
    validation_issues: Sequence[str] = (),
    existing_facts: Sequence[FinancialFact] = (),
    *, cancel_check: Callable[[], bool] | None = None,
    period_header_pages: Sequence[int] = (),
    parser_failed_pages: Sequence[int] = (),
    deadline: float | None = None,
) -> tuple[VisionPageRequest, ...]:
    """Create bounded one-page PDFs for failed statement contexts in memory."""
    if deadline is not None:
        from .vision_financials import VisionAdapterError
        if cancel_check is not None and cancel_check():
            raise VisionAdapterError("VISION_CANCELLED")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VisionAdapterError("VISION_TIMEOUT")
        # User callbacks stay in the host/consent Adapter; workers only read
        # original bytes and create the same bounded manifest, never upload.
        worker_config = replace(config, approve_upload=None)
        pages, error = _run_pdfium_isolated(
            _vision_page_preparation_worker, filing, worker_config,
            tuple(validation_issues), tuple(existing_facts), tuple(period_header_pages),
            tuple(parser_failed_pages),
            timeout_seconds=remaining, cancel_check=cancel_check,
        )
        if error == "PDFIUM_WORKER_CANCELLED":
            raise VisionAdapterError("VISION_CANCELLED")
        if error == "PDFIUM_WORKER_TIMEOUT" or time.monotonic() >= deadline:
            raise VisionAdapterError("VISION_TIMEOUT")
        if error:
            code = error if error in _VISION_PAGE_WORKER_ERROR_CODES else "VISION_PAGE_PREPARATION_UNAVAILABLE"
            raise VisionAdapterError(code)
        return tuple(pages or ())
    if not filing.local_path or not Path(filing.local_path).is_file():
        return ()
    # Prefer formal consolidated statement titles and only two adjacent
    # continuation pages. Classification is title-line based so an unrelated
    # page-wide mention of ``consolidated`` cannot admit a parent-only table.
    try:
        from pypdf import PdfReader, PdfWriter
        # Parse and render the same immutable bytes that passed the hash
        # check. Reopening the path after constructing PdfReader can bind an
        # older page to a newer file if another process replaces the cache.
        original_bytes = Path(filing.local_path).read_bytes()
        source_hash = hashlib.sha256(original_bytes).hexdigest()
        if source_hash != filing.content_hash:
            from .vision_financials import VisionAdapterError
            raise VisionAdapterError("VISION_SOURCE_HASH_MISMATCH")
        reader = PdfReader(BytesIO(original_bytes))
        titles = {
            "income_statement": (
                "consolidated income", "income statement", "statement of profit",
                "profit and loss", "profit or loss", "合并利润表", "合并损益表", "利润表",
            ),
            "balance_sheet": (
                "consolidated balance", "balance sheet", "statement of financial position",
                "合并资产负债表", "资产负债表",
            ),
            "cash_flow": (
                "consolidated statement of cash", "statement of cash flows",
                "cash flow statement", "cash flows from operating",
                "合并现金流量表", "现金流量表",
            ),
        }
        present = {fact.statement for fact in existing_facts}
        target = set(titles)
        if present and not validation_issues:
            target -= present
        elif validation_issues:
            issue_text = " ".join(str(item).lower() for item in validation_issues)
            issue_map = {
                "income_statement": ("income_statement_core_missing", "income_statement"),
                "cash_flow": ("cash_flow_core_missing", "cash_flow"),
                "balance_sheet": ("balance_sheet_core_missing", "balance_sheet", "balance_sheet_imbalance"),
            }
            indicated = {statement for statement, markers in issue_map.items() if any(marker in issue_text for marker in markers)}
            target = indicated or (set(titles) - present or set(titles))
        # Build formal boundaries from individual title lines.  Parent-only
        # titles remain boundaries but are not selected upload targets.
        parent_markers = (
            "parent company", "parent-company", "company only", "company-only",
            "separate financial", "separate statement", "separate accounts",
            "母公司", "单体", "个别财务报表", "个别报表",
        )
        page_texts = [(page.extract_text() or "") for page in reader.pages]
        # Reuse only a broad, explicit presentation declaration from this
        # exact original. It stays local and retains its original page/hash;
        # it is not appended to the uploaded PDF or fabricated on its table.
        from .financial_cloud_proof import CloudUnitContext
        from .financial_sources.document_units import discover_document_units
        unit_contexts = tuple(CloudUnitContext(item.page, item.excerpt, source_hash)
            for item in discover_document_units(page_texts).declarations
            if item.scope == "all")
        mapped_statements = _toc_statement_page_map(page_texts)
        formal_starts: list[tuple[int, str, bool]] = []
        for index, page_text in enumerate(page_texts):
            found: tuple[str, bool] | None = None
            lines = page_text.splitlines()
            for line_index, line in enumerate(lines):
                normalized = re.sub(r"\s+", " ", line).strip().casefold()
                statement = next(
                    (kind for kind, words in titles.items()
                     if any(word in normalized for word in words)),
                    None,
                )
                if statement is None:
                    continue
                title_context = re.sub(
                    r"\s+", " ", " ".join(lines[max(0, line_index - 2):line_index + 1])
                ).casefold()
                consolidated = "consolidated" in normalized or "合并" in normalized
                eligible = not (
                    any(marker in title_context for marker in parent_markers)
                    and not consolidated
                )
                found = (statement, eligible)
                break
            if found is not None:
                formal_starts.append((index, found[0], found[1]))
        chosen: list[int] = [
            page_number - 1
            for statement in sorted(target)
            for page_number in mapped_statements.get(statement, ())
        ]
        # These are hash-bound original header locators produced by the local
        # period prepass. Route them through the same consent/page/byte caps;
        # never create a second upload or change source identity.
        chosen.extend(page - 1 for page in period_header_pages
                      if type(page) is int and 1 <= page <= len(reader.pages))
        if not mapped_statements:
            starts: list[int] = [
                index for index, statement, eligible in formal_starts
                if statement in target and eligible
            ]
            for start in starts:
                for offset in range(3):
                    page_index = start + offset
                    if page_index >= len(reader.pages) or (
                        offset and any(other > start and other <= page_index for other, _, _ in formal_starts)
                    ):
                        break
                    if page_index not in chosen:
                        chosen.append(page_index)
        formal_chosen = list(dict.fromkeys(chosen))

        # Statement titles remain the primary targets, but a large share of
        # issuer-specific context lives in related notes and segment tables.
        # Select only pages tied to a currently incomplete statement and keep
        # this secondary scan small enough for the existing consent/byte cap.
        note_groups: dict[str, tuple[str, ...]] = {
            "segments": (
                "business segment", "operating segment", "segment information",
                "reportable segment", "分部信息", "分部資料", "业务分部", "業務分部",
                "分部报告", "分部報告",
            ),
            "capital": (
                "capital expenditure", "capital expenditures", "purchases of property",
                "purchase of property, plant", "construction in progress", "购建固定资产",
                "購建固定資產", "资本性支出", "資本性支出", "购置物业、厂房及设备",
                "購置物業、廠房及設備",
            ),
            "balance_notes": (
                "borrowings", "lease liabilities", "debt maturity", "interest-bearing debt",
                "trade receivables", "inventories", "应收账款", "應收帳款", "存货", "存貨",
                "借款", "租赁负债", "租賃負債", "债务到期", "債務到期",
            ),
            "cash_notes": (
                "operating cash flow", "cash generated from operations", "cash flows from",
                "经营活动现金流", "經營活動現金流", "经营性现金流", "經營性現金流",
            ),
        }
        wanted_note_groups: set[str] = set()
        if "income_statement" in target:
            wanted_note_groups.add("segments")
        if "cash_flow" in target:
            wanted_note_groups.update({"capital", "cash_notes"})
        if "balance_sheet" in target:
            wanted_note_groups.add("balance_notes")
        issue_text = " ".join(str(item).casefold() for item in validation_issues)
        if not wanted_note_groups and issue_text:
            wanted_note_groups.update({"segments", "capital", "balance_notes"})

        note_candidates: list[tuple[int, int]] = []
        if wanted_note_groups:
            contents_markers = ("table of contents", "contents", "目录", "目錄")
            for page_index, page_text in enumerate(page_texts):
                if page_index in formal_chosen:
                    continue
                normalized = re.sub(r"\s+", " ", page_text).casefold()
                if any(marker in normalized[:240] for marker in contents_markers):
                    continue
                score = sum(
                    1
                    for group in wanted_note_groups
                    if any(marker in normalized for marker in note_groups[group])
                )
                if score:
                    note_candidates.append((score, page_index))
        note_candidates.sort(key=lambda item: (-item[0], item[1]))
        note_limit = min(6, max(0, min(config.max_pages, VISION_MAX_PAGES) - len(formal_chosen)))
        ordered_chosen = formal_chosen + [
            page_index
            for _score, page_index in note_candidates[:note_limit]
            if page_index not in formal_chosen
        ]
        # A parser failure is a priority hint, not a new upload permission or
        # proof that every failed page is financial. Intersect the exact-source
        # failed window with the already justified statements/notes, preserving
        # the same page/byte caps and consent path. Header context stays local.
        failed = {page - 1 for page in parser_failed_pages
                  if type(page) is int and 1 <= page <= len(reader.pages)}
        ordered_chosen = ([page for page in ordered_chosen if page in failed]
                          + [page for page in ordered_chosen if page not in failed])
        selected: list[VisionPageRequest] = []
        total = 0
        for index in ordered_chosen:
            if cancel_check is not None and cancel_check():
                from .vision_financials import VisionAdapterError
                raise VisionAdapterError("VISION_CANCELLED")
            if len(selected) >= min(config.max_pages, VISION_MAX_PAGES):
                break
            writer = PdfWriter()
            writer.add_page(reader.pages[index])
            buffer = BytesIO()
            writer.write(buffer)
            payload = buffer.getvalue()
            if config.provider == "mineru_flash":
                from .vision_financials import default_pdf_to_image_pdf
                payload = default_pdf_to_image_pdf(payload, cancel_check=cancel_check)
            if total + len(payload) > config.max_bytes:
                from .vision_financials import VisionAdapterError
                raise VisionAdapterError("VISION_SIZE_LIMIT")
            selected.append(VisionPageRequest(index + 1, payload, filing.source_url,
                filing.primary_document, unit_contexts=unit_contexts))
            total += len(payload)
        return tuple(selected)
    except Exception as exc:
        from .vision_financials import VisionAdapterError
        if isinstance(exc, VisionAdapterError):
            raise
        return ()


def _emit_ingestion_progress(
    progress: Callable[..., None] | None,
    stage: str,
    current: int,
    total: int,
    filing: FilingDocument | None = None,
    *,
    status: str = "",
    error_code: str = "",
    elapsed_seconds: float = 0.0,
    window_index: int | None = None,
    window_total: int | None = None,
) -> None:
    """Emit optional per-filing detail without breaking legacy callbacks."""

    if progress is None:
        return
    detail = None
    if filing is not None:
        detail = {
            "filing_id": filing.document_id,
            "label": filing.primary_document or filing.accession_number or filing.document_id,
            "status": status or stage,
            "error_code": error_code,
            "elapsed_seconds": max(0.0, float(elapsed_seconds)),
        }
        if window_index is not None and window_total is not None:
            detail["window_index"] = max(0, int(window_index))
            detail["window_total"] = max(0, int(window_total))
    try:
        signature = inspect.signature(progress)
        accepts_detail = len(signature.parameters) >= 4
    except (TypeError, ValueError):
        accepts_detail = False
    if stage == "filing-status" and not accepts_detail:
        return
    if accepts_detail:
        progress(stage, current, total, detail)
    else:
        progress(stage, current, total)


def _shared_pdf_producer(engine, company, filing, manifest, flight, key, parse_timeout, batch_timeout):
    """The existing parser owns work; subscribers own only their waiting."""
    def orphaned():
        # Subscriber callbacks can take their own locks. Never invoke them
        # while holding the shared registry lock.
        with _PDF_FLIGHT_LOCK:
            if flight.retiring:
                return True
            participants = dict(flight.participants)
        now = time.monotonic()
        for cancelled, deadline in participants.values():
            try:
                if deadline > now and not (cancelled and cancelled()):
                    return False
            except Exception:
                continue
        with _PDF_FLIGHT_LOCK:
            # A new subscriber may have arrived while callbacks ran. Give it
            # a chance to keep the producer, instead of cancelling its work.
            if flight.participants.keys() == participants.keys():
                flight.retiring = True
            return flight.retiring

    def progress(stage, current, total, detail=None):
        with _PDF_FLIGHT_LOCK:
            flight.progress_sequence += 1
            flight.latest_progress = (stage, current, total, detail)

    result = ([], [], "pdf_window_failed:ProducerInterrupted")
    try:
        if orphaned():
            result = ([], [], "pdf_parse_cancelled")
        else:
            rows = _parse_local_pdfs_bounded_impl(engine, company, [filing], {filing.document_id: manifest},
                cancel_check=orphaned, progress=progress, max_workers=1,
                parse_timeout_seconds=parse_timeout, batch_timeout_seconds=batch_timeout,
                deadline=time.monotonic() + batch_timeout, _shared_producer=True)
            result = rows.get(filing.document_id, ([], [], "pdf_document_budget_exhausted_resumable"))
    except Exception as error:
        result = ([], [], f"pdf_window_failed:{type(error).__name__}")
    finally:
        with _PDF_FLIGHT_LOCK:
            flight.result = result
            flight.event.set()
            if _PDF_FLIGHTS.get(key) is flight:
                _PDF_FLIGHTS.pop(key, None)


def _parse_local_pdfs_bounded(engine, company, filings, manifests, *, parse=None,
        cancel_check=None, progress=None, max_workers=3, parse_timeout_seconds=120.0,
        batch_timeout_seconds=280.0, worker_entry=None, deadline=None):
    """Share identical native producers, with independently cancellable waits.

    Reuse the SAME bounded parsing/checkpoint/cache implementation below. No
    second parser, acceptance policy or cross-issuer result cache is added.
    Injected parsers/workers retain their existing synchronous test seam.
    """
    default = (parse is None and worker_entry is None and type(engine) is FinancialIngestionEngine
        and getattr(engine._parse_pdf_ast, "__func__", None) is globals().get("_ORIGINAL_PARSE_PDF_AST"))
    if not default:
        return _parse_local_pdfs_bounded_impl(engine, company, filings, manifests, parse=parse,
            cancel_check=cancel_check, progress=progress, max_workers=max_workers,
            parse_timeout_seconds=parse_timeout_seconds, batch_timeout_seconds=batch_timeout_seconds,
            worker_entry=worker_entry, deadline=deadline)
    end = time.monotonic() + max(.1, float(batch_timeout_seconds))
    if deadline is not None:
        end = min(end, deadline)
    remaining = list(filings)
    active, results = {}, {}
    slots = max(1, min(3, int(max_workers)))
    stage_completed: dict[str, set[str]] = {}
    total_filings = len({filing.document_id for filing in filings})

    def relay_progress(filing, args):
        stage, current, total, detail = args
        completed = stage_completed.setdefault(stage, set())
        if current >= total:
            completed.add(filing.document_id)
        detail = detail or {}
        _emit_ingestion_progress(progress, stage, len(completed), total_filings, filing,
            status=detail.get("status", stage), error_code=detail.get("error_code"),
            elapsed_seconds=detail.get("elapsed_seconds", 0),
            window_index=detail.get("window_index"), window_total=detail.get("window_total"))

    try:
        while remaining or active:
            # Harvest completed results before observing a deadline edge.
            for document_id, (flight, key, token, seq, filing) in list(active.items()):
                with _PDF_FLIGHT_LOCK:
                    event_seq, args = flight.progress_sequence, flight.latest_progress
                if (event_seq != seq and args and progress is not None
                        and not (cancel_check and cancel_check())):
                    relay_progress(filing, args)
                    active[document_id] = (flight, key, token, event_seq, filing)
                if flight.event.is_set():
                    results[document_id] = deepcopy(flight.result)
                    with _PDF_FLIGHT_LOCK:
                        flight.participants.pop(token, None)
                    del active[document_id]
            stopped = bool(cancel_check and cancel_check())
            if stopped or time.monotonic() >= end:
                code = "pdf_parse_cancelled" if stopped else "pdf_batch_budget_exhausted_resumable"
                for filing in remaining:
                    results[filing.document_id] = ([], [], code)
                for document_id in active:
                    results[document_id] = ([], [], code)
                break
            while remaining and len(active) < slots:
                filing = remaining.pop(0)
                manifest = manifests.get(filing.document_id)
                if manifest is None or not filing.local_path or not Path(filing.local_path).is_file():
                    continue
                try:
                    actual = engine._file_sha256(filing.local_path, cancel_check=cancel_check, deadline=end)
                except (OSError,TimeoutError):
                    results[filing.document_id] = ([], [], "pdf_file_unreadable")
                    continue
                if not filing.content_hash:
                    filing.content_hash = actual
                if actual.casefold() != filing.content_hash.casefold():
                    results.update(_parse_local_pdfs_bounded_impl(engine, company, [filing], manifests,
                        cancel_check=cancel_check, progress=progress, deadline=end, max_workers=1,
                        parse_timeout_seconds=parse_timeout_seconds,
                        batch_timeout_seconds=batch_timeout_seconds, _shared_producer=True))
                    continue
                identity = json.dumps(asdict(manifest), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                key = (engine._parse_cache_key(filing.local_path, company, filing, actual)
                       + ":" + hashlib.sha256(identity.encode()).hexdigest())
                token = object()
                with _PDF_FLIGHT_LOCK:
                    flight = _PDF_FLIGHTS.get(key)
                    owner = flight is None or flight.retiring
                    if owner:
                        flight = _PdfParseFlight(threading.Event())
                        _PDF_FLIGHTS[key] = flight
                    flight.participants[token] = (cancel_check, end)
                    active[filing.document_id] = (flight, key, token, 0, filing)
                    if owner:
                        try:
                            _PDF_PRODUCERS.submit(_shared_pdf_producer, engine, replace(company), replace(filing),
                                manifest, flight, key, max(.1, float(parse_timeout_seconds)),
                                max(.1, float(batch_timeout_seconds)))
                        except RuntimeError:
                            flight.result = ([], [], "pdf_window_failed:ProducerUnavailable")
                            flight.event.set()
                            _PDF_FLIGHTS.pop(key, None)
            if active:
                next(iter(active.values()))[0].event.wait(.02)
        return {filing.document_id: results[filing.document_id] for filing in filings
                if filing.document_id in results}
    finally:
        with _PDF_FLIGHT_LOCK:
            for flight, key, token, _seq, _filing in active.values():
                flight.participants.pop(token, None)


def _parse_local_pdfs_bounded_impl(
    engine: "FinancialIngestionEngine",
    company: Company,
    filings: Sequence[FilingDocument],
    manifests: dict[str, FilingManifest],
    *,
    parse: Callable[[str, Company, FilingDocument, FilingManifest], tuple[list[FinancialFact], list[EvidenceRef]]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[..., None] | None = None,
    max_workers: int = 3,
    parse_timeout_seconds: float = 120.0,
    batch_timeout_seconds: float = 280.0,
    worker_entry: Callable[..., None] | None = None,
    deadline: float | None = None,
    _shared_producer: bool = False,
) -> dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]]:
    """Parse local PDFs as bounded per-document pipelines.

    Indexing and AST parsing for a document are submitted as one streaming
    pipeline: when one index completes, only that document's AST task is
    queued. This avoids a global pre-index barrier while keeping all progress
    callbacks in the collecting thread. Results are always remapped to filing
    order before returning.
    """
    parse_fn = parse or engine._parse_pdf_ast
    batch_deadline = min(deadline, time.monotonic() + max(0.1, float(batch_timeout_seconds))) if deadline is not None else time.monotonic() + max(0.1, float(batch_timeout_seconds))
    # Subclass/instance parser injection remains a synchronous test seam;
    # only the unmodified base engine is eligible for process isolation.
    default_parser = (
        parse is None
        and type(engine) is FinancialIngestionEngine
        and isinstance(parse_fn, types.MethodType)
    )
    process_isolated = (
        default_parser
        and isinstance(parse_fn, types.MethodType)
        and getattr(parse_fn, "__func__", None) is globals().get("_ORIGINAL_PARSE_PDF_AST")
    )
    cache_enabled = engine._parse_cache_dir is not None
    unique: list[tuple[str, FilingDocument, FilingManifest]] = []
    duplicate_ids: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    actual_hashes: dict[str, str] = {}
    hash_mismatches: dict[str, str] = {}
    for filing in filings:
        if cancel_check is not None and cancel_check():
            return {}
        if time.monotonic() >= batch_deadline:
            return {item.document_id: ([], [], "pdf_batch_budget_exhausted_resumable") for item in filings}
        manifest = manifests.get(filing.document_id)
        if manifest is None or not filing.local_path or not Path(filing.local_path).is_file():
            continue
        if default_parser or cache_enabled:
            try:
                actual_hash = engine._file_sha256(filing.local_path, cancel_check=cancel_check, deadline=batch_deadline)
            except TimeoutError:
                return {item.document_id: ([], [], "pdf_batch_budget_exhausted_resumable") for item in filings}
            except OSError:
                continue
            key = f"hash:{actual_hash.casefold()}:source:{engine._parse_cache_key(filing.local_path, company, filing, actual_hash)}"
            actual_hashes[key] = actual_hash
            if filing.content_hash and filing.content_hash.casefold() != actual_hash.casefold():
                hash_mismatches[key] = "CACHE_HASH_MISMATCH"
        else:
            if filing.content_hash:
                key = f"hash:{filing.content_hash.casefold()}:source:{engine._parse_cache_key(filing.local_path, company, filing, filing.content_hash)}"
            else:
                try:
                    key = f"path:{str(Path(filing.local_path).resolve(strict=False)).casefold()}"
                except OSError:
                    key = f"path:{str(Path(filing.local_path)).casefold()}"
                key += ":source:" + engine._parse_cache_key(filing.local_path, company, filing, "")
        duplicate_ids.setdefault(key, []).append(filing.document_id)
        if key not in seen:
            seen[key] = filing.document_id
            unique.append((key, filing, manifest))
    if not unique or (cancel_check is not None and cancel_check()):
        return {}

    def parse_one(
        item: tuple[str, FilingDocument, FilingManifest],
        candidate_pages: frozenset[int] | None = None,
    ) -> tuple[str, list[FinancialFact], list[EvidenceRef], str | None]:
        key, filing, manifest = item
        if cancel_check is not None and cancel_check():
            return key, [], [], None
        try:
            if default_parser:
                facts, refs = parse_fn(
                    filing.local_path,
                    company,
                    filing,
                    manifest,
                    candidate_pages=candidate_pages,
                    index_precomputed=True,
                )
            else:
                facts, refs = parse_fn(filing.local_path, company, filing, manifest)
            return key, list(facts), list(refs), None
        except Exception as exc:
            return key, [], [], f"pdf_table_parse_failed:{type(exc).__name__}"

    workers = (
        _safe_pdf_worker_count([item[1] for item in unique], requested=max_workers)
        if default_parser
        else max(1, min(3, int(max_workers), len(unique)))
    )
    completed = 0
    total = len(unique)
    results: dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]] = {}
    uncached: list[tuple[str, FilingDocument, FilingManifest]] = []
    cache_keys: dict[str, str] = {}
    pre_errors: dict[str, str] = {}
    flight_waiters: dict[str, _PdfParseFlight] = {}
    flight_owners: dict[str, _PdfParseFlight] = {}
    cache_checked = 0
    # Hash verification happens before cache lookup. A stale database hash can
    # never make an unrelated object look like a valid parse-cache hit.
    for item in unique:
        key, filing, manifest = item
        cache_checked += 1
        _emit_ingestion_progress(
            progress,
            "cache-check",
            cache_checked,
            total,
            filing,
            status="cache-check",
        )
        if not cache_enabled and not default_parser:
            uncached.append(item)
            continue
        try:
            actual_hash = actual_hashes.get(key) or engine._file_sha256(filing.local_path)
            # A stored hash mismatch is an auditable cache failure. Preserve
            # the filing metadata so callers cannot mistake a stale reference
            # for a newly verified object; only hash-less filings are filled.
            if not filing.content_hash:
                filing.content_hash = actual_hash
            cache_key = engine._parse_cache_key(filing.local_path, company, filing, actual_hash)
            cache_keys[key] = cache_key
            cached = None if key in hash_mismatches else engine._load_parse_cache(cache_key)
            if key in hash_mismatches:
                pre_errors[key] = hash_mismatches[key]
        except (OSError, ValueError):
            cached = None
            cache_key = ""
        if cached is None:
            if default_parser and not _shared_producer and cache_key and key not in hash_mismatches:
                with _PDF_FLIGHT_LOCK:
                    flight = _PDF_FLIGHTS.get(cache_key)
                    if flight is None:
                        flight = _PdfParseFlight(threading.Event())
                        _PDF_FLIGHTS[cache_key] = flight
                        flight_owners[key] = flight
                    else:
                        flight_waiters[key] = flight
                        continue
            uncached.append(item)
            continue
        facts, refs = cached
        results[key] = (facts, refs, None)
        completed += 1
        _emit_ingestion_progress(
            progress,
            "filing-parse",
            completed,
            total,
            filing,
            status="cache-hit",
        )
    if not uncached:
        _resolve_pdf_parse_flights(
            flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds,
            deadline=batch_deadline, cancel_check=cancel_check,
        )
        return {
            document_id: results[key]
            for key, document_ids in duplicate_ids.items()
            if key in results
            for document_id in document_ids
        }

    if process_isolated:
        if engine._checkpoint_dir is not None and not hash_mismatches:
            # Opt-in durable window recovery.  The default process scheduler
            # remains unchanged; callers that provide a checkpoint directory
            # get an ordered checkpoint boundary before compiler validation.
            # Each document is an independent pipeline, so checkpoint mode is
            # allowed to overlap documents.  Window order and table context
            # remain strictly sequential inside parse_local_pdf_resumable.
            def parse_checkpointed(
                item: tuple[str, FilingDocument, FilingManifest],
            ) -> tuple[str, list[FinancialFact], list[EvidenceRef], str | None]:
                key, filing, manifest = item
                parse_started = time.monotonic()

                def window_progress(index: int, count: int, status: str) -> None:
                    if time.monotonic() >= batch_deadline or (cancel_check is not None and cancel_check()):
                        return
                    _emit_ingestion_progress(
                        progress,
                        "filing-window",
                        index,
                        count,
                        filing,
                        status=f"window-{status}",
                        elapsed_seconds=time.monotonic() - parse_started,
                        window_index=index,
                        window_total=count,
                    )

                try:
                    facts, refs, window_diagnostics = engine.parse_local_pdf_resumable(
                        company, filing, manifest,
                        cancel_check=cancel_check,
                        progress=window_progress,
                        deadline=batch_deadline,
                    )
                    error = window_diagnostics[0] if window_diagnostics else None
                except Exception as exc:
                    facts, refs, error = [], [], f"pdf_window_failed:{type(exc).__name__}"
                return key, facts, refs, error or pre_errors.get(key)

            checkpoint_workers = max(1, min(workers, len(uncached)))
            checkpoint_futures: dict[Any, tuple[str, FilingDocument, FilingManifest]] = {}
            checkpoint_executor = ThreadPoolExecutor(
                max_workers=checkpoint_workers,
                thread_name_prefix="financial-checkpoint",
            )

            checkpoint_next_item = 0

            def submit_checkpoint() -> None:
                nonlocal checkpoint_next_item
                if (
                    checkpoint_next_item >= len(uncached)
                    or time.monotonic() >= batch_deadline
                    or (cancel_check is not None and cancel_check())
                ):
                    return
                item = uncached[checkpoint_next_item]
                checkpoint_next_item += 1
                checkpoint_futures[checkpoint_executor.submit(parse_checkpointed, item)] = item

            def harvest_checkpoint(done_futures: set[Any]) -> None:
                for future in done_futures:
                    item = checkpoint_futures.pop(future, None)
                    if item is None:
                        continue
                    key, _filing, _manifest = item
                    if future.cancelled():
                        results[key] = ([], [], "pdf_parse_cancelled")
                        continue
                    try:
                        parsed_key, facts, refs, error = future.result()
                    except Exception as exc:
                        parsed_key, facts, refs, error = key, [], [], f"pdf_window_failed:{type(exc).__name__}"
                    results[parsed_key] = (facts, refs, error)
                    if error is None and facts and refs:
                        cache_key = cache_keys.get(parsed_key)
                        if cache_key:
                            engine._store_parse_cache(cache_key, facts, refs)

            try:
                for _ in range(checkpoint_workers):
                    submit_checkpoint()
                while checkpoint_futures:
                    done, _ = wait(tuple(checkpoint_futures), timeout=0.05, return_when=FIRST_COMPLETED)
                    # A future can complete just after wait() forms its done
                    # set. Include every future that is already complete before
                    # applying the cutoff to the remaining cohort.
                    harvest_checkpoint(set(done) | {
                        future for future in checkpoint_futures if future.done()
                    })
                    # Completed durable work belongs to this attempt even if
                    # the clock/cancel signal changed during wait(). Only the
                    # unfinished cohort gets a resumable budget diagnostic.
                    expired = time.monotonic() >= batch_deadline
                    cancelled = cancel_check is not None and cancel_check()
                    if expired or cancelled:
                        # One final nonblocking pass closes the completion race
                        # at the exact batch cutoff without extending the wait.
                        harvest_checkpoint({
                            future for future in checkpoint_futures if future.done()
                        })
                        for pending, item in checkpoint_futures.items():
                            pending.cancel()
                            results[item[0]] = ([], [], "pdf_parse_cancelled" if cancelled
                                               else "pdf_batch_budget_exhausted_resumable")
                        for key, _filing, _manifest in uncached[checkpoint_next_item:]:
                            results.setdefault(
                                key,
                                ([], [], "pdf_parse_cancelled" if cancelled
                                 else "pdf_batch_budget_exhausted_resumable"),
                            )
                        break
                    for _ in range(min(checkpoint_workers - len(checkpoint_futures), len(uncached))):
                        submit_checkpoint()
                stopped = cancel_check is not None and cancel_check()
                expired = time.monotonic() >= batch_deadline
                if stopped or expired:
                    reason = "pdf_parse_cancelled" if stopped else "pdf_batch_budget_exhausted_resumable"
                    for key, _filing, _manifest in uncached:
                        results.setdefault(key, ([], [], reason))
                # Progress is intentionally emitted in filing order, not
                # completion order, so callers receive monotonic counters even
                # when a later document finishes first.
                for index, item in enumerate(uncached, start=1):
                    key, filing, _manifest = item
                    if key not in results:
                        continue
                    error = results[key][2]
                    _emit_ingestion_progress(
                        progress, "filing-parse", completed + index, total, filing,
                        status=("cancelled" if error == "pdf_parse_cancelled"
                                else "needs-action" if error and ("timeout" in error or "budget_exhausted" in error)
                                else "failed" if error else "parsed"),
                        error_code=error or "",
                    )
            finally:
                # Native work owns killable process deadlines. The collector
                # cannot let a broken/custom worker's shutdown hide its budget.
                checkpoint_executor.shutdown(wait=not checkpoint_futures, cancel_futures=True)
            _resolve_pdf_parse_flights(
                flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds,
                deadline=batch_deadline, cancel_check=cancel_check,
            )
            return {
                document_id: results[key]
                for key, document_ids in duplicate_ids.items()
                if key in results
                for document_id in document_ids
            }
        try:
            _parse_local_pdfs_isolated(
                engine,
                company,
                uncached,
                results=results,
                cache_keys=cache_keys,
                pre_errors=pre_errors,
                progress=progress,
                cancel_check=cancel_check,
                max_workers=workers,
                parse_timeout_seconds=parse_timeout_seconds,
                batch_timeout_seconds=max(0.1, batch_deadline - time.monotonic()),
                worker_entry=worker_entry,
            )
        finally:
            # Every owner wakes waiters, including cancellation and process
            # startup failures. Never leave an in-flight entry behind.
            _resolve_pdf_parse_flights(
                flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds,
                deadline=batch_deadline, cancel_check=cancel_check,
            )
        return {
            document_id: results[key]
            for key, document_ids in duplicate_ids.items()
            if key in results
            for document_id in document_ids
        }

    workers = min(workers, len(uncached))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="financial-pdf")
    futures: dict[Any, tuple[str, tuple[str, FilingDocument, FilingManifest]]] = {}
    cancelled = False
    next_item = 0
    indexed = 0
    parsed = completed

    def _index_one(
        item: tuple[str, FilingDocument, FilingManifest],
    ) -> tuple[str, frozenset[int] | None, str | None]:
        key, filing, _manifest = item
        if cancel_check is not None and cancel_check():
            return key, None, None
        try:
            return key, _candidate_financial_pages(filing.local_path), None
        except Exception as exc:
            return key, None, f"pdf_index_failed:{type(exc).__name__}"

    def submit_index() -> None:
        nonlocal next_item
        if (
            next_item >= len(uncached)
            or time.monotonic() >= batch_deadline
            or (cancel_check is not None and cancel_check())
        ):
            return
        item = uncached[next_item]
        next_item += 1
        futures[executor.submit(_index_one, item)] = ("index", item)

    def submit_parse(item: tuple[str, FilingDocument, FilingManifest], candidate_pages=None) -> bool:
        if time.monotonic() >= batch_deadline or (cancel_check is not None and cancel_check()):
            return False
        futures[executor.submit(parse_one, item, candidate_pages)] = ("parse", item)
        return True

    def harvest_thread_future(future: Any) -> None:
        nonlocal indexed, parsed, completed
        kind_item = futures.pop(future, None)
        if kind_item is None or future.cancelled():
            return
        kind, item = kind_item
        key = item[0]
        filing = item[1]
        try:
            if kind == "index":
                key, candidate_pages, index_error = future.result()
                indexed += 1
                _emit_ingestion_progress(progress, "filing-index", indexed, total, filing,
                                         status="local-parsing")
                if index_error is not None:
                    results[key] = ([], [], index_error)
                    parsed += 1
                    _emit_ingestion_progress(progress, "filing-parse", parsed, total, filing,
                                             status="failed", error_code=index_error)
                elif not submit_parse(item, candidate_pages):
                    reason = "pdf_parse_cancelled" if cancel_check is not None and cancel_check() else "pdf_batch_budget_exhausted_resumable"
                    results[key] = ([], [], reason)
                    parsed += 1
                    _emit_ingestion_progress(
                        progress, "filing-parse", parsed, total, filing,
                        status="cancelled" if reason == "pdf_parse_cancelled" else "needs-action",
                        error_code=reason,
                    )
                if not (cancel_check is not None and cancel_check()):
                    submit_index()
            else:
                key, facts, refs, error = future.result()
                results[key] = (facts, refs, error)
                parsed += 1
                completed += 1
                if error is None and facts and refs and cache_enabled:
                    cache_key = cache_keys.get(key)
                    if cache_key:
                        engine._store_parse_cache(cache_key, facts, refs)
                _emit_ingestion_progress(
                    progress, "filing-parse", parsed, total, filing,
                    status=("needs-action" if error and ("timeout" in error or "budget_exhausted" in error)
                            else "failed" if error else "parsed"),
                    error_code=error or "",
                )
                if not default_parser:
                    submit_custom_parse()
        except CancelledError:
            return

    def submit_custom_parse() -> None:
        nonlocal next_item
        if (
            next_item >= len(uncached)
            or time.monotonic() >= batch_deadline
            or (cancel_check is not None and cancel_check())
        ):
            return
        item = uncached[next_item]
        next_item += 1
        futures[executor.submit(parse_one, item)] = ("parse", item)

    try:
        if default_parser:
            for _ in range(min(workers, len(uncached))):
                submit_index()
        else:
            for _ in range(min(workers, len(uncached))):
                submit_custom_parse()
        while futures:
            done, _ = wait(tuple(futures), timeout=0.05, return_when=FIRST_COMPLETED)
            # Completion may race wait()'s snapshot. Drain every future that is
            # now complete before deciding that the batch budget has expired.
            ready = set(done) | {future for future in futures if future.done()}
            for future in ready:
                harvest_thread_future(future)
            cancelled = cancel_check is not None and cancel_check()
            if cancelled or time.monotonic() >= batch_deadline:
                reason = "pdf_parse_cancelled" if cancelled else "pdf_batch_budget_exhausted_resumable"
                # One last nonblocking scan handles futures completing while
                # the first ready set was being consumed.
                for future in tuple(futures):
                    if future.done():
                        harvest_thread_future(future)
                for pending, (_kind, item) in tuple(futures.items()):
                    pending.cancel()
                    if item[0] not in results:
                        results[item[0]] = ([], [], reason)
                        _emit_ingestion_progress(
                            progress, "filing-parse", parsed + 1, total, item[1][1],
                            status="cancelled" if cancelled else "needs-action",
                            error_code=reason,
                        )
                for key, filing, _manifest in uncached:
                    if key not in results:
                        results[key] = ([], [], reason)
                        _emit_ingestion_progress(
                            progress, "filing-parse", parsed + 1, total, filing,
                            status="cancelled" if cancelled else "needs-action",
                            error_code=reason,
                        )
                break
    finally:
        if cancel_check is not None and cancel_check():
            cancelled = True
            for pending in tuple(futures):
                if not pending.done():
                    pending.cancel()
        # Production native workers have their own killable deadlines. An
        # injected/custom parser must not make collector shutdown unbounded;
        # it may finish later, but cannot publish cache or progress here.
        executor.shutdown(wait=not futures, cancel_futures=True)

    stopped = cancelled or (cancel_check is not None and cancel_check())
    expired = time.monotonic() >= batch_deadline
    if stopped or expired:
        reason = "pdf_parse_cancelled" if stopped else "pdf_batch_budget_exhausted_resumable"
        status = "cancelled" if stopped else "needs-action"
        for key, filing, _manifest in uncached:
            if key not in results:
                results[key] = ([], [], reason)
                _emit_ingestion_progress(
                    progress, "filing-parse", parsed + 1, total, filing,
                    status=status, error_code=reason,
                )

    by_document: dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]] = {}
    _resolve_pdf_parse_flights(
        flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds,
        deadline=batch_deadline, cancel_check=cancel_check,
    )
    for key, document_ids in duplicate_ids.items():
        result = results.get(key)
        if result is None:
            continue
        for document_id in document_ids:
            by_document[document_id] = result
    return by_document


def _parse_pdf_process_worker(
    key: str,
    company: Company,
    filing: FilingDocument,
    manifest: FilingManifest,
    candidate_pages: frozenset[int] | None,
    rules: FinancialRulesSnapshot | None = None,
    document_units: DocumentUnitResolution | None = None,
) -> tuple[str, list[FinancialFact], list[EvidenceRef], str | None]:
    """Pickle-safe worker used only for CPU-heavy local statement pages."""

    try:
        facts, refs = FinancialIngestionEngine(compatibility_rules=rules)._parse_pdf_ast(
            filing.local_path,
            company,
            filing,
            manifest,
            candidate_pages=candidate_pages,
            index_precomputed=True,
            document_units=document_units,
        )
        return key, list(facts), list(refs), None
    except Exception as exc:
        return key, [], [], f"pdf_table_parse_failed:{type(exc).__name__}"


def _parse_pdf_process_worker_entry(
    key: str,
    company: Company,
    filing: FilingDocument,
    manifest: FilingManifest,
    candidate_pages: frozenset[int] | None,
    result_queue: Any,
    rules: FinancialRulesSnapshot | None = None,
    index_precomputed: bool = False,
    precomputed_diagnostic: str | None = None,
    precomputed_units: DocumentUnitResolution | None = None,
    precomputed_texts: tuple[str, ...] | None = None,
) -> None:
    """Process entrypoint which returns one bounded, pickle-safe result."""

    try:
        index_error = None
        index_diagnostic = precomputed_diagnostic
        page_texts = precomputed_texts
        if page_texts is not None:
            remember_page_texts(filing.local_path, page_texts)
        if index_precomputed:
            indexed_pages = candidate_pages
        else:
            try:
                indexed_pages, index_diagnostic = _candidate_financial_pages_with_diagnostic(
                    filing.local_path, rules=rules
                )
                page_texts = cached_page_texts(filing.local_path)
            except Exception as exc:
                indexed_pages = None
                index_error = f"pdf_index_failed:{type(exc).__name__}"
        result_queue.put(("filing-index", key, indexed_pages, index_error, index_diagnostic, page_texts))
        # A partial index is deliberately represented by None; the AST parser
        # then fails open to its full-document path for correctness.
        result_queue.put(("filing-result", _parse_pdf_process_worker(
            key, company, filing, manifest, indexed_pages, rules,
            precomputed_units or (discover_document_units(page_texts) if page_texts is not None else None),
        )))
    except BaseException as exc:  # pragma: no cover - process boundary safety
        try:
            result_queue.put(("filing-result", (key, [], [], f"pdf_worker_failed:{type(exc).__name__}")))
        except BaseException:
            pass


def _terminate_pdf_process(process: Any) -> None:
    """Terminate one parser process and wait briefly for OS resource cleanup."""

    try:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1.0)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=1.0)
    except (OSError, ValueError):
        pass


def _close_pdf_result_queue(result_queue: Any) -> None:
    try:
        result_queue.close()
        result_queue.join_thread()
    except (OSError, ValueError, AssertionError):
        pass


def _resolve_pdf_parse_flights(
    owners: dict[str, _PdfParseFlight],
    waiters: dict[str, _PdfParseFlight],
    cache_keys: dict[str, str],
    results: dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]],
    timeout_seconds: float,
    *, deadline: float | None = None, cancel_check: Callable[[], bool] | None = None,
) -> None:
    """Publish owner outcomes and release all waiters on every exit path."""

    for key, flight in owners.items():
        result = results.get(key, ([], [], "pdf_parse_cancelled"))
        with _PDF_FLIGHT_LOCK:
            flight.result = result
            flight.event.set()
            flight_key = cache_keys.get(key, "")
            if _PDF_FLIGHTS.get(flight_key) is flight:
                _PDF_FLIGHTS.pop(flight_key, None)
    wait_deadline = min(deadline, time.monotonic() + max(0.1, float(timeout_seconds))) if deadline is not None else time.monotonic() + max(0.1, float(timeout_seconds))
    for key, flight in waiters.items():
        while not flight.event.is_set() and time.monotonic() < wait_deadline:
            if cancel_check is not None and cancel_check():
                break
            flight.event.wait(timeout=min(0.05, max(0.001, wait_deadline - time.monotonic())))
        if flight.result is not None:
            results[key] = flight.result
        else:
            results[key] = ([], [], "pdf_singleflight_pending_resumable")


def _parse_local_pdfs_isolated(
    engine: "FinancialIngestionEngine",
    company: Company,
    uncached: Sequence[tuple[str, FilingDocument, FilingManifest]],
    *,
    results: dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]],
    cache_keys: dict[str, str],
    pre_errors: dict[str, str],
    progress: Callable[..., None] | None,
    cancel_check: Callable[[], bool] | None,
    max_workers: int,
    parse_timeout_seconds: float,
    batch_timeout_seconds: float,
    worker_entry: Callable[..., None] | None = None,
) -> None:
    """Run the default AST stage in killable per-document processes.

    Each worker owns the bounded text index prepass and AST parse in one
    ``multiprocessing.Process``. Cancellation and timeout can therefore
    terminate a native PDF parse rather than waiting for a thread to return.
    """

    worker_count = max(1, min(3, int(max_workers), len(uncached)))
    total = len(uncached) + sum(1 for value in results.values() if value[2] is None)
    parsed = sum(1 for value in results.values() if value[2] is None)
    indexed = 0
    next_item = 0
    cancelled = False
    processes: dict[str, dict[str, Any]] = {}
    context = mp.get_context("spawn")
    timeout = max(0.1, float(parse_timeout_seconds))
    batch_deadline = time.monotonic() + max(0.1, float(batch_timeout_seconds))
    entrypoint = worker_entry or _parse_pdf_process_worker_entry
    exit_drain_seconds = 0.2

    def start_next() -> None:
        nonlocal next_item, parsed, cancelled
        if cancel_check is not None and cancel_check():
            cancelled = True
            return
        while (
            next_item < len(uncached)
            and len(processes) < worker_count
            and not cancelled
            and time.monotonic() < batch_deadline
        ):
            item = uncached[next_item]
            next_item += 1
            key, filing, manifest = item
            if cancel_check is not None and cancel_check():
                return
            # The injectable worker seam keeps its historical argument
            # contract; parent-side reuse belongs to the production worker.
            cached_texts = (
                cached_page_texts(filing.local_path)
                if worker_entry is None
                else None
            )
            precomputed_pages = None
            precomputed_diagnostic = None
            if cached_texts is not None:
                precomputed_pages = _candidate_pages_from_text(
                    cached_texts,
                    continuation_pages=3,
                    rules=engine._compatibility_rules,
                )
                precomputed_diagnostic = _scanned_image_diagnostic(
                    cached_texts, rules=engine._compatibility_rules
                )
            result_queue = context.Queue(maxsize=2)
            worker_args = (
                key,
                company,
                filing,
                manifest,
                precomputed_pages,
                result_queue,
            )
            if worker_entry is None:
                worker_args = (
                    *worker_args,
                    engine._compatibility_rules,
                    cached_texts is not None,
                    precomputed_diagnostic,
                    discover_document_units(cached_texts) if cached_texts is not None else None,
                    cached_texts,
                )
            process = context.Process(
                target=entrypoint,
                args=worker_args,
                name=f"financial-pdf-{key[-12:]}",
            )
            process.daemon = True
            if cancel_check is not None and cancel_check():
                _close_pdf_result_queue(result_queue)
                results[key] = ([], [], "pdf_parse_cancelled")
                parsed += 1
                _emit_ingestion_progress(
                    progress, "filing-parse", parsed, total, filing,
                    status="cancelled", error_code="pdf_parse_cancelled",
                )
                cancelled = True
                return
            if time.monotonic() >= batch_deadline:
                _close_pdf_result_queue(result_queue)
                results[key] = ([], [], "pdf_batch_timeout")
                parsed += 1
                _emit_ingestion_progress(
                    progress, "filing-parse", parsed, total, filing,
                    status="needs-action", error_code="pdf_batch_timeout",
                )
                continue
            try:
                process.start()
            except (OSError, RuntimeError) as exc:
                _close_pdf_result_queue(result_queue)
                results[key] = ([], [], f"pdf_worker_start_failed:{type(exc).__name__}")
                continue
            processes[key] = {
                "process": process,
                "queue": result_queue,
                "deadline": time.monotonic() + timeout,
                "item": item,
                "indexed": False,
                "index_diagnostic": None,
                "started_at": time.monotonic(),
            }
            _emit_ingestion_progress(
                progress, "filing-status", indexed, total, filing, status="indexing"
            )

    def finish_process(key: str, result: tuple[str, list[FinancialFact], list[EvidenceRef], str | None]) -> None:
        nonlocal parsed
        state = processes.pop(key)
        process = state["process"]
        result_queue = state["queue"]
        try:
            process.join(timeout=0.2)
        finally:
            _close_pdf_result_queue(result_queue)
        _result_key, facts, refs, error = result
        index_diagnostic = state.get("index_diagnostic")
        if index_diagnostic:
            error = f"{error};{index_diagnostic}" if error else index_diagnostic
        error = pre_errors.get(key) or error
        results[key] = (list(facts), list(refs), error)
        parsed += 1
        if error is None and facts and refs:
            cache_key = cache_keys.get(key)
            if cache_key:
                engine._store_parse_cache(cache_key, facts, refs)
        filing = state["item"][1]
        _emit_ingestion_progress(
            progress,
            "filing-parse",
            parsed,
            total,
            filing,
            status=("needs-action" if error and ("timeout" in error or "budget_exhausted" in error)
                    else "failed" if error else "parsed"),
            error_code=error or "",
            elapsed_seconds=time.monotonic() - state["started_at"],
        )

    def consume_message(key: str, state: dict[str, Any], message: tuple[Any, ...]) -> bool:
        """Apply one ordered worker message; return True after a final result."""
        nonlocal indexed
        kind = message[0]
        if kind == "filing-index":
            filing = state["item"][1]
            if len(message) > 5 and message[5] is not None:
                remember_page_texts(filing.local_path, message[5])
            if not state["indexed"]:
                state["indexed"] = True
                if len(message) > 4 and message[4]:
                    state["index_diagnostic"] = str(message[4])
                indexed += 1
                _emit_ingestion_progress(
                    progress,
                    "filing-index",
                    indexed,
                    total,
                    filing,
                    status="local-parsing",
                    elapsed_seconds=time.monotonic() - state["started_at"],
                )
            return False
        if kind == "filing-result":
            finish_process(key, message[1])
            return True
        return False

    def drain_available(key: str, state: dict[str, Any]) -> bool:
        """Drain currently available queue messages without blocking."""
        while key in processes:
            try:
                message = state["queue"].get_nowait()
            except queue.Empty:
                return False
            if consume_message(key, state, message):
                return True
        return key not in processes

    def drain_before_stop(key: str, state: dict[str, Any]) -> bool:
        """Give the multiprocessing queue feeder a short, bounded handoff."""
        if drain_available(key, state):
            return True
        # Queue feeder delivery can lag the child process's final put. A pair
        # of short reads is enough to collect index-then-result without making
        # shutdown depend on an unbounded worker wait.
        for _ in range(2):
            if key not in processes:
                return True
            try:
                message = state["queue"].get(timeout=0.025)
            except queue.Empty:
                continue
            if consume_message(key, state, message):
                return True
            if drain_available(key, state):
                return True
        return key not in processes

    try:
        start_next()
        while processes:
            progressed = False
            now = time.monotonic()
            cancelled = cancel_check is not None and cancel_check()
            batch_expired = now >= batch_deadline
            # Drain already-published index/result messages before acting on
            # cancellation or the batch cutoff. The multiprocessing queue is
            # ordered, so a queued index is applied before its result.
            for key, state in tuple(processes.items()):
                if drain_available(key, state):
                    progressed = True
                    start_next()
            if cancelled or batch_expired:
                timeout_code = "pdf_parse_cancelled" if cancelled else "pdf_batch_timeout"
                timeout_status = "cancelled" if cancelled else "needs-action"
                for key, state in tuple(processes.items()):
                    if drain_before_stop(key, state):
                        start_next()
                for key, state in tuple(processes.items()):
                    _terminate_pdf_process(state["process"])
                    _close_pdf_result_queue(state["queue"])
                    processes.pop(key, None)
                    results[key] = ([], [], timeout_code)
                    parsed += 1
                    filing = state["item"][1]
                    _emit_ingestion_progress(
                        progress,
                        "filing-parse",
                        parsed,
                        total,
                        filing,
                        status=timeout_status,
                        error_code=timeout_code,
                        elapsed_seconds=time.monotonic() - state["started_at"],
                    )
                break
            for key, state in tuple(processes.items()):
                if key not in processes:
                    continue
                if not state["process"].is_alive():
                    # ``multiprocessing.Queue`` uses a feeder thread. A child
                    # can therefore report not-alive before its final put is
                    # visible to the parent. Give that bounded handoff a short
                    # drain window before converting a normal result into an
                    # exit failure.
                    drain_deadline = time.monotonic() + exit_drain_seconds
                    final_result = False
                    while key in processes and time.monotonic() < drain_deadline:
                        try:
                            pending_message = state["queue"].get(
                                timeout=max(0.001, drain_deadline - time.monotonic())
                            )
                        except queue.Empty:
                            continue
                        if consume_message(key, state, pending_message):
                            final_result = True
                            break
                    if final_result:
                        progressed = True
                        start_next()
                    elif key in processes:
                        finish_process(key, (key, [], [], "pdf_worker_exit_failed"))
                        start_next()
                    progressed = True
                elif now >= state["deadline"]:
                    if drain_before_stop(key, state):
                        start_next()
                        continue
                    _terminate_pdf_process(state["process"])
                    _close_pdf_result_queue(state["queue"])
                    processes.pop(key, None)
                    results[key] = ([], [], "pdf_parse_timeout")
                    parsed += 1
                    filing = state["item"][1]
                    _emit_ingestion_progress(
                        progress,
                        "filing-parse",
                        parsed,
                        total,
                        filing,
                        status="needs-action",
                        error_code="pdf_parse_timeout",
                        elapsed_seconds=time.monotonic() - state["started_at"],
                    )
                    start_next()
            if not progressed:
                time.sleep(0.01)
        if time.monotonic() >= batch_deadline or cancelled:
            timeout_code = "pdf_parse_cancelled" if cancelled else "pdf_batch_timeout"
            timeout_status = "cancelled" if cancelled else "needs-action"
            for key, filing, _manifest in uncached[next_item:]:
                results[key] = ([], [], timeout_code)
                parsed += 1
                _emit_ingestion_progress(
                    progress,
                    "filing-parse",
                    parsed,
                    total,
                    filing,
                    status=timeout_status,
                    error_code=timeout_code,
                )
    finally:
        for key, state in tuple(processes.items()):
            _terminate_pdf_process(state["process"])
            _close_pdf_result_queue(state["queue"])
            results.setdefault(key, ([], [], "pdf_parse_cancelled" if cancelled else "pdf_worker_exit_failed"))


def _safe_pdf_worker_count(
    filings: Sequence[FilingDocument], *, requested: int
) -> int:
    """Choose a bounded worker count from per-file, not batch, size.

    A single unusually large report can lower its own scheduling pressure, but
    it must not serialize unrelated reports in the same research request.
    """

    count = len(filings)
    if count <= 1:
        return count
    largest_bytes = 0
    for filing in filings:
        try:
            largest_bytes = max(largest_bytes, Path(filing.local_path).stat().st_size)
        except OSError:
            return 1
    bounded = max(1, min(3, int(requested), count))
    # Keep two lanes for a very large individual PDF; the memory budget is
    # intentionally per-file so small reports can still make progress.
    if largest_bytes > 64 * 1024 * 1024:
        return min(2, bounded)
    return bounded


def _is_deterministic_pdf_window_failure(error: Exception) -> bool:
    """Classify failures safe to short-circuit for an identical PDF input.

    External/process lifecycle failures must remain retryable.  Parser and
    document-shape failures are deterministic for the same content hash,
    parser version, rules and identity digest and may be marked exhausted.
    """
    if isinstance(error, (TimeoutError, PermissionError, OSError, CancelledError)):
        return False
    text = f"{type(error).__name__} {error}".casefold()
    transient_markers = (
        "timeout", "timed out", "network", "connection", "rate limit",
        "too many request", "spawn", "process start", "worker exited",
        "cancel", "resource temporarily", "broken pipe",
    )
    if any(marker in text for marker in transient_markers):
        return False
    # Unknown native/host failures remain resumable. Only a typed parser/shape
    # failure (including the safe worker envelope) fences identical input.
    return (isinstance(error, (ValueError, TypeError, IndexError, KeyError))
            or any(marker in text for marker in (
                "valueerror:", "typeerror:", "indexerror:", "keyerror:",
                "pdfreaderror:", "pdfstreamerror:", "malformed pdf", "invalid pdf",
                "unsupported document shape")))


def _toc_statement_page_map(
    page_texts: Sequence[str],
    *,
    rules: FinancialRulesSnapshot | None = None,
) -> dict[str, tuple[int, ...]]:
    """Resolve audited-statement print ranges to one-based PDF pages.

    Image-only statements have no searchable title.  Some audited reports do,
    however, provide an exact contents page followed by an audit report whose
    printed page number establishes a stable offset.  We use that mapping only
    when all three consolidated statements are present, the audit anchor is
    independently confirmed on a nearby physical page, and every range is
    small and inside the document.  This keeps scanned primary statements
    eligible for vision fallback without admitting arbitrary blank pages.
    """

    page_count = len(page_texts)
    range_pattern = re.compile(r"(\d{1,3})(?:\s*[-\u2013\u2014]\s*(\d{1,3}))?\s*$")
    for toc_index, text in enumerate(page_texts):
        compact = re.sub(r"\s+", "", text).casefold()
        if not (
            ("目录" in compact or "contents" in compact)
            and ("页次" in compact or "pageno" in compact or "page" in compact)
            and ("审计报告" in compact or "auditor" in compact)
        ):
            continue
        print_ranges: dict[str, tuple[int, int]] = {}
        audit_start: int | None = None
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            match = range_pattern.search(line)
            if match is None:
                continue
            start = int(match.group(1))
            end = int(match.group(2) or match.group(1))
            if start < 1 or end < start or end - start > 8:
                continue
            label = line[:match.start()].strip()
            folded = re.sub(r"\s+", "", label).casefold()
            if "审计报告" in folded or "auditor" in folded:
                audit_start = start
                continue
            context = _statement_context(label, rules)
            if context is not None and context[1] == "consolidated":
                print_ranges[context[0]] = (start, end)
        if audit_start is None or set(print_ranges) != {
            "income_statement", "balance_sheet", "cash_flow"
        }:
            continue

        # The first nearby audit-report page must expose the same printed page
        # number near its header; a TOC alone is not enough to establish an
        # offset because annual-report and embedded-report numbering differ.
        audit_physical: int | None = None
        upper = min(page_count, toc_index + 13)
        for physical_index in range(toc_index + 1, upper):
            lines = [
                re.sub(r"\s+", " ", item).strip()
                for item in page_texts[physical_index].splitlines()
                if item.strip()
            ]
            header = " ".join(lines[:8]).casefold()
            printed_numbers = {
                int(item) for item in lines[:4] if re.fullmatch(r"\d{1,3}", item)
            }
            if (
                audit_start in printed_numbers
                and ("审计报告" in header or "auditor" in header)
            ):
                audit_physical = physical_index + 1
                break
        if audit_physical is None:
            continue
        offset = audit_physical - audit_start
        mapped: dict[str, tuple[int, ...]] = {}
        for statement, (start, end) in print_ranges.items():
            physical_pages = tuple(offset + number for number in range(start, end + 1))
            if (
                not physical_pages
                or physical_pages[0] <= toc_index + 1
                or physical_pages[-1] > page_count
            ):
                mapped = {}
                break
            mapped[statement] = physical_pages
        if set(mapped) == {"income_statement", "balance_sheet", "cash_flow"}:
            return mapped
    return {}


def _candidate_pages_from_text(
    page_texts: Sequence[str], *, continuation_pages: int, rules: FinancialRulesSnapshot | None = None
) -> frozenset[int] | None:
    """Convert one page-index text stream into the bounded candidate set."""

    mapped_statements = _toc_statement_page_map(page_texts, rules=rules)
    starts: list[int] = []
    summary_pages: set[int] = set()
    statement_kinds: set[str] = set()
    lexical_starts: list[int] = []
    scoped_units = discover_document_units(page_texts)
    for page_number, text in enumerate(page_texts, 1):
        compact = re.sub(r"\s+", "", text).casefold()
        if disclosure_page_needed(text):
            # Exact explicit audit/note sentences are independent candidates,
            # not a reason to widen all statement continuation windows.
            summary_pages.add(page_number)
        if scoped_units.note_text_for_page(page_number) is not None:
            summary_pages.add(page_number)
        labels = _labels_for_rules(rules)
        if any(label.casefold() in compact for label in labels["reported_roe"]):
            # ROE is commonly disclosed in a standalone performance table
            # outside the three formal statements. Keep the exact page in
            # the bounded coordinate pass without widening its continuation
            # window to unrelated narrative pages.
            summary_pages.add(page_number)
        statement_context = _statement_context(text, rules)
        if statement_context is not None:
            starts.append(page_number)
            statement_kinds.add(statement_context[0])
            continue
        # Damaged/missing contents and clipped statement titles are common in
        # very large exchange PDFs. Recover a bounded page only when several
        # canonical row labels from one statement co-occur; a single narrative
        # mention is intentionally insufficient.
        for statement, threshold in (
            ("income_statement", 3), ("balance_sheet", 4), ("cash_flow", 2),
        ):
            concepts = [
                concept for concept, owner in _STATEMENT_FOR.items()
                if owner == statement and concept != "reported_roe"
            ]
            hits = sum(
                1 for concept in concepts
                if any(_label_compact(label) in _label_compact(compact) for label in labels[concept])
            )
            if hits >= threshold:
                lexical_starts.append(page_number)
                statement_kinds.add(statement)
                break
    # A partial index is unsafe: missing one statement can make the coordinate
    # parser report an apparently valid but incomplete filing. Returning None
    # deliberately fails open to the full-document parser.
    statement_kinds.update(mapped_statements)
    if (not starts and not lexical_starts and not mapped_statements) or not {
        "income_statement", "balance_sheet", "cash_flow"
    }.issubset(statement_kinds):
        return None
    selected: set[int] = set()
    page_count = len(page_texts)
    for start in starts:
        selected.update(
            range(start, min(page_count, start + max(0, continuation_pages)) + 1)
        )
    for start in lexical_starts:
        selected.update(
            range(start, min(page_count, start + max(1, continuation_pages)) + 1)
        )
    for pages in mapped_statements.values():
        selected.update(pages)
    selected.update(summary_pages)
    return frozenset(selected)


def _close_pdf_resource(resource: Any) -> None:
    """Close a PDFium resource without masking the original parse failure."""

    try:
        close = getattr(resource, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _candidate_financial_pages_pypdfium(
    path: str, *, continuation_pages: int, rules: FinancialRulesSnapshot | None = None,
    return_diagnostics: bool = False,
) -> frozenset[int] | tuple[frozenset[int] | None, str | None] | None:
    """Index page text through PDFium, closing page/text/document resources."""

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    page_texts: list[str] = []
    try:
        for page_number in range(len(document)):
            page = document[page_number]
            try:
                text_page = page.get_textpage()
                try:
                    page_texts.append(str(text_page.get_text_range() or ""))
                finally:
                    _close_pdf_resource(text_page)
            finally:
                _close_pdf_resource(page)
    finally:
        _close_pdf_resource(document)
    indexed = _candidate_pages_from_text(page_texts, continuation_pages=continuation_pages, rules=rules)
    remember_page_texts(path, page_texts)
    if return_diagnostics:
        return indexed, _scanned_image_diagnostic(page_texts, rules=rules)
    return indexed


def _pdfium_candidate_pages_worker(
    path: str,
    continuation_pages: int,
    rules: FinancialRulesSnapshot | None,
) -> tuple[frozenset[int] | None, str | None]:
    """Pickle-safe native indexing task used outside an existing PDF worker."""

    result = _candidate_financial_pages_pypdfium(
        path,
        continuation_pages=continuation_pages,
        rules=rules,
        return_diagnostics=True,
    )
    if isinstance(result, tuple):
        return result
    return result, None


def _pdfium_page_count_worker(path: str) -> int:
    """Pickle-safe page-count task for the recovery/index path."""

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    try:
        return len(document)
    finally:
        _close_pdf_resource(document)


def _candidate_financial_pages_pypdf(
    path: str, *, continuation_pages: int, rules: FinancialRulesSnapshot | None = None,
    return_diagnostics: bool = False,
) -> frozenset[int] | tuple[frozenset[int] | None, str | None] | None:
    """Compatibility indexer used when PDFium is unavailable or incomplete."""

    from pypdf import PdfReader

    reader = PdfReader(path)
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    remember_page_texts(path, page_texts)
    indexed = _candidate_pages_from_text(page_texts, continuation_pages=continuation_pages, rules=rules)
    if return_diagnostics:
        return indexed, _scanned_image_diagnostic(page_texts, rules=rules)
    return indexed


def _pypdf_candidate_pages_worker(
    path: str, continuation_pages: int, rules: FinancialRulesSnapshot | None,
):
    """Pickle-safe compatibility reader, under the native worker deadline."""
    return _candidate_financial_pages_pypdf(
        path, continuation_pages=continuation_pages, rules=rules,
        return_diagnostics=True,
    )


def _pypdf_page_count_worker(path: str) -> int:
    from pypdf import PdfReader

    return len(PdfReader(path, strict=False).pages)


def _scanned_image_diagnostic(
    page_texts: Sequence[str], *, rules: FinancialRulesSnapshot | None = None
) -> str | None:
    """Classify only clearly image-backed statement candidates.

    An all-empty document with no independently anchored contents ranges is
    deliberately unresolved.  It is not treated as a missing disclosure, and
    no upload is authorized by this diagnostic alone.
    """
    if not page_texts:
        return None
    mapped = _toc_statement_page_map(page_texts, rules=rules)
    if set(mapped) == {"income_statement", "balance_sheet", "cash_flow"}:
        mapped_pages = {
            page for pages in mapped.values() for page in pages
        }
        if mapped_pages and all(
            not str(page_texts[page - 1] or "").strip()
            for page in mapped_pages
            if 0 < page <= len(page_texts)
        ):
            return "SCANNED_IMAGE_FILING_DETECTED"
        return None
    # Only an entirely textless document is eligible for the unresolved
    # diagnostic.  Blank separator pages in an otherwise textual report do not
    # become false scanned-filing alerts.
    if not any(str(text or "").strip() for text in page_texts):
        return "SCANNED_IMAGE_LAYOUT_UNRESOLVED"
    return None


def _candidate_financial_pages_with_diagnostic(
    path: str, *, continuation_pages: int = 3,
    rules: FinancialRulesSnapshot | None = None,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> tuple[frozenset[int] | None, str | None]:
    """Return the normal index plus a non-authorizing scan diagnostic."""
    if cancel_check is not None and cancel_check():
        return None, "pdf_parse_cancelled"
    if mp.current_process().daemon:
        # The ordinary AST pipeline is already in a killable per-filing child.
        # Avoid nested spawn here; its parent owns the native-crash boundary.
        try:
            indexed = _pdfium_candidate_pages_worker(path, continuation_pages, rules)
            pdfium_diagnostic = indexed[1]
            if indexed[0] is not None:
                return indexed
        except Exception:
            indexed = None
            pdfium_diagnostic = "PDFIUM_WORKER_ERROR"
    else:
        indexed, isolation_error = _run_pdfium_isolated(
            _pdfium_candidate_pages_worker,
            path,
            continuation_pages,
            rules,
            timeout_seconds=min(45.0, max(0.01, deadline - time.monotonic())) if deadline is not None else 45.0,
            cancel_check=cancel_check,
        )
        if isolation_error == "PDFIUM_WORKER_CANCELLED":
            return None, "pdf_parse_cancelled"
        if isolation_error is not None:
            pdfium_diagnostic = isolation_error
            indexed = None
        elif isinstance(indexed, tuple) and len(indexed) == 2:
            pdfium_diagnostic = indexed[1]
            if indexed[0] is not None:
                return indexed
        else:
            indexed = None
            pdfium_diagnostic = "PDFIUM_WORKER_ERROR"
    # A compatibility reader is not a way around a denied process boundary.
    # PDF text extraction can be CPU-heavy or hang even without native code.
    # Keep its deadline/cancellation rather than blocking the coordinator.
    indexed, fallback_error = _run_pdfium_isolated(
        _pypdf_candidate_pages_worker, path, continuation_pages, rules,
        timeout_seconds=min(45.0, max(0.01, deadline - time.monotonic())) if deadline is not None else 45.0, cancel_check=cancel_check,
    )
    if fallback_error == "PDFIUM_WORKER_CANCELLED":
        return None, "pdf_parse_cancelled"
    if fallback_error is not None:
        return None, fallback_error
    if isinstance(indexed, tuple) and len(indexed) == 2:
        return indexed[0], indexed[1] or pdfium_diagnostic
    return None, pdfium_diagnostic


def _candidate_financial_pages(
    path: str, *, continuation_pages: int = 3,
    rules: FinancialRulesSnapshot | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> frozenset[int] | None:
    """Find formal statement pages with a low-memory text prepass.

    PDFium supplies the fast text layer for the normal path. If PDFium cannot
    be imported/read safely, or produces an incomplete three-statement index,
    pypdf remains the compatibility fallback. Both paths retain the bounded
    continuation window and standalone ROE summary-page semantics.
    """

    return _candidate_financial_pages_with_diagnostic(
        path, continuation_pages=continuation_pages, rules=rules,
        cancel_check=cancel_check,
    )[0]


def _pdf_page_count(
    path: str, *, cancel_check: Callable[[], bool] | None = None, deadline: float | None = None
) -> int | None:
    """Read only the document page count without extracting page text."""

    if cancel_check is not None and cancel_check():
        return None
    if mp.current_process().daemon:
        try:
            return _pdfium_page_count_worker(path)
        except Exception:
            pass
    else:
        count, error = _run_pdfium_isolated(
            _pdfium_page_count_worker,
            path,
            timeout_seconds=min(30.0, max(0.01, deadline - time.monotonic())) if deadline is not None else 30.0,
            cancel_check=cancel_check,
        )
        if error == "PDFIUM_WORKER_CANCELLED":
            return None
        if error is None and isinstance(count, int) and count >= 0:
            return count
    if cancel_check is not None and cancel_check():
        return None
    count, error = _run_pdfium_isolated(
        _pypdf_page_count_worker, path, timeout_seconds=min(30.0, max(0.01, deadline - time.monotonic())) if deadline is not None else 30.0,
        cancel_check=cancel_check,
    )
    return count if error is None and isinstance(count, int) and count >= 0 else None


def _pdf_evidence_id_for_fact(fact_id: str) -> str | None:
    """Return the explicit AST fact/evidence association, if one exists.

    Parser facts use the ``ingest:`` prefix; independently extracted source
    disclosures use ``disclosure:`` or ``note:``. Evidence references retain
    the complete candidate ID after ``fact:``. Keep this explicit allowlist
    narrow: arbitrary IDs must not gain provenance from metadata or position.
    """
    for prefix in ("ingest:", "disclosure:", "note:"):
        suffix = fact_id[len(prefix):] if fact_id.startswith(prefix) else ""
        if suffix:
            return "fact:" + (suffix if prefix == "ingest:" else fact_id)
    return None


def _shift_period_year(value: str | None, target_year: int) -> str | None:
    """Shift a source-supported period boundary without changing its shape.

    Comparative columns must use the filing's actual fiscal boundaries.  In
    particular, a March year-end cannot be rewritten as a calendar-year
    January start, and February 29 needs a deterministic non-leap fallback.
    """
    if not value or len(str(value)[:10]) < 10:
        return None
    try:
        current = date.fromisoformat(str(value)[:10])
        try:
            shifted = current.replace(year=int(target_year))
        except ValueError:
            shifted = current.replace(year=int(target_year), day=28)
        return shifted.isoformat()
    except (TypeError, ValueError):
        return None


def _period_from_interval(start: str, end: str) -> str:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        return ""
    if (first.month, first.day, last.month, last.day) == (1, 1, 6, 30):
        return "H1"
    if (first.month, first.day, last.month, last.day) == (1, 1, 3, 31):
        return "Q1"
    if (first.month, first.day, last.month, last.day) == (1, 1, 9, 30):
        return "Q3"
    if (first.month, first.day, last.month, last.day) == (1, 1, 12, 31):
        return "FY"
    # An explicit header is stronger than the filing-level period label.  A
    # valid range that does not match one of our named year-to-date periods
    # stays an interval; borrowing the filing's FY/H1 label would misstate the
    # comparator even though its exact start/end dates are known.
    return "INTERVAL"


def _bind_comparison_period(
    column: _PeriodColumn, statement: str, manifest: FilingManifest,
) -> _ComparisonPeriodBinding:
    """Bind an issuer's comparison column to a point or interval safely.

    Balance-sheet values are points in time and require their own dated header;
    a report's period end is not a substitute for an ambiguous comparison
    label. Flow statements describe intervals, so year-only columns inherit the
    filing's actual interval, while explicit headers can bind a dated range.
    """
    explicit_start = column.period_start or ""
    explicit_end = column.period_end or ""
    if statement == "balance_sheet":
        if not explicit_end:
            return _ComparisonPeriodBinding(
                column.year, "", None, "",
                "unresolved_ambiguous_point_in_time_header", False,
            )
        end_date = date.fromisoformat(explicit_end)
        fiscal_period = {
            (12, 31): "FY", (6, 30): "H1", (3, 31): "Q1", (9, 30): "Q3",
        }.get((end_date.month, end_date.day), "AS_OF")
        basis = (
            "explicit_point_in_time_header"
            if column.period_basis != "explicit_header_interval"
            else "explicit_point_in_time_header_range_end"
        )
        return _ComparisonPeriodBinding(
            column.year or end_date.year, fiscal_period, None, explicit_end, basis, True,
        )

    if statement not in {"income_statement", "cash_flow", "comprehensive_income"}:
        return _ComparisonPeriodBinding(
            column.year, "", None, "", "unresolved_unsupported_statement_period", False,
        )

    if explicit_start and explicit_end and column.period_basis == "explicit_header_interval":
        fiscal_period = _period_from_interval(explicit_start, explicit_end)
        return _ComparisonPeriodBinding(
            int(explicit_end[:4]), fiscal_period, explicit_start, explicit_end,
            "explicit_header_interval", True,
        )

    try:
        current_year = int(manifest.period_end[:4])
        target_year = column.year or current_year
    except (TypeError, ValueError):
        target_year = 0
        current_year = 0
    current_start = _period_start(manifest)
    shifted_end = _shift_period_year(manifest.period_end, target_year) if target_year else None
    shifted_start = _shift_period_year(current_start, target_year) if target_year else None
    if explicit_end:
        # An end date alone only forms a valid comparative interval when it
        # agrees with the same filing interval shifted to that column's year.
        if not shifted_end or explicit_end != shifted_end:
            return _ComparisonPeriodBinding(
                column.year, "", None, "", "unresolved_incomplete_interval_header", False,
            )
        basis = "explicit_header_end_date_and_filing_interval"
        shifted_end = explicit_end
    else:
        basis = "column_year_and_filing_interval"
    if not shifted_start or not shifted_end or not manifest.fiscal_period:
        return _ComparisonPeriodBinding(
            column.year, "", None, "", "unresolved_incomplete_interval_header", False,
        )
    return _ComparisonPeriodBinding(
        int(shifted_end[:4]), manifest.fiscal_period, shifted_start, shifted_end, basis, True,
    )


def _pair_pdf_facts_evidence(
    facts: Sequence[FinancialFact], refs: Sequence[EvidenceRef],
) -> tuple[tuple[tuple[FinancialFact, EvidenceRef], ...], tuple[str, ...]]:
    """Pair AST facts with exactly one explicit evidence reference.

    Missing or duplicate references are diagnostic-only and are deliberately
    excluded from candidate provenance; an unpaired fact must never look
    accepted merely because it happened to occupy the same list position.
    """
    refs_by_id: dict[str, list[EvidenceRef]] = {}
    for ref in refs:
        refs_by_id.setdefault(ref.evidence_id, []).append(ref)
    paired: list[tuple[FinancialFact, EvidenceRef]] = []
    diagnostics: list[str] = []
    for fact in facts:
        expected_id = _pdf_evidence_id_for_fact(fact.fact_id)
        matches = refs_by_id.get(expected_id, []) if expected_id else []
        if len(matches) == 1:
            ref = matches[0]
            page = re.fullmatch(r"page:([1-9]\d*)", str(ref.locator or ""))
            if (
                page is not None
                and (fact.source_page is None or int(page.group(1)) == fact.source_page)
                and bool(ref.document_id)
                and ref.source_url == fact.source_url
            ):
                paired.append((fact, ref))
            else:
                diagnostics.append(f"pdf_evidence_location_mismatch:{fact.fact_id}")
        elif len(matches) > 1:
            diagnostics.append(f"pdf_evidence_ambiguous:{fact.fact_id}")
        else:
            diagnostics.append(f"pdf_evidence_missing:{fact.fact_id}")
    return tuple(paired), tuple(diagnostics)


class FinancialIngestionEngine:
    """Structured-first engine; PDF is a coordinate-aware deterministic fallback."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        max_workers: int = 3,
        parse_timeout_seconds: float = 120.0,
        batch_timeout_seconds: float = 280.0,
        compatibility_rules: FinancialRulesSnapshot | None = None,
        checkpoint_dir: str | Path | None = None,
    ) -> None:
        # The cache is deliberately optional so fixture/injected engines keep
        # their historical behavior. Production supplies a workspace-owned
        # directory; successful entries are content addressed and atomic.
        self._parse_cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._parse_cache_memory: dict[str, tuple[list[FinancialFact], list[EvidenceRef]]] = {}
        self._max_workers = max(1, min(3, int(max_workers)))
        self._parse_timeout_seconds = max(0.1, float(parse_timeout_seconds))
        self._batch_timeout_seconds = max(0.1, min(280.0, float(batch_timeout_seconds)))
        self._compatibility_rules = compatibility_rules or FinancialRulesSnapshot.empty()
        self._checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir is not None else None
        self._last_pdf_context: PdfTableContext | None = None
        self._document_execution: dict[str, dict[str, Any]] = {}

    def with_compatibility_rules(
        self, rules: FinancialRulesSnapshot | None
    ) -> "FinancialIngestionEngine":
        """Return an isolated engine view for one research run.

        Compatibility packs are selected per issuer/run.  A cloned engine
        avoids mutating shared parser state while preserving the same
        content-addressed cache directory and bounded worker policy.
        """
        selected = rules or FinancialRulesSnapshot.empty()
        if selected == self._compatibility_rules:
            return self
        return FinancialIngestionEngine(
            cache_dir=self._parse_cache_dir,
            max_workers=self._max_workers,
            parse_timeout_seconds=self._parse_timeout_seconds,
            batch_timeout_seconds=self._batch_timeout_seconds,
            compatibility_rules=selected,
            checkpoint_dir=self._checkpoint_dir,
        )

    @staticmethod
    def _file_sha256(path: str, *, cancel_check=None, deadline: float | None = None) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if cancel_check is not None and cancel_check():
                    raise CancelledError("PDF hashing cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("PDF hashing exceeded document budget")
                digest.update(chunk)
        return digest.hexdigest()

    def _parse_cache_key(
        self, path: str, company: Company, filing: FilingDocument, actual_hash: str
    ) -> str:
        profile = "|".join(
            str(getattr(company, name, ""))
            for name in ("market", "accounting_standard", "industry", "industry_support", "company_type")
        )
        policy = "|".join((filing.fiscal_period or "FY", filing.period_end or "", "consolidated", company.reporting_currency or ""))
        source_identity = tuple(
            str(getattr(filing, name, "") or "")
            for name in ("company_cik", "document_id", "accession_number", "source_url",
                         "primary_document", "form_type", "filed_at", "revision",
                         "supersedes_document_id")
        )
        material = json.dumps(
            (
                actual_hash,
                _PDF_PARSER_VERSION,
                _PDF_TAXONOMY_VERSION,
                _PDF_CACHE_POLICY_VERSION,
                _SOURCE_DISCLOSURE_VERSION,
                self._compatibility_rules.semantic_fingerprint(),
                profile,
                policy,
                str(company.cik),
                source_identity,
            ), ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def pending_row_mappings(self, company: Company, filings: Sequence[FilingDocument]) -> frozenset[str]:
        """Expose document-local mapping debt before an active-cache shortcut.

        This reads only the exact source/config-addressed cache entries. It
        neither parses nor downloads and is not a new fact-admission gate.
        """
        if self._parse_cache_dir is None:
            return frozenset()
        pending = set()
        for filing in filings:
            if not filing.content_hash:
                continue
            key = self._parse_cache_key(filing.local_path, company, filing, filing.content_hash)
            try:
                payload = json.loads((self._parse_cache_dir / f"{key}.json").read_text(encoding="utf-8"))
                if (payload.get("parser_version") == _PDF_PARSER_VERSION
                    and payload.get("roe_row_mapping_version") != _ROE_ROW_MAPPING_VERSION
                    and any(isinstance(item, dict) and item.get("concept") in {
                        "reported_roe", "reported_roe_excluding_nonrecurring"}
                        for item in payload.get("facts", ()))):
                    pending.add(filing.accession_number)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return frozenset(pending)

    def _load_parse_cache(
        self, key: str
    ) -> tuple[list[FinancialFact], list[EvidenceRef]] | None:
        cached = self._parse_cache_memory.get(key)
        if cached is not None:
            return ([replace(item) for item in cached[0]], [replace(item) for item in cached[1]])
        if self._parse_cache_dir is None:
            return None
        cache_path = self._parse_cache_dir / f"{key}.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            # The digest key prevents ordinary v3/v4 collisions, while this
            # payload marker protects against a stale or manually copied
            # payload being placed under a current key.  Cached facts must be
            # emitted by the same semantic parser revision as the key.
            if (payload.get("parser_version") != _PDF_PARSER_VERSION
                    or payload.get("disclosure_version") != _SOURCE_DISCLOSURE_VERSION):
                return None
            cached_fact_rows = payload["facts"]
            if (
                payload.get("roe_row_mapping_version") != _ROE_ROW_MAPPING_VERSION
                and any(
                    isinstance(item, dict)
                    and item.get("concept") in {
                        "reported_roe", "reported_roe_excluding_nonrecurring",
                    }
                    for item in cached_fact_rows
                )
            ):
                # This page-local mapping change affects only cached documents
                # that actually contain ROE candidates. Other old parse
                # payloads remain reusable without a global parser-version bump.
                return None
            facts = [FinancialFact(**item) for item in cached_fact_rows]
            refs = [EvidenceRef(**item) for item in payload["evidence"]]
            if any(item.parser_version not in {_PDF_PARSER_VERSION, DISCLOSURE_VERSION, *NOTE_DISCLOSURE_VERSIONS} for item in facts):
                return None
            observations = payload.get("document_execution", {})
            if not isinstance(observations, dict):
                observations = {}
            for document_id, observation in observations.items():
                if isinstance(document_id, str) and isinstance(observation, dict):
                    self._document_execution[document_id] = {**observation, "cache_reused": True,
                                                             "attempt_count": 0, "parse_elapsed_seconds": 0.0}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        self._parse_cache_memory[key] = (facts, refs)
        return ([replace(item) for item in facts], [replace(item) for item in refs])

    def _store_parse_cache(
        self,
        key: str,
        facts: Sequence[FinancialFact],
        refs: Sequence[EvidenceRef],
    ) -> None:
        # Only the AST parser may label facts with its current contract. Test,
        # injected, and legacy parse callbacks that omit it stay explicitly
        # unversioned instead of being upgraded by the cache.
        safe_facts = [
            replace(item, parser_version=item.parser_version or "unversioned-parser")
            for item in facts
        ]
        safe_refs = [replace(item) for item in refs]
        self._parse_cache_memory[key] = (safe_facts, safe_refs)
        if self._parse_cache_dir is None:
            return
        payload = json.dumps(
            {
                "parser_version": _PDF_PARSER_VERSION,
                "disclosure_version": _SOURCE_DISCLOSURE_VERSION,
                "roe_row_mapping_version": _ROE_ROW_MAPPING_VERSION,
                "facts": [item.to_dict() for item in safe_facts],
                "evidence": [item.to_dict() for item in safe_refs],
                "document_execution": {document_id: self._document_execution[document_id]
                    for document_id in sorted({ref.document_id for ref in safe_refs})
                    if document_id in self._document_execution},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._parse_cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self._parse_cache_dir / f"{key}.json"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{key}-", suffix=".tmp",
                dir=self._parse_cache_dir, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def parse_local_pdf_resumable(
        self,
        company: Company,
        filing: FilingDocument,
        manifest: FilingManifest,
        *,
        candidate_pages: frozenset[int] | None = None,
        window_size: int = 8,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        deadline: float | None = None,
    ) -> tuple[list[FinancialFact], list[EvidenceRef], tuple[str, ...]]:
        """Parse candidate pages through durable, content-addressed windows.

        This is an opt-in recovery seam.  The ordinary isolated scheduler
        remains the default and supplies hard process termination; callers
        enabling ``checkpoint_dir`` gain per-window restart/resume without
        changing compiler acceptance semantics.
        """

        from .financial_checkpoint import (
            CheckpointKey,
            WindowCheckpointStore,
            plan_page_windows,
            run_checkpointed_windows,
        )

        parse_started = time.monotonic()
        observation = {"source_hash": filing.content_hash, "attempt_count": 1, "cache_reused": False}
        self._document_execution[filing.document_id] = observation
        deadline = min(deadline, time.monotonic() + self._parse_timeout_seconds) if deadline is not None else time.monotonic() + self._parse_timeout_seconds
        user_cancelled = cancel_check
        cancel_check = lambda: (user_cancelled is not None and user_cancelled()) or time.monotonic() >= deadline
        if not filing.local_path or not Path(filing.local_path).is_file():
            return [], [], ("pdf_file_missing",)
        try:
            actual_hash = self._file_sha256(filing.local_path, cancel_check=user_cancelled, deadline=deadline)
        except TimeoutError:
            return [], [], ("pdf_document_budget_exhausted_resumable",)
        except OSError:
            return [], [], ("pdf_file_unreadable",)
        if ((filing.content_hash and actual_hash != filing.content_hash)
                or (manifest.content_hash and manifest.content_hash != actual_hash)):
            observation.update({"failure_stage": "source-identity", "root_error_code": "PDF_SOURCE_HASH_MISMATCH"})
            return [], [], ("PDF_SOURCE_HASH_MISMATCH",)
        # Legacy direct callers may not yet have a hash. Bind the bytes just
        # observed rather than misreporting an absent expectation as a conflict.
        # An explicit conflicting expectation above is never overwritten.
        filing = replace(filing, content_hash=actual_hash)
        manifest = replace(manifest, content_hash=actual_hash)
        observation["source_hash"] = actual_hash
        document_context = read_document_context(filing.local_path, cancel_check=cancel_check,
            timeout_seconds=max(0.01, min(45.0, deadline - time.monotonic())))
        if getattr(document_context, "diagnostic_code", ""):
            return [], [], (document_context.diagnostic_code,)
        page_count = len(document_context.page_texts) or _pdf_page_count(
            filing.local_path, cancel_check=cancel_check, deadline=deadline)
        observation.update({"page_count": page_count,
                            "native_text_page_count": sum(bool(text.strip()) for text in document_context.page_texts)})
        if cancel_check is not None and cancel_check():
            return [], [], (("pdf_parse_cancelled" if user_cancelled is not None and user_cancelled() else "pdf_document_budget_exhausted_resumable"),)
        selected_pages = candidate_pages
        if selected_pages is None and document_context.page_texts:
            selected_pages = _candidate_pages_from_text(document_context.page_texts,
                continuation_pages=3, rules=self._compatibility_rules)
        if selected_pages is None:
            indexed_pages, index_diagnostic = _candidate_financial_pages_with_diagnostic(
                filing.local_path,
                rules=self._compatibility_rules,
                cancel_check=cancel_check,
                deadline=deadline,
            )
            if cancel_check is not None and cancel_check():
                return [], [], (("pdf_parse_cancelled" if user_cancelled is not None and user_cancelled() else "pdf_document_budget_exhausted_resumable"),)
            if index_diagnostic == "PDFIUM_WORKER_START_FAILED":
                return [], [], ("PDF_WORKER_START_FAILED",)
            if indexed_pages is None:
                # ``None`` means the inexpensive index could not establish a
                # complete statement-page map.  Fail open to the real document
                # page range; silently treating this as an empty result loses
                # every fact in reports whose text layer is incomplete.
                if not page_count or page_count < 1:
                    return [], [], ("pdf_candidate_pages_unavailable",)
                selected_pages = frozenset(range(1, page_count + 1))
            else:
                selected_pages = indexed_pages
        if not selected_pages:
            return [], [], ("pdf_candidate_pages_unavailable",)
        pages = frozenset(int(page) for page in selected_pages if int(page) > 0)
        page_count = page_count or max(pages)
        windows = plan_page_windows(page_count, pages, window_size=window_size)
        observation.update({"indexed_page_count": len(pages), "window_total": len(windows)})
        from .financial_statutory_currency import bind_statutory_presentation
        document_context = replace(document_context, units=bind_statutory_presentation(
            document_context.units, document_context.page_texts, company, filing))
        manifest = _manifest_from_observed_pages(
            manifest, filing, (), declarations=document_context.periods,
        )
        if manifest.revision == "period_header_conflict":
            return [], [], ("pdf_statement_period_conflict",)
        if not manifest.period_end:
            return [], period_gap_evidence(filing, document_context.period_gaps), ("PERIOD_DECLARATION_NOT_FOUND",)
        store = WindowCheckpointStore(
            self._checkpoint_dir or (Path(filing.local_path).parent / ".checkpoints")
        )
        key = CheckpointKey(
            actual_hash,
            _PDF_PARSER_VERSION,
            self._compatibility_rules.semantic_fingerprint(),
            identity_digest=hashlib.sha256(
                "|".join(
                    (
                        company.security_id,
                        company.market,
                        company.reporting_currency,
                        manifest.accession_number,
                        manifest.period_end,
                        manifest.fiscal_period,
                        manifest.revision,
                        manifest.source_url,
                        _SOURCE_DISCLOSURE_VERSION,
                    )
                ).encode("utf-8")
            ).hexdigest(),
        )

        worker = _CheckpointPdfWindowWorker(
            filing.local_path,
            company,
            filing,
            manifest,
            self._compatibility_rules,
            document_context.units,
        )

        window_failures: list[str] = []
        failed_pages: set[int] = set()

        def window_failed(_window, error):
            failed_pages.update(_window.pages)
            if getattr(error, "code", "") == "PDF_WORKER_START_FAILED":
                window_failures.append("PDF_WORKER_START_FAILED")

        checkpoints = run_checkpointed_windows(
            store,
            key,
            windows,
            worker,
            max_retries=1,
            cancel_check=cancel_check,
            progress=progress,
            timeout_seconds=self._parse_timeout_seconds,
            input_fingerprint=key.digest,
            deterministic_failure=_is_deterministic_pdf_window_failure,
            on_failure=window_failed,
            deadline=deadline,
        )
        exhausted = store.load_exhausted(key, input_fingerprint=key.digest)
        if exhausted is not None:
            failed_window = exhausted.get("failed_window", {})
            expected = next((window for window in windows if window.index == failed_window.get("index")), None)
            if expected is not None and tuple(failed_window.get("pages", ())) == expected.pages:
                failed_pages.update(expected.pages)
        facts: dict[str, FinancialFact] = {}
        refs: dict[str, EvidenceRef] = {}
        for checkpoint in checkpoints:
            for fact in checkpoint.facts:
                facts.setdefault(fact.fact_id, fact)
            for ref in checkpoint.evidence:
                refs.setdefault(ref.evidence_id, ref)
        diagnostics = (
            () if len(checkpoints) == len(windows) else
            tuple(dict.fromkeys((*window_failures,
                "pdf_window_exhausted_same_input" if exhausted is not None else
                "pdf_parse_cancelled" if user_cancelled is not None and user_cancelled() else
                "pdf_document_budget_exhausted_resumable" if time.monotonic() >= deadline else
                "pdf_window_incomplete")))
        )
        observation.update({"completed_window_count": len(checkpoints),
                            "parser_failed_pages": sorted(failed_pages),
                            "parse_elapsed_seconds": round(time.monotonic() - parse_started, 3),
                            "failure_stage": "parse" if diagnostics else "",
                            "root_error_code": diagnostics[0] if diagnostics else ""})
        return (
            sorted(facts.values(), key=lambda fact: (fact.source_page or 0, fact.fact_id)),
            sorted(refs.values(), key=lambda ref: ref.evidence_id),
            diagnostics,
        )

    def collect_candidate_batches(
        self,
        company: Company,
        filings: Sequence[FilingDocument],
        *,
        structured_sources: Sequence[FinancialSourceAdapter] = (),
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        deadline: float | None = None,
    ) -> FinancialCandidateCollection:
        """Compatibility seam for the source-owned candidate collector."""
        from .financial_sources.pdf import FinancialPdfSource

        return FinancialPdfSource(self).collect_candidate_batches(
            company,
            filings,
            structured_sources=structured_sources,
            cancel_check=cancel_check,
            progress=progress,
            deadline=deadline,
        )

    def ingest(
        self, company: Company, filings: Sequence[FilingDocument], *,
        structured_sources: Sequence[FinancialSourceAdapter] = (),
        vision_fallback: VisionFinancialSourceAdapter | None = None,
        vision_config: VisionFallbackConfig | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> FinancialDataset:
        """Compatibility projection over the canonical candidate pipeline."""

        from .financial_pipeline import FinancialPipeline, FinancialResearchSnapshot

        authorized_stages = (
            frozenset({GapStageKind.MINERU})
            if bool(getattr(vision_config, "enabled", False))
            and bool(getattr(vision_config, "consent", False))
            else frozenset()
        )
        prepared = FinancialPipeline(self).prepare(
            FinancialResearchSnapshot(
                subject=company,
                filings=tuple(filings),
                candidate_sources=tuple(structured_sources),
                authorized_source_stages=authorized_stages,
                vision_fallback=vision_fallback,
                vision_config=vision_config,
                cancel_check=cancel_check,
                progress=progress,
            ),
            CompilationPolicy(
                filings=tuple(filings),
                reporting_currency=company.reporting_currency,
            ),
        )
        compiled = prepared.canonical_dataset
        if not isinstance(compiled, CanonicalFinancialDataset):
            raise TypeError("FINANCIAL_PIPELINE_CANONICAL_DATASET_REQUIRED")
        target_identities: set[tuple[str, str, str]] = set()
        for filing in filings:
            candidate_groups = tuple(
                item for item in compiled.validations
                if item.identity[0] == filing.accession_number
                and str(item.identity[3]).casefold() == "consolidated"
            )
            if not candidate_groups:
                continue
            declared_end = str(filing.period_end or "")[:10]
            selected = next(
                (
                    item for item in candidate_groups
                    if str(item.identity[1])[:10] == declared_end
                ),
                max(candidate_groups, key=lambda item: str(item.identity[1])[:10]),
            )
            target_identities.add((
                selected.identity[0],
                str(selected.identity[1])[:10],
                str(selected.identity[2]).upper(),
            ))
        primary_facts = tuple(
            fact
            for item in compiled.validations
            if (
                item.identity[0],
                str(item.identity[1])[:10],
                str(item.identity[2]).upper(),
            ) in target_identities
            and str(item.identity[3]).casefold() == "consolidated"
            for fact in item.accepted
        )
        # A legacy caller expects ``accepted_facts`` to contain the selected
        # filing cohorts, not hidden comparison columns or auxiliary audit
        # facts.  Preserve partial target facts when a cohort is incomplete;
        # the canonical compiler status still keeps those facts out of model
        # capabilities that require a complete profile.
        if not primary_facts:
            primary_facts = tuple(
                fact for fact in compiled.resolved_facts
                if str(fact.usage_status or "").casefold() != "comparator"
            )
        issues = list(compiled.diagnostics)
        selected_target_groups = tuple(
            item for item in compiled.validations
            if (
                item.identity[0],
                str(item.identity[1])[:10],
                str(item.identity[2]).upper(),
            ) in target_identities
            and str(item.identity[3]).casefold() == "consolidated"
        )
        for item in compiled.validations:
            identity = tuple(item.identity)
            if (
                len(identity) >= 5
                and (identity[0], str(identity[1])[:10], str(identity[2]).upper())
                in target_identities
                and str(identity[3]).casefold() == "consolidated"
            ):
                issues.extend(item.issues)
        target_groups_healthy = bool(selected_target_groups) and all(
            item.status == ValidationStatus.VERIFIED.value
            for item in selected_target_groups
        )
        status = (
            ValidationStatus.REJECTED
            if not primary_facts
            else ValidationStatus.READY_WITH_WARNINGS
            if not compiled.allow_ai or not target_groups_healthy
            else ValidationStatus.VERIFIED
        )
        validation = FinancialValidation(
            status,
            tuple(dict.fromkeys(issues)),
            frozenset(fact.concept for fact in primary_facts),
            primary_facts,
            tuple(compiled.quarantined_facts),
        )
        groups = list(compiled.group_validations)
        for group in groups:
            for fact in group.validation.accepted:
                fact.validation_status = group.validation.status.value
            for fact in group.validation.quarantined:
                fact.validation_status = ValidationStatus.REJECTED.value
        return FinancialDataset(
            primary_facts,
            tuple(compiled.evidence),
            tuple(compiled.manifest),
            validation,
            tuple(dict.fromkeys(issues)),
            tuple(groups),
        )

    @staticmethod
    def _evidence_for_fact(fact: FinancialFact, filing: FilingDocument) -> EvidenceRef:
        return EvidenceRef(
            f"fact:{fact.fact_id}", filing.document_id, filing.source_url,
            f"{filing.form_type} {fact.end_date} / {fact.concept}",
            f"page:{fact.source_page or 0}", fact.raw_text, filing.filed_at,
            filing.content_hash,
            fact.source_bbox,
        )

    def extract_pdf_candidates(
        self,
        path: str,
        company: Company,
        filing: FilingDocument,
        manifest: FilingManifest | None = None,
        *,
        candidate_pages: frozenset[int] | None = None,
    ) -> tuple[list[FinancialFact], list[EvidenceRef]]:
        """Public AST extraction seam for compiler adapters.

        The method intentionally returns unvalidated facts plus evidence.  It
        performs no source-specific acceptance decision; callers must pass
        candidates through :meth:`validate_group` or the canonical compiler.
        """
        resolved_manifest = manifest or _manifest_for(filing)
        if resolved_manifest is None:
            return [], []
        return self._parse_pdf_ast(
            path, company, filing, resolved_manifest,
            candidate_pages=candidate_pages,
            index_precomputed=candidate_pages is not None,
        )

    def validate_group(
        self,
        facts: list[FinancialFact],
        identity: tuple[str, str, str, str, str],
        evidence_map: dict[str, EvidenceRef] | None = None,
        required_concepts: set[str] | frozenset[str] | None = None,
        expected_company_id: str | None = None,
        source_evidence: Sequence[EvidenceRef] = (),
    ) -> FinancialGroupValidation:
        """Public quality-gate seam used by canonical adapters/compiler."""
        return self._validate_group(
            facts, identity, evidence_map, required_concepts, expected_company_id,
            source_evidence,
        )

    def _validate_group(
        self, facts, identity, evidence_map=None, required_concepts=None,
        expected_company_id=None, source_evidence=(),
    ):
        """Legacy entry delegated to the single invariant implementation."""
        return FinancialValidator().validate_group(
            facts, identity, evidence_map, required_concepts, expected_company_id,
            source_evidence,
        )

    def _parse_pdf_ast(
        self,
        path: str,
        company: Company,
        filing: FilingDocument,
        manifest: FilingManifest,
        *,
        candidate_pages: frozenset[int] | None = None,
        index_precomputed: bool = False,
        compatibility_rules: FinancialRulesSnapshot | None = None,
        initial_context: dict[str, Any] | None = None,
        document_units: DocumentUnitResolution | None = None,
    ):
        import pdfplumber

        facts: list[FinancialFact] = []
        refs: list[EvidenceRef] = []
        # Keep row refs positionally aligned with ``facts``. Supplemental unit
        # refs are returned after row refs so duplicate-row replacement by
        # fact index cannot overwrite or shift another fact's provenance.
        normalization_refs: list[EvidenceRef] = []
        disclosure_facts: list[FinancialFact] = []
        disclosure_refs: list[EvidenceRef] = []
        previous: PdfTableContext | None = _checkpoint_context_from_dict(initial_context)
        chapter_unit: PdfTableContext | None = (
            previous if previous and previous.unit_explicit else None
        )
        self._last_pdf_context = previous
        rules = compatibility_rules or self._compatibility_rules
        if not index_precomputed:
            candidate_pages = _candidate_financial_pages(path, rules=rules)
        with pdfplumber.open(path) as pdf:
            document_texts = cached_page_texts(path)
            if document_units is None:
                # The native index normally already owns all text. A direct
                # positioned fixture uses its supplied pages without filesystem
                # I/O; production fallback remains in the killable prepass.
                if document_texts is None:
                    if Path(path).is_file():
                        native_context = read_document_context(path)
                        document_texts = native_context.page_texts
                    else:
                        document_texts = tuple(page.extract_text() or "" for page in pdf.pages)
                document_units = discover_document_units(document_texts)
            if document_texts is not None:
                from .financial_statutory_currency import bind_statutory_presentation
                document_units = bind_statutory_presentation(document_units, document_texts, company, filing)
            normalization_refs.extend(document_units.statutory_presentation)
            if not manifest.period_end:
                observed_pages = cached_page_texts(path)
                if observed_pages is None:
                    observed_pages = []
                    for page_number, page in enumerate(pdf.pages, 1):
                        if candidate_pages is not None and page_number not in candidate_pages:
                            observed_pages.append("")
                            continue
                        try:
                            observed_pages.append(page.extract_text() or "")
                        except Exception:
                            observed_pages.append("")
                manifest = _manifest_from_observed_pages(manifest, filing, observed_pages)
                if not manifest.period_end:
                    return [], period_gap_evidence(filing, statement_period_gaps(observed_pages))
            for page_number, page in enumerate(pdf.pages, 1):
                if candidate_pages is not None and page_number not in candidate_pages:
                    continue
                words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
                if not words:
                    continue
                rows = _positioned_pdf_rows(words)
                page_text = page.extract_text() or ""
                if rows:
                    page_bbox = (min(row.bbox[0] for row in rows), min(row.bbox[1] for row in rows),
                                 max(row.bbox[2] for row in rows), max(row.bbox[3] for row in rows))
                    observed_facts, observed_refs = extract_disclosure_candidates(
                        company, replace(filing, period_end=manifest.period_end, fiscal_period=manifest.fiscal_period),
                        page_number, page_text, page_bbox,
                    )
                    disclosure_facts.extend(observed_facts)
                    disclosure_refs.extend(observed_refs)
                    note_facts, note_refs = extract_note_candidates(company,
                        replace(filing, period_end=manifest.period_end, fiscal_period=manifest.fiscal_period),
                        page_number, document_units.note_text_for_page(page_number) or page_text,
                        page_bbox, document_units.note_for_page(page_number),
                        continuation=document_units.note_continuation_for_page(page_number), positioned_rows=rows)
                    disclosure_facts.extend(note_facts)
                    disclosure_refs.extend(note_refs)
                sections = _page_sections(
                    previous, page_text, rows, page_number,
                    "CNY" if document_units.statutory_presentation else company.reporting_currency,
                    rules, chapter_unit,
                    document_units,
                )
                for section in sections:
                    context = section.context
                    if context.unit_provenance == "explicit":
                        chapter_unit = context
                    statement, scope = context.statement, context.scope
                    multiplier, table_currency = context.multiplier, context.currency
                    table_rows = section.rows
                    statement_ref = None
                    if context.statement_evidence and context.statement_evidence_page is not None and context.statement_evidence_bbox:
                        statement_id = statement_title_evidence_id(
                            filing.document_id, context.statement_evidence_page,
                            context.statement_evidence_bbox, context.statement_evidence,
                        )
                        statement_ref = EvidenceRef(
                            statement_id, filing.document_id, filing.source_url,
                            "formal statement title", f"page:{context.statement_evidence_page}",
                            context.statement_evidence, filing.filed_at, filing.content_hash,
                            context.statement_evidence_bbox,
                        )
                        if all(item.evidence_id != statement_id for item in normalization_refs):
                            normalization_refs.append(statement_ref)
                    if statement == "equity_changes":
                        # A multi-dimensional movements matrix is not a
                        # balance-sheet closing amount. Preserve the literal
                        # disclosure for research/audit until a typed matrix
                        # adapter can prove each component/column identity.
                        excerpt = "\n".join(row.text for row in table_rows)
                        disclosure_id = hashlib.sha256(f"{filing.document_id}|{page_number}|{excerpt}".encode()).hexdigest()[:24]
                        normalization_refs.append(EvidenceRef(
                            f"disclosure:{disclosure_id}", filing.document_id, filing.source_url,
                            "equity movements disclosure", f"page:{page_number}", excerpt,
                            filing.filed_at, filing.content_hash,
                        ))
                        continue
                    table = PdfTableAST(page_number, statement, scope, table_currency, multiplier, _period_headers(table_rows), table_rows)
                    columns = context.periods or _period_columns(table.rows, rules=rules, source_page=page_number)
                    if section.summary and not columns:
                        columns = _summary_roe_period_columns(table.rows, manifest)
                    if manifest.fiscal_period in {'FY', 'CY', 'ANNUAL'}:
                        # Explicit paired annual current/prior headers are
                        # real comparative columns, even without year digits.
                        # Keep their original words and geometry; never resolve
                        # an isolated/ambiguous header or a quarterly 'prior'.
                        columns = tuple(replace(column, year=int(manifest.period_end[:4])
                            - (column.relative_period == 'prior'))
                            if column.year == 0 and column.relative_period in {'current', 'prior'} else column
                            for column in columns)
                    table_text = ' '.join(item.text for item in table.rows)
                    percent_unit_header = any(
                        re.search(
                            r"(?:单位|unit)\s*[:：]?\s*[（(]?\s*[%％]|[（(]\s*[%％]\s*[）)]",
                            row.text,
                            re.IGNORECASE,
                        )
                        for row in table.rows[:12]
                    )
                    # CAS English statements use Operating income for top-line
                    # revenue, unlike US GAAP operating profit. Require the
                    # paired numbered cost header in the same formal table.
                    cas_topline = statement == 'income_statement' and bool(
                        re.search(r'\bI\.\s*Operating income\b', table_text, re.I)
                        and re.search(r'\bII\.\s*Operating cost\b', table_text, re.I)
                    )
                    section_labels = _labels_for_rules(rules)
                    if statement == "comprehensive_income":
                        section_labels = {key: value for key, value in section_labels.items()
                                          if _STATEMENT_FOR[key] == "comprehensive_income"}
                    if any(_label_compact(_row_label_text(row)) in {"capitalexpenditure", "capitalexpenditures"}
                           and not any(_parse_number(cell.text) is not None for cell in row.cells)
                           for row in table.rows):
                        excerpt = "\n".join(row.text for row in table.rows)
                        disclosure_id = hashlib.sha256(f"{filing.document_id}|{page_number}|{excerpt}".encode()).hexdigest()[:24]
                        normalization_refs.append(EvidenceRef(
                            f"disclosure:{disclosure_id}", filing.document_id, filing.source_url,
                            "reported capital expenditures disclosure", f"page:{page_number}", excerpt,
                            filing.filed_at, filing.content_hash,
                        ))
                    if cas_topline:
                        section_labels = dict(section_labels)
                        section_labels['revenue'] = (*section_labels['revenue'], 'operating income')
                        section_labels['operating_income'] = tuple(label for label in section_labels['operating_income'] if label.casefold() != 'operating income')
                    if statement == "cash_flow" and any(
                        has_distinct_capital_payment_label(_row_label_text(row)) for row in table.rows
                    ):
                        # Unsupported or combined payment labels are still
                        # original research material, not zero/missing values
                        # or permission to classify them as cash capex.
                        excerpt = "\n".join(row.text for row in table.rows)
                        disclosure_id = hashlib.sha256(f"{filing.document_id}|{page_number}|{excerpt}".encode()).hexdigest()[:24]
                        normalization_refs.append(EvidenceRef(
                            f"disclosure:{disclosure_id}", filing.document_id, filing.source_url,
                            "capital allocation payments disclosure", f"page:{page_number}", excerpt,
                            filing.filed_at, filing.content_hash,
                        ))
                    summary_page = section.summary
                    revision_boundary = _summary_revision_boundary(table.rows) if summary_page else None
                    if revision_boundary is not None:
                        # Preserve the complete comparison as source material,
                        # but do not pretend its preliminary/final columns are
                        # current/prior fiscal years from an earlier table.
                        excerpt = "\n".join(row.text for row in table.rows[revision_boundary:])
                        disclosure_id = hashlib.sha256(
                            f"{filing.document_id}|{page_number}|revision|{excerpt}".encode()
                        ).hexdigest()[:24]
                        normalization_refs.append(EvidenceRef(
                            f"disclosure:{disclosure_id}", filing.document_id, filing.source_url,
                            "preliminary versus audited financial metrics disclosure",
                            f"page:{page_number}", excerpt, filing.filed_at, filing.content_hash,
                        ))
                    revenue_totals = _revenue_group_total_rows(
                        table.rows, columns, int(manifest.period_end[:4])
                    ) if statement == "income_statement" and not summary_page else {}
                    equity_totals = _equity_group_total_rows(
                        table.rows, columns, int(manifest.period_end[:4])
                    ) if statement == "balance_sheet" and not summary_page else {}
                    for row_index, row in enumerate(table.rows):
                        if revision_boundary is not None and row_index >= revision_boundary:
                            continue
                        merged_row = _merge_visual_rows(table.rows, row_index, rules)
                        if row.bbox in equity_totals:
                            merged_row = equity_totals[row.bbox]
                        compact = _row_label_text(merged_row)
                        # A prose date range is not a financial statement row;
                        # do not let its years become revenue/net-income values.
                        if _is_narrative_date_header(merged_row.text):
                            continue
                        # Attribution headings are often a separate visual
                        # row; keep the context bounded to this table/column.
                        attribution = _attribution_context(table.rows, row_index)
                        candidate_context = " ".join(part for part in (attribution, compact) if part)
                        if "经营活动产生的现金流" in compact and "净额" not in compact:
                            # Some CNINFO tables split the final two Chinese
                            # characters of the OCF label onto a following
                            # visual row. Extend only this bounded candidate,
                            # retaining the current statement context.
                            extra_rows = [merged_row]
                            for probe in table.rows[row_index + 1:row_index + 3]:
                                extra_rows.append(probe)
                                joined = PdfRowAST(
                                    tuple(cell for item in extra_rows for cell in item.cells),
                                    merged_row.top,
                                    merged_row.bbox,
                                )
                                if "量净额" in _row_label_text(joined):
                                    merged_row = joined
                                    compact = _row_label_text(merged_row)
                                    break
                        net_label = _label_compact(compact)
                        if (
                            "netprofitattributableto" in net_label
                            and "shareholdersoftheparentcompany" not in net_label
                        ):
                            # BYD and similar bilingual filings put the numeric
                            # attributable-profit row before the final label
                            # fragment (``shareholders of the parent company``).
                            # Append only following label cells, never their
                            # numeric values, so the comparison column remains
                            # bound to the original canonical row.
                            tail_cells: list[PdfCellAST] = []
                            base_has_values = any(
                                _parse_number(cell.text) is not None
                                for cell in merged_row.cells
                            )
                            for probe in table.rows[row_index + 1:row_index + 4]:
                                probe_has_values = any(
                                    _parse_number(cell.text) is not None
                                    for cell in probe.cells
                                )
                                if base_has_values and probe_has_values:
                                    break
                                tail_cells.extend(
                                    cell for cell in probe.cells
                                    if not base_has_values or _parse_number(cell.text) is None
                                )
                                tail = _label_compact(" ".join(cell.text for cell in tail_cells))
                                combined_has_values = base_has_values or any(
                                    _parse_number(cell.text) is not None
                                    for cell in tail_cells
                                )
                                if "shareholdersoftheparentcompany" in tail and combined_has_values:
                                    merged_row = PdfRowAST(
                                        tuple((*merged_row.cells, *tail_cells)),
                                        merged_row.top,
                                        (
                                            min(merged_row.bbox[0], *(cell.x0 for cell in tail_cells)),
                                            min(merged_row.bbox[1], *(cell.top for cell in tail_cells)),
                                            max(merged_row.bbox[2], *(cell.x1 for cell in tail_cells)),
                                            max(merged_row.bbox[3], *(cell.bottom for cell in tail_cells)),
                                        ),
                                    )
                                    compact = _row_label_text(merged_row)
                                    break
                        row_profit_concept = profit_row_concept(compact, attribution)
                        interest_parent = _interest_parent_row(table.rows, row_index)
                        interest_concept = interest_income_row_concept(
                            compact, _row_label_text(interest_parent) if interest_parent else "",
                        )
                        if interest_concept and interest_parent is not None:
                            # The literal parent label is part of positioned proof.
                            # Its amounts are not values of this child row.
                            merged_row = PdfRowAST(
                                tuple((*(
                                    cell for cell in interest_parent.cells
                                    if _parse_number(cell.text) is None
                                ), *merged_row.cells)), merged_row.top,
                                (min(interest_parent.bbox[0], merged_row.bbox[0]),
                                 min(interest_parent.bbox[1], merged_row.bbox[1]),
                                 max(interest_parent.bbox[2], merged_row.bbox[2]),
                                 max(interest_parent.bbox[3], merged_row.bbox[3])),
                            )
                        row_comprehensive_concept = comprehensive_income_row_concept(compact, attribution)
                        following_rows = table.rows[row_index + 1:row_index + 4]
                        row_equity_concept = equity_row_concept(
                            compact, following_labels=tuple(_row_label_text(item) for item in following_rows),
                        )
                        needs_allocation_heading = (
                            row_profit_concept and profit_row_concept(compact) is None
                        ) or (
                            row_comprehensive_concept and comprehensive_income_row_concept(compact) is None
                        )
                        if needs_allocation_heading and attribution:
                            # Keep the literal classification heading in
                            # positioned proof without importing a sibling
                            # allocation row's numeric cells.
                            merged_row = _append_allocation_heading_proof(
                                table.rows, row_index, merged_row, attribution,
                            )
                            compact = _row_label_text(merged_row)
                        allocation_concept = capital_allocation_payment_concept(compact)
                        for concept, labels in section_labels.items():
                            if concept in {"interest_revenue", "finance_interest_income"} and concept != interest_concept:
                                continue
                            if concept == "capital_expenditure" and has_distinct_capital_payment_label(compact):
                                continue
                            if concept in CAPITAL_ALLOCATION_PAYMENT_ALIASES and allocation_concept != concept:
                                continue
                            if _STATEMENT_FOR[concept] == "comprehensive_income" and row_comprehensive_concept != concept:
                                continue
                            if concept == "equity" and row_equity_concept != "equity":
                                continue
                            if concept in {"mezzanine_equity", "minority_interest_equity"} and balance_component_row_concept(compact) != concept:
                                continue
                            if concept == "total_equity" and row_equity_concept != "total_equity":
                                continue
                            if (
                                concept == "revenue" and row.bbox not in revenue_totals
                                and not total_revenue_label_matches(compact, labels)
                            ):
                                continue
                            if concept in {"net_income", "parent_net_income", "minority_interest_profit", "ordinary_shareholder_net_income"} and concept != row_profit_concept:
                                continue
                            if concept == "operating_cash_flow" and "现金流出小计" in compact:
                                continue
                            if concept == "operating_cash_flow" and not cash_flow_total_label_matches(compact, labels):
                                continue
                            if concept == "assets" and "资产合计" in compact and "资产总计" not in compact and any(prefix in compact for prefix in ("流动资产", "非流动资产")):
                                continue
                            if concept == "liabilities" and "负债合计" in compact and any(prefix in compact for prefix in ("流动负债", "非流动负债")):
                                continue
                            if concept == "liabilities" and not liability_total_label_matches(compact):
                                continue
                            # ``revenue`` is a substring of cost-of-revenue
                            # rows in many IFRS income statements.  A cost
                            # row is not a revenue fact; keep matching bounded
                            # to the formal revenue line instead of allowing
                            # first-match selection to leak a nearby value.
                            if concept == "revenue" and any(
                                marker in _label_compact(compact)
                                for marker in ("costofrevenue", "costofsales", "营业成本")
                            ):
                                continue
                            compact_normalized = _label_compact(compact)
                            label = next(
                                (
                                    candidate
                                    for candidate in labels
                                    if _label_compact(candidate) in compact_normalized
                                    and not (
                                        concept == "net_income"
                                        and _label_compact(candidate) in {"netprofit", "profitaftertax"}
                                        and re.search(
                                            rf"{re.escape(_label_compact(candidate))}[a-z]",
                                            compact_normalized,
                                        )
                                    )
                                ),
                                None,
                            )
                            if (
                                concept == "profit_after_tax"
                                and label is not None
                                and _label_compact(label) in {
                                    _label_compact(item) for item in _GENERIC_NET_INCOME_LABELS
                                }
                            ):
                                # Generic IFRS profit is represented as a
                                # conservative net_income candidate.  Keep
                                # profit_after_tax distinct for non-generic
                                # labels rather than globally renaming facts.
                                continue
                            if label is None and concept == "revenue" and row.bbox in revenue_totals:
                                label = "revenue"
                            if label is None and concept == "equity" and row.bbox in equity_totals:
                                label = "equity attributable to equity holders of the company"
                            if label is None and concept == "equity" and row_equity_concept == "equity":
                                label = _row_label_source_text(merged_row)
                            if (
                                label is None
                                and concept == "capital_expenditure"
                            ):
                                # Retain the observed label so the existing
                                # component classifier can distinguish an
                                # aggregate PPE+intangibles outflow from
                                # separately disclosed component rows.  The
                                # verbatim wording remains in raw_text.
                                label = _capex_cash_outflow_label(compact) or None
                            if concept in {"share_repurchase", "shareholder_dividends_paid"}:
                                capital_return = _capital_return_label(compact)
                                if capital_return is None or capital_return[0] != concept:
                                    continue
                                label = capital_return[1]
                            # Summary labels such as 毛利率 contain 毛利.  Only
                            # an explicitly reported ratio may be accepted from
                            # a summary page; monetary facts require their
                            # formal statement context.
                            if summary_page and concept != "reported_roe":
                                continue
                            if label is None or (statement != _STATEMENT_FOR[concept] and concept != "reported_roe"):
                                continue
                            if concept == "liabilities" and any(
                                token in _label_compact(compact)
                                for token in ("totalliabilitiesandequity", "totalliabilitiesandshareholdersequity")
                            ):
                                continue
                            if concept in {"net_income", "parent_net_income"} and not _net_income_candidate_allowed(candidate_context):
                                continue
                            if concept == "operating_income" and "non-operating" in merged_row.text.casefold():
                                continue
                            if concept == "operating_cash_flow" and label == "经营活动产生的现金流":
                                # The fragment also prefixes cash-inflow rows;
                                # require the continuation's ``量净额`` marker.
                                if "净额" not in compact and "量净" not in compact:
                                    continue
                            # Do not mistake subtotal rows such as 流动资产合计 for
                            # the statement-level 资产合计 row.
                            value_cells = [cell for cell in merged_row.cells if _parse_number(cell.text) is not None]
                            first_value_x = min((cell.x0 for cell in value_cells), default=float("inf"))
                            label_cells = [
                                cell for cell in merged_row.cells
                                if _parse_number(cell.text) is None and cell.x1 <= first_value_x + 4
                            ]
                            normalized_label = _label_compact(label)
                            label_cell = next((cell for cell in label_cells if normalized_label in _label_compact(cell.text)), None)
                            if label_cell is not None and _label_compact(label_cell.text) != normalized_label:
                                prefix = _label_compact(label_cell.text).split(normalized_label, 1)[0]
                                numbered_prefix = bool(re.fullmatch(
                                    r"[一二三四五六七八九十百千万亿零〇0-9]+", prefix
                                ))
                                if (
                                    prefix
                                    and any("\u4e00" <= char <= "\u9fff" for char in prefix[-1:])
                                    and not numbered_prefix
                                    and not (concept == "revenue" and prefix == "其中")
                                ):
                                    continue
                            label_end = max(
                                (
                                    cell.x1 for cell in label_cells
                                    if not re.fullmatch(r"[一二三四五六七八九十百千万亿\d、.．()（）]+", re.sub(r"\s+", "", cell.text))
                                ),
                                default=0.0,
                            )
                            if summary_page and not columns:
                                selected = min(
                                    (cell for cell in value_cells if cell.x0 >= label_end),
                                    key=lambda cell: cell.x0,
                                    default=None,
                                )
                            else:
                                selected = _select_period_cell(merged_row, label_end, columns, int(manifest.period_end[:4]))
                            if selected is None:
                                continue
                            parsed_source = _parse_decimal(selected.text)
                            if parsed_source is None:
                                continue
                            raw_decimal, cell_percent = parsed_source
                            if concept in {
                                "capital_expenditure", "share_repurchase",
                                "shareholder_dividends_paid",
                                "lease_liability_payment", "deferred_asset_acquisition_payment",
                            }:
                                value_transform = "absolute_value"
                            else:
                                value_transform = profit_loss_value_transform(compact, concept)
                            selected_center = (selected.x0 + selected.x1) / 2
                            selected_column = next(
                                (column for column in columns if column.left <= selected_center <= column.right),
                                None,
                            )
                            fact_multiplier = (
                                selected_column.unit_scale
                                if selected_column is not None and selected_column.currency
                                else multiplier
                            )
                            fact_currency = (
                                selected_column.currency
                                if selected_column is not None and selected_column.currency
                                else table_currency
                            )
                            fact_unit_provenance = section.context.unit_provenance
                            if (
                                fact_unit_provenance == "unknown"
                                and selected_column is not None
                                and selected_column.currency
                            ):
                                fact_unit_provenance = "explicit"
                            source_scale = Decimal(str(fact_multiplier))
                            if concept == "reported_roe":
                                # Percent semantics come from the disclosed
                                # cell/row, never from magnitude. Keep the
                                # displayed decimal and the percent scale
                                # separate so the source amount is reproducible.
                                fact_unit_provenance, source_scale, ratio_unit_excerpt = _ratio_unit_source(
                                    merged_row, selected, table.rows, summary=summary_page)
                                value_transform = "identity"
                            normalized_decimal = exact_decimal_arithmetic((raw_decimal, source_scale), "multiply")
                            if value_transform == "absolute_value":
                                normalized_decimal = normalized_decimal.copy_abs()
                            elif value_transform == "negative_absolute_value":
                                normalized_decimal = normalized_decimal.copy_abs().copy_negate()
                            value = float(normalized_decimal)
                            # Avoid repeated nested rows and parent-only tables.
                            period_start = _period_start(manifest) if (
                                statement in {"income_statement", "cash_flow", "comprehensive_income"}
                                or (summary_page and concept == "reported_roe")
                            ) else None
                            fact_concept = concept
                            if concept == "reported_roe" and (
                                "扣除非经常性损益后" in compact
                                or "excludingnonrecurring" in _label_compact(compact)
                            ):
                                fact_concept = "reported_roe_excluding_nonrecurring"
                            identity = f"{filing.document_id}|{fact_concept}|{page_number}|{merged_row.bbox}|{format(normalized_decimal, 'f')}"
                            fact_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
                            unit_excerpt = (
                                section.context.unit_evidence
                                or _unit_evidence_excerpt(page_text, rules)
                                or _unit_evidence_excerpt(table_text, rules)
                                or _unit_evidence_excerpt(merged_row.text, rules)
                            )
                            unit_page = (
                                section.context.unit_evidence_page
                                if section.context.unit_evidence
                                and unit_excerpt == section.context.unit_evidence
                                else page_number
                            )
                            if concept == "reported_roe":
                                # A money declaration cannot prove ratio units.
                                # Bind the actual percent cell/row, or the
                                # percent header of this summary table only.
                                unit_excerpt = ratio_unit_excerpt
                                unit_page = page_number
                            normalization_lineage = None
                            unit_ref = None
                            extra_unit_refs: tuple[EvidenceRef, ...] = ()
                            if unit_excerpt and concept == "reported_roe" and fact_unit_provenance in {"explicit_percent", "explicit_ratio"}:
                                try:
                                    normalization_lineage = SourceAmount(
                                        _raw_decimal_token(selected.text),
                                        format(source_scale.normalize(), "f"),
                                        QuantityKind.RATIO,
                                        "",
                                        (f"unit:{fact_id}",),
                                        value_transform,
                                    ).normalize()
                                except ValueError:
                                    normalization_lineage = None
                            elif (
                                unit_excerpt
                                and concept != "reported_roe"
                                and fact_unit_provenance != "unknown"
                                and re.fullmatch(r"[A-Z]{3}", str(fact_currency or "").upper())
                            ):
                                extra_unit_refs = _extra_money_unit_evidence(
                                    fact_id, filing, excerpt=unit_excerpt, column=selected_column,
                                    page=page_number, currency=str(fact_currency).upper(),
                                    scale=source_scale, scope=section.context.scope,
                                    document_units=document_units,
                                )
                                if any(ref.evidence_id.startswith("unit:statutory:") for ref in extra_unit_refs):
                                    fact_unit_provenance = "statutory_presentation_basis"
                                try:
                                    normalization_lineage = SourceAmount(
                                        _raw_decimal_token(selected.text),
                                        format(source_scale.normalize(), "f"),
                                        QuantityKind.MONEY,
                                        str(fact_currency).upper(),
                                        (f"unit:{fact_id}", *(ref.evidence_id for ref in extra_unit_refs)),
                                        value_transform,
                                    ).normalize()
                                except ValueError:
                                    normalization_lineage = None
                            if normalization_lineage is not None:
                                normalization_refs.extend(extra_unit_refs)
                                unit_ref = EvidenceRef(
                                    f"unit:{fact_id}", filing.document_id, filing.source_url,
                                    f"{manifest.form_type} {manifest.period_end} / displayed unit",
                                    f"page:{unit_page}", unit_excerpt, filing.filed_at,
                                    filing.content_hash,
                                )
                            fact = FinancialFact(
                                fact_id=f"ingest:{fact_id}", company_cik=company.security_id,
                                concept=fact_concept, reported_concept=(
                                    _row_label_source_text(merged_row) if concept == "reported_roe" or concept in {
                                        "net_income", "parent_net_income", "minority_interest_profit",
                                        "ordinary_shareholder_net_income",
                                        "profit_before_tax", "operating_income", "equity", "total_equity",
                                        "mezzanine_equity", "minority_interest_equity",
                                        "liabilities", "operating_cash_flow",
                                        "comprehensive_income", "parent_comprehensive_income", "minority_interest_comprehensive_income",
                                        "lease_liability_payment", "deferred_asset_acquisition_payment",
                                    } else label
                                ), value=value,
                                unit="ratio" if concept == "reported_roe" else (fact_currency or company.reporting_currency),
                                fiscal_year=int(manifest.period_end[:4]), fiscal_period=manifest.fiscal_period,
                                form_type=manifest.form_type, start_date=period_start,
                                end_date=manifest.period_end, filed_at=filing.filed_at,
                                accession_number=manifest.accession_number, source_url=manifest.source_url,
                                scope=scope, entity=company.name, market=company.market,
                                statement=statement, period_start=period_start,
                                consolidated_scope=scope, currency=fact_currency or company.reporting_currency,
                                unit_scale=1.0 if concept == "reported_roe" else fact_multiplier,
                                unit_provenance=fact_unit_provenance,
                                revision=manifest.revision,
                                 source_document=manifest.primary_document, source_page=page_number,
                                 source_bbox=merged_row.bbox,
                                 source_column=(selected_column.header if selected_column is not None else ""),
                                 raw_text=merged_row.text, parser_version=_PDF_PARSER_VERSION,
                                normalization_lineage=normalization_lineage,
                                validation_status=ValidationStatus.READY_WITH_WARNINGS.value,
                                classification_evidence_ids=(statement_ref.evidence_id,) if statement_ref else (),
                            )
                            ref = EvidenceRef(
                                f"fact:{fact_id}", filing.document_id, filing.source_url,
                                f"{manifest.form_type} {manifest.period_end} / {label}",
                                f"page:{page_number}", merged_row.text, filing.filed_at,
                                filing.content_hash, merged_row.bbox,
                            )
                            if concept == "equity" and equity_row_concept(compact) != "equity" and row_equity_concept == "equity":
                                context_ids = []
                                for context_row in following_rows:
                                    context_id = "classification:" + hashlib.sha256(
                                        f"{filing.document_id}|{page_number}|{context_row.bbox}|{context_row.text}".encode("utf-8")
                                    ).hexdigest()[:24]
                                    context_ids.append(context_id)
                                    normalization_refs.append(EvidenceRef(
                                        context_id, filing.document_id, filing.source_url,
                                        "equity source hierarchy", f"page:{page_number}",
                                        context_row.text, filing.filed_at, filing.content_hash, context_row.bbox,
                                    ))
                                fact.classification_evidence_ids = (*fact.classification_evidence_ids, *context_ids)
                            existing_index = next(
                                (
                                    index for index, existing in enumerate(facts)
                                    if FactIdentity.from_fact(existing) == FactIdentity.from_fact(fact)
                                    and existing.statement == fact.statement
                                    and existing.usage_status == fact.usage_status
                                ),
                                None,
                            )
                            if existing_index is None:
                                facts.append(fact)
                                refs.append(ref)
                                if unit_ref is not None:
                                    normalization_refs.append(unit_ref)
                            elif concept == "capital_expenditure":
                                merged = _merge_capex_facts(facts[existing_index], fact)
                                if merged is not None:
                                    facts[existing_index] = merged
                                    refs[existing_index] = replace(
                                        ref,
                                        evidence_id=f"fact:{merged.fact_id.removeprefix('ingest:')}",
                                        title=(
                                            f"{manifest.form_type} {manifest.period_end} / "
                                            "capital_expenditure_components"
                                        ),
                                        excerpt=merged.raw_text,
                                        bbox=merged.source_bbox,
                                    )
                                    if unit_ref is not None:
                                        normalization_refs.append(unit_ref)
                                elif all(existing.fact_id != fact.fact_id for existing in facts):
                                    facts.append(fact)
                                    refs.append(ref)
                                    if unit_ref is not None:
                                        normalization_refs.append(unit_ref)
                            elif all(existing.fact_id != fact.fact_id for existing in facts):
                                # Extraction does not adjudicate conflicts.
                                # Never choose an equity value by closeness to
                                # assets minus liabilities, or replace profit
                                # by an attribution/ranking heuristic. Keep
                                # source observations for the pure compiler.
                                facts.append(fact)
                                refs.append(ref)
                                if unit_ref is not None:
                                    normalization_refs.append(unit_ref)

                            # Keep an issuer-provided comparison column as an
                            # auditable hidden fact.  It belongs to the same
                            # filing but has its own period and explicit
                            # comparator usage, so it cannot replace the
                            # current-period fact or masquerade as another
                            # filing.  Only a like-for-like visual column is
                            # eligible; ambiguous headers simply produce no
                            # comparator candidate.
                            if (not summary_page or concept == "reported_roe") and len(columns) > 1:
                                target_year = int(manifest.period_end[:4])
                                for comparison_column in columns:
                                    if comparison_column.year in {0, target_year}:
                                        continue
                                    comparison_cell = _select_period_cell(
                                        merged_row, label_end, columns, comparison_column.year
                                    )
                                    if comparison_cell is None:
                                        continue
                                    comparison_source = _parse_decimal(comparison_cell.text)
                                    if comparison_source is None:
                                        continue
                                    comparison_raw_decimal, comparison_percent = comparison_source
                                    if concept in {
                                        "capital_expenditure", "share_repurchase",
                                        "shareholder_dividends_paid",
                                        "lease_liability_payment", "deferred_asset_acquisition_payment",
                                    }:
                                        comparison_transform = "absolute_value"
                                    else:
                                        comparison_transform = profit_loss_value_transform(compact, concept)
                                    comparison_period = _bind_comparison_period(
                                        comparison_column,
                                        "income_statement" if summary_page and concept == "reported_roe" else statement,
                                        manifest,
                                    )
                                    comparison_end = comparison_period.period_end
                                    comparison_start = comparison_period.period_start
                                    comparison_center = (comparison_cell.x0 + comparison_cell.x1) / 2
                                    comparison_multiplier = (
                                        comparison_column.unit_scale
                                        if comparison_column.currency else multiplier
                                    )
                                    comparison_currency = (
                                        comparison_column.currency or table_currency
                                        or company.reporting_currency
                                    )
                                    comparison_scale = Decimal(str(comparison_multiplier))
                                    if concept == "reported_roe":
                                        comparison_provenance, comparison_scale, comparison_ratio_excerpt = _ratio_unit_source(
                                            merged_row, comparison_cell, table.rows, summary=summary_page)
                                        comparison_transform = "identity"
                                    comparison_decimal = exact_decimal_arithmetic((comparison_raw_decimal, comparison_scale), "multiply")
                                    if comparison_transform == "absolute_value":
                                        comparison_decimal = comparison_decimal.copy_abs()
                                    elif comparison_transform == "negative_absolute_value":
                                        comparison_decimal = comparison_decimal.copy_abs().copy_negate()
                                    comparison_normalized = float(comparison_decimal)
                                    comparison_identity = (
                                        f"{filing.document_id}|{fact_concept}|{page_number}|"
                                        f"{merged_row.bbox}|{comparison_period.fiscal_year}|"
                                        f"{comparison_period.fiscal_period}|{comparison_start}|{comparison_end}|"
                                        f"{comparison_period.basis}|{format(comparison_decimal, 'f')}"
                                    )
                                    comparison_fact_id = hashlib.sha256(
                                        comparison_identity.encode()
                                    ).hexdigest()[:24]
                                    comparison_unit_excerpt = (
                                        section.context.unit_evidence
                                        or _unit_evidence_excerpt(page_text, rules)
                                        or _unit_evidence_excerpt(table_text, rules)
                                        or _unit_evidence_excerpt(merged_row.text, rules)
                                    )
                                    comparison_unit_page = (
                                        section.context.unit_evidence_page
                                        if section.context.unit_evidence
                                        and comparison_unit_excerpt == section.context.unit_evidence
                                        else page_number
                                    )
                                    if concept == "reported_roe":
                                        comparison_unit_excerpt = comparison_ratio_excerpt
                                        comparison_unit_page = page_number
                                    comparison_lineage = None
                                    comparison_unit_ref = None
                                    comparison_extra_units: tuple[EvidenceRef, ...] = ()
                                    if (
                                        comparison_unit_excerpt
                                        and concept == "reported_roe"
                                        and comparison_provenance in {"explicit_percent", "explicit_ratio"}
                                    ):
                                        try:
                                            comparison_lineage = SourceAmount(
                                                _raw_decimal_token(comparison_cell.text),
                                                format(comparison_scale.normalize(), "f"),
                                                QuantityKind.RATIO, "",
                                                (f"unit:{comparison_fact_id}",),
                                            ).normalize()
                                        except ValueError:
                                            comparison_lineage = None
                                    elif (
                                        comparison_unit_excerpt
                                        and concept != "reported_roe"
                                        and re.fullmatch(r"[A-Z]{3}", str(comparison_currency or "").upper())
                                    ):
                                        comparison_extra_units = _extra_money_unit_evidence(
                                            comparison_fact_id, filing,
                                            excerpt=comparison_unit_excerpt, column=comparison_column,
                                            page=page_number, currency=str(comparison_currency).upper(),
                                            scale=comparison_scale, scope=section.context.scope,
                                            document_units=document_units,
                                        )
                                        try:
                                            comparison_lineage = SourceAmount(
                                                _raw_decimal_token(comparison_cell.text),
                                                format(comparison_scale.normalize(), "f"),
                                                QuantityKind.MONEY,
                                                str(comparison_currency).upper(),
                                                (f"unit:{comparison_fact_id}", *(ref.evidence_id for ref in comparison_extra_units)),
                                                comparison_transform,
                                            ).normalize()
                                        except ValueError:
                                            comparison_lineage = None
                                    if comparison_lineage is not None:
                                        normalization_refs.extend(comparison_extra_units)
                                        comparison_unit_ref = EvidenceRef(
                                            f"unit:{comparison_fact_id}", filing.document_id,
                                            filing.source_url,
                                            f"{manifest.form_type} {comparison_end or 'period undetermined'} / displayed unit",
                                            f"page:{comparison_unit_page}", comparison_unit_excerpt,
                                            filing.filed_at, filing.content_hash,
                                        )
                                    comparison_fact = replace(
                                        fact,
                                        fact_id=f"ingest:{comparison_fact_id}",
                                        value=comparison_normalized,
                                        unit=("ratio" if concept == "reported_roe" else comparison_currency),
                                        currency=comparison_currency,
                                        unit_scale=1.0 if concept == "reported_roe" else comparison_multiplier,
                                        normalization_lineage=comparison_lineage,
                                        unit_provenance=("statutory_presentation_basis" if any(
                                            ref.evidence_id.startswith("unit:statutory:") for ref in comparison_extra_units)
                                            else comparison_provenance if concept == "reported_roe"
                                            else fact.unit_provenance),
                                        fiscal_year=comparison_period.fiscal_year,
                                        fiscal_period=comparison_period.fiscal_period,
                                        start_date=comparison_start,
                                        end_date=comparison_end,
                                        period_start=comparison_start,
                                        usage_status="comparator",
                                         source_column=comparison_column.header,
                                         raw_text=merged_row.text,
                                    )
                                    comparison_ref = replace(
                                        ref,
                                        evidence_id=f"fact:{comparison_fact_id}",
                                        title=(
                                            f"{manifest.form_type} {comparison_end or 'period undetermined'} / {label} "
                                            f"column:{comparison_column.header or comparison_column.year} / "
                                            f"period basis:{comparison_period.basis}"
                                        ),
                                        locator=f"page:{page_number}",
                                        excerpt=comparison_fact.raw_text,
                                    )
                                    comparison_index = next((
                                        position
                                        for position, existing in enumerate(facts)
                                        if FactIdentity.from_fact(existing) == FactIdentity.from_fact(comparison_fact)
                                        and existing.statement == comparison_fact.statement
                                        and existing.usage_status == "comparator"
                                    ), None)
                                    if comparison_index is None:
                                        facts.append(comparison_fact)
                                        refs.append(comparison_ref)
                                        if comparison_unit_ref is not None:
                                            normalization_refs.append(comparison_unit_ref)
                                    elif concept == "capital_expenditure":
                                        merged = _merge_capex_facts(
                                            facts[comparison_index], comparison_fact
                                        )
                                        if merged is not None:
                                            facts[comparison_index] = merged
                                            refs[comparison_index] = replace(
                                                comparison_ref,
                                                evidence_id=(
                                                    f"fact:{merged.fact_id.removeprefix('ingest:')}"
                                                ),
                                                title=(
                                                    f"{manifest.form_type} {comparison_end} / "
                                                    "capital_expenditure_components"
                                                ),
                                                excerpt=merged.raw_text,
                                                bbox=merged.source_bbox,
                                            )
                                            if comparison_unit_ref is not None:
                                                normalization_refs.append(comparison_unit_ref)
                                        elif all(existing.fact_id != comparison_fact.fact_id for existing in facts):
                                            facts.append(comparison_fact)
                                            refs.append(comparison_ref)
                                            if comparison_unit_ref is not None:
                                                normalization_refs.append(comparison_unit_ref)
                                    elif all(existing.fact_id != comparison_fact.fact_id for existing in facts):
                                        facts.append(comparison_fact)
                                        refs.append(comparison_ref)
                                        if comparison_unit_ref is not None:
                                            normalization_refs.append(comparison_unit_ref)
                # Only the last section can continue onto the next page.  A
                # parent section therefore correctly replaces a consolidated
                # context at a same-page boundary.
                if sections:
                    previous = sections[-1].context
                else:
                    previous = None
        self._last_pdf_context = previous
        return [*facts, *disclosure_facts], [*refs, *normalization_refs, *disclosure_refs]


# ---------------------------------------------------------------------------
# Compatibility functions for the service/storage seam. These functions use
# the same label/unit policy as the engine and never bypass its validation gate.
# ---------------------------------------------------------------------------

def parse_structured_snapshot(raw_excerpt: str, compatibility_rules: FinancialRulesSnapshot | None = None) -> dict[str, float]:
    """Parse a bounded, provider-supplied excerpt into normalized values.

    This is intentionally limited to one row at a time; it is not a PDF page
    parser and therefore cannot silently infer a value from unrelated prose.
    """
    scale, _ = _unit_scale(raw_excerpt, compatibility_rules)
    result: dict[str, float] = {}
    compact = re.sub(r"\s+", " ", raw_excerpt)
    # Structured adapters commonly serialize a bounded snapshot as
    # concept=value pairs. Accept only the known concept identifiers.
    for key, token in re.findall(r"([a-z_]+)\s*=\s*([-+]?\d[\d,]*(?:\.\d+)?)", raw_excerpt, flags=re.IGNORECASE):
        if key in _labels_for_rules(compatibility_rules):
            result[key] = float(token.replace(",", "")) * (1.0 if key == "reported_roe" else scale)
    if result:
        return result
    for concept, labels in _labels_for_rules(compatibility_rules).items():
        for label in sorted(labels, key=len, reverse=True):
            match = re.search(re.escape(label), compact, flags=re.IGNORECASE)
            if not match:
                continue
            tail = compact[match.end(): match.end() + 300]
            number_match = re.search(r"(?:\(\s*[+-]?[\d,]+(?:\.\d+)?\s*\)|[+-]?[\d,]+(?:\.\d+)?%?)", tail)
            if not number_match:
                continue
            value = _parse_number(number_match.group())
            if value is None:
                continue
            result[concept] = value if concept == "reported_roe" else value * scale
            break
    return result


def _source_numeric_for_concept(
    raw_excerpt: str,
    concept: str,
    compatibility_rules: FinancialRulesSnapshot | None = None,
) -> tuple[str, Decimal, bool] | None:
    """Recover the exact displayed token from the bounded source excerpt."""
    key_pattern = re.compile(
        rf"({re.escape(concept)})\s*=\s*({_NUM.pattern}%?)",
        re.IGNORECASE,
    )
    match = key_pattern.search(raw_excerpt)
    if match:
        parsed = _parse_decimal(match.group(2))
        if parsed is not None:
            return _raw_decimal_token(match.group(2)), parsed[0], parsed[1]
    compact = re.sub(r"\s+", " ", raw_excerpt)
    for label in sorted(_labels_for_rules(compatibility_rules).get(concept, ()), key=len, reverse=True):
        label_match = re.search(re.escape(label), compact, flags=re.IGNORECASE)
        if not label_match:
            continue
        tail = compact[label_match.end(): label_match.end() + 300]
        number_match = re.search(
            r"(?:\(\s*[+-]?[\d,]+(?:\.\d+)?\s*\)|[+-]?[\d,]+(?:\.\d+)?%?)",
            tail,
        )
        if not number_match:
            continue
        token = number_match.group()
        parsed = _parse_decimal(token)
        if parsed is not None:
            return _raw_decimal_token(token), parsed[0], parsed[1]
    return None


def parse_financial_pages(
    pages: Sequence[tuple[int, str]], filing: FilingDocument, company: Company,
    compatibility_rules: FinancialRulesSnapshot | None = None,
) -> tuple[list[FinancialFact], list[EvidenceRef]]:
    """Compatibility adapter for bounded provider excerpts and unit tests."""
    manifest = _manifest_for(filing)
    if manifest is None:
        return [], []
    manifest = _manifest_from_observed_pages(
        manifest, filing, tuple(raw_text for _page_number, raw_text in pages)
    )
    if not manifest.period_end:
        return [], []
    facts: list[FinancialFact] = []
    evidence: list[EvidenceRef] = []
    for page_number, raw_text in pages:
        values = parse_structured_snapshot(raw_text, compatibility_rules)
        statement_context = _statement_context(raw_text, compatibility_rules)
        if statement_context is None and "=" not in raw_text:
            # A free-form narrative is not a statement table. Structured
            # key=value snapshots are the only context-free compatibility form.
            continue
        for concept, value in values.items():
            statement = _STATEMENT_FOR[concept]
            if statement_context and statement_context[0] != statement and concept != "reported_roe":
                continue
            scale, currency_hint = _unit_scale(raw_text, compatibility_rules)
            # This legacy adapter may use the old unit parser for the display
            # multiplier, but a bare ``元`` is not proof of a reporting
            # currency. Keep only a uniquely named currency from the source.
            declared_currencies = _explicit_currencies(raw_text, compatibility_rules)
            currency_hint = next(iter(declared_currencies)) if len(declared_currencies) == 1 else ""
            scope = statement_context[1] if statement_context else "consolidated"
            period_start = None if statement == "balance_sheet" else _period_start(manifest)
            fact_id = hashlib.sha256(f"{filing.document_id}|{concept}|{page_number}|{value}".encode()).hexdigest()[:24]
            unit_excerpt = _unit_evidence_excerpt(raw_text, compatibility_rules)
            source_value = _source_numeric_for_concept(
                raw_text, concept, compatibility_rules
            )
            normalization_lineage = None
            unit_ref = None
            if source_value is not None and unit_excerpt:
                raw_token, raw_decimal, is_percent = source_value
                unit_evidence_id = f"unit:{fact_id}"
                if concept == "reported_roe":
                    if is_percent:
                        source_scale = Decimal("0.01")
                    elif any(marker in raw_text for marker in ("%", "％", "百分比")):
                        source_scale = Decimal("0.01")
                    else:
                        source_scale = Decimal("1")
                    quantity_kind = QuantityKind.RATIO
                    lineage_currency = ""
                    value_transform = "identity"
                else:
                    source_scale = Decimal(str(scale))
                    quantity_kind = QuantityKind.MONEY
                    lineage_currency = str(currency_hint or "").upper()
                    value_transform = (
                        "absolute_value"
                        if concept in {"capital_expenditure", "share_repurchase", "shareholder_dividends_paid"}
                        else "identity"
                    )
                if (
                    quantity_kind is QuantityKind.RATIO
                    or re.fullmatch(r"[A-Z]{3}", lineage_currency)
                ):
                    try:
                        normalization_lineage = SourceAmount(
                            raw_token, format(source_scale.normalize(), "f"), quantity_kind,
                            lineage_currency, (unit_evidence_id,), value_transform,
                        ).normalize()
                        value = float(Decimal(normalization_lineage.normalized_value))
                        unit_ref = EvidenceRef(
                            unit_evidence_id, filing.document_id, filing.source_url,
                            f"{manifest.form_type} {manifest.period_end} / displayed unit",
                            f"page:{page_number}", unit_excerpt, filing.filed_at,
                            filing.content_hash,
                        )
                    except ValueError:
                        normalization_lineage = None
            fact = FinancialFact(
                f"compat:{fact_id}", company.security_id, concept, concept, value,
                "ratio" if concept == "reported_roe" else (currency_hint or ""),
                int(manifest.period_end[:4]), manifest.fiscal_period, manifest.form_type,
                period_start, manifest.period_end, filing.filed_at, manifest.accession_number,
                manifest.source_url, scope=scope, entity=company.name, market=company.market,
                statement=statement, period_start=period_start, consolidated_scope=scope,
                currency=currency_hint or "", unit_scale=1.0 if concept == "reported_roe" else scale,
                unit_provenance=("explicit" if _explicit_unit_info(raw_text, compatibility_rules)[2] else "unknown"),
                source_document=manifest.primary_document, source_page=page_number,
                raw_text=raw_text[:2000], parser_version="financial-ingestion-compat-v1",
                normalization_lineage=normalization_lineage,
                validation_status=ValidationStatus.READY_WITH_WARNINGS.value,
            )
            facts.append(fact)
            evidence.append(EvidenceRef(
                f"fact:compat:{fact_id}", filing.document_id, filing.source_url,
                f"{manifest.form_type} {manifest.period_end} / {concept}",
                f"page:{page_number}", raw_text[:2000], filing.filed_at, filing.content_hash,
            ))
            if unit_ref is not None:
                evidence.append(unit_ref)
    # Keep the first occurrence of each concept, preserving statement order.
    unique: dict[str, FinancialFact] = {}
    for fact in facts:
        unique.setdefault(fact.concept, fact)
    selected_ids = {fact.fact_id for fact in unique.values()}
    selected_evidence_ids = {
        f"fact:{fact.fact_id}" for fact in unique.values()
    }
    selected_evidence_ids.update(
        evidence_id
        for fact in unique.values()
        if fact.normalization_lineage is not None
        for evidence_id in fact.normalization_lineage.unit_evidence_ids
    )
    return list(unique.values()), [
        ref for ref in evidence if ref.evidence_id in selected_evidence_ids
    ]


def ingest_official_pdf(filing: FilingDocument, company: Company):
    """Service adapter backed by the public engine seam."""
    dataset = FinancialIngestionEngine().ingest(company, [filing])
    if not dataset.accepted_facts:
        raise FinancialExtractionError("; ".join(dataset.diagnostics) or "no_financial_facts")
    warnings = tuple(dataset.diagnostics)
    return list(dataset.accepted_facts), list(dataset.evidence), warnings


# Captured after class construction so tests that monkeypatch the parser do
# not accidentally masquerade as the production, process-isolated path.
_ORIGINAL_PARSE_PDF_AST = FinancialIngestionEngine._parse_pdf_ast
