"""Strict replayable numeric continuations from positioned statement rows.

Some PDF tables split the final fractional digit onto the immediately
following physical line.  This module recognizes only that narrow shape.  It
does not infer a value from magnitude, repair arbitrary text, or use a
computed value stored in a witness; replay re-runs the same token and geometry
checks against the captured rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Mapping, Sequence


_DECIMAL_TOKEN = re.compile(
    r"^[+-]?(?:(?:\d{1,3}(?:,\d{3})+)|\d+)\.\d$"
)
_DIGIT_FRAGMENT = re.compile(r"^\d$")
_SCHEMA = "numeric-row-continuation-v1"


@dataclass(frozen=True, slots=True)
class _Cell:
    text: str
    bbox: tuple[float, float, float, float]

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def top(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def bottom(self) -> float:
        return self.bbox[3]


@dataclass(frozen=True, slots=True)
class _Row:
    text: str
    bbox: tuple[float, float, float, float]
    cells: tuple[_Cell, ...]


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    values = tuple(value)
    if not all(_finite(item) for item in values):
        return None
    left, top, right, bottom = (float(item) for item in values)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _cell_from(value: Any) -> _Cell | None:
    text = getattr(value, "text", None)
    if not isinstance(text, str):
        return None
    geometry = _bbox(getattr(value, "bbox", None))
    if geometry is None:
        attrs = tuple(getattr(value, name, None) for name in ("x0", "top", "x1", "bottom"))
        geometry = _bbox(attrs)
    if geometry is None:
        return None
    return _Cell(text, geometry)


def _row_from(value: Any) -> _Row | None:
    cells_raw = getattr(value, "cells", None)
    if not isinstance(cells_raw, (tuple, list)):
        return None
    cells = tuple(_cell_from(cell) for cell in cells_raw)
    if not cells or any(cell is None for cell in cells):
        return None
    geometry = _bbox(getattr(value, "bbox", None))
    if geometry is None:
        return None
    text = getattr(value, "text", None)
    if not isinstance(text, str):
        return None
    return _Row(text, geometry, tuple(cell for cell in cells if cell is not None))


def _witness(rows: Sequence[_Row]) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "rows": [
            {
                "text": row.text,
                "bbox": list(row.bbox),
                "cells": [
                    {"text": cell.text, "bbox": list(cell.bbox)}
                    for cell in row.cells
                ],
            }
            for row in rows
        ],
    }


def _parse_decimal(text: str) -> Decimal | None:
    token = text.strip()
    if not _DECIMAL_TOKEN.fullmatch(token):
        return None
    try:
        value = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _first_row_parts(row: _Row) -> tuple[list[tuple[_Cell, Decimal]], list[str]] | None:
    numbers: list[tuple[_Cell, Decimal]] = []
    labels: list[str] = []
    for cell in row.cells:
        text = cell.text.strip()
        value = _parse_decimal(text)
        if value is not None:
            numbers.append((cell, value))
            continue
        # A nonnumeric token containing a digit is an unrecognized numeric
        # token, not a safe label.  This rejects NaN/Infinity-like numeric
        # spellings and period/value mixtures without guessing.
        if any(char.isdigit() for char in text):
            return None
        if text:
            labels.append(text)
    if len(numbers) != 2 or not labels:
        return None
    return numbers, labels


def _second_row_parts(row: _Row) -> list[_Cell] | None:
    if len(row.cells) != 2:
        return None
    fragments: list[_Cell] = []
    for cell in row.cells:
        text = cell.text.strip()
        if _DIGIT_FRAGMENT.fullmatch(text) is None:
            return None
        fragments.append(cell)
    return fragments


def _aligned(
    upper: Sequence[tuple[_Cell, Decimal]], lower: Sequence[_Cell]
) -> tuple[tuple[_Cell, Decimal, _Cell], tuple[_Cell, Decimal, _Cell]] | None:
    # A positioned row is expected to retain the parser's left-to-right cell
    # order.  A reversed lower sequence is a crossed/ambiguous continuation,
    # even if its coordinates could otherwise be sorted into two columns.
    if tuple(item[0].x0 for item in upper) != tuple(
        sorted(item[0].x0 for item in upper)
    ):
        return None
    if tuple(cell.x0 for cell in lower) != tuple(sorted(cell.x0 for cell in lower)):
        return None
    upper_sorted = tuple(sorted(upper, key=lambda item: item[0].x0))
    lower_sorted = tuple(sorted(lower, key=lambda item: item.x0))
    if upper_sorted[0][0].x1 >= upper_sorted[1][0].x0:
        return None
    if lower_sorted[0].x1 >= lower_sorted[1].x0:
        return None
    aligned: list[tuple[_Cell, Decimal, _Cell]] = []
    for (upper_cell, value), lower_cell in zip(upper_sorted, lower_sorted):
        if abs(lower_cell.x1 - upper_cell.x1) > 2.0:
            return None
        center = (lower_cell.x0 + lower_cell.x1) / 2.0
        if not upper_cell.x0 <= center <= upper_cell.x1:
            return None
        aligned.append((upper_cell, value, lower_cell))
    if len({item[2].x1 for item in aligned}) != 2:
        return None
    return aligned[0], aligned[1]


def _join_amount(token: str, fragment: str) -> str | None:
    value = f"{token.strip()}{fragment.strip()}"
    try:
        decimal = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None
    return value if decimal.is_finite() else None


def _compute_pair(first: _Row, second: _Row) -> dict[str, Any] | None:
    for row in (first, second):
        # The printable witness and the positioned tokens must describe the
        # same row. Otherwise a changed token could acquire the old quote.
        if (" ".join(row.text.split()) != " ".join(" ".join(cell.text for cell in row.cells).split())
                or any(not (row.bbox[0] <= cell.x0 < cell.x1 <= row.bbox[2]
                            and row.bbox[1] <= cell.top < cell.bottom <= row.bbox[3])
                       for cell in row.cells)):
            return None
    first_parts = _first_row_parts(first)
    second_parts = _second_row_parts(second)
    if first_parts is None or second_parts is None:
        return None

    gap = second.bbox[1] - first.bbox[3]
    first_height = first.bbox[3] - first.bbox[1]
    # ``gap`` guards physical adjacency against the upper row's height.  The
    # overall line spacing is measured between row tops; using the lower row's
    # bottom would reject ordinary rows with different text heights even when
    # their line spacing remains within the 20pt bound.
    row_spacing = second.bbox[1] - first.bbox[1]
    if gap < 0 or gap > first_height or row_spacing > 20.0:
        return None
    matched = _aligned(first_parts[0], second_parts)
    if matched is None:
        return None
    left, right = matched
    current = _join_amount(left[0].text, left[2].text)
    prior = _join_amount(right[0].text, right[2].text)
    if current is None or prior is None:
        return None
    return {
        "label": " ".join(first_parts[1]),
        "current": current,
        "prior": prior,
        "quote": f"{first.text}\n{second.text}",
        "witness": _witness((first, second)),
    }


def _rows_from_witness(witness: Mapping[str, Any]) -> tuple[_Row, _Row] | None:
    if witness.get("schema") != _SCHEMA:
        return None
    rows = witness.get("rows")
    if not isinstance(rows, list) or len(rows) != 2:
        return None
    decoded: list[_Row] = []
    for item in rows:
        if not isinstance(item, Mapping):
            return None
        text = item.get("text")
        geometry = _bbox(item.get("bbox"))
        cells_value = item.get("cells")
        if not isinstance(text, str) or geometry is None or not isinstance(cells_value, list):
            return None
        cells: list[_Cell] = []
        for cell in cells_value:
            if not isinstance(cell, Mapping) or not isinstance(cell.get("text"), str):
                return None
            cell_geometry = _bbox(cell.get("bbox"))
            if cell_geometry is None:
                return None
            cells.append(_Cell(cell["text"], cell_geometry))
        if not cells:
            return None
        decoded.append(_Row(text, geometry, tuple(cells)))
    return decoded[0], decoded[1]


def replay_wrapped_pair(witness: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recompute one continuation from its captured rows, or return ``None``."""

    if not isinstance(witness, Mapping):
        return None
    rows = _rows_from_witness(witness)
    return _compute_pair(*rows) if rows is not None else None


def collect_wrapped_pairs(positioned_rows: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Collect only adjacent, two-column one-digit numeric continuations."""

    if not isinstance(positioned_rows, (tuple, list)):
        return ()
    rows = tuple(_row_from(item) for item in positioned_rows)
    if any(row is None for row in rows):
        return ()
    valid_rows = tuple(row for row in rows if row is not None)
    results: list[dict[str, Any]] = []
    for index in range(len(valid_rows) - 1):
        first, second = valid_rows[index], valid_rows[index + 1]
        candidate = _compute_pair(first, second)
        if candidate is None:
            continue
        replayed = replay_wrapped_pair(candidate["witness"])
        if replayed is not None:
            results.append(replayed)
    return tuple(results)


__all__ = ["collect_wrapped_pairs", "replay_wrapped_pair"]
