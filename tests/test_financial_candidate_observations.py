from __future__ import annotations

from dataclasses import replace
import json

import pytest

from openthesis.domain import FilingDocument, FinancialFact
from openthesis.financial_contracts import FactValidationRecord, SourceComponentObservation
from openthesis.markets import build_company
from openthesis.storage import Storage, StorageCorruptionError


def _candidate_fixture():
    company = build_company("000811.SZ", "OBS", market="CN_A")
    filing = FilingDocument(
        "candidate-observations-doc", company.security_id,
        "candidate-observations-accession", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-01T00:00:00+00:00", "annual.pdf",
        "https://example.invalid/annual.pdf", content_hash="a" * 64,
    )
    fact = FinancialFact(
        "candidate-observations-fact", company.security_id, "revenue", "Revenue",
        123.0, "CNY", 2025, "FY", filing.form_type, "2025-01-01",
        filing.period_end, filing.filed_at, filing.accession_number,
        filing.source_url, currency="CNY", source_page=4, raw_text="Revenue 123",
    )
    record = FactValidationRecord(
        fact.fact_id,
        False,
        ("normalization_unit_declaration_mismatch",),
        (
            ("amount", SourceComponentObservation("observed", "123", ("row:revenue",))),
            ("currency", SourceComponentObservation("unknown", None, ())),
        ),
        (("currency_declaration", ("unit:revenue",)),),
    ).to_dict()
    observation = {
        "accession_number": filing.accession_number,
        "period_end": filing.period_end,
        "fiscal_period": filing.fiscal_period,
        "source_hash": filing.content_hash,
    }
    return company, filing, fact, record, observation


def test_quarantined_fact_retains_candidate_observations_after_restart(tmp_path):
    company, filing, fact, record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])

    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-observations-generation",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        fact_validation_records={fact.fact_id: record},
        parsing_observations=[observation],
    )

    assert storage.get_facts(company.security_id) == []
    assert storage.get_financial_research_snapshot(company.security_id)["parsing_observations"] == [
        observation
    ]
    audit_fact = next(
        item for item in storage.get_facts_audit(company.security_id)
        if item["fact_id"] == fact.fact_id
    )
    assert audit_fact["fact_validation_record"] == record

    reopened = Storage(tmp_path)
    assert reopened.get_facts(company.security_id) == []
    assert reopened.get_financial_research_snapshot(company.security_id)["parsing_observations"] == [
        observation
    ]
    reopened_audit = next(
        item for item in reopened.get_facts_audit(company.security_id)
        if item["fact_id"] == fact.fact_id
    )
    assert reopened_audit["fact_validation_record"] == record


def test_stale_candidate_keeps_generation_scoped_record_and_cannot_replace_observation(tmp_path):
    company, filing, fact, first_record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])

    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-observations-active",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        fact_validation_records={fact.fact_id: first_record},
        parsing_observations=[observation],
    )
    stale_fact = replace(fact, generation_id="candidate-observations-stale")
    stale_record = {
        **first_record,
        "issues": ["stale_candidate_must_not_replace_active"],
    }
    stale_result = storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [stale_fact],
        generation_id="candidate-observations-stale",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        expected_generations={filing.accession_number: "not-the-active-generation"},
        fact_validation_records={fact.fact_id: stale_record},
        parsing_observations=[observation],
    )

    assert stale_result["state"] == "stale_generation"
    assert storage.get_financial_research_snapshot(company.security_id)["parsing_observations"] == [
        observation
    ]
    audit_rows = [
        item for item in storage.get_facts_audit(company.security_id)
        if item["fact_id"] == fact.fact_id
    ]
    by_generation = {item["generation_id"]: item for item in audit_rows}
    assert by_generation["candidate-observations-active"]["fact_validation_record"] == first_record
    assert by_generation["candidate-observations-stale"]["fact_validation_record"] == stale_record
    assert storage.get_facts(company.security_id) == []


@pytest.mark.parametrize("stale_field", ["content_hash", "period_end", "fiscal_period"])
def test_changed_current_filing_identity_hides_old_parse_observation(tmp_path, stale_field):
    company, filing, fact, record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-observations-old-source",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        fact_validation_records={fact.fact_id: record},
        parsing_observations=[observation],
    )

    if stale_field == "content_hash":
        current_filing = replace(filing, content_hash="b" * 64)
    elif stale_field == "period_end":
        current_filing = replace(filing, period_end="2024-12-31")
    else:
        current_filing = replace(filing, fiscal_period="Q1")
    storage.save_filings([current_filing])

    assert storage.get_financial_research_snapshot(company.security_id)["parsing_observations"] == []
    if stale_field == "content_hash":
        assert storage.get_facts(company.security_id) == []


