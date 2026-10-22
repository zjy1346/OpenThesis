from copy import deepcopy

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from openthesis.report_document import assemble_report_document, render_html, render_markdown
from openthesis.report_content import read_trusted_snapshots
from openthesis.research_section_validation import project_supported_sections
from openthesis.storage import Storage


SOURCE_A = "filing:annual:<script>alert(1)</script>"
SOURCE_B = "filing:annual:page:2"
SOURCE_C = "filing:quarterly:page:4"
BODY_A = "Subscription expansion may support future growth."
BODY_B = "Retention trends may influence recurring revenue."
BODY_C = "Margin pressure may persist into the next period."


def _stored_report(tmp_path, language):
    run_id = f"verification-notes-{language}"
    store = Storage(tmp_path)
    store.save_company(DEMO_COMPANY)
    run = ResearchRun(
        run_id,
        deepcopy(DEMO_COMPANY),
        "verification-note-workflow",
        "fixture-pack",
        "1",
        "offline",
        "fixture",
        "2025-12-31",
        RunStatus.PARTIAL,
    )
    store.save_run(run)

    candidate = {
        "business_model": [
            {"analysis": BODY_A, "evidence_ids": [SOURCE_A]},
            {"summary": BODY_B, "evidence_ids": [SOURCE_B]},
        ],
        "financial_quality": [
            {"analysis": BODY_C, "evidence_ids": [SOURCE_C]},
        ],
    }
    safe, audit = project_supported_sections(
        candidate,
        list(candidate),
        lambda claims: {"normalized_claims": claims, "claim_verifications": []},
        lambda _text, _refs: False,
        {SOURCE_A, SOURCE_B, SOURCE_C},
    )
    store.save_artifact(ResearchArtifact(
        "report:verification-notes",
        run_id,
        "research-report",
        "Research report",
        {"report": safe, "verification": {"passed": False, "section_validation": audit}},
    ))
    artifacts = Storage(tmp_path).get_artifacts(run_id)
    trusted_before = read_trusted_snapshots(artifacts, run_id=run_id)
    document = assemble_report_document(run_id, artifacts, language=language, run_status="partial")
    return document, trusted_before


@pytest.mark.parametrize(
    ("language", "chapter_summary", "report_summary"),
    [
        (
            "en",
            "This chapter contains 2 items pending verification.",
            "3 items pending verification across 2 chapters.",
        ),
        (
            "zh-CN",
            "本章含 2 项待核验内容。",
            "3 项内容待核验，涉及 2 个章节。",
        ),
        (
            "zh-Hant",
            "本章含 2 項待核驗內容。",
            "3 項內容待核驗，涉及 2 個章節。",
        ),
    ],
)
def test_stored_partial_units_render_chapter_notes_and_shared_footnotes(
    tmp_path, language, chapter_summary, report_summary
):
    document, trusted_before = _stored_report(tmp_path, language)
    markdown = render_markdown(document)
    html = render_html(document)

    business = next(section for section in document.sections if section.section_id == "business_model")
    financial = next(section for section in document.sections if section.section_id == "financial_quality")
    report_summary_section = next(section for section in document.sections if section.section_id == "verification-summary")

    assert business.body.count(chapter_summary) == 1
    assert sum(node.kind == "verification_note" for node in business.nodes) == 1
    assert sum(node.kind == "verification_note" for node in financial.nodes) == 1
    assert report_summary in report_summary_section.body
    assert "Pending verification ·" not in business.body

    business_units = [node for node in business.nodes if node.role == "analysis"]
    financial_units = [node for node in financial.nodes if node.role == "analysis"]
    assert {node.text for node in business_units} == {BODY_A, BODY_B}
    assert [node.verification_note_id for node in business_units] == ["1", "1"]
    assert [(node.text, node.verification_note_id) for node in financial_units] == [(BODY_C, "2")]

    note_one = next(node for node in business.nodes if node.kind == "verification_note")
    assert note_one.source_ids == (SOURCE_A, SOURCE_B)
    assert note_one.validation_record == business_units[0].validation_record

    # Presentation-only markers and notes do not rewrite committed proof data.
    expected = {
        node["node_id"]: (node["body"], node["body_sha256"], tuple(node["evidence_ids"]))
        for node in trusted_before
    }
    for node in (*business_units, *financial_units):
        assert (node.text, node.validation_record.body_sha256, node.validation_record.evidence_ids) == expected[
            node.validation_record.node_id
        ]

    assert markdown.count(chapter_summary) == 1
    assert markdown.count("[^1]") == 3  # two unit markers and one shared definition
    assert markdown.count("[^2]") == 2  # one marker and its definition
    reason_prefix = "Reason: " if language == "en" else "原因："
    assert f"[^1]: {reason_prefix}CONTENT_PENDING_VERIFICATION" in markdown
    assert "`filing:annual:&lt;script&gt;alert(1)&lt;/script&gt;`" in markdown
    assert "[^2]:" in markdown
    assert BODY_A in markdown and BODY_B in markdown and BODY_C in markdown

    assert html.count('id="verification-note-1"') == 1
    assert html.count('id="verification-note-2"') == 1
    assert html.count('href="#verification-note-1"') == 2
    assert html.count('href="#verification-note-2"') == 1
    assert "CONTENT_PENDING_VERIFICATION" in html
    assert "filing:annual:&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "filing:annual:<script>" not in html
    assert BODY_A in html and BODY_B in html and BODY_C in html

