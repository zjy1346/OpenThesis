from __future__ import annotations

from types import SimpleNamespace

from openthesis.domain import FilingDocument
from openthesis.financial_pipeline import _parser_execution_observations
from openthesis.markets import build_company


def _filing():
    company = build_company("000811.SZ", "OBS", market="CN_A")
    return FilingDocument(
        "parser-observations-doc", company.security_id,
        "parser-observations-accession", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-01T00:00:00+00:00", "annual.pdf",
        "https://example.invalid/annual.pdf", content_hash="a" * 64,
    )


def test_parser_execution_observations_preserve_all_approved_local_structure_fields():
    filing = _filing()
    local_structure = {
        "ocr_calls": 2,
        "cache_hits": 1,
        "completed_pages": 4,
        "selected_pages": 5,
        "worker_start_count": 1,
        "model_load_seconds": 1.25,
        "inference_seconds": 2.5,
        "peak_memory_bytes": 2048,
        "admitted_field_count": 3,
        "new_verified_field_count": 2,
        "api_key": "must-not-leak",
        "model_path": r"C:\\private\\model.onnx",
        "future_metric": 9,
    }
    snapshot = SimpleNamespace(filings=(filing,), parsing_observations=())
    engine = SimpleNamespace(_document_execution={
        filing.document_id: {
            "source_hash": filing.content_hash,
            "local_structure": local_structure,
            "unapproved_path": r"C:\\private\\annual.pdf",
        },
    })

    observations = _parser_execution_observations(snapshot, engine)

    assert observations == ({
        "accession_number": filing.accession_number,
        "period_end": filing.period_end,
        "fiscal_period": filing.fiscal_period,
        "source_hash": filing.content_hash,
        "stage_timings": {},
        "local_structure": {
            key: value for key, value in local_structure.items()
            if key in {
                "ocr_calls", "cache_hits", "completed_pages", "selected_pages",
                "worker_start_count", "model_load_seconds", "inference_seconds",
                "peak_memory_bytes", "admitted_field_count", "new_verified_field_count",
            }
        },
    },)


def test_parser_execution_observations_ignore_execution_with_stale_source_hash():
    filing = _filing()
    snapshot = SimpleNamespace(filings=(filing,), parsing_observations=())
    engine = SimpleNamespace(_document_execution={
        filing.document_id: {
            "source_hash": "b" * 64,
            "local_structure": {"ocr_calls": 2},
        },
    })

    assert _parser_execution_observations(snapshot, engine) == ()


def test_parser_execution_observations_ignore_saved_diagnostics_missing_accession():
    snapshot = SimpleNamespace(
        filings=(),
        parsing_observations=(
            {"period_end": "2025-12-31", "source_hash": "a" * 64},
            {
                "accession_number": "saved-accession",
                "period_end": "2025-12-31",
                "fiscal_period": "FY",
                "source_hash": "b" * 64,
            },
        ),
    )

    assert _parser_execution_observations(snapshot, SimpleNamespace()) == ({
        "accession_number": "saved-accession",
        "period_end": "2025-12-31",
        "fiscal_period": "FY",
        "source_hash": "b" * 64,
    },)


def test_parser_execution_observations_tolerate_non_mapping_stage_timings():
    filing = _filing()
    snapshot = SimpleNamespace(filings=(filing,), parsing_observations=())
    engine = SimpleNamespace(_document_execution={
        filing.document_id: {
            "source_hash": filing.content_hash,
            "parse_elapsed_seconds": 3.5,
            "stage_timings": [r"C:\\private\\debug.log"],
        },
    })

    observations = _parser_execution_observations(snapshot, engine)

    assert observations[0]["stage_timings"] == {"parse_elapsed_seconds": 3.5}
