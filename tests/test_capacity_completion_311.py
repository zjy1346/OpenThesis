"""Offline completion regressions: successful recovery, not just saved gaps."""
import json

from openthesis.providers import ModelConfig, RustModelGatewayProvider
from openthesis.research import _recursive_synthesis_chunks
from openthesis.research_execution.model import provider_context_capability


def test_long_source_string_is_partitioned_without_losing_any_character():
    original = "2025年收入100元。\n\n风险：原材料价格上涨。\n" * 100
    chunks = _recursive_synthesis_chunks(original, 1200)
    assert len(chunks) > 1
    assert "".join(chunks) == original
    assert all(len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()) <= 1200 for item in chunks)


def test_actual_usage_calibrates_the_real_gateway_budget_across_scoped_copies():
    provider = RustModelGatewayProvider(ModelConfig("fixture-capacity", context_window_hint=100_000))
    initial = provider_context_capability(provider).max_input_bytes
    scoped = provider.for_model_events({"request_id": "r"})
    scoped.observe_input_usage(input_bytes=90_000, input_tokens=60_000,
        provider_id="deepseek", model_id="fixture-model", endpoint_host="api.example.test")
    changed = provider_context_capability(provider)
    assert changed.max_input_bytes != initial
    assert changed.max_input_bytes <= (100_000 - changed.reserved_output_tokens - changed.reserved_repair_tokens) * 1.5
    assert provider.for_request_budget(1000).input_token_measurement("s", "资料")["method"] != "utf8_bytes_div3_estimate"


def test_gateway_stage_output_budget_reaches_the_real_request_builder():
    class Capture(RustModelGatewayProvider):
        def _invoke_gateway(self, request):
            return request
    provider = Capture(ModelConfig("fixture-model"))
    scoped = provider.for_output_budget(32_768)
    request = scoped.generate("system", "material")
    assert request["max_output_tokens"] == 32_768
    assert "max_output_tokens" not in provider.generate("system", "material")


def test_long_source_fragments_retain_identity_and_exact_ranges():
    source = {"evidence_id": "filing:original", "document_id": "official-annual", "content_hash": "a" * 64,
              "page": 12, "currency": "CNY", "fiscal_period": "FY", "end_date": "2025-12-31",
              "raw_text": "收入100元；经营现金流80元。\n" * 200}
    chunks = _recursive_synthesis_chunks(source, 1600)
    fragments = [item for item in chunks if "_source_span" in item]
    assert "".join(item["raw_text"] for item in fragments) == source["raw_text"]
    end = 0
    for item in fragments:
        assert all(item[key] == source[key] for key in source if key != "raw_text")
        span = item["_source_span"]
        assert span["start"] == end
        end = span["end"]
        assert item["raw_text"] == source["raw_text"][span["start"]:end]
        assert len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()) <= 1600


def test_coverage_refines_repeated_confirmed_rejection_until_every_source_span_is_read(tmp_path):
    from openthesis.providers import ProviderError
    from openthesis.research_execution.model import ProviderContextCapability
    from test_synthesis_capacity_preflight import _scenario
    class Provider:
        max_input_bytes = 100_000
        def __init__(self):
            self.accepted, self.rejected = [], set()
        def generate(self, system, user, **kwargs):
            size = len(system.encode()) + len(user.encode())
            if size > 18_000:
                assert user not in self.rejected, "identical rejected wire was resubmitted"
                self.rejected.add(user)
                raise ProviderError("confirmed input rejection", code="MODEL_CONTEXT_CAPACITY")
            self.accepted.append(json.loads(user)["material"])
            return {"observations": ["Original material read."], "source_evidence_ids": ["filing:source"]}
    provider = Provider()
    _storage, _run, workflow, _context, _prior = _scenario(tmp_path, provider)
    text = "收入100元，现金流80元。\n" * 2500
    packet = {"evidence_id": "filing:source", "document_id": "official-annual", "source_page": 12, "raw_text": text}
    capsule = workflow._coverage_capsule(agent_id="financial-analyst", role_prompt="Read all source spans.",
        packet=packet, index=1, total=1, capability=ProviderContextCapability(explicit_max_input_bytes=150_000))
    assert len(provider.rejected) >= 2
    assert capsule["source_evidence_ids"] == ["filing:source"]
    found = []
    def visit(value):
        if isinstance(value, dict):
            if "_source_span" in value:
                found.append((value["_source_span"]["start"], value["raw_text"]))
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for value in provider.accepted:
        visit(value)
    assert "".join(value for _offset, value in sorted(found)) == text


