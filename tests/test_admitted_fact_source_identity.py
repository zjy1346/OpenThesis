"""Admitted fact evidence must retain only an exact original-row identity."""

from copy import deepcopy

import pytest

from openthesis.financial_evidence import build_fact_evidence


CONTENT_HASH = "a" * 64
ROW = "Operating revenue 100 CNY"
SOURCE_URL = "https://example.test/annual-2025.pdf"
BBOX = (10.0, 20.0, 300.0, 40.0)


def _fact() -> dict:
    return {
        "fact_id": "ingest:revenue-2025",
        "company_cik": "CN_A:SZSE:002594.SZ",
        "entity": "Fixture Issuer",
        "market": "CN_A",
        "concept": "revenue",
        "value": 100.0,
        "unit": "CNY",
        "currency": "CNY",
        "unit_scale": 1.0,
        "unit_provenance": "structured_normalized",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "form_type": "ANNUAL_REPORT",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "filed_at": "2026-03-01",
        "accession_number": "accession-2025",
        "source_url": SOURCE_URL,
        "source_document": "2025 Annual Report",
        "source_page": 12,
        "source_bbox": BBOX,
        "raw_text": ROW,
    }


def _source_row(**overrides) -> dict:
    row = {
        "evidence_id": "fact:revenue-2025",
        "document_id": "document:annual-2025",
        "source_url": SOURCE_URL,
        "locator": "page:12",
        "excerpt": ROW,
        "content_hash": CONTENT_HASH,
        "bbox": BBOX,
    }
    row.update(overrides)
    return row


def _bound_record(source_rows=()) -> dict:
    return build_fact_evidence([_fact()], source_records=tuple(source_rows))[0]


def test_matching_row_preserves_exact_source_identity_and_locator():
    record = _bound_record([_source_row()])

    assert record["document_id"] == "document:annual-2025"
    assert record["document_sha256"] == CONTENT_HASH
    assert record["source_row_evidence_id"] == "fact:revenue-2025"
    assert record["locator"] == "page:12"
    assert record["page"] == 12
    assert record["source_url"] == SOURCE_URL
    assert record["raw_text"] == ROW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_hash", "not-a-sha256"),
        ("excerpt", "Operating revenue 101 CNY"),
        ("locator", "page:13"),
        ("bbox", (11.0, 20.0, 300.0, 40.0)),
        ("source_url", "https://example.test/other-report.pdf"),
    ],
)
def test_source_identity_mismatch_cannot_gain_a_document_binding(field, value):
    record = _bound_record([_source_row(**{field: value})])

    assert "document_id" not in record
    assert "document_sha256" not in record
    assert "source_row_evidence_id" not in record
    assert "page" not in record


def test_duplicate_rows_from_different_documents_remain_unbound():
    first = _source_row()
    second = _source_row(
        document_id="document:restated-2025",
        content_hash="b" * 64,
    )

    record = _bound_record([first, second])

    assert "document_id" not in record
    assert "document_sha256" not in record
    assert "source_row_evidence_id" not in record


def test_legacy_fact_without_source_snapshot_does_not_invent_identity():
    fact = deepcopy(_fact())
    record = build_fact_evidence([fact])[0]

    assert record["evidence_id"] == "fact:ingest:revenue-2025"
    assert record["source_url"] == SOURCE_URL
    assert record["raw_text"] == ROW
    assert "document_id" not in record
    assert "document_sha256" not in record
    assert "source_row_evidence_id" not in record

