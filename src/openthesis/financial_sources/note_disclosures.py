"""Independent note rows with explicit scope, annual period and note-only units.

This adapter does not repair formal statement metadata. It contributes another
originally disclosed fact, retaining all scope/period/unit source locations.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re
from types import SimpleNamespace

from ..domain import Company, EvidenceRef, FilingDocument, FinancialFact
from ..financial_contracts import QuantityKind, SourceAmount, STATEMENT_FOR
from ..financial_unit_declarations import literal_money_unit
from ..text_normalization import canonical_search_text
from .document_units import NoteUnitRegion, note_chapter_number, note_chapter_scope

NOTE_DISCLOSURE_VERSION = "financial-scoped-note-disclosure-v4"
NOTE_DISCLOSURE_VERSIONS = frozenset({"financial-scoped-note-disclosure-v1", "financial-scoped-note-disclosure-v2",
    "financial-scoped-note-disclosure-v3", NOTE_DISCLOSURE_VERSION})
_LABELS = {"净利润": "net_income", "经营活动产生的现金流量净额": "operating_cash_flow",
    "营业收入": "revenue", "营业成本": "cost_of_revenue", "利润总额": "profit_before_tax", "资产总计": "assets",
    "负债合计": "liabilities", "所有者权益合计": "total_equity"}
_NUMBER = r"(?:[-+−]?\d[\d,]*(?:\.\d+)?|\(\s*\d[\d,]*(?:\.\d+)?\s*\))"
_ROW = re.compile(r"^\s*(?P<label>" + "|".join(_LABELS) + r")\s+"
    r"(?P<current>" + _NUMBER + r")\s+(?P<prior>" + _NUMBER + r")\s*$")
_ANNUAL = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年\s*(?:年度报告|度报告)")
_REVENUE_CAPTION = re.compile(r"^\s*(?:[（(][一二三四五六七八九十百\d]+[）)]|\d+[、.．])\s*营业收入[和、]营业成本\s*$")
_REVENUE_DETAIL = re.compile(r"^\s*(?:[（(]1[）)]|1[.．])\s*营业收入[和、]营业成本情况\s*$")
_REVENUE_TOTAL = re.compile(r"^\s*合计\s+(?P<revenue>" + _NUMBER + r")\s+(?P<cost>" + _NUMBER
    + r")\s+(?P<prior_revenue>" + _NUMBER + r")\s+(?P<prior_cost>" + _NUMBER + r")\s*$")
_NOTE_TOTAL_LAYOUT_SCHEMA = "note-total-row-layout-v1"
_REVENUE_COLUMN_LABELS = {
    ("收入", "成本", "收入", "成本"),
    ("营业收入", "营业成本", "营业收入", "营业成本"),
}


def _note_content_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()
            and not _ANNUAL.search(line) and not re.fullmatch(r"\s*\d{1,4}\s*", line)]


def _layout_row(row):
    return {
        "text": row.text,
        "bbox": list(row.bbox),
        "top": float(row.top),
        "cells": [
            {"text": cell.text, "bbox": [cell.x0, cell.top, cell.x1, cell.bottom]}
            for cell in row.cells
        ],
    }


def _positioned_revenue_total(positioned_rows, period_header: str, columns_header: str):
    """Bind a split visual total row to its four explicit revenue/cost columns."""
    rows = tuple(positioned_rows or ())
    if not rows:
        return None
    header_rows = []
    for index, row in enumerate(rows):
        labels = tuple(canonical_search_text(cell.text) for cell in row.cells)
        if labels in _REVENUE_COLUMN_LABELS:
            header_rows.append((index, row))
    if not header_rows:
        return None
    header_index, header_row = header_rows[0]
    next_header = next((index for index, _row in header_rows[1:]), len(rows))
    header_cells = tuple(sorted(header_row.cells, key=lambda cell: cell.x0))
    if any(left.x1 >= right.x0 for left, right in zip(header_cells, header_cells[1:])):
        return None

    def amount_cells(row):
        values = []
        labels = []
        for cell in row.cells:
            token = cell.text.strip()
            if re.fullmatch(_NUMBER, token):
                values.append(cell)
            elif token:
                labels.append(canonical_search_text(token))
        return values, labels

    for index in range(header_index + 1, next_header - 1):
        first, second = rows[index], rows[index + 1]
        first_values, first_labels = amount_cells(first)
        second_values, second_labels = amount_cells(second)
        if tuple(first_labels) != ("合计",) or len(first_values) != 3 or second_labels or len(second_values) != 1:
            continue
        if not 0 < float(second.top) - float(first.top) <= 12.0:
            continue
        ordered = tuple(sorted((*first_values, *second_values), key=lambda cell: cell.x0))
        if any(left.x0 >= right.x0 for left, right in zip(ordered, ordered[1:])):
            continue
        aligned = all(
            header.x0 - 3.0 <= (cell.x0 + cell.x1) / 2.0 <= header.x1 + 3.0
            for cell, header in zip(ordered, header_cells)
        )
        if not aligned:
            continue
        quote = f"{first.text}\n{second.text}"
        witness = {
            "schema": _NOTE_TOTAL_LAYOUT_SCHEMA,
            "period_header": period_header,
            "columns_header": columns_header,
            "rows": [_layout_row(header_row), _layout_row(first), _layout_row(second)],
        }
        return {
            "amounts": tuple(cell.text.strip() for cell in ordered),
            "quote": quote,
            "witness": witness,
        }
    return None


def _replay_total_layout(witness):
    if (not isinstance(witness, dict)
            or set(witness) != {"schema", "period_header", "columns_header", "rows"}
            or witness.get("schema") != _NOTE_TOTAL_LAYOUT_SCHEMA
            or not isinstance(witness.get("period_header"), str)
            or not isinstance(witness.get("columns_header"), str)
            or not isinstance(witness.get("rows"), list)
            or len(witness["rows"]) != 3):
        return None
    decoded = []
    for item in witness["rows"]:
        if (not isinstance(item, dict) or set(item) != {"text", "bbox", "top", "cells"}
                or not isinstance(item.get("text"), str)
                or not isinstance(item.get("bbox"), list) or len(item["bbox"]) != 4
                or not isinstance(item.get("cells"), list)):
            return None
        try:
            bbox = tuple(float(value) for value in item["bbox"])
            top = float(item["top"])
            if any(not value == value or abs(value) == float("inf") for value in (*bbox, top)):
                return None
            cells = []
            for cell in item["cells"]:
                if (not isinstance(cell, dict) or set(cell) != {"text", "bbox"}
                        or not isinstance(cell.get("text"), str)
                        or not isinstance(cell.get("bbox"), list) or len(cell["bbox"]) != 4):
                    return None
                x0, cell_top, x1, bottom = (float(value) for value in cell["bbox"])
                if (any(not value == value or abs(value) == float("inf")
                        for value in (x0, cell_top, x1, bottom))
                        or x0 >= x1 or cell_top >= bottom):
                    return None
                cells.append(SimpleNamespace(
                    text=cell["text"], x0=x0, top=cell_top, x1=x1, bottom=bottom,
                ))
            if not cells:
                return None
            decoded.append(SimpleNamespace(
                text=item["text"], bbox=bbox, top=top, cells=tuple(cells),
            ))
        except (TypeError, ValueError, OverflowError):
            return None
    replayed = _positioned_revenue_total(
        decoded, witness["period_header"], witness["columns_header"],
    )
    if replayed is None or replayed["witness"] != witness:
        return None
    return replayed, tuple(decoded)


def _revenue_note_rows(text: str, continuation_text: str = "", positioned_rows=()):
    """One captioned total with two observed header levels, never a subtotal.

    A continuation needs the preceding original's terminal caption, not a
    nearby total in an arbitrary later table.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    captions = [index for index, line in enumerate(lines) if _REVENUE_CAPTION.fullmatch(line)]
    if continuation_text:
        previous = _note_content_lines(continuation_text)
        current = _note_content_lines(text)
        if (captions or len(previous) < 2 or not _REVENUE_CAPTION.fullmatch(previous[-2])
                or not _REVENUE_DETAIL.fullmatch(previous[-1])):
            return ()
        # Parser tokens from two separate verbatim witnesses; neither source
        # excerpt nor any persisted candidate is rewritten/concatenated.
        lines = [*previous[-2:], *current]
        captions = [0]
    if len(captions) != 1:
        return ()
    start = captions[0] + 1
    if start >= len(lines) or not _REVENUE_DETAIL.fullmatch(lines[start]):
        return ()
    if start + 2 >= len(lines):
        return ()
    period_header = lines[start + 1]
    normalized_period = canonical_search_text(period_header)
    if normalized_period in {"项目本期发生额上期发生额", "项目本期金额上期金额"}:
        columns_index = start + 2
    elif normalized_period in {"本期发生额上期发生额", "本期金额上期金额"}:
        columns_index = start + 2
        if columns_index < len(lines) and canonical_search_text(lines[columns_index]) == "项目":
            # This nonstandard order occurs in the original source under test;
            # accept it only when positioned cells can bind the next header
            # and split total back to their visual columns.
            if not positioned_rows:
                return ()
            columns_index += 1
    else:
        return ()
    if columns_index >= len(lines):
        return ()
    columns = lines[columns_index]
    if (canonical_search_text(columns) not in {"收入成本收入成本", "营业收入营业成本营业收入营业成本"}
    ):
        return ()
    column_identity = period_header + "\n" + columns
    for index, line in enumerate(lines[columns_index + 1:], columns_index + 1):
        # The next detail/section caption terminates the total table even if
        # another table happens to repeat the same column labels.
        if re.match(r"^\s*(?:[（(][一二三四五六七八九十百\d]+[）)]|\d+[、.．])", line):
            break
        row = _REVENUE_TOTAL.fullmatch(line)
        if row:
            return (("营业收入", row["revenue"], line, column_identity),
                    ("营业成本", row["cost"], line, column_identity))
        if canonical_search_text(line.split(maxsplit=1)[0] if line.split() else "") == "合计":
            positioned = _positioned_revenue_total(positioned_rows, period_header, columns)
            if positioned is not None and positioned["quote"].splitlines()[0].strip() == line:
                amounts = positioned["amounts"]
                return (("营业收入", amounts[0], positioned["quote"], column_identity),
                        ("营业成本", amounts[1], positioned["quote"], column_identity))
    return ()


