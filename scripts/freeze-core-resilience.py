"""Freeze independent 3.0.0 inputs without rewriting source or answer data.

The history database is opened read-only and copied with SQLite's backup API.
Only the isolated copy is initialized by the application's public read path.
The output records hashes and identities, not secrets or model configuration.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from openthesis.service import AppService


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/golden_research/v2"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def payload_digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def no_provider(_config: object) -> object:
    raise AssertionError("BASELINE_REPORT_READ_CALLED_PROVIDER")


def freeze_history(database: Path, temp_root: Path) -> dict[str, object]:
    before_hash = digest(database)
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute("PRAGMA query_only=ON")
        check = source.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"HISTORY_SOURCE_INTEGRITY:{check}")
        runs = [dict(row) for row in source.execute(
            "SELECT run_id, company_cik, status FROM research_runs ORDER BY run_id"
        )]
        # Raw artifacts freeze the prior input even if a corrected renderer
        # subsequently changes the visible section list.
        artifacts = [dict(row) for row in source.execute(
            "SELECT artifact_id, run_id, artifact_type, payload_json FROM artifacts ORDER BY artifact_id"
        )]
        with tempfile.TemporaryDirectory(prefix="core-resilience-history-", dir=temp_root) as directory:
            clone = sqlite3.connect(Path(directory) / "openthesis.db")
            try:
                source.backup(clone)
            finally:
                clone.close()
            service = AppService(Path(directory), provider_factory=no_provider)
            results = []
            for run in runs:
                report = service.get_report(run["run_id"])
                if report.get("run_id") != run["run_id"] or not report.get("markdown"):
                    raise AssertionError("HISTORY_READ_IDENTITY_OR_CONTENT_LOST")
                results.append({
                    **run,
                    "read_state": report.get("read_state"),
                    "markdown_sha256": hashlib.sha256(report["markdown"].encode("utf-8")).hexdigest(),
                    "visible_sections": report.get("visible_sections", []),
                    "is_substantive": report.get("is_substantive"),
                })
    finally:
        source.close()
    if digest(database) != before_hash:
        raise AssertionError("HISTORY_SOURCE_CHANGED")
    return {
        "source_sha256": before_hash,
        "source_quick_check": check,
        "runs": results,
        "artifact_input_hashes": [{
            "artifact_id": artifact["artifact_id"], "run_id": artifact["run_id"],
            "artifact_type": artifact["artifact_type"],
            "payload_sha256": hashlib.sha256(artifact["payload_json"].encode("utf-8")).hexdigest(),
        } for artifact in artifacts],
        "expected_corrections": ["placeholder_sections_are_not_substantive"],
        "provider_calls": 0,
    }


def freeze_corpus() -> list[dict[str, object]]:
    manifest = json.loads((CORPUS / "cohort_manifest.json").read_text(encoding="utf-8"))
    samples = []
    for issuer in manifest["selected_issuers"]:
        sample_dir = ROOT / issuer["sample_path"]
        packet = json.loads((sample_dir / "source_packet.json").read_text(encoding="utf-8"))
        originals = []
        for source in packet["sources"]:
            cache = Path(source["cache_reference"])
            actual = digest(cache)
            if actual != source["sha256"] or cache.stat().st_size != source["byte_length"]:
                raise AssertionError(f"GOLDEN_SOURCE_CHANGED:{issuer['ticker']}:{source['source_id']}")
            originals.append({
                "source_id": source["source_id"], "type": source["source_type"],
                "sha256": actual, "bytes": cache.stat().st_size,
            })
        artifacts = {name: digest(sample_dir / name) for name in (
            "source_packet.json", "financial_facts.json", "research_report.md", "independent_review.md",
        )}
        samples.append({
            "ticker": issuer["ticker"], "market": issuer["market"],
            "sample_status": issuer["sample_status"], "fact_count": issuer["fact_count"],
            "run_id": packet["software_run"]["run_id"],
            "sample_path": issuer["sample_path"], "artifact_byte_hashes": artifacts,
            "originals": originals,
        })
    if len(samples) != 9 or sum(item["fact_count"] for item in samples) != 365:
        raise AssertionError("GOLDEN_COHORT_SCOPE_CHANGED")
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    temp_root = ROOT / "tmp/core-resilience-3.0.0"
    temp_root.mkdir(parents=True, exist_ok=True)
    source_hashes = {
        str(path.relative_to(ROOT)).replace("\\", "/"): digest(path)
        for path in sorted((ROOT / "src/openthesis").rglob("*.py"))
    }
    result = {
        "schema_version": "openthesis.core-resilience.baseline.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_hashes": source_hashes,
        "rpc_contract_sha256": digest(ROOT / "src/openthesis/sidecar.py"),
        "golden_cohort_manifest_sha256": digest(CORPUS / "cohort_manifest.json"),
        "golden_samples": freeze_corpus(),
        "historical_reports": freeze_history(args.history_db, temp_root),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # A baseline is append-only; accidentally running again cannot replace it.
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({
        "output": str(args.output), "sha256": digest(args.output),
        "source_files": len(source_hashes), "samples": len(result["golden_samples"]),
        "facts": sum(item["fact_count"] for item in result["golden_samples"]),
        "history_reads": len(result["historical_reports"]["runs"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
