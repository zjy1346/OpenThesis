from __future__ import annotations

import unittest

from openthesis.report_document import (
    ReadinessView,
    ReportDocument,
    ReportNode,
    RunDigest,
    SectionBlock,
    assemble_report_document,
    render_markdown,
)


def _assembled_report(report: object, *, language: str = "en", verification: object = None):
    content = {"report": report}
    if verification is not None:
        content["verification"] = verification
    return assemble_report_document(
        "run-semantic-contract",
        [{"artifact_type": "research-report", "artifact_id": "report-semantic-1",
          "run_id": "run-semantic-contract", "content": content}],
        language=language,
    )


def _section(document: ReportDocument, section_id: str) -> SectionBlock:
    return next(section for section in document.sections if section.section_id == section_id)


class ReportContentContractTests(unittest.TestCase):
    def test_normalizer_placeholders_remain_visible_but_never_substantive_across_locales(self) -> None:
        cases = (
            ("en", "This section was not returned with verifiable content"),
            ("zh-CN", "当前研究阶段未返回可验证的此章节内容"),
            ("zh-TW", "本研究階段未返回可驗證的此章節內容"),
        )
        for language, expected in cases:
            with self.subTest(language=language):
                document = _assembled_report({}, language=language)
                section = _section(document, "executive_summary")

                self.assertTrue(section.is_visible)
                self.assertFalse(section.is_substantive)
                self.assertEqual(section.completeness, "incomplete")
                self.assertEqual(section.verification_state, "unknown")
                self.assertTrue(any(node.role == "placeholder" for node in section.nodes))
                self.assertIn(expected, render_markdown(document))

    def test_explicit_notice_and_translated_placeholder_roles_do_not_depend_on_text(self) -> None:
        variants = (
            ("en", "Pending source-backed analysis; this notice is intentionally long."),
            ("zh-CN", "本阶段尚无来源支持的分析，当前只是后续补充提示。"),
            ("zh-TW", "本階段尚無來源支持的分析，這是後續補充提示。"),
            ("en", "Contenu traduit : aucune analyse vérifiée n’a été produite à ce stade."),
        )
        for index, (language, text) in enumerate(variants):
            with self.subTest(language=language, text=text):
                document = _assembled_report({
                    "executive_summary": {
                        "role": "placeholder" if index != 1 else "notice",
                        "source_ids": [f"stage:{index}"],
                        "text": text,
                    },
                }, language=language)
                section = _section(document, "executive_summary")
                content_nodes = [node for node in section.nodes if node.text == text]

                self.assertEqual(len(content_nodes), 1)
                self.assertIn(content_nodes[0].role, {"placeholder", "notice"})
                self.assertEqual(content_nodes[0].source_ids, (f"stage:{index}",))
                self.assertEqual(content_nodes[0].validation_record.state, "unknown")
                self.assertTrue(section.is_visible)
                self.assertFalse(section.is_substantive)
                self.assertIn(text, render_markdown(document))

    def test_legacy_analysis_stays_readable_while_validation_and_completeness_are_unknown(self) -> None:
        text = "Revenue growth came from expanded product demand, while renewal trends remain uncertain."
        document = _assembled_report({"executive_summary": text})
        section = _section(document, "executive_summary")
        content_nodes = [node for node in section.nodes if node.text == text]

        self.assertEqual(len(content_nodes), 1)
        self.assertTrue(section.is_visible)
        self.assertTrue(section.is_substantive)
        self.assertEqual(section.completeness, "unknown")
        self.assertEqual(section.verification_state, "unknown")
        self.assertEqual(content_nodes[0].role, "analysis")
        self.assertEqual(content_nodes[0].validation_record.state, "unknown")
        self.assertFalse(document.readiness.complete)
        self.assertIn(text, render_markdown(document))

    def test_verified_analysis_role_requires_a_real_validation_record(self) -> None:
        content = {
            "executive_summary": {
                "role": "verified_analysis",
                "text": "The explicit role cannot create its own verification proof.",
            },
        }
        legacy = _assembled_report(content)
        legacy_section = _section(legacy, "executive_summary")
        legacy_node = next(node for node in legacy_section.nodes if node.text == content["executive_summary"]["text"])
        self.assertEqual(legacy_node.role, "analysis")
        self.assertEqual(legacy_node.validation_record.state, "unknown")
        self.assertTrue(legacy_section.is_substantive)

        claimed = _assembled_report(content, verification={"passed": True})
        claimed_section = _section(claimed, "executive_summary")
        claimed_node = next(node for node in claimed_section.nodes if node.text == content["executive_summary"]["text"])
        self.assertEqual(claimed_node.role, "analysis")
        self.assertEqual(claimed_node.validation_record.state, "unknown")

        from openthesis.research import _supported_synthesis_sections
        source = {"filing:business": {"kind": "filing_text", "evidence_id": "filing:business",
            "company_cik": "fixture-issuer", "end_date": "2025-12-31", "fiscal_year": 2025,
            "fiscal_period": "FY", "excerpt": content["executive_summary"]["text"]}}
        bound = {"executive_summary": {**content["executive_summary"], "evidence_ids": list(source)}}
        safe, proof = _supported_synthesis_sections(bound, set(source), source, "en")
        verified = _assembled_report(safe, verification={"passed": False, "section_validation": proof})
        verified_section = _section(verified, "executive_summary")
        verified_node = next(node for node in verified_section.nodes if node.text == content["executive_summary"]["text"])
        # An exact official sentence is a source quotation, not newly
        # generated reasoning. It stays verified without inventing analysis.
        self.assertEqual(verified_node.role, "source_quote")
        self.assertEqual(verified_node.validation_record.state, "verified")
        self.assertEqual(verified_node.validation_record.source_artifact_id, "report-semantic-1")

    def test_stale_complete_readiness_cannot_override_unreturned_typed_sections(self) -> None:
        document = assemble_report_document(
            "run-stale-readiness",
            [{
                "artifact_type": "research-report",
                "artifact_id": "report-stale-1",
                "content": {
                    "report": {"executive_summary": "A preserved but incomplete analysis."},
                    "verification": {"passed": True},
                    "readiness": {
                        "state": "complete",
                        "complete": True,
                        "substantive_sections": ["executive_summary"],
                        "missing_sections": [],
                        "missing_stages": [],
                        "issues": [],
                        "recovery_action": "",
                    },
                },
            }],
            language="en",
        )

        self.assertFalse(document.readiness.complete)
        self.assertNotEqual(document.readiness.state, "complete")
        self.assertIn("report_section_content_missing", document.readiness.issues)
        self.assertIn("OpenThesis Staged Research", render_markdown(document))

    def test_year_currency_unit_table_is_visible_but_has_no_substantive_fact(self) -> None:
        node = ReportNode(
            "table",
            rows=(("Fiscal year", "Currency", "Unit"), ("2025", "CNY", "million")),
            role="fact_table",
            materiality="material",
        )
        section = SectionBlock(
            section_id="financial-overview",
            title="Financial overview",
            body="| Fiscal year | Currency | Unit |\n| --- | --- | --- |\n| 2025 | CNY | million |",
            nodes=(node,),
        )
        document = ReportDocument(
            schema_version="1",
            run_id="run-year-unit-only",
            company_name="Example Co.",
            language="en",
            sections=(section,),
            claims=(),
            sources=(),
            readiness=ReadinessView(),
            delivery_diagnostics=(),
            run_digest=RunDigest("run-year-unit-only", 1, True),
        )

        self.assertTrue(section.is_visible)
        self.assertFalse(section.is_substantive)
        self.assertEqual(section.completeness, "unknown")
        self.assertIn("| 2025 | CNY | million |", render_markdown(document))


if __name__ == "__main__":
    unittest.main()
