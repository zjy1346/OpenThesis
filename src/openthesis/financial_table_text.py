"""Pure table/column observations shared by cloud collection and validation.

HTML is parsed by the standard library, never executed. Markdown and plain
rows retain their cells; a note number is not a financial amount. This module
does not fetch sources, choose providers or decide research readiness.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
import re

from .financial_contracts import STATEMENT_FOR, source_numeric_token
from .financial_statement_identity import classify_statement_title, STATEMENT_TITLE_MARKERS
from .financial_taxonomy import (
    FINANCIAL_LABEL_ALIASES, balance_component_row_concept,
    capital_allocation_payment_concept, comprehensive_income_row_concept,
    equity_row_concept, has_distinct_capital_payment_label,
    normalize_financial_label, profit_row_concept, capex_component_kind,
)
from .financial_unit_declarations import literal_money_unit

CLOUD_TABLE_VERSION = "vision-markdown-v4"
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_EXTRA_ALIASES = {
    "revenue": ("营业收入", "营业收入合计", "revenue", "revenues", "total revenue", "operating revenue"),
    "total_operating_revenue": ("营业总收入", "total operating revenue"),
    "assets": ("资产总计", "资产合计", "total assets"),
    "liabilities": ("负债合计", "负债总计", "total liabilities"),
    "share_repurchase": ("repurchase of ordinary shares", "repurchase of own shares", "purchase of treasury shares", "share buyback", "回购本公司股份", "回购股份", "购回股份"),
    "shareholder_dividends_paid": ("dividends paid to shareholders", "dividends paid to equity holders", "向股东支付股利", "向股东支付股息"),
}


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(text: str) -> str:
    parser = _PlainText()
    parser.feed(text)
    return "".join(parser.parts)


class _HTMLTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.current = None
        self.cell = None
        self.spans = (1, 1)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            if self.current is not None:
                raise ValueError("CLOUD_TABLE_NESTED_ROW")
            self.current = []
        elif tag in {"td", "th"} and self.current is not None:
            if self.cell is not None:
                raise ValueError("CLOUD_TABLE_NESTED_CELL")
            attributes = dict(attrs)
            self.spans = tuple(int(attributes.get(key, "1")) for key in ("rowspan", "colspan"))
            if not all(1 <= item <= 32 for item in self.spans):
                raise ValueError("CLOUD_TABLE_SPAN_INVALID")
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.current.append((" ".join("".join(self.cell).split()), *self.spans))
            self.cell = None
        elif tag == "tr" and self.current is not None:
            if self.cell is not None:
                raise ValueError("CLOUD_TABLE_UNCLOSED_CELL")
            self.rows.append(self.current)
            self.current = None

    def grid(self):
        if self.cell is not None or self.current is not None or len(self.rows) > 5000:
            raise ValueError("CLOUD_TABLE_INVALID")
        occupied = {}
        for row_number, row in enumerate(self.rows):
            column = 0
            for text, height, width in row:
                while (row_number, column) in occupied:
                    column += 1
                if column + width > 64 or row_number + height > len(self.rows):
                    raise ValueError("CLOUD_TABLE_SPAN_OUTSIDE")
                for r in range(row_number, row_number + height):
                    for c in range(column, column + width):
                        if (r, c) in occupied:
                            raise ValueError("CLOUD_TABLE_OVERLAPPING_SPAN")
                        occupied[r, c] = text
                column += width
        width = max((column + 1 for _, column in occupied), default=0)
        return tuple(tuple(occupied.get((r, c), "") for c in range(width)) for r in range(len(self.rows)))


@dataclass(frozen=True, slots=True)
class TextTable:
    source: str
    context: str
    rows: tuple[tuple[str, ...], ...]
    plain: bool = False


def _pipe_cells(line):
    line = line.strip().strip("|")
    return tuple(" ".join(plain_text(cell.replace(r"\|", "|")).split())
                 for cell in re.split(r"(?<!\\)\|", line))


def read_text_tables(markdown: str) -> tuple[TextTable, ...]:
    if len(markdown.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("CLOUD_MARKDOWN_SIZE_LIMIT")
    tables = []
    # Work in source order so later parent-only titles cannot contaminate an
    # earlier consolidated table, and vice versa.
    position = 0
    for match in re.finditer(r"(?is)<table\b[^>]*>.*?</table>", markdown):
        tables.extend(_read_non_html(markdown[position:match.start()], markdown[:position]))
        parser = _HTMLTable()
        parser.feed(match.group())
        tables.append(TextTable(match.group(), plain_text(markdown[:match.start()]), parser.grid()))
        position = match.end()
    tables.extend(_read_non_html(markdown[position:], markdown[:position]))
    return tuple(tables)


def _read_non_html(text, prefix):
    lines = text.splitlines(keepends=True)
    result, offset, index, fenced = [], 0, 0, False
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and "|" in line and len(_pipe_cells(line)) >= 2:
            start, rows = index, []
            while index < len(lines) and "|" in lines[index]:
                cells = _pipe_cells(lines[index])
                if not all(re.fullmatch(r"\s*:?-{2,}:?\s*", item) for item in cells):
                    rows.append(cells)
                index += 1
            raw = "".join(lines[start:index])
            result.append(TextTable(raw, plain_text(prefix + text[:offset]), tuple(rows)))
            offset += len(raw)
            continue
        if not fenced:
            match = re.fullmatch(r"\s*(.*?)\s+((?:\(?[+-]?\d[\d,.]*\)?\s*)+)\s*", line)
            if match and match[1] and not _YEAR.fullmatch(match[1].strip()):
                values = tuple(item.group() for item in _NUMBER.finditer(match[2]))
                if concept_for_label(match[1]):
                    result.append(TextTable(line.rstrip(), plain_text(prefix + text[:offset]),
                                            ((match[1].strip(), *values),), True))
        offset += len(line)
        index += 1
    return result


def concept_for_label(label: str, *, statement="", following_labels=()) -> str | None:
    source = re.sub(r"^\s*[一二三四五六七八九十百0-9]+[、.．)）]\s*", "", label)
    normalized = normalize_financial_label(source)
    if not normalized or any(item in normalized for item in ("pershare", "每股", "earningsper", "margin", "利润率", "增长率")):
        return None
    if statement == "comprehensive_income":
        return comprehensive_income_row_concept(source)
    if "comprehensive" in normalized or "综合收益" in normalized:
        return None
    profit = profit_row_concept(source)
    if profit:
        return profit
    equity = equity_row_concept(source, following_labels=following_labels)
    if equity:
        return equity
    component = balance_component_row_concept(source)
    if component:
        return component
    payment = capital_allocation_payment_concept(source)
    if payment:
        return payment
    if has_distinct_capital_payment_label(source):
        return None
    # Exact complete labels, apart from a printed enumeration and a trailing
    # loss/sign explanation. Prose and subtotals cannot borrow an alias.
    clean = re.sub(r"[（(](?:亏损以.*|损失以.*|loss.*)[）)]$", "", source).strip()
    key = normalize_financial_label(clean)
    matches = []
    for concept, aliases in {**FINANCIAL_LABEL_ALIASES, **_EXTRA_ALIASES}.items():
        if concept in {"net_income", "parent_net_income", "minority_interest_profit", "ordinary_shareholder_net_income", "equity", "total_equity", "comprehensive_income", "parent_comprehensive_income", "minority_interest_comprehensive_income"}:
            continue
        if any(key == normalize_financial_label(alias) for alias in aliases):
            matches.append(concept)
    if len(matches) == 1:
        concept = matches[0]
        if concept == "capital_expenditure":
            kind = capex_component_kind(source)
            return {"ppe": "capital_expenditure_ppe", "intangibles": "capital_expenditure_intangible"}.get(kind, concept)
        return concept
    return None


def table_statement(context: str) -> tuple[str, str] | None:
    result = None
    for line in context.splitlines():
        cleaned = re.sub(r"^\s*[#>*]+\s*", "", line).strip()
        markers = {**STATEMENT_TITLE_MARKERS, "income_statement": (
            *STATEMENT_TITLE_MARKERS["income_statement"], "consolidated statement of operations",
            "consolidated statements of operations", "consolidated statement of profit or loss",
            "consolidated statements of profit or loss",)}
        formal = classify_statement_title(cleaned, markers=markers)
        if formal is None:
            # The shared search normalization covers Traditional Chinese;
            # only an entire formal title may use this typography fallback.
            canonical = normalize_financial_label(cleaned)
            canonical = re.sub(r"(?:续|continued|continuation)$", "", canonical)
            for kind, titles in markers.items():
                for title in titles:
                    key = normalize_financial_label(title)
                    if canonical == key:
                        formal = (kind, "consolidated" if any(item in canonical for item in ("合并", "consolidated")) else "parent")
                    elif canonical == "母公司" + key:
                        formal = (kind, "parent")
        if formal:
            result = formal
        elif normalize_financial_label(cleaned) in {"consolidatedfinancialstatements", "合并财务报表"}:
            result = ("", "consolidated")
        elif normalize_financial_label(cleaned) in {"parentcompanyfinancialstatements", "母公司财务报表"}:
            result = ("", "parent")
    return result


def _table_formal_context(table):
    context = table.context
    for row in table.rows:
        if row and concept_for_label(row[0]):
            break
        context += "\n" + "\n".join(row)
    return context


def explicit_table_unit(table: TextTable):
    # Local unit lines belong to the current statement, not a unit anywhere
    # earlier in the Markdown. Headers can carry a unit in each year column.
    context = table.context
    lines = context.splitlines()
    boundary = 0
    for index, line in enumerate(lines):
        if table_statement(line):
            boundary = index
    declarations = []
    for text in (*lines[boundary:], *[" ".join(row) for row in table.rows[:4]]):
        if re.search(r"(?i)unit|currency|单位|單位|币种|幣種|RMB|CNY|USD|HKD|人民币|人民幣", text):
            unit = literal_money_unit(text)
            if len(unit.currencies) > 1 or len(unit.scales) > 1:
                raise ValueError("CLOUD_LOCAL_UNIT_CONFLICT")
            if unit.currencies or unit.scales:
                scale = next(iter(unit.scales), "")
                if re.match(r"(?i)^\s*(?:currency|币种|幣種)\s*[:：]", text) and scale == "1":
                    scale = ""
                declarations.append((text, next(iter(unit.currencies), ""), format(Decimal(scale), "f") if scale else ""))
    currencies = {currency for _, currency, _ in declarations if currency}
    scales = {scale for _, _, scale in declarations if scale}
    if len(currencies) > 1 or len(scales) > 1:
        raise ValueError("CLOUD_LOCAL_UNIT_CONFLICT")
    if not declarations:
        return None
    return ("\n".join(item[0] for item in declarations), next(iter(currencies), ""), next(iter(scales), ""))


@dataclass(frozen=True, slots=True)
class CellObservation:
    table_index: int
    row_index: int
    column_index: int
    concept: str
    label: str
    raw_value: str
    column_text: str
    period_end: str
    revision: str
    statement: str


def observe_cells(tables: tuple[TextTable, ...], period_end: str, fiscal_period: str) -> tuple[CellObservation, ...]:
    end = date.fromisoformat(period_end)
    observations = []
    for table_index, table in enumerate(tables):
        formal = table_statement(_table_formal_context(table))
        if formal is None or formal[1] != "consolidated" or formal[0] == "equity_changes":
            continue
        statement = formal[0]
        headers = {}
        note_columns = set()
        body_started = False
        for row_index, row in enumerate(table.rows):
            if not row:
                continue
            # Layout engines often put a printed enumeration and its label in
            # separate leading columns, with a blank first column. Read the
            # full label region up to the amount columns; note numbers are not
            # part of the label and never become a period amount.
            label_end = min(headers) if headers else 1
            def row_label(cells):
                return " ".join(cell for column, cell in enumerate(cells[:label_end])
                    if column not in note_columns and cell.strip()
                    and not _NUMBER.fullmatch(source_numeric_token(cell)))
            label = row_label(row)
            following = tuple(row_label(item) for item in table.rows[row_index + 1:row_index + 4] if item)
            concept = concept_for_label(label, statement=statement, following_labels=following)
            if concept is None:
                if not body_started and not any(concept_for_label(cell, statement=statement) for cell in row):
                    for column, cell in enumerate(row):
                        years = _YEAR.findall(cell)
                        if column > 0 and len(years) == 1:
                            headers[column] = (int(years[0]), cell)
                        if re.search(r"(?i)note|附注|附註", cell):
                            note_columns.add(column)
                continue
            expected = STATEMENT_FOR.get(concept)
            body_started = True
            if not expected or statement and expected != statement:
                continue
            selected_headers = headers.copy()
            if table.plain:
                context_years, period_line = [], ""
                context_lines = table.context.splitlines()
                last_title = max((index for index, line in enumerate(context_lines) if table_statement(line)), default=0)
                for line in context_lines[last_title:]:
                    if concept_for_label(re.sub(r"\s+[\d,(].*$", "", line)):
                        continue
                    years = _YEAR.findall(line)
                    if years and (len(years) > 1 or re.search(r"(?i)year|ended|截至|年度|\d{4}年|\d{4}-\d{2}-\d{2}", line)):
                        context_years, period_line = years, line
                # Single-value plain rows still need an observed current-year
                # header anywhere in their current statement's context.
                if len(row) == 2 and context_years == [str(end.year)]:
                    selected_headers = {1: (end.year, period_line)}
                elif len(context_years) == len(row) - 1:
                    selected_headers = {index + 1: (int(year), year) for index, year in enumerate(context_years)}
                elif len(context_years) == len(row) - 2 and re.search(r"(?i)note|附注|附註", period_line):
                    selected_headers = {index + 2: (int(year), year) for index, year in enumerate(context_years)}
            if not selected_headers:
                continue
            for column, (year, header) in selected_headers.items():
                if column >= len(row) or not end.year - 1 <= year <= end.year:
                    continue
                raw_value = row[column].strip()
                if not _NUMBER.fullmatch(source_numeric_token(raw_value)):
                    continue
                # Full date headers must agree with this fiscal interval's
                # ending month/day. Same-year interim vs annual is not a year.
                dates = re.findall(r"(?:19|20)\d{2}[-/年](\d{1,2})[-/月](\d{1,2})日?", header)
                if dates and any((int(month), int(day)) != (end.month, end.day) for month, day in dates):
                    continue
                try:
                    observed_end = end.replace(year=year).isoformat()
                except ValueError:
                    continue
                revision = "restated" if any(item in normalize_financial_label(header) for item in ("restated", "经重述", "重列")) else "original"
                observations.append(CellObservation(table_index, row_index, column, concept, label,
                    raw_value, header, observed_end, revision, expected))
    return tuple(observations)
