import json
from copy import deepcopy

from openthesis.domain import ResearchRun
from openthesis.demo import DEMO_COMPANY
from openthesis.providers import ModelConfig
from openthesis.research import _claim_text_support_state, _supported_research_fields
from openthesis.research_assertions import AssertionSession, assertion_session, source_identity, validate_response, validate_with_assertions
from openthesis.research_execution.executor import StageValidation
from openthesis.research_execution.model import ModelStageExecutor
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.model_scheduler import ModelRequestScheduler
from openthesis.resilience.contracts import digest
from openthesis.storage import Storage


def record(text):
    return {"evidence_id": "filing:business", "kind": "filing_text", "raw_text": text,
            "company_cik": "CN_TEST", "end_date": "2025-12-31", "document_id": "official:1",
            "source_url": "https://example.invalid/official", "page": 7}


def review(query, state="supported", **overrides):
    source = query["sources"][0]
    return {"assertion_id": query["assertion_id"], "state": state, "source_spans": [{
        "evidence_id": source["evidence_id"], "start": 0, "end": len(source["raw_text"]),
        "quote": source["raw_text"], "source_sha256": source["source_sha256"], **overrides}]}


def test_common_words_are_not_fact_support_and_negated_substring_is_not_quote():
    source = record("The company sells industrial refrigeration equipment.")
    assert _claim_text_support_state({"kind": "fact", "text": "The company sells consumer insurance."}, [source]) == "insufficient_evidence"
    assert _claim_text_support_state({"kind": "fact", "text": source["raw_text"]}, [source]) == "text_supported"
    negative = record("It is not true that the company sells consumer insurance.")
    assert _claim_text_support_state({"kind": "fact", "text": "the company sells consumer insurance."}, [negative]) != "text_supported"


def test_short_exact_chinese_quote_and_unknown_visual_observation_do_not_share_trust():
    source = record("公司主要从事工业制冷设备的研发、生产和销售。")
    claim = {"kind": "fact", "text": source["raw_text"]}
    assert _claim_text_support_state(claim, [source]) == "text_supported"
    assert _claim_text_support_state(claim, [{**source, "kind": "unverified_visual_observation"}]) == "insufficient_evidence"


def test_model_judgment_is_bound_to_assertion_and_exact_current_source_spans():
    source = record("公司主要从事工业制冷设备的研发、生产和销售。")
    claim = {"kind": "fact", "text": "公司的主营业务为工业制冷设备研发、制造与销售。"}
    collected = AssertionSession()
    with assertion_session(collected):
        assert _claim_text_support_state(claim, [source]) == "insufficient_evidence"
    query = next(iter(collected.queries.values()))
    response = {"structured_output_valid": True, "results": [review(query)]}
    accepted = validate_response(response, [query], "configured-model:1")
    with assertion_session(AssertionSession(accepted)):
        assert _claim_text_support_state(claim, [source]) == "text_supported"
        assert _claim_text_support_state({**claim, "text": "公司主要经营消费保险。"}, [source]) == "insufficient_evidence"
        assert _claim_text_support_state(claim, [{**source, "raw_text": "公司现已停止所有制冷设备业务。"}]) != "text_supported"
    assert not validate_response({**response, "results": [review(query, quote="fabricated")]}, [query], "configured-model:1")
    assert not validate_response({**response, "results": [review(query), review(query)]}, [query], "configured-model:1")
    contradiction = validate_response({**response, "results": [review(query, "contradicted")]}, [query], "configured-model:1")
    with assertion_session(AssertionSession(contradiction)):
        assert _claim_text_support_state(claim, [source]) == "contradicted"


def test_invalid_parent_reference_is_quarantined_without_deleting_independent_child():
    source = record("公司主要从事工业制冷设备的研发、生产和销售。")
    records = {source["evidence_id"]: source}
    original = {"business_model": {"evidence_ids": ["filing:missing"],
        "wrong": "公司经营消费保险。", "detail": {"evidence_ids": [source["evidence_id"]], "text": source["raw_text"]}}}
    safe, audit = _supported_research_fields(original, ["business_model"], set(records), records, "zh-CN")
    assert safe["business_model"]["detail"]["text"] == source["raw_text"]
    assert safe["business_model"]["wrong"] == original["business_model"]["wrong"]
    assert "filing:missing" not in json.dumps(safe)
    assert any(item["path"] == "business_model.evidence_ids" for item in audit["pending_paths"])
    assert any(item["path"] == "business_model.wrong" for item in audit["pending_paths"])