def test_parse_observations_are_isolated_by_company_even_for_same_accession_and_hash(tmp_path):
    company, filing, fact, record, observation = _candidate_fixture()
    other_company = build_company("000858.SZ", "OTHER", market="CN_A")
    other_filing = replace(
        filing, document_id="other-company-doc", company_cik=other_company.security_id,
    )
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_company(other_company)
    storage.save_filings([filing, other_filing])
    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-observations-company-one",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        fact_validation_records={fact.fact_id: record},
        parsing_observations=[observation],
    )

    assert storage.get_financial_research_snapshot(company.security_id)["parsing_observations"] == [
        observation
    ]
    assert storage.get_financial_research_snapshot(other_company.security_id)["parsing_observations"] == []
    assert storage.get_facts(other_company.security_id) == []


def test_legacy_candidate_without_new_observation_keys_reopens_without_parse_metadata(tmp_path):
    company, filing, fact, record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-observations-legacy-archive",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        fact_validation_records={fact.fact_id: record},
        parsing_observations=[observation],
    )
    with storage.connect() as db:
        row = db.execute(
            "SELECT candidate_json FROM financial_ingestion_candidates WHERE generation_id=?",
            ("candidate-observations-legacy-archive",),
        ).fetchone()
        payload = json.loads(row["candidate_json"])
        payload.pop("fact_validation_records")
        payload.pop("parsing_observations")
        db.execute(
            "UPDATE financial_ingestion_candidates SET candidate_json=? WHERE generation_id=?",
            (json.dumps(payload, ensure_ascii=False), "candidate-observations-legacy-archive"),
        )

    reopened = Storage(tmp_path)
    assert reopened.get_financial_research_snapshot(company.security_id)["parsing_observations"] == []
    assert reopened.get_facts(company.security_id) == []
    audit_fact = next(
        item for item in reopened.get_facts_audit(company.security_id)
        if item["fact_id"] == fact.fact_id
    )
    assert "fact_validation_record" not in audit_fact


def test_candidate_missing_legacy_fact_bucket_is_reported_as_corruption(tmp_path):
    company, filing, fact, record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-observations-damaged-archive",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        fact_validation_records={fact.fact_id: record},
        parsing_observations=[observation],
    )
    with storage.connect() as db:
        row = db.execute(
            "SELECT candidate_json FROM financial_ingestion_candidates WHERE generation_id=?",
            ("candidate-observations-damaged-archive",),
        ).fetchone()
        payload = json.loads(row["candidate_json"])
        payload.pop("audit_facts")
        db.execute(
            "UPDATE financial_ingestion_candidates SET candidate_json=? WHERE generation_id=?",
            (json.dumps(payload, ensure_ascii=False), "candidate-observations-damaged-archive"),
        )

    reopened = Storage(tmp_path)
    snapshot = reopened.get_financial_research_snapshot(company.security_id)
    assert snapshot["parsing_observations"] == []
    assert any(item["code"] == "FINANCIAL_CANDIDATE_FACTS_SHAPE_INVALID" for item in snapshot["storage_issues"])
    with pytest.raises(StorageCorruptionError, match="FINANCIAL_CANDIDATE_FACTS_SHAPE_INVALID"):
        reopened.get_facts_audit(company.security_id)


def test_zero_admission_with_bound_parse_observation_persists_admission_incomplete_diagnostic(tmp_path):
    company, filing, fact, record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-admission-incomplete",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions=set(),
        fact_validation_records={fact.fact_id: record},
        parsing_observations=[observation],
    )

    reopened = Storage(tmp_path)
    with reopened.connect() as db:
        row = db.execute(
            "SELECT state, diagnostics_json FROM financial_ingestion_candidates WHERE generation_id=?",
            ("candidate-admission-incomplete",),
        ).fetchone()
    diagnostics = json.loads(row["diagnostics_json"])
    assert row["state"] == "incomplete"
    assert f"{filing.accession_number}:candidate_admission_incomplete" in diagnostics
    assert f"{filing.accession_number}:candidate_parse_incomplete" not in diagnostics
    assert reopened.get_facts(company.security_id) == []


def test_incomplete_without_parse_observation_keeps_legacy_parse_diagnostic(tmp_path):
    company, filing, fact, record, _observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        [fact],
        generation_id="candidate-parse-incomplete",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions=set(),
        fact_validation_records={fact.fact_id: record},
    )

    reopened = Storage(tmp_path)
    with reopened.connect() as db:
        row = db.execute(
            "SELECT state, diagnostics_json FROM financial_ingestion_candidates WHERE generation_id=?",
            ("candidate-parse-incomplete",),
        ).fetchone()
    diagnostics = json.loads(row["diagnostics_json"])
    assert row["state"] == "incomplete"
    assert f"{filing.accession_number}:candidate_parse_incomplete" in diagnostics
    assert f"{filing.accession_number}:candidate_admission_incomplete" not in diagnostics
    assert reopened.get_facts(company.security_id) == []


