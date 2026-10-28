from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from openthesis.financial_sources.numeric_continuations import (
    collect_wrapped_pairs,
    replay_wrapped_pair,
)


@dataclass(frozen=True)
class Cell:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def bbox(self):
        return (self.x0, self.top, self.x1, self.bottom)


@dataclass(frozen=True)
class Row:
    cells: tuple[Cell, ...]
    top: float
    bbox: tuple[float, float, float, float]

    @property
    def text(self):
        return " ".join(cell.text for cell in self.cells)


def _rows(
    current: str = "667,448,757.2",
    prior: str = "674,392,657.6",
    current_tail: str = "3",
    prior_tail: str = "6",
    *,
    gap: float = 4,
    label: str = "归属于母公司股东的净利润",
    tail_extra: str | None = None,
) -> tuple[Row, Row]:
    first = Row(
        (
            Cell(label, 10, 100, 180, 112),
            Cell(current, 220, 100, 340, 112),
            Cell(prior, 400, 100, 520, 112),
        ),
        100,
        (10, 100, 520, 112),
    )
    cells = (
        Cell(current_tail, 328, 116, 340, 124),
        Cell(prior_tail, 508, 116, 520, 124),
    )
    if tail_extra is not None:
        cells += (Cell(tail_extra, 530, 116, 550, 124),)
    second_top = 112 + gap
    second = Row(cells, second_top, (328, second_top, 550 if tail_extra else 520, second_top + 8))
    return first, second


def test_collects_real_two_column_decimal_continuation_and_replays_witness():
    result = collect_wrapped_pairs(_rows())

    assert len(result) == 1
    pair = result[0]
    assert pair["label"] == "归属于母公司股东的净利润"
    assert pair["current"] == "667,448,757.23"
    assert pair["prior"] == "674,392,657.66"
    assert pair["quote"] == (
        "归属于母公司股东的净利润 667,448,757.2 674,392,657.6\n3 6"
    )
    assert pair["witness"]["schema"] == "numeric-row-continuation-v1"
    json.dumps(pair["witness"], ensure_ascii=False)
    assert replay_wrapped_pair(pair["witness"]) == pair


@pytest.mark.parametrize(
    "rows",
    [
        _rows(current="667,448,757", prior="674,392,657"),
        _rows(current="667,448,757.20", prior="674,392,657.60"),
        _rows(current="NaN", prior="674,392,657.6"),
        _rows(current_tail="", prior_tail="6"),
        _rows(tail_extra="note"),
    ],
)
def test_rejects_non_decimal_nonfinite_missing_or_extra_tokens(rows):
    assert collect_wrapped_pairs(rows) == ()


def test_rejects_crossed_or_ambiguous_column_alignment():
    first, second = _rows()
    crossed = Row(
        (
            Cell("3", 508, 116, 520, 124),
            Cell("6", 328, 116, 340, 124),
        ),
        116,
        (328, 116, 520, 124),
    )
    assert collect_wrapped_pairs((first, crossed)) == ()

    ambiguous = Row(
        (
            Cell("3", 328, 116, 340, 124),
            Cell("6", 338, 116, 340, 124),
        ),
        116,
        (328, 116, 519, 124),
    )
    assert collect_wrapped_pairs((first, ambiguous)) == ()


@pytest.mark.parametrize("gap", [-1, 13, 21])
def test_rejects_non_adjacent_or_excessive_vertical_gap(gap):
    first, second = _rows(gap=gap)
    if gap == 13:
        middle = Row((Cell("continued", 10, 116, 90, 124),), 116, (10, 116, 90, 124))
        assert collect_wrapped_pairs((first, middle, second)) == ()
    else:
        assert collect_wrapped_pairs((first, second)) == ()


def test_replay_rejects_tampered_geometry_or_tokens():
    pair = collect_wrapped_pairs(_rows())[0]
    witness = pair["witness"]
    tampered = {
        **witness,
        "rows": [
            witness["rows"][0],
            {
                **witness["rows"][1],
                "cells": [
                    {**witness["rows"][1]["cells"][0], "text": "9"},
                    witness["rows"][1]["cells"][1],
                ],
            },
        ],
    }
    replayed = replay_wrapped_pair(tampered)
    assert replayed is None
    assert replay_wrapped_pair({**witness, "schema": "other"}) is None


@pytest.mark.parametrize("value", [True, "328", float("inf"), float("nan"), -500])
def test_replay_rejects_nonphysical_or_outside_row_coordinates(value):
    witness = json.loads(json.dumps(collect_wrapped_pairs(_rows())[0]["witness"]))
    witness["rows"][1]["cells"][0]["bbox"][0] = value
    assert replay_wrapped_pair(witness) is None
