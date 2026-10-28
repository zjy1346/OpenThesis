from __future__ import annotations

import unittest
from openthesis.financial_profile import build_financial_profile
from openthesis.research_materials import route_materials
from openthesis.research_source_context import (
    repair_observations_from_canonical,
    source_context,
)


class ResearchSourceContextTests(unittest.TestCase):
    def _evidence(self, evidence_id: str, text: str, *, kind: str = "filing_text"):
        return {
            "evidence_id": evidence_id,
            "kind": kind,
            "raw_text": text,
            "source_url": "https://example.invalid/filing.pdf",
            "document_id": "doc-2024",
            "document_sha256": "doc-hash",
            "page": 12,
            "bbox": [10.0, 20.0, 30.0, 40.0],
            "company_cik": "CN_A:SZSE:000811.SZ",
            "entity": "冰轮环境",
            "end_date": "2024-12-31",
            "fiscal_year": 2024,
            "fiscal_period": "FY",
            "scope": "consolidated",
        }

    def _repair(self, fact_id: str, text: str, issue: str):
        return {
            "fact_id": fact_id,
            "evidence_id": f"fact:{fact_id}",
            "concept": "revenue",
            "accession_number": "a-2024",
            "period_end": "2024-12-31",
            "company_cik": "CN_A:SZSE:000811.SZ",
            "raw_text": text,
            "source_page": 56,
            "source_bbox": [1.0, 2.0, 3.0, 4.0],
            "source_document": "2024 annual report",
            "source_hash": "source-hash",
            "issue_codes": [issue],
            "normalization_lineage": {"raw_value": "265", "scale": "unknown"},
        }

    def test_source_context_keeps_accepted_facts_and_separate_repair_channel(self):
        accepted = [{"fact_id": "accepted-1", "concept": "assets", "value": 10}]
        repair = self._repair("candidate-1", "营业收入 265", "unit_unknown")
        result = source_context({
            "facts": accepted,
            "evidence": [self._evidence("filing:1", "公司收入保持增长")],
            "repair_observations": [repair],
            "issues": [{"code": "FINANCIAL_UNIT_UNKNOWN", "fact_id": "candidate-1"}],
        })

        self.assertEqual(result["accepted_facts"], accepted)
        self.assertEqual(result["source_blocks"][0]["source_block_id"].split(":", 1)[0], "source-block")
        self.assertEqual(result["source_blocks"][0]["bbox"], [10.0, 20.0, 30.0, 40.0])
        self.assertEqual(result["source_blocks"][0]["page"], 12)
        self.assertEqual(result["repair_observations"][0]["fact_id"], "candidate-1")
        self.assertNotIn("candidate-1", {item.get("fact_id") for item in result["accepted_facts"]})
        self.assertEqual(result["issues"][0]["code"], "FINANCIAL_UNIT_UNKNOWN")

    def test_canonical_repair_projection_excludes_clean_research_facts(self):
        current = {"fact_id": "current", "concept": "assets", "value": 10}
        candidate = self._repair("candidate", "营业收入 265", "unit_unknown")
        quarantined = self._repair("quarantined", "营业收入 265", "scope_conflict")
        canonical = {
            "research_facts": [current],
            "resolved_facts": [current, candidate],
            "quarantined_facts": [quarantined],
            "coverage": {
                "fact_validation_records": {
                    "current": {"fact_id": "current", "accepted": True, "issues": []},
                    "candidate": {"fact_id": "candidate", "accepted": False, "issues": ["unit_unknown"]},
                }
            },
        }

        observations = repair_observations_from_canonical(canonical)
        by_id = {item["fact_id"]: item for item in observations}
        self.assertNotIn("current", by_id)
        self.assertEqual(set(by_id), {"candidate", "quarantined"})
        for item in observations:
            self.assertEqual(item["allowed_uses"], ["repair_mapping", "model_analysis_labelled"])
            self.assertFalse(item["financial_admission"])
            self.assertNotEqual(item["verification_state"], "verified")
            self.assertIn("source_page", item)
            self.assertIn("issue_codes", item)

    def test_route_hides_untargeted_repair_body_but_target_retrieves_only_matching_observation(self):
        repair_a = self._repair("candidate-a", "A confidential candidate row", "unit_unknown")
        repair_b = self._repair("candidate-b", "B confidential candidate row", "scope_conflict")
        context = {
            "facts": [{"fact_id": "accepted", "concept": "assets", "value": 10}],
            "evidence": [
                {"evidence_id": "fact:accepted", "kind": "financial_fact", "concept": "assets", "value": 10},
                self._evidence("filing:1", "Official body text", kind="filing_text"),
            ],
            "repair_observations": [repair_a, repair_b],
            "issues": [{"code": "FINANCIAL_CANDIDATE_QUARANTINED"}],
        }

        catalog = route_materials(context, "business-analyst")
        self.assertEqual(catalog["evidence"], context["evidence"])
        self.assertEqual(catalog["accepted_facts"], context["facts"])
        self.assertEqual(catalog["repair_observations"], [])
        self.assertTrue(catalog["repair_retrieval"]["required"])
        self.assertEqual(
            {item["fact_id"] for item in catalog["repair_catalog"]},
            {"candidate-a", "candidate-b"},
        )
        self.assertNotIn("raw_text", catalog["repair_catalog"][0])
        self.assertNotIn("candidate-a", str(catalog["evidence_partitions"]))
        self.assertNotIn("candidate-b", str(catalog["evidence_partitions"]))

        targeted = dict(context, repair_targets=[{"fact_id": "candidate-b"}])
        routed = route_materials(targeted, "business-analyst")
        self.assertEqual([item["fact_id"] for item in routed["repair_observations"]], ["candidate-b"])
        self.assertNotIn("A confidential candidate row", str(routed["repair_observations"]))
        self.assertFalse(routed["material_adequacy"]["counts"].get("repair_observations", 0))

    def test_profile_retains_parser_observations_without_creating_repair_facts(self):
        observation = {
            "accession_number": "a-2024",
            "period_end": "2024-12-31",
            "fiscal_period": "FY",
            "source_hash": "source-hash",
        }
        profile = build_financial_profile([], parsing_observations=[observation])

        self.assertEqual(profile.parsing_observations, (observation,))
        self.assertEqual(profile.repair_observations, ())
        self.assertEqual(profile.fact_dicts, ())
        self.assertEqual(profile.metrics, ())


if __name__ == "__main__":
    unittest.main()
