from copy import deepcopy
import json

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import RunStatus
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow, build_fact_evidence, verify_agent_output
from openthesis.research import _supported_synthesis_sections
from openthesis.research import _trusted_stage_result
from openthesis.storage import Storage
from openthesis.valuation_claims import enforce_valuation_consistency
from test_research_workflow import (
    _disclosure_fixture, _valid_growth_output, _valid_research_synthesis_output,
    _verified_demo_revenue_claim,
)


def test_a_matching_structured_value_does_not_verify_a_different_amount_in_prose():
    claim = _verified_demo_revenue_claim()
    claim["text"] = "Annual revenue was USD 999 billion in 2021."
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    verification = verify_agent_output({"claims": [claim]}, set(evidence), "en", evidence)
    assert verification["verified_claim_count"] == 0
    assert verification["unsupported_fact_count"] == 1


def test_material_source_values_are_exact_even_when_relative_error_is_small():
    claim = _verified_demo_revenue_claim()
    claim["value"] += 1
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    verification = verify_agent_output({"claims": [claim]}, set(evidence), "en", evidence)
    assert verification["verified_claim_count"] == 0
    assert verification["unsupported_fact_count"] == 1


def test_a_cited_different_concept_cannot_supply_the_wrong_revenue_amount():
    claim = _verified_demo_revenue_claim()
    other_id = "fact:net-income"
    claim["text"] = "Annual revenue was USD 100 in 2021."
    claim["evidence_ids"] = [*claim["evidence_ids"], other_id]
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    other = deepcopy(evidence[claim["evidence_ids"][0]])
    other.update({"evidence_id": other_id, "concept": "net_income", "value": 100})
    evidence[other_id] = other
    verification = verify_agent_output({"claims": [claim]}, set(evidence), "en", evidence)
    assert verification["verified_claim_count"] == 0
    assert verification["unsupported_fact_count"] == 1


def test_a_cited_comparator_cannot_supply_the_wrong_current_year_amount():
    claim = _verified_demo_revenue_claim()
    prior_id = "fact:prior-revenue"
    claim["text"] = "Annual revenue was USD 100 in 2021."
    claim["evidence_ids"] = [*claim["evidence_ids"], prior_id]
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    other = deepcopy(evidence[claim["evidence_ids"][0]])
    other.update({"evidence_id": prior_id, "value": 100, "fiscal_year": 2020,
                  "start_date": "2020-01-01", "end_date": "2020-12-31"})
    evidence[prior_id] = other
    verification = verify_agent_output({"claims": [claim]}, set(evidence), "en", evidence)
    assert verification["verified_claim_count"] == 0


def test_section_prose_cannot_borrow_a_correct_value_from_its_own_matching_claim():
    good = _verified_demo_revenue_claim()
    report = {"claims": [good], "business_model": {
        "analysis": "Annual revenue was USD 999 billion in 2021.",
        "evidence_ids": good["evidence_ids"], "claims": [good]},
        "thesis": {"analysis": "Verified annual revenue anchors operating scale.", "claims": [good]}}
    original = deepcopy(report)
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en")
    assert "analysis" not in safe["business_model"]
    assert safe["business_model"]["claims"][0]["text"] == good["text"]
    assert safe["thesis"]["analysis"] == report["thesis"]["analysis"]
    assert "999" not in str(safe)
    assert audit["verified_claim_count"] == 1
    assert any(item["path"] == "business_model.analysis" for item in audit["quarantined_paths"])
    assert report == original


def test_one_good_global_claim_does_not_verify_an_unrelated_section():
    good = _verified_demo_revenue_claim()
    report = {"claims": [good], "business_model": "Annual revenue was USD 999 billion in 2021."}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en")
    assert "business_model" not in safe
    assert audit["quarantined_paths"]


def test_unknown_reference_cannot_be_added_beside_an_otherwise_verified_claim():
    good = _verified_demo_revenue_claim()
    report = {"claims": [good], "business_model": {
        "analysis": good["text"], "claims": [good],
        "evidence_ids": [*good["evidence_ids"], "fact:missing"]}}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en")
    # An unknown reference is not a proof that the otherwise correct text is
    # false. 3.1.0 retains it, but cannot claim verified completion from it.
    assert safe["business_model"]["analysis"] == good["text"]
    assert audit["pending_paths"]


