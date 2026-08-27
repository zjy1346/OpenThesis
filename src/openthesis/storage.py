from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .domain import (
    Company,
    FilingDocument,
    FinancialFact,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)


SCHEMA_VERSION = 5


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
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
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
                    revision TEXT NOT NULL DEFAULT 'original',
                    source_document TEXT NOT NULL DEFAULT '',
                    source_page INTEGER,
                    source_bbox_json TEXT,
                    raw_text TEXT NOT NULL DEFAULT '',
                    parser_version TEXT NOT NULL DEFAULT '',
                    validation_status TEXT NOT NULL DEFAULT 'unvalidated',
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
                    status TEXT NOT NULL,
                    issues_json TEXT NOT NULL,
                    covered_concepts_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE INDEX IF NOT EXISTS idx_validation_groups_company
                ON financial_validation_groups(company_cik, period_end DESC);

                CREATE TABLE IF NOT EXISTS financial_retry_state (
                    company_cik TEXT PRIMARY KEY,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_stage TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
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
                    "revision": "TEXT NOT NULL DEFAULT 'original'",
                    "source_document": "TEXT NOT NULL DEFAULT ''",
                    "source_page": "INTEGER",
                    "source_bbox_json": "TEXT",
                    "raw_text": "TEXT NOT NULL DEFAULT ''",
                    "parser_version": "TEXT NOT NULL DEFAULT ''",
                    "validation_status": "TEXT NOT NULL DEFAULT 'unvalidated'",
                },
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
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

    def save_facts(self, facts: list[FinancialFact]) -> None:
        with self.connect() as db:
            self._insert_facts(db, facts)

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
            self._insert_facts(db, facts)

    def replace_financial_ingestion(
        self,
        company_cik: str,
        accession_numbers: list[str],
        accepted_facts: list[FinancialFact],
        quarantined_facts: list[FinancialFact] | None = None,
        validation_groups: list[Any] | tuple[Any, ...] = (),
        evidence: list[Any] | tuple[Any, ...] = (),
    ) -> None:
        """Atomically replace facts, evidence, and validation decisions.

        Rejected facts are retained with ``validation_status=REJECTED`` for
        auditability, while normal reads hide them.  The operation is scoped
        to the supplied accessions so prior research history is never deleted.
        """
        unique = sorted({value for value in accession_numbers if value})
        with self.connect() as db:
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
            self._insert_facts(db, list(accepted_facts) + list(quarantined_facts or ()))
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
                group_id = "|".join((company_cik, *identity))
                db.execute(
                    """
                    INSERT OR REPLACE INTO financial_validation_groups(
                        group_id, company_cik, accession_number, period_end,
                        fiscal_period, consolidated_scope, currency, status,
                        issues_json, covered_concepts_json, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id, company_cik, identity[0], identity[1], identity[2],
                        identity[3], identity[4], status,
                        json.dumps(list(getattr(validation, "issues", ())), ensure_ascii=False),
                        json.dumps(sorted(getattr(validation, "covered_concepts", frozenset())), ensure_ascii=False),
                        utc_now_iso(),
                    ),
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
                currency, unit_scale, revision, source_document, source_page,
                source_bbox_json, raw_text, parser_version, validation_status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    fact.revision,
                    fact.source_document,
                    fact.source_page,
                    json.dumps(fact.source_bbox) if fact.source_bbox is not None else None,
                    fact.raw_text,
                    fact.parser_version,
                    fact.validation_status,
                )
                for fact in facts
            ],
        )

    def get_facts(self, cik: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT f.* FROM financial_facts f
                LEFT JOIN security_listings l ON l.security_id = f.company_cik
                WHERE f.company_cik = ?
                  AND COALESCE(f.validation_status, 'unvalidated') <> 'REJECTED'
                  AND COALESCE(f.consolidated_scope, 'consolidated') = 'consolidated'
                  AND (
                      l.reporting_currency IS NULL
                      OR COALESCE(f.currency, '') = ''
                      OR UPPER(f.currency) = UPPER(l.reporting_currency)
                  )
                ORDER BY fiscal_year DESC, concept
                """,
                (cik,),
            ).fetchall()
        return [self._fact_row(row) for row in rows]

    def get_facts_audit(self, cik: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM financial_facts WHERE company_cik = ? ORDER BY fiscal_year DESC, concept",
                (cik,),
            ).fetchall()
        return [self._fact_row(row) for row in rows]

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
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM financial_validation_groups WHERE company_cik = ? ORDER BY period_end DESC, accession_number",
                (cik,),
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

    def save_run(self, run: ResearchRun) -> None:
        payload = run.to_dict()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO research_runs(
                    run_id, company_cik, payload_json, status, started_at, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
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

    def interrupt_running_runs(
        self, reason: str = "应用在研究完成前退出，任务已标记为中断"
    ) -> int:
        """Mark runs left active by a previous process as safely interrupted."""
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
                payload["status"] = RunStatus.CANCELLED.value
                payload["completed_at"] = completed_at
                db.execute(
                    """
                    UPDATE research_runs
                    SET payload_json = ?, status = ?, completed_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        json.dumps(payload, ensure_ascii=False),
                        RunStatus.CANCELLED.value,
                        completed_at,
                        row["run_id"],
                    ),
                )
        return len(rows)

    def save_artifact(self, artifact: ResearchArtifact) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO artifacts(
                    artifact_id, run_id, artifact_type, title, payload_json,
                    model_id, agent_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.run_id,
                    artifact.artifact_type,
                    artifact.title,
                    json.dumps(artifact.content, ensure_ascii=False),
                    artifact.model_id,
                    artifact.agent_id,
                    artifact.created_at,
                ),
            )

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
