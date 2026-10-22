from openthesis.research_execution.analysis_references import reference_registry, resolve_references
from openthesis.research_execution.model_output import ModelOutputContract, OutputField, repairable_fields


def test_registry_contains_paths_not_repeated_bodies_and_host_copies_current_findings():
    prior = {"base_analyses": {"financial-analyst": {"financial_quality": {
        "analysis": "Cash conversion needs close review.", "evidence_ids": ["filing:cash"]}}}}
    registry = reference_registry(prior)
    assert len(registry) == 1
    key = next(iter(registry))
    assert "Cash conversion" not in str(registry)
    original = {"analysis_refs": [{"analysis_id": key, "section": "financial_quality"}]}
    resolved = resolve_references(original, prior)
    assert resolved["financial_quality"] == prior["base_analyses"]["financial-analyst"]["financial_quality"]
    assert "financial_quality" not in original
    assert not resolved["_analysis_reference_resolution"]["pending"]
    changed = {"base_analyses": {"financial-analyst": {"financial_quality": {"analysis": "A new premise."}}}}
    assert "financial_quality" not in resolve_references(original, changed)
    assert resolve_references(original, changed)["_analysis_reference_resolution"]["pending"]


def test_reference_cannot_move_a_financial_section_to_thesis_or_bypass_source_checks():
    prior = {"financial_quality": {"analysis": "A sourced analytical statement."}}
    key = next(iter(reference_registry(prior)))
    candidate = {"structured_output_valid": True, "analysis_refs": [{"analysis_id": key, "section": "thesis"}]}
    assert not repairable_fields(candidate, ModelOutputContract("test", (OutputField("financial_quality"),)))
    result = resolve_references(candidate, prior)
    assert "thesis" not in result
    assert result["_analysis_reference_resolution"]["pending"]
