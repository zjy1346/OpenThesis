"""Summarize explicit company-level recognition evidence from local JSON files.

Manifest contract (relative result paths are resolved beside the manifest)::

    {
      "schema": "openthesis-company-recognition-manifest-v1",
      "cohort": {"name": "fixed-five", "evaluation_as_of": "2026-10-26"},
      "companies": [{
        "company_id": "stable issuer id",
        "market": "US",
        "industry": "Industrials",
        "config_stratum": "local-default",
        "source_status": "available",
        "result_file": "results/company.json"
      }]
    }

Every manifest row contributes exactly once to the end-to-end denominator.
Missing and unaudited results remain unknown, never implicit successes.
This utility reads and writes JSON only; it does not call providers, models,
PDF parsers, or application services.

Each optional result file uses ``openthesis-company-recognition-result-v1``
and binds to the manifest's exact ``company_id``. Core audit fields are
``core_correctness_audited``, ``required_periods_satisfied``,
``financial_capabilities_ready``, ``first_automatic_completion``, and the
non-negative integer ``user_interventions``. Optional observed measurements
are ``cold_elapsed_seconds``, ``hot_elapsed_seconds``, ``model_calls``,
``ocr_calls``, ``cost_usd``, ``processed_items``, and ``repeated_items``.
Missing measurements stay unknown; they are never changed to zero.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any


MANIFEST_SCHEMA = "openthesis-company-recognition-manifest-v1"
RESULT_SCHEMA = "openthesis-company-recognition-result-v1"
OUTPUT_SCHEMA = "openthesis-company-recognition-summary-v1"
SOURCE_STATUSES = {"available", "unavailable", "unknown"}
CORE_SUCCESS_FLAGS = (
    "required_periods_satisfied",
    "financial_capabilities_ready",
    "first_automatic_completion",
)
WILSON_Z_95 = 1.959963984540054
COHORT_FIELDS = (
    "name",
    "evaluation_as_of",
    "research_period_policy",
    "data_source_config",
    "recognition_component_config",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _nonnegative_integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _load_company_result(base: Path, manifest_row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    result_name = manifest_row.get("result_file")
    if result_name is None or (isinstance(result_name, str) and not result_name.strip()):
        return None, "missing"
    if not isinstance(result_name, str):
        raise ValueError("result_file must be a path string or null")
    path = Path(result_name)
    if not path.is_absolute():
        path = base / path
    try:
        result = _read_json(path)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid"
    if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
        return None, "invalid"
    return result, "loaded"


def _classify(company_id: str, source_status: str, result: dict[str, Any] | None,
              record_status: str) -> str:
    if source_status == "unavailable":
        return "SOURCE_UNAVAILABLE"
    if source_status != "available":
        return "UNKNOWN_SOURCE_STATUS"
    if record_status == "identity_mismatch":
        return "UNKNOWN_RESULT_ID_MISMATCH"
    if record_status == "missing":
        return "UNKNOWN_MISSING_RESULT"
    if record_status != "loaded" or result is None:
        return "UNKNOWN_INVALID_RESULT"
    result_company_id = result.get("company_id")
    if not isinstance(result_company_id, str) or result_company_id.strip() != company_id:
        return "UNKNOWN_RESULT_ID_MISMATCH"
    if result.get("core_correctness_audited") is not True:
        return "UNKNOWN_NOT_AUDITED"

    for flag in CORE_SUCCESS_FLAGS:
        if result.get(flag) is False:
            return "CORE_FAILED"
    interventions = _nonnegative_integer(result.get("user_interventions"))
    if interventions is not None and interventions > 0:
        return "CORE_FAILED"
    if (all(result.get(flag) is True for flag in CORE_SUCCESS_FLAGS)
            and interventions == 0):
        return "CORE_VERIFIED"
    return "UNKNOWN_INCOMPLETE_AUDIT"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return round(value, 6)


def _wilson_interval(successes: int, denominator: int, unknown: int) -> dict[str, Any] | None:
    if denominator <= 0 or unknown > 0:
        return None
    proportion = successes / denominator
    z_squared = WILSON_Z_95 * WILSON_Z_95
    denominator_term = 1 + z_squared / denominator
    center = (proportion + z_squared / (2 * denominator)) / denominator_term
    margin = (WILSON_Z_95 * math.sqrt(
        proportion * (1 - proportion) / denominator + z_squared / (4 * denominator * denominator)
    ) / denominator_term)
    return {"confidence": 0.95, "method": "Wilson", "lower": round(max(0.0, center - margin), 6),
            "upper": round(min(1.0, center + margin), 6)}


def _rate_summary(records: list[dict[str, Any]], *, eligible_statuses: set[str]) -> dict[str, Any]:
    eligible = [row for row in records if row["source_status"] in eligible_statuses]
    denominator = len(eligible)
    successes = sum(row["classification"] == "CORE_VERIFIED" for row in eligible)
    known_failures = sum(row["classification"] in {"CORE_FAILED", "SOURCE_UNAVAILABLE"} for row in eligible)
    unknown = denominator - successes - known_failures
    complete_rate = successes / denominator if denominator > 0 and unknown == 0 else None
    lower_bound = successes / denominator if denominator > 0 else None
    return {
        "denominator": denominator,
        "verified_successes": successes,
        "known_failures": known_failures,
        "unknown": unknown,
        "success_rate": round(complete_rate, 6) if complete_rate is not None else None,
        "verified_rate_lower_bound": round(lower_bound, 6) if lower_bound is not None else None,
        "wilson_95": _wilson_interval(successes, denominator, unknown),
    }


def _complete_sum(records: list[dict[str, Any]], key: str, parser) -> int | float | None:
    if not records:
        return None
    values = [parser(row["result"].get(key)) if row["result"] is not None else None for row in records]
    if any(value is None for value in values):
        return None
    return sum(values)


def _elapsed_summary(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [value for row in records if row["result"] is not None
              if (value := _nonnegative_number(row["result"].get(key))) is not None]
    return {
        "observed": len(values),
        "unknown": len(records) - len(values),
        "p50_seconds": _percentile(values, 0.50),
        "p95_seconds": _percentile(values, 0.95),
    }


def _repeat_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"processed_items": None, "repeated_items": None, "ratio": None, "unknown": 0}
    pairs: list[tuple[int, int]] = []
    for row in records:
        result = row["result"]
        processed = _nonnegative_integer(result.get("processed_items")) if result is not None else None
        repeated = _nonnegative_integer(result.get("repeated_items")) if result is not None else None
        if processed is None or repeated is None or repeated > processed:
            return {"processed_items": None, "repeated_items": None, "ratio": None, "unknown": len(records)}
        pairs.append((processed, repeated))
    processed_total = sum(pair[0] for pair in pairs)
    repeated_total = sum(pair[1] for pair in pairs)
    ratio = repeated_total / processed_total if processed_total > 0 else None
    return {
        "processed_items": processed_total,
        "repeated_items": repeated_total,
        "ratio": round(ratio, 6) if ratio is not None else None,
        "unknown": 0,
    }


def _summarize_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    denominator = len(records)
    successes = sum(row["classification"] == "CORE_VERIFIED" for row in records)
    known_failures = sum(row["classification"] in {"CORE_FAILED", "SOURCE_UNAVAILABLE"} for row in records)
    unknown = denominator - successes - known_failures
    source_counts = {status: sum(row["source_status"] == status for row in records)
                     for status in ("available", "unavailable", "unknown")}
    classifications: dict[str, int] = {}
    for row in records:
        status = row["classification"]
        classifications[status] = classifications.get(status, 0) + 1
    return {
        "company_denominator": denominator,
        "verified_successes": successes,
        "known_failures": known_failures,
        "unknown": unknown,
        "success_rate": round(successes / denominator, 6) if denominator and unknown == 0 else None,
        "verified_rate_lower_bound": round(successes / denominator, 6) if denominator else None,
        "wilson_95": _wilson_interval(successes, denominator, unknown),
        "source_status_counts": source_counts,
        "classification_counts": dict(sorted(classifications.items())),
        "conditional_identification": _rate_summary(records, eligible_statuses={"available"}),
        "elapsed_seconds": {
            "cold": _elapsed_summary(records, "cold_elapsed_seconds"),
            "hot": _elapsed_summary(records, "hot_elapsed_seconds"),
        },
        "model_calls_total": _complete_sum(records, "model_calls", _nonnegative_integer),
        "ocr_calls_total": _complete_sum(records, "ocr_calls", _nonnegative_integer),
        "cost_usd_total": _complete_sum(records, "cost_usd", _nonnegative_number),
        "repeat_ratio": _repeat_summary(records),
    }


def _stratum_value(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else "UNKNOWN"


def summarize_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Return a deterministic summary without printing source paths or raw facts."""
    path = Path(manifest_path).resolve()
    manifest = _read_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("company recognition manifest schema is invalid")
    companies = manifest.get("companies")
    if not isinstance(companies, list):
        raise ValueError("companies must be a list")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in companies:
        if not isinstance(item, dict):
            raise ValueError("each company manifest entry must be an object")
        company_id = item.get("company_id")
        if not isinstance(company_id, str) or not company_id.strip():
            raise ValueError("company_id must be a non-empty stable issuer identifier")
        company_id = company_id.strip()
        identity_key = company_id.casefold()
        if identity_key in seen_ids:
            raise ValueError(f"duplicate company_id: {company_id}")
        seen_ids.add(identity_key)
        source_status = item.get("source_status", "unknown")
        if not isinstance(source_status, str) or source_status.casefold() not in SOURCE_STATUSES:
            raise ValueError("source_status must be available, unavailable, or unknown")
        source_status = source_status.casefold()
        result, record_status = _load_company_result(path.parent, item)
        if result is not None:
            result_company_id = result.get("company_id")
            if not isinstance(result_company_id, str) or result_company_id.strip() != company_id:
                result = None
                record_status = "identity_mismatch"
        classification = _classify(company_id, source_status, result, record_status)
        rows.append({
            "company_id": company_id,
            "market": _stratum_value(item, "market"),
            "industry": _stratum_value(item, "industry"),
            "config_stratum": _stratum_value(item, "config_stratum"),
            "source_status": source_status,
            "classification": classification,
            "record_status": record_status,
            "result": result,
        })

    overall = _summarize_group(rows)
    strata: dict[str, list[dict[str, Any]]] = {}
    for field in ("market", "industry", "config_stratum"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(row[field], []).append(row)
        strata[field] = [
            {"value": value, **_summarize_group(sorted(bucket, key=lambda row: row["company_id"].casefold()))}
            for value, bucket in sorted(buckets.items())
        ]

    conditional = _rate_summary(rows, eligible_statuses={"available"})
    cohort = manifest.get("cohort") if isinstance(manifest.get("cohort"), dict) else {}
    return {
        "schema": OUTPUT_SCHEMA,
        "cohort": {key: cohort[key] for key in COHORT_FIELDS if isinstance(cohort.get(key), str)},
        "denominator_basis": "unique company_id rows in the explicit manifest; unavailable sources remain included",
        "overall": overall,
        "end_to_end": {
            "denominator": overall["company_denominator"],
            "verified_successes": overall["verified_successes"],
            "known_failures": overall["known_failures"],
            "unknown": overall["unknown"],
            "success_rate": overall["success_rate"],
            "verified_rate_lower_bound": overall["verified_rate_lower_bound"],
            "wilson_95": overall["wilson_95"],
        },
        "conditional_identification": conditional,
        "strata": strata,
        "companies": [{key: row[key] for key in (
            "company_id", "market", "industry", "config_stratum", "source_status", "classification", "record_status"
        )} for row in rows],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path,
                        help="explicit company cohort JSON manifest")
    parser.add_argument("--output", type=Path,
                        help="write summary JSON here (default: stdout)")
    args = parser.parse_args(argv)
    summary = summarize_manifest(args.manifest)
    payload = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        manifest_path = args.manifest.resolve()
        output_path = args.output.resolve()
        manifest_data = _read_json(manifest_path)
        input_paths = {manifest_path}
        if isinstance(manifest_data, dict):
            for row in manifest_data.get("companies", []):
                if not isinstance(row, dict) or not isinstance(row.get("result_file"), str):
                    continue
                result_path = Path(row["result_file"])
                input_paths.add((result_path if result_path.is_absolute()
                                 else manifest_path.parent / result_path).resolve())
        if output_path in input_paths:
            raise ValueError("output path must not overwrite the manifest or a company result")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