def test_zero_fact_parse_observation_retains_stage_timings_after_restart(tmp_path):
    company, filing, _fact, _record, observation = _candidate_fixture()
    observation = {
        **observation,
        "stage_timings": {
            "pdf_open_elapsed_seconds": 0.125,
            "parse_elapsed_seconds": 1.5,
            "candidate_mapping_elapsed_seconds": 0.25,
        },
        "page_count": 4,
        "completed_page_count": 4,
        "parse_strategy": "positioned-page-checkpoint",
        "backend": "pdfplumber",
        "cache_reused": False,
        "catalog_cache_hit": True,
        "local_structure": {
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
            "absolute_path": r"C:\\private\\model.onnx",
            "api_key": "must-not-persist",
            "unknown_metric": "ignored",
        },
        # Unknown top-level fields are deliberately ignored by the whitelist.
        "debug_path": r"C:\\confidential\\annual.pdf",
    }
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])

    storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        [],
        generation_id="candidate-zero-facts-observation",
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions=set(),
        parsing_observations=[observation],
    )

    reopened = Storage(tmp_path)
    persisted = reopened.get_financial_research_snapshot(company.security_id)["parsing_observations"]
    expected = {key: value for key, value in observation.items() if key != "debug_path"}
    expected["local_structure"] = {
        key: value for key, value in observation["local_structure"].items()
        if key in {
            "ocr_calls", "cache_hits", "completed_pages", "selected_pages",
            "worker_start_count", "model_load_seconds", "inference_seconds",
            "peak_memory_bytes", "admitted_field_count", "new_verified_field_count",
        }
    }
    assert persisted == [expected]
    assert reopened.get_facts(company.security_id) == []


@pytest.mark.parametrize(
    "bad_metrics",
    [
        pytest.param(
            {"stage_timings": {"pdf_open_elapsed_seconds": 0.1, "absolute_pdf_path": r"C:\\secret.pdf"}},
            id="unapproved-timing-key",
        ),
        pytest.param({"page_count": True}, id="boolean-count"),
        pytest.param({"stage_timings": {"parse_elapsed_seconds": float("inf")}}, id="non-finite-time"),
        pytest.param({"local_structure": "C:\\secret\\debug.json"}, id="non-mapping-local-structure"),
        pytest.param({"local_structure": {"ocr_calls": True}}, id="boolean-local-count"),
        pytest.param({"local_structure": {"completed_pages": 1.5}}, id="fractional-local-count"),
        pytest.param({"local_structure": {"model_load_seconds": float("nan")}}, id="nan-local-time"),
        pytest.param({"local_structure": {"inference_seconds": float("inf")}}, id="infinite-local-time"),
        pytest.param({"local_structure": {"peak_memory_bytes": True}}, id="boolean-peak-memory"),
        pytest.param({"local_structure": {"peak_memory_bytes": 274_877_906_945}}, id="peak-memory-over-limit"),
        pytest.param({"local_structure": {"ocr_calls": 1_000_001}}, id="local-count-over-limit"),
        pytest.param({"local_structure": {"model_load_seconds": 86_400.01}}, id="local-time-over-limit"),
        pytest.param({"backend": "unapproved-backend"}, id="unapproved-backend"),
        pytest.param({"cache_reused": "yes"}, id="non-boolean-cache-flag"),
    ],
)
def test_zero_fact_parse_observation_rejects_unapproved_security_metrics(tmp_path, bad_metrics):
    company, filing, _fact, _record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])

    with pytest.raises(ValueError, match="FINANCIAL_PARSING_OBSERVATION_INVALID"):
        storage.replace_financial_ingestion(
            company.security_id,
            [filing.accession_number],
            [],
            generation_id="candidate-invalid-observation-metrics",
            source_hashes={filing.accession_number: filing.content_hash},
            complete_accessions=set(),
            parsing_observations=[observation | bad_metrics],
        )


@pytest.mark.parametrize("stale_field", ["content_hash", "period_end", "fiscal_period"])
def test_zero_fact_parse_observation_rejects_stale_filing_identity(tmp_path, stale_field):
    company, filing, _fact, _record, observation = _candidate_fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    if stale_field == "content_hash":
        current_filing = replace(filing, content_hash="b" * 64)
    elif stale_field == "period_end":
        current_filing = replace(filing, period_end="2024-12-31")
    else:
        current_filing = replace(filing, fiscal_period="Q1")
    storage.save_filings([current_filing])

    with pytest.raises(ValueError, match="FINANCIAL_PARSING_OBSERVATION_BINDING_INVALID"):
        storage.replace_financial_ingestion(
            company.security_id,
            [filing.accession_number],
            [],
            generation_id="candidate-stale-parse-observation",
            source_hashes={filing.accession_number: filing.content_hash},
            complete_accessions=set(),
            parsing_observations=[observation],
        )
