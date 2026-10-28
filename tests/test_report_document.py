from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from openthesis.report_document import (
    ReportDocument,
    assemble_report_document,
    render_html,
    render_markdown,
)


def _artifacts() -> list[dict[str, object]]:
    evidence = [
        {
            "evidence_id": f"ev-{index}",
            "document_id": "filing-2025",
            "title": "2025 Annual Report",
            "source_url": "https://example.test/annual-report.pdf",
            "locator": f"page:{index + 1}/table:1/row:{index + 1}",
        }
        for index in range(45)
    ]
    return [
        {
            "artifact_type": "deterministic-financial-summary",
            "title": "Financial summary",
            "content": {
                "currency": "CNY",
                "metrics": [{"year": 2025, "revenue": 120_000_000_000}],
                "evidence": evidence,
            },
        },
        {
            "artifact_type": "research-report",
            "title": "Research report",
            "content": {
                "mode": "synthesized",
                "report": {
                    "executive_summary": "Revenue grew on the strength of higher demand.",
                    "business_model": {
                        "summary": "The company sells products through direct and distributor channels.",
                    },
                    "claims": [
                        {
                            "claim_id": "claim-revenue-2025",
                            "text": "FY2025 revenue was CNY 120 billion.",
                            "kind": "fact",
                            "company": "Example Co.",
                            "concept": "revenue",
                            "period": "FY2025",
                            "unit": "CNY",
                            "value": 120_000_000_000,
                            "evidence_ids": ["ev-0"],
                        },
                        {
                            "claim_id": "malformed-text-list",
                            "text": ["do not", "coerce", "this"],
                            "kind": "fact",
                            "evidence_ids": ["ev-1"],
                        },
                    ],
                    "private_protocol_blob": "PRIVATE-UNKNOWN-FIELD",
                },
                "verification": {"passed": True, "verified_claim_count": 1},
            },
        },
    ]


