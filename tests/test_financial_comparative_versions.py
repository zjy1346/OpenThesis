from openthesis.financial_comparatives import comparative_amounts_agree
from openthesis.financials import calculate_metrics, calculate_interim_metrics


def fact(concept, value, year, *, comparator=False, lineage=None):
    return {"fact_id": f"{concept}-{year}-{'comparison' if comparator else 'original'}",
            "concept": concept, "value": value, "fiscal_year": year, "fiscal_period": "FY",
            "end_date": f"{year}-12-31", "filed_at": f"{year + 1 + comparator}-03-01",
            "usage_status": "comparator" if comparator else "research", "currency": "CNY",
            "accession_number": f"fy-{year + comparator}", "source_page": 1,
            "source_bbox": [1, 2, 3, 4], "raw_text": "Original printed amount",
            "unit_provenance": "declared", "normalization_lineage": lineage}


def lineage(raw, scale):
    from decimal import Decimal
    return {"raw_value": raw, "scale": scale, "normalized_value": str(Decimal(raw) * Decimal(scale)),
            "quantity_kind": "money", "value_transform": "identity", "unit_evidence_ids": ["unit:source"], "inputs": []}


def test_original_amounts_not_replaced_and_unaffected_growth_stays_available():
    facts = [fact("revenue", 150, 2025), fact("revenue", 100, 2024), fact("revenue", 120, 2024, comparator=True),
             fact("net_income", 20, 2025), fact("net_income", 10, 2024)]
    metrics = calculate_metrics(facts)
    latest = next(row for row in metrics if row["year"] == 2025)
    assert latest["revenue_growth"] is None
    assert latest["net_income_growth"] == 1.0
    assert next(row for row in metrics if row["year"] == 2024)["revenue"] == 100
    assert {item["value"] for item in latest["comparison_versions"]["revenue"]} == {100, 120}


def test_display_rounding_requires_source_precision_proof():
    fine = fact("revenue", 1234567.12, 2024, lineage=lineage("1234567.12", "1"))
    coarse = fact("revenue", 1230000, 2024, comparator=True, lineage=lineage("123", "10000"))
    assert comparative_amounts_agree(fine, coarse)
    assert not comparative_amounts_agree({**fine, "normalization_lineage": None}, coarse)
    assert not comparative_amounts_agree(fine, {**coarse, "value": 1240000, "normalization_lineage": lineage("124", "10000")} )
    assert not comparative_amounts_agree(fine, {**fine, "value": 1234567.13, "normalization_lineage": lineage("1234567.13", "1")})


def test_comparator_from_unrelated_later_filing_cannot_override_current_baseline():
    current = fact("revenue", 150, 2025)
    historical = fact("revenue", 100, 2024)
    wrong_filing = {**fact("revenue", 120, 2024, comparator=True), "accession_number": "fy-2026"}
    latest = next(row for row in calculate_metrics([current, historical, wrong_filing]) if row["year"] == 2025)
    assert latest["revenue_growth"] == 0.5
    assert latest["comparison_versions"] == {}


def test_unresolved_opening_equity_does_not_silently_fall_back_to_closing_proxy():
    facts = [fact("parent_net_income", 20, 2025), fact("equity", 200, 2025),
             fact("equity", 100, 2024), fact("equity", 120, 2024, comparator=True)]
    latest = next(row for row in calculate_metrics(facts) if row["year"] == 2025)
    assert latest["return_on_equity"] is None
    assert latest["return_on_equity_gap"] == "unresolved_restatement"


def test_parallel_versions_are_visible_in_all_report_languages():
    from openthesis.report_document import assemble_report_document, render_markdown
    facts = [fact("revenue", 150, 2025), fact("revenue", 100, 2024),
             fact("revenue", 120, 2024, comparator=True)]
    metrics = calculate_metrics(facts)
    artifacts = [{"artifact_id": "numbers", "artifact_type": "deterministic-financial-summary",
                  "content": {"metrics": metrics, "currency": "CNY"}}]
    for language, title in (("zh-CN", "官方版本差异"), ("zh-TW", "官方版本差異"),
                            ("en", "Official source versions")):
        body = render_markdown(assemble_report_document("run", artifacts, language=language))
        assert title in body
        assert "CNY 100.00" in body and "CNY 120.00" in body
        assert "fy-2024" in body and "fy-2025" in body
