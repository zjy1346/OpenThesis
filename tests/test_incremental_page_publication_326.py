"""Incremental publication is tied to real compiler and source observations."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openthesis.application.financials import OfficialFinancialRequest
from openthesis.financial_admission import ReadinessLevel
from openthesis.financial_contracts import FinancialSourceFailure
from openthesis.financial_xbrl_semantics import INLINE_PARSER_VERSION
from openthesis.financial_pipeline import _incremental_page_accessions, _parser_execution_observations
from openthesis.sec_client import SecClient, SecFinancialSourceAdapter
from test_official_financial_preparation import setup_preparer


CORE_FACTS = {
    "revenue", "parent_net_income", "operating_cash_flow",
    "assets", "liabilities", "equity",
}


def test_formal_repair_keeps_incomplete_page_case_open_after_incremental_publication(tmp_path):
    from test_financial_repair_use_case import setup_repair

    filing, _adapter, preparer, storage, _refresher, repairer, request, _original = setup_repair(tmp_path)
    _install_partial_page_failure(preparer)
    result = repairer.repair(replace(request, force=True))
    assert result.status == "partial" and len(result.accepted) == 6
    assert result.updated_artifacts
    cases = storage.get_financial_recovery_cases(request.run_id)
    assert any(case["accession_number"] == filing.accession_number
               and case["error_code"] == "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"
               and case["status"] == "open" for case in cases)


def test_formal_repair_full_retry_closes_partial_page_case_without_losing_facts(tmp_path):
    from test_financial_repair_use_case import setup_repair

    filing, _adapter, preparer, storage, _refresher, repairer, request, _original = setup_repair(tmp_path)
    engine = preparer.pipeline.engine
    collect_complete = engine.collect_candidate_batches
    prepare_complete = preparer.pipeline.prepare
    _install_partial_page_failure(preparer)

    partial = repairer.repair(replace(request, force=True))
    assert partial.status == "partial" and len(partial.accepted) == 6
    partial_values = _concept_values(_active_rows(storage, request.company), filing.accession_number)
    assert len(partial_values) == 6
    page_case = next(case for case in storage.get_financial_recovery_cases(request.run_id)
                     if case["accession_number"] == filing.accession_number)
    assert page_case["status"] == "open"

    engine.collect_candidate_batches = collect_complete
    preparer.pipeline.prepare = prepare_complete
    complete = repairer.repair(replace(request, force=True))

    assert complete.status == "succeeded"
    assert len(set(complete.accepted)) == 6
    assert page_case["status"] == "open"  # The earlier result is an immutable snapshot.
    final_case = next(case for case in storage.get_financial_recovery_cases(request.run_id)
                      if case["accession_number"] == filing.accession_number)
    assert final_case["status"] == "resolved"
    assert _concept_values(_active_rows(storage, request.company), filing.accession_number) >= partial_values


def test_formal_complete_repair_resolves_an_existing_runtime_case(tmp_path):
    from test_financial_repair_use_case import setup_repair

    filing, _adapter, _preparer, storage, _refresher, repairer, request, _original = setup_repair(tmp_path)
    storage.save_financial_recovery_case(
        run_id=request.run_id,
        company_cik=request.company.cik,
        accession_number=filing.accession_number,
        document_hash=filing.content_hash,
        stage="filing-parse",
        error_code="FILING_LOCAL_PARSE_FAILED",
        next_action="retry_saved_candidate_validation_or_enable_authorized_cloud",
    )

    result = repairer.repair(replace(request, force=True))

    assert result.status == "succeeded"
    case = next(case for case in storage.get_financial_recovery_cases(request.run_id)
                if case["accession_number"] == filing.accession_number)
    assert case["status"] == "resolved"
    assert case["error_code"] == ""
    assert case["next_action"] == "none"


def _replace_original(company, filing, adapter, raw):
    path = Path(filing.local_path)
    path.write_bytes(raw)
    updated = replace(filing, local_path=str(path), content_hash=hashlib.sha256(raw).hexdigest())
    adapter.filing = updated
    return updated


def _install_partial_page_failure(
    preparer, *, drop_inline_concepts=(), observation=True, observation_overrides=None,
    include_typed_failure=True, diagnostic_codes=("pdf_window_incomplete",),
    failure_code="PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION",
):
    """Decorate the actual HTML collection, leaving compiler/admission real."""
    engine = preparer.pipeline.engine
    collect = engine.collect_candidate_batches
    calls = []
    prepared_views = []
    pipeline_prepare = preparer.pipeline.prepare

    def capture_prepared_view(snapshot, policy):
        view = pipeline_prepare(snapshot, policy)
        prepared_views.append(view)
        return view

    preparer.pipeline.prepare = capture_prepared_view

    def collect_with_partial_page(company, filings, **kwargs):
        collected = collect(company, filings, **kwargs)
        batches = {
            document_id: tuple(
                replace(batch, candidates=tuple(
                    candidate for candidate in batch.candidates
                    if not (candidate.extractor == INLINE_PARSER_VERSION
                            and candidate.fact.concept in drop_inline_concepts)
                ))
                for batch in document_batches
            )
            for document_id, document_batches in collected.batches_by_document.items()
        }
        failures = list(collected.source_failures)
        diagnostics = list(collected.diagnostics)
        for filing in filings:
            calls.append(filing.accession_number)
            if observation:
                raw_observation = {
                    "source_hash": filing.content_hash,
                    "period_end": filing.period_end,
                    "fiscal_period": filing.fiscal_period,
                    "parse_strategy": "positioned-page-checkpoint",
                    "backend": "pdfplumber",
                    "candidate_page_count": 2,
                    "completed_page_count": 1,
                }
                raw_observation.update(observation_overrides or {})
                engine._document_execution[filing.document_id] = raw_observation
            if include_typed_failure:
                failures.append(FinancialSourceFailure(
                    filing.document_id, filing.accession_number,
                    "parse", failure_code, required=True,
                ))
            diagnostics.extend(f"{filing.document_id}:{code}" for code in diagnostic_codes)
        return replace(collected, batches_by_document=batches,
                       diagnostics=tuple(dict.fromkeys(diagnostics)),
                       source_failures=tuple(failures))

    engine.collect_candidate_batches = collect_with_partial_page
    return calls, prepared_views


def _active_rows(storage, company):
    return tuple(storage.get_financial_research_snapshot(company.cik)["facts"])


def _concept_values(rows, accession):
    return {(row["concept"], float(row["value"])) for row in rows
            if row["accession_number"] == accession}


def _companyfacts_revenue_source(filing, value):
    tag = "RevenueFromContractWithCustomerExcludingAssessedTax"
    payload = {"facts": {"us-gaap": {tag: {"units": {"USD": [{
        "val": value, "fy": 2026, "fp": "FY", "form": "10-K",
        "accn": filing.accession_number, "filed": filing.filed_at[:10],
        "end": filing.period_end, "start": "2025-01-27",
    }]}}}}}
    client = object.__new__(SecClient)
    client._get_json = lambda *_args: payload
    return SecFinancialSourceAdapter(client)


def test_bound_partial_page_can_publish_six_compiler_facts_and_retains_page_failure(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    _calls, prepared_views = _install_partial_page_failure(preparer)

    result = preparer.prepare(OfficialFinancialRequest(
        company, adapter, annual_history_years=1,
    ))

    assert {fact.concept for fact in prepared_views[0].facts} == CORE_FACTS
    assert {fact.concept for fact in result.view.facts} == CORE_FACTS
    assert result.view.incremental_accessions == (filing.accession_number,)
    assert result.view.publishable_accessions == (filing.accession_number,)
    assert any(failure.accession_number == filing.accession_number
               and failure.stage == "parse" and failure.code == "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"
               and failure.required
               for failure in prepared_views[0].canonical_dataset.source_failures)
    assert f"{filing.accession_number}:parse:PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION" in result.errors
    assert {row["concept"] for row in _active_rows(storage, company)} == CORE_FACTS


@pytest.mark.parametrize("failure_code", [
    "PDF_WORKER_START_FAILED", "PDF_BACKEND_IMPORT_FAILED",
])
def test_partial_pages_keep_admitted_facts_and_original_runtime_failure(tmp_path, failure_code):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    _calls, prepared_views = _install_partial_page_failure(
        preparer, diagnostic_codes=(), failure_code=failure_code,
    )

    result = preparer.prepare(OfficialFinancialRequest(
        company, adapter, annual_history_years=1,
    ))

    assert {fact.concept for fact in prepared_views[0].facts} == CORE_FACTS
    assert result.view.incremental_accessions == (filing.accession_number,)
    failure = next(item for item in result.view.canonical_dataset.source_failures
                   if item.document_id == filing.document_id
                   and item.accession_number == filing.accession_number)
    assert (failure.stage, failure.code, failure.required) == (
        "parse", failure_code, True,
    )
    assert f"{filing.accession_number}:parse:{failure_code}" in result.errors
    assert result.view.readiness.level is ReadinessLevel.PARTIAL
    assert {row["concept"] for row in _active_rows(storage, company)} == CORE_FACTS


def test_incremental_recompile_does_not_shrink_richer_same_source_facts(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    raw = Path(filing.local_path).read_bytes()
    raw = raw.replace(b"</html>",
        b'<ix:nonFraction name="g:GrossProfit" contextRef="c" unitRef="u" '
        b'format="ixt:num-dot-decimal">250</ix:nonFraction></html>')
    filing = _replace_original(company, filing, adapter, raw)
    first = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))
    before = _active_rows(storage, company)
    before_values = _concept_values(before, filing.accession_number)
    assert {concept for concept, _value in before_values} == CORE_FACTS | {"gross_profit"}

    _calls, _prepared_views = _install_partial_page_failure(
        preparer, drop_inline_concepts={"gross_profit"})
    second = preparer.prepare(OfficialFinancialRequest(
        company, adapter, annual_history_years=1, force=True,
    ))

    after_values = _concept_values(_active_rows(storage, company), filing.accession_number)
    assert second.view.incremental_accessions == (filing.accession_number,)
    assert before_values <= after_values
    assert ("gross_profit", 250.0) in after_values


def test_partial_new_source_keeps_old_active_generation_and_original_hash(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    first = preparer.prepare(OfficialFinancialRequest(
        company, adapter, annual_history_years=1,
    ))
    assert first.publication is not None and first.publication["state"] == "promoted"
    before = storage.get_financial_research_snapshot(company.cik)
    old_generation = before["generations"][filing.accession_number]
    old_values = _concept_values(before["facts"], filing.accession_number)
    old_hash = filing.content_hash
    with storage.connect() as db:
        old_record = db.execute(
            "SELECT source_hashes_json FROM financial_ingestion_candidates WHERE generation_id=?",
            (old_generation,),
        ).fetchone()
    assert old_record is not None
    assert json.loads(old_record[0])[filing.accession_number] == old_hash

    raw = Path(filing.local_path).read_bytes().replace(
        b"</html>",
        b'<ix:nonFraction name="g:GrossProfit" contextRef="c" unitRef="u" '
        b'format="ixt:num-dot-decimal">250</ix:nonFraction></html>',
    )
    filing = _replace_original(company, filing, adapter, raw)
    _calls, _prepared_views = _install_partial_page_failure(
        preparer, drop_inline_concepts={"gross_profit"},
    )
    second = preparer.prepare(OfficialFinancialRequest(
        company, adapter, annual_history_years=1, force=True,
    ))

    after = storage.get_financial_research_snapshot(company.cik)
    assert filing.content_hash != old_hash
    assert second.view.incremental_accessions == (filing.accession_number,)
    assert filing.accession_number not in second.view.publishable_accessions
    assert after["generations"][filing.accession_number] == old_generation
    assert _concept_values(after["facts"], filing.accession_number) == old_values
    assert all(row["generation_id"] == old_generation for row in after["facts"]
               if row["accession_number"] == filing.accession_number)
    with storage.connect() as db:
        active_record = db.execute(
            "SELECT source_hashes_json FROM financial_ingestion_candidates WHERE generation_id=?",
            (old_generation,),
        ).fetchone()
        candidate_record = db.execute(
            "SELECT source_hashes_json, candidate_json FROM financial_ingestion_candidates WHERE generation_id=?",
            (second.publication["generation_id"],),
        ).fetchone()
    assert active_record is not None and candidate_record is not None
    assert json.loads(active_record[0])[filing.accession_number] == old_hash
    assert json.loads(candidate_record[0])[filing.accession_number] == filing.content_hash
    candidate_payload = json.loads(candidate_record[1])
    assert candidate_payload["accepted_facts"]
    assert all(fact["generation_id"] == second.publication["generation_id"]
               for fact in candidate_payload["accepted_facts"])


def test_new_conflicting_value_cannot_replace_old_same_source_generation(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    first = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))
    before = storage.get_financial_research_snapshot(company.cik)
    old_generation = before["generations"][filing.accession_number]
    old_revenue = next(row["value"] for row in before["facts"]
                       if row["accession_number"] == filing.accession_number
                       and row["concept"] == "revenue")
    assert old_revenue == 1000

    _calls, prepared_views = _install_partial_page_failure(
        preparer, drop_inline_concepts={"revenue"})
    companyfacts = _companyfacts_revenue_source(filing, 999)
    second = preparer.prepare(OfficialFinancialRequest(
        company, adapter, candidate_sources=(companyfacts,), annual_history_years=1, force=True,
    ))

    compiled_values = {(fact.concept, float(fact.value)) for fact in prepared_views[0].facts}
    expected_values = {
        ("revenue", 999.0), ("parent_net_income", 100.0),
        ("operating_cash_flow", 110.0), ("assets", 1000.0),
        ("liabilities", 600.0), ("equity", 400.0),
    }
    assert compiled_values >= expected_values, (
        compiled_values, prepared_views[0].canonical_dataset.diagnostics,
        [(item.identity, item.status, item.issues,
          tuple((fact.concept, fact.value) for fact in item.accepted),
          tuple((fact.concept, fact.value) for fact in item.quarantined))
         for item in prepared_views[0].canonical_dataset.validations],
    )
    assert {fact.concept for fact in prepared_views[0].facts} >= CORE_FACTS
    assert second.view.incremental_accessions == (filing.accession_number,)
    after = storage.get_financial_research_snapshot(company.cik)
    assert after["generations"][filing.accession_number] == old_generation
    assert next(row["value"] for row in after["facts"]
                if row["accession_number"] == filing.accession_number
                and row["concept"] == "revenue") == old_revenue


@pytest.mark.parametrize("damage", ["missing_unit", "single_fact_missing_unit_ref"])
def test_missing_units_or_single_fact_unit_dependency_control_incremental_publication(tmp_path, damage):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    raw = Path(filing.local_path).read_bytes()
    if damage == "missing_unit":
        raw = raw.replace(b'<x:unit id="u"><x:measure>iso:USD</x:measure></x:unit>', b"")
    else:
        raw = raw.replace(
            b'<ix:nonFraction name="g:StockholdersEquity" contextRef="i" unitRef="u">400</ix:nonFraction>',
            b'<ix:nonFraction name="g:StockholdersEquity" contextRef="i" unitRef="missing">400</ix:nonFraction>',
        )
    filing = _replace_original(company, filing, adapter, raw)
    _calls, prepared_views = _install_partial_page_failure(preparer)

    result = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))

    compiled_concepts = {fact.concept for fact in prepared_views[0].facts} & CORE_FACTS
    if damage == "missing_unit":
        # The shared unit declaration is a dependency of every numeric cell.
        assert compiled_concepts == set()
        assert result.view.incremental_accessions == ()
        assert result.view.publishable_accessions == ()
        assert not ({row["concept"] for row in _active_rows(storage, company)} & CORE_FACTS)
    else:
        # A malformed unitRef invalidates this cell, not unrelated facts from
        # the same accession. Admission must not require a complete profile.
        independent_concepts = CORE_FACTS - {"equity"}
        assert compiled_concepts == independent_concepts
        assert result.view.incremental_accessions == (filing.accession_number,)
        assert result.view.publishable_accessions == (filing.accession_number,)
        assert {fact.concept for fact in result.view.facts} == independent_concepts
        assert {row["concept"] for row in _active_rows(storage, company)} == independent_concepts


@pytest.mark.parametrize("override", [
    {"source_hash": "0" * 64},
    {"period_end": "1900-01-01"},
])
def test_partial_page_observation_with_wrong_source_or_period_binding_is_not_publishable(tmp_path, override):
    company, filing, adapter, preparer, _storage = setup_preparer(tmp_path)
    _calls, prepared_views = _install_partial_page_failure(
        preparer, observation_overrides=override)

    result = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))

    assert {fact.concept for fact in prepared_views[0].facts} == CORE_FACTS
    assert result.view.incremental_accessions == ()
    assert result.view.publishable_accessions == ()


def test_page_failure_without_a_bound_partial_checkpoint_keeps_original_publication_fence(tmp_path):
    company, filing, adapter, preparer, _storage = setup_preparer(tmp_path)
    _calls, prepared_views = _install_partial_page_failure(preparer, observation=False)

    result = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))

    assert {fact.concept for fact in prepared_views[0].facts} == CORE_FACTS
    assert result.view.incremental_accessions == ()
    assert result.view.publishable_accessions == ()


def test_diagnostic_only_partial_publication_keeps_failure_and_partial_readiness(tmp_path):
    company, filing, adapter, preparer, _storage = setup_preparer(tmp_path)
    _calls, prepared_views = _install_partial_page_failure(
        preparer, include_typed_failure=False,
    )
    admit = preparer.pipeline.admission.admit
    admission_calls = 0

    def first_view_is_full(*args, **kwargs):
        nonlocal admission_calls
        view = admit(*args, **kwargs)
        admission_calls += 1
        if admission_calls == 1:
            view = replace(view, readiness=replace(
                view.readiness, level=ReadinessLevel.FULL, reasons=(), actions=(),
            ))
        return view

    preparer.pipeline.admission.admit = first_view_is_full

    result = preparer.prepare(OfficialFinancialRequest(
        company, adapter, annual_history_years=1,
    ))

    assert prepared_views[0].readiness.level is ReadinessLevel.PARTIAL
    assert "source_page_plan_incomplete" in prepared_views[0].readiness.reasons
    assert "source_page_plan_incomplete" in prepared_views[0].readiness.actions
    failure = next(item for item in result.view.canonical_dataset.source_failures
                   if item.document_id == filing.document_id
                   and item.accession_number == filing.accession_number)
    assert (failure.stage, failure.code, failure.required) == (
        "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION", True,
    )
    assert result.view.incremental_accessions == (filing.accession_number,)
    assert result.view.readiness.level is ReadinessLevel.PARTIAL
    assert "source_page_plan_incomplete" in result.view.readiness.reasons
    assert "source_page_plan_incomplete" in result.view.readiness.actions
    assert f"{filing.accession_number}:parse:PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION" in result.errors
    assert any(item.required and item.code == "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"
               for item in prepared_views[0].canonical_dataset.source_failures)


def test_repair_retries_a_source_whose_page_plan_is_still_incomplete(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    calls, prepared_views = _install_partial_page_failure(preparer)
    first = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))
    assert {fact.concept for fact in prepared_views[0].facts} == CORE_FACTS
    assert first.view.incremental_accessions == (filing.accession_number,)
    assert {row["concept"] for row in _active_rows(storage, company)} == CORE_FACTS

    calls.clear()
    prepared_views.clear()
    second = preparer.prepare(OfficialFinancialRequest(
        company, adapter, annual_history_years=1, repair=True,
    ))

    assert calls == [filing.accession_number]
    assert second.target_accessions == (filing.accession_number,)
    assert second.reused_accessions == ()


@pytest.mark.parametrize(("failure", "diagnostics", "completed", "candidate", "accepted", "obs_update", "expected"), [
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"), (), 1, 2, True, {}, ("acc",)),
    (None, ("acc:pdf_window_incomplete",), 1, 2, True, {}, ("acc",)),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"), ("pdf_window_incomplete",), 1, 2, True, {}, ()),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"), ("pdf_window_failed:WorkerTimeout",), 1, 2, True, {}, ()),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"), ("pdf_window_exhausted",), 1, 2, True, {}, ()),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"), ("pdf_document_budget_exhausted_resumable",), 1, 2, True, {}, ()),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"), ("unrelated nonfatal diagnostic",), 1, 2, True, {}, ("acc",)),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WORKER_START_FAILED"), (), 1, 2, True, {}, ("acc",)),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_BACKEND_IMPORT_FAILED"), (), 1, 2, True, {}, ("acc",)),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WORKER_START_FAILED"), (), 0, 2, True, {}, ()),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_BACKEND_IMPORT_FAILED"), (), 0, 2, True, {}, ()),
    (FinancialSourceFailure("doc", "acc", "parse", "OTHER_PARSE_ERROR"), ("doc:pdf_window_incomplete",), 1, 2, True, {}, ()),
    (FinancialSourceFailure("unknown", "", "parse", "OTHER_PARSE_ERROR"), ("doc:pdf_window_incomplete",), 1, 2, True, {}, ()),
    (FinancialSourceFailure("other-doc", "other-acc", "parse", "OTHER_PARSE_ERROR"), ("doc:pdf_window_incomplete",), 1, 2, True, {}, ("acc",)),
    (FinancialSourceFailure("doc", "acc", "worker", "PDF_WORKER_TIMEOUT"), ("doc:pdf_parse_cancelled",), 1, 2, True, {}, ()),
    (None, ("pdf_file_missing",), 1, 2, True, {}, ()),
    (FinancialSourceFailure("doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"), (), 2, 2, True, {}, ()),
    (None, ("acc:pdf_window_incomplete",), True, 2, True, {}, ()),
    (None, ("acc:pdf_window_incomplete",), 1, 2, False, {}, ()),
    (None, ("acc:pdf_window_incomplete",), 1, 2, True, {"source_hash": "0" * 64}, ()),
    (None, ("acc:pdf_window_incomplete",), 1, 2, True, {"period_end": "1900-01-01"}, ()),
    (None, ("acc:pdf_window_incomplete",), 1, 2, True, {"backend": "other"}, ()),
])
def test_incremental_page_gate_requires_bound_compiler_fact_and_only_local_partial_failure(
    failure, diagnostics, completed, candidate, accepted, obs_update, expected,
):
    filing = SimpleNamespace(
        document_id="doc", accession_number="acc", content_hash="a" * 64,
        period_end="2026-01-25", fiscal_period="FY",
    )
    other = SimpleNamespace(
        document_id="other-doc", accession_number="other-acc", content_hash="b" * 64,
        period_end="2025-01-25", fiscal_period="FY",
    )
    observation = {
        "accession_number": "acc", "source_hash": filing.content_hash,
        "period_end": filing.period_end, "fiscal_period": filing.fiscal_period,
        "parse_strategy": "positioned-page-checkpoint", "backend": "pdfplumber",
        "completed_page_count": completed, "candidate_page_count": candidate,
    }
    observation.update(obs_update)
    fact = SimpleNamespace(
        fact_id="fact-1", accession_number="acc", end_date="2026-01-25",
        fiscal_period="FY",
    )
    dataset = SimpleNamespace(
        coverage={"fact_validation_records": {"fact-1": {"accepted": accepted}}},
        source_failures=(failure,) if failure is not None else (),
        diagnostics=diagnostics,
    )
    view = SimpleNamespace(
        facts=(fact,),
        profile=SimpleNamespace(parsing_observations=(observation,)),
    )

    assert _incremental_page_accessions(
        SimpleNamespace(filings=(filing, other)), dataset, view,
    ) == expected


def test_incremental_page_gate_accepts_admitted_comparison_period_or_as_of_fact():
    filing = SimpleNamespace(
        document_id="doc", accession_number="acc", content_hash="a" * 64,
        period_end="2026-01-25", fiscal_period="FY",
    )
    observation = {
        "accession_number": filing.accession_number,
        "source_hash": filing.content_hash,
        "period_end": filing.period_end,
        "fiscal_period": filing.fiscal_period,
        "parse_strategy": "positioned-page-checkpoint",
        "backend": "pdfplumber",
        "completed_page_count": 1,
        "candidate_page_count": 2,
    }
    comparison_fact = SimpleNamespace(
        fact_id="comparison-fact", accession_number=filing.accession_number,
        end_date="2025-01-25", fiscal_period="AS_OF",
    )
    dataset = SimpleNamespace(
        coverage={"fact_validation_records": {"comparison-fact": {"accepted": True}}},
        source_failures=(FinancialSourceFailure(
            "doc", "acc", "parse", "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION",
        ),),
        diagnostics=(),
    )
    view = SimpleNamespace(
        facts=(comparison_fact,),
        profile=SimpleNamespace(parsing_observations=(observation,)),
    )

    assert _incremental_page_accessions(
        SimpleNamespace(filings=(filing,)), dataset, view,
    ) == (filing.accession_number,)


@pytest.mark.parametrize("bad_identity", [
    {"period_end": "1900-01-01"},
    {"fiscal_period": "Q1"},
    {"source_hash": "0" * 64},
])
def test_parser_execution_observation_never_relabels_explicit_wrong_identity(bad_identity):
    filing = SimpleNamespace(
        document_id="doc", accession_number="acc", content_hash="a" * 64,
        period_end="2026-01-25", fiscal_period="FY",
    )
    raw = {
        "source_hash": filing.content_hash,
        "parse_strategy": "positioned-page-checkpoint", "backend": "pdfplumber",
        "candidate_page_count": 2, "completed_page_count": 1,
        **bad_identity,
    }
    prior_bound = {
        "accession_number": filing.accession_number,
        "period_end": filing.period_end, "fiscal_period": filing.fiscal_period,
        "source_hash": filing.content_hash, "parse_strategy": "positioned-page-checkpoint",
        "backend": "pdfplumber", "candidate_page_count": 2, "completed_page_count": 1,
    }

    observations = _parser_execution_observations(
        SimpleNamespace(filings=(filing,), parsing_observations=(prior_bound,)),
        SimpleNamespace(_document_execution={"doc": raw}),
    )

    assert observations == ()


def test_parser_execution_observation_binds_legacy_missing_period_to_filing():
    filing = SimpleNamespace(
        document_id="doc", accession_number="acc", content_hash="a" * 64,
        period_end="2026-01-25", fiscal_period="FY",
    )
    raw = {
        "source_hash": filing.content_hash,
        "parse_strategy": "positioned-page-checkpoint",
        "candidate_page_count": 2, "completed_page_count": 1,
    }

    observations = _parser_execution_observations(
        SimpleNamespace(filings=(filing,), parsing_observations=()),
        SimpleNamespace(_document_execution={"doc": raw}),
    )

    assert observations == ({
        "accession_number": "acc", "period_end": "2026-01-25",
        "fiscal_period": "FY", "source_hash": "a" * 64,
        "stage_timings": {}, "candidate_page_count": 2,
        "completed_page_count": 1, "parse_strategy": "positioned-page-checkpoint",
        "backend": "pdfplumber",
    },)
