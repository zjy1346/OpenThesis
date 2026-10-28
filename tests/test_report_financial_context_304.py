"""Regression coverage for reports with financial quality but no metric table."""

from openthesis.report_document import assemble_report_document, render_markdown


def _financial_artifact(*, metrics):
    return {
        "artifact_type": "deterministic-financial-summary",
        "artifact_id": "financial-304",
        "run_id": "run-304",
        "content": {
            "currency": "CNY",
            "metrics": metrics,
            "evidence": [
                {
                    "evidence_id": "fact:financial-304",
                    "document_id": "filing-304",
                    "source_url": "https://example.test/filing-304.pdf",
                    "locator": "page:42/table:income/row:revenue",
                    "title": "FY2024 annual report",
                }
            ],
            "financial_quality": {
                "financial_analysis": {
                    "summary": "The accepted financial evidence remains available for review."
                },
                "period_coverage": {
                    "displayed_annual_years": [],
                    "missing_or_rejected_years": [{"year": 2024, "reason_code": "REJECTED"}],
                },
            },
        },
    }


def test_financial_quality_without_metric_nodes_keeps_real_quality_section() -> None:
    document = assemble_report_document(
        "run-304",
        [_financial_artifact(metrics=[])],
        language="en",
    )

    quality = next(section for section in document.sections if section.section_id == "financial-data-quality")
    assert "accepted financial evidence" in quality.body
    assert quality.evidence_ids == ("fact:financial-304",)
    assert "accepted financial evidence" in render_markdown(document)
    assert not any(item.code == "section_projection_failed" and item.location == "report" for item in document.delivery_diagnostics)


def test_bad_metric_does_not_hide_valid_financial_quality_or_normal_evidence_links() -> None:
    document = assemble_report_document(
        "run-304",
        [_financial_artifact(metrics=[
            {"period": "not-a-fiscal-period"},
            {"year": 2024, "period": "FY", "revenue": 1200},
        ])],
        language="en",
    )

    overview = next(section for section in document.sections if section.section_id == "financial-overview")
    quality = next(section for section in document.sections if section.section_id == "financial-data-quality")
    assert overview.evidence_ids == ("fact:financial-304",)
    assert quality.evidence_ids == overview.evidence_ids
    assert "accepted financial evidence" in quality.body

