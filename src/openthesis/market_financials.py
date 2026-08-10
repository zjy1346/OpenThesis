from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact


class FinancialExtractionError(RuntimeError):
    """A downloaded filing cannot be read as a trustworthy financial source."""


PARSER_VERSION = "financial-ingestion-v2"


class ValidationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class StatementContext:
    """Context for one statement table, scoped to a page/continuation chain."""

    statement: str
    consolidated_scope: str
    currency: str
    unit: str
    multiplier: float
    source_page: int
    continuation_pages_left: int = 0


@dataclass(frozen=True, slots=True)
class FinancialValidation:
    status: ValidationStatus
    issues: tuple[str, ...]
    covered_concepts: frozenset[str]
    accepted: tuple[FinancialFact, ...] = ()
    quarantined: tuple[FinancialFact, ...] = ()

    @property
    def allow_ai(self) -> bool:
        return self.status in {ValidationStatus.VERIFIED, ValidationStatus.READY_WITH_WARNINGS}


@dataclass(frozen=True, slots=True)
class ConceptPattern:
    concept: str
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    concept: str
    label: str
    value: float
    page_number: int
    excerpt: str
    score: int
    unit_multiplier: float = 1.0
    statement: str = ""
    scope: str = "consolidated"


CONCEPT_PATTERNS: tuple[ConceptPattern, ...] = (
    ConceptPattern(
        "operating_cash_flow",
        (
            "经营活动产生的现金流量净额",
            "经营活动所得现金净额",
            "net cash generated from operating activities",
            "net cash flows from operating activities",
        ),
    ),
    ConceptPattern(
        "capital_expenditure",
        (
            "购建固定资产、无形资产和其他长期资产支付的现金",
            "购建固定资产、无形资产及其他长期资产支付的现金",
            "purchase of property, plant and equipment",
            "capital expenditure",
        ),
    ),
    ConceptPattern(
        "net_income",
        (
            "归属于上市公司股东的净利润",
            "归属于母公司股东的净利润",
            "本公司权益持有人应占盈利",
            "本公司股东应占溢利",
            "profit attributable to owners of the company",
            "net profit attributable to shareholders",
            "净利润",
        ),
    ),
    ConceptPattern(
        "operating_income",
        ("营业利润", "经营利润", "operating profit", "profit from operations"),
    ),
    ConceptPattern(
        "revenue",
        ("营业收入", "收入合计", "營業收入", "收入總額", "revenue", "turnover"),
    ),
    ConceptPattern("assets", ("资产总计", "总资产", "total assets")),
    ConceptPattern("liabilities", ("负债合计", "总负债", "total liabilities")),
    ConceptPattern(
        "equity",
        (
            "归属于母公司所有者权益（或股东权益）合计",
            "归属于母公司所有者权益合计",
            "归属于上市公司股东的所有者权益",
            "equity attributable to owners of the company",
        ),
    ),
    ConceptPattern(
        "total_equity",
        (
            "所有者权益（或股东权益）合计",
            "所有者权益合计",
            "股东权益合计",
            "权益总额",
            "total equity",
        ),
    ),
    ConceptPattern(
        "reported_roe",
        (
            "加权平均净资产收益率（%）",
            "加权平均净资产收益率(%)",
            "加权平均净资产收益率",
            "weighted average return on equity",
        ),
    ),
)

_NUMBER = re.compile(r"(?<![\w.])(?:\(|-)?\s*\d[\d,，]*(?:\.\d+)?\s*\)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")

_TOPIC_LABELS: dict[str, tuple[str, ...]] = {
    "business": ("主营业务", "业务概览", "公司业务概要", "business overview", "principal activities"),
    "risk_factors": ("风险因素", "主要风险", "risk factors", "principal risks"),
    "management_discussion": ("管理层讨论与分析", "经营情况讨论与分析", "management discussion and analysis"),
    "segments": ("分行业", "分产品", "分部资料", "segment information"),
    "growth": ("发展战略", "未来发展", "业务展望", "growth strategy", "outlook"),
}

