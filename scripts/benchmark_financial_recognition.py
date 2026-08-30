"""Reproducible, read-only benchmark for the canonical recognition pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time
from typing import Any

from openthesis.domain import Company, FilingDocument
from openthesis.financial_ingestion import FinancialIngestionEngine
from openthesis.financial_recognition import FinancialRecognitionCoordinator


def _load_fixture(data_dir: Path, security_id: str) -> tuple[Company, list[FilingDocument]]:
    db_path = data_dir / "openthesis.db"
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        listing = connection.execute(
            """
            SELECT l.*, i.name, i.industry, i.industry_support
            FROM security_listings l JOIN issuers i ON i.issuer_id=l.issuer_id
            WHERE l.security_id=?
            """,
            (security_id,),
        ).fetchone()
        if listing is None:
            raise SystemExit(f"security not found: {security_id}")
        company = Company(
            security_id,
            str(listing["symbol"]),
            str(listing["name"]),
            str(listing["exchange_name"]),
            issuer_id=str(listing["issuer_id"]),
            market=str(listing["market"]),
            security_id=security_id,
            listing_currency=str(listing["listing_currency"]),
            reporting_currency=str(listing["reporting_currency"]),
            accounting_standard=str(listing["accounting_standard"]),
            industry=str(listing["industry"]),
            industry_support=str(listing["industry_support"]),
            source_url=str(listing["source_url"]),
        )
        rows = connection.execute(
            """
            SELECT * FROM filings WHERE company_cik=?
            ORDER BY period_end, filed_at, accession_number
            """,
            (security_id,),
        ).fetchall()
    finally:
        connection.close()
    filings: list[FilingDocument] = []
    seen_hashes: set[str] = set()
    for row in rows:
        path = Path(str(row["local_path"] or ""))
        if not path.is_file():
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        stored_hash = str(row["content_hash"] or "")
        if stored_hash and stored_hash.casefold() != actual_hash.casefold():
            raise SystemExit(f"hash mismatch: {row['accession_number']}")
        if actual_hash in seen_hashes:
            continue
        seen_hashes.add(actual_hash)
        filings.append(FilingDocument(
            str(row["document_id"]), str(row["company_cik"]),
            str(row["accession_number"]), str(row["form_type"]),
            str(row["fiscal_period"]), str(row["period_end"]),
            str(row["filed_at"]), str(row["primary_document"]),
            str(row["source_url"]), str(path), actual_hash,
            str(row["ingested_at"]), str(row["revision"]),
            str(row["supersedes_document_id"]),
        ))
    if not filings:
        raise SystemExit("no hash-verified local filings")
    return company, filings


def _snapshot(outcome: Any) -> str:
    rows = sorted(
        (
            fact.accession_number,
            fact.concept,
            fact.end_date,
            fact.fiscal_period,
            fact.currency,
            fact.value,
            fact.source_page,
        )
        for fact in outcome.dataset.resolved_facts
    )
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _run(company: Company, filings: list[FilingDocument], cache_dir: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    started = time.monotonic()
    outcome = FinancialRecognitionCoordinator(
        FinancialIngestionEngine(cache_dir=cache_dir)
    ).recognize(
        company,
        filings,
        progress=lambda stage, current, total, detail=None: events.append({
            "at": round(time.monotonic() - started, 3),
            "stage": stage,
            "current": current,
            "total": total,
            "filing": (detail or {}).get("filing_id", ""),
            "status": (detail or {}).get("status", ""),
        }),
    )
    elapsed = time.monotonic() - started
    return {
        "elapsed_seconds": round(elapsed, 3),
        "state": outcome.state.value,
        "allow_ai": outcome.dataset.allow_ai,
        "resolved_fact_count": len(outcome.dataset.resolved_facts),
        "quarantined_fact_count": len(outcome.dataset.quarantined_facts),
        "snapshot_sha256": _snapshot(outcome),
        "first_progress_seconds": events[0]["at"] if events else None,
        "events": events,
    }


def _atomic_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    """Publish benchmark progress so an interrupted run leaves valid JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_is_complete(run: dict[str, Any]) -> bool:
    return str(run.get("state", "")).upper() == "COMPLETE"


