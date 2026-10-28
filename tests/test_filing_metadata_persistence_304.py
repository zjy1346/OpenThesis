"""Additive filing-metadata persistence and conflict routing contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openthesis.domain import Company, FilingDocument
from openthesis.financial_sources.pdf import FinancialPdfSource
from openthesis import financial_ingestion
from openthesis.storage import Storage


def _filing(tmp_path: Path, **changes: object) -> FilingDocument:
    values: dict[str, object] = {
        "document_id": "doc-metadata-304",
        "company_cik": "CN_A:SZSE:000858.SZ",
        "accession_number": "acc-metadata-304",
        "form_type": "ANNUAL_REPORT",
        "fiscal_period": "FY",
        "period_end": "2025-12-31",
        "filed_at": "2026-04-30",
        "primary_document": "2025 Annual Report",
        "source_url": "https://example.test/wuliangye-2025.pdf",
        "local_path": str(tmp_path / "original.pdf"),
        "content_hash": "a" * 64,
        "revision": "original",
    }
    values.update(changes)
    return FilingDocument(**values)


def test_new_filing_metadata_roundtrips_without_changing_source_identity(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    company = Company(
        "CN_A:SZSE:000858.SZ", "000858.SZ", "五粮液", "SZSE",
        market="CN_A", security_id="CN_A:SZSE:000858.SZ", accounting_standard="CAS",
    )
    filing = _filing(
        tmp_path,
        period_metadata_status="conflict",
        period_metadata_diagnostic="FILING_PERIOD_METADATA_CONFLICT",
    )
    storage.save_company(company)
    storage.save_filings([filing])

    saved = storage.get_filings(company.security_id)[0]
    assert saved.period_metadata_status == "conflict"
    assert saved.period_metadata_diagnostic == "FILING_PERIOD_METADATA_CONFLICT"
    assert saved.revision == filing.revision
    assert saved.local_path == filing.local_path
    assert saved.content_hash == filing.content_hash
    assert saved.source_url == filing.source_url


def test_old_schema_migration_adds_empty_metadata_without_rewriting_source(tmp_path: Path) -> None:
    db_path = tmp_path / "openthesis.db"
    original_path = str(tmp_path / "legacy.pdf")
    original_hash = "b" * 64
    db = sqlite3.connect(db_path)
    try:
        db.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE companies(cik TEXT PRIMARY KEY, ticker TEXT NOT NULL, name TEXT NOT NULL, exchange_name TEXT NOT NULL DEFAULT '');
            CREATE TABLE filings(
                document_id TEXT PRIMARY KEY, company_cik TEXT NOT NULL,
                accession_number TEXT NOT NULL, form_type TEXT NOT NULL,
                fiscal_period TEXT NOT NULL, period_end TEXT NOT NULL,
                filed_at TEXT NOT NULL, primary_document TEXT NOT NULL,
                source_url TEXT NOT NULL, local_path TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '', ingested_at TEXT NOT NULL
            );
            INSERT INTO metadata VALUES('schema_version', '17');
            INSERT INTO companies VALUES('legacy-metadata', 'LEGACY', 'Legacy metadata', '');
            """,
        )
        db.execute(
            """
            INSERT INTO filings VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-metadata-doc", "legacy-metadata", "legacy-metadata-acc",
                "ANNUAL_REPORT", "FY", "2025-12-31", "2026-04-30", "legacy.pdf",
                "https://example.test/legacy.pdf", original_path, original_hash, "2026-05-01",
            ),
        )
        db.commit()
    finally:
        db.close()

    storage = Storage(tmp_path)
    migrated = storage.get_filings("legacy-metadata")
    assert len(migrated) == 1
    filing = migrated[0]
    assert filing.period_metadata_status == ""
    assert filing.period_metadata_diagnostic == ""
    assert filing.revision == "original"
    assert filing.local_path == original_path
    assert filing.content_hash == original_hash
    assert storage.migration_backup is not None


def test_conflicting_metadata_keeps_original_but_never_enters_pdf_or_structured_fetch(
    tmp_path: Path, monkeypatch
) -> None:
    company = Company(
        "CN_A:SZSE:000858.SZ", "000858.SZ", "五粮液", "SZSE",
        market="CN_A", security_id="CN_A:SZSE:000858.SZ", accounting_standard="CAS",
    )
    filing = _filing(
        tmp_path,
        period_end="2024-12-31",
        period_metadata_status="conflict",
        period_metadata_diagnostic="FILING_PERIOD_METADATA_CONFLICT",
    )
    parser_calls: list[tuple[object, ...]] = []
    structured_calls: list[str] = []

    def no_parse(*args: object, **_kwargs: object):
        parser_calls.append(args)
        return {}

    class StructuredSource:
        def fetch(self, _company, _filing):
            structured_calls.append("fetch")
            raise AssertionError("conflicting filing reached structured fetch")

    monkeypatch.setattr(financial_ingestion, "_parse_local_pdfs_bounded", no_parse)
    engine = type(
        "Engine",
        (),
        {
            "_batch_timeout_seconds": 5.0,
            "_parse_timeout_seconds": 5.0,
            "_max_workers": 1,
        },
    )()
    result = FinancialPdfSource(engine).collect_candidate_batches(
        company,
        [filing],
        structured_sources=[StructuredSource()],
    )

    assert result.batches_by_document == {}
    assert any("FILING_PERIOD_METADATA_CONFLICT" in item for item in result.diagnostics)
    assert parser_calls == []
    assert structured_calls == []
    assert filing.period_end == "2024-12-31"
    assert filing.content_hash == "a" * 64

