"""Pure formal-title semantics shared by extraction and financial admission."""
from __future__ import annotations

import re
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .financial_taxonomy import normalize_financial_label


STATEMENT_TITLE_MARKERS = {
    "comprehensive_income": (
        "合并综合收益表", "合并綜合收益表", "綜合收益表", "综合收益表", "合併綜合全面收益表", "綜合全面收益表",
        "consolidated statement of comprehensive income", "statement of comprehensive income",
    ),
    "equity_changes": (
        "合并所有者权益变动表", "合併所有者權益變動表", "所有者权益变动表", "所有者權益變動表",
        "合併權益變動表", "權益變動表", "consolidated statement of changes in equity", "statement of changes in equity",
    ),
    "balance_sheet": (
        "合并资产负债表", "合併資產負債表", "资产负债表", "資產負債表",
        "consolidated balance sheet", "consolidated balance sheets", "balance sheet", "balance sheets",
        "consolidated statement of financial position", "statement of financial position",
    ),
    "income_statement": (
        "合并利润表", "合併利潤表", "合併損益表", "綜合損益表", "損益表", "利润表", "利潤表",
        "consolidated income statement", "consolidated income statements", "income statement", "income statements",
        "statement of profit or loss", "statement of operations", "statements of operations",
        "statement of income", "statements of income",
    ),
    "cash_flow": (
        "合并现金流量表", "合併現金流量表", "现金流量表", "現金流量表",
        "consolidated cash flow statement", "cash flow statement", "statement of cash flow",
        "consolidated statement of cash flows", "consolidated statements of cash flows", "statement of cash flows",
    ),
}


@dataclass(frozen=True, slots=True)
class StatementHeading:
    """A formal statement title bound to its original page text."""

    kind: str
    scope: str
    raw_text: str
    normalized_title: str
    page: int
    span: tuple[int, int]


_ORDINAL_PREFIX = re.compile(
    r"^\s*(?:"
    r"\d+(?:\.\d+)+(?:[.)、．])?|\d+[.)、．)]|"
    r"[（(][0-9一二三四五六七八九十百零〇ivxlcdm]+[）)]|"
    r"[ivxlcdm]+[.)、．)]"
    r")\s*",
    re.I,
)


def statement_title_evidence_id(document_id: str, page: int, bbox: tuple[float, float, float, float], text: str) -> str:
    return "statement:" + hashlib.sha256(
        f"{document_id}|{page}|{bbox}|{text}".encode("utf-8")
    ).hexdigest()[:24]


def is_narrative_title_tail(candidate: str, preceding: str) -> bool:
    """Reject a wrapped sentence tail, without excluding numbered headings."""
    line = candidate.strip()
    previous = preceding.strip()
    return bool(
        line and line[0].islower() and line.endswith(".")
        and len(previous) >= 35 and previous[-1:].isalpha()
    )


def classify_statement_title(
    text: str,
    *,
    markers: Mapping[str, Sequence[str]] | None = None,
    consolidated_aliases: Sequence[str] = (),
    parent_aliases: Sequence[str] = (),
) -> tuple[str, str] | None:
    """Recognize a formal title, never a narrative mention of a statement.

    Compatibility aliases are supplied by the extraction adapter; this module
    has no source I/O, adapter, compiler, or compatibility-store dependency.
    """
    headings = find_statement_headings(
        text,
        markers=markers,
        consolidated_aliases=consolidated_aliases,
        parent_aliases=parent_aliases,
    )
    if not headings:
        return None
    heading = max(headings, key=lambda item: item.span[0])
    return heading.kind, heading.scope


