from copy import deepcopy

from openthesis.research import _supported_synthesis_sections, verify_agent_output
from openthesis.research_section_validation import project_supported_sections, proven_content_nodes
from openthesis.research_coverage import ResearchCoverageContract


def project(report, *, support=lambda text, refs: False, computed=None):
    return project_supported_sections(
        report, list(report), lambda claims: {
            "normalized_claims": claims,
            "claim_verifications": [
                {"index": i, "state": "insufficient_evidence"} for i in range(len(claims))
            ],
        }, support, {"filing:parent", "filing:child", "filing:opposing"},
        computed_fields=computed,
    )


def test_unlocated_prose_survives_as_pending_not_verified_or_complete():
    raw = {"business_model": {"analysis": "售后服务可能改善客户黏性，但仍需核对续约数据。"}}
    before = deepcopy(raw)
    safe, audit = project(raw)
    assert safe["business_model"]["analysis"] == raw["business_model"]["analysis"]
    nodes = proven_content_nodes(safe, audit)
    assert len(nodes) == 1
    assert nodes[0]["validation_state"] == "partial"
    assert not nodes[0]["evidence_ids"]
    assert audit["pending_paths"]
    coverage = ResearchCoverageContract().assess(
        safe, {"section_validation": audit}, evidence_records={}
    )
    item = next(row for row in coverage.obligations if row.obligation_id == "company_business_model")
    assert item.state == "generated_unverified"
    assert not coverage.complete
    assert raw == before


def test_unknown_role_keeps_own_scalars_and_independently_computed_children():
    raw = {"implied_expectations": {
        "role": "notice", "analysis": "估值仍需对折现率进行敏感度研究。",
        "implied_growth": 0.05,
    }}
    safe, audit = project(raw, computed={"implied_expectations.implied_growth": 0.05})
    assert safe["implied_expectations"]["analysis"] == raw["implied_expectations"]["analysis"]
    assert safe["implied_expectations"]["implied_growth"] == 0.05
    assert "implied_expectations.implied_growth" in audit["computed_paths"]
    assert safe["implied_expectations"]["role"] == "analysis"


def test_children_override_parent_support_and_opposition_never_becomes_support():
    seen = {}
    raw = {"growth_opportunities": [{
        "opportunity_id": "liquid-cooling-01", "time_horizon": "中长期",
        "supporting_evidence_ids": ["filing:parent"],
        "contradicting_evidence_ids": ["filing:opposing"],
        "analysis": "规模扩大可能提高售后收入。",
        "risk": {"analysis": "价格竞争可能降低回报。", "evidence_ids": ["filing:child"]},
    }]}
    def support(text, refs):
        seen[text] = set(refs)
        return False
    safe, audit = project(raw, support=support)
    item = safe["growth_opportunities"][0]
    assert item["opportunity_id"] == "liquid-cooling-01"
    assert item["time_horizon"] == "中长期"
    assert seen[raw["growth_opportunities"][0]["analysis"]] == {"filing:parent"}
    assert seen[raw["growth_opportunities"][0]["risk"]["analysis"]] == {"filing:child"}
    assert item["contradicting_evidence_ids"] == ["filing:opposing"]


def test_proven_conflict_only_removes_its_own_assertion():
    raw = {"business_model": {"analysis": "正常业务分析。", "bad": "已证实错误断言。"}}
    safe, audit = project(raw, support=lambda text, refs:
                          "contradicted" if text == "已证实错误断言。" else False)
    assert safe["business_model"]["analysis"] == "正常业务分析。"
    assert "bad" not in safe["business_model"]
    assert any(item["path"] == "business_model.bad" for item in audit["quarantined_paths"])


def test_uncompiled_numeric_claim_is_displayable_but_not_calculation_input():
    claim = {"kind": "fact", "text": "2025年营业收入为100亿元。", "evidence_ids": []}
    verification = verify_agent_output({"claims": [claim]}, set(), "zh-CN", {})
    checked = verification["claim_verifications"][0]
    assert checked["display_eligible"] is True
    assert checked["calculation_eligible"] is False
    assert verification["displayable_claims"][0]["text"] == claim["text"]
    assert verification["verified_claim_count"] == 0
    safe, audit = _supported_synthesis_sections({"business_model": {
        "summary": claim["text"], "claims": [claim]}}, set(), {}, "zh-CN")
    assert safe["business_model"]["summary"] == claim["text"]
    assert audit["pending_paths"]


