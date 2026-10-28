from openthesis.report_content import ValidationRecord
from openthesis.report_document import ReportNode, _node_markdown


def test_markdown_table_keeps_pipe_and_multiline_text_inside_the_original_cell():
    rows = (
        ("Metric", "Value"),
        ("Segment | Region", "North\r\nWest | Other"),
    )
    proof = ValidationRecord(
        state="verified", source_artifact_id="report:verified", validator="section-content-proof",
        passed=True, node_id="node:stable", body_sha256="a" * 64,
        evidence_ids=("filing:source",), generation="gen:1",
    )
    node = ReportNode("table", rows=rows, validation_record=proof)

    rendered = _node_markdown(node)

    assert rendered == (
        "| Metric | Value |\n"
        "| --- | --- |\n"
        "| Segment \\| Region | North<br>West \\| Other |"
    )
    assert len(rendered.splitlines()) == 3
    assert node.rows is rows
    assert node.rows == rows
    assert node.validation_record is proof


def test_markdown_table_without_special_cells_keeps_legacy_output_exactly():
    node = ReportNode(
        "table",
        rows=(("Metric", "Value"), ("Revenue", "$1.20B")),
    )

    assert _node_markdown(node) == (
        "| Metric | Value |\n"
        "| --- | --- |\n"
        "| Revenue | $1.20B |"
    )
