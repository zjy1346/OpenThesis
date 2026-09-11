"""Structured-first financial ingestion with auditable PDF table parsing.

The public seam in this module deliberately separates source adapters from the
normalisation/validation policy.  A PDF is parsed as positioned words grouped
into page/table/row/cell nodes; numbers are never selected from an unscoped
page-wide regular-expression match.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
import hashlib
import inspect
import json
import multiprocessing as mp
import os
import queue
import re
import tempfile
import threading
import time
import types


_PDF_PARSER_VERSION = "financial-ingestion-ast-v6"
_PDF_TAXONOMY_VERSION = "canonical-taxonomy-v1"
_PDF_CACHE_POLICY_VERSION = "parse-cache-v1"


@dataclass
class _PdfParseFlight:
    """In-process single-flight state for one content-addressed parse."""

    event: threading.Event
    result: tuple[list[FinancialFact], list[EvidenceRef], str | None] | None = None


_PDF_FLIGHT_LOCK = threading.Lock()
_PDF_FLIGHTS: dict[str, _PdfParseFlight] = {}

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .disclosure_identity import DisclosureIdentityResolver, _date_tokens
from .financial_compatibility import FinancialRulesSnapshot
from .market_financials import FinancialValidation, ValidationStatus
from .vision_financials import (
    VISION_MAX_PAGES,
    VisionFallbackConfig,
    VisionFinancialSourceAdapter,
    VisionPageRequest,
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


@dataclass(frozen=True, slots=True)
class PdfRowAST:
    cells: tuple[PdfCellAST, ...]
    top: float
    bbox: tuple[float, float, float, float]

    @property
    def text(self) -> str:
        return " ".join(cell.text for cell in self.cells)


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
            }
            for item in context.periods
        ],
        "last_page": context.last_page,
        "inherited_pages": context.inherited_pages,
        "unit_provenance": context.unit_provenance,
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
            )
            for item in value.get("periods", ())
        )
        return PdfTableContext(
            str(value["statement"]), str(value.get("scope", "consolidated")),
            float(value.get("multiplier", 1.0)), str(value.get("currency", "")),
            bool(value.get("unit_explicit", False)), periods,
            int(value.get("last_page", 0)), int(value.get("inherited_pages", 0)),
            str(value.get("unit_provenance", "unknown")),
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
class FinancialGroupValidation:
    identity: tuple[str, str, str, str, str]
    validation: FinancialValidation


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


@dataclass(frozen=True, slots=True)
class FinancialProfile:
    """Validated financial view consumed by deterministic metrics and research."""

    facts: tuple[FinancialFact, ...]
    fact_dicts: tuple[dict[str, Any], ...]
    metrics: tuple[dict[str, Any], ...]
    interim_metrics: tuple[dict[str, Any], ...]
    validation_groups: tuple[FinancialGroupValidation, ...]
    rejected_periods: tuple[dict[str, Any], ...]
    reporting_currency: str
    status: ValidationStatus
    period_continuity: tuple[dict[str, Any], ...] = ()
    period_coverage: dict[str, Any] = field(default_factory=dict)


def build_financial_profile(
    facts: Sequence[FinancialFact],
    validation_groups: Sequence[FinancialGroupValidation] = (),
    reporting_currency: str = "",
    *,
    selected_filings: Sequence[FilingDocument] = (),
    manifests: Sequence[FilingManifest] = (),
    requested_annual_count: int | None = None,
    research_as_of: str | None = None,
) -> FinancialProfile:
    """Build one trusted, period-aware profile without reimplementing gate logic.

    ``selected_filings``/``manifests`` make an absent fact group observable.  A
    selected annual report with no accepted facts is represented as ``no_facts``
    rather than silently disappearing from the continuity view.
    """

    from .financials import calculate_interim_metrics, calculate_metrics

    groups = tuple(validation_groups)
    rejected_accessions = {
        group.identity[0]
        for group in groups
        if group.validation.status is ValidationStatus.REJECTED
    }
    accepted = tuple(
        fact for fact in facts
        if fact.validation_status != ValidationStatus.REJECTED.value
        and fact.accession_number not in rejected_accessions
    )
    fact_dicts = tuple(fact.to_dict() for fact in accepted)

    period_rows: dict[str, dict[str, Any]] = {}
    for filing in selected_filings:
        period_rows[filing.accession_number] = {
            "accession_number": filing.accession_number,
            "period_end": filing.period_end,
            "fiscal_period": filing.fiscal_period,
            "form_type": filing.form_type,
            "scope": "",
            "currency": "",
            "status": "no_facts",
            "issues": (),
        }
    for manifest in manifests:
        row = period_rows.setdefault(manifest.accession_number, {
            "accession_number": manifest.accession_number,
            "period_end": manifest.period_end,
            "fiscal_period": manifest.fiscal_period,
            "form_type": manifest.form_type,
            "scope": "",
            "currency": "",
            "status": "no_facts",
            "issues": (),
        })
        row.update(period_end=manifest.period_end, fiscal_period=manifest.fiscal_period, form_type=manifest.form_type)
    for group in groups:
        accession, period_end, fiscal_period, scope, currency = group.identity
        row = period_rows.setdefault(accession, {
            "accession_number": accession,
            "period_end": period_end,
            "fiscal_period": fiscal_period,
            "form_type": "",
            "scope": scope,
            "currency": currency,
            "status": "no_facts",
            "issues": (),
        })
        row.update(
            period_end=period_end,
            fiscal_period=fiscal_period,
            scope=scope,
            currency=currency,
            status=("rejected" if group.validation.status is ValidationStatus.REJECTED else "accepted"),
            issues=tuple(group.validation.issues),
        )
    accepted_accessions = {fact.accession_number for fact in accepted}
    for accession, row in period_rows.items():
        if row["status"] == "no_facts" and accession in accepted_accessions:
            row["status"] = "accepted"
    period_continuity = tuple(
        sorted(period_rows.values(), key=lambda item: (str(item.get("period_end", "")), str(item.get("accession_number", ""))), reverse=True)
    )
    rejected_periods = tuple(
        {
            key: value for key, value in row.items()
            if key != "accession_number"
        }
        for row in period_continuity
        if row["status"] == "rejected"
    )
    annual_rows = [
        row for row in period_continuity
        if str(row.get("fiscal_period", "")).upper() in {"", "FY", "CY", "ANNUAL"}
    ]
    annual_years = sorted({
        int(str(row["period_end"])[:4])
        for row in annual_rows
        if str(row.get("period_end", ""))[:4].isdigit()
        and row.get("status") == "accepted"
    })
    rejected_years = tuple(sorted({
        int(str(row["period_end"])[:4])
        for row in annual_rows
        if str(row.get("period_end", ""))[:4].isdigit()
        and row.get("status") in {"rejected", "no_facts"}
    }))
    all_comparator_years = sorted({
        int(fact.fiscal_year) for fact in facts
        if str(getattr(fact, "usage_status", "")).casefold() == "comparator"
    })
    interim_periods = sorted({
        f"{fact.fiscal_year} {fact.fiscal_period}"
        for fact in accepted
        if str(fact.fiscal_period).upper() not in {"", "FY", "CY", "ANNUAL"}
    })
    requested_annual = [
        filing for filing in selected_filings
        if str(filing.fiscal_period or "").upper() in {"", "FY", "CY", "ANNUAL"}
    ]
    display_limit = (
        max(1, int(requested_annual_count))
        if requested_annual_count is not None and int(requested_annual_count) > 0
        else 5
    )
    displayed_years = tuple(annual_years[-display_limit:])
    latest_display_year = displayed_years[-1] if displayed_years else None
    expected_years = (
        tuple(range(latest_display_year - display_limit + 1, latest_display_year + 1))
        if latest_display_year is not None
        else ()
    )
    annual_end_by_year = {
        int(str(row.get("period_end", ""))[:4]): str(row.get("period_end", ""))[:10]
        for row in annual_rows
        if str(row.get("period_end", ""))[:4].isdigit()
        and int(str(row.get("period_end", ""))[:4]) in displayed_years
    }
    latest_period_end = annual_end_by_year.get(latest_display_year) if latest_display_year is not None else None
    if latest_period_end and expected_years:
        requested_range = (
            f"{expected_years[0]}{latest_period_end[4:10]}",
            latest_period_end,
        )
    else:
        requested_ends = sorted(annual_end_by_year.values())
        requested_range = (requested_ends[0], requested_ends[-1]) if requested_ends else None
    hidden_comparator_years = sorted(
        year for year in all_comparator_years if year not in displayed_years
    )
    research_dates = [
        str(fact.filed_at) for fact in accepted if str(fact.filed_at)
    ] + [str(filing.filed_at) for filing in selected_filings if str(filing.filed_at)]
    rejected_status_by_year = {
        int(str(row.get("period_end", ""))[:4]): str(row.get("status", ""))
        for row in annual_rows
        if str(row.get("period_end", ""))[:4].isdigit()
    }
    missing_or_rejected = tuple(
        {
            "year": year,
            "status": (
                "rejected" if rejected_status_by_year.get(year) == "rejected" else "missing"
            ),
            "reason_code": (
                "OFFICIAL_ANNUAL_REJECTED"
                if rejected_status_by_year.get(year) == "rejected"
                else "OFFICIAL_ANNUAL_UNAVAILABLE"
            ),
        }
        for year in expected_years
        if year not in annual_years or rejected_status_by_year.get(year) == "rejected"
    )
    period_coverage = {
        "requested_annual_count": (
            int(requested_annual_count)
            if requested_annual_count is not None
            else len(requested_annual) if selected_filings else None
        ),
        "requested_annual_range": requested_range,
        "available_annual_years": tuple(annual_years),
        "displayed_annual_years": displayed_years,
        "hidden_comparator_years": tuple(hidden_comparator_years),
        "latest_official_fy": annual_years[-1] if annual_years else None,
        "interim_periods": tuple(interim_periods),
        "missing_or_rejected_years": missing_or_rejected,
        "missing_reason_code": (
            "OFFICIAL_ANNUAL_REJECTED"
            if any(item["reason_code"] == "OFFICIAL_ANNUAL_REJECTED" for item in missing_or_rejected)
            else "OFFICIAL_ANNUAL_UNAVAILABLE" if missing_or_rejected else None
        ),
        "research_as_of": research_as_of or (max(research_dates) if research_dates else None),
    }
    statuses = {group.validation.status for group in groups}
    has_missing_periods = any(item["status"] == "no_facts" for item in period_continuity)
    status = (
        ValidationStatus.REJECTED
        if not accepted
        else ValidationStatus.READY_WITH_WARNINGS
        if ValidationStatus.REJECTED in statuses or ValidationStatus.READY_WITH_WARNINGS in statuses or has_missing_periods
        else ValidationStatus.VERIFIED
    )
    return FinancialProfile(
        accepted,
        fact_dicts,
        tuple(calculate_metrics(list(fact_dicts))),
        tuple(calculate_interim_metrics(list(fact_dicts))),
        groups,
        rejected_periods,
        str(reporting_currency or (accepted[0].currency if accepted else "")),
        status,
        period_continuity,
        period_coverage,
    )


_LABELS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业总收入", "营业收入", "营业收入合计", "revenue", "revenues", "total revenue", "operating revenue"),
    "net_income": (
        "归属于母公司股东的净利润", "归属于母公司所有者的净利润",
        "归属于上市公司股东的净利润", "net income attributable to owners",
        "equity holders of the company", "net profit attributable to shareholders of the parent company",
        "net income attributable to alibaba group holding limited",
        "profit attributable to owners", "profit attributable to equity holders",
        "profit attributable to shareholders", "profit for the year attributable to owners",
        "owners of the company", "profit for the year", "profit after tax", "net profit", "净利润",
    ),
    "operating_cash_flow": (
        "经营活动产生的现金流量净额", "经营活动所得现金净额",
        "经营活动产生的现金流",
        "net cash generated from operating activities", "net cash flows from operating activities",
        "net cash flows generated from operating activities",
        "net cash flows (used in)/generated from operating activities",
        "net cash flows generated from/(used in) operating activities",
        "net cash flows generated from/used in operating activities",
        "net cash flow from operating activities",
        "net cash provided by operating activities",
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
    ),
    "total_equity": ("所有者权益合计", "所有者权益（或股东权益）合计", "所有者权益（或股东权", "股东权益合计", "total equity", "total shareholders' equity"),
    "reported_roe": ("加权平均净资产收益率", "weighted average return on equity"),
    "profit_before_tax": ("利润总额", "profit before tax"),
    "profit_after_tax": ("净利润", "profit after tax", "net profit"),
    "operating_income": ("Operating income", "Operating loss", "operating profit", "operating income/(loss)"),
    "capital_expenditure": (
        "purchases and prepayments of property, plant and equipment and intangible assets",
        "purchases of property, plant and equipment and intangible assets",
        "capital expenditure", "capital expenditures",
    ),
    "gross_profit": ("Gross profit", "gross profit/(loss)"),
}
_STATEMENT_FOR = {
    "revenue": "income_statement", "net_income": "income_statement",
    "operating_cash_flow": "cash_flow", "assets": "balance_sheet",
    "liabilities": "balance_sheet", "equity": "balance_sheet",
    "total_equity": "balance_sheet", "reported_roe": "summary",
    "profit_before_tax": "income_statement", "profit_after_tax": "income_statement",
    "operating_income": "income_statement", "gross_profit": "income_statement",
    "capital_expenditure": "cash_flow",
}
_CORE = {"revenue", "net_income", "assets", "liabilities", "equity", "operating_cash_flow"}
_COVERAGE_WARNING_ISSUES = frozenset({
    "income_statement_core_missing",
    "cash_flow_core_missing",
    "balance_sheet_core_missing",
    "core_coverage_insufficient",
})
_STATEMENT_MARKERS = {
    "balance_sheet": ("合并资产负债表", "资产负债表", "consolidated balance sheet", "consolidated balance sheets", "consolidated statement of financial position", "statement of financial position"),
    "income_statement": ("合并利润表", "利润表", "consolidated income statement", "consolidated income statements", "statement of profit or loss"),
    "cash_flow": ("合并现金流量表", "现金流量表", "consolidated cash flow statement", "consolidated statement of cash flows", "consolidated statements of cash flows", "statement of cash flows"),
}
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
) -> FilingManifest:
    """Resolve a provisional metadata date from bounded statement page text.

    Discovery is allowed to retain an annual-looking placeholder (for example
    an H1 filing carrying ``12/31``).  The parser must not turn that placeholder
    into a fact, but it can safely resolve it from dates observed on the
    candidate pages.  Dates remain constrained by ``filed_at`` in the shared
    resolver; absent a source-supported date the manifest stays provisional.
    """
    if manifest.period_end:
        return manifest
    observed: list[str] = []
    for text in page_texts:
        observed.extend(_date_tokens(text or ""))
    if not observed:
        return manifest
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
    resolved_revision = (
        "period_end_verified"
        if manifest.revision == "period_end_provisional"
        else manifest.revision
    )
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
    if any(token in normalized for token in ("万元", "万人民币", "rmbten-thousand")):
        return 10_000.0, currency or "CNY"
    if any(token in normalized for token in ("百万元", "rmbmillion", "inmillions", "million")):
        return 1_000_000.0, currency
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
    positions: list[tuple[int, str, str]] = []
    markers_by_statement = _statement_markers_for_rules(rules)
    raw_lines = text.splitlines()
    lines: list[tuple[int, str]] = []
    offset = 0
    for raw_line in raw_lines:
        lines.append((offset, raw_line.strip()))
        offset += len(raw_line) + 1

    def add_marker_candidates(line_number: int, line_offset: int, clean: str) -> None:
        """Add only title-like marker hits, not narrative note mentions."""

        line_folded = clean.casefold()
        for statement, markers in markers_by_statement.items():
            for marker in markers:
                marker_folded = marker.casefold()
                if marker_folded not in line_folded:
                    continue
                # A formal title may have a page number/report label before it,
                # but prose such as ``charge to the consolidated income
                # statement`` must not establish a statement context.
                prefix = line_folded.split(marker_folded, 1)[0].strip()
                if prefix and not re.fullmatch(
                    r"(?:\d{1,4}\s*(?:[、.)]|\u3001)|[（(][一二三四五六七八九十0-9ivx]+[）)])?"
                    r"(?:20\d{2}\s+annual\s+report)?", prefix
                ) and not any(
                    marker in prefix
                    for marker in ("母公司", "parent", "company only", "company-only", "separate")
                ):
                    continue
                if len(clean) > 48 or "不是" in clean or "说明" in clean:
                    continue
                pos = line_offset + line_folded.find(marker_folded)
                suffix = line_folded.split(marker_folded, 1)[1].strip()
                scope_aliases = (
                    "合并", "consolidated", "母公司", "parent", "company only",
                    "company-only", "separate",
                    *_rules_aliases(rules, "scope_aliases", "consolidated"),
                    *_rules_aliases(rules, "scope_aliases", "parent"),
                )
                has_scope_suffix = any(
                    str(alias).casefold() in suffix for alias in scope_aliases
                )
                if suffix and not (
                    re.fullmatch(
                        r"(?:[\s:：,，.。()（）\[\]{}\-–—]|continued|continuation|cont[.'’_-]*d)*",
                        suffix,
                    )
                    or re.match(
                        r"(?:单位|编制单位|unit|project|项目|for\s+the\s+year|as\s+of|year\s+ended)",
                        suffix,
                    )
                    or has_scope_suffix
                ):
                    continue
                scope = _scope_from_text(clean, rules)
                positions.append((pos, statement, scope))

    for line_number, (line_offset, clean) in enumerate(lines):
        add_marker_candidates(line_number, line_offset, clean)
        # PDF text extraction frequently wraps a statement title at the word
        # boundary (e.g. ``CONSOLIDATED INCOME`` / ``STATEMENT``). Join only
        # short adjacent lines and require the marker to start the first line;
        # this keeps narrative references in notes out of the index.
        if line_number + 1 < len(lines):
            next_offset, next_clean = lines[line_number + 1]
            if clean and next_clean and len(clean) <= 36 and len(next_clean) <= 36:
                joined = f"{clean} {next_clean}"
                joined_folded = joined.casefold()
                for statement, markers in markers_by_statement.items():
                    for marker in markers:
                        marker_folded = marker.casefold()
                        if marker_folded not in joined_folded:
                            continue
                        first_word = marker_folded.split(None, 1)[0]
                        if not joined_folded.startswith(first_word):
                            continue
                        # Reuse the strict prefix check for the joined title;
                        # unlike a body sentence, its marker begins the line.
                        add_marker_candidates(line_number, line_offset, joined)
                        break
                    else:
                        continue
                    break
    if not positions:
        return None
    _, statement, scope = max(positions)
    return statement, scope


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


_ENGLISH_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


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
    if not dates:
        return None
    year, month, day = max(dates)
    return year - 1 if len(dates) == 1 and month == 1 and day == 1 else year


def _period_columns(
    rows: Sequence[PdfRowAST], preferred_currency: str = "", rules: FinancialRulesSnapshot | None = None
) -> tuple[_PeriodColumn, ...]:
    """Choose visual year columns before any one-year title/date row."""
    dual: list[tuple[int, float]] | None = None
    fallback: list[tuple[int, float]] | None = None
    headers_by_year: dict[int, str] = {}
    all_text = " ".join(row.text for row in rows)
    global_scale, _ = _unit_scale(all_text, rules)
    unit_cells: list[tuple[float, str]] = []
    for row_index, row in enumerate(rows):
        if _is_narrative_date_header(row.text):
            continue
        for cell in row.cells:
            normalized = re.sub(r"\s+", "", cell.text.casefold()).translate(str.maketrans({"’": "'", "‘": "'", "＇": "'"}))
            if normalized in {"rmb", "cny", "rmb'000", "rmbmillion", "us$", "usd", "hkd", "hk$"}:
                _, currency = _unit_scale(cell.text, rules)
                if currency:
                    unit_cells.append(((cell.x0 + cell.x1) / 2, currency))
        by_year: dict[int, float] = {}
        for cell in row.cells:
            effective_year = _effective_period_year(cell.text)
            if effective_year is not None:
                by_year.setdefault(effective_year, (cell.x0 + cell.x1) / 2)
                headers_by_year.setdefault(effective_year, cell.text.strip())
                continue
            match = re.search(r"20\d{2}", cell.text)
            if match:
                year = int(match.group())
                by_year.setdefault(year, (cell.x0 + cell.x1) / 2)
                headers_by_year.setdefault(year, cell.text.strip())
        candidates = sorted(by_year.items(), key=lambda item: item[1])
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
    header_text = re.sub(
        r"\s+", "", " ".join(row.text for row in rows[:12]).casefold()
    )
    split_semantic_header = (
        ("currentperiod" in header_text and "previousperiod" in header_text)
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
        value_center = center
        in_interval = [item for item in unit_cells if left <= item[0] <= right]
        preferred = [item for item in in_interval if item[1] == preferred_currency]
        candidates_for_currency = preferred or in_interval or unit_cells
        if candidates_for_currency:
            unit_center, currency = min(
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
                headers_by_year.get(year, ""),
            )
        )
    return tuple(result)


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


def _row_title_context(row: PdfRowAST, rules: FinancialRulesSnapshot | None = None) -> tuple[str, str] | None:
    """Identify a formal statement title from positioned row text."""
    text = row.text.strip()
    folded = text.casefold()
    for statement, markers in _statement_markers_for_rules(rules).items():
        for marker in markers:
            marker_folded = marker.casefold()
            if marker_folded not in folded:
                continue
            if len(text) > 64 or "不是" in text or "说明" in text:
                continue
            scope = _scope_from_text(text, rules)
            return statement, scope
    return None


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
    """Find an unlabeled attributable-equity subtotal after its components."""
    totals: dict[tuple[float, float, float, float], PdfRowAST] = {}
    heading = "equityattributabletoequityholdersofthecompany"
    for index, row in enumerate(rows):
        label = re.sub(r"\s+", "", _row_label_text(row)).casefold()
        if label != heading or any(_parse_number(cell.text) is not None for cell in row.cells):
            continue
        detail_values: list[float] = []
        for candidate in rows[index + 1 : index + 12]:
            candidate_label = _row_label_text(candidate)
            numeric = [cell for cell in candidate.cells if _parse_number(cell.text) is not None]
            if not numeric:
                continue
            if not candidate_label:
                selected = _select_period_cell(candidate, 0.0, columns, target_year)
                total = _parse_number(selected.text) if selected is not None else None
                if total is not None and len(detail_values) >= 2 and abs(sum(detail_values) - total) <= max(1.0, abs(total) * 0.01):
                    totals[candidate.bbox] = candidate
                break
            selected = _select_period_cell(candidate, min((cell.x1 for cell in candidate.cells if _parse_number(cell.text) is None), default=0.0), columns, target_year)
            value = _parse_number(selected.text) if selected is not None else None
            if value is not None:
                detail_values.append(value)
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
) -> tuple[PdfPageSection, ...]:
    """Pure page-context state transition used by the PDF adapter.

    Summary pages never inherit.  Formal titles create a new context; pages
    without a title may inherit only two adjacent pages and only when their
    coordinates still look like the same table.  Multiple titles on one page
    become separate sections, which prevents a parent table header from
    reclassifying the consolidated rows above it.
    """
    rows_tuple = tuple(rows)
    scale, currency, explicit = _explicit_unit_info(page_text, rules)
    if _is_summary_page(page_text, rows_tuple, rules=rules):
        return (PdfPageSection(
            PdfTableContext("summary", "consolidated", scale, currency or default_currency,
                            explicit, _period_columns(rows_tuple, default_currency, rules), page_number, 0,
                            "explicit" if explicit else "unknown"),
            rows_tuple, True, False,
        ),)

    titles = [(index, _row_title_context(row, rules)) for index, row in enumerate(rows_tuple)]
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
                                    previous.unit_provenance),
                    prefix, False, True,
                ))
        for position, (title_index, (statement, scope)) in enumerate(titles):
            end = titles[position + 1][0] if position + 1 < len(titles) else len(rows_tuple)
            section_rows = rows_tuple[title_index:end]
            section_scale = scale if explicit else 1.0
            section_currency = currency or default_currency
            periods = _period_columns(section_rows, default_currency, rules)
            title_text = section_rows[0].text.casefold() if section_rows else ""
            continuation_title = bool(re.search(
                r"续|continued|continuation|cont[\s.'’_-]*d", title_text
            ))
            can_inherit_unit = bool(
                previous
                and previous.statement == statement
                and previous.scope == scope
                and previous.last_page + 1 == page_number
                and not explicit
                and previous.unit_explicit
                and continuation_title
            )
            if can_inherit_unit:
                section_scale = previous.multiplier
                section_currency = previous.currency or default_currency
            section_unit_provenance = (
                "inherited" if can_inherit_unit else "explicit" if explicit else "unknown"
            )
            if not periods and previous and previous.statement == statement and previous.scope == scope:
                periods = previous.periods
            sections.append(PdfPageSection(
                PdfTableContext(statement, scope, section_scale, section_currency,
                                explicit or can_inherit_unit, periods, page_number,
                                previous.inherited_pages + 1 if can_inherit_unit else 0,
                                section_unit_provenance),
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
        inherited_periods = previous.periods or _period_columns(rows_tuple, default_currency, rules)
        inherited_unit_provenance = "explicit" if explicit else "inherited"
        return (PdfPageSection(
            PdfTableContext(previous.statement, previous.scope, inherited_scale, inherited_currency,
                            previous.unit_explicit or explicit, inherited_periods, page_number,
                            previous.inherited_pages + 1, inherited_unit_provenance),
            rows_tuple, False, True,
        ),)
    return ()


def _row_label_text(row: PdfRowAST) -> str:
    """Return label fragments while excluding period/value cells.

    Some Chinese annual reports wrap a single label over two or three visual
    lines and place the period values between those fragments.  Coordinates,
    rather than cell order, identify the label column: non-numeric cells to
    the left of the first numeric cell are retained, including a wrapped tail
    that appears after the values in reading order.
    """
    cells = tuple(sorted(row.cells, key=lambda cell: (cell.top, cell.x0)))
    numeric = [cell for cell in cells if _parse_number(cell.text) is not None]
    first_value_x = min((cell.x0 for cell in numeric), default=None)
    fragments = [
        cell.text
        for cell in cells
        if _parse_number(cell.text) is None
        and (first_value_x is None or cell.x1 <= first_value_x + 4)
    ]
    return re.sub(r"\s+", "", "".join(fragments))


def _known_label(text: str, rules: FinancialRulesSnapshot | None = None) -> bool:
    compact = _label_compact(text)
    return any(_label_compact(label) in compact for labels in _labels_for_rules(rules).values() for label in labels)


def _label_compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold().translate(str.maketrans({"’": "'", "‘": "'", "＇": "'"}))


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


def _net_income_candidate_priority(fact: FinancialFact) -> int:
    """Prefer attributable profit over a generic IFRS profit row."""
    compact = _label_compact(fact.reported_concept or fact.raw_text)
    if any(token in compact for token in (
        "归属于", "attributableto", "equityholders", "parentcompany", "ownersofthecompany",
    )):
        return 2
    if compact in {_label_compact(label) for label in _GENERIC_NET_INCOME_LABELS}:
        return 1
    return 0


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
        if any(_parse_number(cell.text) is not None for cell in row.cells):
            continue
        row_labels = [cell for cell in row.cells if _parse_number(cell.text) is None]
        if anchor is not None and row_labels and min(abs(cell.x0 - anchor) for cell in row_labels) > 24:
            continue
        return label
    return ""


def _merge_visual_rows(rows: Sequence[PdfRowAST], start: int, rules: FinancialRulesSnapshot | None = None) -> PdfRowAST:
    """Merge at most three tightly-spaced visual rows from one table row.

    The merge is deliberately conservative.  Rows must overlap or be within
    one normal line height, share the left label x-coordinate, and either
    contain no text (a values-only line) or not introduce a new complete
    financial label.  This prevents adjacent independent metrics from being
    swallowed while repairing labels such as ``加权平均净资产收益`` + values +
    ``率``.
    """
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
        aligned = any(abs(cell.x0 - anchor) <= 14 for cell in candidate_labels)
        if candidate_labels and not aligned:
            break
        current_label = _row_label_text(
            PdfRowAST(
                tuple(cell for row in variants for cell in row.cells),
                variants[0].top,
                variants[0].bbox,
            )
        )
        candidate_label = _row_label_text(candidate)
        combined_label = current_label + candidate_label
        current_has_values = any(_parse_number(cell.text) is not None for cell in variants[-1].cells)
        candidate_has_values = any(_parse_number(cell.text) is not None for cell in candidate.cells)
        # A heading such as ``Revenues`` introduces a detail block; do not
        # swallow the first detail row into the heading.  Likewise, two
        # adjacent numeric rows are independent line items (the previous
        # implementation merged all English detail rows into one candidate).
        if candidate_has_values and current_label.casefold() in {"revenue", "revenues", "totalrevenue"}:
            break
        if current_has_values and candidate_has_values and candidate_label:
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


def _vision_failed_pages(
    filing: FilingDocument, config: VisionFallbackConfig,
    validation_issues: Sequence[str] = (),
    existing_facts: Sequence[FinancialFact] = (),
) -> tuple[VisionPageRequest, ...]:
    """Create bounded one-page PDFs for failed statement contexts in memory."""
    if not filing.local_path or not Path(filing.local_path).is_file():
        return ()
    # Prefer formal consolidated statement titles and only two adjacent
    # continuation pages. Classification is title-line based so an unrelated
    # page-wide mention of ``consolidated`` cannot admit a parent-only table.
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(filing.local_path)
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
        selected: list[VisionPageRequest] = []
        total = 0
        for index in sorted(chosen):
            writer = PdfWriter()
            writer.add_page(reader.pages[index])
            buffer = BytesIO()
            writer.write(buffer)
            payload = buffer.getvalue()
            if total + len(payload) > config.max_bytes or len(selected) >= min(config.max_pages, VISION_MAX_PAGES):
                break
            selected.append(VisionPageRequest(index + 1, payload, filing.source_url, filing.primary_document))
            total += len(payload)
        return tuple(selected)
    except Exception:
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


def _parse_local_pdfs_bounded(
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
) -> dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]]:
    """Parse local PDFs as bounded per-document pipelines.

    Indexing and AST parsing for a document are submitted as one streaming
    pipeline: when one index completes, only that document's AST task is
    queued. This avoids a global pre-index barrier while keeping all progress
    callbacks in the collecting thread. Results are always remapped to filing
    order before returning.
    """
    parse_fn = parse or engine._parse_pdf_ast
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
        manifest = manifests.get(filing.document_id)
        if manifest is None or not filing.local_path or not Path(filing.local_path).is_file():
            continue
        if default_parser or cache_enabled:
            try:
                actual_hash = engine._file_sha256(filing.local_path)
            except OSError:
                continue
            key = f"hash:{actual_hash.casefold()}"
            actual_hashes[key] = actual_hash
            if filing.content_hash and filing.content_hash.casefold() != actual_hash.casefold():
                hash_mismatches[key] = "CACHE_HASH_MISMATCH"
        else:
            if filing.content_hash:
                key = f"hash:{filing.content_hash.casefold()}"
            else:
                try:
                    key = f"path:{str(Path(filing.local_path).resolve(strict=False)).casefold()}"
                except OSError:
                    key = f"path:{str(Path(filing.local_path)).casefold()}"
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
            if default_parser and cache_key and key not in hash_mismatches:
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
            flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds
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
            for item in uncached:
                key, filing, manifest = item
                try:
                    facts, refs, window_diagnostics = engine.parse_local_pdf_resumable(
                        company, filing, manifest,
                        cancel_check=cancel_check,
                        progress=(
                            lambda current, total, status, filing=filing:
                            _emit_ingestion_progress(
                                progress, "filing-window", current, total, filing,
                                status=status,
                            )
                        ) if progress is not None else None,
                    )
                    error = window_diagnostics[0] if window_diagnostics else None
                except Exception as exc:
                    facts, refs, error = [], [], f"pdf_window_failed:{type(exc).__name__}"
                results[key] = (facts, refs, error or pre_errors.get(key))
                if error is None and facts and refs:
                    cache_key = cache_keys.get(key)
                    if cache_key:
                        engine._store_parse_cache(cache_key, facts, refs)
                _emit_ingestion_progress(
                    progress, "filing-parse", completed + 1, total, filing,
                    status="failed" if error else "parsed", error_code=error or "",
                )
            _resolve_pdf_parse_flights(
                flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds
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
                batch_timeout_seconds=batch_timeout_seconds,
                worker_entry=worker_entry,
            )
        finally:
            # Every owner wakes waiters, including cancellation and process
            # startup failures. Never leave an in-flight entry behind.
            _resolve_pdf_parse_flights(
                flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds
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
        if next_item >= len(uncached):
            return
        item = uncached[next_item]
        next_item += 1
        futures[executor.submit(_index_one, item)] = ("index", item)

    try:
        if default_parser:
            for _ in range(min(workers, len(uncached))):
                submit_index()
        else:
            for item in uncached:
                futures[executor.submit(parse_one, item)] = ("parse", item)
        while futures:
            done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            if cancel_check is not None and cancel_check():
                cancelled = True
                # Do not disturb results that have already completed; cancel
                # only work that has not started and let running workers drain.
                for pending in tuple(futures):
                    if not pending.done():
                        pending.cancel()
            for future in done:
                kind, item = futures.pop(future)
                if future.cancelled():
                    continue
                try:
                    if kind == "index":
                        key, candidate_pages, index_error = future.result()
                        indexed += 1
                        if progress is not None:
                            progress("filing-index", indexed, total)
                        if index_error is not None:
                            results[key] = ([], [], index_error)
                            parsed += 1
                            if progress is not None:
                                progress("filing-parse", parsed, total)
                        elif cancel_check is None or not cancel_check():
                            futures[executor.submit(parse_one, item, candidate_pages)] = ("parse", item)
                        if not cancelled:
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
                        if progress is not None:
                            progress("filing-parse", parsed, total)
                except CancelledError:
                    continue
    finally:
        if cancel_check is not None and cancel_check():
            cancelled = True
            for pending in tuple(futures):
                if not pending.done():
                    pending.cancel()
        # Waiting allows already-running native parsers to release resources;
        # cancel_futures prevents queued filings from starting after cancel.
        executor.shutdown(wait=True, cancel_futures=cancelled)

    by_document: dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]] = {}
    _resolve_pdf_parse_flights(
        flight_owners, flight_waiters, cache_keys, results, parse_timeout_seconds
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
) -> None:
    """Process entrypoint which returns one bounded, pickle-safe result."""

    try:
        index_error = None
        index_diagnostic = None
        try:
            indexed_pages, index_diagnostic = _candidate_financial_pages_with_diagnostic(
                filing.local_path, rules=rules
            )
        except Exception as exc:
            indexed_pages = None
            index_error = f"pdf_index_failed:{type(exc).__name__}"
        result_queue.put(("filing-index", key, indexed_pages, index_error, index_diagnostic))
        # A partial index is deliberately represented by None; the AST parser
        # then fails open to its full-document path for correctness.
        result_queue.put(("filing-result", _parse_pdf_process_worker(
            key, company, filing, manifest, indexed_pages, rules
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
    for key, flight in waiters.items():
        flight.event.wait(timeout=max(0.1, float(timeout_seconds)) + 1.0)
        if flight.result is not None:
            results[key] = flight.result


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
        nonlocal next_item
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
            result_queue = context.Queue(maxsize=2)
            worker_args = (key, company, filing, manifest, None, result_queue)
            if worker_entry is None:
                worker_args = (*worker_args, engine._compatibility_rules)
            process = context.Process(
                target=entrypoint,
                args=worker_args,
                name=f"financial-pdf-{key[-12:]}",
            )
            process.daemon = True
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
            status="failed" if error else "parsed",
            error_code=error or "",
            elapsed_seconds=time.monotonic() - state["started_at"],
        )

    try:
        start_next()
        while processes:
            if cancel_check is not None and cancel_check():
                cancelled = True
                for key, state in tuple(processes.items()):
                    _terminate_pdf_process(state["process"])
                    _close_pdf_result_queue(state["queue"])
                    processes.pop(key, None)
                    results[key] = ([], [], "pdf_parse_cancelled")
                    filing = state["item"][1]
                    _emit_ingestion_progress(
                        progress,
                        "filing-parse",
                        parsed,
                        total,
                        filing,
                        status="cancelled",
                        error_code="pdf_parse_cancelled",
                        elapsed_seconds=time.monotonic() - state["started_at"],
                    )
                break
            progressed = False
            now = time.monotonic()
            if now >= batch_deadline:
                for key, state in tuple(processes.items()):
                    _terminate_pdf_process(state["process"])
                    _close_pdf_result_queue(state["queue"])
                    processes.pop(key, None)
                    results[key] = ([], [], "pdf_batch_timeout")
                    parsed += 1
                    filing = state["item"][1]
                    _emit_ingestion_progress(
                        progress,
                        "filing-parse",
                        parsed,
                        total,
                        filing,
                        status="blocked",
                        error_code="pdf_batch_timeout",
                        elapsed_seconds=time.monotonic() - state["started_at"],
                    )
                break
            for key, state in tuple(processes.items()):
                try:
                    message = state["queue"].get_nowait()
                except queue.Empty:
                    message = None
                if message is not None:
                    progressed = True
                    kind = message[0]
                    if kind == "filing-index":
                        if not state["indexed"]:
                            state["indexed"] = True
                            if len(message) > 4 and message[4]:
                                state["index_diagnostic"] = str(message[4])
                            indexed += 1
                            filing = state["item"][1]
                            _emit_ingestion_progress(
                                progress,
                                "filing-index",
                                indexed,
                                total,
                                filing,
                                status="local-parsing",
                                elapsed_seconds=time.monotonic() - state["started_at"],
                            )
                    elif kind == "filing-result":
                        finish_process(key, message[1])
                        start_next()
                elif not state["process"].is_alive():
                    # ``multiprocessing.Queue`` uses a feeder thread. A child
                    # can therefore report not-alive before its final put is
                    # visible to the parent. Give that bounded handoff a short
                    # drain window before converting a normal result into an
                    # exit failure.
                    final_result = None
                    drain_deadline = time.monotonic() + exit_drain_seconds
                    while time.monotonic() < drain_deadline:
                        try:
                            pending_message = state["queue"].get(
                                timeout=max(0.001, drain_deadline - time.monotonic())
                            )
                        except queue.Empty:
                            continue
                        if pending_message and pending_message[0] == "filing-index":
                            if not state["indexed"]:
                                state["indexed"] = True
                                indexed += 1
                                filing = state["item"][1]
                                _emit_ingestion_progress(
                                    progress,
                                    "filing-index",
                                    indexed,
                                    total,
                                    filing,
                                    status="local-parsing",
                                    elapsed_seconds=time.monotonic() - state["started_at"],
                                )
                        elif pending_message and pending_message[0] == "filing-result":
                            final_result = pending_message[1]
                            break
                    finish_process(
                        key,
                        final_result or (key, [], [], "pdf_worker_exit_failed"),
                    )
                    start_next()
                elif now >= state["deadline"]:
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
                        status="blocked",
                        error_code="pdf_parse_timeout",
                        elapsed_seconds=time.monotonic() - state["started_at"],
                    )
                    start_next()
            if not progressed:
                time.sleep(0.01)
        if not cancelled and time.monotonic() >= batch_deadline:
            for key, filing, _manifest in uncached[next_item:]:
                results[key] = ([], [], "pdf_batch_timeout")
                parsed += 1
                _emit_ingestion_progress(
                    progress,
                    "filing-parse",
                    parsed,
                    total,
                    filing,
                    status="blocked",
                    error_code="pdf_batch_timeout",
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
    for page_number, text in enumerate(page_texts, 1):
        compact = re.sub(r"\s+", "", text).casefold()
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
    # A partial index is unsafe: missing one statement can make the coordinate
    # parser report an apparently valid but incomplete filing. Returning None
    # deliberately fails open to the full-document parser.
    statement_kinds.update(mapped_statements)
    if (not starts and not mapped_statements) or statement_kinds != {"income_statement", "balance_sheet", "cash_flow"}:
        return None
    selected: set[int] = set()
    page_count = len(page_texts)
    for start in starts:
        selected.update(
            range(start, min(page_count, start + max(0, continuation_pages)) + 1)
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
    if return_diagnostics:
        return indexed, _scanned_image_diagnostic(page_texts, rules=rules)
    return indexed


def _candidate_financial_pages_pypdf(
    path: str, *, continuation_pages: int, rules: FinancialRulesSnapshot | None = None,
    return_diagnostics: bool = False,
) -> frozenset[int] | tuple[frozenset[int] | None, str | None] | None:
    """Compatibility indexer used when PDFium is unavailable or incomplete."""

    from pypdf import PdfReader

    reader = PdfReader(path)
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    indexed = _candidate_pages_from_text(page_texts, continuation_pages=continuation_pages, rules=rules)
    if return_diagnostics:
        return indexed, _scanned_image_diagnostic(page_texts, rules=rules)
    return indexed


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
) -> tuple[frozenset[int] | None, str | None]:
    """Return the normal index plus a non-authorizing scan diagnostic."""
    try:
        indexed = _candidate_financial_pages_pypdfium(
            path, continuation_pages=continuation_pages, rules=rules,
            return_diagnostics=True,
        )
        if isinstance(indexed, tuple):
            if indexed[0] is not None:
                return indexed
            # An incomplete PDFium text layer may still carry the useful scan
            # classification; pypdf gets one compatibility attempt below.
            pdfium_diagnostic = indexed[1]
        else:
            pdfium_diagnostic = None
    except Exception:
        pdfium_diagnostic = None
    try:
        indexed = _candidate_financial_pages_pypdf(
            path, continuation_pages=continuation_pages, rules=rules,
            return_diagnostics=True,
        )
        if isinstance(indexed, tuple):
            return indexed[0], indexed[1] or pdfium_diagnostic
        return indexed, pdfium_diagnostic
    except Exception:
        return None, pdfium_diagnostic


def _candidate_financial_pages(path: str, *, continuation_pages: int = 3, rules: FinancialRulesSnapshot | None = None) -> frozenset[int] | None:
    """Find formal statement pages with a low-memory text prepass.

    PDFium supplies the fast text layer for the normal path. If PDFium cannot
    be imported/read safely, or produces an incomplete three-statement index,
    pypdf remains the compatibility fallback. Both paths retain the bounded
    continuation window and standalone ROE summary-page semantics.
    """

    return _candidate_financial_pages_with_diagnostic(
        path, continuation_pages=continuation_pages, rules=rules
    )[0]


def _pdf_page_count(path: str) -> int | None:
    """Read only the document page count without extracting page text."""

    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(path)
        try:
            return len(document)
        finally:
            close = getattr(document, "close", None)
            if callable(close):
                close()
    except Exception:
        try:
            from pypdf import PdfReader

            return len(PdfReader(path, strict=False).pages)
        except Exception:
            return None


@dataclass(frozen=True, slots=True)
class FinancialCandidateCollection:
    """Untrusted candidate batches collected before canonical resolution."""

    manifests: tuple[FilingManifest, ...]
    batches_by_document: dict[str, tuple[Any, ...]]
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[str, ...] = ()


def _pdf_evidence_id_for_fact(fact_id: str) -> str | None:
    """Return the explicit AST fact/evidence association, if one exists.

    The AST intentionally prefixes fact identifiers with ``ingest:`` while
    evidence identifiers use ``fact:``.  Keeping this relation in one named
    seam avoids the old positional zip and avoids treating arbitrary strings
    as evidence identifiers.
    """
    if fact_id.startswith("ingest:") and len(fact_id) > len("ingest:"):
        return "fact:" + fact_id[len("ingest:"):]
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
            paired.append((fact, matches[0]))
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
    def _file_sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
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
        material = "|".join(
            (
                actual_hash,
                _PDF_PARSER_VERSION,
                _PDF_TAXONOMY_VERSION,
                _PDF_CACHE_POLICY_VERSION,
                self._compatibility_rules.semantic_fingerprint(),
                profile,
                policy,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

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
            if payload.get("parser_version") != _PDF_PARSER_VERSION:
                return None
            facts = [FinancialFact(**item) for item in payload["facts"]]
            refs = [EvidenceRef(**item) for item in payload["evidence"]]
            if any(item.parser_version != _PDF_PARSER_VERSION for item in facts):
                return None
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
        # Keep the parser revision in the fact payload and in the cache
        # envelope sourced from one constant.  Injected test/legacy parsers
        # may omit it; normalizing here prevents those facts from becoming a
        # semantically unlabelled cache hit.
        safe_facts = [replace(item, parser_version=_PDF_PARSER_VERSION) for item in facts]
        safe_refs = [replace(item) for item in refs]
        self._parse_cache_memory[key] = (safe_facts, safe_refs)
        if self._parse_cache_dir is None:
            return
        payload = json.dumps(
            {
                "parser_version": _PDF_PARSER_VERSION,
                "facts": [item.to_dict() for item in safe_facts],
                "evidence": [item.to_dict() for item in safe_refs],
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

        if not filing.local_path or not Path(filing.local_path).is_file():
            return [], [], ("pdf_file_missing",)
        try:
            actual_hash = self._file_sha256(filing.local_path)
        except OSError:
            return [], [], ("pdf_file_unreadable",)
        page_count = _pdf_page_count(filing.local_path)
        selected_pages = candidate_pages
        if selected_pages is None:
            indexed_pages = _candidate_financial_pages(
                filing.local_path, rules=self._compatibility_rules
            )
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
        )

        checkpoints = run_checkpointed_windows(
            store,
            key,
            windows,
            worker,
            max_retries=1,
            cancel_check=cancel_check,
            progress=progress,
            timeout_seconds=self._parse_timeout_seconds,
        )
        facts: dict[str, FinancialFact] = {}
        refs: dict[str, EvidenceRef] = {}
        for checkpoint in checkpoints:
            for fact in checkpoint.facts:
                facts.setdefault(fact.fact_id, fact)
            for ref in checkpoint.evidence:
                refs.setdefault(ref.evidence_id, ref)
        diagnostics = (
            () if len(checkpoints) == len(windows) else ("pdf_window_incomplete",)
        )
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
    ) -> FinancialCandidateCollection:
        """Collect every source candidate without selecting or validating facts.

        Structured adapters are deliberately all invoked for a filing.  A
        partial feed therefore remains available for reconciliation with the
        already-parsed PDF batch instead of suppressing complementary rows.
        The PDF prepass is shared with the legacy path, so orchestration never
        opens a document a second time merely to repair a partial source.
        """

        from .financial_compiler import CandidateBatch, FactCandidate, GapStageKind

        manifests: list[FilingManifest] = []
        manifest_by_document: dict[str, FilingManifest] = {}
        diagnostics: list[str] = []
        for filing in filings:
            if cancel_check is not None and cancel_check():
                break
            manifest = _manifest_for(filing)
            if manifest is None:
                diagnostics.append(f"{filing.document_id}:ambiguous_period_identity")
                continue
            manifests.append(manifest)
            manifest_by_document[filing.document_id] = manifest
            filing.form_type = manifest.form_type
            filing.fiscal_period = manifest.fiscal_period
            filing.period_end = manifest.period_end
            filing.revision = manifest.revision
            filing.supersedes_document_id = manifest.supersedes_document_id

        parsed_pdfs = _parse_local_pdfs_bounded(
            self,
            company,
            filings,
            manifest_by_document,
            cancel_check=cancel_check,
            progress=progress,
            max_workers=self._max_workers,
            parse_timeout_seconds=self._parse_timeout_seconds,
            batch_timeout_seconds=self._batch_timeout_seconds,
        )
        batches: dict[str, tuple[Any, ...]] = {}
        all_evidence: list[EvidenceRef] = []
        for index, filing in enumerate(filings, start=1):
            if cancel_check is not None and cancel_check():
                break
            if filing.document_id not in manifest_by_document:
                continue
            structured_candidates: list[FactCandidate] = []
            structured_refs: list[EvidenceRef] = []
            for adapter in structured_sources:
                try:
                    sfacts, srefs, failure = adapter.fetch(company, filing)
                except Exception as exc:
                    diagnostics.append(f"{filing.document_id}:structured:{type(exc).__name__}")
                    continue
                if failure:
                    diagnostics.append(f"{filing.document_id}:{failure}")
                ref_by_fact = {
                    fact.fact_id: ref for fact, ref in zip(sfacts, srefs)
                }
                for fact in sfacts:
                    if fact.unit_provenance == "unknown" and fact.concept != "reported_roe":
                        fact = replace(fact, unit_provenance="structured_normalized")
                    refs = (ref_by_fact[fact.fact_id],) if fact.fact_id in ref_by_fact else ()
                    structured_candidates.append(
                        FactCandidate(fact, refs, f"structured:{type(adapter).__name__}")
                    )
                    structured_refs.extend(refs)
            structured_batch = CandidateBatch(
                filing,
                tuple(structured_candidates),
                tuple(structured_refs),
            )
            pdf_facts, pdf_refs, pdf_error = parsed_pdfs.get(
                filing.document_id, ([], [], None)
            )
            if pdf_error:
                diagnostics.append(f"{filing.document_id}:{pdf_error}")
            paired_pdf, pairing_diagnostics = _pair_pdf_facts_evidence(
                pdf_facts, pdf_refs,
            )
            diagnostics.extend(
                f"{filing.document_id}:{item}" for item in pairing_diagnostics
            )
            pdf_candidates = tuple(
                FactCandidate(
                    fact,
                    (ref,),
                    "financial-ingestion-ast",
                )
                for fact, ref in paired_pdf
            )
            pdf_batch = CandidateBatch(filing, pdf_candidates, tuple(pdf_refs))
            # A provisional discovery identity is only an expected date.  Once
            # an extractor supplies a source-supported statement date, promote
            # the canonical filing/manifest together so the compiler does not
            # reject the otherwise valid cohort merely because discovery had a
            # placeholder (for example H1 with 12/31 metadata).
            if not filing.period_end:
                observed_dates = [
                    candidate.fact.end_date
                    for candidate in (*structured_candidates, *pdf_candidates)
                    if candidate.fact.end_date
                    and (candidate.fact.fiscal_period or filing.fiscal_period).upper()
                    == (filing.fiscal_period or "FY").upper()
                ]
                observed_identity = DisclosureIdentityResolver().resolve(
                    title=filing.primary_document,
                    filed_at=filing.filed_at,
                    provider_metadata={
                        "period_end": "",
                        "fiscal_period": filing.fiscal_period,
                        "revision": "period_end_provisional",
                    },
                    observed_statement_dates=tuple(observed_dates),
                )
                if observed_identity.period_end and not observed_identity.provisional:
                    filing.period_end = observed_identity.period_end
                    filing.revision = "period_end_verified"
                    resolved_manifest = replace(
                        manifest_by_document[filing.document_id],
                        period_end=filing.period_end,
                        revision=filing.revision,
                    )
                    manifest_by_document[filing.document_id] = resolved_manifest
                    manifests = [
                        resolved_manifest if item.document_id == filing.document_id else item
                        for item in manifests
                    ]
            batches[filing.document_id] = (structured_batch, pdf_batch)
            all_evidence.extend(structured_refs)
            all_evidence.extend(pdf_refs)
            if progress is not None:
                progress("filing-candidates", index, len(filings))
        return FinancialCandidateCollection(
            tuple(manifests), batches, tuple(all_evidence), tuple(dict.fromkeys(diagnostics))
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

        from .financial_compiler import FinancialFactCompiler

        compiled = FinancialFactCompiler().compile_from_ingestion(
            company,
            filings,
            self,
            structured_sources=structured_sources,
            vision_fallback=vision_fallback,
            vision_config=vision_config,
            cancel_check=cancel_check,
            progress=progress,
            reporting_currency=company.reporting_currency,
        )
        issues = list(compiled.diagnostics)
        for item in compiled.validations:
            issues.extend(item.issues)
        # ``allow_ai`` describes the selected research cohort only.  The
        # compatibility projection still exposes audit failures from other
        # cohorts as warnings, so callers cannot mistake a partially
        # quarantined ingestion for an entirely verified dataset.
        has_rejected_group = any(
            getattr(item.validation.status, "value", item.validation.status)
            == ValidationStatus.REJECTED.value
            for item in compiled.group_validations
        )
        has_warning_group = any(
            getattr(item.validation.status, "value", item.validation.status)
            == ValidationStatus.READY_WITH_WARNINGS.value
            for item in compiled.group_validations
        )
        status = (
            ValidationStatus.REJECTED
            if not compiled.resolved_facts
            else ValidationStatus.READY_WITH_WARNINGS
            if has_rejected_group or has_warning_group or not compiled.allow_ai
            else ValidationStatus.VERIFIED
        )
        validation = FinancialValidation(
            status,
            tuple(dict.fromkeys(issues)),
            frozenset(fact.concept for fact in compiled.resolved_facts),
            tuple(compiled.resolved_facts),
            tuple(compiled.quarantined_facts),
        )
        groups = list(compiled.group_validations)
        for group in groups:
            for fact in group.validation.accepted:
                fact.validation_status = group.validation.status.value
            for fact in group.validation.quarantined:
                fact.validation_status = ValidationStatus.REJECTED.value
        return FinancialDataset(
            tuple(compiled.resolved_facts),
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
    ) -> FinancialGroupValidation:
        """Public quality-gate seam used by canonical adapters/compiler."""
        return self._validate_group(facts, identity, evidence_map, required_concepts)

    def _validate_group(
        self,
        facts: list[FinancialFact],
        identity: tuple[str, str, str, str, str],
        evidence_map: dict[str, EvidenceRef] | None = None,
        required_concepts: set[str] | frozenset[str] | None = None,
    ) -> FinancialGroupValidation:
        values = {fact.concept: fact.value for fact in facts}
        issues: list[str] = []
        covered = set(values) & _CORE
        required = set(required_concepts or {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"})
        # A normalized structured fact may legitimately use scale=1 while a
        # PDF fact from the same filing retains the table's displayed unit.
        # Only compare scales when the facts come from the same physical
        # document and parser; otherwise cross-source complementing would be
        # rejected merely because their provenance encodes units differently.
        statement_scales: dict[tuple[str, str, str], set[float]] = {}
        for fact in facts:
            if (
                fact.currency
                and fact.concept not in {"reported_roe"}
                and fact.statement in {"income_statement", "balance_sheet", "cash_flow"}
                and fact.source_document
                and fact.parser_version
            ):
                scale_key = (fact.source_document, fact.parser_version, fact.statement)
                statement_scales.setdefault(scale_key, set()).add(float(fact.unit_scale))
        if any(len(scales) > 1 for scales in statement_scales.values()):
            issues.append("statement_unit_scale_inconsistent")
        if {"revenue", "net_income"}.issubset(required) and not {"revenue", "net_income"}.issubset(values):
            issues.append("income_statement_core_missing")
        if "operating_cash_flow" in required and "operating_cash_flow" not in values:
            issues.append("cash_flow_core_missing")
        balance_required = {"assets", "liabilities"} & required
        equity_required = bool({"equity", "total_equity"} & required)
        if (balance_required - values.keys()) or (equity_required and not ({"equity", "total_equity"} & values.keys())):
            issues.append("balance_sheet_core_missing")
        missing_required = required - values.keys()
        if "equity" in missing_required and "total_equity" in values:
            missing_required.remove("equity")
        if missing_required or (equity_required and not ({"equity", "total_equity"} & values.keys())):
            issues.append("core_coverage_insufficient")
        expected_identity = identity[1:]
        for fact in facts:
            fact_identity = (
                fact.end_date,
                (fact.fiscal_period or "FY").upper(),
                fact.consolidated_scope or fact.scope or "unknown",
                fact.currency or "",
            )
            if fact_identity != expected_identity:
                issues.append("group_identity_inconsistent")
            if not fact.accession_number or not fact.source_url or not fact.raw_text:
                issues.append("provenance_missing")
            statement_expected = _STATEMENT_FOR.get(fact.concept)
            if statement_expected and fact.statement != statement_expected and fact.concept != "reported_roe":
                issues.append("statement_mismatch")
            ref = (evidence_map or {}).get(fact.fact_id)
            if fact.parser_version.startswith("financial-ingestion-ast") and (ref is None or ref.bbox is None):
                issues.append("pdf_evidence_bbox_missing")
            if (
                fact.parser_version.startswith("financial-ingestion-ast")
                and
                fact.concept in required
                and fact.concept not in {"reported_roe"}
                and fact.unit_provenance == "unknown"
            ):
                issues.append("unit_provenance_missing")
            if fact.parser_version.startswith("vision-"):
                if ref is None or not ref.content_hash or not ref.locator.startswith("page:") or str(fact.source_page or 0) != ref.locator.split(":", 1)[1] or ref.evidence_id != f"fact:{fact.fact_id}":
                    issues.append("vision_evidence_provenance_missing")
        if values.get("revenue") is not None and values["revenue"] < 0:
            issues.append("negative_revenue")
        assets, liabilities = values.get("assets"), values.get("liabilities")
        equity = values.get("total_equity", values.get("equity"))
        if assets and liabilities is not None:
            ratio = liabilities / assets
            if ratio < 0.01 or ratio > 1.5:
                issues.append("implausible_liabilities_to_assets")
        if assets and equity is not None and abs(equity) / abs(assets) < 0.01:
            issues.append("implausible_total_equity")
        if assets and liabilities is not None and equity is not None:
            if abs(assets - liabilities - equity) / max(abs(assets), 1.0) > 0.08:
                issues.append("balance_sheet_imbalance")
        roe = values.get("reported_roe")
        if roe is not None and not -5 <= roe <= 5:
            issues.append("implausible_reported_roe")
        fatal_issues = [issue for issue in issues if issue not in _COVERAGE_WARNING_ISSUES]
        status = (
            ValidationStatus.REJECTED
            if fatal_issues
            else ValidationStatus.READY_WITH_WARNINGS
            if issues
            else ValidationStatus.VERIFIED
        )
        validation = FinancialValidation(
            status,
            tuple(dict.fromkeys(issues)),
            frozenset(covered),
            tuple(facts) if not fatal_issues else (),
            () if not fatal_issues else tuple(facts),
        )
        return FinancialGroupValidation(identity, validation)

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
    ):
        import pdfplumber

        facts: list[FinancialFact] = []
        refs: list[EvidenceRef] = []
        previous: PdfTableContext | None = _checkpoint_context_from_dict(initial_context)
        self._last_pdf_context = previous
        rules = compatibility_rules or self._compatibility_rules
        if not index_precomputed:
            candidate_pages = _candidate_financial_pages(path, rules=rules)
        with pdfplumber.open(path) as pdf:
            if not manifest.period_end:
                observed_pages = []
                for page_number, page in enumerate(pdf.pages, 1):
                    if candidate_pages is not None and page_number not in candidate_pages:
                        continue
                    try:
                        observed_pages.append(page.extract_text() or "")
                    except Exception:
                        observed_pages.append("")
                manifest = _manifest_from_observed_pages(manifest, filing, observed_pages)
                if not manifest.period_end:
                    return [], []
            for page_number, page in enumerate(pdf.pages, 1):
                if candidate_pages is not None and page_number not in candidate_pages:
                    continue
                words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
                if not words:
                    continue
                rows_by_top: list[list[PdfCellAST]] = []
                for word in words:
                    cell = PdfCellAST(str(word.get("text", "")).strip(), float(word.get("x0", 0)), float(word.get("top", 0)), float(word.get("x1", 0)), float(word.get("bottom", 0)))
                    if cell.text:
                        if rows_by_top and abs(cell.top - rows_by_top[-1][0].top) <= 2.5:
                            rows_by_top[-1].append(cell)
                        else:
                            rows_by_top.append([cell])
                rows = tuple(PdfRowAST(tuple(sorted(cells, key=lambda c: c.x0)), min(c.top for c in cells), (min(c.x0 for c in cells), min(c.top for c in cells), max(c.x1 for c in cells), max(c.bottom for c in cells))) for cells in rows_by_top if cells)
                page_text = page.extract_text() or ""
                sections = _page_sections(previous, page_text, rows, page_number, company.reporting_currency, rules)
                for section in sections:
                    context = section.context
                    statement, scope = context.statement, context.scope
                    multiplier, table_currency = context.multiplier, context.currency
                    table_rows = section.rows
                    table = PdfTableAST(page_number, statement, scope, table_currency, multiplier, _period_headers(table_rows), table_rows)
                    columns = context.periods or _period_columns(table.rows, rules=rules)
                    summary_page = section.summary
                    revenue_totals = _revenue_group_total_rows(
                        table.rows, columns, int(manifest.period_end[:4])
                    ) if statement == "income_statement" and not summary_page else {}
                    equity_totals = _equity_group_total_rows(
                        table.rows, columns, int(manifest.period_end[:4])
                    ) if statement == "balance_sheet" and not summary_page else {}
                    for row_index, row in enumerate(table.rows):
                        merged_row = _merge_visual_rows(table.rows, row_index, rules)
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
                        for concept, labels in _labels_for_rules(rules).items():
                            if concept == "operating_cash_flow" and "现金流出小计" in compact:
                                continue
                            if concept == "assets" and "资产合计" in compact and "资产总计" not in compact and any(prefix in compact for prefix in ("流动资产", "非流动资产")):
                                continue
                            if concept == "liabilities" and "负债合计" in compact and any(prefix in compact for prefix in ("流动负债", "非流动负债")):
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
                            if label is None or (not summary_page and statement != _STATEMENT_FOR[concept] and concept != "reported_roe"):
                                continue
                            if concept == "liabilities" and any(
                                token in _label_compact(compact)
                                for token in ("totalliabilitiesandequity", "totalliabilitiesandshareholdersequity")
                            ):
                                continue
                            if concept == "net_income" and not _net_income_candidate_allowed(candidate_context):
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
                                if prefix and any("\u4e00" <= char <= "\u9fff" for char in prefix[-1:]):
                                    continue
                            label_end = max(
                                (
                                    cell.x1 for cell in label_cells
                                    if not re.fullmatch(r"[一二三四五六七八九十百千万亿\d、.．()（）]+", re.sub(r"\s+", "", cell.text))
                                ),
                                default=0.0,
                            )
                            if summary_page:
                                selected = min(
                                    (cell for cell in value_cells if cell.x0 >= label_end),
                                    key=lambda cell: cell.x0,
                                    default=None,
                                )
                            else:
                                selected = _select_period_cell(merged_row, label_end, columns, int(manifest.period_end[:4]))
                            if selected is None:
                                continue
                            parsed = _parse_number(selected.text)
                            if parsed is None:
                                continue
                            if concept == "capital_expenditure":
                                parsed = abs(parsed)
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
                            value = float(parsed) * (1.0 if concept == "reported_roe" else fact_multiplier)
                            if concept == "reported_roe" and abs(value) > 1:
                                value /= 100.0
                            # Avoid repeated nested rows and parent-only tables.
                            period_start = _period_start(manifest) if statement in {"income_statement", "cash_flow"} else None
                            identity = f"{filing.document_id}|{concept}|{page_number}|{merged_row.bbox}|{value}"
                            fact_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
                            fact = FinancialFact(
                                fact_id=f"ingest:{fact_id}", company_cik=company.security_id,
                                concept=concept, reported_concept=label, value=value,
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
                                validation_status=ValidationStatus.READY_WITH_WARNINGS.value,
                            )
                            ref = EvidenceRef(
                                f"fact:{fact_id}", filing.document_id, filing.source_url,
                                f"{manifest.form_type} {manifest.period_end} / {label}",
                                f"page:{page_number}", merged_row.text, filing.filed_at,
                                filing.content_hash, merged_row.bbox,
                            )
                            existing_index = next(
                                (
                                    index for index, existing in enumerate(facts)
                                    if existing.concept == concept
                                    and existing.fiscal_year == fact.fiscal_year
                                    and existing.end_date == fact.end_date
                                    and existing.usage_status == fact.usage_status
                                ),
                                None,
                            )
                            if existing_index is None:
                                facts.append(fact)
                                refs.append(ref)
                            elif _fact_rank(fact) > _fact_rank(facts[existing_index]):
                                facts[existing_index] = fact
                                refs[existing_index] = ref
                            elif concept == "net_income":
                                if _net_income_candidate_priority(fact) > _net_income_candidate_priority(facts[existing_index]):
                                    facts[existing_index] = fact
                                    refs[existing_index] = ref
                                    continue
                                if _net_income_candidate_priority(fact) < _net_income_candidate_priority(facts[existing_index]):
                                    continue
                                # Wrapped bilingual rows can prepend the prior
                                # line's non-controlling-interest values before
                                # the canonical attributable row. Prefer the
                                # candidate whose label is not preceded by a
                                # numeric value, which preserves the formal
                                # statement total (e.g. Alibaba 103,592 vs
                                # non-controlling interests 1,465).
                                def _has_numeric_prefix(item: FinancialFact) -> bool:
                                    text = item.raw_text.casefold()
                                    marker = "net income"
                                    prefix = text.split(marker, 1)[0] if marker in text else text
                                    return bool(re.search(r"\d", prefix))
                                if _has_numeric_prefix(facts[existing_index]) and not _has_numeric_prefix(fact):
                                    facts[existing_index] = fact
                                    refs[existing_index] = ref
                            elif concept == "operating_cash_flow":
                                # A cash-flow heading can be visually merged
                                # with the preceding net-income row. Prefer a
                                # candidate whose canonical cash label is not
                                # preceded by a numeric adjustment value.
                                def _cash_numeric_prefix(item: FinancialFact) -> bool:
                                    text = item.raw_text.casefold()
                                    markers = ("net cash", "net cash flow", "net cash flows")
                                    marker = next((m for m in markers if m in text), "net cash")
                                    prefix = text.split(marker, 1)[0] if marker in text else text
                                    return bool(re.search(r"\d", prefix))
                                if _cash_numeric_prefix(facts[existing_index]) and not _cash_numeric_prefix(fact):
                                    facts[existing_index] = fact
                                    refs[existing_index] = ref
                            elif concept in {"equity", "total_equity"}:
                                assets = next((item.value for item in facts if item.concept == "assets"), None)
                                liabilities = next((item.value for item in facts if item.concept == "liabilities"), None)
                                existing = facts[existing_index]
                                if assets is not None and liabilities is not None:
                                    target = assets - liabilities
                                    if abs(fact.value - target) < abs(existing.value - target):
                                        facts[existing_index] = fact
                                        refs[existing_index] = ref

                            # Keep an issuer-provided comparison column as an
                            # auditable hidden fact.  It belongs to the same
                            # filing but has its own period and explicit
                            # comparator usage, so it cannot replace the
                            # current-period fact or masquerade as another
                            # filing.  Only a like-for-like visual column is
                            # eligible; ambiguous headers simply produce no
                            # comparator candidate.
                            if not summary_page and len(columns) > 1:
                                target_year = int(manifest.period_end[:4])
                                for comparison_column in columns:
                                    if comparison_column.year in {0, target_year}:
                                        continue
                                    comparison_cell = _select_period_cell(
                                        merged_row, label_end, columns, comparison_column.year
                                    )
                                    if comparison_cell is None:
                                        continue
                                    comparison_value = _parse_number(comparison_cell.text)
                                    if comparison_value is None:
                                        continue
                                    if concept == "capital_expenditure":
                                        comparison_value = abs(comparison_value)
                                    comparison_end = _shift_period_year(
                                        manifest.period_end, comparison_column.year
                                    ) or ""
                                    if not comparison_end:
                                        continue
                                    comparison_start = (
                                        _shift_period_year(
                                            _period_start(manifest),
                                            comparison_column.year - (
                                                int(manifest.period_end[:4])
                                                - int((_period_start(manifest) or manifest.period_end)[:4])
                                            ),
                                        )
                                        if statement in {"income_statement", "cash_flow"}
                                        else None
                                    )
                                    comparison_center = (comparison_cell.x0 + comparison_cell.x1) / 2
                                    comparison_multiplier = (
                                        comparison_column.unit_scale
                                        if comparison_column.currency else multiplier
                                    )
                                    comparison_currency = (
                                        comparison_column.currency or table_currency
                                        or company.reporting_currency
                                    )
                                    comparison_normalized = (
                                        float(comparison_value)
                                        * (1.0 if concept == "reported_roe" else comparison_multiplier)
                                    )
                                    comparison_identity = (
                                        f"{filing.document_id}|{concept}|{page_number}|"
                                        f"{merged_row.bbox}|{comparison_column.year}|{comparison_normalized}"
                                    )
                                    comparison_fact_id = hashlib.sha256(
                                        comparison_identity.encode()
                                    ).hexdigest()[:24]
                                    comparison_fact = replace(
                                        fact,
                                        fact_id=f"ingest:{comparison_fact_id}",
                                        value=comparison_normalized,
                                        fiscal_year=comparison_column.year,
                                        start_date=comparison_start,
                                        end_date=comparison_end,
                                        period_start=comparison_start,
                                        usage_status="comparator",
                                         source_column=comparison_column.header,
                                         raw_text=f"{merged_row.text} [column:{comparison_column.year}]",
                                    )
                                    comparison_ref = replace(
                                        ref,
                                        evidence_id=f"fact:{comparison_fact_id}",
                                        title=(
                                            f"{manifest.form_type} {comparison_end} / {label} "
                                            f"column:{comparison_column.year}"
                                        ),
                                        locator=f"page:{page_number}",
                                        excerpt=comparison_fact.raw_text,
                                    )
                                    if not any(
                                        existing.concept == concept
                                        and existing.fiscal_year == comparison_fact.fiscal_year
                                        and existing.end_date == comparison_fact.end_date
                                        and existing.usage_status == "comparator"
                                        for existing in facts
                                    ):
                                        facts.append(comparison_fact)
                                        refs.append(comparison_ref)
                # Only the last section can continue onto the next page.  A
                # parent section therefore correctly replaces a consolidated
                # context at a same-page boundary.
                if sections:
                    previous = sections[-1].context
                else:
                    previous = None
        self._last_pdf_context = previous
        return facts, refs


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
            scope = statement_context[1] if statement_context else "consolidated"
            period_start = None if statement == "balance_sheet" else _period_start(manifest)
            fact_id = hashlib.sha256(f"{filing.document_id}|{concept}|{page_number}|{value}".encode()).hexdigest()[:24]
            fact = FinancialFact(
                f"compat:{fact_id}", company.security_id, concept, concept, value,
                "ratio" if concept == "reported_roe" else (currency_hint or company.reporting_currency),
                int(manifest.period_end[:4]), manifest.fiscal_period, manifest.form_type,
                period_start, manifest.period_end, filing.filed_at, manifest.accession_number,
                manifest.source_url, scope=scope, entity=company.name, market=company.market,
                statement=statement, period_start=period_start, consolidated_scope=scope,
                currency=currency_hint or company.reporting_currency, unit_scale=1.0 if concept == "reported_roe" else scale,
                unit_provenance=("explicit" if _explicit_unit_info(raw_text, compatibility_rules)[2] else "unknown"),
                source_document=manifest.primary_document, source_page=page_number,
                raw_text=raw_text[:2000], parser_version=_PDF_PARSER_VERSION,
                validation_status=ValidationStatus.READY_WITH_WARNINGS.value,
            )
            facts.append(fact)
            evidence.append(EvidenceRef(
                f"fact:{fact_id}", filing.document_id, filing.source_url,
                f"{manifest.form_type} {manifest.period_end} / {concept}",
                f"page:{page_number}", raw_text[:2000], filing.filed_at, filing.content_hash,
            ))
    # Keep the first occurrence of each concept, preserving statement order.
    unique: dict[str, tuple[FinancialFact, EvidenceRef]] = {}
    for fact, ref in zip(facts, evidence):
        unique.setdefault(fact.concept, (fact, ref))
    return [item[0] for item in unique.values()], [item[1] for item in unique.values()]


def validate_financial_facts(facts: list[FinancialFact]) -> FinancialValidation:
    """Validate facts by filing/period/scope/currency without mixing units."""
    engine = FinancialIngestionEngine()
    groups: dict[tuple[str, str, str, str, str], list[FinancialFact]] = {}
    for fact in facts:
        key = (fact.accession_number, fact.end_date, (fact.fiscal_period or "FY").upper(), fact.consolidated_scope or fact.scope or "unknown", fact.currency or fact.unit)
        groups.setdefault(key, []).append(fact)
    validations = [engine.validate_group(group, identity).validation for identity, group in groups.items()]
    # The ingestion engine keeps warning groups available for audit and repair,
    # but public metrics and model input must cross a stricter trust boundary:
    # only fully VERIFIED groups are accepted. Every other fact is retained as
    # rejected quarantine evidence so it cannot silently leak into analysis.
    accepted = tuple(
        item
        for validation in validations
        if validation.status is ValidationStatus.VERIFIED
        for item in validation.accepted
    )
    quarantined = tuple(
        replace(item, validation_status=ValidationStatus.REJECTED.value)
        for validation in validations
        if validation.status is not ValidationStatus.VERIFIED
        for item in (*validation.accepted, *validation.quarantined)
    )
    issues = tuple(issue for validation in validations for issue in validation.issues)
    if not accepted:
        status = ValidationStatus.REJECTED
    elif any(validation.status is ValidationStatus.REJECTED for validation in validations):
        status = ValidationStatus.READY_WITH_WARNINGS
    elif any(validation.status is ValidationStatus.READY_WITH_WARNINGS for validation in validations):
        status = ValidationStatus.READY_WITH_WARNINGS
    else:
        status = ValidationStatus.VERIFIED
    return FinancialValidation(status, issues, frozenset(item.concept for item in accepted), accepted, quarantined)


def prepare_facts_for_ai(facts: list[FinancialFact]) -> tuple[list[FinancialFact], FinancialValidation]:
    validation = validate_financial_facts(facts)
    return list(validation.accepted), validation


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