def test_text_only_claim_keeps_its_body_as_pending_without_inventing_numeric_proof():
    raw = {"claims": ["售后收入可能改善客户黏性，仍需核对续约情况。"]}
    check = verify_agent_output(raw, set(), "zh-CN", {})
    assert check["displayable_claims"][0]["text"] == raw["claims"][0]
    assert check["verified_claim_count"] == 0
    assert not check["claim_verifications"][0]["calculation_eligible"]
    safe, audit = _supported_synthesis_sections({"business_model": raw}, set(), {}, "zh-CN")
    assert safe["business_model"]["claims"][0]["text"] == raw["claims"][0]
    assert audit["pending_paths"]
    from openthesis.report_document import assemble_report_document, render_html, render_markdown
    document = assemble_report_document("claim-only", [{"artifact_type": "research-report", "content": {
        "report": safe, "verification": {"section_validation": audit}}}], language="zh-CN")
    assert raw["claims"][0] in render_markdown(document)
    assert raw["claims"][0] in render_html(document)
    assert not document.readiness.complete


def test_single_reviewer_exception_and_invalid_number_do_not_erase_siblings():
    raw = {"business_model": {"analysis": "业务原文保留。", "broken": float("nan")},
           "thesis": "仍须分析资本回报的持续性。"}
    def unavailable(text, refs):
        raise RuntimeError("private reviewer details must not leak")
    safe, audit = project(raw, support=unavailable)
    assert safe["business_model"]["analysis"] == raw["business_model"]["analysis"]
    assert safe["thesis"] == raw["thesis"]
    assert "broken" not in safe["business_model"]
    assert "private reviewer" not in str(audit)


def test_pending_content_survives_storage_restart_normal_and_emergency_exports(tmp_path):
    from openthesis.demo import DEMO_COMPANY
    from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
    from openthesis.storage import Storage
    from openthesis.report_document import assemble_report_document, render_html, render_markdown
    from openthesis.safe_report import SafeReportAssembler
    store = Storage(tmp_path)
    store.save_company(DEMO_COMPANY)
    run = ResearchRun("pending-310", deepcopy(DEMO_COMPANY), "wf", "pack", "1",
                      "offline", "fixture", "2025-12-31", RunStatus.PARTIAL)
    store.save_run(run)
    text = "售后服务可能改善黏性；2025年收入100亿元尚未核验。"
    safe, audit = _supported_synthesis_sections({"business_model": {"analysis": text}}, set(), {}, "zh-CN")
    store.save_artifact(ResearchArtifact("report:pending", run.run_id, "research-report", "Research",
        {"report": safe, "verification": {"passed": False, "section_validation": audit}}))
    artifacts = Storage(tmp_path).get_artifacts(run.run_id)
    document = assemble_report_document(run.run_id, artifacts, language="zh-CN")
    assert text in render_markdown(document)
    assert text in render_html(document)
    assert "待核验" in render_markdown(document)
    assert not document.readiness.complete
    emergency = SafeReportAssembler().assemble(run_id=run.run_id, company_name="公司",
        status="partial", artifacts=artifacts, continuity={}, language="zh-CN")
    assert text in emergency.markdown and text in emergency.html
    assert "待核验" in emergency.markdown and "待核验" in emergency.html


def test_annual_operating_profit_and_capex_are_explicitly_visible_in_both_exports():
    from openthesis.report_document import assemble_report_document, render_markdown, render_html
    metrics = [{"fiscal_year": year, "fiscal_period": "FY", "period_end": f"{year}-12-31",
                "revenue": 1000000000.0, "operating_income": float(year * 1000),
                "capital_expenditure": float(year * 100)} for year in range(2021, 2026)]
    document = assemble_report_document("table-310", [{"artifact_id": "financial:310",
        "artifact_type": "deterministic-financial-summary", "content": {"metrics": metrics, "currency": "CNY"}}],
        language="zh-CN")
    money = [fact for section in document.sections for node in section.nodes for fact in node.money_facts]
    for year in range(2021, 2026):
        assert any(fact.concept == "operating_income" and fact.period == f"FY{year}" for fact in money)
        assert any(fact.concept == "capital_expenditure" and fact.period == f"FY{year}" for fact in money)
    for exported in (render_markdown(document), render_html(document)):
        assert "营业利润" in exported and "资本开支" in exported
