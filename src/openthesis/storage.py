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


SCHEMA_VERSION = 2


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
                    FOREIGN KEY(company_cik) REFERENCES companies(cik)
                );

                CREATE INDEX IF NOT EXISTS idx_facts_company_concept_year
                ON financial_facts(company_cik, concept, fiscal_year);

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
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def save_company(self, company: Company) -> None:
        with self.connect() as db:
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

    def save_filings(self, filings: list[FilingDocument]) -> None:
        with self.connect() as db:
            db.executemany(
                """
                INSERT OR REPLACE INTO filings(
                    document_id, company_cik, accession_number, form_type,
                    fiscal_period, period_end, filed_at, primary_document,
                    source_url, local_path, content_hash, ingested_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    )
                    for filing in filings
                ],
            )

    def save_facts(self, facts: list[FinancialFact]) -> None:
        with self.connect() as db:
            db.executemany(
                """
                INSERT OR REPLACE INTO financial_facts(
                    fact_id, company_cik, concept, reported_concept, value,
                    unit, fiscal_year, fiscal_period, form_type, start_date,
                    end_date, filed_at, accession_number, source_url, scope
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    )
                    for fact in facts
                ],
            )

    def get_facts(self, cik: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM financial_facts
                WHERE company_cik = ?
                ORDER BY fiscal_year DESC, concept
                """,
                (cik,),
            ).fetchall()
        return [dict(row) for row in rows]

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
