"""Structured-first financial ingestion with auditable PDF table parsing.

The public seam in this module deliberately separates source adapters from the
normalisation/validation policy.  A PDF is parsed as positioned words grouped
into page/table/row/cell nodes; numbers are never selected from an unscoped
page-wide regular-expression match.
"""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import CancelledError, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
import hashlib
import re

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


def _period_columns_legacy(rows: Sequence[PdfRowAST]) -> tuple[_PeriodColumn, ...]:
    """Build x-coordinate intervals from a real table header row."""
    for row in rows:
        candidates: list[tuple[int, float]] = []
        for cell in row.cells:
            match = re.search(r"20\d{2}", cell.text)
            if match:
                candidates.append((int(match.group()), (cell.x0 + cell.x1) / 2))
        if not candidates:
            # Balance/cash tables often use semantic headers rather than
            # years.  ``期末``/``本期`` is the current-period column and is
            # intentionally represented by year 0; the selector maps it to
            # any requested manifest year.
            semantic = [
                (0, (cell.x0 + cell.x1) / 2)
                for cell in row.cells
                if any(marker in cell.text for marker in ("期末", "本期", "本年", "current"))
            ]
            if semantic:
                candidates = semantic
            else:
                continue
        if len(candidates) < 2 and re.search(r"20\d{2}年(?:\d{1,2}[月—-]|\d{1,2}月\d{1,2}日)", row.text):
            # Do not treat a statement title such as ``2025年1—12月`` as the
            # period header; it has no value-column geometry.
            continue
        if len(candidates) < 2:
            normalized = row.text.casefold()
            title_markers = ("for the year ended", "year ended", "as at", "年度", "年末")
            current_markers = ("current", "期末", "本期", "本年")
            if len(candidates) == 1 and (
                any(marker in normalized for marker in title_markers)
                or (row_index == 0 and not any(marker in normalized for marker in current_markers))
            ):
                continue
        # A title line with one year is not a table header. A one-column table
        # is still supported by giving the column a generous bounded interval.
        centers = [center for _, center in candidates]
        result: list[_PeriodColumn] = []
        for index, (year, center) in enumerate(candidates):
            left = (centers[index - 1] + center) / 2 if index else center - 90
            right = (center + centers[index + 1]) / 2 if index + 1 < len(centers) else center + 90
            result.append(_PeriodColumn(year, center, left, right))
        return tuple(result)
    return ()


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
        "cny", "rmb", "人民币", "元", "千元", "千人民币", "万元",
        "万人民币", "百万元", "rmb'000", "rmbmillion", "inthousands",
        "inmillions", "thousand", "million",
    )
    return scale, currency, any(marker.casefold() in compact for marker in markers)


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
            if not periods and previous and previous.statement == statement and previous.scope == scope:
                periods = previous.periods
            sections.append(PdfPageSection(
                PdfTableContext(statement, scope, section_scale, section_currency,
                                explicit, periods, page_number, 0),
                section_rows, False, False,
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


def _parse_local_pdfs_bounded(
    engine: "FinancialIngestionEngine",
    company: Company,
    filings: Sequence[FilingDocument],
    manifests: dict[str, FilingManifest],
    *,
    parse: Callable[[str, Company, FilingDocument, FilingManifest], tuple[list[FinancialFact], list[EvidenceRef]]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    max_workers: int = 3,
) -> dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]]:
    """Pre-parse local PDFs with bounded, deterministic worker coordination.

    The parser itself is the only work performed in worker threads. Results,
    diagnostics, and progress are collected by the caller thread in completion
    order and then mapped back to the original filing order. Structured sources
    and vision adapters remain in the sequential ingest loop.
    """
    parse_fn = parse or engine._parse_pdf_ast
    default_parser = (
        parse is None
        and getattr(parse_fn, "__func__", None) is FinancialIngestionEngine._parse_pdf_ast
    )
    unique: list[tuple[str, FilingDocument, FilingManifest]] = []
    duplicate_ids: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    for filing in filings:
        manifest = manifests.get(filing.document_id)
        if manifest is None or not filing.local_path or not Path(filing.local_path).is_file():
            continue
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

    page_indexes: dict[str, frozenset[int] | None] = {}
    if default_parser:
        # Whole-document text indexing has a materially higher memory peak
        # than parsing the selected statement pages. Build indexes in a
        # deterministic sequence, then parallelize only bounded page parsing.
        for key, filing, _manifest in unique:
            if cancel_check is not None and cancel_check():
                return {}
            page_indexes[key] = _candidate_financial_pages(filing.local_path)

    def parse_one(item: tuple[str, FilingDocument, FilingManifest]) -> tuple[str, list[FinancialFact], list[EvidenceRef], str | None]:
        key, filing, manifest = item
        if cancel_check is not None and cancel_check():
            return key, [], [], None
        try:
            facts, refs = parse_fn(filing.local_path, company, filing, manifest)
            return key, list(facts), list(refs), None
        except Exception as exc:
            return key, [], [], f"pdf_table_parse_failed:{type(exc).__name__}"

    results: dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]] = {}
    workers = (
        _safe_pdf_worker_count(
            [item[1] for item in unique], requested=max_workers
        )
        if default_parser
        else max(1, min(3, int(max_workers), len(unique)))
    )
    completed = 0
    executor_type = ProcessPoolExecutor if default_parser and workers > 1 else ThreadPoolExecutor
    executor_kwargs = {"max_workers": workers}
    if executor_type is ThreadPoolExecutor:
        executor_kwargs["thread_name_prefix"] = "financial-pdf"
    executor = executor_type(**executor_kwargs)
    futures = []
    cancelled = False
    try:
        futures = [
            executor.submit(
                _parse_pdf_process_worker,
                item[0], company, item[1], item[2], page_indexes.get(item[0]),
            )
            if default_parser and workers > 1
            else executor.submit(parse_one, item)
            for item in unique
        ]
        for future in as_completed(futures):
            if cancel_check is not None and cancel_check():
                cancelled = True
                # Do not disturb results that have already completed; cancel
                # only work that has not started and let running workers drain.
                for pending in futures:
                    if not pending.done():
                        pending.cancel()
            try:
                key, facts, refs, error = future.result()
            except CancelledError:
                continue
            results[key] = (facts, refs, error)
            completed += 1
            if progress is not None:
                progress("filing-parse", completed, len(unique))
    finally:
        if cancel_check is not None and cancel_check():
            cancelled = True
            for pending in futures:
                if not pending.done():
                    pending.cancel()
        # Waiting allows already-running native parsers to release resources;
        # cancel_futures prevents queued filings from starting after cancel.
        executor.shutdown(wait=True, cancel_futures=cancelled)

    by_document: dict[str, tuple[list[FinancialFact], list[EvidenceRef], str | None]] = {}
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