def test_semantic_batch_uses_formal_journal_and_restart_validation_does_not_call_provider(tmp_path):
    store = Storage(tmp_path)
    store.save_company(DEMO_COMPANY)
    run = ResearchRun(run_id="assertion-run", company=DEMO_COMPANY, workflow_id="workflow",
                      research_pack_id="test", research_pack_version="1", provider_id="test",
                      model_id="model", data_as_of="2025-12-31")
    store.save_run(run)
    journal = ModelRequestJournal(store, run.run_id, "configured:1", timeout_seconds=2, cancel_check=lambda: False)
    registry = StageRegistry([StageSpec("business", "1", "analysis", "verify", output_contract="body")])
    generations = journal.prepare(registry, digest({"sources": "fixture"}))
    source = record("公司主要从事工业制冷设备的研发、生产和销售。")
    candidate = {"business_model": {"text": "公司的主营业务为工业制冷设备研发、制造与销售。", "evidence_ids": [source["evidence_id"]]}}
    original = deepcopy(candidate)
    calls = []

    class Provider:
        def generate(self, _system, user, **_kwargs):
            payload = json.loads(user)
            queries = [{**query, "sources": [payload["sources"][key] for key in query["source_block_ids"]]}
                       for query in payload["assertions"]]
            calls.append(len(queries))
            return {"structured_output_valid": True, "results": [review(query) for query in queries]}

    def validate():
        safe, audit = _supported_research_fields(candidate, ["business_model"], {source["evidence_id"]}, {source["evidence_id"]: source}, "zh-CN")
        return StageValidation(bool(safe.get("business_model")), safe if safe.get("business_model") else {},
                               "completed_verified" if safe.get("business_model") else "needs_action", record={"audit": audit})

    executor = ModelStageExecutor(Provider(), ModelRequestScheduler(), request_journal=journal)
    with journal.bind("business", generations["business"]):
        fresh = validate_with_assertions(validate, stage_id="business", journal=journal, model_executor=executor, allow_network=True)
    assert fresh.passed
    restored = ModelRequestJournal(Storage(tmp_path), run.run_id, "configured:1", timeout_seconds=2, cancel_check=lambda: False)
    cached = validate_with_assertions(validate, stage_id="business", journal=restored)
    assert cached.passed
    assert calls == [1]
    assert candidate == original
    assert cached.record["assertion_results"][0]["method"] == "configured_model_semantics"
    assert cached.record["audit"]["nodes"][0]["assertion_results"][0]["source_spans"][0]["page"] == 7


def test_abbreviated_company_name_does_not_split_an_exact_official_sentence():
    source = record("Example Co. sells industrial refrigeration equipment. Other activities are limited.")
    session = AssertionSession()
    assert session.classify({"kind": "fact", "text": "Example Co. sells industrial refrigeration equipment."}, [source]) == "text_supported"
    assert not session.queries
    assert session.classify({"kind": "fact", "text": "Example Co. sells consumer insurance."}, [source]) != "text_supported"


def test_whitespace_cannot_change_english_word_boundaries_or_negation():
    source = record("The company operates nowhere else.")
    assert AssertionSession().classify({"kind": "fact", "text": "The company operates no where else."}, [source]) != "text_supported"


def test_fact_role_and_asserted_scope_cannot_reuse_an_inference_judgment():
    source = record("公司主要从事工业制冷设备的研发、生产和销售。")
    session = AssertionSession()
    text = "公司的主营业务为工业制冷设备研发、制造与销售。"
    session.classify({"kind": "fact", "text": text}, [source])
    session.classify({"kind": "inference", "text": text}, [source])
    assert session.classify({"kind": "fact", "text": text, "fiscal_year": 2024}, [source]) == "insufficient_evidence"
    assert len(session.queries) == 2


def test_exact_quote_cannot_bypass_explicit_entity_period_or_scope_identity():
    source = {**record("The company manufactures industrial equipment."), "scope": "consolidated", "fiscal_period": "FY"}
    for scope in ({"company_cik": "different"}, {"fiscal_year": 2024}, {"end_date": "2024-12-31"},
                  {"fiscal_period": "Q1"}, {"scope": "parent"}, {"entity": "unconfirmed-entity"}):
        claim = {"kind": "fact", "text": source["raw_text"], **scope}
        assert _claim_text_support_state(claim, [source]) == "insufficient_evidence"
        session = AssertionSession()
        assert session.classify(claim, [source]) == "insufficient_evidence" and not session.queries
    assert _claim_text_support_state({"kind": "fact", "text": source["raw_text"], "scope": "consolidated"}, [source]) == "text_supported"


def test_bad_sentence_does_not_erase_independent_full_quotation():
    source = record("公司主要从事工业制冷设备的研发、生产和销售。")
    body = source["raw_text"] + "公司另外销售消费保险。"
    candidate = {"business_model": {"text": body, "evidence_ids": [source["evidence_id"]]}}
    session = AssertionSession()
    with assertion_session(session):
        safe, audit = _supported_research_fields(candidate, ["business_model"], {source["evidence_id"]}, {source["evidence_id"]: source}, "zh-CN")
    assert source["raw_text"] in safe["business_model"]["text"]
    assert "公司另外销售消费保险。" in safe["business_model"]["text"]
    assert audit["nodes"][0]["validation_state"] == "partial"
    assert not audit["nodes"][0]["calculation_eligible"]
    assert any("#sentence[1]" in item["path"] for item in audit["pending_paths"])
    assert candidate["business_model"]["text"] == body


def test_discourse_dependent_sentence_is_not_salvaged_as_independent_fact():
    from openthesis.research_assertions import independent_sentences
    for text in ("收入并未上升。因此利润会增长。", "Costs rose. Therefore profits may fall.",
                 "Example Co. manufactures equipment. Insurance is not a product."):
        assert independent_sentences(text) == (text,)


def test_metric_name_does_not_bless_unrelated_qualitative_claim():
    source = {"evidence_id": "fact:revenue", "kind": "financial_fact", "concept": "revenue", "value": 100,
              "currency": "CNY", "unit": "CNY", "fiscal_year": 2025}
    candidate = {"financial_quality": {"text": "revenue proves the company is the world's dominant insurer.", "evidence_ids": ["fact:revenue"]}}
    safe, audit = _supported_research_fields(candidate, ["financial_quality"], {"fact:revenue"}, {"fact:revenue": source}, "en")
    assert safe["financial_quality"]["text"] == candidate["financial_quality"]["text"]
    assert audit["pending_paths"]
    assert all(node["validation_state"] == "partial" and not node["calculation_eligible"] for node in audit["nodes"])
