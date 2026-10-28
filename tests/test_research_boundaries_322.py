"""Local-only boundary tests for research-input-stability 3.2.2.

These tests use tmp_path-backed stores and deterministic workers; they never
contact a model provider or read a user database.
"""
from __future__ import annotations

import json
import time

import pytest

from openthesis.financial_checkpoint import (
    CheckpointKey,
    PdfWindow,
    WindowCheckpoint,
    WindowCheckpointStore,
    run_checkpointed_windows,
)
from openthesis.providers import ProviderError
from openthesis.research_execution.call_budget import (
    extend_plan,
    plan_status,
    prepare_external_plan,
    prepare_plan,
    record_gap,
    reserve,
    settle,
)
from openthesis.research_execution.source_compiler import (
    compact_record_table,
    expand_record_tables,
)
from test_assertion_retention_312 import setup


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_record_tables_round_trip_heterogeneous_values_and_only_compact_when_smaller():
    repeated_source = "Original disclosure phrase retained exactly. " * 80
    records = []
    for index in range(16):
        row = {
            "kind": "repeated" if index in {3, 4} else "segment",
            "source_text": repeated_source,
            "enabled": index % 2 == 0,
            "integer_zero": 0,
            "float_zero": 0.0,
            "nested": {"sequence": [index % 3, None, {"confirmed": index % 2 == 0}]},
        }
        if index % 4 == 0:
            row["nullable"] = None
        elif index % 4 == 1:
            row["nullable"] = False
        if index % 3:
            row["heterogeneous_key"] = {"value": index, "note": ["retain", None]}
        records.append(row)
    records.extend([records[3], records[3]])

    compact = compact_record_table(records)
    assert isinstance(compact, dict) and compact.get("record_table") == "columns-data-v1"
    assert len(_json(compact).encode("utf-8")) < len(_json(records).encode("utf-8"))
    expanded = expand_record_tables(compact)
    assert _json(expanded) == _json(records)
    assert type(expanded[0]["integer_zero"]) is int
    assert type(expanded[0]["float_zero"]) is float
    assert type(expanded[0]["enabled"]) is bool
    assert "nullable" not in expanded[2]
    assert expanded[0]["nullable"] is None

    short = [{"value": 0}, {"value": 0.0}, {"value": False}]
    assert _json(compact_record_table(short)) == _json(short)

    incompressible = [
        {f"unique_schema_{index}": f"row-{index}-" + str(index * 1_000_003),
         f"nested_schema_{index}": [index, {"v": index + 0.5}]}
        for index in range(12)
    ]
    unchanged = compact_record_table(incompressible)
    assert not (isinstance(unchanged, dict) and unchanged.get("record_table") == "columns-data-v1")
    assert _json(unchanged) == _json(incompressible)


def test_record_table_document_groups_preserve_document_period_and_unit_fields():
    records = []
    for document_number in range(2):
        document_id = f"doc-{document_number}"
        for row_number in range(8):
            records.append({
                "document_id": document_id,
                "accession_number": f"000000000{document_number}26-000001",
                "document_sha256": f"sha256-{document_number}" * 8,
                "form_type": "10-K",
                "source_url": f"https://example.test/filing/{document_number}",
                "document_revision": "original",
                "period": f"FY{2024 + document_number}",
                "fiscal_period": f"FY{2024 + document_number}",
                "period_end": f"{2024 + document_number}-12-31",
                "unit": "USD millions" if document_number == 0 else "USD thousands",
                "statement_name": "Consolidated income statement",
                "metric": f"metric-{row_number}",
                "value": row_number * 17,
                "source_text": "Reported amount from the original filing. " * 24,
            })

    compact = compact_record_table(records)
    assert isinstance(compact, dict) and compact.get("record_table") == "columns-data-v1"
    assert len(compact["groups"]) == 2
    expanded = expand_record_tables(compact)
    assert _json(expanded) == _json(records)
    assert [(row["document_id"], row["fiscal_period"], row["period_end"], row["unit"])
            for row in expanded[::8]] == [
        ("doc-0", "FY2024", "2024-12-31", "USD millions"),
        ("doc-1", "FY2025", "2025-12-31", "USD thousands"),
    ]


def test_expired_checkpoint_deadline_returns_cached_prefix_and_leaves_gap_resumable(tmp_path):
    checkpoint_store = WindowCheckpointStore(tmp_path / "checkpoints")
    key = CheckpointKey("pdf-digest", "parser-v1", "rules-v1")
    windows = (PdfWindow(0, (1, 2)), PdfWindow(1, (3, 4)), PdfWindow(2, (5,)))
    for window in windows[:2]:
        checkpoint_store.save(WindowCheckpoint(key, window, (), (), {"last_page": window.pages[-1]}))

    worker_calls = []

    def worker(window, context):
        worker_calls.append((window.index, dict(context)))
        return (), (), {"last_page": window.pages[-1], "resumed_from": context.get("last_page")}

    progress = []
    cached_prefix = run_checkpointed_windows(
        checkpoint_store,
        key,
        windows,
        worker,
        max_retries=0,
        deadline=time.monotonic() - 1,
        progress=lambda current, total, status: progress.append((current, total, status)),
    )
    assert [item.window.index for item in cached_prefix] == [0, 1]
    assert worker_calls == []
    assert (2, 3, "budget_exhausted_resumable") in progress
    assert checkpoint_store.load(key, 2, expected_pages=windows[2].pages) is None

    resumed = run_checkpointed_windows(checkpoint_store, key, windows, worker, max_retries=0)
    assert [item.window.index for item in resumed] == [0, 1, 2]
    assert worker_calls == [(2, {"last_page": 4})]
    assert resumed[2].context["resumed_from"] == 4


