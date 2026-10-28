from __future__ import annotations

import unittest

from openthesis.application.reports import ReportReadSnapshot, ReportService
from openthesis.report_html import render_research_html
from openthesis.reporting import render_research_run


class ReportServiceSemanticsTests(unittest.TestCase):
    def test_snapshot_detaches_borrowed_artifacts_and_continuity_before_read(self):
        artifacts = [{"artifact_type": "research-report", "content": {"report": {"executive_summary": "original"}}}]
        continuity = {"gaps": ["original"]}
        snapshot = ReportReadSnapshot("run", "Issuer", "partial", artifacts, continuity, "en")
        identity = snapshot.input_generation
        artifacts[0]["content"]["report"]["executive_summary"] = "changed outside"
        continuity["gaps"].append("changed outside")
        self.assertEqual(snapshot.artifacts[0]["content"]["report"]["executive_summary"], "original")
        self.assertEqual(snapshot.continuity, {"gaps": ["original"]})
        self.assertEqual(snapshot.input_generation, identity)

    def test_custom_renderer_mutation_cannot_change_other_format_or_report_input(self):
        artifacts = [{"content": {"text": "original"}}]
        continuity = {"gaps": ["original"]}

        def markdown(_run_id, items, **kwargs):
            items[0]["content"]["text"] = "renderer mutation"
            kwargs["continuity"]["gaps"].append("renderer mutation")
            return "# Original\n\nOriginal body"

        def html(_run_id, items, **kwargs):
            self.assertEqual(items[0]["content"]["text"], "original")
            self.assertEqual(kwargs["continuity"], {"gaps": ["original"]})
            return "<p>Original body</p>"

        snapshot = ReportReadSnapshot("run", "Issuer", "partial", artifacts, continuity, "en")
        identity = snapshot.input_generation
        result = ReportService(markdown, html).read(snapshot)
        self.assertEqual(result.report_input_generation, identity)
        self.assertEqual(snapshot.artifacts, tuple(artifacts))
        self.assertEqual(snapshot.continuity, continuity)

    def test_public_read_keeps_notices_visible_without_claiming_substance(self):
        service = ReportService(render_research_run, render_research_html)
        for language in ("en", "zh-CN", "zh-TW"):
            snapshot = ReportReadSnapshot("run", "Issuer", "partial", ({
                "artifact_id": "report-1", "artifact_type": "research-report",
                "content": {"report": {}},
            },), None, language)
            with self.subTest(language=language):
                result = service.read(snapshot)
                executive = next(section for section in result.visible_sections if section["section_id"] == "executive_summary")
                self.assertTrue(executive["is_visible"])
                self.assertFalse(executive["is_substantive"])
                self.assertFalse(result.is_substantive)
                self.assertEqual(result.read_state, "diagnostic_only")
                self.assertEqual(executive["completeness"], "incomplete")
                self.assertEqual(executive["content_roles"], ["placeholder"])
                self.assertTrue(all(node["role"] == "placeholder" for node in executive["content_semantics"]))
                self.assertTrue(result.markdown)
                self.assertTrue(result.html)

    def test_real_legacy_analysis_is_preserved_as_partial_not_verified(self):
        service = ReportService(render_research_run, render_research_html)
        text = "The cash flow discussion remains available while the source coverage is incomplete."
        snapshot = ReportReadSnapshot("run", "Issuer", "completed", ({
            "artifact_id": "report-1", "artifact_type": "research-report",
            "content": {"report": {"executive_summary": text}},
        },), None, "en")
        result = service.read(snapshot)
        self.assertTrue(result.is_substantive)
        self.assertEqual(result.read_state, "partial")
        executive = next(section for section in result.visible_sections if section["section_id"] == "executive_summary")
        self.assertEqual(executive["verification_state"], "unknown")
        self.assertEqual(executive["completeness"], "unknown")
        self.assertEqual(executive["content_roles"], ["analysis"])
        self.assertTrue(all(node["validation_state"] == "unknown" for node in executive["content_semantics"]))
        self.assertIn(text, result.markdown)
        self.assertIn(text, result.html)
