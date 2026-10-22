from __future__ import annotations

import unittest

from openthesis.report_document import (
    _stage_section_fallbacks,
    assemble_report_document,
    render_html,
    render_markdown,
)
from openthesis.report_projection import project_report_diagnostics, project_report_value


class ReportLineageExtensions312Tests(unittest.TestCase):
    def test_growth_fallback_merges_by_identity_without_summing_generations(self) -> None:
        artifacts = [
            {
                "artifact_id": "growth-old",
                "artifact_type": "growth-opportunities",
                "content": {
                    "opportunities": [
                        {"opportunity_id": "platform", "title": "Platform", "mechanism": "Old platform mechanism."},
                        {"opportunity_id": "services", "title": "Platform", "mechanism": "Distinct service mechanism."},
                        {"title": "Cloud expansion", "mechanism": "Capacity growth."},
                    ],
                },
            },
            {
                "artifact_id": "growth-new",
                "artifact_type": "growth-opportunities",
                "content": {
                    "opportunities": [
                        {"opportunity_id": "platform", "title": "Platform", "mechanism": "Updated platform mechanism."},
                        {"title": "Cloud expansion", "mechanism": "Capacity growth."},
                    ],
                },
            },
        ]

        result = _stage_section_fallbacks(artifacts)["growth_opportunities"]
        opportunities = result["opportunities"]

        self.assertEqual(
            {item.get("opportunity_id"): item["mechanism"] for item in opportunities},
            {
                "platform": "Updated platform mechanism.",
                "services": "Distinct service mechanism.",
                None: "Capacity growth.",
            },
        )
        self.assertEqual(len(opportunities), 3)
        lineage = result["_stage_lineage"]["opportunities"]
        lineage_by_identity = {item["identity"]: item["artifact_id"] for item in lineage}
        self.assertEqual(lineage_by_identity["id:platform"], "growth-new")
        self.assertEqual(lineage_by_identity["id:services"], "growth-old")
        content_identity = next(identity for identity in lineage_by_identity if identity.startswith("content:"))
        self.assertEqual(lineage_by_identity[content_identity], "growth-new")
        self.assertEqual(artifacts[0]["content"]["opportunities"][0]["mechanism"], "Old platform mechanism.")

        document = assemble_report_document("growth-lineage-run", artifacts, language="en")
        growth_section = next(item for item in document.sections if item.section_id == "growth_opportunities")
        markdown = render_markdown(document)
        self.assertEqual(set(growth_section.source_artifact_ids), {"growth-old", "growth-new"})
        self.assertIn("Updated platform mechanism.", markdown)
        self.assertIn("Distinct service mechanism.", markdown)
        self.assertIn("Capacity growth.", markdown)
        self.assertNotIn("Old platform mechanism.", markdown)

    def test_growth_fallback_does_not_borrow_another_run(self) -> None:
        artifacts = [
            {
                "artifact_id": "growth-other-run",
                "run_id": "other-run",
                "artifact_type": "growth-opportunities",
                "content": {"opportunities": [{"opportunity_id": "old", "title": "Old", "mechanism": "Other run."}]},
            },
            {
                "artifact_id": "growth-current-run",
                "run_id": "current-run",
                "artifact_type": "growth-opportunities",
                "content": {"opportunities": [{"opportunity_id": "current", "title": "Current", "mechanism": "Current run."}]},
            },
        ]

        result = _stage_section_fallbacks(artifacts, run_id="current-run")["growth_opportunities"]
        self.assertEqual([item["opportunity_id"] for item in result["opportunities"]], ["current"])
        self.assertEqual(result["_stage_lineage"]["opportunities"][0]["artifact_id"], "growth-current-run")

    def test_failed_new_growth_generation_inherits_only_available_prior_items(self) -> None:
        artifacts = [
            {
                "artifact_id": "growth-good",
                "artifact_type": "growth-opportunities",
                "content": {"opportunities": [{"opportunity_id": "id-1", "title": "Good", "mechanism": "Usable mechanism."}]},
            },
            {
                "artifact_id": "growth-failed",
                "artifact_type": "growth-opportunities",
                "content": {"opportunities": [], "_validation": {"passed": False}},
            },
        ]

        opportunities = _stage_section_fallbacks(artifacts)["growth_opportunities"]["opportunities"]
        self.assertEqual([item["opportunity_id"] for item in opportunities], ["id-1"])
        self.assertEqual(opportunities[0]["mechanism"], "Usable mechanism.")

    def test_scenario_extensions_survive_projection_and_render_without_control_fields(self) -> None:
        raw_scenario = {
            "name": "Base case",
            "summary": "Demand remains durable.",
            "analyst_note": "Watch renewal elasticity as contracts reset.",
            "pricing_context": "Current contracts renew across the next two years.",
            "company": "CONTROL_COMPANY_SHOULD_NOT_RENDER",
            "as_of": "CONTROL_DATE_SHOULD_NOT_RENDER",
            "schema": "CONTROL_SCHEMA_SHOULD_NOT_RENDER",
            "candidate_detail": {"unstructured": "NESTED_CANDIDATE_SHOULD_NOT_RENDER"},
            "evidence_ids": ["fact:scenario", "fact:unavailable"],
        }
        projected = project_report_value(
            {"scenarios": [raw_scenario]},
            include_technical=False,
            section="scenarios",
            available_evidence={"fact:scenario"},
        )
        self.assertIn("extensions", projected["scenarios"][0])
        self.assertEqual(
            [item["text"] for item in projected["scenarios"][0]["extensions"]],
            [
                "Watch renewal elasticity as contracts reset.",
                "Current contracts renew across the next two years.",
                "NESTED_CANDIDATE_SHOULD_NOT_RENDER",
            ],
        )
        self.assertTrue(all(item["evidence_count"] == 1 for item in projected["scenarios"][0]["extensions"]))
        self.assertNotIn("company", projected["scenarios"][0])
        self.assertNotIn("candidate_detail", projected["scenarios"][0])

        document = assemble_report_document("run-scenarios-extension", [
            {
                "artifact_id": "financial",
                "artifact_type": "deterministic-financial-summary",
                "content": {"evidence": [{"evidence_id": "fact:scenario"}]},
            },
            {
                "artifact_id": "report",
                "artifact_type": "research-report",
                "content": {"report": {"scenarios": [raw_scenario]}},
            },
        ], language="en")
        markdown = render_markdown(document)
        html = render_html(document)
        for phrase in (
            "Watch renewal elasticity as contracts reset.",
            "Current contracts renew across the next two years.",
            "NESTED_CANDIDATE_SHOULD_NOT_RENDER",
        ):
            self.assertIn(phrase, markdown)
            self.assertIn(phrase, html)
        self.assertIn("Base case", markdown)
        self.assertIn("analyst note", markdown)
        self.assertIn("Scenario Context", markdown)
        self.assertIn("Valid Evidence References", markdown)
        for control in (
            "CONTROL_COMPANY_SHOULD_NOT_RENDER",
            "CONTROL_DATE_SHOULD_NOT_RENDER",
            "CONTROL_SCHEMA_SHOULD_NOT_RENDER",
        ):
            self.assertNotIn(control, markdown)
            self.assertNotIn(control, html)
        self.assertEqual(
            raw_scenario["candidate_detail"],
            {"unstructured": "NESTED_CANDIDATE_SHOULD_NOT_RENDER"},
        )

    def test_nested_scenario_extension_paths_keep_analysis_and_report_omissions(self) -> None:
        raw_scenario = {
            "name": "Base case",
            "summary": "Known summary remains unchanged.",
            "analyst_note": {
                "rationale": "Renewals support durable demand.",
                "scenario detail": [
                    "Pricing resets are staggered.",
                    {"limitation": "Customer concentration remains a risk.", "confidence_score": 0.91},
                ],
                "company": "CONTROL_COMPANY_SHOULD_NOT_RENDER",
                "api_key": "SECRET_SHOULD_NOT_RENDER",
            },
            "probability": 0.62,
            "evidence_ids": ["fact:scenario"],
        }
        raw_report = {"scenarios": [raw_scenario]}
        projected = project_report_value(
            raw_report,
            include_technical=False,
            section="scenarios",
            available_evidence={"fact:scenario"},
        )
        item = projected["scenarios"][0]

        self.assertEqual(item["summary"], "Known summary remains unchanged.")
        self.assertEqual(item["probability"], 0.62)
        self.assertEqual(
            [(extension["field"], extension["text"]) for extension in item["extensions"]],
            [
                ("analyst note › rationale", "Renewals support durable demand."),
                ("analyst note › scenario detail[0]", "Pricing resets are staggered."),
                ("analyst note › scenario detail[1] › limitation", "Customer concentration remains a risk."),
            ],
        )
        self.assertTrue(all(extension["evidence_count"] == 1 for extension in item["extensions"]))
        diagnostics = project_report_diagnostics(raw_report)
        self.assertTrue(any("confidence_score" in path for path in diagnostics))

        document = assemble_report_document("run-nested-scenario-extension", [
            {
                "artifact_id": "financial",
                "artifact_type": "deterministic-financial-summary",
                "content": {"evidence": [{"evidence_id": "fact:scenario"}]},
            },
            {
                "artifact_id": "report",
                "artifact_type": "research-report",
                "content": {"report": raw_report},
            },
        ], language="en")
        markdown = render_markdown(document)
        html = render_html(document)
        for phrase in (
            "Renewals support durable demand.",
            "Pricing resets are staggered.",
            "Customer concentration remains a risk.",
        ):
            self.assertIn(phrase, markdown)
            self.assertIn(phrase, html)
        for hidden in (
            "CONTROL_COMPANY_SHOULD_NOT_RENDER",
            "SECRET_SHOULD_NOT_RENDER",
            "0.91",
        ):
            self.assertNotIn(hidden, markdown)
            self.assertNotIn(hidden, html)

    def test_nested_risk_counterevidence_extension_is_readable_and_secret_safe(self) -> None:
        raw_counterargument = {
            "title": "Supplier concentration",
            "counterargument": "A single supplier could constrain production.",
            "risk_review": {
                "risk": "Qualification delays may extend the constraint.",
                "counterevidence": [
                    {"finding": "The latest filing describes a second qualified source."},
                    {"finding": "Management reported staged capacity additions.", "secret_token": "SECRET_TOKEN_SHOULD_NOT_RENDER"},
                    {"metric": 0.73},
                ],
                "as_of": "CONTROL_DATE_SHOULD_NOT_RENDER",
            },
            "supporting_evidence_ids": ["filing:valid"],
        }
        raw_report = {"counterarguments": [raw_counterargument]}
        projected = project_report_value(
            raw_report,
            include_technical=False,
            section="counterarguments",
            available_evidence={"filing:valid"},
        )
        item = projected["counterarguments"][0]
        self.assertEqual(item["counterargument"], "A single supplier could constrain production.")
        self.assertEqual(
            [extension["text"] for extension in item["extensions"]],
            [
                "Qualification delays may extend the constraint.",
                "The latest filing describes a second qualified source.",
                "Management reported staged capacity additions.",
            ],
        )
        self.assertTrue(all(extension["evidence_count"] == 1 for extension in item["extensions"]))
        diagnostics = project_report_diagnostics(raw_report)
        self.assertTrue(any("metric" in path for path in diagnostics))

        document = assemble_report_document("run-risk-counterevidence-extension", [
            {
                "artifact_id": "financial",
                "artifact_type": "deterministic-financial-summary",
                "content": {"evidence": [{"evidence_id": "filing:valid"}]},
            },
            {
                "artifact_id": "report",
                "artifact_type": "research-report",
                "content": {"report": raw_report},
            },
        ], language="en")
        markdown = render_markdown(document)
        html = render_html(document)
        for phrase in (
            "Qualification delays may extend the constraint.",
            "The latest filing describes a second qualified source.",
            "Management reported staged capacity additions.",
        ):
            self.assertIn(phrase, markdown)
            self.assertIn(phrase, html)
        self.assertNotIn("SECRET_TOKEN_SHOULD_NOT_RENDER", markdown)
        self.assertNotIn("SECRET_TOKEN_SHOULD_NOT_RENDER", html)
        self.assertNotIn("CONTROL_DATE_SHOULD_NOT_RENDER", markdown)
        self.assertNotIn("CONTROL_DATE_SHOULD_NOT_RENDER", html)

    def test_extension_projection_keeps_more_than_128_fields_and_long_text(self) -> None:
        notes = [f"Distinct analyst note {index}." for index in range(140)]
        long_note = "Long-form analysis " * 500
        projected = project_report_value(
            {"scenarios": [{"summary": "Base", "analyst_note": {"notes": notes, "long_note": long_note}}]},
            include_technical=False,
            section="scenarios",
        )
        extensions = projected["scenarios"][0]["extensions"]

        self.assertEqual(len(extensions), 141)
        self.assertEqual(extensions[-1]["text"], long_note.strip())
        self.assertEqual(extensions[-1]["field"], "analyst note › long note")

    def test_safe_depth_limit_emits_visible_pending_notice_and_path_diagnostic(self) -> None:
        nested = {"analysis": "Deep analysis remains in the stored candidate."}
        for index in range(70):
            nested = {f"level_{index}": nested}
        raw_report = {"scenarios": [{"summary": "Base", "analyst_note": nested}]}

        projected = project_report_value(
            raw_report,
            include_technical=False,
            section="scenarios",
        )
        extensions = projected["scenarios"][0]["extensions"]
        pending = [item for item in extensions if "pending review" in item["text"]]

        self.assertEqual(len(pending), 1)
        self.assertTrue(any("level_" in path for path in project_report_diagnostics(raw_report)))


if __name__ == "__main__":
    unittest.main()
