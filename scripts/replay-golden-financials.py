"""Replay frozen golden originals through production candidate/compilation seams.

No answer is fed into the extractor. No network, model or application DB is
used. Unsupported/missing originals remain explicit failures, including HTML
sources until their production extractor is wired. Source and answer fixtures
are read-only. The optional output contains numbers and hashes, not local
source paths or original excerpts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import runpy
from typing import Any

from openthesis.financial_contracts import STATEMENT_FOR


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/golden_research/v2"
REPLAY = runpy.run_path(str(ROOT / "scripts/replay-financial-ingestion.py"))
ADJUDICATIONS = runpy.run_path(str(ROOT / "scripts/golden-financial-adjudications.py"))


def canonical_currency(value: str) -> str:
    value = str(value).upper().strip()
    return "CNY" if value == "RMB" else value


def source_manifest(packet: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    issuer = packet["issuer"]
    currency = canonical_currency(answer["issuer"]["reporting_currency"])
    filings = []
    for source in packet["sources"]:
        if source["source_type"] != "annual_filing":
            continue
        locators = dict(item.split(":", 1) for item in source.get("locators", []) if ":" in item)
        filings.append({
            "document_id": locators.get("document_id", ""),
            "accession_number": source["accession"],
            "form_type": locators.get("form_type", ""),
            "fiscal_period": source["fiscal_period"],
            "period_end": locators.get("database_period_end", ""),
            "filed_at": locators.get("filed_at", ""),
            "primary_document": source["document_title"],
            "source_url": source["official_url"],
            "local_path": source["cache_reference"],
            "content_sha256": source["sha256"],
        })
    return {"company": {
        "cik": issuer.get("cik") or issuer["security_id"],
        "security_id": issuer["security_id"], "ticker": issuer["ticker"],
        "name": issuer["legal_name"], "market": issuer["market"],
        "exchange": issuer["exchange"], "reporting_currency": currency,
        "accounting_standard": answer["issuer"]["accounting_standard"],
    }, "filings": filings}


def compare_row(expected: dict[str, Any], period: dict[str, Any], source: dict[str, Any],
                accepted: list[dict[str, Any]]) -> dict[str, Any]:
    concept = expected["concept"]
    gold_scope = str(expected.get("scope", "") or "").strip().casefold()
    # This frozen cohort records group-consolidated facts, including line items
    # attributable to the parent within consolidated statements. Do not infer a
    # scope for rows whose reviewed gold contract does not say so explicitly.
    target_scope = "consolidated" if "consolidated" in gold_scope else ""
    # Explicit reviewed-gold aliases are semantic only; they never select an
    # accepted fact by amount. All other identities stay exact.
    canonical_concept = {
        "parent_equity": "equity",
        "net_income_attributable_to_parent": "parent_net_income",
        "operating_profit": "operating_income",
    }.get(concept, concept)
    matches = ([item for item in accepted
                if item["concept"] == canonical_concept
                and str(item.get("scope", "") or "").strip().casefold() == target_scope
                and item["accession"] == source["accession"]
                and item["period_end"] == period["period_end"]
                and item["fiscal_period"] == period["fiscal_period"]
                and item["period_start"] == (
                    None if STATEMENT_FOR.get(canonical_concept) == "balance_sheet"
                    else period["period_start"]
                )]
               if target_scope else [])
    base = {"concept": concept, "source_id": expected["source_id"],
            "period_end": period["period_end"], "expected_status": expected["status"],
            "expected_scope_contract": target_scope or "unsupported",
            "expected_normalized_value": expected.get("normalized_value"),
            "candidate_fact_ids": [item["fact_id"] for item in matches]}
    if not target_scope:
        return {**base, "status": "GOLD_SCOPE_UNSUPPORTED"}
    if expected["status"] != "CONFIRMED":
        # Failure to extract a field does not prove the original did not report
        # it. Such source-absence adjudication needs independent source evidence.
        return {**base, "status": "UNEXPECTED_FACT" if matches else "SOURCE_ABSENCE_REVIEW_PENDING"}
    if not matches:
        return {**base, "status": "MISSING"}
    if len(matches) != 1:
        return {**base, "status": "AMBIGUOUS"}
    actual = matches[0]
    lineage = actual.get("normalization_lineage")
    if not isinstance(lineage, dict):
        return {**base, "status": "ORIGINAL_PRECISION_UNVERIFIED"}
    try:
        amount_matches = Decimal(lineage["normalized_value"]) == Decimal(expected["normalized_value"])
        unit_matches = Decimal(lineage["scale"]) == Decimal(str(expected["unit_scale_to_base"]))
    except (KeyError, InvalidOperation, TypeError):
        return {**base, "status": "INVALID_AMOUNT_CONTRACT"}
    currency_matches = canonical_currency(actual["currency"]) == canonical_currency(expected["normalized_currency"])
    return {**base, "status": "MATCHED" if amount_matches and unit_matches and currency_matches else "MISMATCH",
            "actual_normalized_value": lineage["normalized_value"],
            "amount_matches": amount_matches, "scale_matches": unit_matches, "currency_matches": currency_matches,
            "source_page": actual["source_page"], "parser_version": actual["extractor_parser_version"]}


def canonical_digest(payload: dict[str, Any], digest_field: str) -> str:
    contents = {key: value for key, value in payload.items() if key != digest_field}
    encoded = json.dumps(contents, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_frozen_sample(sample: Path, frozen: dict[str, Any]) -> None:
    for name in ("source_packet.json", "financial_facts.json"):
        if hashlib.sha256((sample / name).read_bytes()).hexdigest() != frozen["artifact_byte_hashes"][name]:
            raise ValueError("GOLDEN_FROZEN_FILE_CHANGED")


def validate_output_path(output: Path, baseline: Path, adjudications: Path | None, selected: list[dict]) -> None:
    destination = output.resolve()
    protected = {baseline.resolve()}
    if adjudications is not None:
        protected.add(adjudications.resolve())
    for item in selected:
        packet = json.loads((ROOT / item["sample_path"] / "source_packet.json").read_text(encoding="utf-8"))
        protected.update(Path(source["cache_reference"]).resolve() for source in packet["sources"]
                         if source.get("cache_reference"))
    if destination.is_relative_to(CORPUS.resolve()) or destination in protected:
        raise ValueError("GOLDEN_OUTPUT_MUST_NOT_OVERWRITE_FROZEN_INPUT")


def evaluate(sample: Path, frozen: dict[str, Any], *, adjudications: dict | None = None) -> dict[str, Any]:
    packet_file = sample / "source_packet.json"
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    answer = json.loads((sample / "financial_facts.json").read_text(encoding="utf-8"))
    verify_frozen_sample(sample, frozen)
    if (canonical_digest(packet, "source_packet_sha256") != answer["source_packet_sha256"]
            or packet["source_packet_sha256"] != answer["source_packet_sha256"]):
        raise ValueError("GOLDEN_SOURCE_PACKET_IDENTITY_CHANGED")
    replay = REPLAY["replay"](source_manifest(packet, answer))
    verify_frozen_sample(sample, frozen)
    sources = {source["source_id"]: source for source in packet["sources"]}
    rows, reviewed_rows = [], []
    ticker = packet["issuer"]["ticker"]
    used = set()
    for period in answer["periods"]:
        for row in (*period["facts"], *period.get("extensions", [])):
            source = sources[row["source_id"]]
            strict = compare_row(row, period, source, replay["accepted_facts"])
            rows.append(strict)
            key = ADJUDICATIONS["row_key"](ticker, period, row)
            entry = (adjudications or {}).get(key)
            if entry is None:
                reviewed_rows.append(strict)
                continue
            original = Path(source["cache_reference"])
            ADJUDICATIONS["verify_entry"](entry, ticker, period, row, source, packet, frozen, original.read_bytes())
            used.add(key)
            corrected = {**row, "concept": entry["corrected_concept"]}
            observations = entry["source_observations"]
            # Exact reviewed tag AND context, never a same-valued alternate tag.
            accepted = [fact for fact in replay["accepted_facts"]
                        if any(fact["reported_concept"] == observation["reported_concept"]
                               and fact["source_column"] == "context:" + observation["context_id"]
                               for observation in observations)]
            reviewed = compare_row(corrected, period, source, accepted)
            reviewed_rows.append({**reviewed, "concept": row["concept"],
                "corrected_concept": corrected["concept"], "strict_status": strict["status"],
                "erratum_row_sha256": entry["row_sha256"],
                "status": "MATCHED_APPROVED_ERRATUM" if reviewed["status"] == "MATCHED" else reviewed["status"]})
    if adjudications is not None and used != {key for key in adjudications if key[0] == ticker}:
        raise ValueError("ERRATUM_ROW_NOT_IN_FROZEN_SAMPLE")
    counts = dict(Counter(row["status"] for row in rows))
    result = {"ticker": ticker, "status": "PASS" if counts == {"MATCHED": len(rows)} else "FAIL",
              "counts": counts, "rows": rows, "production_replay": replay}
    if adjudications is not None:
        result["adjudicated_counts"] = dict(Counter(row["status"] for row in reviewed_rows))
        result["adjudicated_rows"] = reviewed_rows
        result["adjudicated_status"] = ("PASS" if all(row["status"] in {
            "MATCHED", "MATCHED_APPROVED_ERRATUM"} for row in reviewed_rows) else "FAIL")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", action="append", help="select issuer; repeat for multiple issuers")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=ROOT / "docs/releases/core-resilience-3.0.0-baseline.json")
    parser.add_argument("--adjudications", type=Path, help="optional approved concept-only errata; strict result is retained")
    args = parser.parse_args()
    cohort = json.loads((CORPUS / "cohort_manifest.json").read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    adjudications = (ADJUDICATIONS["load_adjudications"](args.adjudications, args.baseline)
                    if args.adjudications is not None else None)
    frozen = {item["ticker"]: item for item in baseline["golden_samples"]}
    selected = [item for item in cohort["selected_issuers"]
                if not args.ticker or item["ticker"] in args.ticker]
    if not selected:
        raise ValueError("GOLDEN_SAMPLE_SELECTION_EMPTY")
    validate_output_path(args.output, args.baseline, args.adjudications, cohort["selected_issuers"])
    rows = []
    for item in selected:
        print(json.dumps({"ticker": item["ticker"], "state": "reparsing_originals"}), flush=True)
        result = evaluate(ROOT / item["sample_path"], frozen[item["ticker"]], adjudications=adjudications)
        rows.append(result)
        progress = {"ticker": item["ticker"], "status": result["status"], "counts": result["counts"]}
        if adjudications is not None:
            progress.update(adjudicated_status=result["adjudicated_status"], adjudicated_counts=result["adjudicated_counts"])
        print(json.dumps(progress), flush=True)
    payload = {"schema": "openthesis.golden-production-replay.v1", "read_only_sources": True,
               "model_calls": 0, "network_calls": 0, "application_database_writes": 0,
               "cohort_sha256": hashlib.sha256((CORPUS / "cohort_manifest.json").read_bytes()).hexdigest(),
               "status": "PASS" if all(row["status"] == "PASS" for row in rows) else "FAIL", "samples": rows}
    if adjudications is not None:
        payload["adjudications_sha256"] = hashlib.sha256(args.adjudications.read_bytes()).hexdigest()
        payload["adjudicated_status"] = "PASS" if all(row["adjudicated_status"] == "PASS" for row in rows) else "FAIL"
    REPLAY["_write_output"](args.output, payload)
    # Do not hide the legacy FAIL or turn an unresolved source gap into a pass.
    raise SystemExit(0 if payload.get("adjudicated_status", payload["status"]) == "PASS" else 1)


if __name__ == "__main__":
    main()
