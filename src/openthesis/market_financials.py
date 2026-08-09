from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact


class FinancialExtractionError(RuntimeError):
    """A downloaded filing cannot be read as a trustworthy financial source."""


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


def parse_financial_pages(
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
        explicit_multiplier = _explicit_unit_multiplier(text)
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
            start_date=f"{fiscal_year}-01-01",
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

    by_year: dict[int, dict[str, float]] = {}
    latest_filed: dict[tuple[int, str], str] = {}
    for fact in facts:
        key = (fact.fiscal_year, fact.concept)
        if key not in latest_filed or fact.filed_at >= latest_filed[key]:
            by_year.setdefault(fact.fiscal_year, {})[fact.concept] = float(fact.value)
            latest_filed[key] = fact.filed_at
    if not by_year:
        return ["no_financial_facts"]
    useful = {"revenue", "net_income", "assets", "operating_cash_flow"}
    if not any(useful.intersection(values) for values in by_year.values()):
        return ["no_core_financial_facts"]
    issues: list[str] = []
    for year, values in by_year.items():
        revenue = values.get("revenue")
        if revenue is not None and revenue < 0:
            issues.append(f"{year}:negative_revenue")
        assets = values.get("assets")
        liabilities = values.get("liabilities")
        total_equity = values.get("total_equity")
        parent_equity = values.get("equity")
        if assets and liabilities is not None:
            ratio = liabilities / assets
            if ratio < 0.01 or ratio > 1.5:
                issues.append(f"{year}:implausible_liabilities_to_assets")
        if assets and parent_equity is not None and abs(parent_equity) / assets < 0.01:
            issues.append(f"{year}:implausible_parent_equity")
        if assets and liabilities is not None and total_equity is not None:
            imbalance = abs(assets - liabilities - total_equity) / max(abs(assets), 1.0)
            if imbalance > 0.08:
                issues.append(f"{year}:balance_sheet_imbalance")
        reported_roe = values.get("reported_roe")
        if reported_roe is not None and not -5.0 <= reported_roe <= 5.0:
            issues.append(f"{year}:implausible_reported_roe")
    revenue_by_year = sorted(
        (year, values["revenue"])
        for year, values in by_year.items()
        if values.get("revenue", 0) > 0
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
