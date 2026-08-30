"""Structured-first financial ingestion with auditable PDF table parsing.

The public seam in this module deliberately separates source adapters from the
normalisation/validation policy.  A PDF is parsed as positioned words grouped
into page/table/row/cell nodes; numbers are never selected from an unscoped
page-wide regular-expression match.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


_PDF_PARSER_VERSION = "financial-ingestion-ast-v3"
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
        facts = list(self.facts_by_document.get(filing.document_id, ()))
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


def build_financial_profile(
    facts: Sequence[FinancialFact],
    validation_groups: Sequence[FinancialGroupValidation] = (),
    reporting_currency: str = "",
    *,
    selected_filings: Sequence[FilingDocument] = (),
    manifests: Sequence[FilingManifest] = (),
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
    )


_LABELS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业总收入", "营业收入", "营业收入合计", "revenue", "revenues", "total revenue", "operating revenue"),
    "net_income": (
        "归属于母公司股东的净利润", "归属于母公司所有者的净利润",
        "归属于上市公司股东的净利润", "net income attributable to owners",
        "equity holders of the company", "net profit attributable to shareholders of the parent company",
        "net income attributable to alibaba group holding limited",
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
    "balance_sheet": ("合并资产负债表", "资产负债表", "consolidated balance sheet", "consolidated balance sheets", "statement of financial position"),
    "income_statement": ("合并利润表", "利润表", "consolidated income statement", "consolidated income statements", "statement of profit or loss"),
    "cash_flow": ("合并现金流量表", "现金流量表", "consolidated cash flow statement", "consolidated statements of cash flows", "statement of cash flows"),
}
_NUM = re.compile(r"(?:\(\s*[+-]?[\d,]+(?:\.\d+)?\s*\)|[+-]?[\d,]+(?:\.\d+)?)")


def _manifest_for(filing: FilingDocument) -> FilingManifest | None:
    title = filing.primary_document or ""
    folded = title.casefold()
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
    return FilingManifest(
        filing.document_id, filing.accession_number, filing.source_url,
        filing.primary_document, form, period, period_end,
        filing.revision, filing.supersedes_document_id, filing.content_hash,
    )


def _unit_scale(text: str) -> tuple[float, str]:
    """Parse explicit currency and unit markers from one table context."""
    normalized = re.sub(r"\s+", "", text.casefold()).translate(str.maketrans({"’": "'", "‘": "'", "′": "'", "＇": "'", "ʼ": "'"}))
    if any(token in normalized for token in ("hk$", "hk£", "hkd", "港元", "港币")):
        currency = "HKD"
    elif any(token in normalized for token in ("usd", "us$", "美元")):
        currency = "USD"
    elif any(token in normalized for token in ("cny", "rmb", "人民币", "元")):
        currency = "CNY"
    else:
        currency = ""
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


def _explicit_currencies(text: str) -> frozenset[str]:
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
    return frozenset(currencies)


def _statement_context(text: str) -> tuple[str, str] | None:
    positions: list[tuple[int, str, str]] = []
    folded = text.casefold()
    offset = 0
    for line in text.splitlines():
        clean = line.strip()
        line_folded = clean.casefold()
        for statement, markers in _STATEMENT_MARKERS.items():
            for marker in markers:
                if marker.casefold() not in line_folded:
                    continue
                # Narrative sentences such as “不是利润表” are not table
                # identities. Require a short title-like line.
                if len(clean) > 48 or "不是" in clean or "说明" in clean:
                    continue
                pos = offset + line_folded.find(marker.casefold())
                scope = "consolidated" if "合并" in clean or "consolidated" in line_folded else "parent"
                positions.append((pos, statement, scope))
        offset += len(line) + 1
    if not positions:
        return None
    _, statement, scope = max(positions)
    return statement, scope


def _period_start(manifest: FilingManifest) -> str | None:
    if manifest.fiscal_period == "FY":
        try:
            end = date.fromisoformat(manifest.period_end[:10])
            previous = end.replace(year=end.year - 1)
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
    rows: Sequence[PdfRowAST], preferred_currency: str = ""
) -> tuple[_PeriodColumn, ...]:
    """Choose visual year columns before any one-year title/date row."""
    dual: list[tuple[int, float]] | None = None
    fallback: list[tuple[int, float]] | None = None
    all_text = " ".join(row.text for row in rows)
    global_scale, _ = _unit_scale(all_text)
    unit_cells: list[tuple[float, str]] = []
    for row_index, row in enumerate(rows):
        for cell in row.cells:
            normalized = re.sub(r"\s+", "", cell.text.casefold()).translate(str.maketrans({"’": "'", "‘": "'", "＇": "'"}))
            if normalized in {"rmb", "cny", "rmb'000", "rmbmillion", "us$", "usd", "hkd", "hk$"}:
                _, currency = _unit_scale(cell.text)
                if currency:
                    unit_cells.append(((cell.x0 + cell.x1) / 2, currency))
        by_year: dict[int, float] = {}
        for cell in row.cells:
            effective_year = _effective_period_year(cell.text)
            if effective_year is not None:
                by_year.setdefault(effective_year, (cell.x0 + cell.x1) / 2)
                continue
            match = re.search(r"20\d{2}", cell.text)
            if match:
                by_year.setdefault(int(match.group()), (cell.x0 + cell.x1) / 2)
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


def _explicit_unit_info(text: str) -> tuple[float, str, bool]:
    """Return ``(scale, currency, explicit)`` for a page/table heading.

    ``_unit_scale`` intentionally defaults to one for compatibility.  The
    parser must nevertheless distinguish an absent heading from an explicit
    ``元``/``RMB`` heading so an empty continuation page cannot reset a
    thousand-yuan context to a unit scale of one.
    """
    scale, currency = _unit_scale(text)
    if len(_explicit_currencies(text)) > 1:
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
    return scale, currency, any(marker.casefold() in compact for marker in markers) or explicit_unit_header


def _row_title_context(row: PdfRowAST) -> tuple[str, str] | None:
    """Identify a formal statement title from positioned row text."""
    text = row.text.strip()
    folded = text.casefold()
    for statement, markers in _STATEMENT_MARKERS.items():
        for marker in markers:
            marker_folded = marker.casefold()
            if marker_folded not in folded:
                continue
            if len(text) > 64 or "不是" in text or "说明" in text:
                continue
            scope = "consolidated" if "合并" in text or "consolidated" in folded else "parent"
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


def _continuation_compatible(context: PdfTableContext, rows: Sequence[PdfRowAST]) -> bool:
    """Require a labelled target row and a value in a known period column."""
    if not context.periods or not rows:
        return False
    for index in range(len(rows)):
        merged = _merge_visual_rows(rows, index)
        if not _known_label(_row_label_text(merged)):
            continue
        label_end = max(
            (cell.x1 for cell in merged.cells if _parse_number(cell.text) is None),
            default=0.0,
        )
        for cell in merged.cells:
            if _parse_number(cell.text) is None or cell.x0 < label_end:
                continue
            center = (cell.x0 + cell.x1) / 2
            if any(column.left <= center <= column.right for column in context.periods):
                return True
    return False


def _page_sections(
    previous: PdfTableContext | None,
    page_text: str,
    rows: Sequence[PdfRowAST],
    page_number: int,
    default_currency: str,
) -> tuple[PdfPageSection, ...]:
    """Pure page-context state transition used by the PDF adapter.

    Summary pages never inherit.  Formal titles create a new context; pages
    without a title may inherit only two adjacent pages and only when their
    coordinates still look like the same table.  Multiple titles on one page
    become separate sections, which prevents a parent table header from
    reclassifying the consolidated rows above it.
    """
    rows_tuple = tuple(rows)
    scale, currency, explicit = _explicit_unit_info(page_text)
    if _is_summary_page(page_text, rows_tuple):
        return (PdfPageSection(
            PdfTableContext("summary", "consolidated", scale, currency or default_currency,
                            explicit, _period_columns(rows_tuple, default_currency), page_number, 0),
            rows_tuple, True, False,
        ),)

    titles = [(index, _row_title_context(row)) for index, row in enumerate(rows_tuple)]
    titles = [(index, context) for index, context in titles if context is not None]
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
            if _continuation_compatible(previous, prefix):
                sections.append(PdfPageSection(
                    PdfTableContext(previous.statement, previous.scope, previous.multiplier,
                                    previous.currency, previous.unit_explicit,
                                    previous.periods, page_number, previous.inherited_pages + 1),
                    prefix, False, True,
                ))
        for position, (title_index, (statement, scope)) in enumerate(titles):
            end = titles[position + 1][0] if position + 1 < len(titles) else len(rows_tuple)
            section_rows = rows_tuple[title_index:end]
            section_scale = scale if explicit else 1.0
            section_currency = currency or default_currency
            periods = _period_columns(section_rows, default_currency)
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
            if not periods and previous and previous.statement == statement and previous.scope == scope:
                periods = previous.periods
            sections.append(PdfPageSection(
                PdfTableContext(statement, scope, section_scale, section_currency,
                                explicit or can_inherit_unit, periods, page_number,
                                previous.inherited_pages + 1 if can_inherit_unit else 0),
                section_rows, False, can_inherit_unit,
            ))
        return tuple(sections)

    if (
        previous
        and previous.statement != "summary"
        and previous.last_page + 1 == page_number
        and previous.inherited_pages < 2
        and _continuation_compatible(previous, rows_tuple)
    ):
        inherited_scale = previous.multiplier if not explicit else scale
        inherited_currency = currency or previous.currency or default_currency
        return (PdfPageSection(
            PdfTableContext(previous.statement, previous.scope, inherited_scale, inherited_currency,
                            previous.unit_explicit or explicit, previous.periods, page_number,
                            previous.inherited_pages + 1),
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


def _known_label(text: str) -> bool:
    compact = _label_compact(text)
    return any(_label_compact(label) in compact for labels in _LABELS.values() for label in labels)


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


def _merge_visual_rows(rows: Sequence[PdfRowAST], start: int) -> PdfRowAST:
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
        if candidate_label and current_label and _known_label(candidate_label) and (
            "现金流" in current_label
            or "资产总计" in candidate_label
            or "负债合计" in candidate_label
            or "资产合计" in candidate_label and ("流动资产" in current_label or "非流动资产" in current_label)
        ):
            break
        # A separate complete row label is never a continuation fragment.
        if candidate_label and _known_label(candidate_label) and not _known_label(combined_label):
            break
        variants.append(candidate)
        merged_probe = PdfRowAST(
            tuple(cell for row in variants for cell in row.cells),
            variants[0].top,
            variants[0].bbox,
        )
        if _known_label(_row_label_text(merged_probe)) and any(
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


def _is_summary_page(page_text: str, rows: Sequence[PdfRowAST]) -> bool:
    """Detect the metrics page even when PDF text wraps its labels."""
    compact = re.sub(r"\s+", "", page_text).casefold()
    if any(label.casefold() in compact for label in _LABELS["reported_roe"]):
        return True
    return any(
        any(label.casefold() in _row_label_text(_merge_visual_rows(rows, index)).casefold() for label in _LABELS["reported_roe"])
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
        formal_starts: list[tuple[int, str, bool]] = []
        for index, page in enumerate(reader.pages):
            found: tuple[str, bool] | None = None
            lines = (page.extract_text() or "").splitlines()
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
        starts: list[int] = [
            index for index, statement, eligible in formal_starts
            if statement in target and eligible
        ]
        chosen: list[int] = []
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
) -> tuple[str, list[FinancialFact], list[EvidenceRef], str | None]:
    """Pickle-safe worker used only for CPU-heavy local statement pages."""

    try:
        facts, refs = FinancialIngestionEngine()._parse_pdf_ast(
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
) -> None:
    """Process entrypoint which returns one bounded, pickle-safe result."""

    try:
        index_error = None
        try:
            indexed_pages = _candidate_financial_pages(filing.local_path)
        except Exception as exc:
            indexed_pages = None
            index_error = f"pdf_index_failed:{type(exc).__name__}"
        result_queue.put(("filing-index", key, indexed_pages, index_error))
        # A partial index is deliberately represented by None; the AST parser
        # then fails open to its full-document path for correctness.
        result_queue.put(("filing-result", _parse_pdf_process_worker(
            key, company, filing, manifest, indexed_pages
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
            process = context.Process(
                target=entrypoint,
                args=(key, company, filing, manifest, None, result_queue),
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


def _candidate_pages_from_text(
    page_texts: Sequence[str], *, continuation_pages: int
) -> frozenset[int] | None:
    """Convert one page-index text stream into the bounded candidate set."""

    starts: list[int] = []
    summary_pages: set[int] = set()
    statement_kinds: set[str] = set()
    for page_number, text in enumerate(page_texts, 1):
        compact = re.sub(r"\s+", "", text).casefold()
        if any(label.casefold() in compact for label in _LABELS["reported_roe"]):
            # ROE is commonly disclosed in a standalone performance table
            # outside the three formal statements. Keep the exact page in
            # the bounded coordinate pass without widening its continuation
            # window to unrelated narrative pages.
            summary_pages.add(page_number)
        statement_context = _statement_context(text)
        if statement_context is not None:
            starts.append(page_number)
            statement_kinds.add(statement_context[0])
    # A partial index is unsafe: missing one statement can make the coordinate
    # parser report an apparently valid but incomplete filing. Returning None
    # deliberately fails open to the full-document parser.
    if not starts or statement_kinds != {"income_statement", "balance_sheet", "cash_flow"}:
        return None
    selected: set[int] = set()
    page_count = len(page_texts)
    for start in starts:
        selected.update(
            range(start, min(page_count, start + max(0, continuation_pages)) + 1)
        )
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
    path: str, *, continuation_pages: int
) -> frozenset[int] | None:
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
    return _candidate_pages_from_text(page_texts, continuation_pages=continuation_pages)


def _candidate_financial_pages_pypdf(
    path: str, *, continuation_pages: int
) -> frozenset[int] | None:
    """Compatibility indexer used when PDFium is unavailable or incomplete."""

    from pypdf import PdfReader

    reader = PdfReader(path)
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    return _candidate_pages_from_text(page_texts, continuation_pages=continuation_pages)


def _candidate_financial_pages(path: str, *, continuation_pages: int = 3) -> frozenset[int] | None:
    """Find formal statement pages with a low-memory text prepass.

    PDFium supplies the fast text layer for the normal path. If PDFium cannot
    be imported/read safely, or produces an incomplete three-statement index,
    pypdf remains the compatibility fallback. Both paths retain the bounded
    continuation window and standalone ROE summary-page semantics.
    """

    try:
        indexed = _candidate_financial_pages_pypdfium(
            path, continuation_pages=continuation_pages
        )
        if indexed is not None:
            return indexed
    except Exception:
        pass
    try:
        return _candidate_financial_pages_pypdf(
            path, continuation_pages=continuation_pages
        )
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class FinancialCandidateCollection:
    """Untrusted candidate batches collected before canonical resolution."""

    manifests: tuple[FilingManifest, ...]
    batches_by_document: dict[str, tuple[Any, ...]]
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[str, ...] = ()


class FinancialIngestionEngine:
    """Structured-first engine; PDF is a coordinate-aware deterministic fallback."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        max_workers: int = 3,
        parse_timeout_seconds: float = 120.0,
        batch_timeout_seconds: float = 280.0,
    ) -> None:
        # The cache is deliberately optional so fixture/injected engines keep
        # their historical behavior. Production supplies a workspace-owned
        # directory; successful entries are content addressed and atomic.
        self._parse_cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._parse_cache_memory: dict[str, tuple[list[FinancialFact], list[EvidenceRef]]] = {}
        self._max_workers = max(1, min(3, int(max_workers)))
        self._parse_timeout_seconds = max(0.1, float(parse_timeout_seconds))
        self._batch_timeout_seconds = max(0.1, min(280.0, float(batch_timeout_seconds)))

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
            facts = [FinancialFact(**item) for item in payload["facts"]]
            refs = [EvidenceRef(**item) for item in payload["evidence"]]
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
        safe_facts = [replace(item) for item in facts]
        safe_refs = [replace(item) for item in refs]
        self._parse_cache_memory[key] = (safe_facts, safe_refs)
        if self._parse_cache_dir is None:
            return
        payload = json.dumps(
            {"facts": [item.to_dict() for item in safe_facts], "evidence": [item.to_dict() for item in safe_refs]},
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
            pdf_candidates = tuple(
                FactCandidate(
                    fact,
                    ((ref,) if ref is not None else ()),
                    "financial-ingestion-ast",
                )
                for fact, ref in zip(pdf_facts, pdf_refs)
            )
            pdf_batch = CandidateBatch(filing, pdf_candidates, tuple(pdf_refs))
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
        status = (
            ValidationStatus.VERIFIED
            if compiled.allow_ai
            else ValidationStatus.READY_WITH_WARNINGS
            if compiled.resolved_facts
            else ValidationStatus.REJECTED
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
    ):
        import pdfplumber

        facts: list[FinancialFact] = []
        refs: list[EvidenceRef] = []
        previous: PdfTableContext | None = None
        if not index_precomputed:
            candidate_pages = _candidate_financial_pages(path)
        with pdfplumber.open(path) as pdf:
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
                sections = _page_sections(previous, page_text, rows, page_number, company.reporting_currency)
                for section in sections:
                    context = section.context
                    statement, scope = context.statement, context.scope
                    multiplier, table_currency = context.multiplier, context.currency
                    table_rows = section.rows
                    table = PdfTableAST(page_number, statement, scope, table_currency, multiplier, _period_headers(table_rows), table_rows)
                    columns = context.periods or _period_columns(table.rows)
                    summary_page = section.summary
                    revenue_totals = _revenue_group_total_rows(
                        table.rows, columns, int(manifest.period_end[:4])
                    ) if statement == "income_statement" and not summary_page else {}
                    equity_totals = _equity_group_total_rows(
                        table.rows, columns, int(manifest.period_end[:4])
                    ) if statement == "balance_sheet" and not summary_page else {}
                    for row_index, row in enumerate(table.rows):
                        merged_row = _merge_visual_rows(table.rows, row_index)
                        compact = _row_label_text(merged_row)
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
                        for concept, labels in _LABELS.items():
                            if concept == "operating_cash_flow" and "现金流出小计" in compact:
                                continue
                            if concept == "assets" and "资产合计" in compact and "资产总计" not in compact and any(prefix in compact for prefix in ("流动资产", "非流动资产")):
                                continue
                            if concept == "liabilities" and "负债合计" in compact and any(prefix in compact for prefix in ("流动负债", "非流动负债")):
                                continue
                            label = next((label for label in labels if _label_compact(label) in _label_compact(compact)), None)
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
                                revision=manifest.revision,
                                source_document=manifest.primary_document, source_page=page_number,
                                source_bbox=merged_row.bbox,
                                raw_text=merged_row.text, parser_version="financial-ingestion-ast-v2",
                                validation_status=ValidationStatus.READY_WITH_WARNINGS.value,
                            )
                            ref = EvidenceRef(
                                f"fact:{fact_id}", filing.document_id, filing.source_url,
                                f"{manifest.form_type} {manifest.period_end} / {label}",
                                f"page:{page_number}", merged_row.text, filing.filed_at,
                                filing.content_hash, merged_row.bbox,
                            )
                            existing_index = next(
                                (index for index, existing in enumerate(facts) if existing.concept == concept),
                                None,
                            )
                            if existing_index is None:
                                facts.append(fact)
                                refs.append(ref)
                            elif _fact_rank(fact) > _fact_rank(facts[existing_index]):
                                facts[existing_index] = fact
                                refs[existing_index] = ref
                            elif concept == "net_income":
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
                # Only the last section can continue onto the next page.  A
                # parent section therefore correctly replaces a consolidated
                # context at a same-page boundary.
                if sections:
                    previous = sections[-1].context
                else:
                    previous = None
        return facts, refs


