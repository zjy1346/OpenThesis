from openthesis.research_section_validation import project_supported_sections, is_structural_content_path
from openthesis.research_coverage import has_analysis


def test_timestamp_locators_and_host_counts_do_not_become_research_assertions():
    report = {"evidence_and_methodology": {"analysis": "We compare the supplied disclosures and retain counterevidence.",
        "as_of": "2026-10-20T00:00:00+00:00", "source_evidence_ids": ["filing:313"],
        "supporting_evidence_count": 1}}
    safe, audit = project_supported_sections(report, ["evidence_and_methodology"],
        lambda _: {}, lambda text, refs: True, {"filing:313"})
    assert safe["evidence_and_methodology"]["source_evidence_ids"] == ["filing:313"]
    assert [node["source_path"] for node in audit["nodes"]] == ["evidence_and_methodology.analysis"]
    assert all(node["validation_state"] == "verified" for node in audit["nodes"])
    assert is_structural_content_path("evidence_and_methodology.source_evidence_ids[0]")
    assert is_structural_content_path("growth_opportunities.opportunities[0].supporting_evidence_count")


def test_metadata_alone_and_forged_timestamp_cannot_fulfill_an_obligation():
    metadata = {"as_of": "2026-10-20", "source_evidence_ids": ["filing:313"], "supporting_evidence_count": 500}
    assert not has_analysis(metadata)
    safe, audit = project_supported_sections({"evidence_and_methodology": {**metadata,
        "as_of": "The company earned 999 billion.", "analysis": "Unreviewed earnings assertion."}},
        ["evidence_and_methodology"], lambda _: {}, lambda *_: False, {"filing:313"})
    assert "as_of" not in safe["evidence_and_methodology"]
    assert all(node["validation_state"] != "verified" for node in audit["nodes"])
    assert any(item["reason"] == "invalid_observation_timestamp" for item in audit["pending_paths"])
