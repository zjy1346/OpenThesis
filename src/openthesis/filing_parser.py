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
        r"\bprincipal business\b",
        r"\bbusiness review\b",
    ),
    "risk_factors": (
        r"\bitem\s+1a[.\s:-]+risk factors\b",
        r"\brisk factors\b",
        r"\bpotential risks?\b",
        r"\brisks? and uncertainties\b",
        r"\brisks? faced\b",
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
    "audit": (
        r"\breport of independent registered public accounting firm\b",
        r"\breport of (?:the )?independent auditors?\b",
        r"\bindependent auditor.?s? report\b",
        r"\baudit opinion\b",
        r"[审審][计計][报報]告",
        r"[审審][计計]意[见見]",
    ),
}

# Disclosure language belongs to the extraction boundary, not an issuer rule.
TOPIC_PATTERNS['business'] += (r'主[营營][业業][务務]', r'[业業][务務]概[况況]',)
TOPIC_PATTERNS['risk_factors'] += (r'[风風][险險]因素', r'可能面[临臨]的[风風][险險]', r'主要[风風][险險]',)
TOPIC_PATTERNS['management_discussion'] += (r'管理[层層][讨討][论論][与與]分析', r'管理[层層][讨討][论論]及分析', r'[经經][营營]情况[讨討][论論]',)
TOPIC_PATTERNS['competition'] += (r'核心[竞競][争爭]力', r'[竞競][争爭]格局',)
TOPIC_PATTERNS['customers'] += (r'主要客[户戶]', r'客[户戶]集中度',)
TOPIC_PATTERNS['segments'] += (r'分部[资資]料', r'分部[报報]告',)
TOPIC_PATTERNS['capital_allocation'] += (r'[资資]本[开開]支', r'在建工程',)
TOPIC_PATTERNS['growth'] += (r'[发發]展[战戰]略', r'[业業][务務]展望', r'新[产產]品',)


def _disclosure_pages(path: Path):
    """Stream PDF pages, keeping citations tied to the source page."""
    if path.suffix.lower() == '.pdf':
        import pdfplumber
        with pdfplumber.open(path) as document:
            for number, page in enumerate(document.pages, 1):
                try:
                    yield number, page.extract_text() or ''
                finally:
                    page.close()
    else:
        yield None, SecClient.extract_filing_text(path)


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
    evidence: list[EvidenceRef] = []
    counts: dict[str, int] = {}
    for page, text in _disclosure_pages(path):
        refs = _topic_evidence_from_text(filing, text, page, max_per_topic, radius)
        for ref in refs:
            topic = ref.title.split(' · ')[-1]
            if counts.get(topic, 0) < max_per_topic:
                evidence.append(ref)
                counts[topic] = counts.get(topic, 0) + 1
    return evidence


def _topic_evidence_from_text(filing, text, page, max_per_topic, radius):
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
                # Contents lists are navigation, not substantive disclosure.
                if re.search(r'(?:(?:\.\s*){3,}|…{2,}|目\s*[录錄]|table of contents)', excerpt, re.I):
                    continue
                identity = f"{filing.document_id}|{page}|{topic}|{match.start()}|{excerpt}"
                evidence_id = f"filing:{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
                evidence.append(
                    EvidenceRef(
                        evidence_id=evidence_id,
                        document_id=filing.document_id,
                        source_url=filing.source_url,
                        title=f"{filing.form_type} {filing.period_end} · {topic}",
                        locator=f"page:{page};character:{start}-{end}" if page else f"character:{start}-{end}",
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
    if path.suffix.lower() == '.pdf':
        return []  # PDF numeric tables belong to the canonical AST pipeline.
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
                title=f"{filing.form_type} {filing.period_end} · 表格 {table_index}",
                locator=f"table:{table_index}",
                excerpt=excerpt,
                published_at=filing.filed_at,
                content_hash=filing.content_hash,
            )
        )
        if len(evidence) >= maximum_tables:
            break
    return evidence


def build_filing_evidence(
    filings: Iterable[FilingDocument],
    *,
    maximum_filings: int = 2,
) -> list[dict[str, object]]:
    collected: list[dict[str, object]] = []
    ordered = sorted(filings, key=lambda item: item.filed_at, reverse=True)
    # Recent interim releases must not displace the annual business/risk body.
    annual = next((item for item in ordered if item.fiscal_period == 'FY'), None)
    selected = ([annual] if annual else []) + [item for item in ordered if item is not annual]
    for filing in selected[:maximum_filings]:
        try:
            collected.extend(item.to_dict() for item in extract_topic_evidence(filing))
            collected.extend(item.to_dict() for item in extract_table_evidence(filing))
        except Exception as exc:
            # Financial AST validation and narrative extraction are separate
            # capabilities. Retain numeric success while exposing missing text.
            collected.append({'evidence_id': f'material-gap:{filing.document_id}',
                              'kind': 'material_gap', 'document_id': filing.document_id,
                              'reason': f'disclosure_text_unavailable:{type(exc).__name__}'})
    return collected
