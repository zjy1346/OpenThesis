"""3.1.1 production seams: original contribution, immutable start and recovery."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openthesis.financial_contracts import CandidateBatch, FactCandidate, FactGroupValidation, FinancialDataset
from openthesis.financial_pipeline import document_processing_outcome
from openthesis.providers import ModelConfig, RustModelGatewayProvider
from openthesis.research import _supported_research_fields, _lossless_synthesis_value
from openthesis.research_execution.model import provider_context_capability
from openthesis.service import _model_config_from_request
from openthesis.research_materials import canonical_request_materials
from openthesis.research_materials import prompt_materials
from test_financial_pipeline import _filing, _fact


def contribution(role="comparator", *, conflict=False, admitted=True):
    filing = replace(_filing(), content_hash="a" * 64)
    fact = _fact(filing)
    group = FactGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"),
        "READY_WITH_WARNINGS" if conflict else "VERIFIED", ("unit_conflict",) if conflict else (),
        frozenset({"revenue"}), (fact,) if admitted else (), (replace(fact, fact_id="bad"),) if conflict else (), role)
    candidates = (FactCandidate(fact), FactCandidate(replace(fact, fact_id="bad"))) if conflict else (FactCandidate(fact),)
    collection = SimpleNamespace(batches_by_document={filing.document_id: (CandidateBatch(filing, candidates),)}, source_failures=())
    dataset = FinancialDataset((filing,), (fact,) if admitted else (), (), (), (), (group,), {}, (), False)
    return filing, dataset, collection


def test_comparator_counts_actual_admission_without_target_validation():
    filing, dataset, collection = contribution()
    outcome = document_processing_outcome(filing, dataset, collection)
    assert outcome["status"] == "historical"
    assert outcome["roles"] == ["comparator"]
    assert outcome["admitted_count"] == 1
    assert outcome["availability"] == "complete"


def test_conflicted_sibling_retains_usable_cells_and_exact_issue():
    filing, dataset, collection = contribution("target", conflict=True)
    outcome = document_processing_outcome(filing, dataset, collection)
    assert (outcome["status"], outcome["admitted_count"], outcome["quarantined_count"]) == ("partial", 1, 1)
    assert outcome["issue_codes"] == ["unit_conflict"]


def test_same_accession_other_document_hash_does_not_claim_this_originals_cells():
    filing, dataset, collection = contribution()
    altered = replace(filing, content_hash="b" * 64)
    outcome = document_processing_outcome(altered, dataset, collection)
    assert outcome["admitted_count"] == 0
    assert outcome["status"] == "needs-action"


def test_summary_only_older_comparator_does_not_imply_missing_full_statements():
    filing, dataset, collection = contribution("target")
    roe = replace(_fact(filing), fact_id="summary-only", concept="reported_roe", statement="summary", end_date="2020-12-31")
    group = FactGroupValidation((filing.accession_number, roe.end_date, "FY", "consolidated", "CNY"),
        "READY_WITH_WARNINGS", ("income_statement_core_missing", "cash_flow_core_missing", "compiler_incomplete_profile"),
        frozenset({"reported_roe"}), (roe,), (), "comparator")
    dataset = replace(dataset, validations=(*dataset.validations, group))
    batch = collection.batches_by_document[filing.document_id][0]
    collection.batches_by_document[filing.document_id] = (replace(batch, candidates=(*batch.candidates, FactCandidate(roe))),)
    outcome = document_processing_outcome(filing, dataset, collection)
    assert outcome["admitted_count"] == 2
    assert outcome["issue_codes"] == []
    assert outcome["status"] == "validated"


def test_context_hint_reaches_all_provider_wrappers_without_128k_fallback():
    config = _model_config_from_request({"configured_model_id": "selected", "context_window_hint": 1_000_000})
    provider = RustModelGatewayProvider(config)
    for wrapped in (provider, provider.for_request_budget(1000), provider.for_model_events({"request_id": "r"})):
        assert provider_context_capability(wrapped).max_input_tokens == 1_000_000
    assert not hasattr(RustModelGatewayProvider(ModelConfig("unknown")), "context_window_tokens")


def test_major_latest_period_gaps_require_scope_but_one_missing_table_does_not():
    from openthesis.application.research_preparation import financial_scope_gaps
    filing = _filing()
    base = {"end_date": filing.period_end, "fiscal_period": filing.fiscal_period, "scope": "consolidated"}
    assert financial_scope_gaps([{**base, "concept": "assets"}], [filing])[0]["missing_statements"] == ["income_statement", "cash_flow"]
    assert financial_scope_gaps([{**base, "concept": name} for name in ("assets", "revenue")], [filing]) == ()
    old = replace(filing, period_end="2020-12-31")
    assert financial_scope_gaps([{**base, "end_date": old.period_end, "concept": name}
                                for name in ("assets", "revenue", "operating_cash_flow")], [old, filing])


@pytest.mark.parametrize("bad", [True, "1000000", -1, 0, 1.5])
def test_context_hint_rejects_malformed_configuration(bad):
    with pytest.raises(ValueError, match="MODEL_CONTEXT_HINT_INVALID"):
        _model_config_from_request({"configured_model_id": "selected", "context_window_hint": bad})


def test_known_changed_disclosure_quantity_isolated_without_erasing_unknown_sibling():
    source = {"evidence_id": "filing:revenue", "kind": "filing_text", "raw_text": "Revenue was CNY 120 million in 2025.",
              "company_cik": "CN_TEST", "document_id": "original", "end_date": "2025-12-31", "fiscal_year": 2025}
    output = {"financial_quality": {"analysis": "Revenue was CNY 999 million in 2025.", "evidence_ids": [source["evidence_id"]]},
              "business_model": {"analysis": "A legitimate analysis whose evidence has not yet been independently checked."}}
    safe, audit = _supported_research_fields(output, list(output), {source["evidence_id"]}, {source["evidence_id"]: source}, "en")
    assert "financial_quality" not in safe
    assert safe["business_model"] == output["business_model"]
    assert any(row["reason"] == "source_confirmed_assertion_conflict" for row in audit["quarantined_paths"])
    output['claims'] = [{'claim_id': 'wrong-numeric-claim', 'kind': 'fact',
        'text': 'Revenue was CNY 999 million in 2025.', 'evidence_ids': [source['evidence_id']]}]
    safe, audit = _supported_research_fields(output, list(output), {source['evidence_id']}, {source['evidence_id']: source}, 'en')
    assert safe['claims'] == []
    assert safe['business_model'] == output['business_model']


def test_shared_objects_are_not_misrepresented_as_cyclic_material():
    shared = {"analysis": "Preserve this exact shared conclusion."}
    assert _lossless_synthesis_value({"a": shared, "b": shared}) == {"a": shared, "b": shared}
    cyclic = {"analysis": "Readable sibling"}
    cyclic["self"] = cyclic
    assert _lossless_synthesis_value(cyclic)["analysis"] == "Readable sibling"


def test_wire_registry_has_one_body_and_reconstructs_conflicting_source_identities():
    body = "Original disclosure: the period, amount and unit must be retained exactly."
    materials = {"evidence": [{"evidence_id": "filing:a", "raw_text": body, "excerpt": body,
                               "fiscal_year": 2024, "currency": "CNY"},
                              {"evidence_id": "filing:b", "raw_text": body, "fiscal_year": 2025, "currency": "HKD"}]}
    result, prior, registry = canonical_request_materials(materials, {})
    import json
    assert json.dumps([result, prior, registry]).count(body) == 1
    def restore(value):
        if isinstance(value, dict):
            if set(value) == {"material_ref"}:
                return registry[value["material_ref"]]["text"]
            return {key: restore(child) for key, child in value.items()}
        return [restore(item) for item in value] if isinstance(value, list) else value
    assert restore(result) == materials


def test_numeric_wire_reference_restores_exact_fact_and_evidence_without_merging_overrides():
    from openthesis.financial_evidence import build_fact_evidence
    from openthesis.demo import demo_facts
    facts = demo_facts()[:2]
    evidence = build_fact_evidence(facts)
    evidence[0]['currency'] = 'USD'  # Different metadata is not deduplicated away.
    result = prompt_materials({'facts': facts, 'evidence': evidence}, 'financial-analyst')
    by_id = {fact['fact_id']: fact for fact in result['accepted_facts']}
    originals = result['evidence_partitions']['quantitative_facts']
    assert all('accepted_fact_ref' in item and 'value' not in item for item in originals)
    restored = [{**by_id[item['accepted_fact_ref']], **{key: value for key, value in item.items() if key != 'accepted_fact_ref'}}
                for item in originals]
    for expected, actual in zip(evidence, restored):
        from openthesis.research_execution.reading_view import _HOST_AUDIT_FIELDS
        assert all(actual[key] == value for key, value in expected.items() if key not in _HOST_AUDIT_FIELDS)
        assert 'generation_id' not in actual  # Local publication lease is not source material.
    assert restored[0]['currency'] == 'USD'


def test_publication_generation_does_not_churn_identical_source_prompt():
    import json
    from openthesis.demo import demo_facts
    from openthesis.financial_evidence import build_fact_evidence
    from openthesis.research import _agent_prompt_bundle
    fact = {**demo_facts()[0], 'generation_id': 'local-publication-one'}
    def prompt(value):
        return _agent_prompt_bundle('financial-analyst', 'Review financial evidence', 'en',
            json.dumps({'facts': [value], 'evidence': build_fact_evidence([value])}), {})
    assert prompt(fact) == prompt({**fact, 'generation_id': 'local-publication-two'})
    assert prompt(fact) != prompt({**fact, 'value': fact['value'] + 1})


def test_formal_preparation_finishes_before_any_research_wire(tmp_path):
    from openthesis.application.research_preparation import NewResearchPreparer
    from openthesis.application_services import ResearchOrchestrator
    from test_source_prefetch_overlap_303 import fixture
    from test_research_workflow import SevenStageProvider
    company, _filing_value, official, store, request = fixture(tmp_path)
    provider = SevenStageProvider()
    collection = official.pipeline.engine.collect_candidate_batches
    events = []
    def collecting(*args, **kwargs):
        assert provider.calls == [], "research started while financial material was changing"
        events.append("collecting")
        result = collection(*args, **kwargs)
        events.append("collected")
        return result
    official.pipeline.engine.collect_candidate_batches = collecting
    preparer = NewResearchPreparer(store, official, SimpleNamespace())
    original_prepare = preparer.prepare
    def prepared(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        assert provider.calls == []
        events.append("frozen")
        return result
    preparer.prepare = prepared
    ResearchOrchestrator(store, lambda _config: provider, new_research_preparer=preparer).start(request)
    assert events == ["collecting", "collected", "frozen"]
    assert provider.calls.count("business-analyst") == 1
    assert store.get_financial_research_snapshot(company.cik)["facts"]


def test_missing_core_financial_scope_uses_source_stage_without_global_confirmation_gate(tmp_path):
    from openthesis.application.research_preparation import NewResearchPreparer
    from openthesis.application_services import ResearchOrchestrator
    from test_source_prefetch_overlap_303 import fixture
    from test_research_workflow import SevenStageProvider
    company, _filing_value, official, store, request = fixture(tmp_path)
    original_prepare = official.prepare
    def missing(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        profile = replace(result.view.profile, facts=(), fact_dicts=(), metrics=(), interim_metrics=())
        return replace(result, view=replace(result.view, fact_dicts=(), facts=(),
            evidence=(), canonical_dataset=None, profile=profile,
            capabilities=tuple((key, False) for key, _value in result.view.capabilities)))
    official.prepare = missing
    provider = SevenStageProvider()
    orchestrator = ResearchOrchestrator(store, lambda _config: provider,
        new_research_preparer=NewResearchPreparer(store, official, SimpleNamespace()))
    run = orchestrator.start(request)
    assert provider.calls.count('business-analyst') == 1
    assert 'financial-analyst' not in provider.calls
    assert run.data_snapshot['official_source_state']['scope_gaps']
    assert run.data_snapshot['official_source_state']['recovery_targets'] == [_filing_value.accession_number]
    assert run.data_snapshot['financial_capabilities']['quantitative_facts'] is False
    assert run.data_snapshot['financial_capabilities']['business_disclosures'] is True
    # The verified original remains available for independent source-stage work.
    assert store.get_filings(company.cik)


def test_physical_request_diagnostics_do_not_export_prompts_and_keep_usage_unknown(tmp_path):
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    from openthesis.research_execution.registry import standard_model_registry
    from openthesis.model_scheduler import ModelRequestScheduler
    from test_model_request_journal import _journal, _store
    from test_model_stage_executor import RecordingProvider
    import json
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(standard_model_registry(), "frozen-material")["financial-analyst"]
    provider = RecordingProvider({"analysis": "Candidate without supplier usage"})
    executor = ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal)
    system = "PRIVATE_SYSTEM_NOT_FOR_DIAGNOSTIC_EXPORT"
    body = "PRIVATE_DISCLOSURE_NOT_FOR_DIAGNOSTIC_EXPORT"
    request = ModelStageRequest("financial-analyst", system, json.dumps({"material": body}))
    with journal.bind("financial-analyst", generation):
        executor.execute(request)
        executor.execute(request)
    assert len(provider.requests) == 1
    with store.connect() as db:
        rows = db.execute("SELECT diagnostics_json FROM model_wire_diagnostics").fetchall()
    assert len(rows) == 1
    encoded = rows[0]["diagnostics_json"]
    assert body not in encoded and system not in encoded
    diagnostic = json.loads(encoded)
    assert diagnostic["input_bytes"] == request.input_bytes
    assert diagnostic["wire_state"] == "response_received"
    assert diagnostic["usage"]["input_tokens"] is None
    assert diagnostic["token_counting_method"] == "utf8_bytes_estimate"
    assert diagnostic["input_tokens_estimate"] >= diagnostic["input_bytes"]
    status = store.model_call_status(journal.run_id)
    assert len(status) == 1
    assert status[0]["purpose"] == "generation"
    assert status[0]["diagnostics"]["input_bytes"] == request.input_bytes
    assert body not in json.dumps(status)


def test_confirmed_context_rejection_is_not_retried_with_identical_material(tmp_path):
    from openthesis.providers import ProviderError
    from openthesis.research import SynthesisContextLimitError
    from test_synthesis_capacity_preflight import RecordingCoverageProvider, _scenario, _call, _coverage_checkpoints
    class RejectOnce(RecordingCoverageProvider):
        def __init__(self):
            super().__init__(None)
            self.rejected = None
        def generate(self, system_prompt, user_prompt, **kwargs):
            if self.rejected is None:
                self.rejected = (system_prompt, user_prompt)
                raise ProviderError('Confirmed context rejection', code='MODEL_CONTEXT_CAPACITY')
            assert (system_prompt, user_prompt) != self.rejected
            return super().generate(system_prompt, user_prompt, **kwargs)
    provider = RejectOnce()
    storage, run, workflow, context, prior = _scenario(tmp_path, provider)
    try:
        _call(workflow, context, prior, run)
    except SynthesisContextLimitError:
        pass  # An indivisible envelope stays explicit; completed partitions survive.
    assert provider.rejected is not None
    assert provider.requests
    assert _coverage_checkpoints(storage, run)