def _replace_plan(journal, request_plan, envelope=None):
    with journal.store.connect() as db:
        db.execute("DELETE FROM model_run_call_plans WHERE run_id=?", (journal.run_id,))
        prepare_plan(db, journal.run_id, ["business"], request_plan=request_plan, envelope=envelope)
        return plan_status(db, journal.run_id)


def _request_context(request_id, attempt_id, purpose="generation"):
    return {"request_id": request_id, "attempt_id": attempt_id,
            "stage_id": "business", "generation": 1, "purpose": purpose}


def test_large_default_workload_is_adaptive_and_not_rejected_as_over_standard(tmp_path):
    journal = setup(tmp_path)
    plan = _replace_plan(journal, [{
        "stage_id": "business", "purpose": "generation",
        "input_estimate": 800_000, "output_limit": 110_000,
    }])
    assert plan["schema"] == "research-call-plan-v4-adaptive"
    assert plan["policy"] == "adaptive"
    assert plan["workload_fits"] is True
    assert plan["max_calls"] is None
    assert plan["max_input_estimate"] is None
    assert plan["max_output_tokens"] is None
    assert plan["initial_forecast"]["max_calls"] == 3
    assert plan["initial_forecast"]["max_input_estimate"] == 1_600_000
    assert plan["initial_forecast"]["max_output_tokens"] == 114_096

    context = _request_context("proof-312:over-envelope", "attempt-over-envelope")
    with journal.store.connect() as db:
        reserve(db, journal.run_id, context, purpose="generation",
                input_estimate=800_000, output_limit=110_000)
        assert db.execute("SELECT COUNT(*) FROM model_call_reservations WHERE run_id=?",
                          (journal.run_id,)).fetchone()[0] == 1


def test_external_vision_reservation_survives_plan_compilation_without_resetting_envelope(tmp_path):
    journal = setup(tmp_path)
    envelope = {
        "mode": "deep", "max_input_tokens": 2_000_000, "max_output_tokens": 150_000,
        "max_total_tokens": 2_100_000, "max_calls": 32, "warning_seconds": 900,
        "max_automatic_seconds": 2_400,
    }
    vision = {
        "request_id": "vision:bridge-test", "attempt_id": "vision-attempt-1",
        "stage_id": "financial-vision", "generation": 1,
    }
    with journal.store.connect() as db:
        # The fixture's normal journal preparation creates the text plan;
        # remove it to model the real early-vision-before-compilation route.
        db.execute("DELETE FROM model_run_call_plans WHERE run_id=?", (journal.run_id,))
        prepare_external_plan(db, journal.run_id, envelope)
        reserve(db, journal.run_id, vision, purpose="vision_recognition",
                input_estimate=1_000, output_limit=2_000, trigger="failed_page_recognition")
        before_compile = plan_status(db, journal.run_id)
        started_at = before_compile["started_at"]

        prepare_plan(db, journal.run_id, ["business"], request_plan=[{
            "stage_id": "business", "purpose": "generation",
            "input_estimate": 10_000, "output_limit": 5_000,
        }])
        after_compile = plan_status(db, journal.run_id)

    assert after_compile["started_at"] == started_at
    assert after_compile["user_envelope"] == before_compile["user_envelope"]
    assert after_compile["user_envelope"]["max_calls"] == 32
    assert after_compile["user_envelope"]["max_input_tokens"] == 2_000_000
    assert after_compile["user_envelope"]["max_output_tokens"] == 150_000
    assert after_compile["user_envelope"]["max_total_tokens"] == 2_100_000
    assert after_compile["consumption"] == {
        "calls": 1, "input_reserved_or_actual": 1_000,
        "output_reserved_or_actual": 2_000, "unknown_usage_calls": 1,
    }
    assert after_compile["workload_bounds"]["max_input_estimate"] == 21_000
    assert after_compile["workload_bounds"]["max_output_tokens"] == 11_096