# Canonical labels used by modern Chinese and English disclosures.  The
# repository historically contained a few mojibake labels; keeping those in
# CONCEPT_PATTERNS preserves backwards compatibility while these labels make
# the parser usable with UTF-8 official PDFs.
_CANONICAL_LABELS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入", "营业总收入", "营业额", "收入", "revenue", "turnover"),
    "operating_income": ("营业利润", "经营利润", "operating profit", "profit from operations"),
    "net_income": ("净利润", "归属于上市公司股东的净利润", "归属于母公司股东的净利润", "profit attributable to owners of the company", "net income"),
    "operating_cash_flow": ("经营活动产生的现金流量净额", "经营活动现金流量净额", "net cash generated from operating activities", "net cash flows from operating activities"),
    "capital_expenditure": ("购建固定资产、无形资产和其他长期资产支付的现金", "资本性支出", "capital expenditure", "purchase of property, plant and equipment"),
    "assets": ("资产总计", "资产总额", "total assets"),
    "liabilities": ("负债合计", "负债总额", "total liabilities"),
    "equity": ("归属于母公司所有者权益合计", "归属于上市公司股东的所有者权益", "equity attributable to owners of the company"),
    "total_equity": ("所有者权益合计", "股东权益合计", "权益总额", "total equity"),
    "reported_roe": ("加权平均净资产收益率", "weighted average return on equity"),
}


def _labels_for(definition: ConceptPattern) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*definition.labels, *_CANONICAL_LABELS.get(definition.concept, ()))))


_STATEMENT_TITLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("balance_sheet", ("合并资产负债表", "资产负债表", "consolidated balance sheet", "statement of financial position")),
    ("income_statement", ("合并利润表", "利润表", "consolidated income statement", "statement of profit or loss")),
    ("cash_flow", ("合并现金流量表", "现金流量表", "consolidated cash flow statement", "statement of cash flows")),
)


def _statement_title_legacy(text: str) -> tuple[str, str] | None:
    lowered = text.casefold()
    for statement, labels in _STATEMENT_TITLES:
        if any(label.casefold() in lowered for label in labels):
            scope = "consolidated" if any(token in lowered for token in ("合并", "consolidated")) else "parent"
            return statement, scope
    # A few issuers use a generic section heading but still provide an explicit
    # unit and statement rows.  Treat it as an unknown statement only when a
    # unit is present; this is safer than inheriting a unit from an unrelated
    # narrative page.
    if _explicit_unit_multiplier_v2(text) is not None:
        return "financial_statement", "consolidated" if "合并" in text or "consolidated" in lowered else "parent"
    return None


def _statement_title(text: str) -> tuple[str, str] | None:
    """Recognize only explicit financial statement headings."""
    lowered = text.casefold()
    heading = "\n".join(text.splitlines()[:2]).casefold()
    for statement, labels in _STATEMENT_TITLES:
        if any(label.casefold() in heading for label in labels):
            if any(token in heading for token in ("consolidated", "鍚堝苟", "合并")):
                scope = "consolidated"
            elif any(token in heading for token in ("parent", "母公司")):
                scope = "parent"
            else:
                scope = "unknown"
            return statement, scope
    return None


def extract_pdf_financials(
    filing: FilingDocument,
    company: Company,
) -> tuple[list[FinancialFact], list[dict[str, object]], list[str]]:
    path = Path(filing.local_path)
    if not filing.local_path or not path.is_file():
        raise FinancialExtractionError("downloaded financial report is unavailable")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - covered by frozen-package smoke tests
        raise FinancialExtractionError("PDF financial-report support is not installed") from exc
    try:
        reader = PdfReader(path)
        pages = [(index, page.extract_text() or "") for index, page in enumerate(reader.pages, 1)]
    except Exception as exc:
        raise FinancialExtractionError("official financial report PDF could not be read") from exc
    facts, evidence = parse_financial_pages(pages, filing, company)
    evidence.extend(_topic_evidence(pages, filing))
    warnings: list[str] = []
    if not facts:
        warnings.append("No high-confidence annual financial values were extracted from the PDF.")
    return facts, [item.to_dict() for item in evidence], warnings