def note_row_disclosures(text: str, year: int, continuation_text: str = "", *, wrapped_pairs=(), positioned_rows=()):
    """Two clearly labelled columns only; never choose a nearby monetary row."""
    if set(_ANNUAL.findall(text)) != {str(year)}:
        return ()
    if continuation_text:
        if set(_ANNUAL.findall(continuation_text)) != {str(year)}:
            return ()
        return _revenue_note_rows(text, continuation_text, positioned_rows)
    active_header, result, cash_reconciliation = "", [], False
    lines = text.splitlines()
    for index, line in enumerate(lines):
        compact = canonical_search_text(line)
        if re.fullmatch(r"[一二三四五六七八九十百\d]*现金流量表?补充资料", compact):
            cash_reconciliation = True
            active_header = ""
            continue
        if re.match(r"^\s*(?:[（(][一二三四五六七八九十百]+[）)]|[一二三四五六七八九十百]+、)", line):
            cash_reconciliation = False
            active_header = ""
        if compact in {"项目本期发生额上期发生额", "项目本期金额上期金额"}:
            active_header = line.strip()
            continue
        # A different table header terminates the association. In particular,
        # tax adjustments and balance/FX tables cannot inherit period columns.
        if compact.startswith("项目") and compact != "项目":
            active_header = ""
        row = _ROW.fullmatch(line)
        if row and active_header and cash_reconciliation and _LABELS[row["label"]] in {"net_income", "operating_cash_flow"}:
            # A dangling digit row is part of the original amount, not an
            # independently rounded value. Without positioned column proof do
            # not emit a deceptively complete prefix (and never add tolerance).
            fragment = index + 1 < len(lines) and re.fullmatch(r"\s*\d\s+\d\s*", lines[index + 1])
            clipped = fragment and any(re.fullmatch(r"[-+−]?[\d,]+\.\d", row[key]) for key in ("current", "prior"))
            if clipped:
                quote = line.strip() + "\n" + lines[index + 1].strip()
                matches = [pair for pair in wrapped_pairs
                    if re.sub(r"\s+", "", pair["quote"]) == re.sub(r"\s+", "", quote)
                    and canonical_search_text(pair["label"]) == canonical_search_text(row["label"])]
                if len(matches) == 1:
                    result.append((row["label"], matches[0]["current"], quote, active_header))
                continue
            result.append((row["label"], row["current"], line.strip(), active_header))
    return (*result, *_revenue_note_rows(text, positioned_rows=positioned_rows))


