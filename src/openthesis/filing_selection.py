from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .domain import FilingDocument


ANNUAL = "ANNUAL_REPORT"
PERIODIC = frozenset({"INTERIM_REPORT", "QUARTERLY_REPORT"})
LISTING = frozenset({"PROSPECTUS", "LISTING_REPORT"})


@dataclass(frozen=True, slots=True)
class ResearchFilingSet:
    """A policy result describing the official documents safe to research."""

    documents: tuple[FilingDocument, ...]
    annual_years: tuple[int, ...]
    used_listing_fallback: bool


def select_research_filings(
    candidates: list[FilingDocument],
    *,
    annual_limit: int = 5,
) -> ResearchFilingSet:
    """Prefer annual reports; use periodic reports only for years lacking one.

    Listing documents are a last-resort source for newly listed companies and are
    included only when no annual report exists anywhere in the discovered set.
    """

    # De-duplicate by fiscal period, not announcement date/document id.  A
    # correction/revision is a new document id but must replace the original
    # for analysis while the original remains available to audit callers.
    by_period: dict[tuple[int, str], FilingDocument] = {}
    for item in candidates:
        key = (_fiscal_year(item), (item.fiscal_period or "FY").upper())
        current = by_period.get(key)
        if current is None or _revision_rank(item) > _revision_rank(current):
            by_period[key] = item
    ordered = sorted(by_period.values(), key=_sort_key, reverse=True)
    annual_by_year: dict[int, FilingDocument] = {}
    periodic_by_year: dict[int, list[FilingDocument]] = {}
    listing: list[FilingDocument] = []
    for filing in ordered:
        year = _fiscal_year(filing)
        if filing.form_type == ANNUAL:
            annual_by_year.setdefault(year, filing)
        elif filing.form_type in PERIODIC:
            periodic_by_year.setdefault(year, []).append(filing)
        elif filing.form_type in LISTING:
            listing.append(filing)

    kept_annual_years = tuple(sorted(annual_by_year, reverse=True)[: max(1, annual_limit)])
    selected: list[FilingDocument] = [annual_by_year[year] for year in kept_annual_years]

    # A periodic filing is normally useful only when its fiscal year has no annual
    # report. Keep every discovered period for such a year so a newly listed
    # issuer can be researched from Q1/H1/Q3 instead of one arbitrarily chosen
    # snapshot.
    primary_periodics: list[FilingDocument] = []
    for year in sorted(periodic_by_year, reverse=True):
        if year in annual_by_year:
            continue
        current = sorted(periodic_by_year[year], key=_period_rank)
        selected.extend(current)
        primary_periodics.extend(current)

    # Like-for-like interim growth needs the previous year's same period even if
    # that prior year also has an annual report. Retain only that bounded
    # comparison companion; do not reintroduce every same-year periodic filing or
    # compare cumulative Q1/H1/Q3 values with a full fiscal year.
    selected_ids = {item.document_id for item in selected}
    for filing in primary_periodics:
        companion = by_period.get((_fiscal_year(filing) - 1, filing.fiscal_period.upper()))
        if companion is not None and companion.document_id not in selected_ids:
            selected.append(companion)
            selected_ids.add(companion.document_id)

    used_listing_fallback = not annual_by_year and bool(listing)
    if used_listing_fallback:
        selected.extend(listing[:2])

    selected.sort(key=_sort_key, reverse=True)
    return ResearchFilingSet(
        documents=tuple(selected),
        annual_years=kept_annual_years,
        used_listing_fallback=used_listing_fallback,
    )


def _fiscal_year(filing: FilingDocument) -> int:
    try:
        return int(filing.period_end[:4])
    except (TypeError, ValueError):
        try:
            return int(filing.filed_at[:4])
        except (TypeError, ValueError):
            return 0


def _period_rank(filing: FilingDocument) -> tuple[int, str]:
    rank = {"Q1": 1, "H1": 2, "Q3": 3, "Q": 3}.get(filing.fiscal_period, 0)
    return rank, filing.filed_at


def _sort_key(filing: FilingDocument) -> tuple[date, int, int, str]:
    try:
        period_end = date.fromisoformat(filing.period_end[:10])
    except (TypeError, ValueError):
        period_end = date.min
    kind_rank = {
        "ANNUAL_REPORT": 4,
        "QUARTERLY_REPORT": 3,
        "INTERIM_REPORT": 2,
        "PROSPECTUS": 1,
        "LISTING_REPORT": 0,
    }.get(filing.form_type, -1)
    return period_end, kind_rank, _document_authority(filing), filing.filed_at


def _document_authority(filing: FilingDocument) -> int:
    """Prefer the native-market full annual report over mirrored announcements."""

    title = (filing.primary_document or "").casefold()
    company_id = (filing.company_cik or "").casefold()
    rank = 0
    if company_id.startswith("cn_a:"):
        if any(token in title for token in ("港股公告", "h股公告", "英文版", "english")):
            rank -= 100
        if any(token in title for token in ("年度报告", "年度報告", "annual report")):
            rank += 30
        if "摘要" in title or "summary" in title:
            rank -= 40
    return rank


def _revision_rank(filing: FilingDocument) -> tuple[int, int, str]:
    """Prefer explicit corrected versions, then the latest filed timestamp."""
    title = (filing.primary_document or "").casefold()
    revision = 1 if filing.revision.casefold() not in {"", "original", "orig"} else 0
    if any(token in title for token in ("更正", "修订", "revision", "restated", "corrigendum")):
        revision = max(revision, 1)
    if filing.supersedes_document_id:
        revision += 1
    return revision, _document_authority(filing), str(filing.filed_at)
