"""Compare the live 3.0.0 checkout with the immutable A-stage baseline.

This is evidence collection only: the baseline and source database are read
only, the historical database is copied with SQLite Online Backup, and the
application's public report API is exercised only against that isolated copy.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "docs/releases/core-resilience-3.0.0-baseline.json"
DEFAULT_HISTORY_DB = Path("D:/test_qa_287/openthesis.db")
EXPECTED_BASELINE_SHA256 = "ECA25337439145C32BDE7301BD0951599B4367E40FDF02A16E0A24D2DD94AA14"
EXPECTED_RUNS = 12
EXPECTED_SAMPLES = 9
EXPECTED_FACTS = 365
LANGUAGE_REQUESTS = (("zh-CN", "zh-CN"), ("zh-TW", "zh-Hant"), ("en", "en"))
EXPECTED_PUBLIC_READS = EXPECTED_RUNS * len(LANGUAGE_REQUESTS)

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _load_freezer():
    """Reuse the frozen A-stage cohort/hash implementation without editing it."""
    path = ROOT / "scripts/freeze-core-resilience.py"
    spec = importlib.util.spec_from_file_location("freeze_core_resilience", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("BASELINE_FREEZER_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FREEZER = _load_freezer()


def _load_presentation_verifier():
    spec = importlib.util.spec_from_file_location(
        "historical_presentation_adjudications", ROOT / "scripts/historical-presentation-adjudications.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("PRESENTATION_VERIFIER_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRESENTATION = _load_presentation_verifier()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _payload_hash(payload: str) -> str:
    # Deliberately match freeze_history(): hash the exact stored payload_json
    # UTF-8 bytes, not a reserialized object.
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _issue(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def compare_historical_report(expected: Mapping[str, Any], actual: Mapping[str, Any], *,
                             compare_body: bool = True) -> dict[str, Any]:
    """Compare one public AppService result; no prose-based role inference."""
    run_id = str(expected.get("run_id") or actual.get("run_id") or "")
    issues: list[dict[str, Any]] = []

    identity_checks = {
        "run_id": (expected.get("run_id"), actual.get("run_id")),
        "company_cik": (expected.get("company_cik"), actual.get("company_cik")),
        "status": (expected.get("status"), actual.get("status")),
    }
    for field, (wanted, found) in identity_checks.items():
        if wanted != found:
            issues.append(_issue("HISTORICAL_IDENTITY_CHANGED", run_id=run_id,
                                 field=field, expected=wanted, actual=found))

    markdown = actual.get("markdown")
    current_markdown_hash = hashlib.sha256(
        markdown.encode("utf-8") if isinstance(markdown, str) else b""
    ).hexdigest()
    if compare_body and current_markdown_hash != expected.get("markdown_sha256"):
        issues.append(_issue("HISTORICAL_BODY_CHANGED", run_id=run_id,
                             expected=expected.get("markdown_sha256"), actual=current_markdown_hash))
    elif not compare_body:
        issues.append(_issue("HISTORICAL_BODY_NOT_FROZEN_FOR_LANGUAGE", run_id=run_id))

    baseline_sections = {
        str(section.get("section_id") or ""): section
        for section in expected.get("visible_sections", []) if isinstance(section, dict)
    }
    current_sections = {
        str(section.get("section_id") or ""): section
        for section in actual.get("visible_sections", []) if isinstance(section, dict)
    }
    for section_id in sorted(baseline_sections.keys() - current_sections.keys()):
        old = baseline_sections[section_id]
        issues.append(_issue("HISTORICAL_SECTION_MISSING", run_id=run_id,
                             section_id=section_id,
                             artifact_ids=list(old.get("source_artifact_ids", []))))
    visibility_extensions: list[str] = []
    for section_id in sorted(current_sections.keys() - baseline_sections.keys()):
        current = current_sections[section_id]
        # The frozen public list excluded non-substantive, already-rendered
        # sections. The new list explicitly describes visibility separately.
        # An additive non-material descriptor is not a body regression; its
        # typed role is checked below. Newly substantive metadata still needs
        # review, even when the exact rendered body has not changed.
        if current.get("is_substantive") is False:
            visibility_extensions.append(section_id)
        else:
            issues.append(_issue("HISTORICAL_MATERIAL_VISIBILITY_CHANGED", run_id=run_id,
                                 section_id=section_id,
                                 artifact_ids=list(current.get("source_artifact_ids", []))))

    section_results: list[dict[str, Any]] = []
    for section_id in sorted(baseline_sections.keys() | current_sections.keys()):
        old = baseline_sections.get(section_id, {})
        current = current_sections.get(section_id, {})
        artifact_ids = list(dict.fromkeys((
            *(old.get("source_artifact_ids", []) if isinstance(old.get("source_artifact_ids", []), list) else []),
            *(current.get("source_artifact_ids", []) if isinstance(current.get("source_artifact_ids", []), list) else []),
        )))
        valid_roles = {
            "analysis", "verified_analysis", "assumption", "fact_table",
            "source", "notice", "placeholder", "unknown",
        }
        raw_roles = current.get("content_roles")
        roles = [str(value) for value in raw_roles
                 if isinstance(value, str)] if isinstance(raw_roles, list) else []
        if not roles and isinstance(current.get("role"), str):
            # Compatibility for early public snapshots that exposed one role.
            roles = [str(current["role"])]
        semantics = current.get("content_semantics")
        semantic_nodes = [item for item in semantics if isinstance(item, dict)] if isinstance(semantics, list) else []
        semantic_roles = [str(item.get("role") or "unknown") for item in semantic_nodes]
        semantic_sources = [
            (item.get("role") in {"notice", "placeholder", "source"}
             and item.get("materiality") == "non_material")
            or
            (
                isinstance(item.get("source_ids"), list)
                and any(isinstance(value, str) and value.strip() for value in item["source_ids"])
            )
            or (isinstance(item.get("validation_source_artifact_id"), str)
                and bool(item["validation_source_artifact_id"].strip()))
            for item in semantic_nodes
        ]
        role_values_valid = bool(roles) and all(role in valid_roles and role != "unknown" for role in roles)
        semantic_roles_match = (
            bool(semantic_nodes)
            and set(semantic_roles) == set(roles)
            and all(role in valid_roles and role != "unknown" for role in semantic_roles)
        )
        role_proven = role_values_valid and semantic_roles_match and all(semantic_sources)
        role = roles[0] if len(set(roles)) == 1 else "mixed" if roles else "unknown"
        # The A-stage baseline predates typed role metadata. Current public
        # material semantic nodes require source references. Explicitly typed
        # non-material notices need no factual verification to be excluded
        # from analysis. Neither case infers roles from rendered prose.
        if not role_proven and (old or current):
            issues.append(_issue("LEGACY_SECTION_ROLE_UNPROVEN", run_id=run_id,
                                 section_id=section_id, artifact_ids=artifact_ids))
        if roles and semantic_nodes and set(semantic_roles) != set(roles):
            issues.append(_issue("HISTORICAL_CONTENT_ROLES_INCONSISTENT", run_id=run_id,
                                 section_id=section_id, artifact_ids=artifact_ids,
                                 content_roles=roles, semantic_roles=sorted(set(semantic_roles))))
        substantive = current.get("is_substantive")
        non_substantive_roles = {"notice", "placeholder", "source"}
        if roles and set(roles).issubset(non_substantive_roles) and substantive is True:
            issues.append(_issue("PLACEHOLDER_COUNTED_SUBSTANTIVE", run_id=run_id,
                                 section_id=section_id, artifact_ids=artifact_ids))
        section_results.append({
            "section_id": section_id,
            "artifact_ids": artifact_ids,
            "role": role,
            "content_roles": roles,
            "role_proven": role_proven,
            "role_source": "typed_public_content_semantics" if role_proven else "unproven_or_missing_source_semantics",
            "content_semantics": semantic_nodes,
            "is_visible": current.get("is_visible"),
            "is_substantive": substantive,
            "baseline_is_substantive": old.get("is_substantive"),
            "completeness": current.get("completeness"),
        })

    read_state = actual.get("read_state", actual.get("report_read_state"))
    pending_codes = {"LEGACY_SECTION_ROLE_UNPROVEN", "HISTORICAL_BODY_NOT_FROZEN_FOR_LANGUAGE",
                     "HISTORICAL_MATERIAL_VISIBILITY_CHANGED"}
    if read_state == "diagnostic_only":
        issues.append(_issue("HISTORICAL_READ_DIAGNOSTIC_ONLY", run_id=run_id,
                             read_state=read_state))
        pending_codes.add("HISTORICAL_READ_DIAGNOSTIC_ONLY")

    if any(item["code"] not in pending_codes for item in issues):
        state = "FAIL"
    elif issues:
        state = "PENDING"
    else:
        state = "PASS"
    return {
        "run_id": run_id,
        "identity": {key: {"expected": pair[0], "actual": pair[1], "matches": pair[0] == pair[1]}
                     for key, pair in identity_checks.items()},
        "markdown_sha256": current_markdown_hash,
        "body_matches": current_markdown_hash == expected.get("markdown_sha256") if compare_body else None,
        "body_comparison": "frozen_hash" if compare_body else "not_frozen_for_language",
        "read_state": read_state,
        "is_substantive": actual.get("is_substantive"),
        "counted_as_pass": state == "PASS",
        "state": state,
        "visibility_extensions": visibility_extensions,
        "sections": section_results,
        "issues": issues,
    }


def read_historical_reports(service: Any, expected_runs: Sequence[Mapping[str, Any]],
                            source_runs: Sequence[Mapping[str, Any]],
                            default_languages: Mapping[str, str], *,
                            presentation_entries: Mapping[str, Any] | None = None,
                            source_hashes: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Read every frozen run in each requested language through AppService."""
    from openthesis.i18n import normalize_language

    expected_by_id = {str(item.get("run_id") or ""): item for item in expected_runs}
    source_by_id = {str(item.get("run_id") or ""): item for item in source_runs}
    result: dict[str, Any] = {
        "expected_runs": len(expected_by_id),
        "actual_runs": len(expected_by_id.keys() & source_by_id.keys()),
        "expected_public_read_calls": len(expected_by_id) * len(LANGUAGE_REQUESTS),
        "actual_public_read_calls": 0,
        "runs": [],
        "language_reads": [],
        "requested_language_counts": {},
        "actual_language_counts": {},
    }

    for run_id in sorted(expected_by_id):
        expected = expected_by_id[run_id]
        source_run = source_by_id.get(run_id)
        default_language = normalize_language(default_languages.get(run_id))
        if source_run is None:
            missing = {
                "run_id": run_id, "state": "FAIL", "counted_as_pass": False,
                "issues": [_issue("HISTORY_RUN_MISSING", run_id=run_id)],
            }
            result["runs"].append(missing)
            for requested_language, canonical_language in LANGUAGE_REQUESTS:
                result["language_reads"].append({
                    "run_id": run_id,
                    "requested_language": requested_language,
                    "expected_report_language": canonical_language,
                    "state": "FAIL", "counted_as_pass": False,
                    "issues": [_issue("HISTORY_RUN_MISSING", run_id=run_id)],
                })
            continue

        for requested_language, expected_language in LANGUAGE_REQUESTS:
            is_frozen_language = default_language == expected_language
            result["actual_public_read_calls"] += 1
            result["requested_language_counts"][requested_language] = (
                result["requested_language_counts"].get(requested_language, 0) + 1
            )
            try:
                report = service.get_report(run_id, language=requested_language)
                reported_language = str(report.get("report_language") or "unknown")
                recognized_language = reported_language in {"zh-CN", "zh-Hant", "zh-TW", "en"}
                canonical_reported_language = normalize_language(reported_language) if recognized_language else reported_language
                result["actual_language_counts"][canonical_reported_language] = (
                    result["actual_language_counts"].get(canonical_reported_language, 0) + 1
                )
                observed = {**report, "company_cik": source_run.get("company_cik")}
                comparison = compare_historical_report(
                    expected, observed, compare_body=is_frozen_language
                )
                comparison.update({
                    "requested_language": requested_language,
                    "expected_report_language": expected_language,
                    "report_language": reported_language,
                })
                language_issue: dict[str, Any] | None = None
                if not recognized_language or canonical_reported_language != expected_language:
                    language_issue = _issue(
                        "HISTORICAL_LANGUAGE_CHANGED", run_id=run_id,
                        requested=requested_language, expected=expected_language,
                        actual=reported_language,
                    )
                elif report.get("ticker") != source_run.get("ticker"):
                    language_issue = _issue(
                        "HISTORICAL_TICKER_CHANGED", run_id=run_id,
                        expected=source_run.get("ticker"), actual=report.get("ticker"),
                    )
                if language_issue is not None:
                    comparison["issues"].append(language_issue)
                    comparison["state"] = "FAIL"
                    comparison["counted_as_pass"] = False
                if is_frozen_language and presentation_entries is not None and run_id in presentation_entries:
                    from openthesis.application.reports import artifacts_for_report_projection
                    revision = service.storage.latest_report_revision(run_id)
                    artifacts = artifacts_for_report_projection(service.storage.get_artifacts(run_id),
                        revision["payload"] if revision else None)
                    try:
                        comparison["presentation_adjudication"] = PRESENTATION.approved_comparison(
                            comparison, expected, presentation_entries[run_id],
                            source_hashes=(source_hashes or {}).get(run_id, {}), artifacts=artifacts)
                    except ValueError as exc:
                        # Retain strict state and its issues. A rejected erratum
                        # is a separate failure, never hidden by a new hash.
                        comparison["presentation_adjudication"] = {
                            "run_id": run_id, "state": "FAIL", "error_code": str(exc)}
                result["language_reads"].append(comparison)
                if is_frozen_language:
                    result["runs"].append(comparison)
            except Exception as exc:
                failed = {
                    "run_id": run_id,
                    "requested_language": requested_language,
                    "expected_report_language": expected_language,
                    "state": "FAIL", "counted_as_pass": False,
                    "issues": [_issue(
                        "HISTORICAL_PUBLIC_READ_FAILED", run_id=run_id,
                        error_code=getattr(exc, "code", "") or type(exc).__name__,
                        error_detail=str(exc)[:500],
                    )],
                }
                result["language_reads"].append(failed)
                if is_frozen_language:
                    result["runs"].append(failed)

    for requested_language, _canonical in LANGUAGE_REQUESTS:
        result["requested_language_counts"].setdefault(requested_language, 0)
    result["language_read_pass"] = sum(item.get("state") == "PASS" for item in result["language_reads"])
    result["language_read_pending"] = sum(item.get("state") == "PENDING" for item in result["language_reads"])
    result["language_read_failed"] = sum(item.get("state") == "FAIL" for item in result["language_reads"])
    return result