def _parse_financial_pages_legacy(
    pages: Iterable[tuple[int, str]],
    filing: FilingDocument,
    company: Company,
) -> tuple[list[FinancialFact], list[EvidenceRef]]:
    fiscal_year = _filing_year(filing)
    candidates: dict[str, list[_Candidate]] = {item.concept: [] for item in CONCEPT_PATTERNS}
    inherited_multiplier = 1.0
    statement_context_remaining = 0
    for page_number, raw_text in pages:
        text = _normalize_text(raw_text)
        if not text:
            continue
        explicit_multiplier = _explicit_unit_multiplier_v2(text)
        if explicit_multiplier is not None:
            inherited_multiplier = explicit_multiplier
        multiplier = inherited_multiplier
        if _looks_like_financial_statement(text):
            statement_context_remaining = 4
        page_is_statement = statement_context_remaining > 0
        page_is_quarterly_summary = "分季度主要财务数据" in text or (
            "第一季度" in text and "第四季度" in text
        )
        statement_context_remaining = max(0, statement_context_remaining - 1)
        lines = text.splitlines()
        for line_index, line in enumerate(lines):
            compact = re.sub(r"\s+", " ", line).strip()
            lowered = compact.casefold()
            for definition in CONCEPT_PATTERNS:
                label = next((item for item in definition.labels if item.casefold() in lowered), "")
                if not label:
                    continue
                if _reject_candidate(definition.concept, compact, label):
                    continue
                combined = (
                    " ".join(lines[line_index : line_index + 3])
                    if definition.concept == "reported_roe"
                    else compact
                )
                combined = re.sub(r"\s+", " ", combined).strip()
                combined_lowered = combined.casefold()
                tail = combined[combined_lowered.find(label.casefold()) + len(label) :]
                number = _first_financial_value(tail)
                if number is None:
                    continue
                if definition.concept == "reported_roe":
                    value = number / 100.0 if abs(number) > 1 else number
                else:
                    value = number * multiplier
                if definition.concept == "capital_expenditure":
                    value = abs(value)
                if not _plausible(value):
                    continue
                candidates[definition.concept].append(
                    _Candidate(
                        concept=definition.concept,
                        label=label,
                        value=value,
                        page_number=page_number,
                        excerpt=compact[:1200],
                        score=_candidate_score(
                            definition.concept,
                            compact,
                            label,
                            page_is_statement=page_is_statement,
                            page_is_quarterly_summary=page_is_quarterly_summary,
                            explicit_unit=explicit_multiplier is not None,
                        ),
                    )
                )

    found: dict[str, tuple[FinancialFact, EvidenceRef]] = {}
    for definition in CONCEPT_PATTERNS:
        if not candidates[definition.concept]:
            continue
        candidate = max(candidates[definition.concept], key=lambda item: (item.score, item.page_number))
        # Mentions in narrative prose are not deterministic table facts. Accept
        # only an exact row label or a candidate anchored to a financial table.
        if candidate.score < 40:
            continue
        identity = (
            f"{filing.document_id}|{candidate.concept}|{fiscal_year}|"
            f"{candidate.page_number}|{candidate.value}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        fact = FinancialFact(
            fact_id=f"market:{digest}",
            company_cik=company.security_id,
            concept=candidate.concept,
            reported_concept=candidate.label,
            value=candidate.value,
            unit="ratio" if candidate.concept == "reported_roe" else company.reporting_currency,
            fiscal_year=fiscal_year,
            fiscal_period=filing.fiscal_period,
            form_type=filing.form_type,
            start_date=_period_start(filing, fiscal_year),
            end_date=filing.period_end,
            filed_at=filing.filed_at,
            accession_number=filing.accession_number,
            source_url=filing.source_url,
            scope="consolidated",
        )
        evidence = EvidenceRef(
            evidence_id=f"fact:{digest}",
            document_id=filing.document_id,
            source_url=filing.source_url,
            title=f"{filing.form_type} {fiscal_year} · {candidate.label}",
            locator=f"page:{candidate.page_number}",
            excerpt=candidate.excerpt,
            published_at=filing.filed_at,
            content_hash=filing.content_hash,
        )
        found[definition.concept] = (fact, evidence)
    ordered = [found[item.concept] for item in CONCEPT_PATTERNS if item.concept in found]
    return [item[0] for item in ordered], [item[1] for item in ordered]


def _normalize_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("，", ",")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def _first_value(value: str) -> float | None:
    match = _NUMBER.search(value[:240])
    if not match:
        return None
    token = re.sub(r"\s+", "", match.group()).replace(",", "")
    negative = token.startswith("(") or token.startswith("-")
    token = token.strip("()-")
    try:
        number = float(token)
    except ValueError:
        return None
    return -number if negative else number