def test_received_usage_is_reloaded_only_for_the_same_model_configuration(tmp_path):
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    from openthesis.research_execution.registry import standard_model_registry
    from openthesis.research_execution.responses import received_response
    from test_model_request_journal import _journal, _store
    class Gateway(RustModelGatewayProvider):
        def _invoke_gateway(self, request):
            return received_response({"analysis": "Received source-bound analysis."}, {
                "input_tokens": 60_000, "output_tokens": 10, "provider_id": "deepseek",
                "model_id": "fixture-actual-model", "endpoint_host": "api.example.test",
                "configured_model_id": "selected-model"})
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(standard_model_registry(), "materials")["financial-analyst"]
    config = ModelConfig("selected-model", configuration_version=3, context_window_hint=100_000)
    provider = Gateway(config)
    executor = ModelStageExecutor(provider, ModelRequestScheduler(), request_journal=journal)
    with journal.bind("financial-analyst", generation):
        executor.execute(ModelStageRequest("financial-analyst", "s", "资料" * 15_000))
    assert provider.input_bytes_per_token > 1
    reopened = Gateway(config)
    journal.hydrate_input_budget(reopened)
    assert reopened.input_bytes_per_token == provider.input_bytes_per_token
    changed = Gateway(ModelConfig("selected-model", configuration_version=4, context_window_hint=100_000))
    journal.hydrate_input_budget(changed)
    assert changed.input_bytes_per_token == 1


def test_matching_counter_overrides_byte_estimate_but_not_explicit_transport_limit():
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    class Provider:
        context_window_tokens = 10_000
        tokenizer_name = "fixture-matching-tokenizer"
        def count_input_tokens(self, system, user):
            return 100
        def generate(self, *args, **kwargs):
            return {"analysis": "Complete"}
    provider = Provider()
    executor = ModelStageExecutor(provider, ModelRequestScheduler())
    result = executor.execute(ModelStageRequest("analysis", "s", "中" * 3000, max_input_bytes=1000))
    assert result["analysis"] == "Complete"
    provider.max_input_bytes = 1000
    import pytest
    from openthesis.research import SynthesisContextLimitError
    with pytest.raises(SynthesisContextLimitError):
        executor.execute(ModelStageRequest("analysis", "s", "中" * 3000, max_input_bytes=1000))


def test_adaptive_recovery_finishes_the_requested_stage_not_just_a_partial_checkpoint(tmp_path):
    from openthesis.providers import ProviderError
    from test_synthesis_capacity_preflight import _scenario, _call
    class Provider:
        def __init__(self):
            self.rejected, self.accepted = set(), []
        def generate(self, system, user, **kwargs):
            payload = json.loads(user)
            if len(system.encode()) + len(user.encode()) > 50_000:
                assert user not in self.rejected
                self.rejected.add(user)
                raise ProviderError("confirmed context capacity", code="MODEL_CONTEXT_CAPACITY")
            self.accepted.append(payload)
            if "target_agent" in payload:
                return {"observations": ["Source-backed observations."], "risks": ["A preserved counterargument."]}
            return {"executive_summary": "Complete source-backed synthesis.", "claims": [],
                    "business_model": "Complete", "financial_quality": "Complete", "balance_sheet": "Complete",
                    "competitive_position": "Complete", "growth_opportunities": ["Complete"],
                    "counterarguments": ["A preserved counterargument."], "scenarios": ["Complete"],
                    "thesis": "Complete", "invalidation_conditions": ["Complete"],
                    "leading_indicators": ["Complete"], "unresolved_questions": ["Explicit unknown."]}
    provider = Provider()
    storage, run, workflow, context, prior = _scenario(tmp_path, provider)
    prior["analyses"]["financial_quality"] = ["Complete company source. " * 5000]
    result = _call(workflow, context, prior, run)
    assert result["executive_summary"] == "Complete source-backed synthesis."
    assert result["counterarguments"] == ["A preserved counterargument."]
    assert result["_stage_material_manifest"]["mode"] == "partitioned-material-coverage"
    assert result["_coverage_ledger"]["complete"] is True
    assert provider.rejected and provider.accepted


