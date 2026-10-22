from copy import deepcopy

from openthesis.research import _supported_research_fields, _presentation_stage_value, _trusted_stage_result, verify_agent_output
from openthesis.research_content_flow import parsed_paths
from openthesis.research_section_validation import is_structural_content_path


def test_root_controls_not_body_but_new_professional_content_survives():
    original = {"agent": "research-synthesizer", "as_of": "2026-10-19", "company": {"name": "Issuer"},
        "specialist_demand_analysis": {"analysis": "Demand elasticity remains an important uncertainty."},
        "business_model": {"analysis": "Useful operating analysis."}}
    before = deepcopy(original)
    safe, audit = _supported_research_fields(original, list(original), set(), {}, "en")
    assert not {"agent", "as_of", "company"} & set(safe)
    assert safe["specialist_demand_analysis"]["analysis"] == original["specialist_demand_analysis"]["analysis"]
    assert audit["system_metadata"]["agent"] == "research-synthesizer"
    assert "agent" not in _presentation_stage_value(original)
    assert all(not path.startswith(("$.agent", "$.as_of", "$.company")) for path in parsed_paths(original))
    assert original == before


def test_declared_content_envelope_and_legacy_metadata_nodes():
    original = {"content": {"specialist_analysis": "Keep this independent analysis.", "agent": "research-synthesizer"},
                "metadata": {"model_id": "configured"}, "diagnostics": {"status": "pending"}}
    safe, audit = _supported_research_fields(original, ["content", "specialist_analysis"], set(), {}, "en")
    assert safe["specialist_analysis"] == "Keep this independent analysis."
    assert "metadata" not in safe
    assert audit["response_diagnostics"]["diagnostics"] == {"status": "pending"}
    assert is_structural_content_path("company.name")
    assert is_structural_content_path("agent")
    assert not is_structural_content_path("business_model.agent_distribution_analysis")
    assert is_structural_content_path("stage.agent")


def test_wrapped_stage_controls_do_not_become_qualitative_findings():
    original = {"agent": "financial-analyst", "generated_at": "2026-10-19",
                "financial_quality": {"analysis": "Keep the operating uncertainty."}, "claims": []}
    trusted = _trusted_stage_result(original, verify_agent_output(original, set(), "en", {}), {}, "en")
    assert "agent" not in trusted
    assert trusted["financial_quality"]["analysis"] == original["financial_quality"]["analysis"]
    assert trusted["_section_validation"]["stage_system_metadata"]["agent"] == "financial-analyst"
