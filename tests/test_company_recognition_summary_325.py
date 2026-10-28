import importlib.util
import json
from pathlib import Path
import shutil
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "summarize-company-recognition.py"
SPEC = importlib.util.spec_from_file_location("company_recognition_summary_325", SCRIPT)
summary_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary_script)


@pytest.fixture
def tmp_path():
    path = Path(__file__).parent / ".company-recognition-325" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def _audited_result(company_id, **overrides):
    result = {
        "schema": summary_script.RESULT_SCHEMA,
        "company_id": company_id,
        "core_correctness_audited": True,
        "required_periods_satisfied": True,
        "financial_capabilities_ready": True,
        "first_automatic_completion": True,
        "user_interventions": 0,
    }
    result.update(overrides)
    return result


def _write_result(root, name, payload):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path.name if path.parent == root else str(path.relative_to(root))


def _write_manifest(root, companies, *, cohort=None):
    path = root / "cohort.json"
    path.write_text(json.dumps({
        "schema": summary_script.MANIFEST_SCHEMA,
        "cohort": cohort or {},
        "companies": companies,
    }), encoding="utf-8")
    return path


def _company(company_id, result_file=None, **values):
    row = {
        "company_id": company_id,
        "market": "US",
        "industry": "Industrials",
        "config_stratum": "local-default",
        "source_status": "available",
    }
    if result_file is not None:
        row["result_file"] = result_file
    row.update(values)
    return row


def test_only_explicit_audited_core_conditions_count_as_success(tmp_path):
    result_file = _write_result(tmp_path, "results/issuer.json", _audited_result(
        "issuer-1", allow_ai=False, facts=[], model_calls=None,
    ))
    manifest = _write_manifest(tmp_path, [_company("issuer-1", result_file)])

    summary = summary_script.summarize_manifest(manifest)

    assert summary["companies"][0]["classification"] == "CORE_VERIFIED"
    assert summary["end_to_end"]["success_rate"] == 1.0
    assert summary["end_to_end"]["wilson_95"]["method"] == "Wilson"
    assert summary["conditional_identification"]["success_rate"] == 1.0


@pytest.mark.parametrize("field", [
    "required_periods_satisfied",
    "financial_capabilities_ready",
    "first_automatic_completion",
])
def test_explicit_core_failure_cannot_be_replaced_by_allow_ai_or_facts(tmp_path, field):
    result_file = _write_result(tmp_path, "result.json", _audited_result(
        "issuer-1", **{field: False, "allow_ai": True, "facts": [{"value": 1}]},
    ))
    manifest = _write_manifest(tmp_path, [_company("issuer-1", result_file)])

    summary = summary_script.summarize_manifest(manifest)

    assert summary["companies"][0]["classification"] == "CORE_FAILED"
    assert summary["end_to_end"]["success_rate"] == 0.0


def test_user_intervention_prevents_core_verified_status(tmp_path):
    result_file = _write_result(tmp_path, "result.json", _audited_result("issuer-1", user_interventions=1))
    manifest = _write_manifest(tmp_path, [_company("issuer-1", result_file)])

    summary = summary_script.summarize_manifest(manifest)

    assert summary["companies"][0]["classification"] == "CORE_FAILED"
    assert summary["end_to_end"]["verified_successes"] == 0


def test_missing_user_intervention_count_is_unknown_not_implicit_zero(tmp_path):
    result = _audited_result("issuer-1")
    del result["user_interventions"]
    result_file = _write_result(tmp_path, "result.json", result)
    summary = summary_script.summarize_manifest(
        _write_manifest(tmp_path, [_company("issuer-1", result_file)])
    )

    assert summary["companies"][0]["classification"] == "UNKNOWN_INCOMPLETE_AUDIT"
    assert summary["end_to_end"]["verified_successes"] == 0


@pytest.mark.parametrize("result", [
    {"schema": summary_script.RESULT_SCHEMA, "company_id": "issuer-1", "allow_ai": True,
     "facts": [{"concept": "revenue"}]},
    _audited_result("issuer-1", core_correctness_audited=False),
])
def test_unaudited_or_unstructured_result_is_unknown_not_pass(tmp_path, result):
    result_file = _write_result(tmp_path, "result.json", result)
    manifest = _write_manifest(tmp_path, [_company("issuer-1", result_file)])

    summary = summary_script.summarize_manifest(manifest)

    assert summary["companies"][0]["classification"].startswith("UNKNOWN_")
    assert summary["end_to_end"]["success_rate"] is None
    assert summary["end_to_end"]["verified_rate_lower_bound"] == 0.0
    assert summary["end_to_end"]["wilson_95"] is None


