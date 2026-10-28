from __future__ import annotations

import hashlib
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openthesis.domain import Company, EvidenceRef, FinancialFact
from openthesis.financial_compiler import FactCandidate


ROOT = Path(__file__).resolve().parents[1]
REPLAY = runpy.run_path(str(ROOT / "scripts" / "replay-financial-ingestion.py"))


def test_candidate_export_keeps_provenance_hash_but_not_raw_excerpt_or_path() -> None:
    raw_excerpt = "Revenue 2025 CNY 123"
    fact = FinancialFact(
        fact_id="ingest:revenue-2025",
        company_cik="CN_A:SSE:600519.SH",
        concept="revenue",
        reported_concept="营业收入",
        value=123.0,
        unit="CNY",
        fiscal_year=2025,
        fiscal_period="FY",
        form_type="ANNUAL_REPORT",
        start_date="2025-01-01",
        end_date="2025-12-31",
        filed_at="2026-04-17",
        accession_number="1225114741",
        source_url="https://example.invalid/source.pdf",
        source_page=12,
        raw_text=raw_excerpt,
    )
    evidence = EvidenceRef(
        "fact:revenue-2025", "doc-1", "https://example.invalid/source.pdf",
        "annual report", "page:12", raw_excerpt, "2026-04-17",
    )

    projected = REPLAY["_candidate_view"](
        FactCandidate(fact, (evidence,), "financial-ingestion-ast")
    )
    encoded = str(projected)

    assert projected["fact"]["raw_excerpt_sha256"] == hashlib.sha256(raw_excerpt.encode()).hexdigest()
    assert projected["evidence_ids"] == ["fact:revenue-2025"]
    assert raw_excerpt not in encoded
    assert "local_path" not in encoded


def test_manifest_records_local_source_hash_without_echoing_path(tmp_path: Path) -> None:
    source = tmp_path / "secret-local-report.pdf"
    source.write_bytes(b"local pdf bytes")
    company = Company(
        "CN_A:SSE:600519.SH", "600519.SH", "Moutai", market="CN_A",
        reporting_currency="CNY", accounting_standard="CAS",
    )
    filings, sources = REPLAY["_filings_from_manifest"]([{
        "document_id": "annual-2025",
        "accession_number": "1225114741",
        "form_type": "ANNUAL_REPORT",
        "fiscal_period": "FY",
        "period_end": "2025-12-31",
        "filed_at": "2026-04-17",
        "primary_document": "2025 annual report",
        "local_path": str(source),
    }], company)

    assert filings[0].content_hash == hashlib.sha256(b"local pdf bytes").hexdigest()
    assert sources == [{
        "document_id": "annual-2025",
        "accession": "1225114741",
        "sha256": hashlib.sha256(b"local pdf bytes").hexdigest(),
    }]
    assert str(source) not in str(sources)


def test_manifest_rejects_source_hash_mismatch_before_parsing(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"original report")
    company = Company("issuer", "600519.SH", "Moutai", market="CN_A", reporting_currency="CNY")
    manifest = [{
        "document_id": "annual-2025",
        "accession_number": "1225114741",
        "form_type": "ANNUAL_REPORT",
        "fiscal_period": "FY",
        "period_end": "2025-12-31",
        "filed_at": "2026-04-17",
        "primary_document": "2025 annual report",
        "local_path": str(source),
        "content_sha256": "0" * 64,
    }]

    with pytest.raises(ValueError, match="replay_source_hash_mismatch"):
        REPLAY["_filings_from_manifest"](manifest, company)


def test_replay_rejects_original_changed_during_parsing(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"original report")
    manifest = {
        "company": {
            "cik": "issuer",
            "ticker": "600519.SH",
            "name": "Moutai",
            "market": "CN_A",
            "reporting_currency": "CNY",
        },
        "filings": [{
            "document_id": "annual-2025",
            "accession_number": "1225114741",
            "form_type": "ANNUAL_REPORT",
            "fiscal_period": "FY",
            "period_end": "2025-12-31",
            "filed_at": "2026-04-17",
            "primary_document": "2025 annual report",
            "local_path": str(source),
        }],
    }

    class MutatingEngine:
        def __init__(self, checkpoint_dir):
            self.checkpoint_dir = checkpoint_dir

        def collect_candidate_batches(self, _company, filings, **_kwargs):
            assert filings[0].content_hash == hashlib.sha256(b"original report").hexdigest()
            source.write_bytes(b"changed during parser")
            return SimpleNamespace(batches_by_document={"annual-2025": []}, diagnostics=[])

    class EmptyPipeline:
        def compile_from_ingestion(self, company, filings, engine, **_kwargs):
            engine.collect_candidate_batches(company, filings)
            return SimpleNamespace(
                diagnostics=[], resolved_facts=[], quarantined_facts=[], validations=[], allow_ai=False,
            )

    with patch.dict(REPLAY["replay"].__globals__, {
        "FinancialIngestionEngine": MutatingEngine,
        "FinancialCandidatePipeline": EmptyPipeline,
    }):
        with pytest.raises(ValueError, match="replay_source_changed_during_parse"):
            REPLAY["replay"](manifest)