def _first_financial_value(value: str) -> float | None:
    cleaned = value.strip()
    # Chinese annual-report tables often place a note reference (e.g. 七、80)
    # before the actual current-period value. A split ROE row similarly starts
    # with a footnote marker such as (1).
    cleaned = re.sub(r"^(?:[一二三四五六七八九十]+、\s*\d{1,3}|\(\d{1,2}\)|（\d{1,2}）)\s*", "", cleaned)
    return _first_value(cleaned)


def _explicit_unit_multiplier(text: str) -> float | None:
    header = text[:4000].casefold()
    rules = (
        (("单位：百万元", "单位:百万元", "hk$ million", "rmb million"), 1_000_000.0),
        (("单位：万元", "单位:万元"), 10_000.0),
        (("单位：千元", "单位:千元", "hk$'000", "rmb'000", "in thousands"), 1_000.0),
    )
    for labels, multiplier in rules:
        if any(label in header for label in labels):
            return multiplier
    return None


def _unit_multiplier(text: str) -> float:
    """Backward-compatible helper used by external callers and older tests."""

    return _explicit_unit_multiplier(text) or 1.0


def _looks_like_financial_statement(text: str) -> bool:
    header = text[:2500].casefold()
    return any(
        label in header
        for label in (
            "合并资产负债表",
            "合并利润表",
            "合并现金流量表",
            "主要会计数据",
            "consolidated statement",
            "financial statements",
        )
    )


def _reject_candidate(concept: str, line: str, label: str) -> bool:
    lowered = line.casefold()
    if concept == "revenue" and any(
        token in lowered
        for token in (
            "综合收益",
            "綜合收益",
            "其他综合收益",
            "其他綜合收益",
            "comprehensive income",
        )
    ):
        return True
    if concept == "liabilities" and label in {"负债合计", "total liabilities"}:
        if "流动负债合计" in line or "非流动负债合计" in line:
            return True
    if concept in {"equity", "total_equity"}:
        if "权益总额的" in line or "equity interest of" in lowered:
            return True
        if "%" in line and len(_NUMBER.findall(line)) <= 2:
            return True
    if concept == "reported_roe" and "%" not in line and "percentage" not in lowered:
        # Tables extracted from PDFs sometimes omit the percent sign, but prose
        # without one is too ambiguous to become a deterministic ratio.
        return True
    return False


def _candidate_score(
    concept: str,
    line: str,
    label: str,
    *,
    page_is_statement: bool,
    page_is_quarterly_summary: bool,
    explicit_unit: bool,
) -> int:
    normalized = re.sub(r"^[\s（()一二三四五六七八九十、.0-9]+", "", line).casefold()
    score = len(label) * 2
    if normalized.startswith(label.casefold()):
        score += 60
    if page_is_statement:
        score += 35
    if explicit_unit:
        score += 10
    if page_is_quarterly_summary and concept in {
        "revenue",
        "net_income",
        "operating_income",
        "operating_cash_flow",
    }:
        score -= 90
    if concept == "liabilities" and label.casefold() in {"负债合计", "总负债", "total liabilities"}:
        score += 30
    if concept == "equity" and ("归属于" in label or "attributable" in label.casefold()):
        score += 35
    if concept == "total_equity" and any(token in line for token in ("所有者权益合计", "股东权益合计")):
        score += 30
    if any(token in line for token in ("约占", "激励", "比例为", "同比", "说明：")):
        score -= 80
    return score


