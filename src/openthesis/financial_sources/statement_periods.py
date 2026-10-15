"""Pure, source-bound period declarations from formal statement headers.

Publication, audit-signature and note narrative dates are not reporting dates.
This module records declarations; it does not guess a calendar year end.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import re
from typing import Sequence

from ..disclosure_identity import _date_tokens, _parse_date
from ..financial_statement_identity import find_statement_headings


_CONTENTS_PAGE_TITLE = re.compile(r"^(?:table\s+of\s+contents|contents|目录|目錄|目次)$", re.I)
_ANCHOR = re.compile(
    r"^(?:for\s+the\s+)?(?:(?:three|six|nine|twelve)\s+months?\s+ended|"
    r"years?\s+ended|as\s+(?:of|at))\b",
    re.I,
)
_MONTHS = "January February March April May June July August September October November December".split()
_MONTH_DAY = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\b", re.I)
_DAY_MONTH = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\b", re.I)
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)")


def column_period_years(header: str) -> frozenset[int]:
    """Read the printed opening/closing column identity, not a bare year.

    January 1 opening balances belong to the preceding fiscal year. The same
    interpretation is used by admission, including Chinese/English/numeric
    dates; it does not invent a missing period or replace the stored header.
    """
    text = re.sub(r"\s+", " ", str(header or "")).strip()
    tokens = _date_tokens(text)
    dates = {_parse_date(token) for token in tokens}
    dates.discard(None)
    dates.update(_numeric_header_dates(text))
    if dates:
        end = max(dates)
        return frozenset({end.year - 1 if len(dates) == 1 and end.month == end.day == 1 else end.year})
    return frozenset(int(year) for year in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text))


def _numeric_header_dates(line: str) -> tuple[date, ...]:
    """Exact day/month/year column literals, never a narrative date search."""
    dates = []
    for match in _NUMERIC_DATE.finditer(line):
        day, month, year = map(int, match.groups())
        try:
            dates.append(date(year, month, day))
        except ValueError:
            return ()
    return tuple(dates)


@dataclass(frozen=True, slots=True)
class StatementPeriodDeclaration:
    page: int
    period_end: str
    excerpt: str
    duration_months: int | None = None


@dataclass(frozen=True, slots=True)
class StatementPeriodGap:
    page: int
    kind: str
    excerpt: str
    span: tuple[int, int]
    code: str = "PERIOD_DECLARATION_NOT_FOUND"


def statement_period_gaps(page_texts: Sequence[str]) -> tuple[StatementPeriodGap, ...]:
    """Source-bound formal headers needing repair; never inferred periods."""
    declared_pages = {item.page for item in statement_period_declarations(page_texts)}
    gaps = []
    for page, text in enumerate(page_texts, 1):
        if page in declared_pages:
            continue
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if _CONTENTS_PAGE_TITLE.fullmatch(first_line):
            continue
        headings = find_statement_headings(text, page=page)
        for index, heading in enumerate(headings):
            next_heading = headings[index + 1].span[0] if index + 1 < len(headings) else len(text)
            # A bounded original header excerpt, including its missing or
            # unreadable date row, is retained for the existing page/OCR lane.
            tail = text[heading.span[1]:next_heading].splitlines(keepends=True)[:17]
            end = heading.span[1] + sum(len(line) for line in tail)
            gaps.append(StatementPeriodGap(page, heading.kind, text[heading.span[0]:end],
                                           (heading.span[0], end)))
    return tuple(gaps)


def period_gap_evidence(filing, gaps):
    from ..domain import EvidenceRef
    return [EvidenceRef("period-gap:" + hashlib.sha256(
        f"{filing.document_id}|{filing.content_hash}|{gap.page}|{gap.span}|{gap.excerpt}".encode()).hexdigest()[:24],
        filing.document_id, filing.source_url, f"{gap.code} / {gap.kind}",
        f"page:{gap.page}", gap.excerpt, filing.filed_at, filing.content_hash) for gap in gaps]


def _bounded_anchor_header(line: str, anchor: re.Match[str], tokens: Sequence[str]) -> bool:
    """Reject prose after an anchored date while allowing split header rows."""
    suffix = line[anchor.end():]
    for token in tokens:
        suffix = suffix.replace(token, " ")
    if not tokens:
        fragment = _MONTH_DAY.search(suffix) or _DAY_MONTH.search(suffix)
        if fragment:
            suffix = suffix[:fragment.start()] + " " + suffix[fragment.end():]
    suffix = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", suffix)
    return re.fullmatch(r"[\s,，;；|()（）:：.]*", suffix) is not None


def statement_period_declarations(page_texts: Sequence[str]) -> tuple[StatementPeriodDeclaration, ...]:
    """Read only bounded, anchored date headers below formal statement titles.

    A split ``Year ended March 31,`` / ``2024 2025 2026`` is one declaration
    per printed year. Conflicting declarations stay distinct for the caller.
    """
    declarations: list[StatementPeriodDeclaration] = []
    for page_number, text in enumerate(page_texts, 1):
        source_text = text or ""
        first_content_line = next((line.strip() for line in source_text.splitlines() if line.strip()), "")
        if _CONTENTS_PAGE_TITLE.fullmatch(first_content_line):
            continue
        lines: list[tuple[int, int, str]] = []
        offset = 0
        for raw_line in source_text.splitlines(keepends=True):
            line = raw_line.rstrip("\r\n")
            clean = re.sub(r"\s+", " ", line).strip()
            if clean:
                lines.append((offset, offset + len(line), clean))
            offset += len(raw_line)

        headings = find_statement_headings(source_text, page=page_number)
        heading_rows = {
            heading.span[0]: next(
                (index for index, (start, _, _) in enumerate(lines) if start == heading.span[0]),
                None,
            )
            for heading in headings
        }
        for heading in headings:
            title = next(
                (index for index, (start, end, _) in enumerate(lines)
                 if start <= heading.span[1] <= end),
                None,
            )
            if title is None:
                continue
            later_titles = [
                row for start, row in heading_rows.items()
                if start > heading.span[0] and row is not None and row > title
            ]
            header_end = min([title + 17, *later_titles])
            header = [line for _, _, line in lines[title + 1:header_end]]
            duration_months = None
            for index, line in enumerate(header):
                numeric_tokens = [item.group() for item in _NUMERIC_DATE.finditer(line)]
                tokens = list(dict.fromkeys((*_date_tokens(line), *numeric_tokens)))
                # Chinese/numeric dates are permitted only in a dates-only
                # header row. A director signature after the table is excluded.
                remainder = line
                for token in tokens:
                    remainder = remainder.replace(token, "")
                dates_only = bool(tokens) and not re.sub(r"[\s,，;；|()（）]+", "", remainder)
                previous = header[index - 1] if index else ""
                year_grid = bool(re.fullmatch(r"(?:year\s+ended\s*)+", previous, re.I))
                # HK bilingual tables print a repeated semantic header followed
                # by "Item 31/12/2025 31/12/2024". Do not allow a generic prose
                # prefix or a signature to turn the same dates into a header.
                numeric_grid = bool(
                    numeric_tokens and year_grid
                    and re.fullmatch(r"\s*Item\s*", remainder, re.I)
                )
                anchor = _ANCHOR.match(line)
                anchored_header = bool(anchor and _bounded_anchor_header(line, anchor, tokens))
                if not (anchored_header or dates_only or numeric_grid):
                    continue
                if anchored_header:
                    duration_match = re.match(
                        r"(?:for\s+the\s+)?(three|six|nine|twelve)\s+months?\s+ended\b", line, re.I,
                    )
                    if duration_match:
                        duration_months = {
                            "three": 3, "six": 6, "nine": 9, "twelve": 12,
                        }[duration_match.group(1).casefold()]
                    elif re.match(r"(?:for\s+the\s+)?years?\s+ended\b", line, re.I):
                        duration_months = 12
                dates = [parsed for token in tokens if (parsed := _parse_date(token)) is not None]
                dates.extend(_numeric_header_dates(line))
                if numeric_grid:
                    duration_months = 12
                excerpt = "\n".join((previous, line)) if numeric_grid else line
                if anchored_header:
                    month_day = _MONTH_DAY.search(line)
                    day_month = _DAY_MONTH.search(line)
                    if month_day or day_month:
                        match = month_day or day_month
                        month_text, day_text = match.groups() if month_day else tuple(reversed(match.groups()))
                        month = next(number for number, name in enumerate(_MONTHS, 1) if name.casefold() == month_text.casefold())
                        # Only a year-list header, before units or values, may
                        # complete an English month/day on the preceding line.
                        year_lines = []
                        for following in header[index + 1:index + 4]:
                            if re.fullmatch(r"(?:19|20)\d{2}(?:\s+(?:19|20)\d{2})*", following):
                                year_lines.append(following)
                            else:
                                break
                        years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ".join([line, *year_lines]))
                        for year in years:
                            try:
                                dates.append(date(int(year), month, int(day_text)))
                            except ValueError:
                                pass
                        excerpt = "\n".join([line, *year_lines])
                for parsed in dict.fromkeys(dates):
                    declaration = StatementPeriodDeclaration(page_number, parsed.isoformat(), excerpt, duration_months)
                    if declaration not in declarations:
                        declarations.append(declaration)
    return tuple(declarations)