# ---------------------------------------------------------------------------
# Compatibility functions for the service/storage seam. These functions use
# the same label/unit policy as the engine and never bypass its validation gate.
# ---------------------------------------------------------------------------

def parse_structured_snapshot(raw_excerpt: str) -> dict[str, float]:
    """Parse a bounded, provider-supplied excerpt into normalized values.

    This is intentionally limited to one row at a time; it is not a PDF page
    parser and therefore cannot silently infer a value from unrelated prose.
    """
    scale, _ = _unit_scale(raw_excerpt)
    result: dict[str, float] = {}
    compact = re.sub(r"\s+", " ", raw_excerpt)
    # Structured adapters commonly serialize a bounded snapshot as
    # concept=value pairs. Accept only the known concept identifiers.
    for key, token in re.findall(r"([a-z_]+)\s*=\s*([-+]?\d[\d,]*(?:\.\d+)?)", raw_excerpt, flags=re.IGNORECASE):
        if key in _LABELS:
            result[key] = float(token.replace(",", "")) * (1.0 if key == "reported_roe" else scale)
    if result:
        return result
    for concept, labels in _LABELS.items():
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
    pages: Sequence[tuple[int, str]], filing: FilingDocument, company: Company
) -> tuple[list[FinancialFact], list[EvidenceRef]]:
    """Compatibility adapter for bounded provider excerpts and unit tests."""
    manifest = _manifest_for(filing)
    if manifest is None:
        return [], []
    facts: list[FinancialFact] = []
    evidence: list[EvidenceRef] = []
    for page_number, raw_text in pages:
        values = parse_structured_snapshot(raw_text)
        statement_context = _statement_context(raw_text)
        if statement_context is None and "=" not in raw_text:
            # A free-form narrative is not a statement table. Structured
            # key=value snapshots are the only context-free compatibility form.
            continue
        for concept, value in values.items():
            statement = _STATEMENT_FOR[concept]
            if statement_context and statement_context[0] != statement and concept != "reported_roe":
                continue
            scale, currency_hint = _unit_scale(raw_text)
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
                source_document=manifest.primary_document, source_page=page_number,
                raw_text=raw_text[:2000], parser_version="financial-ingestion-v2",
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