def _safe_pdf_worker_count(
    filings: Sequence[FilingDocument], *, requested: int
) -> int:
    """Keep process isolation inside a conservative aggregate file budget.

    Compressed annual reports expand substantially during text and coordinate
    extraction. Small independent documents may use two workers; larger
    batches remain sequential after concurrent download and page indexing.
    """

    count = len(filings)
    if count <= 1:
        return count
    total_bytes = 0
    for filing in filings:
        try:
            total_bytes += Path(filing.local_path).stat().st_size
        except OSError:
            return 1
    if total_bytes > 8 * 1024 * 1024:
        return 1
    return max(1, min(2, int(requested), count))


def _candidate_financial_pages(path: str, *, continuation_pages: int = 3) -> frozenset[int] | None:
    """Find formal statement pages with a low-memory text prepass.

    Annual reports can contain hundreds of narrative pages. Extracting
    coordinate words from every page is both slow and memory-heavy, so pypdf
    first locates title-like financial statements. The coordinate parser then
    reads only each title page and a bounded continuation window. If the index
    cannot be built, callers fail open to the established full-document path.
    """

    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        starts: list[int] = []
        summary_pages: set[int] = set()
        statement_kinds: set[str] = set()
        for page_number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            compact = re.sub(r"\s+", "", text).casefold()
            if any(
                label.casefold() in compact
                for label in _LABELS["reported_roe"]
            ):
                # ROE is commonly disclosed in a standalone performance table
                # outside the three formal statements. Keep the exact page in
                # the bounded coordinate pass without widening its continuation
                # window to unrelated narrative pages.
                summary_pages.add(page_number)
            statement_context = _statement_context(text)
            if statement_context is not None:
                starts.append(page_number)
                statement_kinds.add(statement_context[0])
        # A partial index is unsafe: missing one statement can make the
        # coordinate parser report an apparently valid but incomplete filing.
        # Returning None deliberately fails open to the full-document parser.
        if not starts or statement_kinds != {"income_statement", "balance_sheet", "cash_flow"}:
            return None
        selected: set[int] = set()
        page_count = len(reader.pages)
        for start in starts:
            selected.update(
                range(start, min(page_count, start + max(0, continuation_pages)) + 1)
            )
        selected.update(summary_pages)
        return frozenset(selected)
    except Exception:
        return None


