from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Sequence, TYPE_CHECKING

from .domain import (
    Company,
    CURRENT_DERIVED_VERSION,
    FilingDocument,
    FinancialFact,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)


if TYPE_CHECKING:
    from .research_continuity import StageAttempt


SCHEMA_VERSION = 13

# One immutable description of the derived-data inputs.  Source records are
# never migrated in place; a changed input contract makes current reads miss
# until a new deterministic research pass writes the current version.
DERIVED_PIPELINE_CONTRACT = {
    "version": "financial-derived-pipeline-v1",
    "disclosure_identity": "disclosure-identity-v1",
    "parser": "financial-ingestion-ast-v9",
    "rules": "financial-rules-v1",
    "facts": CURRENT_DERIVED_VERSION,
    "validation": "financial-validation-v1",
    "report_projection": "report-projection-v1",
}


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.filings_dir = self.data_dir / "filings"
        self.filings_dir.mkdir(parents=True, exist_ok=True)
        self.packs_dir = self.data_dir / "research-packs"
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "openthesis.db"
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            # WAL is a persistent database setting. Configure it once at
            # initialization rather than renegotiating it on every worker
            # connection; per-connection busy_timeout is set in ``connect``.
            db.execute("PRAGMA journal_mode = WAL")
            # Read the prior contract before migration.  A changed/missing
            # contract plus legacy derived rows requires a future rebuild;
            # startup itself must never pretend that rebuild completed.
            try:
                prior_contract_row = db.execute(
                    "SELECT value FROM metadata WHERE key = 'derived_pipeline_contract'"
                ).fetchone()
                prior_rebuild_row = db.execute(
                    "SELECT value FROM metadata WHERE key = 'derived_rebuild_required'"
                ).fetchone()
            except sqlite3.OperationalError:
                prior_contract_row = None
                prior_rebuild_row = None
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS companies (
                    cik TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    name TEXT NOT NULL,
                    exchange_name TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS issuers (
                    issuer_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    industry TEXT NOT NULL DEFAULT '',
                    industry_support TEXT NOT NULL DEFAULT 'standard'
                );

                CREATE TABLE IF NOT EXISTS security_listings (
                    security_id TEXT PRIMARY KEY,
                    issuer_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    exchange_name TEXT NOT NULL,
                    listing_currency TEXT NOT NULL,
                    reporting_currency TEXT NOT NULL,
                    accounting_standard TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(issuer_id) REFERENCES issuers(issuer_id)
                );

                CREATE INDEX IF NOT EXISTS idx_listings_issuer
                ON security_listings(issuer_id);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_market_symbol
                ON security_listings(market, symbol);

                CREATE TABLE IF NOT EXISTS filings (
                    document_id TEXT PRIMARY KEY,
                    company_cik TEXT NOT NULL,
                    accession_number TEXT NOT NULL,
                    form_type TEXT NOT NULL,
                    fiscal_period TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    filed_at TEXT NOT NULL,
                    primary_document TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    local_path TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    ingested_at TEXT NOT NULL,
                    revision TEXT NOT NULL DEFAULT 'original',
                    supersedes_document_id TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE TABLE IF NOT EXISTS financial_facts (
                    fact_id TEXT PRIMARY KEY,
                    company_cik TEXT NOT NULL,
                    concept TEXT NOT NULL,
                    reported_concept TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    fiscal_year INTEGER NOT NULL,
                    fiscal_period TEXT NOT NULL,
                    form_type TEXT NOT NULL,
                    start_date TEXT,
                    end_date TEXT NOT NULL,
                    filed_at TEXT NOT NULL,
                    accession_number TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    entity TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT '',
                    statement TEXT NOT NULL DEFAULT '',
                    period_start TEXT,
                    consolidated_scope TEXT NOT NULL DEFAULT 'consolidated',
                    currency TEXT NOT NULL DEFAULT '',
                    unit_scale REAL NOT NULL DEFAULT 1.0,
                    unit_provenance TEXT NOT NULL DEFAULT 'unknown',
                    revision TEXT NOT NULL DEFAULT 'original',
                     source_document TEXT NOT NULL DEFAULT '',
                     source_page INTEGER,
                     source_bbox_json TEXT,
                     source_column TEXT NOT NULL DEFAULT '',
                     raw_text TEXT NOT NULL DEFAULT '',
                    parser_version TEXT NOT NULL DEFAULT '',
                    generation_id TEXT NOT NULL DEFAULT '',
                    validation_status TEXT NOT NULL DEFAULT 'unvalidated',
                    extraction_status TEXT NOT NULL DEFAULT 'unresolved',
                    usage_status TEXT NOT NULL DEFAULT 'audit_only',
                    provenance_status TEXT NOT NULL DEFAULT 'unresolved',
                    derived_version TEXT NOT NULL DEFAULT 'legacy',
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE INDEX IF NOT EXISTS idx_facts_company_concept_year
                ON financial_facts(company_cik, concept, fiscal_year);

                CREATE TABLE IF NOT EXISTS financial_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT '',
                    bbox_json TEXT
                );

                CREATE TABLE IF NOT EXISTS financial_validation_groups (
                    group_id TEXT PRIMARY KEY,
                    company_cik TEXT NOT NULL,
                    accession_number TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    fiscal_period TEXT NOT NULL,
                    consolidated_scope TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'target',
                    status TEXT NOT NULL,
                    issues_json TEXT NOT NULL,
                    covered_concepts_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    derived_version TEXT NOT NULL DEFAULT 'legacy',
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE INDEX IF NOT EXISTS idx_validation_groups_company
                ON financial_validation_groups(company_cik, period_end DESC);

                CREATE TABLE IF NOT EXISTS financial_ingestion_candidates (
                    generation_id TEXT PRIMARY KEY,
                    company_cik TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parser_version TEXT NOT NULL DEFAULT '',
                    source_hashes_json TEXT NOT NULL DEFAULT '{}',
                    accessions_json TEXT NOT NULL DEFAULT '[]',
                    state TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL DEFAULT '[]',
                    candidate_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS financial_active_generations (
                    company_cik TEXT NOT NULL,
                    accession_number TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    PRIMARY KEY(company_cik, accession_number),
                    FOREIGN KEY(generation_id)
                        REFERENCES financial_ingestion_candidates(generation_id)
                );

                CREATE TABLE IF NOT EXISTS financial_retry_state (
                    company_cik TEXT PRIMARY KEY,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_stage TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE TABLE IF NOT EXISTS financial_recovery_cases (
                    case_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    company_cik TEXT NOT NULL,
                    accession_number TEXT NOT NULL,
                    document_hash TEXT NOT NULL DEFAULT '',
                    pages_json TEXT NOT NULL DEFAULT '[]',
                    fields_json TEXT NOT NULL DEFAULT '[]',
                    stage TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    next_action TEXT NOT NULL DEFAULT '',
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE INDEX IF NOT EXISTS idx_financial_recovery_run
                ON financial_recovery_cases(run_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_financial_recovery_company
                ON financial_recovery_cases(company_cik, updated_at DESC);

                CREATE TABLE IF NOT EXISTS vision_task_journal (
                    task_key TEXT PRIMARY KEY,
                    company_cik TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    remote_task_id TEXT NOT NULL DEFAULT '',
                    page_hashes_json TEXT NOT NULL,
                    page_numbers_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE INDEX IF NOT EXISTS idx_vision_task_company
                ON vision_task_journal(company_cik, updated_at DESC);

                CREATE TABLE IF NOT EXISTS vision_result_cache (
                    task_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY,
                    company_cik TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS research_stage_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id),
                    UNIQUE(run_id, stage, attempt)
                );

                CREATE INDEX IF NOT EXISTS idx_stage_attempt_run
                ON research_stage_attempts(run_id, attempt_id);

                CREATE TABLE IF NOT EXISTS research_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_research_job_run
                ON research_jobs(run_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS report_revisions (
                    revision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    parent_revision_id TEXT,
                    generation INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id),
                    FOREIGN KEY(parent_revision_id) REFERENCES report_revisions(revision_id),
                    UNIQUE(run_id, generation),
                    UNIQUE(run_id, content_sha256)
                );

                CREATE TABLE IF NOT EXISTS latest_report_revisions (
                    run_id TEXT PRIMARY KEY,
                    revision_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id),
                    FOREIGN KEY(revision_id) REFERENCES report_revisions(revision_id)
                );

                CREATE TABLE IF NOT EXISTS thesis_versions (
                    thesis_version_id TEXT PRIMARY KEY,
                    company_cik TEXT NOT NULL,
                    run_id TEXT,
                    version INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    FOREIGN KEY(company_cik) REFERENCES companies(cik),
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id),
                    UNIQUE(company_cik, version)
                );

                CREATE INDEX IF NOT EXISTS idx_thesis_company_version
                ON thesis_versions(company_cik, version DESC);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_snapshot_cache (
                    cache_key TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qualitative_evidence_cache (
                    cache_key TEXT PRIMARY KEY,
                    evidence_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Existing installations are migrated in place.  ALTER TABLE is
            # intentionally additive: historical filings/facts remain intact.
            self._ensure_columns(
                db,
                "filings",
                {
                    "revision": "TEXT NOT NULL DEFAULT 'original'",
                    "supersedes_document_id": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._ensure_columns(
                db,
                "financial_facts",
                {
                    "entity": "TEXT NOT NULL DEFAULT ''",
                    "market": "TEXT NOT NULL DEFAULT ''",
                    "statement": "TEXT NOT NULL DEFAULT ''",
                    "period_start": "TEXT",
                    "consolidated_scope": "TEXT NOT NULL DEFAULT 'consolidated'",
                    "currency": "TEXT NOT NULL DEFAULT ''",
                    "unit_scale": "REAL NOT NULL DEFAULT 1.0",
                    "unit_provenance": "TEXT NOT NULL DEFAULT 'unknown'",
                    "revision": "TEXT NOT NULL DEFAULT 'original'",
                     "source_document": "TEXT NOT NULL DEFAULT ''",
                     "source_page": "INTEGER",
                     "source_bbox_json": "TEXT",
                     "source_column": "TEXT NOT NULL DEFAULT ''",
                     "raw_text": "TEXT NOT NULL DEFAULT ''",
                    "parser_version": "TEXT NOT NULL DEFAULT ''",
                    "generation_id": "TEXT NOT NULL DEFAULT ''",
                    "validation_status": "TEXT NOT NULL DEFAULT 'unvalidated'",
                    "extraction_status": "TEXT NOT NULL DEFAULT 'unresolved'",
                    "usage_status": "TEXT NOT NULL DEFAULT 'audit_only'",
                    "provenance_status": "TEXT NOT NULL DEFAULT 'unresolved'",
                    "derived_version": "TEXT NOT NULL DEFAULT 'legacy'",
                },
            )
            self._ensure_columns(
                db,
                "vision_task_journal",
                {"page_numbers_json": "TEXT NOT NULL DEFAULT '[]'"},
            )
            self._ensure_columns(
                db,
                "financial_validation_groups",
                {
                    "derived_version": "TEXT NOT NULL DEFAULT 'legacy'",
                    "role": "TEXT NOT NULL DEFAULT 'target'",
                },
            )
            self._backfill_active_fact_generations(db)
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('derived_pipeline_contract', ?)",
                (json.dumps(DERIVED_PIPELINE_CONTRACT, sort_keys=True),),
            )
            prior_contract = None
            if prior_contract_row is not None:
                try:
                    prior_contract = json.loads(str(prior_contract_row[0]))
                except (TypeError, json.JSONDecodeError):
                    prior_contract = None
            legacy_rows = db.execute(
                "SELECT (SELECT COUNT(*) FROM financial_facts "
                "WHERE COALESCE(derived_version, 'legacy') <> ?) + "
                "(SELECT COUNT(*) FROM financial_validation_groups "
                "WHERE COALESCE(derived_version, 'legacy') <> ?)",
                (CURRENT_DERIVED_VERSION, CURRENT_DERIVED_VERSION),
            ).fetchone()[0]
            prior_required = str(prior_rebuild_row[0]) if prior_rebuild_row else "0"
            rebuild_required = "1" if (
                int(legacy_rows or 0) > 0
                and prior_contract != DERIVED_PIPELINE_CONTRACT
            ) else prior_required
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('derived_rebuild_required', ?)",
                (rebuild_required,),
            )

    @staticmethod
    def _ensure_columns(
        db: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        existing = {
            str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _backfill_active_fact_generations(db: sqlite3.Connection) -> None:
        """Repair generation tags written by early candidate-ledger builds."""
        rows = db.execute(
            "SELECT a.company_cik, a.generation_id, c.candidate_json "
            "FROM financial_active_generations a "
            "JOIN financial_ingestion_candidates c USING(generation_id)"
        ).fetchall()
        for row in rows:
            try:
                candidate = json.loads(str(row["candidate_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            fact_ids = {
                str(fact.get("fact_id", ""))
                for key in ("accepted_facts", "quarantined_facts", "audit_facts")
                for fact in candidate.get(key, ())
                if isinstance(fact, dict) and fact.get("fact_id")
            } if isinstance(candidate, dict) else set()
            if not fact_ids:
                continue
            placeholders = ", ".join("?" for _ in fact_ids)
            db.execute(
                f"UPDATE financial_facts SET generation_id = ? "
                f"WHERE company_cik = ? AND fact_id IN ({placeholders}) "
                "AND COALESCE(generation_id, '') = ''",
                (str(row["generation_id"]), str(row["company_cik"]), *sorted(fact_ids)),
            )

    def save_company(self, company: Company) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO issuers(issuer_id, name, industry, industry_support)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(issuer_id) DO UPDATE SET
                    name=excluded.name,
                    industry=excluded.industry,
                    industry_support=excluded.industry_support
                """,
                (
                    company.issuer_id,
                    company.name,
                    company.industry,
                    company.industry_support,
                ),
            )
            db.execute(
                """
                INSERT INTO security_listings(
                    security_id, issuer_id, symbol, market, exchange_name,
                    listing_currency, reporting_currency, accounting_standard,
                    source_url
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id) DO UPDATE SET
                    issuer_id=excluded.issuer_id,
                    symbol=excluded.symbol,
                    market=excluded.market,
                    exchange_name=excluded.exchange_name,
                    listing_currency=excluded.listing_currency,
                    reporting_currency=excluded.reporting_currency,
                    accounting_standard=excluded.accounting_standard,
                    source_url=excluded.source_url
                """,
                (
                    company.security_id,
                    company.issuer_id,
                    company.ticker,
                    company.market,
                    company.exchange,
                    company.listing_currency,
                    company.reporting_currency,
                    company.accounting_standard,
                    company.source_url,
                ),
            )
            db.execute(
                """
                INSERT INTO companies(cik, ticker, name, exchange_name)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(cik) DO UPDATE SET
                    ticker=excluded.ticker,
                    name=excluded.name,
                    exchange_name=excluded.exchange_name
                """,
                (company.cik, company.ticker, company.name, company.exchange),
            )

    def get_security_listing(self, security_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT l.*, i.name, i.industry, i.industry_support
                FROM security_listings l
                JOIN issuers i ON i.issuer_id = l.issuer_id
                WHERE l.security_id = ?
                """,
                (security_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_filings(self, filings: list[FilingDocument]) -> None:
        with self.connect() as db:
            db.executemany(
                """
                INSERT OR REPLACE INTO filings(
                    document_id, company_cik, accession_number, form_type,
                    fiscal_period, period_end, filed_at, primary_document,
                    source_url, local_path, content_hash, ingested_at,
                    revision, supersedes_document_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        filing.document_id,
                        filing.company_cik,
                        filing.accession_number,
                        filing.form_type,
                        filing.fiscal_period,
                        filing.period_end,
                        filing.filed_at,
                        filing.primary_document,
                        filing.source_url,
                        filing.local_path,
                        filing.content_hash,
                        filing.ingested_at,
                        filing.revision,
                        filing.supersedes_document_id,
                    )
                    for filing in filings
                ],
            )

    def company_exists(self, company_cik: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM companies WHERE cik = ?",
                (company_cik,),
            ).fetchone()
        return row is not None

    def get_filings(self, company_cik: str) -> list[FilingDocument]:
        """Return stored official filings in deterministic newest-first order."""

        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM filings
                WHERE company_cik = ?
                ORDER BY period_end DESC, filed_at DESC, document_id
                """,
                (company_cik,),
            ).fetchall()
        return [FilingDocument(**dict(row)) for row in rows]

    def get_financial_retry_state(self, company_cik: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM financial_retry_state WHERE company_cik = ?",
                (company_cik,),
            ).fetchone()
        if row is None:
            return {
                "company_cik": company_cik,
                "attempt_count": 0,
                "last_stage": "",
                "last_error": "",
                "updated_at": "",
            }
        return dict(row)

    def record_financial_retry_attempt(
        self, company_cik: str, *, stage: str, error: str
    ) -> dict[str, Any]:
        updated_at = utc_now_iso()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO financial_retry_state(
                    company_cik, attempt_count, last_stage, last_error, updated_at
                ) VALUES(?, 1, ?, ?, ?)
                ON CONFLICT(company_cik) DO UPDATE SET
                    attempt_count=financial_retry_state.attempt_count + 1,
                    last_stage=excluded.last_stage,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (company_cik, stage, error, updated_at),
            )
            row = db.execute(
                "SELECT * FROM financial_retry_state WHERE company_cik = ?",
                (company_cik,),
            ).fetchone()
        return dict(row)

    def save_financial_recovery_case(
        self,
        *,
        run_id: str,
        company_cik: str,
        accession_number: str,
        document_hash: str = "",
        pages: Sequence[int] = (),
        fields: Sequence[str] = (),
        stage: str = "",
        error_code: str = "",
        next_action: str = "",
        status: str = "open",
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a bounded recovery target without secrets or model output."""

        safe_accession = str(accession_number).strip()
        if not str(run_id).strip() or not safe_accession:
            raise ValueError("run_id and accession_number are required")
        case_id = "|".join((str(run_id), str(company_cik), safe_accession))
        updated_at = utc_now_iso()
        safe_pages = [int(item) for item in pages]
        safe_fields = [str(item)[:160] for item in fields]
        safe_diagnostics = {
            str(key)[:80]: str(value)[:800]
            for key, value in (diagnostics or {}).items()
            if str(key) not in {"api_key", "token", "secret", "response"}
        }
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO financial_recovery_cases(
                    case_id, run_id, company_cik, accession_number, document_hash,
                    pages_json, fields_json, stage, error_code, attempts, status,
                    next_action, diagnostics_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    document_hash=excluded.document_hash,
                    pages_json=excluded.pages_json,
                    fields_json=excluded.fields_json,
                    stage=excluded.stage,
                    error_code=excluded.error_code,
                    attempts=financial_recovery_cases.attempts + 1,
                    status=excluded.status,
                    next_action=excluded.next_action,
                    diagnostics_json=excluded.diagnostics_json,
                    updated_at=excluded.updated_at
                """,
                (
                    case_id, str(run_id), str(company_cik), safe_accession,
                    str(document_hash)[:256], json.dumps(safe_pages),
                    json.dumps(safe_fields, ensure_ascii=False), str(stage)[:120],
                    str(error_code)[:120], str(status)[:80], str(next_action)[:160],
                    json.dumps(safe_diagnostics, ensure_ascii=False), updated_at,
                ),
            )
            row = db.execute(
                "SELECT * FROM financial_recovery_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return self._recovery_case_row(row)

    def get_financial_recovery_cases(
        self, run_id: str | None = None, *, company_cik: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM financial_recovery_cases"
        params: list[str] = []
        if run_id is not None:
            query += " WHERE run_id = ?"
            params.append(str(run_id))
        elif company_cik is not None:
            query += " WHERE company_cik = ?"
            params.append(str(company_cik))
        query += " ORDER BY updated_at DESC, case_id"
        with self.connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [self._recovery_case_row(row) for row in rows]

    def reassign_financial_recovery_cases(self, source_run_id: str, target_run_id: str) -> int:
        """Attach pre-report session cases to the durable research run."""
        if not str(source_run_id).strip() or not str(target_run_id).strip():
            raise ValueError("recovery case ids are required")
        with self.connect() as db:
            result = db.execute(
                "UPDATE financial_recovery_cases SET run_id = ?, updated_at = ? WHERE run_id = ?",
                (str(target_run_id), utc_now_iso(), str(source_run_id)),
            )
        return int(result.rowcount)

    def resolve_financial_recovery_cases(
        self, run_id: str, accessions: Sequence[str] = ()
    ) -> int:
        """Close only the accessions that completed deterministic recovery."""
        values = [str(item) for item in accessions if str(item)]
        with self.connect() as db:
            if values:
                marks = ", ".join("?" for _ in values)
                result = db.execute(
                    f"UPDATE financial_recovery_cases SET status = 'resolved', error_code = '', next_action = 'none', updated_at = ? WHERE run_id = ? AND accession_number IN ({marks})",
                    (utc_now_iso(), str(run_id), *values),
                )
            else:
                result = db.execute(
                    "UPDATE financial_recovery_cases SET status = 'resolved', error_code = '', next_action = 'none', updated_at = ? WHERE run_id = ?",
                    (utc_now_iso(), str(run_id)),
                )
        return int(result.rowcount)

    @staticmethod
    def _recovery_case_row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        for key, default in (("pages_json", []), ("fields_json", []), ("diagnostics_json", {})):
            encoded = item.pop(key, None)
            try:
                item[key.removesuffix("_json")] = json.loads(encoded) if encoded else default
            except (TypeError, ValueError, json.JSONDecodeError):
                item[key.removesuffix("_json")] = default
        return item

    def save_vision_task(
        self,
        task_key: str,
        *,
        company_cik: str,
        document_id: str,
        provider: str,
        page_hashes: Sequence[str],
        page_numbers: Sequence[int] = (),
        status: str,
        remote_task_id: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        """Persist only safe cloud-task metadata, never document bytes or secrets."""

        updated_at = utc_now_iso()
        safe_hashes = [str(value) for value in page_hashes]
        safe_page_numbers = [int(value) for value in page_numbers]
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO vision_task_journal(
                    task_key, company_cik, document_id, provider, remote_task_id,
                    page_hashes_json, page_numbers_json, status, error_code, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    company_cik=excluded.company_cik,
                    document_id=excluded.document_id,
                    provider=excluded.provider,
                    remote_task_id=CASE
                        WHEN excluded.remote_task_id <> '' THEN excluded.remote_task_id
                        ELSE vision_task_journal.remote_task_id
                    END,
                    page_hashes_json=excluded.page_hashes_json,
                    page_numbers_json=excluded.page_numbers_json,
                    status=excluded.status,
                    error_code=excluded.error_code,
                    updated_at=excluded.updated_at
                """,
                (
                    task_key,
                    company_cik,
                    document_id,
                    provider,
                    remote_task_id,
                    json.dumps(safe_hashes, separators=(",", ":")),
                    json.dumps(safe_page_numbers, separators=(",", ":")),
                    status,
                    error_code,
                    updated_at,
                ),
            )
            row = db.execute(
                "SELECT * FROM vision_task_journal WHERE task_key = ?", (task_key,)
            ).fetchone()
        payload = dict(row)
        payload["page_hashes"] = json.loads(payload.pop("page_hashes_json"))
        payload["page_numbers"] = json.loads(payload.pop("page_numbers_json"))
        return payload

    def get_vision_task(self, task_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM vision_task_journal WHERE task_key = ?", (task_key,)
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["page_hashes"] = json.loads(payload.pop("page_hashes_json"))
        payload["page_numbers"] = json.loads(payload.pop("page_numbers_json"))
        return payload

    def list_vision_tasks(self, company_cik: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM vision_task_journal
                WHERE company_cik = ? ORDER BY updated_at DESC, task_key
                """,
                (company_cik,),
            ).fetchall()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["page_hashes"] = json.loads(item.pop("page_hashes_json"))
            item["page_numbers"] = json.loads(item.pop("page_numbers_json"))
            payloads.append(item)
        return payloads

    def save_vision_result(self, task_key: str, result: dict[str, Any]) -> None:
        """Cache a complete candidate result; callers must not store credentials."""

        with self.connect() as db:
            db.execute(
                """
                INSERT INTO vision_result_cache(task_key, result_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (task_key, json.dumps(result, ensure_ascii=False), utc_now_iso()),
            )

    def get_vision_result(self, task_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT result_json FROM vision_result_cache WHERE task_key = ?",
                (task_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def save_facts(self, facts: list[FinancialFact]) -> None:
        with self.connect() as db:
            self._insert_facts(db, [self._canonical_fact(fact) for fact in facts])

    def replace_facts_for_filings(
        self,
        company_cik: str,
        accession_numbers: list[str],
        facts: list[FinancialFact],
    ) -> None:
        """Atomically replace parser output so stale facts cannot survive a reparse."""

        unique_accessions = sorted({item for item in accession_numbers if item})
        with self.connect() as db:
            for accession_number in unique_accessions:
                db.execute(
                    "DELETE FROM financial_facts WHERE company_cik = ? AND accession_number = ?",
                    (company_cik, accession_number),
                )
            self._insert_facts(db, [self._canonical_fact(fact) for fact in facts])

    def replace_financial_ingestion(
        self,
        company_cik: str,
        accession_numbers: list[str],
        accepted_facts: list[FinancialFact],
        quarantined_facts: list[FinancialFact] | None = None,
        validation_groups: list[Any] | tuple[Any, ...] = (),
        evidence: list[Any] | tuple[Any, ...] = (),
        audit_facts: list[FinancialFact] | tuple[FinancialFact, ...] = (),
        *,
        generation_id: str | None = None,
        parser_version: str = "",
        source_hashes: dict[str, str] | None = None,
        complete_accessions: set[str] | frozenset[str] | None = None,
        candidate_diagnostics: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Atomically replace facts, evidence, and validation decisions.

        Rejected facts are retained with ``validation_status=REJECTED`` for
        auditability, while normal reads hide them.  The operation is scoped
        to the supplied accessions so prior research history is never deleted.
        """
        unique = sorted({value for value in accession_numbers if value})
        with self.connect() as db:
            promotable = set(unique)
            accession_states: dict[str, str] = {item: "promoted" for item in unique}
            normalized_hashes = {
                str(key): str(value).casefold()
                for key, value in (source_hashes or {}).items()
                if key and value
            }
            if generation_id:
                # Tag candidate facts before both staging and active
                # materialization. Readers can then match rows to the active
                # generation instead of observing a mixed accession.
                accepted_facts = [replace(fact, generation_id=generation_id) for fact in accepted_facts]
                quarantined_facts = [
                    replace(fact, generation_id=generation_id)
                    for fact in (quarantined_facts or ())
                ]
                audit_facts = [replace(fact, generation_id=generation_id) for fact in audit_facts]
                now = utc_now_iso()
                parser_value = str(parser_version or "")
                group_payload = []
                for group in validation_groups:
                    validation = getattr(group, "validation", None)
                    group_payload.append({
                        "identity": list(getattr(group, "identity", ())),
                        "role": str(getattr(group, "role", "") or "target"),
                        "status": str(getattr(getattr(validation, "status", None), "value", "")),
                        "issues": list(getattr(validation, "issues", ())),
                        "covered_concepts": sorted(getattr(validation, "covered_concepts", ())),
                        "accepted_fact_ids": [fact.fact_id for fact in getattr(validation, "accepted", ())],
                        "quarantined_fact_ids": [fact.fact_id for fact in getattr(validation, "quarantined", ())],
                    })
                candidate_payload = {
                    "accepted_facts": [fact.to_dict() for fact in accepted_facts],
                    "quarantined_facts": [fact.to_dict() for fact in (quarantined_facts or ())],
                    "audit_facts": [fact.to_dict() for fact in audit_facts],
                    "evidence": [item.to_dict() for item in evidence],
                    "validation_groups": group_payload,
                }
                diagnostics = list(dict.fromkeys(str(item) for item in candidate_diagnostics if item))

                for accession in unique:
                    if complete_accessions is not None and accession not in complete_accessions:
                        accession_states[accession] = "incomplete"
                        diagnostics.append(f"{accession}:candidate_parse_incomplete")
                        promotable.discard(accession)
                        continue

                    # When exactly the same source bytes are reparsed, a
                    # drastic loss of distinct statement concepts is an
                    # investigation trigger. It is not evidence that old
                    # numbers are correct; the old set is kept active while
                    # the new candidate and its provenance remain auditable.
                    existing_hash_row = db.execute(
                        "SELECT content_hash FROM filings WHERE company_cik = ? AND accession_number = ? ORDER BY ingested_at DESC LIMIT 1",
                        (company_cik, accession),
                    ).fetchone()
                    existing_hash = str(existing_hash_row[0] if existing_hash_row else "")
                    candidate_hash = normalized_hashes.get(accession, "")
                    existing = db.execute(
                        "SELECT concept, parser_version FROM financial_facts "
                        "WHERE company_cik = ? AND accession_number = ? "
                        "AND COALESCE(validation_status, 'unvalidated') <> 'REJECTED' "
                        "AND COALESCE(usage_status, 'audit_only') IN ('canonical_research', 'comparator')",
                        (company_cik, accession),
                    ).fetchall()
                    existing_parsers = {str(row["parser_version"] or "") for row in existing}
                    existing_core = {
                        str(row["concept"]).casefold() for row in existing
                    } & {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity", "total_equity"}
                    candidate_core = {
                        str(fact.concept).casefold() for fact in accepted_facts
                        if fact.accession_number == accession
                    } & {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity", "total_equity"}
                    same_source = bool(
                        existing_hash and candidate_hash
                        and existing_hash.casefold() == candidate_hash
                    )
                    known_untrusted_legacy = any(
                        version.endswith("ast-v7") for version in existing_parsers
                    )
                    if (
                        same_source
                        and not known_untrusted_legacy
                        and len(existing_core) >= 4
                        and len(candidate_core) <= 1
                    ):
                        accession_states[accession] = "investigate"
                        promotable.discard(accession)
                        diagnostics.append(
                            f"{accession}:same_source_core_coverage_shrank:{','.join(sorted(existing_core - candidate_core))}"
                        )

                candidate_state = (
                    "promoted" if len(promotable) == len(unique)
                    else "partially_promoted" if promotable
                    else "investigate" if "investigate" in accession_states.values()
                    else "incomplete"
                )
                db.execute(
                        "INSERT OR REPLACE INTO financial_ingestion_candidates("
                    "generation_id, company_cik, created_at, parser_version, "
                        "source_hashes_json, accessions_json, state, diagnostics_json, candidate_json"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id, company_cik, now, parser_value,
                        json.dumps(normalized_hashes, sort_keys=True),
                        json.dumps(unique), candidate_state,
                        json.dumps(diagnostics, ensure_ascii=False),
                        json.dumps(candidate_payload, ensure_ascii=False),
                    ),
                )
                if not promotable:
                    return {
                        "generation_id": generation_id,
                        "state": candidate_state,
                        "accessions": accession_states,
                        "diagnostics": diagnostics,
                    }

                accepted_facts = [fact for fact in accepted_facts if fact.accession_number in promotable]
                quarantined_facts = [fact for fact in (quarantined_facts or ()) if fact.accession_number in promotable]
                audit_facts = [fact for fact in audit_facts if fact.accession_number in promotable]
                validation_groups = [
                    group for group in validation_groups
                    if tuple(getattr(group, "identity", ()))
                    and tuple(getattr(group, "identity", ()))[0] in promotable
                ]
                document_ids = {
                    str(row[0])
                    for accession in promotable
                    for row in db.execute(
                        "SELECT document_id FROM filings WHERE company_cik = ? AND accession_number = ?",
                        (company_cik, accession),
                    ).fetchall()
                }
                evidence = [item for item in evidence if str(getattr(item, "document_id", "")) in document_ids]
                unique = sorted(promotable)

            for accession in unique:
                # Evidence is keyed by filing document rather than accession.
                # Resolve the document ids before replacing parser output so a
                # reparse cannot leave excerpts from an older, richer parse.
                document_ids = {
                    str(row[0])
                    for row in db.execute(
                        """
                        SELECT document_id FROM filings
                        WHERE company_cik = ? AND accession_number = ?
                        """,
                        (company_cik, accession),
                    ).fetchall()
                }
                document_ids.update(
                    str(getattr(item, "document_id", ""))
                    for item in evidence
                    if getattr(item, "document_id", "")
                )
                if document_ids:
                    placeholders = ", ".join("?" for _ in document_ids)
                    db.execute(
                        f"DELETE FROM financial_evidence WHERE document_id IN ({placeholders})",
                        tuple(sorted(document_ids)),
                    )
                db.execute(
                    "DELETE FROM financial_facts WHERE company_cik = ? AND accession_number = ?",
                    (company_cik, accession),
                )
                db.execute(
                    "DELETE FROM financial_validation_groups WHERE company_cik = ? AND accession_number = ?",
                    (company_cik, accession),
                )
            # Quarantine is a storage-boundary invariant. Parsers may hand us
            # the original fact instance whose status is still ``unvalidated``;
            # never allow that metadata omission to expose a rejected fact via
            # the normal ``get_facts`` query.
            rejected_facts = [
                replace(
                    fact,
                    validation_status="REJECTED",
                    usage_status="quarantined",
                )
                for fact in (quarantined_facts or ())
            ]
            # Audit-only facts are retained for traceability but are never
            # rewritten as REJECTED and never appear in the canonical view.
            retained_audit = [
                replace(fact, usage_status="audit_only")
                for fact in audit_facts
            ]
            self._insert_facts(
                db,
                [self._canonical_fact(fact) for fact in accepted_facts]
                + retained_audit
                + rejected_facts,
            )
            for item in evidence:
                bbox = getattr(item, "bbox", None)
                db.execute(
                    """
                    INSERT OR REPLACE INTO financial_evidence(
                        evidence_id, document_id, source_url, title, locator,
                        excerpt, published_at, content_hash, bbox_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.evidence_id, item.document_id, item.source_url,
                        item.title, item.locator, item.excerpt, item.published_at,
                        item.content_hash, json.dumps(bbox) if bbox is not None else None,
                    ),
                )
            for group in validation_groups:
                identity = tuple(getattr(group, "identity", ()))
                if len(identity) != 5:
                    continue
                validation = getattr(group, "validation", None)
                if validation is None:
                    continue
                status = getattr(getattr(validation, "status", None), "value", str(getattr(validation, "status", "REJECTED")))
                # Include the issuer key to avoid collisions when two
                # securities use the same accession/period identity.
                role = str(getattr(group, "role", "") or "target").casefold()
                if role not in {"target", "comparator", "audit"}:
                    role = "target"
                group_id = "|".join((company_cik, *identity, role))
                db.execute(
                    """
                    INSERT OR REPLACE INTO financial_validation_groups(
                        group_id, company_cik, accession_number, period_end,
                        fiscal_period, consolidated_scope, currency, role, status,
                        issues_json, covered_concepts_json, updated_at, derived_version
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id, company_cik, identity[0], identity[1], identity[2],
                        identity[3], identity[4], role, status,
                        json.dumps(list(getattr(validation, "issues", ())), ensure_ascii=False),
                        json.dumps(sorted(getattr(validation, "covered_concepts", frozenset())), ensure_ascii=False),
                        utc_now_iso(), CURRENT_DERIVED_VERSION,
                    ),
                )
            if generation_id:
                for accession in unique:
                    db.execute(
                        "INSERT OR REPLACE INTO financial_active_generations "
                        "(company_cik, accession_number, generation_id) VALUES(?, ?, ?)",
                        (company_cik, accession, generation_id),
                    )
                db.execute(
                    "UPDATE financial_ingestion_candidates SET state = ? WHERE generation_id = ?",
                    (
                        "promoted" if all(state == "promoted" for state in accession_states.values())
                        else "partially_promoted",
                        generation_id,
                    ),
                )
            self._clear_rebuild_marker_if_fully_rebuilt(db)
        if generation_id:
            return {
                "generation_id": generation_id,
                "state": "promoted" if all(state == "promoted" for state in accession_states.values()) else "partially_promoted",
                "accessions": accession_states,
                "diagnostics": diagnostics,
            }
        return None

    @staticmethod
    def _clear_rebuild_marker_if_fully_rebuilt(db: sqlite3.Connection) -> None:
        """Clear the migration marker only after no legacy derived rows remain."""
        legacy_rows = db.execute(
            "SELECT (SELECT COUNT(*) FROM financial_facts "
            "WHERE COALESCE(derived_version, 'legacy') <> ?) + "
            "(SELECT COUNT(*) FROM financial_validation_groups "
            "WHERE COALESCE(derived_version, 'legacy') <> ?)",
            (CURRENT_DERIVED_VERSION, CURRENT_DERIVED_VERSION),
        ).fetchone()[0]
        if int(legacy_rows or 0) == 0:
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('derived_rebuild_required', '0')"
            )

    @staticmethod
    def _canonical_fact(fact: FinancialFact) -> FinancialFact:
        """Mark facts entering the accepted compatibility write path.

        The dataclass default is fail-closed for untrusted construction.  The
        historical ``save_facts``/accepted-ingestion methods are explicit
        accepted write seams, so they add the current derived version and
        verified provenance while audit/quarantine inputs remain untouched.
        """
        return replace(
            fact,
            extraction_status="extracted",
            usage_status="canonical_research",
            provenance_status="verified",
            derived_version=CURRENT_DERIVED_VERSION,
        )

    @staticmethod
    def _insert_facts(db: sqlite3.Connection, facts: list[FinancialFact]) -> None:
        db.executemany(
            """
            INSERT OR REPLACE INTO financial_facts(
                fact_id, company_cik, concept, reported_concept, value,
                unit, fiscal_year, fiscal_period, form_type, start_date,
                end_date, filed_at, accession_number, source_url, scope,
                entity, market, statement, period_start, consolidated_scope,
                 currency, unit_scale, unit_provenance, revision, source_document, source_page,
                 source_bbox_json, source_column, raw_text, parser_version, validation_status
                , extraction_status, usage_status, provenance_status, derived_version,
                generation_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    fact.fact_id,
                    fact.company_cik,
                    fact.concept,
                    fact.reported_concept,
                    fact.value,
                    fact.unit,
                    fact.fiscal_year,
                    fact.fiscal_period,
                    fact.form_type,
                    fact.start_date,
                    fact.end_date,
                    fact.filed_at,
                    fact.accession_number,
                    fact.source_url,
                    fact.scope,
                    fact.entity,
                    fact.market,
                    fact.statement,
                    fact.period_start,
                    fact.consolidated_scope,
                    fact.currency,
                    fact.unit_scale,
                    fact.unit_provenance,
                    fact.revision,
                    fact.source_document,
                     fact.source_page,
                     json.dumps(fact.source_bbox) if fact.source_bbox is not None else None,
                     fact.source_column,
                     fact.raw_text,
                    fact.parser_version,
                    fact.validation_status,
                    fact.extraction_status,
                    fact.usage_status,
                    fact.provenance_status,
                    fact.derived_version,
                     fact.generation_id,
                )
                for fact in facts
            ],
        )

    def get_facts(self, cik: str) -> list[dict[str, Any]]:
        return self.get_financial_research_snapshot(cik)["facts"]

    def get_financial_research_snapshot(self, cik: str) -> dict[str, Any]:
        """Capture facts and active generation pointers in one read view."""
        with self.connect() as db:
            db.execute("BEGIN")
            generations = {
                str(row["accession_number"]): str(row["generation_id"])
                for row in db.execute(
                    "SELECT accession_number, generation_id "
                    "FROM financial_active_generations WHERE company_cik = ?",
                    (cik,),
                ).fetchall()
            }
            rows = db.execute(
                """
                SELECT f.* FROM financial_facts f
                LEFT JOIN security_listings l ON l.security_id = f.company_cik
                LEFT JOIN financial_active_generations g
                  ON g.company_cik = f.company_cik
                 AND g.accession_number = f.accession_number
                WHERE f.company_cik = ?
                  AND COALESCE(f.validation_status, 'unvalidated') <> 'REJECTED'
                  AND COALESCE(f.usage_status, 'audit_only') IN ('canonical_research', 'comparator')
                  AND COALESCE(f.derived_version, 'legacy') = ?
                  AND (g.generation_id IS NULL OR f.generation_id = g.generation_id)
                  AND COALESCE(f.consolidated_scope, 'consolidated') = 'consolidated'
                  AND (
                      l.reporting_currency IS NULL
                      OR COALESCE(f.currency, '') = ''
                      OR UPPER(f.currency) = UPPER(l.reporting_currency)
                  )
                ORDER BY fiscal_year DESC, concept
                """,
                (cik, CURRENT_DERIVED_VERSION),
            ).fetchall()
        return {
            "facts": [self._fact_row(row) for row in rows],
            "generations": generations,
        }

    def get_facts_audit(self, cik: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM financial_facts WHERE company_cik = ? ORDER BY fiscal_year DESC, concept",
                (cik,),
            ).fetchall()
            candidates = db.execute(
                "SELECT generation_id, candidate_json FROM financial_ingestion_candidates "
                "WHERE company_cik = ? ORDER BY created_at DESC, generation_id DESC",
                (cik,),
            ).fetchall()
        result = [self._fact_row(row) for row in rows]
        seen = {
            (str(item.get("fact_id", "")), str(item.get("generation_id", "")))
            for item in result
        }
        # Candidate generations are deliberately excluded from normal research
        # reads. Keep their facts inspectable through the audit view without
        # materializing or overwriting the currently active generation.
        for candidate in candidates:
            try:
                payload = json.loads(candidate["candidate_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            generation_id = str(candidate["generation_id"] or "")
            for bucket, usage_status in (
                ("accepted_facts", "candidate_audit"),
                ("quarantined_facts", "quarantined"),
                ("audit_facts", "audit_only"),
            ):
                fact_items = payload.get(bucket, [])
                if not isinstance(fact_items, list):
                    continue
                for raw in fact_items:
                    if not isinstance(raw, dict):
                        continue
                    fact = dict(raw)
                    fact_id = str(fact.get("fact_id", ""))
                    if not fact_id:
                        continue
                    fact_generation = str(fact.get("generation_id") or generation_id)
                    identity = (fact_id, fact_generation)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    fact["generation_id"] = fact_generation
                    fact["usage_status"] = usage_status
                    if bucket == "quarantined_facts":
                        fact["validation_status"] = "REJECTED"
                    fact.setdefault("source_bbox", None)
                    result.append(fact)
        result.sort(
            key=lambda item: (
                -int(item.get("fiscal_year") or 0),
                str(item.get("concept", "")),
                str(item.get("generation_id", "")),
                str(item.get("fact_id", "")),
            )
        )
        return result

    @staticmethod
    def _fact_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        encoded = item.pop("source_bbox_json", None)
        if encoded:
            try:
                item["source_bbox"] = tuple(float(value) for value in json.loads(encoded))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["source_bbox"] = None
        else:
            item["source_bbox"] = None
        return item

    def get_validation_groups(self, cik: str) -> list[dict[str, Any]]:
        """Return only groups produced by the current derived contract."""
        return self._get_validation_groups(cik, current_only=True)

    def get_validation_groups_audit(self, cik: str) -> list[dict[str, Any]]:
        """Return all historical group decisions for diagnostics/audit."""
        return self._get_validation_groups(cik, current_only=False)

    def _get_validation_groups(
        self, cik: str, *, current_only: bool
    ) -> list[dict[str, Any]]:
        clause = " AND COALESCE(derived_version, 'legacy') = ?" if current_only else ""
        params: tuple[Any, ...] = (cik, CURRENT_DERIVED_VERSION) if current_only else (cik,)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM financial_validation_groups WHERE company_cik = ?"
                + clause
                + " ORDER BY period_end DESC, accession_number",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("issues_json", "covered_concepts_json"):
                try:
                    item[key.removesuffix("_json")] = json.loads(item.pop(key))
                except (TypeError, json.JSONDecodeError):
                    item[key.removesuffix("_json")] = []
            result.append(item)
        return result

    def get_financial_evidence(self, document_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM financial_evidence"
        params: tuple[Any, ...] = ()
        if document_id:
            query += " WHERE document_id = ?"
            params = (document_id,)
        query += " ORDER BY evidence_id"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            encoded = item.pop("bbox_json", None)
            if encoded:
                try:
                    item["bbox"] = tuple(float(value) for value in json.loads(encoded))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["bbox"] = None
            else:
                item["bbox"] = None
            result.append(item)
        return result

    def save_qualitative_evidence_cache(
        self, cache_key: str, evidence: list[dict[str, Any]]
    ) -> None:
        """Persist immutable parser output keyed by content and parser contract."""

        with self.connect() as db:
            db.execute(
                """
                INSERT INTO qualitative_evidence_cache(cache_key, evidence_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    evidence_json=excluded.evidence_json,
                    updated_at=excluded.updated_at
                """,
                (cache_key, json.dumps(evidence, ensure_ascii=False), utc_now_iso()),
            )

    def get_qualitative_evidence_cache(
        self, cache_key: str
    ) -> list[dict[str, Any]] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT evidence_json FROM qualitative_evidence_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except (TypeError, json.JSONDecodeError):
            return None
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else None

    def save_run(self, run: ResearchRun) -> None:
        payload = run.to_dict()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO research_runs(
                    run_id, company_cik, payload_json, status, started_at, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    completed_at=excluded.completed_at
                """,
                (
                    run.run_id,
                    run.company.cik,
                    json.dumps(payload, ensure_ascii=False),
                    run.status.value,
                    run.started_at,
                    run.completed_at,
                ),
            )

    def save_run_with_artifacts(
        self, run: ResearchRun, artifacts: list[ResearchArtifact]
    ) -> None:
        """Persist a run snapshot and new immutable artifacts atomically.

        Artifact identifiers are content identities, not overwrite slots.  A
        repeated identical write is idempotent; reusing an identifier for
        different content is a storage-integrity error.
        """
        payload = run.to_dict()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO research_runs(
                    run_id, company_cik, payload_json, status, started_at, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    completed_at=excluded.completed_at
                """,
                (
                    run.run_id,
                    run.company.cik,
                    json.dumps(payload, ensure_ascii=False),
                    run.status.value,
                    run.started_at,
                    run.completed_at,
                ),
            )
            for artifact in artifacts:
                encoded = json.dumps(
                    artifact.content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = db.execute(
                    """
                    SELECT run_id, artifact_type, title, payload_json,
                           model_id, agent_id
                    FROM artifacts WHERE artifact_id = ?
                    """,
                    (artifact.artifact_id,),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """
                        INSERT INTO artifacts(
                            artifact_id, run_id, artifact_type, title,
                            payload_json, model_id, agent_id, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact.artifact_id,
                            artifact.run_id,
                            artifact.artifact_type,
                            artifact.title,
                            encoded,
                            artifact.model_id,
                            artifact.agent_id,
                            artifact.created_at,
                        ),
                    )
                    if artifact.artifact_type == "research-report":
                        self._append_report_revision_db(
                            db, artifact.run_id, artifact.content
                        )
                    continue
                try:
                    stored_content = json.loads(existing["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    stored_content = None
                expected = (
                    artifact.run_id,
                    artifact.artifact_type,
                    artifact.title,
                    artifact.content,
                    artifact.model_id,
                    artifact.agent_id,
                )
                actual = (
                    existing["run_id"],
                    existing["artifact_type"],
                    existing["title"],
                    stored_content,
                    existing["model_id"],
                    existing["agent_id"],
                )
                if actual != expected:
                    raise RuntimeError("ARTIFACT_IDENTITY_CONFLICT")
                if artifact.artifact_type == "research-report":
                    self._append_report_revision_db(
                        db, artifact.run_id, artifact.content
                    )

    def interrupt_running_runs(
        self, reason: str = "应用在研究完成前退出；已保留完成阶段，可从同一研究记录继续"
    ) -> int:
        """Recover active runs as visible partial records after process exit."""
        completed_at = utc_now_iso()
        with self.connect() as db:
            rows = db.execute(
                "SELECT run_id, payload_json FROM research_runs WHERE status = ?",
                (RunStatus.RUNNING.value,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                errors = list(payload.get("errors", []))
                if reason not in errors:
                    errors.append(reason)
                payload["errors"] = errors
                payload["status"] = RunStatus.PARTIAL.value
                payload["completed_at"] = completed_at
                db.execute(
                    """
                    UPDATE research_runs
                    SET payload_json = ?, status = ?, completed_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        json.dumps(payload, ensure_ascii=False),
                        RunStatus.PARTIAL.value,
                        completed_at,
                        row["run_id"],
                    ),
                )
                job_rows = db.execute(
                    "SELECT job_id, snapshot_json FROM research_jobs WHERE run_id = ? AND state IN ('queued', 'running', 'cancelling')",
                    (row["run_id"],),
                ).fetchall()
                for job_row in job_rows:
                    try:
                        snapshot = json.loads(job_row["snapshot_json"])
                    except (TypeError, json.JSONDecodeError):
                        snapshot = {"job_id": job_row["job_id"], "run_id": row["run_id"]}
                    snapshot.update({
                        "state": "completed", "stage": "partial", "percent": 100,
                        "message": reason, "error_code": "PROCESS_INTERRUPTED",
                    })
                    db.execute(
                        "UPDATE research_jobs SET state = 'completed', stage = 'partial', snapshot_json = ?, updated_at = ? WHERE job_id = ?",
                        (json.dumps(snapshot, ensure_ascii=False), completed_at, job_row["job_id"]),
                    )
        return len(rows)

    def save_artifact(self, artifact: ResearchArtifact) -> None:
        encoded = json.dumps(
            artifact.content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.connect() as db:
            try:
                db.execute(
                    """
                    INSERT INTO artifacts(
                        artifact_id, run_id, artifact_type, title, payload_json,
                        model_id, agent_id, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id, artifact.run_id, artifact.artifact_type,
                        artifact.title, encoded, artifact.model_id, artifact.agent_id,
                        artifact.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = db.execute(
                    "SELECT run_id, artifact_type, title, payload_json, model_id, agent_id FROM artifacts WHERE artifact_id = ?",
                    (artifact.artifact_id,),
                ).fetchone()
                expected = (
                    artifact.run_id, artifact.artifact_type, artifact.title,
                    artifact.content, artifact.model_id, artifact.agent_id,
                )
                actual = None
                if existing is not None:
                    try:
                        stored_content = json.loads(existing["payload_json"])
                    except (TypeError, json.JSONDecodeError):
                        stored_content = None
                    actual = (
                        existing["run_id"], existing["artifact_type"], existing["title"],
                        stored_content, existing["model_id"], existing["agent_id"],
                    )
                if actual != expected:
                    raise RuntimeError("ARTIFACT_IDENTITY_CONFLICT") from exc
            if artifact.artifact_type == "research-report":
                self._append_report_revision_db(
                    db, artifact.run_id, artifact.content
                )

    def append_stage_attempt(self, attempt: "StageAttempt") -> None:
        payload = attempt.to_dict()
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS value FROM research_stage_attempts WHERE run_id = ? AND stage = ?",
                (payload["run_id"], payload["stage"]),
            ).fetchone()
            ordinal = max(int(payload["attempt"]), int(row["value"]) + 1)
            db.execute(
                """
                INSERT INTO research_stage_attempts(
                    run_id, stage, attempt, outcome, error_code, message,
                    diagnostics_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"], payload["stage"], ordinal,
                    payload["outcome"], payload["error_code"], payload["message"],
                    json.dumps(payload["diagnostics"], ensure_ascii=False),
                    payload["created_at"],
                ),
            )

    def save_research_job(self, snapshot: dict[str, Any]) -> None:
        run_id = str(snapshot.get("run_id") or "")
        if not run_id:
            return
        with self.connect() as db:
            if db.execute("SELECT 1 FROM research_runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                return
            db.execute(
                """
                INSERT INTO research_jobs(job_id, run_id, state, stage, snapshot_json, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    state=excluded.state,
                    stage=excluded.stage,
                    snapshot_json=excluded.snapshot_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(snapshot["job_id"]), run_id, str(snapshot.get("state", "queued")),
                    str(snapshot.get("stage", "preparing")),
                    json.dumps(snapshot, ensure_ascii=False), utc_now_iso(),
                ),
            )

    def get_research_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT snapshot_json FROM research_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def continuity_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT stage, attempt, outcome, error_code, message,
                       diagnostics_json, created_at
                FROM research_stage_attempts WHERE run_id = ? ORDER BY attempt_id
                """,
                (run_id,),
            ).fetchall()
        attempts: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["diagnostics"] = json.loads(item.pop("diagnostics_json"))
            except (TypeError, json.JSONDecodeError):
                item["diagnostics"] = {}
            attempts.append(item)
        return {"run_id": run_id, "attempts": attempts, "latest": attempts[-1] if attempts else None}

    def append_report_revision(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Append an immutable report revision and atomically advance latest."""

        with self.connect() as db:
            return self._append_report_revision_db(
                db,
                run_id,
                payload,
                expected_generation=expected_generation,
            )

    @staticmethod
    def _append_report_revision_db(
        db: sqlite3.Connection,
        run_id: str,
        payload: dict[str, Any],
        *,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """Append and advance a report revision inside the caller transaction."""

        import hashlib

        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        created_at = utc_now_iso()
        latest = db.execute(
            "SELECT revision_id, generation FROM latest_report_revisions WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        current_generation = int(latest["generation"]) if latest is not None else 0
        if expected_generation is not None and current_generation != expected_generation:
            raise RuntimeError("REPORT_REVISION_CONFLICT")
        existing = db.execute(
            "SELECT revision_id, generation, parent_revision_id, created_at FROM report_revisions WHERE run_id = ? AND content_sha256 = ?",
            (run_id, digest),
        ).fetchone()
        if existing is not None:
            return {
                **dict(existing),
                "run_id": run_id,
                "content_sha256": digest,
                "payload": payload,
            }
        generation = current_generation + 1
        parent = str(latest["revision_id"]) if latest is not None else None
        revision_id = f"{run_id}:report:{generation}:{digest[:16]}"
        db.execute(
            """
            INSERT INTO report_revisions(
                revision_id, run_id, parent_revision_id, generation,
                payload_json, content_sha256, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (revision_id, run_id, parent, generation, encoded, digest, created_at),
        )
        if latest is None:
            db.execute(
                "INSERT INTO latest_report_revisions(run_id, revision_id, generation) VALUES(?, ?, ?)",
                (run_id, revision_id, generation),
            )
        else:
            cursor = db.execute(
                """
                UPDATE latest_report_revisions SET revision_id = ?, generation = ?
                WHERE run_id = ? AND generation = ?
                """,
                (revision_id, generation, run_id, current_generation),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("REPORT_REVISION_CONFLICT")
        return {
            "revision_id": revision_id,
            "run_id": run_id,
            "parent_revision_id": parent,
            "generation": generation,
            "content_sha256": digest,
            "created_at": created_at,
            "payload": payload,
        }

    def latest_report_revision(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT r.* FROM latest_report_revisions l
                JOIN report_revisions r ON r.revision_id = l.revision_id
                WHERE l.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT r.run_id, r.status, r.started_at, r.completed_at,
                       c.ticker, c.name, r.payload_json
                FROM research_runs r
                JOIN companies c ON c.cik = r.company_cik
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT r.run_id, r.status, r.started_at, r.completed_at,
                       c.ticker, c.name, r.payload_json
                FROM research_runs r
                JOIN companies c ON c.cik = r.company_cik
                WHERE r.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_run(self, run_id: str) -> bool:
        """Delete one finished research record without deleting shared company data."""

        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM research_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] in {RunStatus.CREATED.value, RunStatus.RUNNING.value}:
                raise ValueError("an active research run cannot be deleted")
            db.execute(
                "UPDATE thesis_versions SET run_id = NULL WHERE run_id = ? AND created_by = 'user'",
                (run_id,),
            )
            db.execute(
                "DELETE FROM thesis_versions WHERE run_id = ?",
                (run_id,),
            )
            db.execute("DELETE FROM artifacts WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM financial_recovery_cases WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM research_jobs WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM research_stage_attempts WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM latest_report_revisions WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM report_revisions WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM research_runs WHERE run_id = ?", (run_id,))
        return True

    def get_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM artifacts
                WHERE run_id = ?
                ORDER BY created_at
                """,
                (run_id,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["content"] = json.loads(item.pop("payload_json"))
            results.append(item)
        return results

    def save_thesis_version(
        self,
        company_cik: str,
        content: dict[str, Any],
        *,
        run_id: str | None = None,
        created_by: str = "user",
        created_at: str,
    ) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM thesis_versions WHERE company_cik = ?",
                (company_cik,),
            ).fetchone()
            version = int(row["version"]) + 1
            thesis_version_id = f"{company_cik}:v{version}"
            db.execute(
                """
                INSERT INTO thesis_versions(
                    thesis_version_id, company_cik, run_id, version,
                    content_json, created_at, created_by
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thesis_version_id,
                    company_cik,
                    run_id,
                    version,
                    json.dumps(content, ensure_ascii=False),
                    created_at,
                    created_by,
                ),
            )
        return {
            "thesis_version_id": thesis_version_id,
            "company_cik": company_cik,
            "run_id": run_id,
            "version": version,
            "content": content,
            "created_at": created_at,
            "created_by": created_by,
        }

    def list_thesis_versions(
        self, company_cik: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "WHERE t.company_cik = ?" if company_cik else ""
        params: tuple[Any, ...] = (company_cik, limit) if company_cik else (limit,)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT t.*, c.ticker, c.name
                FROM thesis_versions t
                JOIN companies c ON c.cik = t.company_cik
                {where}
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["content"] = json.loads(item.pop("content_json"))
            results.append(item)
        return results

    def get_thesis_version(self, thesis_version_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT t.*, c.ticker, c.name
                FROM thesis_versions t
                JOIN companies c ON c.cik = t.company_cik
                WHERE t.thesis_version_id = ?
                """,
                (thesis_version_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        return result

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def save_market_snapshot(self, cache_key: str, snapshot: dict[str, Any], updated_at: str) -> None:
        """Atomically persist normalized market data, never raw provider data."""
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO market_snapshot_cache(cache_key, snapshot_json, updated_at) VALUES(?, ?, ?)",
                (cache_key, json.dumps(snapshot, ensure_ascii=False), updated_at),
            )

    def get_market_snapshot(self, cache_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT snapshot_json FROM market_snapshot_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(str(row["snapshot_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
