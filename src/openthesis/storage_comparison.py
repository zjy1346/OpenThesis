"""Read-only revision proof assembly for atomic comparison publication.

No model, report projection or business analysis belongs in this repository
adapter. Storage supplies its transaction; the comparator consumes the proof.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


class ComparisonError(RuntimeError):
    """Stable, non-sensitive diagnostics crossing the application boundary."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.retryable = False


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def write_revision_context(db: sqlite3.Connection, revision_id: str, run_id: str) -> None:
    """Freeze the publishing identity only for a newly created revision.

    Old revisions remain unknown. A model change or repeat write must never
    backfill them using today's configuration. No secrets are copied here.
    """
    row = db.execute("SELECT payload_json FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
    try:
        metadata = json.loads(row["payload_json"]) if row is not None else None
        if (not isinstance(metadata, dict) or metadata.get("run_id") != run_id
                or not isinstance(metadata.get("provider_id"), str)
                or not isinstance(metadata.get("model_id"), str)):
            raise ValueError("invalid publishing identity")
        context = {
            "schema": "report-revision-context-v1", "revision_id": revision_id, "run_id": run_id,
            "provider": metadata["provider_id"], "model": metadata["model_id"],
            "publishing_run_sha256": hashlib.sha256(_canonical(metadata).encode("utf-8")).hexdigest(),
        }
        encoded = _canonical(context)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("REPORT_REVISION_CONTEXT_INVALID") from exc
    db.execute("INSERT INTO report_revision_contexts(revision_id,context_json,context_sha256) VALUES(?,?,?)",
               (revision_id, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()))


def read_comparison_snapshot(db: sqlite3.Connection, primary_id: str,
                             secondary_id: str) -> dict[str, Any]:
    if not primary_id or not secondary_id or primary_id == secondary_id:
        raise ValueError("COMPARISON_RUN_IDENTITY_INVALID")
    result: dict[str, Any] = {}
    for role, run_id in (("primary", primary_id), ("secondary", secondary_id)):
        run = db.execute("SELECT company_cik, status, payload_json FROM research_runs WHERE run_id=?",
                         (run_id,)).fetchone()
        head = db.execute("""SELECT r.*, l.generation AS head_generation
            FROM latest_report_revisions l JOIN report_revisions r ON r.revision_id=l.revision_id
            WHERE l.run_id=?""", (run_id,)).fetchone()
        if run is None or head is None:
            raise ComparisonError("COMPARISON_REPORT_UNAVAILABLE")
        context_row = db.execute(
            "SELECT context_json,context_sha256 FROM report_revision_contexts WHERE revision_id=?",
            (head["revision_id"],)).fetchone()
        if context_row is None:
            raise ComparisonError("COMPARISON_REPORT_IDENTITY_UNVERIFIED")
        try:
            metadata = json.loads(run["payload_json"])
            payload = json.loads(head["payload_json"])
            context = json.loads(context_row["context_json"])
            if (not isinstance(metadata, dict) or not isinstance(payload, dict)
                    or metadata.get("run_id") != run_id
                    or metadata.get("status") != run["status"]
                    or not isinstance(metadata.get("company"), dict)
                    or metadata["company"].get("cik") != run["company_cik"]
                    or head["run_id"] != run_id
                    or head["head_generation"] != head["generation"]
                    or int(head["generation"]) < 1
                    or not isinstance(metadata.get("provider_id"), str)
                    or not isinstance(metadata.get("model_id"), str)
                    or not isinstance(context, dict)
                    or context.get("schema") != "report-revision-context-v1"
                    or context.get("revision_id") != head["revision_id"]
                    or context.get("run_id") != run_id
                    or not isinstance(context.get("provider"), str)
                    or not isinstance(context.get("model"), str)
                    or hashlib.sha256(_canonical(context).encode("utf-8")).hexdigest()
                       != context_row["context_sha256"]):
                raise ValueError("invalid revision identity")
            report_digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
            if report_digest != head["content_sha256"]:
                raise ValueError("invalid revision hash")
            run_digest = hashlib.sha256(_canonical(metadata).encode("utf-8")).hexdigest()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ComparisonError("COMPARISON_REPORT_INTEGRITY") from exc
        result[role] = {
            "run_id": run_id, "company_cik": run["company_cik"],
            "provider": context["provider"], "model": context["model"],
            "revision_context": context,
            "run_digest": run_digest, "revision_id": head["revision_id"],
            "generation": head["generation"], "content_sha256": report_digest,
            "report_payload": payload,
        }
    if result["primary"]["company_cik"] != result["secondary"]["company_cik"]:
        raise ComparisonError("COMPARISON_COMPANY_CONFLICT")
    return result
