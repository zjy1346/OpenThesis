"""Opaque renderer output stays readable but cannot assert semantic quality."""
import pytest

from openthesis.application.reports import ReportReadSnapshot, ReportService


@pytest.mark.parametrize("body", [
    "This section was not returned with verifiable content in the current research stage.",
    "研究暂未形成完整结论；请重试。",
    "Revenue increased 30%, but this custom renderer supplies no typed proof.",
])
def test_untyped_renderer_body_never_claims_substantive_content(body):
    service = ReportService(lambda *_args, **_kwargs: body,
                            lambda *_args, **_kwargs: "<p>" + body + "</p>")
    snapshot = ReportReadSnapshot("run", "issuer", "completed", (), None, "en")

    result = service.read(snapshot)

    assert result.markdown == body
    assert body in result.html
    assert result.is_substantive is False
    assert result.read_state == "diagnostic_only"
    assert len(result.visible_sections) == 1
    section = result.visible_sections[0]
    assert section["is_visible"] is True
    assert section["is_substantive"] is False
    assert section["verification_state"] == "unverified"
    assert section["source_artifact_ids"] == []
    assert result.diagnostics == ({"code": "LEGACY_RENDERER_UNVERIFIED", "location": "report"},)
