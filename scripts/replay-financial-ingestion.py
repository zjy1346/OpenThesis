"""Read-only local replay of financial PDFs through the production compiler.

The manifest supplies a company and one or more already-downloaded filings.
This tool does not fetch sources, call models, use a configured vision fallback,
or write the application's database. It records parser-window progress,
untrusted candidates, compiler admission/quarantine reasons, and input hashes.
Raw excerpts and local paths are deliberately omitted from the output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from openthesis.domain import Company, FilingDocument
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_ingestion import FinancialIngestionEngine


SCHEMA_VERSION = "openthesis.financial-ingestion-replay.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fact_view(fact: Any) -> dict[str, Any]:
    raw_text = str(getattr(fact, "raw_text", "") or "")
    return {
        "fact_id": str(getattr(fact, "fact_id", "") or ""),
        "concept": str(getattr(fact, "concept", "") or ""),
        "reported_concept": str(getattr(fact, "reported_concept", "") or ""),
        "value": _finite_number(getattr(fact, "value", None)),
        "unit": str(getattr(fact, "unit", "") or ""),
        "currency": str(getattr(fact, "currency", "") or ""),
        "unit_scale": _finite_number(getattr(fact, "unit_scale", None)),
        "unit_provenance": str(getattr(fact, "unit_provenance", "") or ""),
        "fiscal_year": getattr(fact, "fiscal_year", None),
        "fiscal_period": str(getattr(fact, "fiscal_period", "") or ""),
        "period_start": getattr(fact, "start_date", None),
        "period_end": str(getattr(fact, "end_date", "") or ""),
        "accession": str(getattr(fact, "accession_number", "") or ""),
        "statement": str(getattr(fact, "statement", "") or ""),
        "scope": str(
            getattr(fact, "consolidated_scope", "")
            or getattr(fact, "scope", "")
            or ""
        ),
        "source_page": getattr(fact, "source_page", None),
        "source_bbox": getattr(fact, "source_bbox", None),
        "source_column": str(getattr(fact, "source_column", "") or ""),
        "extractor_parser_version": str(getattr(fact, "parser_version", "") or ""),
        "raw_excerpt_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        if raw_text else "",
    }


def _candidate_view(candidate: Any) -> dict[str, Any]:
    return {
        "extractor": str(getattr(candidate, "extractor", "") or ""),
        "fact": _fact_view(candidate.fact),
        "evidence_ids": sorted({
            str(getattr(item, "evidence_id", "") or "")
            for item in getattr(candidate, "evidence", ())
            if getattr(item, "evidence_id", "")
        }),
    }


def _validation_view(validation: Any) -> dict[str, Any]:
    identity = tuple(getattr(validation, "identity", ()) or ())
    accepted = tuple(getattr(validation, "accepted", ()) or ())
    quarantined = tuple(getattr(validation, "quarantined", ()) or ())
    status = getattr(validation, "status", "")
    return {
        "identity": [str(item) for item in identity],
        "role": str(getattr(validation, "role", "") or ""),
        "status": str(getattr(status, "value", status) or ""),
        "covered_concepts": sorted(
            str(item) for item in getattr(validation, "covered", ()) if item
        ),
        "issues": [str(item) for item in getattr(validation, "issues", ()) if item],
        "accepted_fact_ids": sorted(str(item.fact_id) for item in accepted),
        "quarantined_fact_ids": sorted(str(item.fact_id) for item in quarantined),
    }


def _company_from_manifest(value: Any) -> Company:
    if not isinstance(value, dict):
        raise ValueError("company_manifest_invalid")
    required = ("cik", "ticker", "name", "market", "reporting_currency")
    if any(not str(value.get(key, "")).strip() for key in required):
        raise ValueError("company_manifest_missing_required_field")
    return Company(
        cik=str(value["cik"]),
        ticker=str(value["ticker"]),
        name=str(value["name"]),
        exchange=str(value.get("exchange", "")),
        issuer_id=str(value.get("issuer_id", "")),
        market=str(value["market"]),
        security_id=str(value.get("security_id", value["cik"])),
        listing_currency=str(value.get("listing_currency", value["reporting_currency"])),
        reporting_currency=str(value["reporting_currency"]),
        accounting_standard=str(value.get("accounting_standard", "")),
        industry=str(value.get("industry", "")),
        industry_support=str(value.get("industry_support", "standard")),
        source_url=str(value.get("source_url", "")),
    )


def _filings_from_manifest(value: Any, company: Company) -> tuple[list[FilingDocument], list[dict[str, str]]]:
    if not isinstance(value, list) or not value:
        raise ValueError("filings_manifest_empty")
    filings: list[FilingDocument] = []
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("filing_manifest_invalid")
        required = (
            "document_id", "accession_number", "form_type", "fiscal_period",
            "period_end", "filed_at", "primary_document", "local_path",
        )
        if any(not str(row.get(key, "")).strip() for key in required):
            raise ValueError("filing_manifest_missing_required_field")
        source_path = Path(str(row["local_path"])).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError("replay_source_unavailable")
        source_hash = _sha256(source_path)
        declared_hash = str(row.get("content_sha256", "")).strip().casefold()
        if declared_hash and declared_hash != source_hash.casefold():
            raise ValueError("replay_source_hash_mismatch")
        document_id = str(row["document_id"])
        if document_id in seen:
            raise ValueError("filing_document_id_duplicate")
        seen.add(document_id)
        accession = str(row["accession_number"])
        filings.append(FilingDocument(
            document_id=document_id,
            company_cik=company.cik,
            accession_number=accession,
            form_type=str(row["form_type"]),
            fiscal_period=str(row["fiscal_period"]),
            period_end=str(row["period_end"]),
            filed_at=str(row["filed_at"]),
            primary_document=str(row["primary_document"]),
            source_url=str(row.get("source_url", "")),
            local_path=str(source_path),
            content_hash=source_hash,
            revision=str(row.get("revision", "original")),
            supersedes_document_id=str(row.get("supersedes_document_id", "")),
        ))
        sources.append({"document_id": document_id, "accession": accession, "sha256": source_hash})
    return filings, sources


def replay(manifest: dict[str, Any]) -> dict[str, Any]:
    """Run the local parse+compiler path without model, network, or DB access."""

    company = _company_from_manifest(manifest.get("company"))
    filings, source_manifest = _filings_from_manifest(manifest.get("filings"), company)
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}

    def progress(stage: str, current: int, total: int, detail: dict[str, Any] | None = None) -> None:
        safe_detail = detail if isinstance(detail, dict) else {}
        event: dict[str, Any] = {
            "stage": str(stage),
            "current": max(0, int(current)),
            "total": max(0, int(total)),
        }
        for key in ("filing_id", "status", "error_code", "window_index", "window_total"):
            if key in safe_detail and safe_detail[key] is not None:
                event[key] = safe_detail[key]
        elapsed = _finite_number(safe_detail.get("elapsed_seconds"))
        if elapsed is not None:
            event["elapsed_seconds"] = max(0.0, elapsed)
        events.append(event)

    # Checkpoint files are isolated to an automatically cleaned temporary
    # directory. The engine stays the base class so its process isolation and
    # bounded parser scheduler remain enabled.
    with tempfile.TemporaryDirectory(prefix="openthesis-financial-replay-") as checkpoint_dir:
        engine = FinancialIngestionEngine(checkpoint_dir=checkpoint_dir)
        collect = engine.collect_candidate_batches

        def collect_and_capture(subject: Company, source_filings: Any, **kwargs: Any):
            result = collect(subject, source_filings, **kwargs)
            captured["collection"] = result
            return result

        engine.collect_candidate_batches = collect_and_capture  # type: ignore[method-assign]
        dataset = FinancialFactCompiler().compile_from_ingestion(
            company,
            filings,
            engine,
            progress=progress,
        )

    collection = captured.get("collection")
    if collection is None:
        raise RuntimeError("candidate_collection_not_captured")

    candidate_documents: list[dict[str, Any]] = []
    batch_diagnostics: list[str] = []
    for filing in filings:
        batches = collection.batches_by_document.get(filing.document_id, ())
        candidates = [candidate for batch in batches for candidate in batch.candidates]
        batch_diagnostics.extend(
            str(item)
            for batch in batches
            for item in getattr(batch, "diagnostics", ())
            if item
        )
        candidate_documents.append({
            "document_id": filing.document_id,
            "accession": filing.accession_number,
            "period_end": filing.period_end,
            "candidate_count": len(candidates),
            "candidates": [_candidate_view(item) for item in candidates],
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "model_calls": 0,
        "network_calls": 0,
        "application_database_writes": 0,
        "company": {
            "security_id": company.security_id,
            "ticker": company.ticker,
            "name": company.name,
            "market": company.market,
            "reporting_currency": company.reporting_currency,
            "accounting_standard": company.accounting_standard,
        },
        "sources": source_manifest,
        "candidate_documents": candidate_documents,
        "candidate_collection_diagnostics": list(collection.diagnostics),
        "candidate_batch_diagnostics": list(dict.fromkeys(batch_diagnostics)),
        "compiler_diagnostics": list(dataset.diagnostics),
        "validations": [_validation_view(item) for item in dataset.validations],
        "accepted_facts": [_fact_view(item) for item in dataset.resolved_facts],
        "quarantined_facts": [_fact_view(item) for item in dataset.quarantined_facts],
        "progress_events": events,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "allow_ai": bool(dataset.allow_ai),
    }


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="JSON company + local filing manifest")
    parser.add_argument("--output", type=Path, help="optional diagnostic JSON path; defaults to stdout")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("replay_manifest_invalid")
        payload = replay(manifest)
        if args.output is not None:
            if args.output.expanduser().resolve() == args.manifest.expanduser().resolve():
                raise ValueError("output_must_not_overwrite_manifest")
            _write_output(args.output, payload)
            print(json.dumps({
                "status": "REPLAY_COMPLETE",
                "output_name": args.output.name,
                "source_count": len(payload["sources"]),
                "candidate_count": sum(item["candidate_count"] for item in payload["candidate_documents"]),
            }, ensure_ascii=False))
        else:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    except FileNotFoundError as exc:
        code = "replay_source_unavailable" if str(exc) == "replay_source_unavailable" else "manifest_or_source_unavailable"
        print(json.dumps({"status": "REPLAY_BLOCKED", "error_code": code}, ensure_ascii=False))
        return 2
    except OSError:
        print(json.dumps({"status": "REPLAY_BLOCKED", "error_code": "replay_io_failed"}, ensure_ascii=False))
        return 2
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        error_code = str(exc) if str(exc) else type(exc).__name__
        print(json.dumps({"status": "REPLAY_BLOCKED", "error_code": error_code}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "REPLAY_ERROR", "error_code": f"parser:{type(exc).__name__}"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