def financial_quality_issues(facts: list[FinancialFact]) -> list[str]:
    """Return deterministic reasons why extracted facts are unsafe for AI use."""

    by_period: dict[tuple[int, str], dict[str, float]] = {}
    latest_filed: dict[tuple[int, str, str], str] = {}
    for fact in facts:
        period = (fact.fiscal_period or "FY").upper()
        key = (fact.fiscal_year, period, fact.concept)
        if key not in latest_filed or fact.filed_at >= latest_filed[key]:
            by_period.setdefault((fact.fiscal_year, period), {})[fact.concept] = float(fact.value)
            latest_filed[key] = fact.filed_at
    if not by_period:
        return ["no_financial_facts"]
    useful = {"revenue", "net_income", "assets", "operating_cash_flow"}
    if not any(useful.intersection(values) for values in by_period.values()):
        return ["no_core_financial_facts"]
    issues: list[str] = []
    for (year, period), values in by_period.items():
        prefix = str(year) if period == "FY" else f"{year}-{period}"
        revenue = values.get("revenue")
        if revenue is not None and revenue < 0:
            issues.append(f"{prefix}:negative_revenue")
        assets = values.get("assets")
        liabilities = values.get("liabilities")
        total_equity = values.get("total_equity")
        parent_equity = values.get("equity")
        if assets and liabilities is not None:
            ratio = liabilities / assets
            if ratio < 0.01 or ratio > 1.5:
                issues.append(f"{prefix}:implausible_liabilities_to_assets")
        if assets and parent_equity is not None and abs(parent_equity) / assets < 0.01:
            issues.append(f"{prefix}:implausible_parent_equity")
        if assets and liabilities is not None and total_equity is not None:
            imbalance = abs(assets - liabilities - total_equity) / max(abs(assets), 1.0)
            if imbalance > 0.08:
                issues.append(f"{prefix}:balance_sheet_imbalance")
        reported_roe = values.get("reported_roe")
        if reported_roe is not None and not -5.0 <= reported_roe <= 5.0:
            issues.append(f"{prefix}:implausible_reported_roe")
    revenue_by_year = sorted(
        (year, values["revenue"])
        for (year, period), values in by_period.items()
        if period == "FY" and values.get("revenue", 0) > 0
    )
    for (previous_year, previous), (current_year, current) in zip(
        revenue_by_year,
        revenue_by_year[1:],
    ):
        if current_year - previous_year != 1:
            continue
        ratio = max(current, previous) / max(min(current, previous), 1.0)
        if ratio > 20:
            issues.append(f"{current_year}:implausible_revenue_change")
    return issues


def _filing_year(filing: FilingDocument) -> int:
    match = _YEAR.search(filing.period_end) or _YEAR.search(filing.primary_document)
    return int(match.group()) if match else 0


