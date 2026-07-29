from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .domain import EvidenceRef, FilingDocument
from .sec_client import SecClient


TOPIC_PATTERNS: dict[str, tuple[str, ...]] = {
    "business": (
        r"\bitem\s+1[.\s:-]+business\b",
        r"\bour business\b",
        r"\bbusiness overview\b",
    ),
    "risk_factors": (
        r"\bitem\s+1a[.\s:-]+risk factors\b",
        r"\brisk factors\b",
    ),
    "management_discussion": (
        r"\bitem\s+7[.\s:-]+management.{0,20}discussion",
        r"\bmanagement.{0,20}discussion and analysis\b",
    ),
    "competition": (
        r"\bcompetitive environment\b",
        r"\bcompetition\b",
    ),
    "customers": (
        r"\bcustomer concentration\b",
        r"\bour customers\b",
    ),
    "segments": (
        r"\breportable segments?\b",
        r"\bsegment revenue\b",
    ),
    "capital_allocation": (
        r"\bcapital expenditures?\b",
        r"\bcapital allocation\b",
    ),
    "growth": (
        r"\bgrowth strateg(?:y|ies)\b",
        r"\bgrowth opportunities\b",
        r"\bnew products?\b",
    ),
}


class FilingTableParser(HTMLParser):
    """Extract readable rows from ordinary HTML tables in SEC filings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = []
        elif tag == "tr" and self._table_depth == 1:
            self._current_row = []
        elif tag in {"td", "th"} and self._table_depth == 1:
            self._current_cell = []
        elif tag == "br" and self._current_cell is not None:
            self._current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._current_cell is not None:
            value = _clean_excerpt("".join(self._current_cell))
            if self._current_row is not None:
                self._current_row.append(value)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            if any(self._current_row):
                if self._current_table is not None:
                    self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._table_depth:
            if self._table_depth == 1 and self._current_table:
                self.tables.append(self._current_table)
                self._current_table = None
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)


def _clean_excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_topic_evidence(
    filing: FilingDocument,
    *,
    max_per_topic: int = 2,
    radius: int = 850,
) -> list[EvidenceRef]:
    if not filing.local_path:
        return []
    path = Path(filing.local_path)
    if not path.exists():
        return []
    text = SecClient.extract_filing_text(path)
    evidence: list[EvidenceRef] = []
    for topic, patterns in TOPIC_PATTERNS.items():
        occupied: list[tuple[int, int]] = []
        found = 0
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                start = max(0, match.start() - radius // 4)
                end = min(len(text), match.end() + radius)
                if any(start < old_end and end > old_start for old_start, old_end in occupied):
                    continue
                excerpt = _clean_excerpt(text[start:end])
                if len(excerpt) < 120:
                    continue
                identity = f"{filing.document_id}|{topic}|{match.start()}|{excerpt}"
                evidence_id = f"filing:{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
                evidence.append(
                    EvidenceRef(
                        evidence_id=evidence_id,
                        document_id=filing.document_id,
                        source_url=filing.source_url,
                        title=f"{filing.form_type} {filing.period_end} Â· {topic}",
                        locator=f"character:{start}-{end}",
                        excerpt=excerpt,
                        published_at=filing.filed_at,
                        content_hash=filing.content_hash,
                    )
                )
                occupied.append((start, end))
                found += 1
                if found >= max_per_topic:
                    break
            if found >= max_per_topic:
                break
    return evidence


def extract_table_evidence(
    filing: FilingDocument,
    *,
    maximum_tables: int = 8,
    maximum_rows: int = 14,
    maximum_characters: int = 2600,
) -> list[EvidenceRef]:
    """Convert meaningful filing tables into bounded, traceable evidence."""
    if not filing.local_path:
        return []
    path = Path(filing.local_path)
    if not path.exists():
        return []
    parser = FilingTableParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    evidence: list[EvidenceRef] = []
    for table_index, rows in enumerate(parser.tables, start=1):
        normalized_rows = [
            [cell for cell in row if cell][:12]
            for row in rows[:maximum_rows]
            if any(cell for cell in row)
        ]
        flattened = [cell for row in normalized_rows for cell in row]
        # Layout-only tables are common in filings. Keep only tables with enough
        # data and at least one numeric value.
        if len(flattened) < 4 or not any(re.search(r"\d", cell) for cell in flattened):
            continue
        excerpt = "\n".join(" | ".join(row) for row in normalized_rows)
        excerpt = excerpt[:maximum_characters].strip()
        identity = f"{filing.document_id}|table|{table_index}|{excerpt}"
        evidence.append(
            EvidenceRef(
                evidence_id=f"table:{hashlib.sha256(identity.encode()).hexdigest()[:20]}",
                document_id=filing.document_id,
                source_url=filing.source_url,
                title=f"{filing.form_type} {filing.period_end} Â· è¡¨æ ¼ {table_index}",
                locator=f"table:{table_index}",
                exce}Ó{h‘éì¶»§q«^u•¹ ˆˆ¤((€€€±¥¹•Ì¹•áÑ•¹¡lˆŒŒƒž‚Sž¦Û¢þž¢,ˆ°€ˆ‰t¤(€€€™½È…ÉÑ¥™…Ð¥¸…ÉÑ¥™…ÑÌè(€€€€€€€±¥¹•Ì¹…ÁÁ•¹ (€€€€€€€€€€€˜ˆ´í…ÉÑ¥™…ÑlÑ¥Ñ±”uôƒ
Üí…ÉÑ¥™…Ñl…•¹Ñ}¥uõ€ƒ
Üí…ÉÑ¥™…Ñlµ½‘•±}¥uõ€ˆ(€€€€€€€€¤(€€€±¥¹•Ì¹•áÑ•¹ (€€€€€€€l(€€€€€€€€€€€€ˆˆ°(€€€€€€€€€€€€ˆŒŒƒšZçšÎW¢¾Óšb8ˆ°(€€€€€€€€€€€€ˆˆ°(€€€€€€€€€€€€‹¢Ò‹–*‡šVÃ–óšv—¢«žîOšz–2[’ê/–º{–æÛžRÇž†»–ºkšŸž¢/–ê?¢º‡žº_Žš¢‡–z/žRš"C––ºç–þ¦†ï’â;¢¾š6»Žˆ(€€€€€€€€€€€€‹–¢ºû–J3šr«ž~—¦†ç–2ë–"¾òošržî#š*W¢Ö–"“šZ·žRÇžR£š"ß¢«¢†3’ös–ëŽˆ°(€€€€€€€t(€€€€¤(€€€É•ÑÕÉ¸€‰q¸ˆ¹©½¥¸¡±¥¹•Ì¤(