def _failure_evidence(
    result: dict[str, Any], *, phase: str, run: dict[str, Any]
) -> None:
    result["status"] = "FAILED"
    result["failure_evidence"] = {
        "phase": phase,
        "state": run.get("state", ""),
        "run": run,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    default_data = Path(os.environ.get("LOCALAPPDATA", ".")) / "org.openthesis.desktop"
    parser.add_argument("--data-dir", type=Path, default=default_data)
    parser.add_argument("--security-id", default="CN_A:SZSE:002594.SZ")
    parser.add_argument("--cold-runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    company, filings = _load_fixture(args.data_dir, args.security_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cold: list[dict[str, Any]] = []
    warm: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "security_id": args.security_id,
        "reports": len(filings),
        "compressed_bytes": sum(Path(item.local_path).stat().st_size for item in filings),
        "manifest": [
            {
                "accession": item.accession_number,
                "period": item.fiscal_period,
                "period_end": item.period_end,
                "sha256": item.content_hash,
            }
            for item in filings
        ],
        "environment": {
            "system": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "status": "RUNNING",
        "cold": cold,
        "warm": warm,
        "checkpoint": {"phase": "cold", "completed_rounds": 0},
    }
    _atomic_checkpoint(args.output, result)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as temporary:
        root = Path(temporary)
        for index in range(max(1, args.cold_runs)):
            cold_run = _run(company, filings, root / f"cold-{index}")
            cold.append(cold_run)
            result["cold_p50_seconds"] = round(
                statistics.median(item["elapsed_seconds"] for item in cold), 3
            )
            result["cold_max_seconds"] = max(
                item["elapsed_seconds"] for item in cold
            )
            result["checkpoint"] = {
                "phase": "cold",
                "completed_rounds": len(cold),
                "last_state": cold_run.get("state", ""),
            }
            _atomic_checkpoint(args.output, result)
            if not _run_is_complete(cold_run):
                _failure_evidence(result, phase="cold", run=cold_run)
                _atomic_checkpoint(args.output, result)
                print(json.dumps({
                    "status": result["status"],
                    "phase": "cold",
                    "state": cold_run.get("state", ""),
                    "output": str(args.output),
                }, ensure_ascii=False))
                return 1
        last_successful_cold_cache = root / f"cold-{len(cold) - 1}"
        # Warm runs intentionally reuse the last successful cold cache. There
        # is no separate warm seed: the first warm round is the first reuse.
        for _index in range(3):
            warm_run = _run(company, filings, last_successful_cold_cache)
            warm.append(warm_run)
            result["checkpoint"] = {
                "phase": "warm",
                "completed_rounds": len(warm),
                "last_state": warm_run.get("state", ""),
            }
            _atomic_checkpoint(args.output, result)
            if not _run_is_complete(warm_run):
                _failure_evidence(result, phase="warm", run=warm_run)
                _atomic_checkpoint(args.output, result)
                print(json.dumps({
                    "status": result["status"],
                    "phase": "warm",
                    "state": warm_run.get("state", ""),
                    "output": str(args.output),
                }, ensure_ascii=False))
                return 1
    result["status"] = "COMPLETE"
    result["warm_max_seconds"] = max(item["elapsed_seconds"] for item in warm)
    result["checkpoint"] = {
        "phase": "complete",
        "completed_rounds": len(warm),
    }
    _atomic_checkpoint(args.output, result)
    print(json.dumps({
        "status": result["status"],
        "reports": result["reports"],
        "compressed_mb": round(result["compressed_bytes"] / 1024 / 1024, 2),
        "cold_seconds": [item["elapsed_seconds"] for item in cold],
        "warm_seconds": [item["elapsed_seconds"] for item in warm],
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