def _value_differences(ticker: str, path: str, expected: Any, actual: Any) -> list[dict[str, Any]]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences: list[dict[str, Any]] = []
        for key in sorted(expected.keys() | actual.keys()):
            next_path = f"{path}.{key}" if path else str(key)
            if key not in expected:
                differences.append({"ticker": ticker, "field": next_path,
                                    "expected": "<missing>", "actual": actual[key]})
            elif key not in actual:
                differences.append({"ticker": ticker, "field": next_path,
                                    "expected": expected[key], "actual": "<missing>"})
            else:
                differences.extend(_value_differences(ticker, next_path, expected[key], actual[key]))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        key_field = next((field for field in ("source_id", "artifact_id")
                          if all(isinstance(item, dict) and field in item for item in (*expected, *actual))), None)
        if key_field:
            old = {str(item[key_field]): item for item in expected}
            new = {str(item[key_field]): item for item in actual}
            differences = []
            for identity in sorted(old.keys() | new.keys()):
                path_key = f"{path}[{identity}]"
                if identity not in old:
                    differences.append({"ticker": ticker, "field": path_key,
                                        "expected": "<missing>", "actual": new[identity]})
                elif identity not in new:
                    differences.append({"ticker": ticker, "field": path_key,
                                        "expected": old[identity], "actual": "<missing>"})
                else:
                    differences.extend(_value_differences(ticker, path_key, old[identity], new[identity]))
            return differences
        if len(expected) != len(actual):
            return [{"ticker": ticker, "field": path + ".length",
                     "expected": len(expected), "actual": len(actual)}]
        differences = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            differences.extend(_value_differences(ticker, f"{path}[{index}]", left, right))
        return differences
    if expected != actual:
        return [{"ticker": ticker, "field": path, "expected": expected, "actual": actual}]
    return []