class FinancialIngestionEngine:
    """Structured-first engine; PDF is a coordinate-aware deterministic fallback."""

    def ingest(
        self, company: Company, filings: Sequence[FilingDocument], *,
        structured_sources: Sequence[FinancialSourceAdapter] = (),
        vision_fallback: VisionFinancialSourceAdapter | None = None,
        vision_config: VisionFallbackConfig | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> FinancialDataset:
        manifests: list[FilingManifest] = []
        diagnostics: list[str] = []
        facts: list[FinancialFact] = []
        evidence: list[EvidenceRef] = []
        groups: list[FinancialGroupValidation] = []
        reconciliation_quarantine: list[FinancialFact] = []
        total_filings = len(filings)
        manifest_by_document: dict[str, FilingManifest] = {}
        for filing in filings:
            manifest = _manifest_for(filing)
            if manifest is None:
                diagnostics.append(f"{filing.document_id}:ambiguous_period_identity")
                continue
            manifests.append(manifest)
            manifest_by_document[filing.document_id] = manifest
        parsed_pdfs = _parse_local_pdfs_bounded(
            self,
            company,
            filings,
            manifest_by_document,
            cancel_check=cancel_check,
            progress=progress,
        )
        for filing_index, filing in enumerate(filings, start=1):
            if cancel_check is not None and cancel_check():
                break
            manifest = manifest_by_document.get(filing.document_id)
            if manifest is None:
                continue
            parsed: list[FinancialFact] = []
            refs: list[EvidenceRef] = []
            for adapter in structured_sources:
                sfacts, srefs, failure = adapter.fetch(company, filing)
                if failure:
                    diagnostics.append(f"{filing.document_id}:{failure}")
                    continue
                if sfacts:
                    structured_identity = (
                        sfacts[0].accession_number,
                        sfacts[0].end_date,
                        (sfacts[0].fiscal_period or "FY").upper(),
                        sfacts[0].consolidated_scope or sfacts[0].scope or "unknown",
                        sfacts[0].currency or company.reporting_currency,
                    )
                    structured_refs = {ref.evidence_id.removeprefix("fact:"): ref for ref in srefs}
                    structured_refs.update({fact.fact_id: ref for fact, ref in zip(sfacts, srefs)})
                    structured_validation = self._validate_group(
                        list(sfacts), structured_identity, structured_refs
                    )
                    if structured_validation.validation.status in {
                        ValidationStatus.VERIFIED,
                        ValidationStatus.READY_WITH_WARNINGS,
                    } and structured_validation.validation.accepted and (
                        "core_coverage_insufficient"
                        not in structured_validation.validation.issues
                    ):
                        parsed, refs = sfacts, srefs
                        break
                    # A partial structured feed is useful diagnostics but is
                    # not allowed to suppress a complete official PDF table.
                    diagnostics.extend(
                        f"{filing.document_id}:structured_source_quality:{issue}"
                        for issue in structured_validation.validation.issues
                    )
                    diagnostics.append(f"{filing.document_id}:structured_source_incomplete")
                    if not (filing.local_path and Path(filing.local_path).is_file()):
                        # Preserve a rejected structured group for quarantine
                        # and audit when no document fallback is available.
                        parsed, refs = sfacts, srefs
                        break
            if not parsed and filing.local_path and Path(filing.local_path).is_file():
                cached_parse = parsed_pdfs.get(filing.document_id)
                if cached_parse is not None:
                    parsed, refs, parse_error = cached_parse
                    if parse_error:
                        diagnostics.append(f"{filing.document_id}:{parse_error}")
            if not parsed and vision_fallback is not None and vision_config is not None and vision_config.enabled:
                if progress:
                    progress("vision-processing", filing_index, total_filings)
                pages = _vision_failed_pages(filing, vision_config)
                if pages:
                    if progress:
                        progress("vision-processing", filing_index, total_filings)
                    result = vision_fallback.extract(
                        company, filing, pages, vision_config, cancel_check=cancel_check
                    )
                    diagnostics.extend(f"{filing.document_id}:{item}" for item in result.diagnostics)
                    if result.facts:
                        parsed, refs = list(result.facts), list(result.evidence)
            if not parsed:
                diagnostics.append(f"{filing.document_id}:no_financial_facts")
                if progress:
                    progress("filing-validation", filing_index, total_filings)
                continue
            if not refs:
                refs = [self._evidence_for_fact(fact, filing) for fact in parsed]
            group_map: dict[tuple[str, str, str, str, str], list[FinancialFact]] = {}
            for fact in parsed:
                identity = (fact.accession_number, fact.end_date, (fact.fiscal_period or "FY").upper(), fact.consolidated_scope or fact.scope or "unknown", fact.currency or company.reporting_currency)
                group_map.setdefault(identity, []).append(fact)
            evidence_map = {ref.evidence_id.removeprefix("fact:"): ref for ref in refs}
            evidence_map.update({fact.fact_id: ref for fact, ref in zip(parsed, refs)})
            validated = [self._validate_group(group, identity, evidence_map) for identity, group in group_map.items()]
            if (
                vision_fallback is not None
                and vision_config is not None
                and vision_config.enabled
                and any(
                    item.validation.status is ValidationStatus.REJECTED
                    or "core_coverage_insufficient" in item.validation.issues
                    for item in validated
                )
            ):
                pages = _vision_failed_pages(filing, vision_config, tuple(issue for item in validated for issue in item.validation.issues), parsed)
                if pages:
                    if progress:
                        progress("vision-processing", filing_index, total_filings)
                    result = vision_fallback.extract(
                        company, filing, pages, vision_config, cancel_check=cancel_check
                    )
                    diagnostics.extend(f"{filing.document_id}:{item}" for item in result.diagnostics)
                    if result.facts:
                        seed = parsed[0] if parsed else result.facts[0]
                        identity = (seed.accession_number, seed.end_date, (seed.fiscal_period or "FY").upper(), seed.consolidated_scope or seed.scope or "unknown", seed.currency or company.reporting_currency)
                        parsed, refs, audit_facts = _reconcile_candidates(
                            parsed, refs, result.facts, result.evidence, prefer_vision=True,
                            validate=lambda selected, selected_map: self._validate_group(selected, identity, selected_map),
                        )
                        reconciliation_quarantine.extend(audit_facts)
                        group_map = {}
                        for fact in parsed:
                            identity = (fact.accession_number, fact.end_date, (fact.fiscal_period or "FY").upper(), fact.consolidated_scope or fact.scope or "unknown", fact.currency or company.reporting_currency)
                            group_map.setdefault(identity, []).append(fact)
                        evidence_map = {ref.evidence_id.removeprefix("fact:"): ref for ref in refs}
                        evidence_map.update({fact.fact_id: ref for fact, ref in zip(parsed, refs)})
                        validated = [self._validate_group(group, identity, evidence_map) for identity, group in group_map.items()]
                        if progress:
                            progress("filing-validation", filing_index, total_filings)
            for result in validated:
                groups.append(result)
                diagnostics.extend(f"{filing.document_id}:{issue}" for issue in result.validation.issues)
            facts.extend(parsed)
            evidence.extend(refs)
            if progress:
                progress("filing-validation", filing_index, total_filings)
        accepted = tuple(f for result in groups for f in result.validation.accepted)
        quarantined = tuple(f for result in groups for f in result.validation.quarantined) + tuple(reconciliation_quarantine)
        for fact in reconciliation_quarantine:
            fact.validation_status = ValidationStatus.REJECTED.value
        issues = tuple(issue for result in groups for issue in result.validation.issues)
        if not accepted:
            status = ValidationStatus.REJECTED
        elif any(result.validation.status is ValidationStatus.REJECTED for result in groups):
            status = ValidationStatus.READY_WITH_WARNINGS
        elif any(result.validation.status is ValidationStatus.READY_WITH_WARNINGS for result in groups):
            status = ValidationStatus.READY_WITH_WARNINGS
        else:
            status = ValidationStatus.VERIFIED
        validation = FinancialValidation(status, issues, frozenset(f.concept for f in accepted), accepted, quarantined)
        # Preserve each fact's own group status.  The dataset aggregate may be
        # READY_WITH_WARNINGS because another filing was quarantined.
        for result in groups:
            for fact in result.validation.accepted:
                fact.validation_status = result.validation.status.value
            for fact in result.validation.quarantined:
                fact.validation_status = ValidationStatus.REJECTED.value
        return FinancialDataset(accepted, tuple(evidence), tuple(manifests), validation, tuple(dict.fromkeys(diagnostics)), tuple(groups))

    @staticmethod
    def _evidence_for_fact(fact: FinancialFact, filing: FilingDocument) -> EvidenceRef:
        return EvidenceRef(
            f"fact:{fact.fact_id}", filing.document_id, filing.source_url,
            f"{filing.form_type} {fact.end_date} / {fact.concept}",
            f"page:{fact.source_page or 0}", fact.raw_text, filing.filed_at,
            filing.content_hash,
            fact.source_bbox,
        )

    def _validate_group(
        self,
        facts: list[FinancialFact],
        identity: tuple[str, str, str, str, str],
        evidence_map: dict[str, EvidenceRef] | None = None,
    ) -> FinancialGroupValidation:
        values = {fact.concept: fact.value for fact in facts}
        issues: list[str] = []
        covered = set(values) & _CORE
        required = (
            {"revenue", "net_income"}
            | {"operating_cash_flow"}
            | {"assets", "liabilities"}
        )
        if not {"revenue", "net_income"}.issubset(values):
            issues.append("income_statement_core_missing")
        if "operating_cash_flow" not in values:
            issues.append("cash_flow_core_missing")
        if not {"assets", "liabilities"}.issubset(values) or not ({"equity", "total_equity"} & values.keys()):
            issues.append("balance_sheet_core_missing")
        if not required.issubset(values) or not ({"equity", "total_equity"} & values.keys()):
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
                            if concept == "net_income" and not _net_income_candidate_allowed(compact):
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
    validations = [engine._validate_group(group, identity).validation for identity, group in groups.items()]
    accepted = tuple(item for validation in validations for item in validation.accepted)
    quarantined = tuple(item for validation in validations for item in validation.quarantined)
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