def test_unavailable_sources_stay_in_end_to_end_denominator_only(tmp_path):
    good_file = _write_result(tmp_path, "good.json", _audited_result("available-issuer"))
    companies = [
        _company("unavailable-issuer", source_status="unavailable"),
        _company("available-issuer", good_file),
    ]
    summary = summary_script.summarize_manifest(_write_manifest(tmp_path, companies))

    assert summary["end_to_end"]["denominator"] == 2
    assert summary["end_to_end"]["verified_successes"] == 1
    assert summary["end_to_end"]["success_rate"] == 0.5
    assert summary["conditional_identification"]["denominator"] == 1
    assert summary["conditional_identification"]["success_rate"] == 1.0


def test_missing_result_and_unknown_source_are_explicit_unknowns(tmp_path):
    companies = [
        _company("missing-result"),
        _company("unknown-source", source_status="unknown"),
    ]
    summary = summary_script.summarize_manifest(_write_manifest(tmp_path, companies))

    assert [row["classification"] for row in summary["companies"]] == [
        "UNKNOWN_MISSING_RESULT", "UNKNOWN_SOURCE_STATUS",
    ]
    assert summary["end_to_end"]["denominator"] == 2
    assert summary["end_to_end"]["unknown"] == 2
    assert summary["end_to_end"]["success_rate"] is None
    assert summary["conditional_identification"]["denominator"] == 1
    assert summary["conditional_identification"]["unknown"] == 1


def test_result_company_identity_must_match_manifest(tmp_path):
    result_file = _write_result(tmp_path, "result.json", _audited_result(
        "different-issuer", cold_elapsed_seconds=5, cost_usd=0.1,
    ))
    summary = summary_script.summarize_manifest(
        _write_manifest(tmp_path, [_company("issuer-1", result_file)])
    )

    assert summary["companies"][0]["classification"] == "UNKNOWN_RESULT_ID_MISMATCH"
    assert summary["end_to_end"]["verified_successes"] == 0
    assert summary["overall"]["elapsed_seconds"]["cold"]["observed"] == 0
    assert summary["overall"]["cost_usd_total"] is None


def test_duplicate_company_ids_are_rejected_to_protect_unique_denominator(tmp_path):
    manifest = _write_manifest(tmp_path, [_company("issuer-1"), _company("ISSUER-1")])

    with pytest.raises(ValueError, match="duplicate company_id"):
        summary_script.summarize_manifest(manifest)


def test_market_industry_and_config_strata_are_reported_separately(tmp_path):
    first = _write_result(tmp_path, "a.json", _audited_result("issuer-a"))
    second = _write_result(tmp_path, "b.json", _audited_result("issuer-b", required_periods_satisfied=False))
    companies = [
        _company("issuer-a", first, market="US", industry="Industrials", config_stratum="local"),
        _company("issuer-b", second, market="HK", industry="Technology", config_stratum="cloud-enabled"),
    ]
    summary = summary_script.summarize_manifest(_write_manifest(tmp_path, companies))

    assert {row["value"] for row in summary["strata"]["market"]} == {"US", "HK"}
    assert {row["value"] for row in summary["strata"]["industry"]} == {"Industrials", "Technology"}
    assert {row["value"] for row in summary["strata"]["config_stratum"]} == {"local", "cloud-enabled"}
    assert all(row["company_denominator"] == 1 for row in summary["strata"]["market"])


def test_wilson_interval_and_linear_percentiles_are_deterministic():
    interval = summary_script._wilson_interval(1, 2, 0)

    assert interval == {"confidence": 0.95, "method": "Wilson", "lower": 0.094531, "upper": 0.905469}
    assert summary_script._percentile([1.0, 2.0, 3.0, 4.0], 0.50) == 2.5
    assert summary_script._percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 3.85


def test_elapsed_percentiles_use_observed_values_without_inventing_unknown_times(tmp_path):
    result_file = _write_result(tmp_path, "result.json", _audited_result(
        "issuer-1", cold_elapsed_seconds=10, hot_elapsed_seconds=2,
    ))
    summary = summary_script.summarize_manifest(
        _write_manifest(tmp_path, [_company("issuer-1", result_file), _company("issuer-2")])
    )

    cold = summary["overall"]["elapsed_seconds"]["cold"]
    hot = summary["overall"]["elapsed_seconds"]["hot"]
    assert cold == {"observed": 1, "unknown": 1, "p50_seconds": 10.0, "p95_seconds": 10.0}
    assert hot == {"observed": 1, "unknown": 1, "p50_seconds": 2.0, "p95_seconds": 2.0}