def _ref_id(role: str, document: str, page: int, excerpt: str) -> str:
    return f"note-{role}:" + hashlib.sha256(f"{document}|{page}|{excerpt}".encode()).hexdigest()[:24]


def extract_note_candidates(company: Company, filing: FilingDocument, page: int, text: str,
                            bbox: tuple[float, float, float, float], region: NoteUnitRegion | None,
                            *, continuation: tuple[int, str] | None = None, positioned_rows=()):
    if (region is None or not region.start_page <= page < region.end_page
            or filing.fiscal_period != "FY" or not re.fullmatch(r"(?:19|20)\d{2}-12-31", filing.period_end or "")
            or not filing.content_hash or not company.name
            or canonical_search_text(company.name) not in canonical_search_text(text)):
        return [], []
    year = int(filing.period_end[:4])
    prior_page, prior_text = continuation if continuation else (0, "")
    if continuation and (prior_page != page - 1 or prior_page < region.start_page
            or canonical_search_text(company.name) not in canonical_search_text(prior_text)
            or _local_unit_conflicts(prior_text, region.declaration.currency, region.declaration.scale)):
        return [], []
    from .numeric_continuations import collect_wrapped_pairs
    wrapped = collect_wrapped_pairs(positioned_rows)
    rows = note_row_disclosures(
        text, year, prior_text, wrapped_pairs=wrapped, positioned_rows=positioned_rows,
    )
    if not rows:
        return [], []
    if _local_unit_conflicts(text, region.declaration.currency, region.declaration.scale):
        return [], []
    context_id = _ref_id("context", filing.document_id, page, text)
    unit_id = _ref_id("unit", filing.document_id, region.declaration.page, region.declaration.excerpt)
    scope_id = _ref_id("scope", filing.document_id, region.start_page, region.heading)
    period_id = _ref_id("period", filing.document_id, region.period_page, region.period_excerpt)
    boundary_id = _ref_id("boundary", filing.document_id, region.end_page, region.end_heading)
    common = [EvidenceRef(key, filing.document_id, filing.source_url, title, f"page:{source_page}",
        excerpt, filing.filed_at, filing.content_hash, bound_box) for key, title, source_page, excerpt, bound_box in (
            (context_id, "original scoped note table", page, text, bbox),
            (unit_id, "original note-only unit", region.declaration.page, region.declaration.excerpt, None),
            (scope_id, "original note scope heading", region.start_page, region.heading, None),
            (period_id, "original calendar accounting period", region.period_page, region.period_excerpt, None),
            (boundary_id, "original next note chapter boundary", region.end_page, region.end_heading, None))]
    continuation_ids = ()
    if continuation:
        prior_id = _ref_id("continuation", filing.document_id, prior_page, prior_text)
        common.append(EvidenceRef(prior_id, filing.document_id, filing.source_url,
            "original adjacent note caption", f"page:{prior_page}", prior_text,
            filing.filed_at, filing.content_hash, None))
        continuation_ids = (prior_id,)
    facts, refs = [], []
    for label, raw, row, header in rows:
        layout_ids = ()
        layouts = [pair for pair in wrapped
                   if re.sub(r"\s+", "", pair["quote"]) == re.sub(r"\s+", "", row)]
        total_layout = None
        if "\n" in row and "\n" in header:
            period_header, columns_header = header.split("\n", 1)
            total_layout = _positioned_revenue_total(positioned_rows, period_header, columns_header)
            if total_layout is not None and total_layout["quote"] == row:
                layouts = []
                payload = json.dumps(total_layout["witness"], ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"), allow_nan=False)
                layout_id = _ref_id("layout", filing.document_id, page, payload)
                common.append(EvidenceRef(layout_id, filing.document_id, filing.source_url,
                    "original total-row column alignment", f"page:{page}", payload,
                    filing.filed_at, filing.content_hash, bbox))
                layout_ids = (layout_id,)
        if len(layouts) == 1:
            payload = json.dumps(layouts[0]["witness"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            layout_id = _ref_id("layout", filing.document_id, page, payload)
            common.append(EvidenceRef(layout_id, filing.document_id, filing.source_url,
                "original numeric continuation coordinates", f"page:{page}", payload,
                filing.filed_at, filing.content_hash, bbox))
            layout_ids = (layout_id,)
        token = raw.replace(",", "").replace(" ", "").replace("−", "-")
        if token.startswith("("):
            token = "-" + token[1:-1]
        identity_text = f"{filing.document_id}|{page}|{row}|{region.scope}"
        if label in {"营业收入", "营业成本"}:
            # Two different concepts share the same printed total row.
            identity_text += "|" + label
        identity = hashlib.sha256(identity_text.encode()).hexdigest()[:24]
        lineage = SourceAmount(token, region.declaration.scale, QuantityKind.MONEY,
                               region.declaration.currency, (unit_id,)).normalize()
        concept = _LABELS[label]
        start = f"{year}-01-01" if STATEMENT_FOR[concept] != "balance_sheet" else None
        facts.append(FinancialFact(f"note:{identity}", company.security_id, concept, label,
            float(Decimal(lineage.normalized_value)), region.declaration.currency, year, "FY", filing.form_type,
            start, filing.period_end, filing.filed_at, filing.accession_number, filing.source_url,
            scope=region.scope, entity=company.name, market=company.market, statement=STATEMENT_FOR[concept],
            period_start=start, consolidated_scope=region.scope, currency=region.declaration.currency,
            unit_scale=float(Decimal(region.declaration.scale)), unit_provenance="scoped_note_explicit",
            revision=filing.revision, source_document=filing.primary_document, source_page=page,
            source_bbox=bbox, source_column=header, raw_text=row, parser_version=NOTE_DISCLOSURE_VERSION,
            normalization_lineage=lineage, classification_evidence_ids=(context_id, scope_id, period_id, boundary_id, *continuation_ids, *layout_ids)))
        refs.append(EvidenceRef(f"fact:note:{identity}", filing.document_id, filing.source_url,
            "independent disclosed note amount", f"page:{page}", row, filing.filed_at, filing.content_hash, bbox))
    return facts, [*refs, *common]


def _local_unit_conflicts(text: str, currency: str, scale: str) -> bool:
    for line in text.splitlines():
        if not re.match(r"^\s*(?:单位|币种|unit|currency)\s*[:：]", line, re.I):
            continue
        literal = literal_money_unit(line)
        if ((literal.currencies and literal.currencies != {currency})
                or (literal.scale_explicit and literal.scales != {scale})):
            return True
    return False


def note_source_issue(fact: FinancialFact, row: EvidenceRef | None, evidence: dict[str, EvidenceRef]) -> str | None:
    if fact.parser_version not in NOTE_DISCLOSURE_VERSIONS:
        return None
    lineage = fact.normalization_lineage
    if row is None or lineage is None or len(fact.classification_evidence_ids) not in {4, 5, 6} or len(lineage.unit_evidence_ids) != 1:
        return "note_source_proof_missing"
    context, scope, period, boundary = (evidence.get(key) for key in fact.classification_evidence_ids[:4])
    extras = fact.classification_evidence_ids[4:]
    continuations = [evidence.get(key) for key in extras if key.startswith("note-continuation:")]
    layouts = [evidence.get(key) for key in extras if key.startswith("note-layout:")]
    if len(continuations) > 1 or len(layouts) > 1 or len(extras) != len(continuations) + len(layouts):
        return "note_source_proof_missing"
    continuation = continuations[0] if continuations else None
    unit = evidence.get(lineage.unit_evidence_ids[0])
    if any(ref is None for ref in (context, scope, period, boundary, unit)):
        return "note_source_proof_missing"
    for role, ref in (("context", context), ("scope", scope), ("period", period), ("boundary", boundary), ("unit", unit)):
        match = re.fullmatch(r"page:(\d+)", ref.locator)
        if (match is None or not re.fullmatch(r"[0-9a-fA-F]{64}", ref.content_hash or "")
                or ref.document_id != row.document_id or ref.source_url != row.source_url
                or ref.content_hash != row.content_hash
                or ref.evidence_id != _ref_id(role, ref.document_id, int(match[1]), ref.excerpt)):
            return "note_source_binding_mismatch"
    page, scope_page = int(context.locator[5:]), int(scope.locator[5:])
    if continuations:
        if (fact.parser_version not in {"financial-scoped-note-disclosure-v3", NOTE_DISCLOSURE_VERSION} or continuation is None
                or continuation.document_id != row.document_id or continuation.source_url != row.source_url
                or continuation.content_hash != row.content_hash or continuation.locator != f"page:{page - 1}"
                or continuation.evidence_id != _ref_id("continuation", row.document_id, page - 1, continuation.excerpt)
                or page - 1 < scope_page
                or not fact.entity or canonical_search_text(fact.entity) not in canonical_search_text(continuation.excerpt)
                or _local_unit_conflicts(continuation.excerpt, fact.currency, lineage.scale)):
            return "note_continuation_identity_unproven"
    expected_scope = note_chapter_scope(scope.excerpt)
    literal = literal_money_unit(unit.excerpt)
    wrapped = ()
    positioned_rows = ()
    if layouts:
        from .numeric_continuations import replay_wrapped_pair
        layout = layouts[0]
        if (fact.parser_version != NOTE_DISCLOSURE_VERSION or layout is None
                or layout.document_id != row.document_id or layout.source_url != row.source_url
                or layout.content_hash != row.content_hash or layout.locator != row.locator
                or layout.bbox != context.bbox
                or len(layout.excerpt) > 16000
                or layout.evidence_id != _ref_id("layout", row.document_id, page, layout.excerpt)):
            return "note_numeric_continuation_unproven"
        try:
            payload = json.loads(layout.excerpt)
            if payload.get("schema") == _NOTE_TOTAL_LAYOUT_SCHEMA:
                replayed = _replay_total_layout(payload)
                if replayed is None:
                    return "note_numeric_continuation_unproven"
                _total, positioned_rows = replayed
                if any(not (context.bbox[0] <= cell.x0 < cell.x1 <= context.bbox[2]
                        and context.bbox[1] <= cell.top < cell.bottom <= context.bbox[3])
                        for line in positioned_rows for cell in line.cells):
                    return "note_numeric_continuation_unproven"
            else:
                pair = replay_wrapped_pair(payload)
                if pair is None or any(not (context.bbox[0] <= cell["bbox"][0] < cell["bbox"][2] <= context.bbox[2]
                        and context.bbox[1] <= cell["bbox"][1] < cell["bbox"][3] <= context.bbox[3])
                        for line in payload["rows"] for cell in line["cells"]):
                    return "note_numeric_continuation_unproven"
                wrapped = (pair,)
        except (ValueError, KeyError, TypeError, IndexError):
            return "note_numeric_continuation_unproven"
    row_in_context = row.excerpt in context.excerpt or (wrapped and
        re.sub(r"\s+", "", row.excerpt) in re.sub(r"\s+", "", context.excerpt))
    if (expected_scope is None
            or "本附注" not in canonical_search_text(unit.excerpt)
            or not literal.proves(fact.currency, lineage.scale)
            or not re.search(r"会计年度.*1月1日.*12月31日", re.sub(r"\s+", "", period.excerpt))
            or int(unit.locator[5:]) > scope_page or int(period.locator[5:]) > scope_page or scope_page > page
            or not re.fullmatch(r"[一二三四五六七八九十百]+[、.．].+", boundary.excerpt.strip())
            or note_chapter_number(boundary.excerpt) != note_chapter_number(scope.excerpt) + 1
            or page >= int(boundary.locator[5:])
            or row.locator != context.locator or row.bbox != context.bbox or row.bbox != fact.source_bbox
            or fact.source_page != page or row.excerpt != fact.raw_text or not row_in_context
            or _local_unit_conflicts(context.excerpt, fact.currency, lineage.scale)
            or not fact.entity or canonical_search_text(fact.entity) not in canonical_search_text(context.excerpt)):
        return "note_scope_period_unit_unproven"
    matching = [item for item in note_row_disclosures(
        context.excerpt, fact.fiscal_year,
        continuation.excerpt if continuation else "",
        wrapped_pairs=wrapped, positioned_rows=positioned_rows,
    )
                if item[0] == fact.reported_concept and item[2] == fact.raw_text and item[3] == fact.source_column]
    if len(matching) != 1:
        return "note_column_identity_unproven"
    label, raw, _, _ = matching[0]
    token = raw.replace(",", "").replace(" ", "").replace("−", "-")
    if token.startswith("("):
        token = "-" + token[1:-1]
    expected = SourceAmount(token, lineage.scale, QuantityKind.MONEY, fact.currency,
                            tuple(lineage.unit_evidence_ids)).normalize()
    start = f"{fact.fiscal_year}-01-01" if STATEMENT_FOR[_LABELS[label]] != "balance_sheet" else None
    if (fact.concept != _LABELS[label] or fact.reported_concept != label or lineage != expected
            or fact.fiscal_period != "FY" or fact.end_date != f"{fact.fiscal_year}-12-31"
            or fact.scope != expected_scope or fact.consolidated_scope != expected_scope
            or fact.start_date != start or fact.period_start != start or fact.unit != fact.currency
            or fact.statement != STATEMENT_FOR[fact.concept] or fact.unit_scale != float(Decimal(lineage.scale))
            or fact.unit_provenance != "scoped_note_explicit"):
        return "note_fact_identity_or_amount_mismatch"
    return None
