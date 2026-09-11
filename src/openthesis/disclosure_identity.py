"""Resolve filing disclosure identity before financial facts are compiled.

This module deliberately has one small public seam.  Provider metadata is
useful discovery input, not an authority that may silently rewrite a title's
period or invent a calendar year-end for a non-calendar issuer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class DisclosureIdentity:
    fiscal_period: str
    period_end: str | None
    revision: str
    provisional: bool
    diagnostics: tuple[str, ...] = ()
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)


class DisclosureIdentityResolver:
    """A deterministic, source-aware disclosure identity resolver."""

    def resolve(
        self,
        *,
        title: str = "",
        filed_at: str = "",
        provider_metadata: Mapping[str, Any] | None = None,
        observed_statement_dates: Sequence[str] = (),
        revision: str = "original",
    ) -> DisclosureIdentity:
        metadata = dict(provider_metadata or {})
        diagnostics: list[str] = []
        title_period, title_date = _period_from_title(title)
        provider_period = _normalise_period(metadata.get("fiscal_period"))
        fiscal_period = title_period or provider_period or "FY"

        filed_date = _parse_date(filed_at)
        explicit_candidates: list[date] = []
        if title_date is not None:
            explicit_candidates.append(title_date)
        for value in observed_statement_dates:
            parsed = _parse_date(value)
            if parsed is not None:
                explicit_candidates.append(parsed)

        # Explicit title/statement dates outrank provider metadata.  A
        # metadata-only 12/31 is intentionally provisional: many issuers use
        # March or June year ends, and announcement year is not a period end.
        metadata_date = _parse_date(metadata.get("period_end"))
        valid_explicit = [
            candidate for candidate in explicit_candidates
            if _date_is_valid(candidate, filed_date)
        ]
        if title_date is not None and any(
            candidate != title_date for candidate in valid_explicit
        ):
            diagnostics.append("title_observed_period_conflict")
        # The latest valid observed date is the current disclosure period;
        # callers may provide a comparison column first.
        # A report title identifies the disclosure year, while page text often
        # also contains filing/audit dates and prior-year comparison columns.
        # Prefer dates in the title's report year before selecting the latest
        # remaining observed date; this prevents an audit date from becoming a
        # fabricated period end.
        title_year = _title_report_year(title)
        preferred_explicit = (
            [candidate for candidate in valid_explicit if candidate.year == title_year]
            if title_year is not None else []
        )
        if preferred_explicit:
            chosen: date | None = max(preferred_explicit)
        elif valid_explicit and title_year is not None:
            # Only comparison/filing dates were observed.  Guessing one as the
            # report period would silently move facts into the wrong cohort.
            chosen = None
            diagnostics.append("title_observed_period_conflict")
        else:
            chosen = max(valid_explicit) if valid_explicit else None
        if explicit_candidates and chosen is None:
            diagnostics.append("period_end_after_filed_at")
        if chosen is None and metadata_date is not None:
            if not _date_is_valid(metadata_date, filed_date):
                diagnostics.append("period_end_after_filed_at")
            elif metadata_date.month == 12 and metadata_date.day == 31 and title_date is None:
                diagnostics.append("period_end_provisional")
            else:
                chosen = metadata_date
        elif chosen is not None and metadata_date is not None and metadata_date != chosen:
            diagnostics.append("provider_period_conflict")

        provisional = chosen is None
        if provisional and "period_end_provisional" not in diagnostics:
            diagnostics.append("period_end_provisional")
        return DisclosureIdentity(
            fiscal_period=fiscal_period,
            period_end=chosen.isoformat() if chosen is not None else None,
            revision=str(revision or metadata.get("revision") or "original"),
            provisional=provisional,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            raw_metadata=metadata,
        )


def _normalise_period(value: Any) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    if text in {"H1", "HY", "HALFYEAR", "INTERIM"}:
        return "H1"
    if text in {"Q1", "Q2", "Q3", "Q4"}:
        return text
    if text in {"FY", "ANNUAL", "YEAR"}:
        return "FY"
    return ""


def _period_from_title(title: str) -> tuple[str, date | None]:
    text = str(title or "")
    lowered = text.casefold()
    if re.search(r"半年度|半年度报告|中期|上半年|半年报|half[- ]?year|six months|interim", lowered):
        period = "H1"
    elif re.search(r"第三季度|三季度|第3季度|q3|third quarter", lowered):
        period = "Q3"
    elif re.search(r"第一季度|一季度|第1季度|q1|first quarter", lowered):
        period = "Q1"
    elif re.search(r"第二季度|二季度|第2季度|q2|second quarter", lowered):
        period = "Q2"
    elif re.search(r"第四季度|四季度|第4季度|q4|fourth quarter", lowered):
        period = "Q4"
    elif re.search(r"年度|年报|annual|year ended|fiscal year", lowered):
        period = "FY"
    else:
        period = ""
    dates = [_parse_date(match) for match in _date_tokens(text)]
    dates = [item for item in dates if item is not None]
    return period, dates[0] if dates else None


def _title_report_year(title: str) -> int | None:
    """Return the first explicit report year in a title, if any."""
    match = re.search(r"\b(20\d{2})\b|(?<!\d)(20\d{2})年", str(title or ""))
    return int(next(group for group in match.groups() if group)) if match else None


def _date_tokens(text: str) -> list[str]:
    tokens = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text)
    tokens.extend(re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", text))
    tokens.extend(
        re.findall(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            text,
            flags=re.IGNORECASE,
        )
    )
    tokens.extend(
        re.findall(
            r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
            text,
            flags=re.IGNORECASE,
        )
    )
    return tokens


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y年%m月%d日",
        "%B %d, %Y",
        "%B %d %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _date_is_valid(value: date, filed_at: date | None) -> bool:
    return filed_at is None or value <= filed_at