def test_unknown_usage_cost_and_repeat_metrics_remain_null(tmp_path):
    result_file = _write_result(tmp_path, "result.json", _audited_result("issuer-1"))
    summary = summary_script.summarize_manifest(
        _write_manifest(tmp_path, [_company("issuer-1", result_file)])
    )

    assert summary["overall"]["model_calls_total"] is None
    assert summary["overall"]["ocr_calls_total"] is None
    assert summary["overall"]["cost_usd_total"] is None
    assert summary["overall"]["repeat_ratio"] == {
        "processed_items": None, "repeated_items": None, "ratio": None, "unknown": 1,
    }


def test_observed_usage_cost_and_repeat_ratio_aggregate_only_complete_metrics(tmp_path):
    first = _write_result(tmp_path, "a.json", _audited_result(
        "issuer-a", model_calls=2, ocr_calls=1, cost_usd=0.1,
        processed_items=10, repeated_items=2,
    ))
    second = _write_result(tmp_path, "b.json", _audited_result(
        "issuer-b", model_calls=0, ocr_calls=3, cost_usd=0.2,
        processed_items=10, repeated_items=1,
    ))
    summary = summary_script.summarize_manifest(_write_manifest(tmp_path, [
        _company("issuer-a", first), _company("issuer-b", second),
    ]))

    assert summary["overall"]["model_calls_total"] == 2
    assert summary["overall"]["ocr_calls_total"] == 4
    assert summary["overall"]["cost_usd_total"] == pytest.approx(0.3)
    assert summary["overall"]["repeat_ratio"] == {
        "processed_items": 20, "repeated_items": 3, "ratio": 0.15, "unknown": 0,
    }


def test_partial_measurements_remain_unknown(tmp_path):
    first = _write_result(tmp_path, "a.json", _audited_result(
        "issuer-a", model_calls=2, processed_items=0, repeated_items=0,
    ))
    second = _write_result(tmp_path, "b.json", _audited_result("issuer-b"))
    summary = summary_script.summarize_manifest(_write_manifest(tmp_path, [
        _company("issuer-a", first), _company("issuer-b", second),
    ]))

    assert summary["overall"]["model_calls_total"] is None
    assert summary["overall"]["ocr_calls_total"] is None
    assert summary["overall"]["cost_usd_total"] is None
    assert summary["overall"]["repeat_ratio"] == {
        "processed_items": None, "repeated_items": None, "ratio": None, "unknown": 2,
    }


def test_zero_observed_processed_items_have_no_repeat_ratio(tmp_path):
    result_file = _write_result(tmp_path, "zero.json", _audited_result(
        "issuer-1", processed_items=0, repeated_items=0,
    ))
    summary = summary_script.summarize_manifest(
        _write_manifest(tmp_path, [_company("issuer-1", result_file)])
    )

    assert summary["overall"]["repeat_ratio"] == {
        "processed_items": 0, "repeated_items": 0, "ratio": None, "unknown": 0,
    }


def test_empty_manifest_has_null_rates_and_no_fabricated_repeat_counts(tmp_path):
    summary = summary_script.summarize_manifest(_write_manifest(tmp_path, []))

    assert summary["end_to_end"]["denominator"] == 0
    assert summary["end_to_end"]["success_rate"] is None
    assert summary["end_to_end"]["wilson_95"] is None
    assert summary["conditional_identification"]["success_rate"] is None
    assert summary["overall"]["repeat_ratio"] == {
        "processed_items": None, "repeated_items": None, "ratio": None, "unknown": 0,
    }


def test_cli_writes_summary_json_without_research_side_effects(tmp_path):
    result_file = _write_result(tmp_path, "result.json", _audited_result("issuer-1"))
    manifest = _write_manifest(tmp_path, [_company("issuer-1", result_file)])
    output = tmp_path / "out" / "summary.json"

    assert summary_script.main(["--manifest", str(manifest), "--output", str(output)]) == 0
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert parsed["schema"] == summary_script.OUTPUT_SCHEMA
    assert parsed["overall"]["company_denominator"] == 1
    assert "result_file" not in parsed["companies"][0]


def test_cli_refuses_to_overwrite_an_input_result_file(tmp_path):
    result_file = _write_result(tmp_path, "result.json", _audited_result("issuer-1"))
    manifest = _write_manifest(tmp_path, [_company("issuer-1", result_file)])

    with pytest.raises(ValueError, match="must not overwrite"):
        summary_script.main(["--manifest", str(manifest), "--output", str(tmp_path / result_file)])
