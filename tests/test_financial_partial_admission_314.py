"""Local fact admission must not inherit an optional cohort's completeness."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from openthesis.financial_admission import FinancialAdmission
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import CandidateBatch, CompilationPolicy
from openthesis.financials import calculate_interim_metrics
from openthesis.service import _canonical_retry_snapshot, _canonical_snapshot_digest
from openthesis.storage import Storage
from openthesis.application.research_snapshot import financial_semantics_digest
from test_financial_compiler import _facts, _filing, _subject


def interim_batch():
    filing = replace(_filing("interim-314", "2026-06-30", "Interim report"),
                     fiscal_period="H1", filed_at="2026-08-30")
    current = tuple(replace(item, fact=replace(item.fact, fiscal_period="H1",
        start_date=None if item.fact.statement == "balance_sheet" else "2026-01-01",
        period_start=None if item.fact.statement == "balance_sheet" else "2026-01-01"))
        for item in _facts(filing, net_income=20_000_000_000))
    prior = tuple(replace(item, fact=replace(item.fact,
        fact_id=item.fact.fact_id + ":prior", fiscal_year=2025,
        end_date="2025-06-30", start_date="2025-01-01", period_start="2025-01-01",
        usage_status="comparator", value=item.fact.value / 2))
        for item in current if item.fact.statement != "balance_sheet")
    return filing, current, prior


def test_partial_group_does_not_demote_independently_accepted_cells():
    filing, current, _ = interim_batch()
    dataset = FinancialFactCompiler().compile(_subject(),
        (CandidateBatch(filing, current[:-1]),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", fiscal_period="H1"))
    assert not dataset.allow_ai  # Legacy completeness is intentionally still false.
    assert dataset.research_facts
    assert all(f.validation_status == "VERIFIED" for f in dataset.research_facts)
    view = FinancialAdmission().admit(_subject(), dataset, selected_filings=(filing,))
    assert view.model_ready
    assert view.readiness.level.value == "partial"


def test_optional_roe_conflict_preserves_core_research_and_true_conflict():
    filing, current, prior = interim_batch()
    ratios = tuple(replace(current[0], fact=replace(current[0].fact,
        fact_id=f"roe-{index}", concept="reported_roe", reported_concept="加权平均净资产收益率",
        statement="summary", value=value, unit_provenance="explicit_ratio"))
        for index, value in enumerate((0.02, 0.03)))
    dataset = FinancialFactCompiler().compile(_subject(),
        (CandidateBatch(filing, current + prior + ratios),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", fiscal_period="H1"))
    view = FinancialAdmission().admit(_subject(), dataset, selected_filings=(filing,))
    assert not dataset.allow_ai
    assert view.model_ready
    assert not any(f.concept == "reported_roe" for f in view.facts)
    assert {f.fact_id for f in view.quarantined_facts} == {"roe-0", "roe-1"}
    assert any(item["concept"] == "reported_roe" for item in dataset.conflicts)
    assert view.profile.interim_metrics[0]["revenue_growth"] == pytest.approx(1)


def test_h1_flow_comparison_needs_no_prior_h1_balance_sheet():
    filing, current, prior = interim_batch()
    dataset = FinancialFactCompiler().compile(_subject(),
        (CandidateBatch(filing, current + prior),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", fiscal_period="H1"))
    view = FinancialAdmission().admit(_subject(), dataset, selected_filings=(filing,))
    metric = view.profile.interim_metrics[0]
    assert metric["revenue_growth"] == pytest.approx(1)
    assert metric["operating_cash_flow_growth"] == pytest.approx(1)
    assert "assets" not in {f.concept for f in dataset.comparator_facts}
    assert view.capability_map["interim_comparison"]


def test_independently_filed_partial_h1_is_valid_per_metric_baseline():
    filing, current, prior = interim_batch()
    old_filing = replace(filing, document_id="prior-h1", accession_number="prior-h1",
                         period_end="2025-06-30", filed_at="2025-08-30")
    old = tuple(replace(item, fact=replace(item.fact, accession_number="prior-h1",
                    filed_at=old_filing.filed_at, usage_status="canonical_research"),
                    evidence=tuple(replace(ref, document_id=old_filing.document_id)
                                   for ref in item.evidence)) for item in prior)
    dataset = FinancialFactCompiler().compile(_subject(),
        (CandidateBatch(filing, current), CandidateBatch(old_filing, old)),
        CompilationPolicy(filings=(filing, old_filing), reporting_currency="CNY", fiscal_period="H1"))
    metric = FinancialAdmission().admit(_subject(), dataset).profile.interim_metrics[0]
    assert metric["revenue_growth"] == pytest.approx(1)
    assert metric["comparison_source"] == "independent_historical"


def test_same_h1_label_does_not_make_quarter_only_revenue_comparable():
    _filing_value, current, prior = interim_batch()
    records = [item.fact.to_dict() for item in current + prior]
    for record in records:
        if record["usage_status"] == "comparator" and record["concept"] == "revenue":
            record.update(start_date="2025-04-01", period_start="2025-04-01")
    metric = calculate_interim_metrics(records)[0]
    assert metric["revenue_growth"] is None
    assert metric["operating_cash_flow_growth"] == pytest.approx(1)


@pytest.mark.parametrize("field,value", [("currency", "USD"), ("scope", "parent"),
    ("end_date", "2025-03-31"), ("start_date", "2025-04-01")])
def test_comparison_identity_mismatch_only_disables_its_metric(field, value):
    _filing_value, current, prior = interim_batch()
    records = [item.fact.to_dict() for item in current + prior]
    for record in records:
        if record["usage_status"] == "comparator" and record["concept"] == "revenue":
            record[field] = value
            if field == "scope":
                record["consolidated_scope"] = value
    metric = calculate_interim_metrics(records)[0]
    assert metric["revenue_growth"] is None
    assert metric["operating_cash_flow_growth"] == pytest.approx(1)


def test_prior_year_end_balance_keeps_official_date_and_distinct_binding():
    filing, current, prior = interim_batch()
    balance_prior = tuple(replace(item, fact=replace(item.fact,
        fact_id=item.fact.fact_id + ":year-end", fiscal_year=2025, fiscal_period="FY",
        end_date="2025-12-31", usage_status="comparator"))
        for item in current if item.fact.statement == "balance_sheet")
    dataset = FinancialFactCompiler().compile(_subject(),
        (CandidateBatch(filing, current + prior + balance_prior),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", fiscal_period="H1"))
    view = FinancialAdmission().admit(_subject(), dataset, selected_filings=(filing,))
    bindings = [item for item in view.profile.comparison_bindings if item["concept"] == "assets"]
    assert any(item["relation"] == "previous_year_end" and item["status"] == "available"
               and item["prior_end"] == "2025-12-31" for item in bindings)
    assert not any(item["relation"] == "year_over_year" and item["status"] == "available"
                   for item in bindings)
    assert all(f.end_date == "2025-12-31" for f in dataset.comparator_facts
               if f.statement == "balance_sheet")


def test_source_scoped_conflict_does_not_block_other_admitted_source():
    filing, current, _ = interim_batch()
    dataset = FinancialFactCompiler().compile(_subject(), (CandidateBatch(filing, current),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", fiscal_period="H1"))
    dataset = replace(dataset, diagnostics=("other-source:content_hash_mismatch",))
    view = FinancialAdmission().admit(_subject(), dataset, selected_filings=(filing,))
    assert view.model_ready and view.readiness.can_continue
    assert view.readiness.level.value == "partial"
    blocked = FinancialAdmission().admit(_subject(),
        replace(dataset, diagnostics=("research_identity_conflict",)), selected_filings=(filing,))
    assert not blocked.research_ready


def test_snapshot_restore_uses_admission_not_legacy_complete_profile(tmp_path):
    filing, current, _ = interim_batch()
    dataset = FinancialFactCompiler().compile(_subject(),
        (CandidateBatch(filing, current[:-1]),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", fiscal_period="H1"))
    store = Storage(tmp_path)
    store.save_company(_subject())
    store.save_filings([filing])
    store.replace_financial_ingestion(_subject().cik, [filing.accession_number],
        list(dataset.research_facts), validation_groups=dataset.group_validations,
        evidence=dataset.evidence)
    payload = {"data_snapshot": {"financial_fact_ids_sha256":
        _canonical_snapshot_digest(sorted(f.fact_id for f in dataset.research_facts))}}
    restored = _canonical_retry_snapshot(store, _subject(), payload)
    assert {f.fact_id for f in restored} == {f.fact_id for f in dataset.research_facts}
    payload["data_snapshot"]["financial_semantics_sha256"] = financial_semantics_digest(
        [f.to_dict() for f in restored])
    assert _canonical_retry_snapshot(store, _subject(), payload)
    changed = [f.to_dict() for f in restored]
    changed[0]["value"] += 1
    assert financial_semantics_digest(changed) != payload["data_snapshot"]["financial_semantics_sha256"]
    payload["data_snapshot"]["financial_semantics_sha256"] = financial_semantics_digest(changed)
    with pytest.raises(ValueError, match="FINANCIAL_SNAPSHOT_STALE"):
        _canonical_retry_snapshot(store, _subject(), payload)
    payload["data_snapshot"]["financial_fact_ids_sha256"] = "wrong"
    with pytest.raises(ValueError, match="FINANCIAL_SNAPSHOT_STALE"):
        _canonical_retry_snapshot(store, _subject(), payload)


@pytest.mark.parametrize("can_continue,updated,error,state", [
    (True, ["summary"], "quality:reported_roe", "completed"),
    (False, [], "quality:no_facts", "failed"),
    (True, [], "FILING_REPORT_REFRESH_FAILED", "failed"),
    (True, ["summary"], "FINANCIAL_GENERATION_CONFLICT", "failed"),
])
def test_partial_repair_job_separates_execution_and_financial_completeness(
        tmp_path, monkeypatch, can_continue, updated, error, state):
    from openthesis.service import AppService, _ResearchJob
    service = AppService(tmp_path)
    result = {"status": "partial", "research_can_continue": can_continue,
              "updated_artifacts": updated, "error": error}
    monkeypatch.setattr(service, "retry_financials", lambda *_args, **_kwargs: {"financial_retry": result})
    job = _ResearchJob(job_id="job-314", run_id="run-314", stage="filing-discovery")
    service._run_financial_retry_job(job, "run-314", False)
    assert job.state == state
    assert job.operation_result["status"] == "partial"
    if state == "completed":
        assert job.error_code is None
        assert "可继续研究" in job.message


def test_healthy_core_cannot_short_circuit_pending_local_roe_mapping(tmp_path):
    from openthesis.application.financials import OfficialFinancialPreparer
    from openthesis.financial_ingestion import FinancialIngestionEngine
    from openthesis.financial_pipeline import FinancialPipeline
    from openthesis.financial_contracts import PDF_PARSER_VERSION
    filing, current, _ = interim_batch()
    dataset = FinancialFactCompiler().compile(_subject(), (CandidateBatch(filing, current),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", fiscal_period="H1"))
    view = FinancialAdmission().admit(_subject(), dataset, selected_filings=(filing,))
    cache = tmp_path / "parse-cache"
    engine = FinancialIngestionEngine(cache_dir=cache)
    key = engine._parse_cache_key(filing.local_path, _subject(), filing, filing.content_hash)
    ratio = replace(current[0].fact, concept="reported_roe", parser_version=PDF_PARSER_VERSION)
    engine._store_parse_cache(key, (ratio,), ())
    path = cache / f"{key}.json"
    old_payload = json.loads(path.read_text(encoding="utf-8"))
    old_payload.pop("roe_row_mapping_version")
    path.write_text(json.dumps(old_payload), encoding="utf-8")
    engine = FinancialIngestionEngine(cache_dir=cache)
    preparer = OfficialFinancialPreparer(Storage(tmp_path / "data"), SimpleNamespace(),
        SimpleNamespace(verified_cache=lambda document: document), FinancialPipeline(engine))
    assert preparer._healthy_accessions(_subject(), (filing,), view) == set()
    engine._store_parse_cache(key, (ratio,), ())
    assert preparer._healthy_accessions(_subject(), (filing,), view) == {filing.accession_number}