def test_confirmed_output_exhaustion_does_not_repeat_the_whole_request():
    from copy import copy
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    from openthesis.research_execution.responses import received_response
    class Provider:
        context_window_tokens = 100_000
        def __init__(self):
            self.calls, self.budget = [], None
        def for_output_budget(self, tokens):
            scoped = copy(self)
            scoped.budget = tokens
            return scoped
        def generate(self, *_args, **_kwargs):
            self.calls.append(self.budget)
            return received_response({"analysis": "Complete material-backed result."}, {
                "input_tokens": 1000, "output_budget_tokens": self.budget,
                "finish_reason": "length" if self.budget < 8192 else "stop"})
    provider = Provider()
    result = ModelStageExecutor(provider, ModelRequestScheduler()).execute(
        ModelStageRequest("analysis", "system", "material", max_output_tokens=4096))
    assert provider.calls == [4096]
    assert result["analysis"] == "Complete material-backed result."
    assert result["_output_incomplete"] == {
        "code": "MODEL_OUTPUT_BUDGET_EXHAUSTED", "state": "pending"}


def test_unsupported_output_budget_never_replays_identical_wire_and_retains_partial():
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    class Provider:
        calls = 0
        def generate(self, *_args, **_kwargs):
            self.calls += 1
            return {"analysis": "A useful received finding.", "_response_meta": {"finish_reason": "length"}}
    provider = Provider()
    result = ModelStageExecutor(provider, ModelRequestScheduler()).execute(
        ModelStageRequest("analysis", "system", "material", max_output_tokens=4096))
    assert provider.calls == 1
    assert result["analysis"] == "A useful received finding."
    assert result["_output_incomplete"]["code"] == "MODEL_OUTPUT_BUDGET_EXHAUSTED"


def test_truncated_output_with_all_section_keys_is_not_verified_complete():
    from openthesis.research_execution.executor import StageExecutor, StageValidation
    from openthesis.research_execution.registry import StageRegistry, StageSpec
    candidate = {"analysis": "Useful but truncated finding.",
                 "_output_incomplete": {"code": "MODEL_OUTPUT_BUDGET_EXHAUSTED"}}
    graph = StageRegistry((StageSpec("analysis", "1", "model", "verify", output_contract="analysis"),))
    result = StageExecutor({"model": lambda *_: candidate}, {
        "verify": lambda _spec, output, _inputs: StageValidation(True, output, "completed_verified")
    }).execute(graph, {})["analysis"]
    assert result.validation.state == "completed_partial"
    assert result.validation.trusted_output["analysis"] == candidate["analysis"]
    assert not result.validation.passed
    assert "MODEL_OUTPUT_BUDGET_EXHAUSTED" in result.validation.issues


def test_capsule_keeps_findings_but_never_certifies_exhausted_output(tmp_path):
    from openthesis.research_execution.model import ProviderContextCapability
    from test_synthesis_capacity_preflight import _scenario
    class Provider:
        def generate(self, *_args, **_kwargs):
            return {"observations": ["Useful source-backed finding."],
                    "_response_meta": {"finish_reason": "length"}}
    _store, _run, workflow, _context, _prior = _scenario(tmp_path, Provider())
    capsule = workflow._coverage_capsule(agent_id="financial-analyst", role_prompt="Read the source.",
        packet={"evidence_id": "filing:original", "raw_text": "Revenue 100 CNY."},
        index=1, total=1, capability=ProviderContextCapability(explicit_max_input_bytes=100_000),
        refinement=8)
    assert capsule["complete"] is False
    assert capsule["content"]["observations"] == ["Useful source-backed finding."]


def test_synthesis_verifier_retains_valid_claims_but_rejects_false_completeness():
    from unittest.mock import patch
    from openthesis.research import validate_research_synthesis
    from test_report_readiness import _complete_report
    report = {**_complete_report(), "_response_meta": {"finish_reason": "length"}}
    with patch("openthesis.research.verify_agent_output", return_value={
            "passed": True, "issues": [], "verified_claim_count": 1, "unsupported_fact_count": 0}):
        verification = validate_research_synthesis(report, set())
    assert verification["claim_integrity_passed"] is True
    assert verification["passed"] is False
    assert "MODEL_OUTPUT_BUDGET_EXHAUSTED" in verification["issues"]
    assert verification["report_readiness"]["complete"] is False