def _period_start(filing: FilingDocument, fiscal_year: int) -> str:
    """Derive cumulative period start from the actual period end."""
    try:
        end = date.fromisoformat(filing.period_end[:10])
    except ValueError:
        return f"{fiscal_year}-01-01"
    period = (filing.fiscal_period or "FY").upper()
    if period in {"FY", "ANNUAL", "CY", ""}:
        try:
            return (end.replace(year=end.year - 1) + timedelta(days=1)).isoformat()
        except ValueError:
            return (end - timedelta(days=365) + timedelta(days=1)).isoformat()
    # For interim cumulative statements the issuer's fiscal-year start is the
    # first day of the quarter containing the reported end date.  This also
    # avoids hard-coding January for non-calendar fiscal years.
    if period in {"Q1", "Q"}:
        month = ((end.month - 1) // 3) * 3 + 1
        return end.replace(month=month, day=1).isoformat()
    # H1/Q3/9M are cumulative from the fiscal-year start.  Approximate the
    # start by subtracting the elapsed calendar months; this handles common
    # non-December year ends without assuming January unconditionally.
    elapsed = {"Q2": 5, "H1": 5, "Q3": 8, "9M": 8}.get(period, 0)
    month_index = end.year * 12 + end.month - 1 - elapsed
    year, month = divmod(month_index, 12)
    return date(year, month + 1, 1).isoformat()


def _plausible(value: float) -> bool:
    return value != 0 and abs(value) < 10**18


def _topic_evidence(
    pages: Iterable[tuple[int, str]],
    filing: FilingDocument,
) -> list[EvidenceRef]:
    evidence: list[EvidenceRef] = []
    for topic, labels in _TOPIC_LABELS.items():
        for page_number, raw_text in pages:
            text = _normalize_text(raw_text)
            lowered = text.casefold()
            position = next((lowered.find(label.casefold()) for label in labels if label.casefold() in lowered), -1)
            if position < 0:
                continue
            start = max(0, position - 180)
            excerpt = re.sub(r"\s+", " ", text[start : position + 1800]).strip()
            if len(excerpt) < 100:
                continue
            identity = f"{filing.document_id}|{topic}|{page_number}|{excerpt}"
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            evidence.append(
                EvidenceRef(
                    evidence_id=f"filing:{digest}",
                    document_id=filing.document_id,
                    source_url=filing.source_url,
                    title=f"{filing.form_type} {filing.period_end} · {topic}",
                    locator=f"page:{page_number}",
                    excerpt=excerpt,
                    published_at=filing.filed_at,
                    content_hash=filing.content_hash,
                )
            )
            break
    return evidence


# ---------------------------------------------------------------------------
# Context-aware parser (v2)
# ---------------------------------------------------------------------------

def _unit_name(text: str, multiplier: float, currency: str) -> str:
    lowered = text.casefold()
    if "million" in lowered or "百万" in text or "鐧句竾" in text:
        return f"{currency}:million"
    if "thousand" in lowered or "千" in text or "鍗冨" in text:
        return f"{currency}:thousand"
    if "万" in text or "涓囧" in text:
        return f"{currency}:ten-thousand"
    return currency


def _looks_like_continuation(text: str, statement: str) -> bool:
    lowered = text.casefold()
    if any(marker in lowered for marker in ("续", "continued", "continuation")):
        return True
    concepts = {
        "balance_sheet": {"assets", "liabilities", "equity", "total_equity"},
        "cash_flow": {"operating_cash_flow", "capital_expenditure"},
        "income_statement": {"revenue", "operating_income", "net_income"},
    }.get(statement, {item.concept for item in CONCEPT_PATTERNS})
    labels = {label.casefold() for item in CONCEPT_PATTERNS if item.concept in concepts for label in _labels_for(item)}
    return sum(1 for label in labels if label and label in lowered) >= 1 and not _statement_title(text)


def _statement_for_concept(concept: str, context: StatementContext | None) -> str:
    if context is not None:
        return context.statement
    if concept in {"assets", "liabilities", "equity", "total_equity"}:
        return "balance_sheet"
    if concept in {"operating_cash_flow", "capital_expenditure"}:
        return "cash_flow"
    return "income_statement"


def parse_financial_pages(
    pages: Iterable[tuple[int, str]],
    filing: FilingDocument,
    company: Company,
) -> tuple[list[FinancialFact], list[EvidenceRef]]:
    """Parse statement rows with page-local units and explicit provenance.

    A unit is accepted only from the current statement header or an adjacent
    continuation page carrying recognizable rows.  Narrative pages reset the
    context, preventing the old global multiplier inheritance bug.
    """
    fiscal_year = _filing_year(filing)
    candidates: dict[str, list[_Candidate]] = {item.concept: [] for item in CONCEPT_PATTERNS}
    context: StatementContext | None = None
    previous_page = 0
    for page_number, raw_text in pages:
        text = _normalize_text(raw_text)
        if not text:
            continue
        explicit_multiplier = _explicit_unit_multiplier(text)
        title_info = _statement_title(text)
        if title_info:
            statement, scope = title_info
            lowered_title = text.casefold()
            if scope == "parent" and not any(token in lowered_title for token in ("鍚堝苟", "consolidated", "合并", "母公司", "parent")):
                scope = "unknown"
            multiplier = explicit_multiplier or 1.0
            context = StatementContext(
                statement,
                scope,
                company.reporting_currency,
                _unit_name(text, multiplier, company.reporting_currency),
                multiplier,
                page_number,
                3,
            )
        elif context and page_number == previous_page + 1 and context.continuation_pages_left > 0 and _looks_like_continuation(text, context.statement):
            multiplier = context.multiplier
            context = StatementContext(
                context.statement,
                context.consolidated_scope,
                context.currency,
                context.unit,
                context.multiplier,
                context.source_page,
                context.continuation_pages_left - 1,
            )
        else:
            context = None
            multiplier = explicit_multiplier or 1.0
        previous_page = page_number
        page_is_statement = context is not None
        page_concept_density = sum(
            1 for definition in CONCEPT_PATTERNS
            if any(label.casefold() in text.casefold() for label in _labels_for(definition))
        )
        strong_header = any(marker in text.casefold() for marker in ("主要会计数据", "主要财务数据", "鍒嗗搴︿富瑕佽储鍔℃暟鎹?"))
        lowered_page = text.casefold()
        page_is_quarterly_summary = any(
            marker in lowered_page
            for marker in ("quarterly summary", "鍒嗗搴︿富瑕佽储鍔℃暟鎹?")
        )
        lines = text.splitlines()
        for line_index, line in enumerate(lines):
            compact = re.sub(r"\s+", " ", line).strip()
            if not compact:
                continue
            lowered = compact.casefold()
            for definition in CONCEPT_PATTERNS:
                label = next((item for item in _labels_for(definition) if item.casefold() in lowered), "")
                if not label or _reject_candidate(definition.concept, compact, label):
                    continue
                if any(marker in compact for marker in ("不是利润表", "不构成财务报表", "非财务报表")):
                    continue
                if (
                    not page_is_statement
                    and explicit_multiplier is not None
                    and page_concept_density < 3
                    and not strong_header
                    and definition.concept != "reported_roe"
                ):
                    continue
                combined = " ".join(lines[line_index : line_index + 3]) if definition.concept == "reported_roe" else compact
                combined = re.sub(r"\s+", " ", combined).strip()
                combined_lower = combined.casefold()
                label_pos = combined_lower.find(label.casefold())
                tail = combined[label_pos + len(label) :] if label_pos >= 0 else ""
                number = _first_financial_value(tail)
                if number is None:
                    continue
                value = number / 100.0 if definition.concept == "reported_roe" and abs(number) > 1 else number
                if definition.concept != "reported_roe":
                    value *= multiplier
                if definition.concept == "capital_expenditure":
                    value = abs(value)
                if not _plausible(value):
                    continue
                score = _candidate_score(
                    definition.concept,
                    compact,
                    label,
                    page_is_statement=page_is_statement,
                    page_is_quarterly_summary=page_is_quarterly_summary,
                    explicit_unit=explicit_multiplier is not None or context is not None,
                )
                # A row with a verified unit/header is stronger than a
                # same-named narrative mention, but never accept free prose.
                if score < 40:
                    continue
                candidates[definition.concept].append(
                    _Candidate(
                        definition.concept,
                        label,
                        value,
                        page_number,
                        compact[:1200],
                        score,
                        multiplier if definition.concept != "reported_roe" else 1.0,
                        context.statement if context else _statement_for_concept(definition.concept, None),
                        context.consolidated_scope if context else "consolidated",
                    )
                )

    found: dict[str, tuple[FinancialFact, EvidenceRef]] = {}
    for definition in CONCEPT_PATTERNS:
        if not candidates[definition.concept]:
            continue
        candidate = max(candidates[definition.concept], key=lambda item: (item.score, item.page_number))
        identity = f"{filing.document_id}|{candidate.concept}|{fiscal_year}|{candidate.page_number}|{candidate.value}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        fact = FinancialFact(
            fact_id=f"market:{digest}",
            company_cik=company.security_id,
            concept=candidate.concept,
            reported_concept=candidate.label,
            value=candidate.value,
            unit="ratio" if candidate.concept == "reported_roe" else company.reporting_currency,
            fiscal_year=fiscal_year,
            fiscal_period=filing.fiscal_period,
            form_type=filing.form_type,
            start_date=_period_start(filing, fiscal_year),
            end_date=filing.period_end,
            filed_at=filing.filed_at,
            accession_number=filing.accession_number,
            source_url=filing.source_url,
            scope=candidate.scope,
            entity=company.name,
            market=company.market,
            statement=candidate.statement,
            period_start=_period_start(filing, fiscal_year),
            consolidated_scope=candidate.scope,
            currency=company.reporting_currency,
            unit_scale=candidate.unit_multiplier,
            source_document=filing.primary_document,
            source_page=candidate.page_number,
            raw_text=candidate.excerpt,
            parser_version=PARSER_VERSION,
            validation_status=ValidationStatus.READY_WITH_WARNINGS.value,
            revision="original",
        )
        evidence = EvidenceRef(
            evidence_id=f"fact:{digest}",
            document_id=filing.document_id,
            source_url=filing.source_url,
            title=f"{filing.form_type} {fiscal_year} / {candidate.label}",
            locator=f"page:{candidate.page_number}",
            excerpt=candidate.excerpt,
            published_at=filing.filed_at,
            content_hash=filing.content_hash,
        )
        found[definition.concept] = (fact, evidence)
    ordered = [found[item.concept] for item in CONCEPT_PATTERNS if item.concept in found]
    return [item[0] for item in ordered], [item[1] for item in ordered]