def compare_golden_samples(expected: Sequence[Mapping[str, Any]],
                           actual: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare complete frozen sample records, including original byte hashes."""
    old = {str(item.get("ticker") or ""): dict(item) for item in expected}
    new = {str(item.get("ticker") or ""): dict(item) for item in actual}
    differences: list[dict[str, Any]] = []
    for ticker in sorted(old.keys() | new.keys()):
        if ticker not in old:
            differences.append({"ticker": ticker, "field": "<sample>",
                                "expected": "<missing>", "actual": new[ticker]})
        elif ticker not in new:
            differences.append({"ticker": ticker, "field": "<sample>",
                                "expected": old[ticker], "actual": "<missing>"})
        else:
            differences.extend(_value_differences(ticker, "", old[ticker], new[ticker]))
    return {"expected_samples": len(old), "actual_samples": len(new),
            "expected_facts": sum(int(item.get("fact_count", 0)) for item in expected),
            "actual_facts": sum(int(item.get("fact_count", 0)) for item in actual),
            "matches": not differences, "differences": differences}


def logical_database_sha256(path: Path) -> str:
    """Hash user-visible SQLite schema and rows, independent of WAL layout."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        schema = [tuple(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )]
        tables = [row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        contents: dict[str, list[list[Any]]] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = []
            for row in connection.execute(f"SELECT * FROM {quoted}"):
                values = []
                for value in row:
                    if value is None:
                        values.append(["null", None])
                    elif isinstance(value, bytes):
                        values.append(["bytes", value.hex()])
                    elif isinstance(value, float):
                        values.append(["float", value.hex()])
                    elif isinstance(value, int):
                        values.append(["int", str(value)])
                    else:
                        values.append(["text", str(value)])
                rows.append(values)
            contents[table] = sorted(rows, key=_canonical)
        encoded = _canonical({"schema": schema, "tables": contents}).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    finally:
        connection.close()


def _read_source_snapshot(database: Path) -> tuple[sqlite3.Connection, list[dict[str, Any]], list[dict[str, Any]], str]:
    before_hash = FREEZER.digest(database)
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA query_only=ON")
    check = source.execute("PRAGMA quick_check").fetchone()[0]
    if check != "ok":
        source.close()
        raise RuntimeError(f"HISTORY_SOURCE_INTEGRITY:{check}")
    runs = [dict(row) for row in source.execute(
        "SELECT r.run_id,r.company_cik,r.status,r.payload_json,c.ticker "
        "FROM research_runs r JOIN companies c ON c.cik=r.company_cik ORDER BY r.run_id"
    )]
    artifacts = [dict(row) for row in source.execute(
        "SELECT artifact_id,run_id,artifact_type,payload_json FROM artifacts ORDER BY artifact_id"
    )]
    return source, runs, artifacts, before_hash


def _compare_artifacts(expected: Sequence[Mapping[str, Any]],
                       actual: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    old = {str(item.get("artifact_id") or ""): dict(item) for item in expected}
    new = {str(item.get("artifact_id") or ""): dict(item) for item in actual}
    differences = []
    for artifact_id in sorted(old.keys() | new.keys()):
        if artifact_id not in old:
            item = new[artifact_id]
            differences.append({"run_id": item.get("run_id"), "artifact_id": artifact_id,
                                "field": "<artifact>", "expected": "<missing>", "actual": item})
        elif artifact_id not in new:
            item = old[artifact_id]
            differences.append({"run_id": item.get("run_id"), "artifact_id": artifact_id,
                                "field": "<artifact>", "expected": item, "actual": "<missing>"})
        else:
            old_item, new_item = old[artifact_id], new[artifact_id]
            old_hash = old_item.get("payload_sha256")
            new_hash = new_item.get("payload_sha256")
            if old_hash != new_hash:
                differences.append({"run_id": old_item.get("run_id"), "artifact_id": artifact_id,
                                    "field": "payload_sha256", "expected": old_hash, "actual": new_hash})
            for field in ("run_id", "artifact_type"):
                if old_item.get(field) != new_item.get(field):
                    differences.append({"run_id": old_item.get("run_id"), "artifact_id": artifact_id,
                                        "field": field, "expected": old_item.get(field),
                                        "actual": new_item.get(field)})
    return {"expected_artifacts": len(old), "actual_artifacts": len(new),
            "matches": not differences, "differences": differences}


def compare_baseline(baseline_path: Path, history_database: Path, *,
                     temp_root: Path | None = None,
                     presentation_adjudications: Path | None = None) -> dict[str, Any]:
    baseline_hash = FREEZER.digest(baseline_path)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    # Hex spelling is case-insensitive; byte-level digest equality is not.
    baseline_integrity = baseline_hash.upper() == EXPECTED_BASELINE_SHA256
    presentation_manifest = None
    if presentation_adjudications is not None:
        presentation_manifest = PRESENTATION.load_manifest(presentation_adjudications, baseline)
        if presentation_manifest["baseline_sha256"] != baseline_hash:
            raise ValueError("PRESENTATION_MANIFEST_BASELINE_CONFLICT")

    corpus_error = ""
    try:
        current_samples = FREEZER.freeze_corpus()
        current_manifest_hash = FREEZER.digest(FREEZER.CORPUS / "cohort_manifest.json")
        corpus = compare_golden_samples(baseline.get("golden_samples", []), current_samples)
    except Exception as exc:
        current_samples, current_manifest_hash = [], ""
        freeze_error = str(exc)[:500]
        corpus = {"expected_samples": len(baseline.get("golden_samples", [])),
                  "actual_samples": 0, "expected_facts": EXPECTED_FACTS,
                  "actual_facts": 0, "matches": False, "differences": [],
                  "error_code": "GOLDEN_FREEZE_FAILED", "error_detail": freeze_error}
        corpus["differences"].append({
            "ticker": "<cohort>", "field": "freeze_corpus",
            "expected": "9 unchanged source packages / 365 frozen facts",
            "actual": freeze_error,
        })
        corpus_error = type(exc).__name__
    corpus["manifest_matches"] = current_manifest_hash == baseline.get("golden_cohort_manifest_sha256")
    corpus["manifest_sha256"] = current_manifest_hash
    corpus["expected_manifest_sha256"] = baseline.get("golden_cohort_manifest_sha256")
    if not corpus["manifest_matches"]:
        corpus["differences"].append({"ticker": "<cohort>", "field": "manifest_sha256",
                                     "expected": corpus["expected_manifest_sha256"],
                                     "actual": current_manifest_hash})
    if corpus["expected_samples"] != EXPECTED_SAMPLES or corpus["actual_samples"] != EXPECTED_SAMPLES:
        corpus["differences"].append({"ticker": "<cohort>", "field": "sample_count",
                                     "expected": EXPECTED_SAMPLES,
                                     "actual": corpus["actual_samples"]})
    if corpus["expected_facts"] != EXPECTED_FACTS or corpus["actual_facts"] != EXPECTED_FACTS:
        corpus["differences"].append({"ticker": "<cohort>", "field": "fact_count",
                                     "expected": EXPECTED_FACTS, "actual": corpus["actual_facts"]})
    corpus["matches"] = not corpus["differences"]
    if corpus_error:
        corpus["error_type"] = corpus_error

    history_expected = baseline["historical_reports"]
    runs_expected = history_expected.get("runs", [])
    artifacts_expected = history_expected.get("artifact_input_hashes", [])
    history_result: dict[str, Any] = {
        "expected_runs": len(runs_expected), "actual_runs": 0, "runs": [],
        "expected_public_read_calls": EXPECTED_PUBLIC_READS, "actual_public_read_calls": 0,
        "language_reads": [], "requested_language_counts": {}, "actual_language_counts": {},
        "language_read_pass": 0, "language_read_pending": 0, "language_read_failed": 0,
        "expected_artifacts": len(artifacts_expected), "artifact_input_hashes": {},
        "provider_calls": 0,
    }
    source_before_hash = ""
    source_after_hash = ""
    source_quick_check = ""
    source = None
    comparison_temp_root = temp_root or (ROOT / "tmp/core-resilience-3.0.0")
    comparison_temp_root.mkdir(parents=True, exist_ok=True)
    try:
        source, source_runs, source_artifacts, source_before_hash = _read_source_snapshot(history_database)
        source_quick_check = "ok"
        current_artifacts = [{
            "artifact_id": item["artifact_id"], "run_id": item["run_id"],
            "artifact_type": item["artifact_type"],
            "payload_sha256": _payload_hash(item["payload_json"]),
        } for item in source_artifacts]
        history_result["artifact_input_hashes"] = _compare_artifacts(artifacts_expected, current_artifacts)
        if source_before_hash != history_expected.get("source_sha256"):
            history_result.setdefault("issues", []).append(_issue(
                "HISTORY_SOURCE_HASH_CHANGED", expected=history_expected.get("source_sha256"),
                actual=source_before_hash,
            ))

        with tempfile.TemporaryDirectory(
            prefix="core-resilience-compare-",
            dir=str(comparison_temp_root),
        ) as temporary:
            database_clone = Path(temporary) / "openthesis.db"
            clone = sqlite3.connect(database_clone)
            try:
                source.backup(clone)
            finally:
                clone.close()
            source.close()
            source = None

            provider_counter = {"calls": 0}
            def no_provider(_config: Any) -> Any:
                provider_counter["calls"] += 1
                raise AssertionError("HISTORICAL_REPORT_READ_CALLED_PROVIDER")

            # AppService initialization (including compatibility migration) is
            # intentionally before this first logical hash.
            from openthesis.service import AppService
            from openthesis.i18n import normalize_language
            service = AppService(Path(temporary), provider_factory=no_provider)
            pre_read_hash = logical_database_sha256(database_clone)
            source_run_by_id = {str(row["run_id"]): row for row in source_runs}
            expected_by_id = {str(row["run_id"]): row for row in runs_expected}
            current_run_ids = set(source_run_by_id)
            expected_run_ids = set(expected_by_id)
            if current_run_ids != expected_run_ids:
                history_result.setdefault("issues", []).append(_issue(
                    "HISTORY_RUN_SET_CHANGED",
                    missing=sorted(expected_run_ids - current_run_ids),
                    added=sorted(current_run_ids - expected_run_ids),
                ))
            fallback_language = service.storage.get_setting("report_language", "zh-CN")
            default_languages: dict[str, str] = {}
            for run_id, source_run in source_run_by_id.items():
                try:
                    payload = json.loads(str(source_run.get("payload_json") or "{}"))
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                payload_language = payload.get("report_language") if isinstance(payload, dict) else None
                default_languages[run_id] = normalize_language(str(payload_language or fallback_language))

            presentation_options = {}
            if presentation_manifest is not None:
                presentation_options = {
                    "presentation_entries": {row["run_id"]: row for row in presentation_manifest["entries"]},
                    "source_hashes": {run_id: {row["artifact_id"]: row["payload_sha256"]
                        for row in current_artifacts if row["run_id"] == run_id}
                        for run_id in expected_by_id},
                }
            history_result.update(read_historical_reports(
                service, runs_expected, source_runs, default_languages, **presentation_options
            ))
            history_result["expected_public_read_calls"] = EXPECTED_PUBLIC_READS
            expected_request_counts = {requested: EXPECTED_RUNS for requested, _ in LANGUAGE_REQUESTS}
            if history_result["requested_language_counts"] != expected_request_counts:
                history_result.setdefault("issues", []).append(_issue(
                    "HISTORICAL_LANGUAGE_REQUEST_COVERAGE_INCOMPLETE",
                    expected=expected_request_counts,
                    actual=history_result["requested_language_counts"],
                ))
            expected_result_counts = {canonical: EXPECTED_RUNS for _, canonical in LANGUAGE_REQUESTS}
            if history_result["actual_language_counts"] != expected_result_counts:
                history_result.setdefault("issues", []).append(_issue(
                    "HISTORICAL_LANGUAGE_RESULT_COVERAGE_INCOMPLETE",
                    expected=expected_result_counts,
                    actual=history_result["actual_language_counts"],
                ))
            if history_result["actual_public_read_calls"] != EXPECTED_PUBLIC_READS:
                history_result.setdefault("issues", []).append(_issue(
                    "HISTORICAL_PUBLIC_READ_COUNT_MISMATCH",
                    expected=EXPECTED_PUBLIC_READS,
                    actual=history_result["actual_public_read_calls"],
                ))
            post_read_hash = logical_database_sha256(database_clone)
            provider_calls = provider_counter["calls"]
            history_result["provider_calls"] = provider_calls
            with closing(sqlite3.connect(database_clone.resolve().as_uri() + "?mode=ro", uri=True)) as metadata_reader:
                metadata_reader.execute("PRAGMA query_only=ON")
                schema_version = metadata_reader.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
            history_result["clone_logical_database"] = {
                "before_read_sha256": pre_read_hash,
                "after_read_sha256": post_read_hash,
                "unchanged": pre_read_hash == post_read_hash,
                "schema_version_after_initialization": schema_version[0] if schema_version else None,
            }
    except Exception as exc:
        history_result.setdefault("issues", []).append(_issue(
            "HISTORY_COMPARISON_FAILED", error_type=type(exc).__name__,
            error_detail=str(exc)[:500],
        ))
    finally:
        if source is not None:
            source.close()
        try:
            source_after_hash = FREEZER.digest(history_database)
        except OSError:
            source_after_hash = ""

    source_unchanged = bool(source_before_hash) and source_before_hash == source_after_hash
    history_result["source_database"] = {
        "path": str(history_database),
        "quick_check": source_quick_check,
        "baseline_sha256": history_expected.get("source_sha256"),
        "before_sha256": source_before_hash,
        "after_sha256": source_after_hash,
        "matches_baseline": source_before_hash == history_expected.get("source_sha256"),
        "unchanged": source_unchanged,
    }
    if not source_unchanged:
        history_result.setdefault("issues", []).append(_issue(
            "HISTORY_SOURCE_CHANGED_DURING_COMPARISON",
            before=source_before_hash, after=source_after_hash,
        ))
    if history_result["expected_runs"] != EXPECTED_RUNS or history_result["actual_runs"] != EXPECTED_RUNS:
        history_result.setdefault("issues", []).append(_issue(
            "HISTORY_RUN_COUNT_MISMATCH", expected=EXPECTED_RUNS,
            actual=history_result["actual_runs"],
        ))
    if history_result["provider_calls"] != 0:
        history_result.setdefault("issues", []).append(_issue(
            "HISTORICAL_READ_PROVIDER_CALLS_NONZERO", actual=history_result["provider_calls"],
        ))
    logical = history_result.get("clone_logical_database", {})
    if logical and not logical.get("unchanged"):
        history_result.setdefault("issues", []).append(_issue(
            "PUBLIC_READ_MUTATED_CLONE_DATABASE", before=logical.get("before_read_sha256"),
            after=logical.get("after_read_sha256"),
        ))
    history_result["passes"] = sum(item.get("state") == "PASS" for item in history_result["runs"])
    history_result["pending"] = sum(item.get("state") == "PENDING" for item in history_result["runs"])
    history_result["failed"] = sum(item.get("state") == "FAIL" for item in history_result["runs"])
    history_result["all_historical_reads_pass"] = (
        history_result["passes"] == EXPECTED_RUNS
        and history_result["pending"] == 0
        and history_result["failed"] == 0
        and history_result["language_read_pass"] == EXPECTED_PUBLIC_READS
        and history_result["language_read_pending"] == 0
        and history_result["language_read_failed"] == 0
        and history_result["actual_public_read_calls"] == EXPECTED_PUBLIC_READS
        and not history_result.get("issues")
        and history_result["artifact_input_hashes"].get("matches") is True
        and source_unchanged
        and provider_calls == 0
        and logical.get("unchanged") is True
    )

    hard_failures = (
        not baseline_integrity
        or not corpus["matches"]
        or history_result["failed"] > 0
        or history_result["language_read_failed"] > 0
        or bool(history_result.get("issues"))
        or history_result["artifact_input_hashes"].get("matches") is not True
        or not source_unchanged
        or (logical and not logical.get("unchanged"))
        or provider_calls != 0
    )
    pending = (
        history_result["pending"] > 0
        or history_result["passes"] != EXPECTED_RUNS
        or history_result["language_read_pending"] > 0
        or history_result["language_read_pass"] != EXPECTED_PUBLIC_READS
    )
    state = "FAIL" if hard_failures else "PENDING" if pending else "PASS"
    if presentation_manifest is not None:
        def reviewed_state(item):
            adjudication = item.get("presentation_adjudication", {})
            if adjudication.get("state") == "PASS_APPROVED_PRESENTATION_ERRATUM":
                return "PASS_APPROVED_PRESENTATION_ERRATUM"
            return "FAIL" if adjudication.get("state") == "FAIL" else item["state"]

        run_states = [reviewed_state(item) for item in history_result["runs"]]
        read_states = [reviewed_state(item) for item in history_result["language_reads"]]
        used = {item["run_id"] for item in history_result["runs"] if "presentation_adjudication" in item}
        unused = sorted({item["run_id"] for item in presentation_manifest["entries"]} - used)
        reviewed_fail = ("FAIL" in run_states or "FAIL" in read_states or bool(unused)
            or not baseline_integrity or not corpus["matches"] or bool(history_result.get("issues"))
            or history_result["artifact_input_hashes"].get("matches") is not True
            or not source_unchanged or logical.get("unchanged") is not True or provider_calls != 0)
        history_result["approved_presentation_review"] = {
            "manifest_sha256": PRESENTATION.MANIFEST_SHA256,
            "strict_state": state,
            "state": "FAIL" if reviewed_fail else "PENDING" if "PENDING" in read_states
                or len(read_states) != EXPECTED_PUBLIC_READS or len(run_states) != EXPECTED_RUNS else "PASS",
            "original_language_pass": run_states.count("PASS"),
            "original_language_approved_errata": run_states.count("PASS_APPROVED_PRESENTATION_ERRATUM"),
            "original_language_failed": run_states.count("FAIL"),
            "language_pending": read_states.count("PENDING"),
            "language_failed": read_states.count("FAIL"),
            "unused_run_ids": unused,
        }
    return {
        "schema_version": "openthesis.core-resilience.comparison.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline": {"path": str(baseline_path), "sha256": baseline_hash,
                     "expected_sha256": EXPECTED_BASELINE_SHA256,
                     "integrity_matches": baseline_integrity},
        "golden_corpus": corpus,
        "historical_reports": history_result,
        "scope_note": "A/B evidence only; this does not recompile the 365 answers or accept a 3.0.0 release.",
        "state": state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-db", type=Path, default=DEFAULT_HISTORY_DB)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--presentation-adjudications", type=Path,
                        help="Separately review approved display errata; strict results remain unchanged.")
    parser.add_argument("--output", type=Path, required=True,
                        help="Write a new comparison record; existing files are never overwritten.")
    args = parser.parse_args()
    temp_root = ROOT / "tmp/core-resilience-3.0.0"
    temp_root.mkdir(parents=True, exist_ok=True)
    result = compare_baseline(args.baseline, args.history_db, temp_root=temp_root,
                              presentation_adjudications=args.presentation_adjudications)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"state": result["state"], "output": str(args.output),
                      "golden_samples": result["golden_corpus"]["actual_samples"],
                      "golden_facts": result["golden_corpus"]["actual_facts"],
                      "historical_runs": result["historical_reports"]["actual_runs"],
                      "historical_reads": result["historical_reports"]["actual_public_read_calls"],
                      "language_read_pending": result["historical_reports"]["language_read_pending"],
                      "passes": result["historical_reports"]["passes"],
                      "pending": result["historical_reports"]["pending"],
                      "failed": result["historical_reports"]["failed"]}, ensure_ascii=False))
    return 0 if result["state"] == "PASS" else 2 if result["state"] == "PENDING" else 1


if __name__ == "__main__":
    raise SystemExit(main())