def test_length_cut_is_journaled_once_and_reuses_the_received_body(tmp_path):
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    from openthesis.research_execution.registry import standard_model_registry
    from openthesis.research_execution.responses import received_response
    from test_model_request_journal import _journal, _store
    calls = []
    class Gateway(RustModelGatewayProvider):
        def _invoke_gateway(self, request):
            limit = request["max_output_tokens"]
            calls.append(limit)
            return received_response({"analysis": "Source-backed analysis."}, {
                "input_tokens": 1000, "output_tokens": 100,
                "output_budget_tokens": limit, "finish_reason": "length" if limit == 4096 else "stop"})
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(standard_model_registry(), "materials")["financial-analyst"]
    executor = ModelStageExecutor(Gateway(ModelConfig("selected-model", context_window_hint=100_000)),
        ModelRequestScheduler(), request_journal=journal)
    request = ModelStageRequest("financial-analyst", "system", "material", max_output_tokens=4096)
    with journal.bind("financial-analyst", generation):
        first = executor.execute(request)
        second = executor.execute(request)
    assert calls == [4096]
    assert first == second
    assert first["_output_incomplete"]["state"] == "pending"
    with store.connect() as db:
        rows = db.execute("SELECT input_digest,response_received FROM model_request_journal").fetchall()
        diagnostics = [json.loads(row[0]) for row in db.execute("SELECT diagnostics_json FROM model_wire_diagnostics")]
    assert len(rows) == 1
    assert len({row["input_digest"] for row in rows}) == 1
    assert all(row["response_received"] == 1 for row in rows)
    assert [item["applied_output_budget_tokens"] for item in diagnostics] == [4096]


def test_length_cut_invalid_json_does_not_trigger_same_limit_format_repairs(tmp_path):
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    from openthesis.research_execution.registry import standard_model_registry
    from openthesis.research_execution.responses import received_response
    from test_model_request_journal import _journal, _store
    calls = []
    class Gateway(RustModelGatewayProvider):
        def _invoke_gateway(self, request):
            calls.append(request)
            return received_response({"analysis": "Closed source-backed finding.",
                "_response_error": "invalid_json", "structured_output_valid": False}, {"finish_reason": "length"})
    journal = _journal(_store(tmp_path))
    generation = journal.prepare(standard_model_registry(), "materials")["financial-analyst"]
    executor = ModelStageExecutor(Gateway(ModelConfig("selected-model")), ModelRequestScheduler(), request_journal=journal)
    with journal.bind("financial-analyst", generation):
        result = executor.execute(ModelStageRequest("financial-analyst", "system", "material", max_output_tokens=4096))
    assert len(calls) == 1
    assert result["analysis"] == "Closed source-backed finding."
    assert result["_output_incomplete"]["state"] == "pending"


def test_real_gateway_without_context_hint_keeps_a_usable_role_specific_recovery_budget(tmp_path):
    from test_synthesis_capacity_preflight import _scenario
    gateway = RustModelGatewayProvider(ModelConfig("selected-model"))
    _store, _run, workflow, _context, _prior = _scenario(tmp_path, gateway)
    budgets = [workflow._stage_output_budget(stage) for stage in
               ("financial-analyst", "growth-opportunities", "research-synthesizer")]
    assert budgets == sorted(set(budgets))
    for stage in ("financial-analyst", "growth-opportunities", "research-synthesizer"):
        assert provider_context_capability(workflow._stage_budget_provider(stage)).max_input_bytes > 30_000


def test_synthesis_output_exhaustion_adds_only_missing_sections_once_and_preserves_material(tmp_path):
    from test_synthesis_capacity_preflight import _scenario
    seen = []
    class Provider:
        def generate(self, _system, user, **_kwargs):
            payload = json.loads(user)
            seen.append(payload)
            delivery = payload.get("sectional_delivery")
            if delivery:
                sections = delivery["sections"]
                return {section: "Complete independently sourced " + section for section in sections}
            return {"executive_summary": "Original closed executive summary.",
                    "business_model": "Original closed business model.", "claims": [],
                    "extension": {"risk": "Preserve this received concern."},
                    "_response_meta": {"finish_reason": "length"}}
    _store, run, workflow, context, _prior = _scenario(tmp_path, Provider())
    result = workflow._run_synthesis_with_budget(context, {}, {}, {}, {}, run=run)
    assert len(seen) == 2
    delivery = seen[1]["sectional_delivery"]
    requested = delivery["sections"]
    initially_closed = {"executive_summary", "business_model"}
    assert isinstance(requested, list) and len(requested) > 1
    assert requested == [section for section in delivery["all_sections"] if section not in initially_closed]
    assert initially_closed.isdisjoint(requested)
    assert {key for key in result if key in delivery["all_sections"]} >= set(delivery["all_sections"])
    assert seen[1]["material_registry"] == seen[0]["material_registry"]
    assert seen[1]["research_context"] == seen[0]["research_context"]
    assert "_output_incomplete" not in result
    assert result["_sectional_delivery"]["complete"] is True
    assert result["executive_summary"] == "Original closed executive summary."
    assert result["business_model"] == "Original closed business model."
    assert all(result.get(section) == "Complete independently sourced " + section for section in requested)
    assert result["extension"] == {"risk": "Preserve this received concern."}
    assert "Do not shorten" in seen[1]["task_instructions"]