class ReportDocumentTests(unittest.TestCase):
    def test_formal_sections_use_typed_projection_and_diagnose_disallowed_fields(self) -> None:
        document = assemble_report_document(
            "run-typed-sections",
            [{
                "artifact_type": "research-report",
                "content": {"report": {
                    "business_model": {
                        "summary": "通过直营与经销渠道销售产品。",
                        "currency": "SHOULD_NOT_RENDER",
                        "possible_moats": [{
                            "kind": "cost_advantage",
                            "description": "规模降低单位成本。",
                            "durability": "high",
                        }],
                        "risks": [{"title": "客户集中", "text": "前五大客户占比较高。", "severity": "high"}],
                    },
                }},
            }],
            language="zh-CN",
        )

        section = next(item for item in document.sections if item.section_id == "business_model")
        rendered = render_markdown(document)
        self.assertIn("通过直营与经销渠道销售产品。", section.body)
        self.assertIn("规模降低单位成本。", section.body)
        self.assertIn("成本优势", section.body)
        self.assertIn("客户集中", section.body)
        self.assertNotIn("SHOULD_NOT_RENDER", rendered)
        self.assertTrue(any(
            item.code == "unknown_protocol_field"
            and item.location == "business_model.currency"
            for item in document.delivery_diagnostics
        ))

    def test_stage_payloads_survive_incomplete_synthesis_and_retry_cause_is_visible(self) -> None:
        artifacts = [
            {
                "artifact_type": "verified-research-dossier",
                "content": {
                    "verified_analyses": {
                        "business-analyst": {
                            "business_model": {"summary": "Recurring software subscriptions."}
                        }
                    }
                },
            },
            {
                "artifact_type": "growth-opportunities",
                "content": {
                    "opportunities": [],
                    "_response_error": "empty_content",
                    "_validation": {"passed": False, "issues": ["empty"]},
                },
            },
            {
                "artifact_type": "research-report",
                "content": {"mode": "staged-fallback", "report": {}},
            },
        ]

        document = assemble_report_document("run-staged", artifacts, language="en")
        markdown = render_markdown(document)

        self.assertIn("OpenThesis", markdown)
        self.assertIn("Recurring software subscriptions.", markdown)
        self.assertIn("growth-opportunity model returned no usable content", markdown)

    def test_unverified_legacy_dossier_is_not_promoted_to_visible_research(self) -> None:
        document = assemble_report_document("run-legacy-dossier", [{
            "artifact_type": "verified-research-dossier",
            "content": {"analyses": {
                "business-analyst": {"business_model": {"summary": "UNVERIFIED legacy text."}}
            }},
        }], language="en")

        markdown = render_markdown(document)
        self.assertNotIn("UNVERIFIED legacy text", markdown)
        self.assertTrue(any(item.code == "legacy_dossier_unverified" for item in document.delivery_diagnostics))

    def test_balance_sheet_drops_empty_columns_and_keeps_available_equity_data(self) -> None:
        document = assemble_report_document("run-balance-columns", [{
            "artifact_type": "deterministic-financial-summary",
            "content": {"currency": "CNY", "metrics": [{
                "year": 2025, "assets": None, "liabilities": None,
                "equity": None, "total_equity": 100,
            }]},
        }], language="en")

        balance = next(item for item in document.sections if item.section_id == "deterministic-balance-sheet")
        table = next(node for node in balance.nodes if node.kind == "table")
        self.assertEqual(table.rows[0], ("Fiscal year", "Total equity"))
        self.assertTrue(balance.is_substantive)

    def test_readable_sections_are_separate_from_overall_readiness(self) -> None:
        document = assemble_report_document("run-partial-visible", [{
            "artifact_type": "research-report",
            "artifact_id": "report-final-1",
            "content": {
                "report": {"executive_summary": "A preserved research conclusion with evidence limitations."},
                "verification": {"passed": False},
            },
        }], language="en", run_status="partial")

        section = next(item for item in document.sections if item.section_id == "executive_summary")
        self.assertTrue(section.is_substantive)
        self.assertEqual(section.verification_state, "unknown")
        self.assertTrue(section.is_visible)
        self.assertEqual(section.completeness, "unknown")
        self.assertEqual(section.source_artifact_ids, ("report-final-1",))
        self.assertFalse(document.readiness.complete)

    def test_legacy_report_node_construction_defaults_new_semantics_to_unknown(self) -> None:
        from openthesis.report_document import ReportNode

        legacy_node = ReportNode("paragraph", "Legacy persisted prose", (), 0, "", ())

        self.assertEqual(legacy_node.role, "unknown")
        self.assertEqual(legacy_node.source_ids, ())
        self.assertEqual(legacy_node.validation_record.state, "unknown")
        self.assertEqual(legacy_node.materiality, "unknown")

    def test_one_section_projection_failure_does_not_hide_other_research(self) -> None:
        from openthesis import report_document

        original = report_document._typed_section_nodes

        def fail_executive_summary(section, value, language):
            if section == "executive_summary":
                raise ValueError("malformed executive summary")
            return original(section, value, language)

        with patch(
            "openthesis.report_document._typed_section_nodes",
            side_effect=fail_executive_summary,
        ):
            document = assemble_report_document(
                "run-isolated-section-failure", _artifacts(), language="en"
            )

        executive = next(
            item for item in document.sections
            if item.section_id == "executive_summary"
        )
        business = next(
            item for item in document.sections if item.section_id == "business_model"
        )
        self.assertFalse(executive.is_substantive)
        self.assertEqual(executive.diagnostic_code, "section_projection_failed")
        self.assertTrue(business.is_substantive)
        self.assertIn("sells products", business.body)
        self.assertTrue(any(
            item.code == "section_projection_failed"
            and item.location == "executive_summary"
            for item in document.delivery_diagnostics
        ))

    def test_financial_cash_flow_gap_preserves_the_specific_reconciliation_failure(self) -> None:
        document = assemble_report_document("run-fcf-gap", [{
            "artifact_type": "deterministic-financial-summary",
            "content": {"currency": "USD", "metrics": [{
                "year": 2025, "revenue": 1000, "operating_cash_flow": 80,
                "capital_expenditure": 20, "free_cash_flow": None,
                "free_cash_flow_gap": "generation_mismatch",
                "money_metadata": {
                    "revenue": {"currency": "USD", "unit_provenance": "explicit"},
                    "operating_cash_flow": {"currency": "USD", "unit_provenance": "explicit", "generation_id": "g1"},
                    "capital_expenditure": {"currency": "USD", "unit_provenance": "explicit", "generation_id": "g2"},
                },
            }]},
        }], language="en")

        financial = next(item for item in document.sections if item.section_id == "financial-overview")
        self.assertIn("different accepted data generations", financial.body)
        self.assertNotIn("Free cash flow: $60", financial.body)

    def test_technical_growth_evidence_ids_are_opt_in(self) -> None:
        artifacts = [{
            "artifact_type": "growth-opportunities",
            "content": {
                "opportunities": [{
                    "title": "New platform",
                    "mechanism": "Expand the addressable market.",
                    "evidence_grade": "C",
                    "supporting_evidence_ids": ["fact:technical-only"],
                }]
            },
        }]

        public_markdown = render_markdown(
            assemble_report_document("run-public", artifacts, language="en")
        )
        technical_markdown = render_markdown(
            assemble_report_document(
                "run-technical", artifacts, language="en", include_technical=True
            )
        )

        self.assertNotIn("fact:technical-only", public_markdown)
        self.assertIn("fact:technical-only", technical_markdown)

    def test_assembly_is_immutable_and_keeps_claim_identity_out_of_claim_text(self) -> None:
        document = assemble_report_document(
            "run-report-document", _artifacts(), continuity={}, language="en"
        )

        self.assertIsInstance(document, ReportDocument)
        claim = next(item for item in document.claims if item.claim_id == "claim-revenue-2025")
        self.assertIs(type(claim.text), str)
        self.assertEqual(claim.text, "FY2025 revenue was CNY 120 billion.")
        self.assertEqual(claim.identity.concept, "revenue")
        self.assertEqual(claim.identity.period, "FY2025")
        self.assertEqual(claim.identity.unit, "CNY")
        self.assertEqual(claim.value, 120_000_000_000)
        self.assertEqual(claim.evidence_ids, ("ev-0",))
        with self.assertRaises(FrozenInstanceError):
            document.run_id = "mutated"  # type: ignore[misc]

    def test_unknown_protocol_and_illegal_claim_shape_are_diagnosed_and_isolated(self) -> None:
        document = assemble_report_document(
            "run-diagnostics", _artifacts(), continuity={}, language="en"
        )

        self.assertTrue(any(item.code == "unknown_protocol_field" for item in document.delivery_diagnostics))
        self.assertTrue(any(item.code == "claim_text_not_string" for item in document.delivery_diagnostics))
        malformed = next(item for item in document.claims if item.claim_id == "malformed-text-list")
        self.assertIs(type(malformed.text), str)
        self.assertEqual(malformed.text, "")
        self.assertEqual(malformed.state, "rejected")
        markdown = render_markdown(document)
        html = render_html(document)
        self.assertNotIn("PRIVATE-UNKNOWN-FIELD", markdown)
        self.assertNotIn("PRIVATE-UNKNOWN-FIELD", html)
        self.assertIn("Revenue grew on the strength of higher demand.", markdown)
        self.assertIn("Revenue grew on the strength of higher demand.", html)

    def test_sources_aggregate_by_document_but_render_every_locator(self) -> None:
        document = assemble_report_document(
            "run-sources", _artifacts(), continuity={}, language="en"
        )

        self.assertEqual(len(document.sources), 1)
        self.assertEqual(len(document.sources[0].locators), 45)
        financial = next(section for section in document.sections if section.section_id == "financial-overview")
        sources = next(section for section in document.sections if section.section_id == "evidence-sources")
        research_process = next(section for section in document.sections if section.section_id == "research-process")
        self.assertEqual(len(financial.evidence_ids), 45)
        self.assertEqual(len(sources.evidence_ids), 45)
        self.assertTrue(sources.is_visible)
        self.assertFalse(sources.is_substantive)
        self.assertTrue(all(node.role == "source" for node in sources.nodes if node.kind != "heading"))
        self.assertEqual(research_process.evidence_ids, ())
        markdown = render_markdown(document)
        html = render_html(document)
        self.assertIn("page:1/table:1/row:1", markdown)
        self.assertIn("page:45/table:1/row:45", markdown)
        self.assertIn("[2025 Annual Report (", markdown)
        self.assertIn("page:1/table:1/row:1", html)
        self.assertIn("page:45/table:1/row:45", html)
        self.assertNotIn("PRIVATE-UNKNOWN-FIELD", markdown + html)

    def test_failed_run_cannot_render_persisted_complete_readiness(self) -> None:
        artifacts = _artifacts()
        final = next(item for item in artifacts if item["artifact_type"] == "research-report")
        final["content"]["readiness"] = {
            "state": "complete",
            "complete": True,
            "substantive_sections": ["executive_summary"],
            "missing_sections": [],
            "missing_stages": [],
            "issues": [],
            "recovery_action": "",
        }

        document = assemble_report_document(
            "run-failed", artifacts, language="en", run_status="failed"
        )

        self.assertEqual(document.readiness.state, "failed")
        self.assertFalse(document.readiness.complete)
        self.assertIn("research_run_failed", document.readiness.issues)
        self.assertFalse(document.readiness.to_dict()["complete"])
        self.assertIn("Staged research", render_html(document))

    def test_incomplete_run_cannot_render_persisted_complete_readiness(self) -> None:
        artifacts = _artifacts()
        final = next(item for item in artifacts if item["artifact_type"] == "research-report")
        final["content"]["readiness"] = {
            "state": "complete", "complete": True, "issues": [],
        }

        document = assemble_report_document(
            "run-cancelled", artifacts, language="en", run_status="cancelled"
        )

        self.assertEqual(document.readiness.state, "substantive_partial")
        self.assertFalse(document.readiness.complete)
        self.assertIn("research_run_not_completed", document.readiness.issues)

    def test_financial_nodes_keep_generation_scope_and_unit_uncertainty(self) -> None:
        artifact = {
            "artifact_type": "deterministic-financial-summary",
            "content": {
                "currency": "",
                "financial_generations": {"annual-2025": "gen-2025"},
                "metrics": [{
                    "year": 2025,
                    "revenue": 123.0,
                    "assets": 500.0,
                    "liabilities": 200.0,
                    "equity": 250.0,
                    "total_equity": 300.0,
                    "money_metadata": {
                        "revenue": {
                            "currency": "CNY", "unit_provenance": "explicit",
                            "scope": "consolidated", "accession_number": "annual-2025",
                            "evidence_ids": ["fact:revenue"],
                        },
                        "assets": {
                            "currency": "", "unit_provenance": "unknown",
                            "scope": "consolidated", "accession_number": "annual-2025",
                        },
                        "liabilities": {
                            "currency": "CNY", "unit_provenance": "explicit",
                            "scope": "consolidated", "accession_number": "annual-2025",
                            "evidence_ids": ["fact:liabilities"],
                        },
                        "equity": {
                            "currency": "CNY", "unit_provenance": "explicit",
                            "scope": "parent", "accession_number": "annual-2025",
                        },
                        "total_equity": {
                            "currency": "CNY", "unit_provenance": "inherited",
                            "scope": "consolidated", "accession_number": "annual-2025",
                        },
                    },
                }],
            },
        }
        document = assemble_report_document("run-money", [artifact], language="zh-CN")
        overview = next(item for item in document.sections if item.section_id == "financial-overview")
        overview_money = next(node for node in overview.nodes if node.kind == "table")
        revenue = next(item for item in overview_money.money_facts if item.concept == "revenue")
        self.assertEqual(revenue.generation_id, "gen-2025")
        self.assertEqual(revenue.evidence_ids, ("fact:revenue",))
        self.assertEqual(revenue.unit_provenance, "explicit")

        balance = next(item for item in document.sections if item.section_id == "deterministic-balance-sheet")
        table = next(node for node in balance.nodes if node.kind == "table")
        self.assertEqual(
            {item.concept for item in table.money_facts},
            {"assets", "liabilities", "equity", "total_equity"},
        )
        self.assertIn("单位待核实", render_markdown(document))
        rendered_balance = render_markdown(document)
        self.assertLess(
            rendered_balance.index("归属于母公司权益"),
            rendered_balance.index("所有者权益合计"),
        )
        self.assertIn("¥123", rendered_balance)

    def test_claims_are_grouped_by_confidence_in_both_renderers(self) -> None:
        artifacts = _artifacts()
        final = artifacts[1]
        final["content"]["report"]["claims"] = [
            {"claim_id": "high", "text": "High confidence claim", "confidence": 0.92},
            {"claim_id": "medium", "text": "Medium confidence claim", "confidence": 0.67},
            {"claim_id": "low", "text": "Low confidence claim", "confidence": 0.3},
            {"claim_id": "unknown", "text": "Confidence missing"},
        ]
        document = assemble_report_document("run-confidence", artifacts, language="en")
        html = render_html(document)
        for tier in ("high", "medium", "low", "unknown"):
            self.assertIn(f"confidence-{tier}", html)
        self.assertLess(html.index("confidence-high"), html.index("confidence-medium"))
        self.assertLess(html.index("confidence-medium"), html.index("confidence-low"))


if __name__ == "__main__":
    unittest.main()
