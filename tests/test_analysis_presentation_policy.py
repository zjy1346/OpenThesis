"""Presentation coverage is not a second-model factual-verification verdict."""
from openthesis.research import _supported_research_fields
from openthesis.research_assertions import AssertionSession, assertion_session, validate_response
from openthesis.report_document import assemble_report_document, render_markdown
from openthesis.research_section_validation import proven_content_nodes


SOURCE = {"evidence_id": "filing:business", "kind": "filing_text",
          "company_cik": "CN_TEST", "end_date": "2025-12-31", "document_id": "official:annual",
          "raw_text": "公司主要从事工业制冷设备的研发、生产和销售。", "page": 7}
TEXT = "工业制冷设备制造与销售构成业务基础，售后服务可能有助于客户关系延续。"


def project(text=TEXT, extra=None):
    output = {"business_model": {"analysis": text, "evidence_ids": [SOURCE["evidence_id"]]}, **(extra or {})}
    return _supported_research_fields(output, ["business_model"], {SOURCE["evidence_id"]},
                                     {SOURCE["evidence_id"]: SOURCE}, "zh-CN")


def test_source_bound_analysis_is_visible_partial_not_verified_or_completed():
    safe, audit = project()
    assert safe["business_model"]["analysis"] == TEXT
    node = proven_content_nodes(safe, audit)[0]
    assert node["role"] == "analysis" and node["validation_state"] == "partial"
    assert node["assertion_results"][0]["state"] == "unresolved"
    assert node["assertion_results"][0]["source_bindings"][0]["page"] == 7
    document = assemble_report_document("fixture", [{"artifact_id": "report", "run_id": "fixture",
        "artifact_type": "research-report", "content": {"report": safe,
        "verification": {"passed": False, "section_validation": audit}}}], language="zh-CN")
    body = render_markdown(document)
    assert TEXT in body
    assert "本章含 1 项待核验内容" in body
    assert "事实前提尚未独立核验" not in body
    assert not document.readiness.complete


def test_known_contradiction_is_not_presented_as_analysis():
    session = AssertionSession()
    with assertion_session(session):
        project()
    query = next(iter(session.queries.values()))
    review = validate_response({"structured_output_valid": True, "results": [{
        "assertion_id": query["assertion_id"], "state": "contradicted", "source_spans": [{
            "source_block_id": query["sources"][0]["source_block_id"], "quote": SOURCE["raw_text"]}]}]}, [query], "reviewer")
    with assertion_session(AssertionSession(review)):
        safe, _ = project()
    assert "business_model" not in safe


def test_new_policy_cannot_admit_wrong_numbers_or_fact_role():
    safe, _ = project("公司2025年收入为99,999亿元。")
    # 3.1.3 retains unverified source prose for display, never as a numeric
    # fact or calculation input. Verify the actual trust boundary, not erasure.
    assert not safe["claims"]
    safe, audit = project("公司2025年收入为99,999亿元。")
    nodes = proven_content_nodes(safe, audit)
    assert nodes and all(node["validation_state"] == "partial" and not node["calculation_eligible"]
                         for node in nodes)
    assert nodes[0]["issue_code"] == "CONTENT_NUMERIC_PREMISE_UNVERIFIED"
    safe, audit = _supported_research_fields({"business_model": {
        "historical_facts": "公司是全球消费保险行业领导者。", "evidence_ids": [SOURCE["evidence_id"]]}},
        ["business_model"], {SOURCE["evidence_id"]}, {SOURCE["evidence_id"]: SOURCE}, "zh-CN")
    assert not safe["claims"]
    assert all(node["validation_state"] != "verified" and not node["calculation_eligible"]
               for node in proven_content_nodes(safe, audit))


def test_source_link_without_original_identity_cannot_create_partial_analysis():
    source = {**SOURCE, "document_id": None}
    safe, audit = _supported_research_fields({"business_model": {"analysis": TEXT, "evidence_ids": [SOURCE["evidence_id"]]}},
        ["business_model"], {SOURCE["evidence_id"]}, {SOURCE["evidence_id"]: source}, "zh-CN")
    assert not safe["claims"]
    nodes = proven_content_nodes(safe, audit)
    assert nodes and all(node["validation_state"] == "partial" and not node["calculation_eligible"]
                         for node in nodes)
    assert nodes[0]["assertion_results"][0]["source_bindings"][0]["document_id"] is None