def test_sectional_output_failure_retains_received_sections_and_does_not_claim_completion(tmp_path):
    from openthesis.providers import ProviderError
    from test_synthesis_capacity_preflight import _scenario
    calls = []
    class Provider:
        def generate(self, _system, user, **_kwargs):
            payload = json.loads(user)
            delivery = payload.get("sectional_delivery")
            calls.append(delivery.get("sections") if delivery else None)
            if delivery:
                raise ProviderError("unknown remote outcome", code="MODEL_NETWORK_ERROR")
            return {"executive_summary": "Keep original closed executive summary.",
                    "financial_quality": "Keep original closed financial finding.",
                    "extension": {"risk": "Preserve original received analysis."},
                    "_response_meta": {"finish_reason": "length"}}
    _store, run, workflow, context, _prior = _scenario(tmp_path, Provider())
    result = workflow._run_synthesis_with_budget(context, {}, {}, {}, {}, run=run)
    assert len(calls) == 2
    assert calls[0] is None
    assert isinstance(calls[1], list) and len(calls[1]) > 1
    assert "executive_summary" not in calls[1]
    assert "financial_quality" not in calls[1]
    assert result["financial_quality"] == "Keep original closed financial finding."
    assert result["executive_summary"] == "Keep original closed executive summary."
    assert result["extension"] == {"risk": "Preserve original received analysis."}
    assert "business_model" not in result
    assert result["_sectional_delivery"]["complete"] is False
    assert result["_output_incomplete"]["state"] == "pending"
    assert all(item.get("code") == "MODEL_NETWORK_ERROR" for item in result["_sectional_delivery"]["sections"]
               if item["section"] in calls[1])


def test_auxiliary_section_and_coverage_receipts_are_never_adopted_as_full_stage_candidates(tmp_path):
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
    from openthesis.research_execution.registry import standard_model_registry
    from test_model_request_journal import _journal, _store
    class Provider:
        def generate(self, *_args, **_kwargs):
            return {"analysis": "One material partition or report section."}
    journal = _journal(_store(tmp_path))
    generation = journal.prepare(standard_model_registry(), "materials")["final-synthesis"]
    with journal.bind("final-synthesis", generation):
        for purpose in ("section_delivery", "material_coverage"):
            ModelStageExecutor(Provider(), ModelRequestScheduler(), request_journal=journal).execute(
                ModelStageRequest("auxiliary", "system", f"material:{purpose}", purpose=purpose))
    assert journal.saved_generation_candidates() == {}
    with journal.store.connect() as db:
        rows = db.execute("SELECT purpose,input_digest FROM model_request_journal").fetchall()
    assert sorted(row[0] for row in rows) == ["material_coverage", "section_delivery"]
    assert len({row[1] for row in rows}) == 2


def test_explicit_refusal_with_length_metadata_does_not_trigger_output_recovery(tmp_path):
    from test_synthesis_capacity_preflight import _scenario
    calls = []
    class Provider:
        def generate(self, *_args, **_kwargs):
            calls.append(1)
            return {"_response_error": "provider_refusal", "_response_meta": {
                "finish_reason": "length", "refusal": "Provider declined this request."}}
    _store, run, workflow, context, _prior = _scenario(tmp_path, Provider())
    result = workflow._run_synthesis_with_budget(context, {}, {}, {}, {}, run=run)
    assert calls == [1]
    assert result["_response_error"] == "provider_refusal"
    assert "_sectional_delivery" not in result
