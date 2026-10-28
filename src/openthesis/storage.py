from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace, asdict, dataclass
import re
import math
import time
import threading
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any, Iterator, Sequence, TYPE_CHECKING, Callable, Mapping
from .financial_contracts import PDF_PARSER_VERSION
from .financial_xbrl_semantics import INLINE_PARSER_VERSION
from .storage_research_publication import (
    INPUT_FIELDS, ResearchPublicationConflict, ResearchPublicationSnapshot, bind_stage_heads,
    capture_snapshot, input_digest, validate_snapshot,
)
from .resilience.payloads import load_payload, store_payload, strict_loads

from .domain import (
    Company,
    CURRENT_DERIVED_VERSION,
    FilingDocument,
    FinancialFact,
    FinancialDimension,
    FinancialNormalizationLineage,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)


if TYPE_CHECKING:
    from .research_continuity import StageAttempt


SCHEMA_VERSION = 20
STARTUP_MIGRATION = "startup-read-models-v20-r7"


class StorageSchemaError(RuntimeError):
    """Do not let an older binary silently write a newer database schema."""


class StorageCorruptionError(RuntimeError):
    """A persisted financial fact cannot be safely reconstructed."""


@dataclass(frozen=True, slots=True)
class StorageReadResult:
    records: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, str], ...] = ()


def _storage_issue(exc: Exception, path: str, row_id: str, *, artifact: bool = False) -> dict[str, str]:
    supplied = str(exc) if isinstance(exc, (ValueError, StorageCorruptionError)) else ""
    code = supplied if re.fullmatch(r"(?:RECOVERY_PAYLOAD|FINANCIAL_FACT|FINANCIAL_CANDIDATE)_[A-Z0-9_]{1,70}", supplied) else "STORAGE_ROW_INVALID"
    return {"code": code, "path": path, "artifact_id" if artifact else "row_id": row_id}


_PARSING_TIMING_KEYS = frozenset({
    "source_hash_elapsed_seconds", "document_text_context_elapsed_seconds",
    "catalog_elapsed_seconds", "candidate_plan_elapsed_seconds", "pdf_open_elapsed_seconds",
    "page_text_cache_elapsed_seconds", "worker_session_open_elapsed_seconds", "worker_page_elapsed_seconds",
    "parse_elapsed_seconds", "source_collection_elapsed_seconds",
    "candidate_mapping_elapsed_seconds", "compiler_elapsed_seconds",
    "admission_elapsed_seconds", "candidate_mapping_seconds",
    "checkpoint_write_elapsed_seconds",
    "geometry_extract_elapsed_seconds",
})
_PARSING_COUNT_KEYS = frozenset({
    "page_count", "candidate_page_count", "completed_page_count", "worker_session_start_count",
    "geometry_cache_hits", "geometry_extracted_pages",
})