def test_legacy_exact_claim_text_can_be_preserved_without_invented_section_binding():
    good = _verified_demo_revenue_claim()
    report = {"claims": [good], "executive_summary": good["text"]}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, _ = _supported_synthesis_sections(report, set(evidence), evidence, "en")
    assert safe["executive_summary"] == good["text"]


def test_nonfinite_provider_content_is_rejected_without_crashing_the_projector():
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections({"business_model": float("nan")}, set(evidence), evidence, "en")
    assert safe == {"claims": []}
    assert audit["input_sha256"] is not None
    assert audit["quarantined_paths"]


def test_labelled_forecasts_and_questions_are_not_treated_as_verified_historical_values():
    good = _verified_demo_revenue_claim()
    report = {"claims": [good], "scenarios": [{
        "kind": "forecast", "name": "base", "analysis": "Revenue may reach USD 999 billion.",
        "assumptions": {"future_revenue": 999_000_000_000, "probability": 0.4},
        "evidence_ids": good["evidence_ids"]}], "unresolved_questions": [{
        "kind": "question", "question": "Can margins improve?", "evidence_ids": good["evidence_ids"]}]}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en")
    assert safe["scenarios"] == report["scenarios"]
    assert safe["unresolved_questions"] == report["unresolved_questions"]
    assert audit["verified_claim_count"] == 1
    assert audit["assumption_paths"]
    assert not audit["quarantined_paths"]


def test_a_forecast_label_cannot_hide_an_explicit_historical_fact():
    good = _verified_demo_revenue_claim()
    report = {"claims": [good], "scenarios": [{
        "kind": "forecast", "name": "base", "analysis": "Revenue may rise.",
        "historical_facts": {"analysis": "Annual revenue was USD 999 billion in 2021.", "claims": [good]},
        "evidence_ids": good["evidence_ids"]}]}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en")
    assert "analysis" not in safe["scenarios"][0]["historical_facts"]
    assert safe["scenarios"][0]["historical_facts"]["claims"][0]["text"] == good["text"]
    assert "999" not in str(safe)
    assert audit["quarantined_paths"]


def test_extended_coverage_fields_receive_the_same_evidence_bound_projection():
    good = _verified_demo_revenue_claim()
    report = {"claims": [good], "management_and_capital_allocation": {
        "analysis": "Verified revenue anchors operating scale.", "claims": [good]}}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en")
    section = safe["management_and_capital_allocation"]
    assert section["analysis"] == report["management_and_capital_allocation"]["analysis"]
    assert len(section["claims"]) == 1
    assert all(section["claims"][0][key] == value for key, value in good.items())
    assert section["claims"][0]["company_cik"] == DEMO_COMPANY.cik
    assert not audit["quarantined_paths"]


def test_engine_computed_valuation_survives_without_becoming_a_verified_historical_claim():
    good = _verified_demo_revenue_claim()
    valuation = {"status": "ok", "implied_fcf_growth": 0.05,
                 "equity_market_value": 1000.0, "base_free_cash_flow": 100.0,
                 "currency": "USD", "discount_rate": 0.1, "terminal_growth": 0.03,
                 "horizon_years": 10, "market_as_of": "2026-10-04",
                 "sensitivity": [{"fcf_growth": 0.03, "equity_value": 800.0}]}
    report, _ = enforce_valuation_consistency(
        {"claims": [good], "valuation_and_sensitivity": {}}, valuation, "en")
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en", valuation=valuation)
    assert safe["implied_expectations"] == report["implied_expectations"]
    assert safe["valuation_and_sensitivity"] == report["valuation_and_sensitivity"]
    assert audit["verified_claim_count"] == 1
    assert audit["computed_paths"]
    assert not audit["quarantined_paths"]