def find_statement_headings(
    text: str,
    *,
    page: int = 1,
    markers: Mapping[str, Sequence[str]] | None = None,
    consolidated_aliases: Sequence[str] = (),
    parent_aliases: Sequence[str] = (),
) -> tuple[StatementHeading, ...]:
    """Discover numbered or wrapped formal statement headings in page text.

    ``span`` uses Python string offsets into the exact input, and ``raw_text``
    is the corresponding source slice. The ordinal is removed only from
    ``normalized_title``; source evidence is never rewritten.
    """
    markers_by_statement = markers if markers is not None else STATEMENT_TITLE_MARKERS
    physical_lines: list[tuple[int, int, str]] = []
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        physical_lines.append((offset, offset + len(line), line))
        offset += len(raw_line)
    if text and not physical_lines:
        physical_lines.append((0, len(text), text))

    scope_aliases = (
        "合并", "合併", "consolidated", "母公司", "parent", "company only", "company-only", "separate",
        *consolidated_aliases, *parent_aliases,
    )

    def clean_title(candidate: str) -> str:
        collapsed = re.sub(r"\s+", " ", candidate).strip()
        return _ORDINAL_PREFIX.sub("", collapsed, count=1).strip()

    def classify(candidate: str) -> tuple[str, str, str] | None:
        clean = clean_title(candidate)
        folded = clean.casefold()
        if len(clean) > 160 or "不是" in clean or "说明" in clean:
            return None
        for statement, title_markers in markers_by_statement.items():
            for marker in title_markers:
                marker_folded = marker.casefold()
                marker_at = folded.find(marker_folded)
                if marker_at < 0:
                    continue
                prefix = folded[:marker_at].strip()
                if prefix and not re.fullmatch(
                    r"(?:20\d{2}\s+annual\s+report)?", prefix,
                ) and prefix not in {
                    "母公司", "parent", "parent company", "company only", "company-only", "separate",
                    *(alias.casefold().strip() for alias in parent_aliases),
                }:
                    continue
                suffix = folded[marker_at + len(marker_folded):].strip()
                has_scope_suffix = (
                    any(normalize_financial_label(alias) == normalize_financial_label(suffix) for alias in scope_aliases)
                    or re.fullmatch(
                        r"of\s+(?:the\s+)?(?:company\s+as\s+(?:the\s+)?parent|parent\s+company)[.：:。]*", suffix,
                    ) is not None
                )
                if suffix and not (
                    re.fullmatch(r"(?:[\s:：,，.。()（）\[\]{}\-–—]|续|續|continued|continuation|cont[.'’_-]*d)*", suffix)
                    or re.match(r"(?:单位|编制单位|unit|for\s+the\s+year|as\s+of|year\s+ended)", suffix)
                    or has_scope_suffix
                ):
                    continue
                scope = "consolidated" if any(
                    alias.casefold() in folded for alias in ("合并", "合併", "consolidated", *consolidated_aliases)
                ) else "parent"
                return statement, scope, clean
        return None

    headings: dict[tuple[int, int, str, str], StatementHeading] = {}
    preceding = ""
    for index, (start, end, first_line) in enumerate(physical_lines):
        previous_line = preceding
        if first_line.strip():
            preceding = first_line
        if is_narrative_title_tail(first_line, previous_line):
            continue
        candidates = [(end, first_line.strip())]
        if index + 1 < len(physical_lines):
            next_start, next_end, next_line = physical_lines[index + 1]
            line_break = "\r\n" if text[end:end + 2] == "\r\n" else text[end:end + 1]
            if line_break in {"\n", "\r\n", "\r"} and next_start == end + len(line_break):
                first_clean = first_line.strip()
                next_clean = next_line.strip()
                if first_clean and next_clean and len(first_clean) <= 75 and len(next_clean) <= 75:
                    # Only extend a title when the first physical line is not
                    # already a complete formal heading.
                    if classify(first_clean) is None:
                        candidates.append((next_end, f"{first_clean} {next_clean}"))
        for candidate_end, semantic_candidate in candidates:
            identified = classify(semantic_candidate)
            if identified is None:
                continue
            kind, scope, normalized = identified
            span = (start, candidate_end)
            heading = StatementHeading(kind, scope, text[start:candidate_end], normalized, page, span)
            headings[(span[0], span[1], kind, scope)] = heading
    return tuple(sorted(headings.values(), key=lambda item: (item.span[0], item.span[1])))