def _normalize_parsing_observation(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep source-bound parser diagnostics bounded, numeric, and non-sensitive."""
    required = ("accession_number", "period_end", "fiscal_period", "source_hash")
    if any(not isinstance(raw.get(key), str) or not raw.get(key) for key in required):
        raise ValueError("FINANCIAL_PARSING_OBSERVATION_INVALID")
    normalized: dict[str, Any] = {key: str(raw[key]) for key in required}
    for key in _PARSING_COUNT_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
            raise ValueError("FINANCIAL_PARSING_OBSERVATION_INVALID")
        normalized[key] = value
    timings = raw.get("stage_timings")
    if timings is not None:
        if not isinstance(timings, Mapping) or any(key not in _PARSING_TIMING_KEYS for key in timings):
            raise ValueError("FINANCIAL_PARSING_OBSERVATION_INVALID")
        safe_timings = {}
        for key, value in timings.items():
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or not 0 <= float(value) <= 86_400):
                raise ValueError("FINANCIAL_PARSING_OBSERVATION_INVALID")
            safe_timings[key] = round(float(value), 4)
        normalized["stage_timings"] = safe_timings
    for key, allowed in {
        "parse_strategy": {"positioned-page-checkpoint"},
        "backend": {"pdfplumber"},
    }.items():
        value = raw.get(key)
        if value is not None:
            if value not in allowed:
                raise ValueError("FINANCIAL_PARSING_OBSERVATION_INVALID")
            normalized[key] = value
    for key in ("cache_reused", "catalog_cache_hit"):
        if key in raw:
            if type(raw[key]) is not bool:
                raise ValueError("FINANCIAL_PARSING_OBSERVATION_INVALID")
            normalized[key] = raw[key]
    if 'local_structure' in raw:
        local = raw['local_structure']
        if not isinstance(local, Mapping):
            raise ValueError('FINANCIAL_PARSING_OBSERVATION_INVALID')
        safe_local = {}
        for key in ('ocr_calls','cache_hits','completed_pages','selected_pages','worker_start_count',
                    'model_load_seconds','inference_seconds','peak_memory_bytes','admitted_field_count','new_verified_field_count'):
            value = local.get(key)
            if value is None:
                continue
            limit = 274_877_906_944 if key == 'peak_memory_bytes' else 86_400 if key.endswith('_seconds') else 1_000_000
            if (isinstance(value, bool) or not isinstance(value, (int,float))
                    or (not key.endswith('_seconds') and not isinstance(value, int))
                    or not math.isfinite(value) or not 0 <= value <= limit):
                raise ValueError('FINANCIAL_PARSING_OBSERVATION_INVALID')
            safe_local[key] = value
        normalized['local_structure'] = safe_local
    return normalized

# One immutable description of the derived-data inputs.  Source records are
# never migrated in place; a changed input contract makes current reads miss
# until a new deterministic research pass writes the current version.
DERIVED_PIPELINE_CONTRACT = {
    "version": "financial-derived-pipeline-v1",
    "disclosure_identity": "disclosure-identity-v1",
    "parser": PDF_PARSER_VERSION,
    "inline_parser": INLINE_PARSER_VERSION,
    "structured_parser": "sec-companyfacts-v4",
    "structured_derivation": "exact-explicit-inputs-v2",
    "rules": "financial-rules-v1",
    "facts": CURRENT_DERIVED_VERSION,
    "validation": "financial-validation-v6",
    "report_projection": "report-projection-v1",
}


class Storage:
    def __init__(self, data_dir: Path, *, initialization_progress=None):
        from .model_activity import ModelActivityLedger
        self.model_activity = ModelActivityLedger()
        self._health_error = ""
        self._maintenance_started = False
        self._maintenance_lock = threading.Lock()
        self._initialization_progress = initialization_progress
        self.maintenance_status = {"state": "idle", "code": ""}
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.filings_dir = self.data_dir / "filings"
        self.filings_dir.mkdir(parents=True, exist_ok=True)
        self.packs_dir = self.data_dir / "research-packs"
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "openthesis.db"
        self._startup_progress("checking_schema")
        self.migration_backup: Path | None = self._prepare_schema_upgrade()
        self._initialize()
        self._startup_progress("storage_ready")

    def _startup_progress(self, stage, completed=None, total=None):
        if callable(self._initialization_progress):
            try:
                self._initialization_progress(stage, completed, total)
            except Exception:
                pass  # Display telemetry cannot fail a schema transaction.

    def _prepare_schema_upgrade(self) -> Path | None:
        """Validate and online-backup an existing DB before any schema write.

        Online Backup includes committed WAL data. Originals and artifacts are
        not rewritten, and backups are never replaced by a later startup.
        """
        if not self.db_path.exists():
            return None
        source = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            source.execute("PRAGMA query_only = ON")
            exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
            row = source.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() if exists else None
            try:
                version = int(row[0]) if row else 0
            except (ValueError, TypeError) as exc:
                raise StorageSchemaError("STORAGE_SCHEMA_VERSION_INVALID") from exc
            if version > SCHEMA_VERSION:
                raise StorageSchemaError("STORAGE_SCHEMA_NEWER_THAN_BINARY")
            if version == SCHEMA_VERSION:
                marker = source.execute("SELECT value FROM metadata WHERE key='startup_migration'").fetchone()
                if marker and marker[0] == STARTUP_MIGRATION:
                    return None
            self._startup_progress("checking_integrity")
            check = source.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise StorageSchemaError("STORAGE_INTEGRITY_CHECK_FAILED")
            directory = self.data_dir / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            backup_path = directory / f"pre-schema-{version}-to-{SCHEMA_VERSION}-{uuid.uuid4().hex}.db"
            target = sqlite3.connect(backup_path)
            try:
                self._startup_progress("backing_up")
                source.backup(target, pages=128, progress=lambda _status, remaining, total:
                              self._startup_progress("backing_up", total - remaining, total))
                if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise StorageSchemaError("STORAGE_BACKUP_INTEGRITY_FAILED")
            finally:
                target.close()
            return backup_path
        finally:
            source.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._health_error:
            raise StorageCorruptionError(self._health_error)
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            if self._health_error:
                raise StorageCorruptionError(self._health_error)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_background_maintenance(self) -> None:
        """Cooperative, read-only health work, outside the first-screen path."""
        with self._maintenance_lock:
            if self._maintenance_started:
                return
            self._maintenance_started = True
        def check():
            if not self.db_path.exists():
                return
            connection = None
            try:
                connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)
                connection.execute("PRAGMA query_only=ON")
                prior = connection.execute("SELECT value FROM metadata WHERE key='last_background_health_check'").fetchone()
                if prior and time.time() - float(prior[0]) < 86400:
                    self.maintenance_status = {"state": "recently_checked", "code": ""}
                    return
                self.maintenance_status = {"state": "checking", "code": ""}
                connection.set_progress_handler(lambda: (time.sleep(.001), 0)[1], 2000)
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    self._health_error = "STORAGE_HEALTH_RECOVERY_REQUIRED"
                    self.maintenance_status = {"state": "recovery_required", "code": self._health_error,
                        "next_action": "restore_verified_backup_or_export"}
                    return
                self.maintenance_status = {"state": "healthy", "code": ""}
                with self.connect() as writer:
                    writer.execute("INSERT OR REPLACE INTO metadata VALUES('last_background_health_check',?)", (str(time.time()),))
            except (sqlite3.Error, OSError, ValueError) as error:
                # Busy/IO failures are not proof of corruption. Never delete,
                # restore or quarantine history on a failed background probe.
                error_code = getattr(error, "sqlite_errorcode", None)
                if (isinstance(error_code, int)
                        and error_code & 0xff in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}):
                    self._health_error = "STORAGE_HEALTH_RECOVERY_REQUIRED"
                    self.maintenance_status = {"state": "recovery_required", "code": self._health_error,
                        "next_action": "restore_verified_backup_or_export"}
                else:
                    self.maintenance_status = {"state": "unavailable", "code": "STORAGE_HEALTH_CHECK_UNAVAILABLE"}
            finally:
                if connection is not None:
                    connection.close()
        timer = threading.Timer(2, check)
        timer.daemon = True
        timer.start()

    def _initialize(self) -> None:
        with self.connect() as db:
            try:
                metadata = dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('schema_version','startup_migration','derived_pipeline_contract')"))
            except sqlite3.OperationalError:
                metadata = {}
            if (metadata.get("schema_version") == str(SCHEMA_VERSION)
                    and metadata.get("startup_migration") == STARTUP_MIGRATION):
                # A derived-contract change invalidates reads; it is not a
                # reason to replay schema DDL or parse every old candidate.
                contract = json.dumps(DERIVED_PIPELINE_CONTRACT, sort_keys=True)
                if metadata.get("derived_pipeline_contract") != contract:
                    db.execute("INSERT OR REPLACE INTO metadata VALUES('derived_pipeline_contract',?)", (contract,))
                    db.execute("INSERT OR REPLACE INTO metadata VALUES('derived_rebuild_required','1')")
                return
            self._startup_progress("migrating_schema")
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
                BEGIN IMMEDIATE;
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
                    normalization_lineage_json TEXT,
                    classification_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    dimensions_json TEXT NOT NULL DEFAULT '[]',
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
                CREATE TABLE IF NOT EXISTS report_revision_contexts (
                    revision_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    context_sha256 TEXT NOT NULL,
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
                    "period_metadata_status": "TEXT NOT NULL DEFAULT ''",
                    "period_metadata_diagnostic": "TEXT NOT NULL DEFAULT ''",
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
                    "normalization_lineage_json": "TEXT",
                    "classification_evidence_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                    "dimensions_json": "TEXT NOT NULL DEFAULT '[]'",
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
            self._startup_progress("backfilling_facts")
            self._backfill_active_fact_generations(db)
            from .resilience.repository import SCHEMA_STATEMENTS

            for statement in SCHEMA_STATEMENTS:
                db.execute(statement)
            self._ensure_columns(db, "recovery_actions", {
                "response_received": "INTEGER NOT NULL DEFAULT 0",
                "response_receipt_json": "TEXT",
            })
            from .research_execution.durable import SCHEMA_STATEMENTS as STAGE_SCHEMA_STATEMENTS
            for statement in STAGE_SCHEMA_STATEMENTS:
                db.execute(statement)
            from .research_execution.requests import SCHEMA_STATEMENTS as REQUEST_SCHEMA_STATEMENTS
            for statement in REQUEST_SCHEMA_STATEMENTS:
                db.execute(statement)
            from .storage_read_models import initialize_read_models
            self._startup_progress("building_read_models")
            initialize_read_models(db)
            self._ensure_columns(db, "model_request_journal", {
                "response_received": "INTEGER NOT NULL DEFAULT 0",
                "receipt_json": "TEXT",
                "diagnostic_trace_id": "TEXT NOT NULL DEFAULT ''",
                "candidate_schema": "TEXT NOT NULL DEFAULT 'legacy'",
                "purpose": "TEXT NOT NULL DEFAULT ''",
                "authorization_id": "TEXT NOT NULL DEFAULT ''",
                "supersedes_json": "TEXT NOT NULL DEFAULT '[]'",
            })
            self._ensure_columns(db, "research_stage_attempts", {"action_id": "TEXT NOT NULL DEFAULT ''"})
            self._ensure_columns(db, "research_jobs", {"revision": "INTEGER NOT NULL DEFAULT 0"})
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
            # Last write in the same schema/backfill transaction. Interrupted
            # migrations roll back and cannot expose a partially ready DB.
            db.execute("INSERT OR REPLACE INTO metadata VALUES('startup_migration',?)", (STARTUP_MIGRATION,))

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
                    revision, supersedes_document_id, period_metadata_status, period_metadata_diagnostic
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        filing.period_metadata_status,
                        filing.period_metadata_diagnostic,
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
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
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
                WHERE vision_task_journal.company_cik=excluded.company_cik
                  AND vision_task_journal.document_id=excluded.document_id
                  AND vision_task_journal.provider=excluded.provider
                  AND vision_task_journal.page_hashes_json=excluded.page_hashes_json
                  AND vision_task_journal.page_numbers_json=excluded.page_numbers_json
                  AND (vision_task_journal.remote_task_id=''
                       OR excluded.remote_task_id=''
                       OR vision_task_journal.remote_task_id=excluded.remote_task_id)
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
            if cursor.rowcount != 1:
                raise ValueError("VISION_TASK_IDENTITY_CONFLICT")
            row = db.execute(
                "SELECT * FROM vision_task_journal WHERE task_key = ?", (task_key,)
            ).fetchone()
        payload = dict(row)
        payload["page_hashes"] = json.loads(payload.pop("page_hashes_json"))
        payload["page_numbers"] = json.loads(payload.pop("page_numbers_json"))
        return payload

    def claim_vision_task(
        self, task_key: str, *, company_cik: str, document_id: str,
        provider: str, page_hashes: Sequence[str], page_numbers: Sequence[int] = (),
    ) -> bool:
        """Atomically persist intent before POST; unknown outcomes never reset.

        Only an explicit remote rejection with no task id permits another
        claim. A crashed submitting worker cannot prove rejection merely by
        losing its lease, so elapsed time is deliberately not a reset rule.
        """
        if not all(isinstance(value, str) and value for value in
                   (task_key, company_cik, document_id, provider)):
            raise ValueError("VISION_TASK_IDENTITY_INVALID")
        hashes = json.dumps([str(value) for value in page_hashes], separators=(",", ":"))
        numbers = json.dumps([int(value) for value in page_numbers], separators=(",", ":"))
        now = utc_now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""INSERT OR IGNORE INTO vision_task_journal(
                task_key,company_cik,document_id,provider,page_hashes_json,page_numbers_json,
                status,updated_at) VALUES(?,?,?,?,?,?,'submitting',?)""",
                (task_key, company_cik, document_id, provider, hashes, numbers, now))
            if cursor.rowcount:
                return True
            cursor = db.execute("""UPDATE vision_task_journal
                SET status='submitting',error_code='',updated_at=?
                WHERE task_key=? AND company_cik=? AND document_id=? AND provider=?
                  AND page_hashes_json=? AND page_numbers_json=?
                  AND status='rejected' AND remote_task_id=''""",
                (now, task_key, company_cik, document_id, provider, hashes, numbers))
            return cursor.rowcount == 1

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
        expected_generations: dict[str, str] | None = None,
        source_failures: Sequence[Any] = (),
        cancel_check: Callable[[], bool] | None = None,
        fact_validation_records: Mapping[str, Any] | None = None,
        parsing_observations: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any] | None:
        """Atomically replace facts, evidence, and validation decisions.

        Rejected facts are retained with ``validation_status=REJECTED`` for
        auditability, while normal reads hide them.  The operation is scoped
        to the supplied accessions so prior research history is never deleted.
        """
        unique = sorted({value for value in accession_numbers if value})
        if expected_generations is not None and (
            not generation_id or not set(unique).issubset(expected_generations)
            or any(not isinstance(key, str) or not key or not isinstance(value, str)
                   for key, value in expected_generations.items())
        ):
            raise ValueError("FINANCIAL_PUBLICATION_SNAPSHOT_INVALID")
        with self.connect() as db:
            # A read-then-write on two deferred transactions is not CAS. Hold
            # SQLite's write lock while checking heads and publishing all rows.
            db.execute("BEGIN IMMEDIATE")
            if cancel_check is not None and cancel_check():
                raise CancelledError("financial publication cancelled")
            if generation_id and db.execute(
                "SELECT 1 FROM financial_ingestion_candidates WHERE generation_id=?",
                (generation_id,),
            ).fetchone() is not None:
                raise ValueError("FINANCIAL_GENERATION_ALREADY_EXISTS")
            changed_generations = []
            if expected_generations is not None:
                for accession, expected in expected_generations.items():
                    row = db.execute("SELECT generation_id FROM financial_active_generations "
                                     "WHERE company_cik=? AND accession_number=?",
                                     (company_cik, accession)).fetchone()
                    if str(row[0] if row else "") != expected:
                        changed_generations.append(accession)
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

                candidate_facts = (
                    *accepted_facts, *(quarantined_facts or ()), *audit_facts,
                )
                candidate_fact_ids = {str(fact.fact_id) for fact in candidate_facts}
                validation_payload: dict[str, dict[str, Any]] = {}
                if fact_validation_records is not None:
                    if not isinstance(fact_validation_records, Mapping):
                        raise ValueError("FINANCIAL_FACT_VALIDATION_RECORDS_INVALID")
                    for supplied_id, raw_record in fact_validation_records.items():
                        if not isinstance(supplied_id, str) or not supplied_id:
                            raise ValueError("FINANCIAL_FACT_VALIDATION_RECORD_INVALID")
                        # Records are intentionally scoped to facts in this
                        # candidate archive; unrelated compiler records are ignored.
                        if supplied_id not in candidate_fact_ids:
                            continue
                        to_dict = getattr(raw_record, "to_dict", None)
                        record = to_dict() if callable(to_dict) else raw_record
                        if not isinstance(record, Mapping):
                            raise ValueError("FINANCIAL_FACT_VALIDATION_RECORD_INVALID")
                        record_id = record.get("fact_id", supplied_id)
                        accepted = record.get("accepted")
                        issues = record.get("issues")
                        components = record.get("components")
                        dependencies = record.get("dependencies")
                        if (
                            record_id != supplied_id
                            or type(accepted) is not bool
                            or not isinstance(issues, (list, tuple))
                            or any(not isinstance(item, str) for item in issues)
                            or not isinstance(components, Mapping)
                            or not isinstance(dependencies, Mapping)
                        ):
                            raise ValueError("FINANCIAL_FACT_VALIDATION_RECORD_INVALID")
                        clean_components: dict[str, dict[str, Any]] = {}
                        for name, observation in components.items():
                            if not isinstance(name, str) or not name or not isinstance(observation, Mapping):
                                raise ValueError("FINANCIAL_FACT_VALIDATION_RECORD_INVALID")
                            state = observation.get("state")
                            evidence_ids = observation.get("evidence_ids")
                            if (
                                not isinstance(state, str)
                                or state not in {"observed", "unknown", "conflict", "verified"}
                                or "value" not in observation
                                or not isinstance(evidence_ids, (list, tuple))
                                or any(not isinstance(item, str) for item in evidence_ids)
                            ):
                                raise ValueError("FINANCIAL_FACT_VALIDATION_RECORD_INVALID")
                            clean_components[name] = {
                                "state": state,
                                "value": observation.get("value"),
                                "evidence_ids": list(evidence_ids),
                            }
                        clean_dependencies: dict[str, list[str]] = {}
                        for name, ids in dependencies.items():
                            if (
                                not isinstance(name, str) or not name
                                or not isinstance(ids, (list, tuple))
                                or any(not isinstance(item, str) for item in ids)
                            ):
                                raise ValueError("FINANCIAL_FACT_VALIDATION_RECORD_INVALID")
                            clean_dependencies[name] = list(ids)
                        normalized_record = {
                            "fact_id": supplied_id,
                            "accepted": accepted,
                            "issues": list(issues),
                            "components": clean_components,
                            "dependencies": clean_dependencies,
                        }
                        try:
                            json.dumps(normalized_record, ensure_ascii=False, allow_nan=False)
                        except (TypeError, ValueError) as exc:
                            raise ValueError("FINANCIAL_FACT_VALIDATION_RECORD_INVALID") from exc
                        validation_payload[supplied_id] = normalized_record

                observation_payload: list[dict[str, Any]] = []
                if not isinstance(parsing_observations, (list, tuple)):
                    raise ValueError("FINANCIAL_PARSING_OBSERVATIONS_INVALID")
                for raw_observation in parsing_observations:
                    if not isinstance(raw_observation, Mapping):
                        raise ValueError("FINANCIAL_PARSING_OBSERVATION_INVALID")
                    normalized_observation = _normalize_parsing_observation(raw_observation)
                    accession = normalized_observation["accession_number"]
                    period_end = normalized_observation["period_end"]
                    fiscal_period = normalized_observation["fiscal_period"]
                    source_hash = normalized_observation["source_hash"]
                    expected_hash = normalized_hashes.get(accession, "")
                    current_filing = db.execute(
                        "SELECT content_hash, period_end, fiscal_period FROM filings "
                        "WHERE company_cik=? AND accession_number=? "
                        "ORDER BY ingested_at DESC, document_id DESC LIMIT 1",
                        (company_cik, accession),
                    ).fetchone()
                    if (
                        accession not in unique or not expected_hash
                        or expected_hash != source_hash.casefold()
                        or current_filing is None
                        or not str(current_filing["content_hash"] or "")
                        or str(current_filing["content_hash"] or "").casefold() != source_hash.casefold()
                        or str(current_filing["period_end"] or "") != period_end
                        or str(current_filing["fiscal_period"] or "") != fiscal_period
                    ):
                        raise ValueError("FINANCIAL_PARSING_OBSERVATION_BINDING_INVALID")
                    normalized_observation["source_hash"] = source_hash.casefold()
                    observation_payload.append(normalized_observation)
                observed_accessions = {
                    item["accession_number"] for item in observation_payload
                }
                candidate_payload = {
                    "accepted_facts": [fact.to_dict() for fact in accepted_facts],
                    "quarantined_facts": [fact.to_dict() for fact in (quarantined_facts or ())],
                    "audit_facts": [fact.to_dict() for fact in audit_facts],
                    "evidence": [item.to_dict() for item in evidence],
                    "validation_groups": group_payload,
                    "source_failures": [asdict(item) for item in source_failures],
                    "fact_validation_records": validation_payload,
                    "parsing_observations": observation_payload,
                }
                diagnostics = list(dict.fromkeys(str(item) for item in candidate_diagnostics if item))

                if changed_generations:
                    # Preserve the candidate, but none of its dependent input
                    # cohort may replace a newer publication. A rejected CAS
                    # is not a repaired report and does not overwrite heads.
                    promotable.clear()
                    accession_states = {item: "stale_generation" for item in unique}
                    diagnostics.extend(f"{item}:financial_generation_changed" for item in sorted(changed_generations))

                for accession in unique:
                    if changed_generations:
                        continue
                    if complete_accessions is not None and accession not in complete_accessions:
                        accession_states[accession] = "incomplete"
                        incomplete_kind = (
                            "admission" if accession in observed_accessions else "parse"
                        )
                        diagnostics.append(f"{accession}:candidate_{incomplete_kind}_incomplete")
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
                    "stale_generation" if changed_generations
                    else "promoted" if len(promotable) == len(unique)
                    else "partially_promoted" if promotable
                    else "investigate" if "investigate" in accession_states.values()
                    else "incomplete"
                )
                db.execute(
                        "INSERT INTO financial_ingestion_candidates("
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

            prior_facts = []
            if generation_id:
                for accession in unique:
                    prior_facts.extend(self._fact_row(row) for row in db.execute(
                        "SELECT * FROM financial_facts WHERE company_cik=? AND accession_number=? "
                        "AND usage_status='canonical_research' AND provenance_status='verified' "
                        "AND COALESCE(validation_status, '')<>'REJECTED'",
                        (company_cik, accession)))

            for accession in unique:
                if cancel_check is not None and cancel_check():
                    raise CancelledError("financial publication cancelled")
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
            if cancel_check is not None and cancel_check():
                raise CancelledError("financial publication cancelled")
            if generation_id and prior_facts:
                # The correction record and promoted financial facts share
                # this transaction. A lost CAS or cancellation revokes nothing.
                from .report_content import financial_correction_invalidation
                for run_row in db.execute("SELECT run_id FROM research_runs WHERE company_cik=?", (company_cik,)):
                    run_id = str(run_row[0])
                    artifacts = [dict(row) for row in db.execute(
                        "SELECT artifact_id,run_id,artifact_type,agent_id,payload_json FROM artifacts "
                        "WHERE run_id=? ORDER BY rowid", (run_id,))]
                    for artifact_row in artifacts:
                        artifact_row["content"] = load_payload(db, artifact_row.pop("payload_json"))
                    invalidation = financial_correction_invalidation(artifacts, run_id=run_id,
                        company_cik=company_cik, old_facts=prior_facts, accepted_facts=accepted_facts,
                        validation_records=validation_payload, source_hashes=normalized_hashes,
                        generation_id=generation_id)
                    if invalidation is not None:
                        self._save_artifact_db(db, ResearchArtifact(
                            run_id + ":content-invalidation:" + invalidation["record_sha256"][:20], run_id,
                            "trusted-content-invalidation", "Source-confirmed financial correction",
                            invalidation, agent_id="trusted-financial-publication", created_at=utc_now_iso()))
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
            usage_status="comparator" if fact.usage_status == "comparator" else "canonical_research",
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
                generation_id, normalization_lineage_json,
                classification_evidence_ids_json, dimensions_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(
                        fact.to_dict()["normalization_lineage"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) if fact.normalization_lineage is not None else None,
                    json.dumps(
                        list(fact.classification_evidence_ids),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(fact.to_dict()["dimensions"], ensure_ascii=False, separators=(",", ":")),
                )
                for fact in facts
            ],
        )

    def get_facts(self, cik: str) -> list[dict[str, Any]]:
        return self.get_financial_research_snapshot(cik)["facts"]

    def get_financial_research_snapshot(self, cik: str, *, include_evidence: bool = False) -> dict[str, Any]:
        """Capture active rows, pointers and optionally their proofs in one view."""
        with self.connect() as db:
            db.execute("BEGIN")
            return self._financial_research_snapshot_db(db, cik, include_evidence=include_evidence)

    def _financial_research_snapshot_db(self, db: sqlite3.Connection, cik: str, *,
                                        include_evidence: bool = False) -> dict[str, Any]:
        generations = {
            str(row["accession_number"]): str(row["generation_id"])
            for row in db.execute(
                "SELECT accession_number, generation_id "
                "FROM financial_active_generations WHERE company_cik = ?", (cik,),
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
                      OR TRIM(COALESCE(l.reporting_currency, '')) = ''
                      OR COALESCE(f.currency, '') = ''
                      OR UPPER(f.currency) = UPPER(l.reporting_currency)
                  )
                ORDER BY fiscal_year DESC, concept
            """, (cik, CURRENT_DERIVED_VERSION),
        ).fetchall()
        # Internal preparers revalidate these active audit rows before model
        # use. They stay outside get_facts and the consolidated metric lane.
        # Inactive/candidate generations never enter this same-transaction view.
        disclosures = db.execute(
            """SELECT f.* FROM financial_facts f
               JOIN financial_active_generations g
                 ON g.company_cik = f.company_cik AND g.accession_number = f.accession_number
                    AND g.generation_id = f.generation_id
               WHERE f.company_cik = ? AND f.derived_version = ?
                 AND f.usage_status = 'audit_only' AND f.validation_status <> 'REJECTED'
                 AND (f.dimensions_json <> '[]' OR f.concept = 'shares_outstanding')
               ORDER BY f.fiscal_year DESC, f.concept, f.fact_id""",
            (cik, CURRENT_DERIVED_VERSION),
        ).fetchall()
        issues = []
        # Older accepted-write code flattened comparator usage into canonical
        # usage. Recover only an unambiguous, usable role from the existing
        # same-transaction validation ledger; amounts/provenance are untouched
        # and the preparer still revalidates the original evidence afterward.
        ledger_roles: dict[tuple, set[str]] = {}
        for group in db.execute("""SELECT accession_number,period_end,fiscal_period,
                consolidated_scope,currency,role FROM financial_validation_groups
                WHERE company_cik=? AND derived_version=? AND status IN ('VERIFIED','READY_WITH_WARNINGS')""",
                (cik, CURRENT_DERIVED_VERSION)).fetchall():
            identity = tuple(group[key] for key in ("accession_number", "period_end", "fiscal_period",
                                                    "consolidated_scope", "currency"))
            ledger_roles.setdefault(identity, set()).add(group["role"])
        def read_facts(items, bucket):
            accepted = []
            for row in items:
                try:
                    fact = self._fact_row(row)
                    identity = tuple(fact[key] for key in ("accession_number", "end_date", "fiscal_period",
                                                          "consolidated_scope", "currency"))
                    if fact["usage_status"] == "canonical_research" and ledger_roles.get(identity) == {"comparator"}:
                        fact["usage_status"] = "comparator"
                    accepted.append(fact)
                except (ValueError, TypeError, StorageCorruptionError) as exc:
                    issues.append(_storage_issue(exc, bucket + "/" + str(row["fact_id"]), str(row["fact_id"])))
            return accepted
        try:
            observations = self._candidate_parsing_observations_db(db, cik, issues=issues)
        except (ValueError, TypeError, StorageCorruptionError) as exc:
            observations = []
            issues.append(_storage_issue(exc, "parsing_observations", cik))
        snapshot = {
            "facts": read_facts(rows, "financial_facts"),
            "supplemental_facts": read_facts(disclosures, "supplemental_facts"),
            "generations": generations,
            "parsing_observations": observations,
            "storage_issues": issues,
        }
        if include_evidence:
            refs = db.execute("""SELECT e.* FROM financial_evidence e
                JOIN filings f ON f.document_id=e.document_id
                WHERE f.company_cik=? ORDER BY e.evidence_id""", (cik,)).fetchall()
            snapshot["evidence"] = []
            for row in refs:
                try:
                    snapshot["evidence"].append(self._financial_evidence_row(row))
                except (ValueError, TypeError) as exc:
                    issues.append(_storage_issue(exc, "financial_evidence/" + str(row["evidence_id"]), str(row["evidence_id"])))
        return snapshot

    @staticmethod
    def _candidate_parsing_observations_db(
        db: sqlite3.Connection, cik: str, *, issues: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return source-current parse observations without admitting candidates."""
        current_filings: dict[str, tuple[str, str, str]] = {}
        for row in db.execute(
            "SELECT accession_number, content_hash, period_end, fiscal_period FROM filings "
            "WHERE company_cik = ? ORDER BY ingested_at DESC, document_id DESC",
            (cik,),
        ).fetchall():
            accession = str(row["accession_number"] or "")
            if accession and accession not in current_filings:
                current_filings[accession] = (
                    str(row["content_hash"] or "").casefold(),
                    str(row["period_end"] or ""),
                    str(row["fiscal_period"] or ""),
                )

        resolved: set[str] = set()
        observations: list[dict[str, Any]] = []
        candidates = db.execute(
            "SELECT generation_id, state, source_hashes_json, candidate_json "
            "FROM financial_ingestion_candidates WHERE company_cik = ? "
            "ORDER BY created_at DESC, generation_id DESC",
            (cik,),
        ).fetchall()
        healthy_candidates = []
        for candidate in candidates:
            try:
                hashes = strict_loads(candidate["source_hashes_json"] or "{}")
                payload = strict_loads(candidate["candidate_json"] or "{}")
                if not isinstance(hashes, dict) or not isinstance(payload, dict):
                    raise ValueError("FINANCIAL_CANDIDATE_OBSERVATION_ARCHIVE_SHAPE_INVALID")
                observations_payload = payload.get("parsing_observations", [])
                if not isinstance(observations_payload, list):
                    raise ValueError("FINANCIAL_CANDIDATE_PARSING_OBSERVATIONS_SHAPE_INVALID")
                normalized_observations = []
                for item in observations_payload:
                    if not isinstance(item, dict):
                        raise ValueError("FINANCIAL_CANDIDATE_PARSING_OBSERVATION_SHAPE_INVALID")
                    normalized_observations.append(_normalize_parsing_observation(item))
                observations_payload = normalized_observations
                for bucket in ("accepted_facts", "quarantined_facts", "audit_facts"):
                    facts_payload = payload.get(bucket, ())
                    if not isinstance(facts_payload, list) or any(not isinstance(item, dict) for item in facts_payload):
                        raise ValueError("FINANCIAL_CANDIDATE_FACTS_SHAPE_INVALID")
                healthy_candidates.append(candidate)
            except (ValueError, TypeError, StorageCorruptionError) as exc:
                if issues is not None:
                    issues.append(_storage_issue(exc, "financial_ingestion_candidates/" + candidate["generation_id"], candidate["generation_id"]))
        for candidate in healthy_candidates:
            if str(candidate["state"] or "") == "stale_generation":
                continue
            try:
                source_hashes = strict_loads(candidate["source_hashes_json"] or "{}")
                payload = strict_loads(candidate["candidate_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StorageCorruptionError(
                    "FINANCIAL_CANDIDATE_OBSERVATION_ARCHIVE_JSON_INVALID"
                ) from exc
            if not isinstance(source_hashes, dict) or not isinstance(payload, dict):
                raise StorageCorruptionError(
                    "FINANCIAL_CANDIDATE_OBSERVATION_ARCHIVE_SHAPE_INVALID"
                )
            # Candidate archives written before parse observations existed
            # remain valid; absence means there is no archived observation.
            raw_observations = payload.get("parsing_observations", [])
            if not isinstance(raw_observations, list):
                raise StorageCorruptionError(
                    "FINANCIAL_CANDIDATE_PARSING_OBSERVATIONS_SHAPE_INVALID"
                )
            for bucket in ("accepted_facts", "quarantined_facts", "audit_facts"):
                raw_facts = payload.get(bucket, [])
                if not isinstance(raw_facts, list):
                    raise StorageCorruptionError(
                        "FINANCIAL_CANDIDATE_FACTS_SHAPE_INVALID"
                    )
                for fact in raw_facts:
                    if not isinstance(fact, dict):
                        raise StorageCorruptionError(
                            "FINANCIAL_CANDIDATE_FACT_SHAPE_INVALID"
                        )
            archive_observations: list[dict[str, Any]] = []
            for raw in raw_observations:
                if not isinstance(raw, dict):
                    raise StorageCorruptionError(
                        "FINANCIAL_CANDIDATE_PARSING_OBSERVATION_SHAPE_INVALID"
                    )
                try:
                    raw = _normalize_parsing_observation(raw)
                except ValueError as exc:
                    raise StorageCorruptionError(
                        "FINANCIAL_CANDIDATE_PARSING_OBSERVATION_SHAPE_INVALID"
                    ) from exc
                accession = raw["accession_number"]
                if accession in resolved:
                    continue
                current = current_filings.get(accession)
                if current is None:
                    continue
                archive_hash = str(source_hashes.get(accession, "")).casefold()
                source_hash = raw["source_hash"].casefold()
                if (
                    not current[0] or archive_hash != current[0] or source_hash != current[0]
                    or raw["period_end"] != current[1]
                    or raw["fiscal_period"] != current[2]
                ):
                    continue
                normalized_observation = dict(raw)
                normalized_observation["source_hash"] = source_hash
                archive_observations.append(normalized_observation)
            if archive_observations:
                resolved.update(item["accession_number"] for item in archive_observations)
                observations.extend(archive_observations)
        observations.sort(key=lambda item: (
            item["accession_number"], item["period_end"], item["fiscal_period"], item["source_hash"],
        ))
        return observations

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
        validation_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        candidate_payloads: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            generation_id = str(candidate["generation_id"] or "")
            try:
                payload = json.loads(candidate["candidate_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StorageCorruptionError(
                    "FINANCIAL_CANDIDATE_ARCHIVE_JSON_INVALID"
                ) from exc
            if not isinstance(payload, dict):
                raise StorageCorruptionError("FINANCIAL_CANDIDATE_ARCHIVE_SHAPE_INVALID")
            candidate_payloads[generation_id] = payload
            candidate_fact_ids: set[str] = set()
            for bucket in ("accepted_facts", "quarantined_facts", "audit_facts"):
                fact_items = payload.get(bucket)
                if not isinstance(fact_items, list):
                    raise StorageCorruptionError("FINANCIAL_CANDIDATE_FACTS_SHAPE_INVALID")
                for raw in fact_items:
                    if (
                        not isinstance(raw, dict)
                        or not isinstance(raw.get("fact_id"), str)
                        or not raw.get("fact_id")
                    ):
                        raise StorageCorruptionError("FINANCIAL_CANDIDATE_FACT_SHAPE_INVALID")
                    candidate_fact_ids.add(raw["fact_id"])
            records = payload.get("fact_validation_records", {})
            if not isinstance(records, dict):
                raise StorageCorruptionError(
                    "FINANCIAL_FACT_VALIDATION_RECORDS_SHAPE_INVALID"
                )
            for fact_id, raw_record in records.items():
                if not isinstance(fact_id, str) or not fact_id:
                    raise StorageCorruptionError(
                        "FINANCIAL_FACT_VALIDATION_RECORDS_SHAPE_INVALID"
                    )
                if fact_id not in candidate_fact_ids:
                    continue
                if not isinstance(raw_record, dict):
                    raise StorageCorruptionError(
                        "FINANCIAL_FACT_VALIDATION_RECORD_SHAPE_INVALID"
                    )
                accepted = raw_record.get("accepted")
                issues = raw_record.get("issues")
                components = raw_record.get("components")
                dependencies = raw_record.get("dependencies")
                if (
                    raw_record.get("fact_id") != fact_id
                    or type(accepted) is not bool
                    or not isinstance(issues, list)
                    or any(not isinstance(item, str) for item in issues)
                    or not isinstance(components, dict)
                    or not isinstance(dependencies, dict)
                ):
                    raise StorageCorruptionError(
                        "FINANCIAL_FACT_VALIDATION_RECORD_SHAPE_INVALID"
                    )
                for name, observation in components.items():
                    if (
                        not isinstance(name, str) or not name or not isinstance(observation, dict)
                        or not isinstance(observation.get("state"), str)
                        or observation.get("state") not in {"observed", "unknown", "conflict", "verified"}
                        or "value" not in observation
                        or not isinstance(observation.get("evidence_ids"), list)
                        or any(not isinstance(item, str) for item in observation.get("evidence_ids", ()))
                    ):
                        raise StorageCorruptionError(
                            "FINANCIAL_FACT_VALIDATION_RECORD_SHAPE_INVALID"
                        )
                if any(
                    not isinstance(name, str) or not name or not isinstance(ids, list)
                    or any(not isinstance(item, str) for item in ids)
                    for name, ids in dependencies.items()
                ):
                    raise StorageCorruptionError(
                        "FINANCIAL_FACT_VALIDATION_RECORD_SHAPE_INVALID"
                    )
                validation_by_identity[(fact_id, generation_id)] = dict(raw_record)
        for item in result:
            identity = (str(item.get("fact_id", "")), str(item.get("generation_id", "")))
            record = validation_by_identity.get(identity)
            if record is not None:
                item["fact_validation_record"] = record
        seen = {
            (str(item.get("fact_id", "")), str(item.get("generation_id", "")))
            for item in result
        }
        # Candidate generations are deliberately excluded from normal research
        # reads. Keep their facts inspectable through the audit view without
        # materializing or overwriting the currently active generation.
        for candidate in candidates:
            generation_id = str(candidate["generation_id"] or "")
            payload = candidate_payloads[generation_id]
            for bucket, usage_status in (
                ("accepted_facts", "candidate_audit"),
                ("quarantined_facts", "quarantined"),
                ("audit_facts", "audit_only"),
            ):
                fact_items = payload[bucket]
                for raw in fact_items:
                    fact = dict(raw)
                    lineage_payload = fact.get("normalization_lineage")
                    if lineage_payload is not None and not self._valid_lineage_payload(
                        lineage_payload
                    ):
                        raise StorageCorruptionError(
                            "FINANCIAL_FACT_NORMALIZATION_LINEAGE_SHAPE_INVALID"
                        )
                    classification_ids = fact.get("classification_evidence_ids", [])
                    if (
                        not isinstance(classification_ids, (list, tuple))
                        or any(not isinstance(item, str) for item in classification_ids)
                    ):
                        raise StorageCorruptionError(
                            "FINANCIAL_FACT_CLASSIFICATION_EVIDENCE_SHAPE_INVALID"
                        )
                    fact["classification_evidence_ids"] = tuple(classification_ids)
                    fact["dimensions"] = self._dimension_payload(fact.get("dimensions", []))
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
                    validation_record = validation_by_identity.get(identity)
                    if validation_record is not None:
                        fact["fact_validation_record"] = validation_record
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
    def _dimension_payload(payload: Any) -> tuple[dict[str, Any], ...]:
        """One shape contract for active rows and persisted candidate audits."""
        try:
            if not isinstance(payload, list) or len(payload) > 32:
                raise ValueError("dimension_shape")
            parsed = tuple(FinancialDimension.from_value(value) for value in payload)
            if len({value.axis for value in parsed}) != len(parsed):
                raise ValueError("duplicate_dimension")
        except (TypeError, ValueError) as exc:
            raise StorageCorruptionError("FINANCIAL_FACT_DIMENSIONS_INVALID") from exc
        return tuple(asdict(value) for value in parsed)

    @staticmethod
    def _valid_lineage_payload(payload: Any) -> bool:
        def valid_node(node: Any) -> bool:
            if not isinstance(node, dict):
                return False
            string_fields = (
                "raw_value", "scale", "normalized_value", "quantity_kind", "currency",
            )
            if any(not isinstance(node.get(field), str) for field in string_fields):
                return False
            if any(
                field in node and not isinstance(node[field], str)
                for field in ("method", "value_transform")
            ):
                return False
            evidence_ids = node.get("unit_evidence_ids")
            inputs = node.get("inputs")
            return (
                isinstance(evidence_ids, list)
                and all(isinstance(item, str) for item in evidence_ids)
                and isinstance(inputs, list)
                and all(valid_node(item) for item in inputs)
            )

        return valid_node(payload)

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

        lineage_json = item.pop("normalization_lineage_json", None)
        if lineage_json is None:
            item["normalization_lineage"] = None
        else:
            try:
                lineage_payload = strict_loads(str(lineage_json))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StorageCorruptionError(
                    "FINANCIAL_FACT_NORMALIZATION_LINEAGE_JSON_INVALID"
                ) from exc
            if lineage_payload is not None and (
                not Storage._valid_lineage_payload(lineage_payload)
                or FinancialNormalizationLineage.from_value(lineage_payload) is None
            ):
                raise StorageCorruptionError(
                    "FINANCIAL_FACT_NORMALIZATION_LINEAGE_SHAPE_INVALID"
                )
            item["normalization_lineage"] = lineage_payload

        classification_json = item.pop("classification_evidence_ids_json", None)
        try:
            classification_payload = strict_loads(str(classification_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageCorruptionError(
                "FINANCIAL_FACT_CLASSIFICATION_EVIDENCE_JSON_INVALID"
            ) from exc
        if (
            not isinstance(classification_payload, list)
            or any(not isinstance(evidence_id, str) for evidence_id in classification_payload)
        ):
            raise StorageCorruptionError(
                "FINANCIAL_FACT_CLASSIFICATION_EVIDENCE_SHAPE_INVALID"
            )
        item["classification_evidence_ids"] = tuple(classification_payload)
        dimensions_json = item.pop("dimensions_json", "[]")
        try:
            dimensions = strict_loads(dimensions_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageCorruptionError("FINANCIAL_FACT_DIMENSIONS_INVALID") from exc
        item["dimensions"] = Storage._dimension_payload(dimensions)
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
            failures = []
            for key in ("issues_json", "covered_concepts_json"):
                try:
                    decoded = strict_loads(item.pop(key))
                    if not isinstance(decoded, list) or (key == "covered_concepts_json" and any(not isinstance(value, str) for value in decoded)):
                        raise ValueError("STORAGE_VALIDATION_GROUP_FIELD_INVALID")
                    item[key.removesuffix("_json")] = decoded
                except (TypeError, ValueError):
                    item[key.removesuffix("_json")] = []
                    failures.append({"code": "STORAGE_VALIDATION_GROUP_FIELD_INVALID", "path": key.removesuffix("_json")})
            if failures:
                item["status"] = "UNVERIFIABLE"
                item["covered_concepts"] = []
                item["issues"] = [*item.get("issues", []), *failures]
                item["storage_issues"] = failures
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
        return [self._financial_evidence_row(row) for row in rows]

    @staticmethod
    def _financial_evidence_row(row) -> dict[str, Any]:
        item = dict(row)
        encoded = item.pop("bbox_json", None)
        if encoded:
            coordinates = strict_loads(encoded)
            if not isinstance(coordinates, list) or len(coordinates) != 4 or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) for value in coordinates
            ):
                raise ValueError("FINANCIAL_FACT_EVIDENCE_LOCATOR_INVALID")
            item["bbox"] = tuple(float(value) for value in coordinates)
        else:
            item["bbox"] = None
        return item

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
        self, run: ResearchRun, artifacts: list[ResearchArtifact], *,
        expected_run_digest: str | None = None,
        expected_financial_snapshot: tuple[str, dict[str, Any]] | None = None,
        expected_report_generation: int | None = None,
        preserve_existing_payload: bool = False,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        """Persist a run snapshot and new immutable artifacts atomically.

        Artifact identifiers are content identities, not overwrite slots.  A
        repeated identical write is idempotent; reusing an identifier for
        different content is a storage-integrity error.
        """
        payload = run.to_dict()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if cancel_check is not None and cancel_check():
                raise CancelledError("report publication cancelled")
            existing_run = None
            if expected_run_digest is not None or preserve_existing_payload:
                row = db.execute("SELECT payload_json FROM research_runs WHERE run_id=?", (run.run_id,)).fetchone()
                existing_run = json.loads(row["payload_json"]) if row else None
            if expected_run_digest is not None:
                encoded = json.dumps(existing_run, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
                if existing_run is None or hashlib.sha256(encoded).hexdigest() != expected_run_digest:
                    raise RuntimeError("RESEARCH_RUN_SNAPSHOT_CONFLICT")
            if expected_financial_snapshot is not None:
                key, captured = expected_financial_snapshot
                if self._financial_research_snapshot_db(
                        db, key, include_evidence="evidence" in captured) != captured:
                    raise RuntimeError("FINANCIAL_GENERATION_CONFLICT")
            if expected_report_generation is not None:
                head = db.execute("SELECT generation FROM latest_report_revisions WHERE run_id=?", (run.run_id,)).fetchone()
                if (int(head["generation"]) if head else 0) != expected_report_generation:
                    raise RuntimeError("REPORT_REVISION_CONFLICT")
            if preserve_existing_payload and isinstance(existing_run, dict):
                payload = {**existing_run, **payload}
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
                self._save_artifact_db(db, artifact)

            if cancel_check is not None and cancel_check():
                raise CancelledError("report publication cancelled")

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

    @staticmethod
    def _canonical_artifact_json(content: Any) -> str:
        """Encode artifact content with strict, deterministic value identity."""

        return json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _save_artifact_db(self, db: sqlite3.Connection, artifact: ResearchArtifact) -> None:
        """Insert or verify one immutable artifact within an existing transaction."""

        encoded = self._canonical_artifact_json(artifact.content)
        stored_encoded = (store_payload(db, artifact.content)
            if artifact.artifact_type in {"stage-candidate", "stage-checkpoint", "stage-content-flow", "agent-analysis",
                "research-report", "trusted-content-snapshot"} else encoded)
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
                    artifact_id, run_id, artifact_type, title, payload_json,
                    model_id, agent_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id, artifact.run_id, artifact.artifact_type,
                    artifact.title, stored_encoded, artifact.model_id, artifact.agent_id,
                    artifact.created_at,
                ),
            )
        else:
            try:
                stored_content = load_payload(db, existing["payload_json"])
                stored_encoded = self._canonical_artifact_json(stored_content)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("ARTIFACT_IDENTITY_CONFLICT") from exc
            if (
                existing["run_id"], existing["artifact_type"], existing["title"],
                stored_encoded, existing["model_id"], existing["agent_id"],
            ) != (
                artifact.run_id, artifact.artifact_type, artifact.title,
                encoded, artifact.model_id, artifact.agent_id,
            ):
                raise RuntimeError("ARTIFACT_IDENTITY_CONFLICT")
        if artifact.artifact_type == "research-report":
            self._append_report_revision_db(db, artifact.run_id, artifact.content)
        if existing is None and artifact.artifact_type in {"research-report", "stage-checkpoint"}:
            from .report_content import trusted_snapshot_for_artifact, read_trusted_snapshots, artifacts_for_trusted_input
            evidence_row = db.execute("""SELECT payload_json FROM artifacts
                WHERE run_id=? AND artifact_type='deterministic-financial-summary'
                ORDER BY rowid DESC LIMIT 1""", (artifact.run_id,)).fetchone()
            read_issues = []
            try:
                evidence_payload = load_payload(db, evidence_row["payload_json"]) if evidence_row else {}
                evidence_records = evidence_payload.get("evidence", []) if isinstance(evidence_payload, dict) else []
            except (ValueError, TypeError) as exc:
                evidence_records = []
                read_issues.append(_storage_issue(exc, "evidence-artifact", artifact.run_id))
            retained_nodes = None
            if artifact.content.get("mode") == "progressive-local":
                originals = [dict(row) for row in db.execute(
                    "SELECT artifact_id,run_id,artifact_type,payload_json FROM artifacts WHERE run_id=? AND artifact_id<>? ORDER BY rowid",
                    (artifact.run_id, artifact.artifact_id))]
                healthy_originals = []
                for row in originals:
                    try:
                        row["content"] = load_payload(db, row.pop("payload_json"))
                        healthy_originals.append(row)
                    except (ValueError, TypeError) as exc:
                        read_issues.append(_storage_issue(exc, "artifacts/" + row["artifact_id"], row["artifact_id"], artifact=True))
                retained_nodes = read_trusted_snapshots(artifacts_for_trusted_input(healthy_originals,
                    artifact.content.get("publication_input_digest", "")), run_id=artifact.run_id)
            if read_issues:
                self._save_artifact_db(db, ResearchArtifact(
                    artifact.run_id + ":storage-read-diagnostic:" + hashlib.sha256(json.dumps(read_issues, sort_keys=True).encode()).hexdigest()[:20],
                    artifact.run_id, "storage-read-diagnostic", "Storage read issues", {"issues": read_issues},
                    agent_id="storage-read"))
            snapshot = trusted_snapshot_for_artifact(artifact.to_dict(), evidence_records=evidence_records,
                                                     retained_nodes=retained_nodes)
            if artifact.content.get("mode") == "progressive-local" and snapshot is None:
                raise ValueError("RESEARCH_PROGRESS_ORIGIN_PROOF_MISSING")
            if snapshot is not None:
                self._save_artifact_db(db, ResearchArtifact(
                    artifact_id=artifact.run_id + ":trusted-content-snapshot:" + snapshot["snapshot_sha256"][:20],
                    run_id=artifact.run_id, artifact_type="trusted-content-snapshot",
                    title=artifact.title, content=snapshot, model_id=artifact.model_id,
                    agent_id="trusted-content-commit", created_at=artifact.created_at))

    @staticmethod
    def _append_stage_attempt_db(db: sqlite3.Connection, attempt: "StageAttempt") -> None:
        payload = attempt.to_dict()
        row = db.execute(
            "SELECT COALESCE(MAX(attempt), 0) AS value FROM research_stage_attempts "
            "WHERE run_id = ? AND stage = ?",
            (payload["run_id"], payload["stage"]),
        ).fetchone()
        ordinal = max(int(payload["attempt"]), int(row["value"]) + 1)
        db.execute(
            """
            INSERT INTO research_stage_attempts(
                run_id, stage, attempt, outcome, error_code, message,
                diagnostics_json, created_at, action_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["run_id"], payload["stage"], ordinal,
                payload["outcome"], payload["error_code"], payload["message"],
                json.dumps(payload["diagnostics"], ensure_ascii=False, allow_nan=False),
                payload["created_at"], payload.get("action_id", ""),
            ),
        )

    def save_artifact(self, artifact: ResearchArtifact) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._save_artifact_db(db, artifact)

    def append_stage_attempt(self, attempt: "StageAttempt") -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._append_stage_attempt_db(db, attempt)

    def save_run_start(self, run: ResearchRun, attempt: "StageAttempt") -> None:
        """Create an identity and its initial checkpoint, never reset an old run."""
        if run.status is not RunStatus.RUNNING or attempt.run_id != run.run_id:
            raise ValueError("RESEARCH_RUN_START_INVALID")
        encoded = self._canonical_artifact_json(run.to_dict())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM research_runs WHERE run_id=?", (run.run_id,)).fetchone():
                raise RuntimeError("RESEARCH_RUN_ALREADY_STARTED")
            db.execute("""INSERT INTO research_runs(
                run_id,company_cik,payload_json,status,started_at,completed_at)
                VALUES(?,?,?,?,?,?)""", (run.run_id, run.company.cik, encoded,
                    run.status.value, run.started_at, run.completed_at))
            self._append_stage_attempt_db(db, attempt)

    @staticmethod
    def _lifecycle_run_db(db: sqlite3.Connection, run_id: str) -> tuple[dict[str, Any], sqlite3.Row]:
        row = db.execute(
            "SELECT company_cik,payload_json,status,completed_at FROM research_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise StorageCorruptionError("CONTINUITY_RUN_NOT_FOUND")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageCorruptionError("CONTINUITY_RUN_PAYLOAD_INVALID") from exc
        valid_statuses = {item.value for item in RunStatus}
        if (not isinstance(payload, dict) or payload.get("run_id") != run_id
                or not isinstance(payload.get("company"), dict)
                or payload["company"].get("cik") != row["company_cik"]
                or not isinstance(payload.get("errors"), list)
                or payload.get("status") not in valid_statuses
                or row["status"] not in valid_statuses or payload["status"] != row["status"]):
            raise StorageCorruptionError("CONTINUITY_RUN_PAYLOAD_INVALID")
        return payload, row

    def _write_lifecycle_run_db(self, db: sqlite3.Connection, run_id: str, payload: dict[str, Any]) -> None:
        db.execute("UPDATE research_runs SET payload_json=?,status=?,completed_at=? WHERE run_id=?",
            (self._canonical_artifact_json(payload), payload["status"], payload.get("completed_at"), run_id))

    def save_run_stage_result(
        self, run_id: str, attempt: "StageAttempt", *, snapshot_updates: dict[str, Any] | None = None,
        status: RunStatus | None = None, error: str = "", completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Patch only specified lifecycle fields on the latest persisted payload.

        This is not full research publication and cannot promote a run to
        COMPLETED. Domain operations supply typed checkpoints; persistence
        keeps unrelated captured inputs and unknown legacy extensions intact.
        """
        if (not isinstance(run_id, str) or not run_id.strip() or attempt.run_id != run_id
                or (snapshot_updates is not None and not isinstance(snapshot_updates, dict))
                or status not in {None, RunStatus.PARTIAL, RunStatus.CANCELLED, RunStatus.FAILED}
                or not isinstance(error, str)):
            raise ValueError("RESEARCH_RUN_STAGE_RESULT_INVALID")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload, row = self._lifecycle_run_db(db, run_id)
            if snapshot_updates is not None:
                snapshot = payload.get("data_snapshot", {})
                if not isinstance(snapshot, dict):
                    raise StorageCorruptionError("CONTINUITY_RUN_SNAPSHOT_INVALID")
                payload["data_snapshot"] = {**snapshot, **snapshot_updates}
            if status is not None and row["status"] not in {RunStatus.COMPLETED.value, RunStatus.CANCELLED.value}:
                payload["status"], payload["completed_at"] = status.value, completed_at
            if error and error not in payload["errors"]:
                payload["errors"].append(error)
            self._append_stage_attempt_db(db, attempt)
            self._write_lifecycle_run_db(db, run_id, payload)
            return payload

    def capture_research_publication(self, run: ResearchRun) -> ResearchPublicationSnapshot:
        """Capture before analysis; a late worker cannot adopt changed inputs."""
        with self.connect() as db:
            db.execute("BEGIN")
            payload, _ = self._lifecycle_run_db(db, run.run_id)
            if input_digest(payload) != input_digest(run.to_dict()):
                raise ResearchPublicationConflict("RESEARCH_PUBLICATION_INPUT_CONFLICT")
            return capture_snapshot(db, payload)

    def begin_research_publication(
        self, run: ResearchRun, *, expected: ResearchPublicationSnapshot | None,
    ) -> ResearchPublicationSnapshot:
        """Start this execution without destroying a newer persisted envelope."""
        if run.status is not RunStatus.RUNNING:
            raise ValueError("RESEARCH_PUBLICATION_START_INVALID")
        proposed = run.to_dict()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if expected is None:
                if db.execute("SELECT 1 FROM research_runs WHERE run_id=?", (run.run_id,)).fetchone():
                    raise ResearchPublicationConflict("RESEARCH_PUBLICATION_INPUT_CONFLICT")
                payload = proposed
                db.execute("""INSERT INTO research_runs(
                    run_id,company_cik,payload_json,status,started_at,completed_at)
                    VALUES(?,?,?,?,?,?)""", (run.run_id, run.company.cik,
                        self._canonical_artifact_json(payload), run.status.value,
                        run.started_at, run.completed_at))
            else:
                payload, _ = self._lifecycle_run_db(db, run.run_id)
                if expected.run_id != run.run_id:
                    raise ResearchPublicationConflict("RESEARCH_PUBLICATION_INPUT_CONFLICT")
                validate_snapshot(db, payload, expected)
                if payload.get("company", {}).get("cik") != run.company.cik:
                    raise ResearchPublicationConflict("RESEARCH_PUBLICATION_INPUT_CONFLICT")
                payload.update({key: proposed[key] for key in INPUT_FIELDS})
                payload["status"] = run.status.value
                # Persisted errors/extensions belong to the latest envelope.
                self._write_lifecycle_run_db(db, run.run_id, payload)
            return capture_snapshot(db, payload)

    def update_research_model(
        self, snapshot: ResearchPublicationSnapshot, *, provider_id: str, model_id: str,
        model_configuration: dict[str, Any],
    ) -> ResearchPublicationSnapshot:
        """Capture an explicitly selected resume model before graph preparation."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload, _ = self._lifecycle_run_db(db, snapshot.run_id)
            validate_snapshot(db, payload, snapshot)
            if payload["status"] == RunStatus.CANCELLED.value:
                raise CancelledError("research publication cancelled")
            payload.update(provider_id=provider_id, model_id=model_id,
                           model_configuration=model_configuration)
            self._write_lifecycle_run_db(db, snapshot.run_id, payload)
            return capture_snapshot(db, payload)

    def update_research_definition(
        self, snapshot: ResearchPublicationSnapshot, *, workflow_id: str,
        research_configuration: dict[str, Any],
    ) -> ResearchPublicationSnapshot:
        """Bind an OT graph without resetting captured data or unknown fields."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload, _ = self._lifecycle_run_db(db, snapshot.run_id)
            validate_snapshot(db, payload, snapshot)
            payload["workflow_id"] = workflow_id
            payload["research_configuration"] = research_configuration
            self._write_lifecycle_run_db(db, snapshot.run_id, payload)
            return capture_snapshot(db, payload)

    def bind_research_publication_stages(
        self, snapshot: ResearchPublicationSnapshot, generations: dict[str, int],
    ) -> ResearchPublicationSnapshot:
        with self.connect() as db:
            db.execute("BEGIN")
            payload, _ = self._lifecycle_run_db(db, snapshot.run_id)
            return bind_stage_heads(db, payload, snapshot, generations)

    def publish_research_result(
        self, snapshot: ResearchPublicationSnapshot, artifact: ResearchArtifact,
        attempt: "StageAttempt", *, status: RunStatus, completed_at: str,
        cancel_check: Callable[[], bool], thesis_content: dict[str, Any] | None = None,
        thesis_created_by: str = "",
        thesis_artifact_factory: Callable[[dict[str, Any]], ResearchArtifact] | None = None,
    ) -> dict[str, Any]:
        """One commit owns outcome, report, revision, attempt and optional thesis.

        Validators belong to the workflow. This repository enforces the frozen
        input/head contract and never republishes a stale worker's full payload.
        """
        from .research_continuity import StageOutcome
        if (artifact.run_id != snapshot.run_id
                or artifact.artifact_type not in {"research-report", "stage-outcome"}
                or attempt.run_id != snapshot.run_id
                or status not in {RunStatus.COMPLETED, RunStatus.PARTIAL}
                or (status is RunStatus.COMPLETED and (
                    artifact.artifact_type != "research-report"
                    or attempt.outcome is not StageOutcome.COMPLETE
                    or artifact.content.get("verification", {}).get("passed") is not True
                    or artifact.content.get("readiness", {}).get("complete") is not True))
                or (status is RunStatus.PARTIAL and attempt.outcome is StageOutcome.COMPLETE)
                or (thesis_content is not None and thesis_artifact_factory is None)):
            raise ValueError("RESEARCH_PUBLICATION_RESULT_INVALID")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload, row = self._lifecycle_run_db(db, snapshot.run_id)
            if cancel_check() or row["status"] == RunStatus.CANCELLED.value:
                raise CancelledError("research publication cancelled")
            validate_snapshot(db, payload, snapshot)
            payload["status"], payload["completed_at"] = status.value, completed_at
            self._write_lifecycle_run_db(db, snapshot.run_id, payload)
            self._save_artifact_db(db, artifact)
            if artifact.artifact_type == "research-report":
                head = db.execute("""SELECT r.content_sha256 FROM latest_report_revisions l
                    JOIN report_revisions r ON r.revision_id=l.revision_id WHERE l.run_id=?""",
                    (snapshot.run_id,)).fetchone()
                expected_body = hashlib.sha256(self._canonical_artifact_json(
                    artifact.content).encode("utf-8")).hexdigest()
                # An old identical artifact may be deduplicated for ordinary
                # history writes, but that must not masquerade as current
                # publication while a different body still owns the head.
                if head is None or head["content_sha256"] != expected_body:
                    raise ResearchPublicationConflict("RESEARCH_PUBLICATION_REVISION_CONFLICT")
            if thesis_content is not None:
                thesis = self._save_thesis_version_db(db, payload["company"]["cik"], thesis_content,
                    run_id=snapshot.run_id, created_by=thesis_created_by, created_at=completed_at)
                thesis_artifact = thesis_artifact_factory(thesis)
                if (not isinstance(thesis_artifact, ResearchArtifact)
                        or thesis_artifact.run_id != snapshot.run_id
                        or thesis_artifact.artifact_type != "thesis-snapshot"):
                    raise ValueError("RESEARCH_PUBLICATION_THESIS_INVALID")
                self._save_artifact_db(db, thesis_artifact)
            self._append_stage_attempt_db(db, attempt)
            if cancel_check():
                raise CancelledError("research publication cancelled")
            return payload

    def publish_research_progress(
        self, snapshot: ResearchPublicationSnapshot, artifact: ResearchArtifact, *,
        cancel_check: Callable[[], bool],
    ) -> ResearchPublicationSnapshot:
        """CAS-append a trusted local revision without completing the run."""
        from .report_content import trusted_snapshot_for_artifact
        if (artifact.run_id != snapshot.run_id or artifact.artifact_type != "research-report"
                or artifact.content.get("mode") != "progressive-local"
                or artifact.content.get("readiness", {}).get("complete") is not False
                or trusted_snapshot_for_artifact(artifact.to_dict()) is None):
            raise ValueError("RESEARCH_PROGRESS_CONTRACT_INVALID")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload, row = self._lifecycle_run_db(db, snapshot.run_id)
            if cancel_check() or row["status"] == RunStatus.CANCELLED.value:
                raise CancelledError("research publication cancelled")
            validate_snapshot(db, payload, snapshot)
            self._save_artifact_db(db, artifact)
            if cancel_check():
                raise CancelledError("research publication cancelled")
            return capture_snapshot(db, payload)

    def save_continuity_result(
        self,
        run_id: str,
        artifact: ResearchArtifact,
        attempt: "StageAttempt",
        *,
        message: str,
        completed_at: str | None,
    ) -> dict[str, Any]:
        """Atomically persist one continuity notice, attempt and run update."""

        if (
            not isinstance(run_id, str) or not run_id.strip()
            or not isinstance(artifact, ResearchArtifact)
            or artifact.run_id != run_id
            or artifact.artifact_type != "continuity-notice"
            or not callable(getattr(attempt, "to_dict", None))
        ):
            raise ValueError("CONTINUITY_RESULT_INVALID")
        attempt_payload = attempt.to_dict()
        if not isinstance(attempt_payload, dict) or attempt_payload.get("run_id") != run_id:
            raise ValueError("CONTINUITY_RESULT_INVALID")
        if not isinstance(message, str):
            raise ValueError("CONTINUITY_RESULT_INVALID")

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload, row = self._lifecycle_run_db(db, run_id)

            errors = list(payload["errors"])
            if message and message not in errors:
                errors.append(message)
            payload["errors"] = errors
            status = str(row["status"])
            if status not in {RunStatus.COMPLETED.value, RunStatus.CANCELLED.value}:
                status = RunStatus.PARTIAL.value
                stored_completed_at = completed_at
            else:
                stored_completed_at = row["completed_at"]
            payload["status"] = status
            payload["completed_at"] = stored_completed_at

            self._save_artifact_db(db, artifact)
            self._append_stage_attempt_db(db, attempt)
            self._write_lifecycle_run_db(db, run_id, payload)
            return payload

    def model_call_status(self, run_id: str) -> list[dict]:
        return self.model_call_progress(run_id)["calls"]

    def research_diagnostics(self, run_id: str) -> dict:
        from .storage_read_models import research_diagnostic_snapshot
        return research_diagnostic_snapshot(self, run_id)

    def model_call_plan(self, run_id: str) -> dict | None:
        from .research_execution.call_budget import plan_status
        with self.connect() as db:
            return plan_status(db, run_id)

    def model_call_history(self, run_id: str, *, stage_id: str, after: str = "", limit: int = 10) -> dict:
        """Keyset pages of call receipts, not candidates, prompts or report bodies.

        The durable compact read model is the history index. New events update
        it incrementally; opening a card never reconstructs the research run.
        Cursor keys identify physical attempts, so changing live timings does
        not move existing rows between pages.
        """
        if (not isinstance(stage_id, str) or not stage_id or not isinstance(after, str)
                or len(stage_id) > 256 or len(after) > 1024 or type(limit) is not int or not 1 <= limit <= 50):
            raise ValueError("MODEL_HISTORY_PAGE_INVALID")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM research_runs WHERE run_id=?", (run_id,)).fetchone():
                raise KeyError("research run not found")
        key = ()
        if after:
            try:
                decoded = json.loads(after)
            except (TypeError, ValueError) as exc:
                raise ValueError("MODEL_HISTORY_CURSOR_INVALID") from exc
            if not isinstance(decoded, list) or len(decoded) != 2 or not all(isinstance(value, str) for value in decoded):
                raise ValueError("MODEL_HISTORY_CURSOR_INVALID")
            key = tuple(decoded)
        progress = self.model_call_progress(run_id)
        rows = sorted((call for call in progress["calls"] if call["stage_id"] == stage_id
                       and (call["request_id"], call["attempt_id"]) > key),
                      key=lambda call: (call["request_id"], call["attempt_id"]))
        page = rows[:limit]
        return {"revision": progress["revision"], "calls": page,
                "next_cursor": json.dumps([page[-1]["request_id"], page[-1]["attempt_id"]]) if len(rows) > limit else None}

    def model_call_progress(self, run_id: str) -> dict:
        if not run_id:
            return {"revision": "0:0", "calls": []}
        from .storage_read_models import status_snapshot
        revision, durable = status_snapshot(self, run_id)
        calls = {(call["request_id"], call["attempt_id"]): call for call in durable}
        for active in self.model_activity.snapshot(run_id):
            key = (active["request_id"], active["attempt_id"])
            prior = calls.get(key, {})
            calls[key] = {**prior, **active, "usage": active.get("usage") or prior.get("usage", {})}
        return {"revision": f"{revision}:{self.model_activity.revision}", "calls": list(calls.values())}

    def _build_model_call_status(self, run_id: str, *, request_ids=None, recovery_action_ids=None) -> list[dict]:
        """Live timings + durable receipts; never infer missing usage as zero."""
        if not run_id:
            return []
        fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
                  "reasoning_tokens", "answer_tokens")
        with self.connect() as db:
            request_filter = "" if request_ids is None else " AND request_id IN (" + ",".join("?" for _ in request_ids) + ")"
            request_params = (run_id, *(sorted(request_ids) if request_ids is not None else ()))
            rows = db.execute("""SELECT j.request_id,j.owner_id,j.state,j.receipt_json,j.diagnostic_code,
                j.purpose,j.stage_id,j.generation,j.created_at,j.updated_at FROM model_request_journal j
                WHERE j.run_id=?""" + request_filter + " ORDER BY j.created_at", request_params).fetchall()
            wire_rows = db.execute("""SELECT request_id,attempt_id,stage_id,purpose,diagnostics_json,created_at
                FROM model_wire_diagnostics WHERE run_id=?""" + request_filter + " ORDER BY created_at", request_params).fetchall()
            local_reuses = db.execute("SELECT COUNT(*) FROM model_result_reuse WHERE run_id=?", (run_id,)).fetchone()[0]
            saved_judgments = db.execute("SELECT COUNT(*) FROM model_assertion_proofs WHERE run_id=?", (run_id,)).fetchone()[0]
            from .research_execution.call_budget import plan_status
            call_plan = plan_status(db, run_id)
            recovery_filter = "" if recovery_action_ids is None else " AND e.action_id IN (" + ",".join("?" for _ in recovery_action_ids) + ")"
            recovery_events = db.execute("""SELECT e.payload_json,e.action_id,a.state,a.diagnostic_code,
                a.updated_at,a.started_at,a.external_intent,a.lease_token FROM recovery_events e
                JOIN recovery_plans p ON p.plan_id=e.plan_id
                JOIN recovery_incidents i ON i.incident_id=p.incident_id
                JOIN recovery_actions a ON a.action_id=e.action_id AND a.plan_id=p.plan_id
                WHERE i.run_id=? AND e.state='model-call-observed'""" + recovery_filter + " ORDER BY e.event_id",
                (run_id, *(sorted(recovery_action_ids) if recovery_action_ids is not None else ()))).fetchall()
            from .resilience.payloads import load_payload
            observed_calls = []
            for event in recovery_events:
                try:
                    payload = load_payload(db, event["payload_json"])
                    receipt = payload.get("receipt", {})
                    context = receipt.get("model_call", {})
                    if not context:
                        continue
                    suffix = ":repair:" + event["action_id"]
                    observed_id = str(context.get("request_id") or "")
                    parent = db.execute("SELECT request_id,stage_id,generation FROM model_request_journal WHERE run_id=? AND request_id=? AND stage_id=? AND generation=?",
                        (run_id, observed_id[:-len(suffix)] if observed_id.endswith(suffix) else "",
                         context.get("stage_id"), context.get("generation"))).fetchone()
                    from .resilience.contracts import digest
                    if (parent is None or context.get("run_id") != run_id
                            or context.get("protocol") != "model-events-v1"
                            or not isinstance(context.get("attempt_id"), str)
                            or payload.get("owner_digest") != digest(context["attempt_id"].partition(":")[0])):
                        continue
                    observed_calls.append((dict(event), receipt, context))
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue  # A damaged diagnostic is a local miss, not a lost report.
        calls = {}
        for row in rows:
            try:
                receipt = json.loads(row["receipt_json"] or "{}")
                physical_state = receipt.get("physical_call_state")
                physical_elapsed = receipt.get("physical_call_elapsed_seconds")
                receipt = receipt.get("physical_call_receipt", receipt)
                metadata = receipt.get("metadata", {})
                usage = {name: metadata.get(name) if type(metadata.get(name)) is int and metadata[name] >= 0 else None
                         for name in fields}
                model_identity = {name: metadata[name] for name in ("configured_model_id", "model_id", "alias", "provider_id", "usage_source", "usage_contract")
                    if isinstance(metadata.get(name), str) and len(metadata[name]) <= 256
                    and all(ord(c) >= 32 for c in metadata[name])}
            except (ValueError, TypeError, AttributeError):
                usage = {name: None for name in fields}
                model_identity = {}
                physical_state = None
                physical_elapsed = None
            state = ("saved" if row["state"] in {"candidate", "late_candidate"} else
                     "rate_limited" if row["state"] == "rejected" and row["diagnostic_code"] in {"MODEL_RATE_LIMITED", "MODEL_HTTP_429"}
                     else "rejected" if row["state"] == "rejected" else "local_failure"
                     if row["state"] == "local_processing_failed" else "remote_unknown")
            if physical_state in {"saved", "rejected"}:
                state = physical_state
            calls[(row["request_id"], row["owner_id"])] = {"request_id": row["request_id"], "attempt_id": row["owner_id"],
                "purpose": row["purpose"],
                "physical_attempt": not str(row["diagnostic_code"]).startswith(("MODEL_RUN_", "MODEL_BUDGET_", "MODEL_STAGE_GENERATION_BUDGET_", "MODEL_WORK_ITEM_")),
                "stage_id": row["stage_id"], "generation": row["generation"], "state": state, "usage": usage,
                "elapsed_seconds": physical_elapsed if type(physical_elapsed) in {int, float}
                    and math.isfinite(physical_elapsed) and physical_elapsed >= 0 else max(0, row["updated_at"] - row["created_at"]),
                "idle_seconds": None, "remote_status_unconfirmed": state == "remote_unknown",
                "diagnostic_code": row["diagnostic_code"], **model_identity}
        for event, receipt, context in observed_calls:
            metadata = receipt.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            usage = {name: metadata.get(name) if type(metadata.get(name)) is int and metadata[name] >= 0 else None
                     for name in fields}
            if receipt.get("response_received") is True:
                state = ("saved" if event["state"] in {"committed", "partial_committed"} else "local_failure")
            elif receipt.get("rejected_status") in {400, 401, 403, 404, 405, 413, 415, 422, 429}:
                state = "rate_limited" if receipt.get("diagnostic_code") in {"MODEL_RATE_LIMITED", "MODEL_HTTP_429"} else "rejected"
            else:
                state = "remote_unknown"
            calls[(context["request_id"], context["attempt_id"])] = {
                "request_id": context["request_id"], "attempt_id": context["attempt_id"],
                "physical_attempt": bool(event['external_intent']) or receipt.get('response_received') is True,
                "stage_id": context["stage_id"], "generation": context["generation"],
                "state": state, "usage": usage, "idle_seconds": None,
                "elapsed_seconds": max(0, event["updated_at"] - (event["started_at"] or event["updated_at"])),
                "diagnostic_code": event["diagnostic_code"], "remote_status_unconfirmed": state == "remote_unknown",
                **{name: metadata[name] for name in ("configured_model_id", "model_id", "alias", "provider_id", "usage_source", "usage_contract")
                    if isinstance(metadata.get(name), str) and len(metadata[name]) <= 256
                    and all(ord(c) >= 32 for c in metadata[name])}}
        for row in wire_rows:
            key = (row["request_id"], row["attempt_id"])
            try:
                diagnostic = json.loads(row["diagnostics_json"])
                if not isinstance(diagnostic, dict) or diagnostic.get("schema") != "model-wire-diagnostics-v1":
                    continue
                wire_state = {"response_received": "saved", "send_intent_recorded": "remote_unknown",
                              "local_processing_failed": "local_failure"}.get(
                                  diagnostic.get("wire_state"), diagnostic.get("wire_state", "remote_unknown"))
                calls.setdefault(key, {"request_id": key[0], "attempt_id": key[1], "stage_id": row["stage_id"],
                    "generation": diagnostic.get("generation", 0), "state": wire_state,
                    "elapsed_seconds": diagnostic.get("elapsed_seconds", max(0, time.time() - row["created_at"])),
                    "idle_seconds": None, "remote_status_unconfirmed": wire_state == "remote_unknown",
                    "usage": diagnostic.get("usage") or {},
                    **{name: diagnostic[name] for name in ("model_id", "configured_model_id", "provider_id") if name in diagnostic}})
                calls[key]["purpose"] = row["purpose"]
                calls[key]["diagnostics"] = diagnostic
                for name in ("usage_source", "usage_contract"):
                    if isinstance(diagnostic.get(name), str):
                        calls[key][name] = diagnostic[name][:256]
                if diagnostic.get("wire_state") == "not_sent":
                    calls[key]["physical_attempt"] = False
            except (ValueError, TypeError):
                continue  # Diagnostic damage cannot revoke a paid response.
        for call in calls.values():
            call["reuse_summary"] = {"local_result_reuses": local_reuses, "saved_judgments": saved_judgments}
            call["call_plan"] = call_plan
        return list(calls.values())

    def save_research_job(self, snapshot: dict[str, Any]) -> None:
        run_id = str(snapshot.get("run_id") or "")
        if not run_id:
            return
        with self.connect() as db:
            if db.execute("SELECT 1 FROM research_runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                return
            db.execute(
                """
                INSERT INTO research_jobs(job_id, run_id, state, stage, snapshot_json, updated_at, revision)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    state=excluded.state,
                    stage=excluded.stage,
                    snapshot_json=excluded.snapshot_json,
                    updated_at=excluded.updated_at,
                    revision=excluded.revision
                WHERE excluded.revision >= research_jobs.revision
                """,
                (
                    str(snapshot["job_id"]), run_id, str(snapshot.get("state", "queued")),
                    str(snapshot.get("stage", "preparing")),
                    json.dumps(snapshot, ensure_ascii=False), utc_now_iso(), int(snapshot.get("revision", 0)),
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
                       diagnostics_json, created_at, action_id
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
        from .storage_comparison import write_revision_context
        write_revision_context(db, revision_id, run_id)
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

    def get_comparison_snapshot(self, primary_id: str, secondary_id: str) -> dict[str, Any]:
        """Capture both report heads and their metadata in one read transaction."""
        from .storage_comparison import read_comparison_snapshot

        with self.connect() as db:
            db.execute("BEGIN")
            return read_comparison_snapshot(db, primary_id, secondary_id)

    def save_comparison_artifact(self, artifact: ResearchArtifact, snapshot: dict[str, Any],
                                 attempt: "StageAttempt", *,
                                 cancel_check: Callable[[], bool] | None = None) -> None:
        """Publish only against the exact pair of captured, intact revisions."""
        from .storage_comparison import ComparisonError, read_comparison_snapshot

        if (artifact.artifact_type != "model-comparison"
                or artifact.run_id != snapshot["primary"]["run_id"]
                or attempt.run_id != artifact.run_id or attempt.outcome.value != "complete"):
            raise ValueError("COMPARISON_ARTIFACT_IDENTITY_INVALID")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if cancel_check is not None and cancel_check():
                raise CancelledError("comparison publication cancelled")
            current = read_comparison_snapshot(db, snapshot["primary"]["run_id"],
                                               snapshot["secondary"]["run_id"])
            if self._canonical_artifact_json(current) != self._canonical_artifact_json(snapshot):
                raise ComparisonError("COMPARISON_INPUT_CONFLICT")
            self._save_artifact_db(db, artifact)
            self._append_stage_attempt_db(db, attempt)
            if cancel_check is not None and cancel_check():
                raise CancelledError("comparison publication cancelled")

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
            db.execute("DELETE FROM report_revision_contexts WHERE revision_id IN "
                       "(SELECT revision_id FROM report_revisions WHERE run_id=?)", (run_id,))
            db.execute("DELETE FROM report_revisions WHERE run_id = ?", (run_id,))
            # Recovery rows form a child-first foreign-key tree. They belong to
            # this run, not to the shared provider circuit or company cache.
            plans_for_run = "SELECT plan_id FROM recovery_plans WHERE incident_id IN (SELECT incident_id FROM recovery_incidents WHERE run_id=?)"
            db.execute(f"DELETE FROM recovery_events WHERE plan_id IN ({plans_for_run})", (run_id,))
            db.execute("DELETE FROM recovery_target_leases WHERE run_id=?", (run_id,))
            db.execute(f"DELETE FROM recovery_actions WHERE plan_id IN ({plans_for_run})", (run_id,))
            db.execute("DELETE FROM recovery_plans WHERE incident_id IN (SELECT incident_id FROM recovery_incidents WHERE run_id=?)", (run_id,))
            db.execute("DELETE FROM recovery_incidents WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM stage_verification_snapshots WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM model_request_journal WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM model_stage_request_heads WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM research_runs WHERE run_id = ?", (run_id,))
        return True

    def get_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.read_artifacts(run_id).records)

    def read_artifacts(self, run_id: str) -> StorageReadResult:
        """Read healthy siblings and explicit issues in one consistent view."""
        with self.connect() as db:
            db.execute("BEGIN")
            rows = db.execute(
                """
                SELECT * FROM artifacts
                WHERE run_id = ?
                ORDER BY created_at
                """,
                (run_id,),
            ).fetchall()
            results: list[dict[str, Any]] = []
            issues = []
            for row in rows:
                item = dict(row)
                try:
                    item["content"] = load_payload(db, item.pop("payload_json"))
                    if not isinstance(item["content"], dict):
                        raise ValueError("STORAGE_ARTIFACT_SHAPE_INVALID")
                    results.append(item)
                except (ValueError, TypeError) as exc:
                    issues.append({**_storage_issue(exc, "artifacts/" + str(row["artifact_id"]) + "/content", str(row["artifact_id"]), artifact=True),
                                   "artifact_type": str(row["artifact_type"]), "impact_scope": "artifact"})
        return StorageReadResult(tuple(results), tuple(issues))

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
            db.execute("BEGIN IMMEDIATE")
            return self._save_thesis_version_db(db, company_cik, content, run_id=run_id,
                created_by=created_by, created_at=created_at)

    @staticmethod
    def _save_thesis_version_db(
        db: sqlite3.Connection, company_cik: str, content: dict[str, Any], *,
        run_id: str | None, created_by: str, created_at: str,
    ) -> dict[str, Any]:
        row = db.execute("SELECT COALESCE(MAX(version),0) AS version "
                         "FROM thesis_versions WHERE company_cik=?", (company_cik,)).fetchone()
        version = int(row["version"]) + 1
        thesis_version_id = f"{company_cik}:v{version}"
        db.execute("""INSERT INTO thesis_versions(
            thesis_version_id,company_cik,run_id,version,content_json,created_at,created_by)
            VALUES(?,?,?,?,?,?,?)""", (thesis_version_id, company_cik, run_id, version,
                json.dumps(content, ensure_ascii=False, allow_nan=False), created_at, created_by))
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

    def get_settings(self, defaults: Mapping[str, str]) -> dict[str, str]:
        if not defaults:
            return {}
        with self.connect() as db:
            rows = dict(db.execute("SELECT key,value FROM settings WHERE key IN (" + ",".join("?" for _ in defaults) + ")", tuple(defaults)))
        return {key: str(rows.get(key, default)) for key, default in defaults.items()}

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