def test_a_model_computed_tag_cannot_promote_a_forged_valuation():
    good = _verified_demo_revenue_claim()
    valuation = {"status": "ok", "implied_fcf_growth": 0.05,
                 "equity_market_value": 1000.0, "base_free_cash_flow": 100.0, "currency": "USD"}
    report = {"claims": [good], "implied_expectations": {
        "role": "verified_analysis", "implied_growth": 0.99,
        "equity_market_value": 9999.0, "_computed": True}}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    safe, audit = _supported_synthesis_sections(report, set(evidence), evidence, "en", valuation=valuation)
    assert "implied_expectations" not in safe
    assert audit["verified_claim_count"] == 1
    assert audit["quarantined_paths"]


def test_reference_only_stage_claims_cannot_admit_wrong_historical_prose():
    good = _verified_demo_revenue_claim()
    unverified = {"text": "Further research is needed.", "kind": "inference",
                  "evidence_ids": good["evidence_ids"]}
    stage = {"analysis": "Annual revenue was USD 999 billion in 2021.", "claims": [unverified]}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    verification = verify_agent_output(stage, set(evidence), "en", evidence)
    assert verification["claim_verifications"][0]["state"] == "reference_only"
    safe = _trusted_stage_result(stage, verification, evidence, "en")
    assert "999" not in str(safe)


def test_invalid_forecast_references_do_not_escape_via_trusted_channels():
    good = _verified_demo_revenue_claim()
    bad = {"text": "Revenue may be USD 999 billion.", "kind": "forecast",
           "evidence_ids": ["fact:not-registered"]}
    stage = {"analysis": "Verified revenue anchors operating scale.", "claims": [good, bad]}
    evidence = {record["evidence_id"]: record for record in build_fact_evidence(demo_facts())}
    verification = verify_agent_output(stage, set(evidence), "en", evidence)
    safe = _trusted_stage_result(stage, verification, evidence, "en")
    assert safe["analysis"] == stage["analysis"]
    assert "not-registered" not in str(safe)
    assert "999" in str(safe)  # A forecast remains visible, not a proven fact.
    pending = [node for node in safe["_section_validation"]["nodes"]
               if "999" in str(node.get("body", ""))]
    assert pending and all(not node["calculation_eligible"] for node in pending)


def test_final_synthesis_keeps_independently_supported_sections_when_another_claim_fails(tmp_path):
    good = _verified_demo_revenue_claim()
    bad = {"text": "Invented margin is 99%.", "kind": "fact",
           "evidence_ids": ["fact:not-registered"]}
    section = {"analysis": "Verified annual revenue anchors the assessment of operating scale.",
               "evidence_ids": good["evidence_ids"], "claims": [good]}
    synthesis = _valid_research_synthesis_output()
    synthesis.update({"executive_summary": deepcopy(section), "business_model": deepcopy(section),
                      "thesis": deepcopy(section),
                      "financial_quality": {"analysis": "Invented margin is 99%.", "claims": [bad]},
                      "claims": [good, bad]})

    class Provider:
        calls = []

        def generate(self, _system, prompt, *, json_mode=True):
            agent = json.loads(prompt)["agent"]
            self.calls.append(agent)
            if agent == "growth-opportunity-analyst":
                return _valid_growth_output()
            if agent == "research-synthesizer":
                return deepcopy(synthesis)
            return {"kind": "opinion", "analysis": "Retained stage context.", "claims": []}

    storage = Storage(tmp_path)
    storage.save_company(DEMO_COMPANY)
    provider = Provider()
    workflow = ResearchWorkflow(storage, builtin_pack(), provider,
                                ModelConfig(configured_model_id="test.fake"), report_language="en")
    run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
    artifact = next(item for item in reversed(storage.get_artifacts(run.run_id))
                    if item["artifact_type"] == "research-report")
    report = artifact["content"]["report"]
    for key in ("executive_summary", "business_model", "thesis"):
        assert report[key]["analysis"] == section["analysis"]
    # Unproven is not contradicted: the receipt/body is retained as pending,
    # while readiness remains partial and no invalid reference is promoted.
    assert "Invented margin" in str(report)
    assert "fact:not-registered" not in str(report.get("claims", []))
    assert run.status == RunStatus.PARTIAL
    assert not artifact["content"]["readiness"]["complete"]
    assert provider.calls.count("research-synthesizer") == 1
    assert "research-synthesizer-repair" not in provider.calls