def validate_financial_facts(facts: list[FinancialFact]) -> FinancialValidation:
    """Validate independently by period/scope/currency; quarantine bad groups."""
    groups: dict[tuple[int, str, str, str], list[FinancialFact]] = {}
    for fact in facts:
        key = (fact.fiscal_year, (fact.fiscal_period or "FY").upper(), fact.consolidated_scope or fact.scope or "unknown", fact.currency or fact.unit)
        groups.setdefault(key, []).append(fact)
    core = {"revenue", "net_income", "assets", "liabilities", "equity", "operating_cash_flow"}
    accepted: list[FinancialFact] = []
    quarantined: list[FinancialFact] = []
    issues: list[str] = []
    for key, group in groups.items():
        group_issues = financial_quality_issues(group)
        covered = {fact.concept for fact in group}
        if len(covered & core) < 3:
            group_issues = [*group_issues, "core_coverage_insufficient"]
        if group_issues:
            quarantined.extend(group)
            issues.extend(f"{key[0]}-{key[1]}:{issue}" for issue in group_issues)
        else:
            accepted.extend(group)
    covered = frozenset(fact.concept for fact in accepted)
    if not accepted:
        normalized_issues = list(issues or ["core_coverage_insufficient"])
        if any("core_coverage_insufficient" in issue for issue in normalized_issues) and "core_coverage_insufficient" not in normalized_issues:
            normalized_issues.append("core_coverage_insufficient")
        return FinancialValidation(ValidationStatus.REJECTED, tuple(normalized_issues), covered, (), tuple(quarantined))
    status = ValidationStatus.VERIFIED if len(covered & core) >= len(core) else ValidationStatus.READY_WITH_WARNINGS
    for fact in accepted:
        fact.validation_status = status.value
    return FinancialValidation(status, tuple(issues), covered, tuple(accepted), tuple(quarantined))