def test_exact_budget_extension_preserves_consumption_and_unknown_reasoning_is_reserved(tmp_path):
    journal = setup(tmp_path)
    _replace_plan(journal, [{
        "stage_id": "business", "purpose": "generation",
        "input_estimate": 10_000, "output_limit": 5_000,
    }], envelope={"policy": "adaptive", "user_hard_limits": {"max_calls": 1}})
    first = _request_context("proof-312:first", "attempt-first")
    with journal.store.connect() as db:
        reserve(db, journal.run_id, first, purpose="generation",
                input_estimate=10_000, output_limit=5_000)
        settle(db, first, {"input_tokens": 8_000, "output_tokens": 1_200,
                           "reasoning_tokens": 300}, "response_received",
               reasoning_in_output=None)
        before = plan_status(db, journal.run_id)
        assert before["consumption"]["calls"] == 1
        assert db.execute("SELECT output_actual FROM model_call_reservations WHERE request_id=?",
                          (first["request_id"],)).fetchone()[0] == 1_500

        gap_context = _request_context("proof-312:verification", "attempt-verification")
        with pytest.raises(ProviderError) as raised:
            reserve(db, journal.run_id, gap_context, purpose="semantic_verification",
                    input_estimate=2_000, output_limit=1_000)
        assert raised.value.code == "MODEL_RUN_CALL_BUDGET_EXHAUSTED"
        record_gap(db, journal.run_id, gap_context, "semantic_verification", 2_000, 1_000,
                   raised.value.code)
        db.commit()
        pending = plan_status(db, journal.run_id)
        gap_ids = [gap["gap_id"] for gap in pending["pending_budget_gaps"]]
        assert len(gap_ids) == 1

        after = extend_plan(db, journal.run_id, pending["plan_digest"], gap_ids)
        assert after["consumption"] == before["consumption"]
        assert after["consumption"]["input_reserved_or_actual"] == 8_000
        assert after["consumption"]["output_reserved_or_actual"] == 1_500
        assert after["remaining"]["calls"] == 1
        assert after["verification_batches"] == 1
        assert after["pending_budget_gaps"] == []
        db.commit()

        reserve(db, journal.run_id, gap_context, purpose="semantic_verification",
                input_estimate=2_000, output_limit=1_000)
        resumed = plan_status(db, journal.run_id)
        assert resumed["consumption"]["calls"] == 2
        assert resumed["consumption"]["input_reserved_or_actual"] == 10_000
        assert resumed["consumption"]["output_reserved_or_actual"] == 2_500
        db.commit()

        with pytest.raises(ValueError, match="MODEL_BUDGET_PLAN_CHANGED"):
            extend_plan(db, journal.run_id, pending["plan_digest"], gap_ids)


def test_research_diagnostics_preserves_unknown_cost_cache_and_never_echoes_inputs(tmp_path):
    journal = setup(tmp_path)
    quote = {
        "input_per_million": 0.2,
        "output_per_million": 0.8,
        "currency": "USD",
        "source": "QUOTE_PRIVATE_SENTINEL",
        "route_identity": "ROUTE_PRIVATE_SENTINEL",
    }
    _replace_plan(journal, [{
        "stage_id": "business", "purpose": "generation",
        "input_estimate": 10_000, "output_limit": 5_000,
    }], envelope={
        "mode": "standard", "max_input_tokens": 1_500_000, "max_output_tokens": 100_000,
        "max_total_tokens": 1_600_000, "max_calls": 16, "warning_seconds": 900,
        "max_automatic_seconds": 1_200, "max_cost": 99.0, "price_quote": quote,
    })
    raw = {
        "schema": "model-wire-diagnostics-v1",
        "wire_state": "response_received",
        "usage": {"input_tokens": 12, "output_tokens": 4, "reasoning_tokens": 3,
                  "cache_read_input_tokens": None, "cache_write_input_tokens": None},
        "system_prompt": "PROMPT_PRIVATE_SENTINEL",
        "user_prompt": "SOURCE_QUOTE_PRIVATE_SENTINEL",
        "api_key": "KEY_PRIVATE_SENTINEL",
        "arbitrary_quote": "ARBITRARY_QUOTE_PRIVATE_SENTINEL",
        "price_quote": quote,
    }
    with journal.store.connect() as db:
        db.execute("INSERT INTO model_wire_diagnostics VALUES(?,?,?,?,?,?,?)",
                   ("proof-312:diagnostic", "attempt-diagnostic", journal.run_id,
                    "business", "generation", json.dumps(raw), 100.0))

    first = journal.store.research_diagnostics(journal.run_id)
    second = journal.store.research_diagnostics(journal.run_id)
    assert first["schema"] == "openthesis.research-diagnostics.v1"
    assert first["physical_requests"] == second["physical_requests"] == 1
    assert first["attempt_count"] == second["attempt_count"] == 1
    assert first["totals"]["cache_read_input_tokens"]["value"] is None
    assert first["totals"]["cache_read_input_tokens"]["unknown_attempts"] == 1
    assert first["cache_hit_rate"] is None
    assert first["actual_cost"]["amount"] is None
    assert first["actual_cost"]["currency"] is None
    assert first["actual_cost"]["status"] == "unknown_without_complete_route_bound_tariffs_and_receipts"
    assert "price_quote" not in first["budget"]["user_envelope"]
    exported = _json(first)
    for sentinel in (
        "QUOTE_PRIVATE_SENTINEL", "ROUTE_PRIVATE_SENTINEL", "PROMPT_PRIVATE_SENTINEL",
        "SOURCE_QUOTE_PRIVATE_SENTINEL", "KEY_PRIVATE_SENTINEL", "ARBITRARY_QUOTE_PRIVATE_SENTINEL",
    ):
        assert sentinel not in exported
