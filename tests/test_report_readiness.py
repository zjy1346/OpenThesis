from openthesis.report_html import render_research_html
from openthesis.report_readiness import (
    REQUIRED_REPORT_SECTIONS,
    assess_report_readiness,
    report_readiness_notice,
)
from openthesis.reporting import render_research_run
from openthesis.service import _report_completion_state


def _complete_report():
    report = {key: f"Evidence-backed analysis for {key.replace('_', ' ')}." for key in REQUIRED_REPORT_SECTIONS}
    report["claims"] = [{"text": "Verified claim supported by source evidence."}]
    return report


def test_report_readiness_requires_substantive_sections_and_verified_claims():
    readiness = assess_report_readiness(
        _complete_report(),
        {"passed": True, "claim_count": 1, "verified_claim_count": 1, "unsupported_fact_count": 0},
    )

    assert readiness.state == "complete"
    assert readiness.complete
    assert readiness.missing_sections == ()


def test_report_readiness_does_not_count_placeholders_or_numeric_metadata_as_content():
    report = {key: "当前研究阶段未返回可验证的此章节内容。" for key in REQUIRED_REPORT_SECTIONS}
    report["financial_quality"] = {"revenue": 123, "confidence": 0.9, "evidence_ids": ["fact:123"]}
    report["claims"] = [{"text": ["a list is not a claim body"], "evidence_ids": ["fact:123"]}]

    readiness = assess_report_readiness(
        report,
        {"passed": True, "claim_count": 1, "verified_claim_count": 0, "unsupported_fact_count": 0},
    )

    assert readiness.state == "action_required"
    assert set(readiness.missing_sections) == set(REQUIRED_REPORT_SECTIONS)


def test_report_readiness_does_not_treat_unverified_claim_count_as_verification():
    readiness = assess_report_readiness(
        _complete_report(),
        {"passed": True, "claim_count": 14, "verified_claim_count": 0, "unsupported_fact_count": 0},
    )

    assert readiness.state == "substantive_partial"
    assert "no_verifiable_claims" in readiness.issues


def test_report_readiness_requires_valuation_result_or_explicit_applicability_limit():
    report = _complete_report()
    report.pop("implied_expectations")
    missing = assess_report_readiness(
        report,
        {"passed": True, "claim_count": 1, "verified_claim_count": 1, "unsupported_fact_count": 0},
    )
    assert missing.state == "substantive_partial"
    assert "implied_expectations" in missing.missing_sections

    report["implied_expectations"] = {
        "summary": "No verified same-currency equity value or positive annual FCF; reverse DCF is not applicable."
    }
    limited = assess_report_readiness(
        report,
        {"passed": True, "claim_count": 1, "verified_claim_count": 1, "unsupported_fact_count": 0},
    )
    assert limited.complete


def test_report_readiness_requires_stage_and_official_fact_evidence():
    report = _complete_report()
    readiness = assess_report_readiness(
        report,
        {"passed": True, "claim_count": 1, "verified_claim_count": 1, "unsupported_fact_count": 0},
        missing_stages=("forecast-scenarios",),
        financial_evidence_count=0,
    )

    assert readiness.state == "substantive_partial"
    assert readiness.missing_stages == ("forecast-scenarios",)
    assert "no_verified_financial_source_evidence" in readiness.issues


def test_reporters_show_partial_readiness_without_calling_it_a_complete_report():
    readiness = assess_report_readiness(
        {"executive_summary": "The run preserved only its completed summary."},
        {"passed": False, "claim_count": 0, "unsupported_fact_count": 0},
        allow_complete=False,
    ).to_dict()
    artifacts = [
        {
            "artifact_type": "research-report",
            "title": "Complete Long-term Research Report",
            "agent_id": "research-synthesizer",
            "model_id": "fixture-model",
            "content": {
                "mode": "synthesis-incomplete",
                "report": {"executive_summary": "The run preserved only its completed summary."},
                "verification": {"passed": False, "claim_count": 0, "unsupported_fact_count": 0},
                "readiness": readiness,
            },
        }
    ]

    markdown = render_research_run("run-id", artifacts, language="en")
    html = render_research_html("run-id", artifacts, language="en")

    assert "OpenThesis Staged Research" in markdown
    assert "OpenThesis Staged Research" in html
    assert "No final integrated report has been generated." in markdown
    assert "No final integrated report has been generated." in html
    assert "OpenThesis Long-term Company Research" not in markdown
    assert "OpenThesis Long-term Company Research" not in html
    assert "Some required sections" not in markdown  # status remains a plain, localized recovery message


def test_readiness_notice_is_hidden_only_for_complete_reports():
    assert report_readiness_notice({"state": "complete"}, "zh-CN") is None
    assert report_readiness_notice({"state": "action_required"}, "zh-CN")[0] == "研究尚未形成可用报告"


def test_completed_run_with_empty_required_section_is_not_marked_research_complete():
    artifacts = [
        {
            "artifact_type": "deterministic-financial-summary",
            "content": {"evidence": [{"kind": "financial_fact", "evidence_id": "fact:revenue"}]},
        },
        *[
            {"artifact_type": stage, "content": {"summary": "stage evidence retained"}}
            for stage in (
                "verified-research-dossier",
                "growth-opportunities",
                "counter-analysis",
                "forecast-scenarios",
            )
        ],
        {
            "artifact_type": "research-report",
            "content": {
                "mode": "synthesized",
                "report": {**_complete_report(), "growth_opportunities": "当前研究阶段未返回可验证的此章节内容。"},
                "verification": {"passed": True, "claim_count": 1, "verified_claim_count": 1, "unsupported_fact_count": 0},
            },
        },
    ]

    state = _report_completion_state({"status": "completed"}, artifacts, None)

    assert not state["research_complete"]
    assert state["action_required"]
    assert state["report_readiness"]["state"] == "substantive_partial"
    assert "growth_opportunities" in state["report_readiness"]["missing_sections"]

