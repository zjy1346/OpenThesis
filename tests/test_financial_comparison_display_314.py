from openthesis.reporting import _interim_comparison_lines


def test_balance_sheet_previous_year_end_is_not_presented_as_h1_yoy():
    row = {"assets": 200, "comparison_bindings": [{"concept": "assets", "status": "available",
        "relation": "previous_year_end", "current_end": "2026-06-30", "current_start": "",
        "prior_end": "2025-12-31", "prior_start": "", "prior_value": 100}]}
    chinese = "\n".join(_interim_comparison_lines(row, "zh-CN", "CNY"))
    english = "\n".join(_interim_comparison_lines(row, "en", "CNY"))
    traditional = "\n".join(_interim_comparison_lines(row, "zh-Hant", "CNY"))
    assert "较上年末" in chinese and "2025-12-31" in chinese
    assert "2025-06-30" not in chinese and "同比" not in chinese
    assert "previous year-end" in english and "¥200" in english
    assert "資產" in traditional and "較上年末" in traditional and "比較口徑" in traditional


def test_unavailable_metric_never_acquires_a_comparison_caption():
    assert _interim_comparison_lines({"comparison_bindings": [
        {"status": "conflict", "concept": "revenue"}]}, "en", "CNY") == []
