import pytest
from openthesis.research_numeric_assertions import numeric_prose_supported

SOURCES = [{"kind": "filing_text", "evidence_id": "filing:2026h1", "fiscal_year": 2026,
    "fiscal_period": "H1", "raw_text": "本报告未经审计。"},
    {"kind": "filing_text", "evidence_id": "filing:2025", "fiscal_year": 2025,
     "fiscal_period": "FY", "raw_text": "审计意见：标准无保留。"}]


@pytest.mark.parametrize("text", ["FY2025年度审计意见为标准无保留；2026 H1报告未经审计。",
    "H1 2026报告未经审计。", "2026H1报告未经审计。"])
def test_source_bound_interim_labels_are_not_currency_amounts(text):
    assert numeric_prose_supported(text, SOURCES), "numeric precheck must allow period-only prose to reach semantic verification"


@pytest.mark.parametrize("text", ["2026 H1收入为999亿元。", "Revenue CNY 2026 H1.",
    "2027 H1报告未经审计。", "2026 H2报告未经审计。"])
def test_period_recognition_never_exempts_money_or_unknown_source_period(text):
    assert not numeric_prose_supported(text, SOURCES)


def test_local_numeric_gap_is_recorded_separately_from_a_missing_semantic_verdict():
    from openthesis.research import _supported_research_fields
    text = "Revenue may depend on reported CNY 999 billion."
    records = {source["evidence_id"]: source for source in SOURCES}
    safe, audit = _supported_research_fields({"business_model": {"analysis": text,
        "evidence_ids": list(records)}}, ["business_model"], set(records), records, "en")
    assert safe["business_model"]["analysis"] == text
    assert audit["local_numeric_gaps"]
    assert audit["nodes"][0]["issue_code"] == "CONTENT_NUMERIC_PREMISE_UNVERIFIED"
    assert audit["nodes"][0]["validation_state"] == "partial"