# Unit recognition intentionally operates on the local page/header text only.
def _explicit_unit_multiplier_v2(text: str) -> float | None:
    header = text[:4000].casefold().replace(" ", "")
    if any(token in header for token in ("百万元", "百万人民币", "鍗曚綅锛氱櫨涓囧厓", "rmbmillion", "hk$million", "usd million", "in millions")):
        return 1_000_000.0
    if any(token in header for token in ("万元", "万人民币", "鍗曚綅锛氫竾鍏", "rmbten-thousand")):
        return 10_000.0
    if any(token in header for token in ("千元", "千人民币", "鍗曚綅锛氬崈鍏", "rmb'000", "hk$'000", "inthousands")):
        return 1_000.0
    if re.search(r"\b(?:million|mn)\b", header):
        return 1_000_000.0
    if re.search(r"\b(?:thousand|000)\b", header):
        return 1_000.0
    return None


_NUMBER_V2 = re.compile(r"(?<![\w.])(?:\(\s*[+-]?[\d,]+(?:\.\d+)?\s*\)|[+-]?\s*[\d,]+(?:\.\d+)?|[—–-])")


def _first_value(value: str) -> float | None:
    match = _NUMBER_V2.search(value[:400])
    if not match:
        return None
    token = re.sub(r"\s+", "", match.group()).replace(",", "")
    if token in {"-", "—", "–"}:
        return None
    negative = token.startswith("(") or token.startswith("-")
    token = token.strip("()-")
    try:
        number = float(token)
    except ValueError:
        return None
    return -number if negative else number


def _first_financial_value(value: str) -> float | None:
    cleaned = value.strip()
    cleaned = re.sub(r"^(?:\([^)]{0,4}\)|\[[^]]{0,4}\]|[①②③④⑤⑥⑦⑧⑨⑩])\s*", "", cleaned)
    matches = list(_NUMBER_V2.finditer(cleaned[:400]))
    if not matches:
        return None
    # Footnote glyphs in extracted Chinese PDFs can become a lone zero before
    # the actual value.  Prefer the next substantial/comma-separated number.
    if len(matches) > 1:
        first = matches[0].group().replace(",", "").strip()
        second = matches[1].group()
        if first.isdigit() and len(first) <= 3 and "," in second:
            return _first_value(cleaned[matches[1].start() :])
    return _first_value(cleaned)
