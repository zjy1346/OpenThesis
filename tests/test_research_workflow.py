from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, RunStatus
from openthesis.financials import NormalizedMoney
from openthesis.ot import compile_studio_draft, minimal_studio_draft
from openthesis.packs import builtin_pack, load_pack
from openthesis.markets import build_company
from openthesis.providers import ModelConfig, ProviderError
from openthesis.research import (
    ResearchCancelled,
    ResearchContext,
    ResearchWorkflow,
    _trusted_stage_result,
    _synthesis_prior_artifacts,
    _synthesis_repair_input,
    _partition_synthesis_context,
    _skeptical_prior_artifacts,
    _gate_stage_output,
    ProviderContextCapability,
    SynthesisContextLimitError,
    provider_context_capability,
    verify_agent_output,
    build_fact_evidence,
)
from openthesis.growth import normalize_growth_output
from openthesis.storage import Storage


def _valid_growth_output() -> dict[str, object]:
    return {
        "opportunities": [
            {
                "title": "Synthetic opportunity",
                "category": "product expansion",
                "mechanism": "The addressable market expands.",
                "evidence_grade": "C",
                "maturity_stage": "early",
                "time_horizon_years": 3,
                "probability_range": [0.3, 0.5],
                "supporting_evidence_ids": ["fact:808ac9481ae812762bdc728c"],
                "contradicting_evidence_ids": [],
                "scenario_eligibility": ["base"],
            }
        ]
    }


class DeterministicWorkflowTests(unittest.TestCase):
    def test_growth_lineage_records_cap_and_retained_counts(self) -> None:
        template = _valid_growth_output()["opportunities"][0]
        output = {"opportunities": [
            {**template, "opportunity_id": f"growth-{index}"}
            for index in range(1, 7)
        ]}
        normalized = normalize_growth_output(
            output,
            {"fact:808ac9481ae812762bdc728c"},
            "en",
        ).output
        lineage = normalized["_lineage"]
        self.assertEqual(lineage["raw_candidate_count"], 6)
        self.assertEqual(lineage["normalized_count"], 5)
        self.assertTrue(lineage["cap_applied"])
        self.assertEqual(lineage["cap_limit"], 5)
        self.assertEqual(lineage["rejected_count"], 1)

    def test_fact_evidence_retains_canonical_identity_and_unit_provenance(self) -> None:
        fact = {
            "fact_id": "revenue-1",
            "company_cik": "issuer-1",
            "entity": "Issuer One",
            "market": "CN_A",
            "concept": "revenue",
            "value": 123,
            "unit": "CNY",
            "unit_scale": 1000,
            "unit_provenance": "explicit",
            "scope": "consolidated",
            "consolidated_scope": "consolidated",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "form_type": "ANNUAL_REPORT",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "filed_at": "2026-03-01",
            "accession_number": "acc-1",
            "source_document": "report.pdf",
            "source_url": "https://example.test/report.pdf",
        }
        record = build_fact_evidence([fact])[0]
        for key in (
            "company_cik", "entity", "market", "scope", "consolidated_scope",
            "unit_scale", "unit_provenance", "start_date", "end_date", "filed_at",
            "form_type", "fiscal_period", "accession_number", "source_document",
        ):
            self.assertEqual(record[key], fact[key])

    def test_conflicting_same_target_citation_cannot_hide_behind_one_match(self) -> None:
        records = {
            "fact:good": {
                "kind": "financial_fact", "concept": "revenue", "value": 100,
                "unit": "CNY", "fiscal_year": 2025, "fiscal_period": "FY",
                "end_date": "2025-12-31", "scope": "consolidated",
            },
            "fact:bad": {
                "kind": "financial_fact", "concept": "revenue", "value": 900,
                "unit": "CNY", "fiscal_year": 2025, "fiscal_period": "FY",
                "end_date": "2025-12-31", "scope": "consolidated",
            },
        }
        result = verify_agent_output(
            {"claims": [{"kind": "fact", "concept": "revenue", "value": 100,
                         "unit": "CNY", "fiscal_year": 2025,
                         "fiscal_period": "FY", "end_date": "2025-12-31",
                         "scope": "consolidated",
                         "evidence_ids": list(records)}]},
            set(records), "en", records,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["claim_verifications"][0]["state"], "contradicted")

    def test_same_target_scope_or_currency_conflict_is_not_unrelated(self) -> None:
        records = {
            "fact:cny": {"kind": "financial_fact", "concept": "revenue", "value": 100,
                         "unit": "CNY", "currency": "CNY", "fiscal_year": 2025,
                         "fiscal_period": "FY", "end_date": "2025-12-31",
                         "scope": "consolidated"},
            "fact:usd": {"kind": "financial_fact", "concept": "revenue", "value": 100,
                         "unit": "USD", "currency": "USD", "fiscal_year": 2025,
                         "fiscal_period": "FY", "end_date": "2025-12-31",
                         "scope": "consolidated"},
        }
        result = verify_agent_output(
            {"claims": [{"kind": "fact", "concept": "revenue", "value": 100,
                         "unit": "CNY", "currency": "CNY", "fiscal_year": 2025,
                         "fiscal_period": "FY", "end_date": "2025-12-31",
                         "scope": "consolidated", "evidence_ids": list(records)}]},
            set(records), "en", records,
        )
        self.assertEqual(result["claim_verifications"][0]["state"], "contradicted")

    def test_incomplete_fact_or_calculation_cannot_be_numeric_verified(self) -> None:
        records = {"fact:raw": {"kind": "financial_fact", "concept": "revenue", "value": 100,
                                "unit": "CNY", "fiscal_year": 2025, "fiscal_period": "FY",
                                "end_date": "2025-12-31"}}
        fact = verify_agent_output(
            {"claims": [{"kind": "fact", "concept": "revenue", "evidence_ids": ["fact:raw"]}]},
            set(records), "en", records,
        )
        calc = verify_agent_output(
            {"claims": [{"kind": "calculation", "value": 100, "unit": "CNY",
                         "fiscal_year": 2025, "fiscal_period": "FY",
                         "evidence_ids": ["fact:raw"]}]},
            set(records), "en", records,
        )
        self.assertEqual(fact["claim_verifications"][0]["state"], "insufficient_evidence")
        self.assertEqual(calc["claim_verifications"][0]["state"], "insufficient_evidence")

    def test_semantic_reviewer_only_assists_qualitative_inference(self) -> None:
        records = {"filing:text": {"kind": "filing_text", "raw_text": "Demand remains stable."}}
        calls: list[str] = []

        def reviewer(claim: dict[str, object], _records: list[dict[str, object]]) -> str:
            calls.append(str(claim.get("kind")))
            return "entailed"

        result = verify_agent_output(
            {"claims": [{"kind": "inference", "text": "The addressable opportunity is durable.",
                         "evidence_ids": ["filing:text"]}]},
            set(records), "en", records, semantic_reviewer=reviewer,
        )
        self.assertEqual(result["claim_verifications"][0]["state"], "text_supported")
        self.assertEqual(calls, ["inference"])
        assumption = verify_agent_output(
            {"claims": [{"kind": "assumption", "text": "Demand is stable.",
                         "evidence_ids": ["filing:text"]}]},
            set(records), "en", records,
            semantic_reviewer=lambda *_: "entailed",
        )
        self.assertEqual(assumption["claim_verifications"][0]["state"], "reference_only")
        incomplete = verify_agent_output(
            {"claims": [{"kind": "fact", "concept": "revenue",
                         "text": "The addressable opportunity is durable.",
                         "evidence_ids": ["filing:text"]}]},
            set(records), "en", records, semantic_reviewer=lambda *_: "entailed",
        )
        self.assertEqual(incomplete["claim_verifications"][0]["state"], "insufficient_evidence")
        conflicting = verify_agent_output(
            {"claims": [{"kind": "fact", "concept": "revenue", "value": 200,
                         "unit": "USD", "fiscal_year": 2025, "fiscal_period": "FY",
                         "end_date": "2025-12-31", "evidence_ids": ["fact:raw"]}]},
            {"fact:raw"}, "en", {
                "fact:raw": {"kind": "financial_fact", "concept": "revenue", "value": 100,
                              "unit": "CNY", "fiscal_year": 2025, "fiscal_period": "FY",
                              "end_date": "2025-12-31"}
            }, semantic_reviewer=lambda *_: "entailed",
        )
        self.assertEqual(conflicting["claim_verifications"][0]["state"], "contradicted")

    def test_skeptical_input_contains_all_canonical_evidence_and_claim_graph(self) -> None:
        evidence = [{"evidence_id": f"fact:{index}"} for index in range(41)]
        dossier = {"trusted_channels": {"verified_facts": [{"text": "Revenue", "evidence_ids": ["fact:40"]}]}}
        context = ResearchContext(DEMO_COMPANY, [], [], [], evidence)
        payload = _skeptical_prior_artifacts(context, dossier, {})
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("fact:40", encoded)
        self.assertTrue(payload["thesis_claim_graph"])

        class CaptureProvider:
            context_window_tokens = 100_000

            def __init__(self) -> None:
                self.user_prompt = ""

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                self.user_prompt = user_prompt
                return {"claims": []}

        with tempfile.TemporaryDirectory() as directory:
            provider = CaptureProvider()
            workflow = ResearchWorkflow(
                Storage(Path(directory)), builtin_pack(), provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                report_language="en",
            )
            workflow._run_agent(
                "skeptical-analyst", "prompts/skeptical-analyst.md",
                context.compact_json(), payload,
            )
        self.assertIn("fact:40", provider.user_prompt)
        self.assertIn("canonical_evidence", provider.user_prompt)
        self.assertIn("thesis_claim_graph", provider.user_prompt)

    def test_compact_context_retains_all_evidence_and_metrics(self) -> None:
        context = ResearchContext(
            DEMO_COMPANY,
            [],
            [{"year": index} for index in range(8)],
            [],
            [{"evidence_id": f"fact:{index}"} for index in range(41)],
        )
        payload = json.loads(context.compact_json())
        self.assertEqual(len(payload["metrics"]), 8)
        self.assertEqual(len(payload["evidence"]), 41)

    def test_factual_prose_without_deterministic_fields_stays_unresolved(self) -> None:
        records = {
            "filing:text": {
                "kind": "filing_text",
                "raw_text": "营业收入同比增长20%。",
            }
        }
        result = verify_agent_output(
            {"claims": [{"kind": "fact", "text": "营业收入同比增长20%。",
                         "evidence_ids": ["filing:text"]}]},
            set(records), "zh-CN", records,
        )
        self.assertEqual(result["claim_verifications"][0]["state"], "insufficient_evidence")
        self.assertFalse(result["passed"])

    def test_trusted_stage_result_exposes_typed_channels(self) -> None:
        records = {
            "fact:revenue": {
                "kind": "financial_fact", "concept": "revenue", "value": 100,
                "unit": "CNY", "fiscal_year": 2025, "fiscal_period": "FY",
                "end_date": "2025-12-31",
            }
        }
        result = {"claims": [{"kind": "fact", "concept": "revenue", "value": 100,
                               "unit": "CNY", "fiscal_year": 2025,
                               "fiscal_period": "FY",
                               "evidence_ids": ["fact:revenue"]}]}
        verification = verify_agent_output(result, set(records), "en", records)
        trusted = _trusted_stage_result(result, verification)
        self.assertEqual(trusted["trusted_channels"]["verified_facts"][0]["concept"], "revenue")

    def test_synthesis_projection_is_bounded_and_preserves_sections_ids_and_numbers(self) -> None:
        long = "evidence:fact:revenue-2025 " + ("narrative " * 500)
        projected = _synthesis_prior_artifacts(
            {"analyses": {"financial_quality": long, "claims": [{"text": long, "evidence_ids": ["evidence:fact:revenue-2025"], "value": 42}]}},
            {"opportunities": [long]},
            {"strongest_counterarguments": [long]},
            {"scenarios": [{"name": "base", "value": 12.5}]},
        )
        encoded = json.dumps(projected, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), 32_000)
        self.assertIn("financial_quality", projected["base_analyses"])
        self.assertIn("evidence:fact:revenue-2025", encoded.decode("utf-8"))
        self.assertEqual(projected["forecast"]["scenarios"][0]["value"], 12.5)

    def test_synthesis_repair_input_targets_only_missing_sections(self) -> None:
        verification = {"issues": ["Missing required report sections: counterarguments"], "unsupported_fact_count": 0}
        synthesis = {key: "ok" for key in ("executive_summary", "business_model", "financial_quality", "balance_sheet", "competitive_position", "growth_opportunities", "scenarios", "thesis", "invalidation_conditions", "leading_indicators", "unresolved_questions", "claims")}
        repair = _synthesis_repair_input(
            synthesis, verification,
            {"financial_quality": "dossier"}, {"opportunities": ["growth"]},
            {"strongest_counterarguments": ["risk"]}, {"scenarios": ["base"]},
        )
        self.assertEqual(repair["repair_sections"], ["counterarguments"])
        self.assertNotIn("invalid_synthesis", repair)
        self.assertNotIn("growth_opportunities", repair["section_context"])
        self.assertLessEqual(len(json.dumps(repair, ensure_ascii=False).encode("utf-8")), 24_000)

    def test_synthesis_projection_preserves_extreme_width_and_depth_losslessly(self) -> None:
        deep: object = "evidence:fact:critical-id " + ("redundant prose " * 4000)
        for _ in range(12):
            deep = {"repeated": [deep] * 40, "claims": [{"evidence_ids": ["evidence:fact:critical-id"], "value": 99}]}
        projected = _synthesis_prior_artifacts(
            {"analyses": {"financial_quality": deep, "business_model": deep}},
            {"opportunities": [deep] * 40},
            {"strongest_counterarguments": [deep] * 40, "unsupported_assumptions": ["assumption"]},
            {"scenarios": [{"name": "base", "value": 12.5}] * 40},
        )
        self.assertGreater(len(json.dumps(projected, ensure_ascii=False).encode("utf-8")), 32_000)
        self.assertEqual(set(projected), {"base_analyses", "growth_opportunities", "counter_analysis", "forecast", "source_evidence_ids"})
        self.assertIn("evidence:fact:critical-id", projected["source_evidence_ids"])
        self.assertEqual(projected["forecast"]["scenarios"][0]["value"], 12.5)

    def test_staged_fallback_uses_latest_annual_metric_row_for_balance_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflow = ResearchWorkflow(
                Storage(Path(directory)),
                builtin_pack(),
                None,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                report_language="zh-Hant",
            )
            fallback = workflow._build_staged_fallback(
                {}, {}, {}, {},
                [{"year": 2024, "assets": 100, "liabilities": 40, "equity": 60}],
            )
            self.assertEqual(fallback["balance_sheet"]["year"], 2024)
            self.assertEqual(fallback["balance_sheet"]["assets"], 100)
            self.assertEqual(fallback["balance_sheet"]["liabilities"], 40)
            self.assertEqual(fallback["balance_sheet"]["equity"], 60)
            self.assertIn("資產負債表摘要", fallback["balance_sheet"]["summary"])

    def test_financial_claim_period_and_value_must_match_cited_evidence(self) -> None:
        evidence = {
            "fact:revenue-2025": {
                "evidence_id": "fact:revenue-2025",
                "kind": "financial_fact",
                "concept": "revenue",
                "value": 100.0,
                "unit": "CNY",
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "end_date": "2025-12-31",
            }
        }
        valid = verify_agent_output(
            {"claims": [{"kind": "fact", "evidence_ids": ["fact:revenue-2025"], "concept": "revenue", "value": 100.0, "unit": "CNY", "fiscal_year": 2025, "fiscal_period": "FY"}]},
            set(evidence), "en", evidence,
        )
        invalid = verify_agent_output(
            {"claims": [{"kind": "fact", "evidence_ids": ["fact:revenue-2025"], "concept": "revenue", "value": 120.0, "unit": "CNY", "fiscal_year": 2024, "fiscal_period": "FY"}]},
            set(evidence), "en", evidence,
        )

        self.assertTrue(valid["passed"])
        self.assertFalse(invalid["passed"])
        self.assertIn("period/value", invalid["issues"][0])

    def test_claim_with_opposite_direction_is_contradicted_even_with_valid_evidence_id(self) -> None:
        evidence = {
            "fact:revenue-growth": {
                "evidence_id": "fact:revenue-growth",
                "kind": "financial_fact",
                "concept": "revenue_growth",
                "value": -0.20,
                "unit": "ratio",
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "end_date": "2025-12-31",
                "raw_text": "Revenue decreased by 20% year over year.",
            }
        }
        result = verify_agent_output(
            {
                "claims": [{
                    "kind": "fact",
                    "text": "Revenue grew by 80% year over year.",
                    "evidence_ids": ["fact:revenue-growth"],
                }]
            },
            set(evidence),
            "en",
            evidence,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["claim_verifications"][0]["state"], "contradicted")

    def test_chinese_filing_text_opposite_direction_is_contradicted(self) -> None:
        evidence = {
            "filing:text:revenue": {
                "evidence_id": "filing:text:revenue",
                "kind": "filing_text",
                "raw_text": "营业收入同比下降20%。",
            }
        }
        result = verify_agent_output(
            {
                "claims": [{
                    "kind": "inference",
                    "text": "营业收入同比增长80%。",
                    "evidence_ids": ["filing:text:revenue"],
                }]
            },
            set(evidence),
            "zh-CN",
            evidence,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["claim_verifications"][0]["state"], "contradicted")

    def test_failed_stage_material_is_not_packaged_as_verified_dossier(self) -> None:
        result = {
            "claims": [{
                "kind": "fact",
                "text": "Revenue grew by 80%.",
                "evidence_ids": ["fact:missing"],
            }],
            "analysis": "unverified stage text",
        }
        verification = verify_agent_output(result, set(), "en", {})
        self.assertFalse(verification["passed"])
        self.assertEqual(verification["claim_verifications"][0]["state"], "insufficient_evidence")
        trusted = _trusted_stage_result(result, verification)
        self.assertNotIn("unverified stage text", trusted)
        self.assertEqual(trusted["_verification_state"], "failed_verification")

    def test_arbitrary_no_claims_analysis_cannot_become_verified(self) -> None:
        result = {"analysis": "营业收入与竞争力均表现良好，但没有可核验引用。"}
        verification = verify_agent_output(result, set(), "zh-CN", {})
        trusted = _trusted_stage_result(result, verification)
        self.assertEqual(trusted["_verification_state"], "failed_verification")
        self.assertNotIn("analysis", trusted)

    def test_growth_opportunity_without_supporting_evidence_is_not_verified(self) -> None:
        opportunity = {
            "title": "Unsupported opportunity",
            "mechanism": "A market may expand.",
            "supporting_evidence_ids": [],
            "contradicting_evidence_ids": [],
        }
        trusted, verification = _gate_stage_output(
            {"opportunities": [opportunity]},
            {"fact:revenue"},
            {
                "fact:revenue": {
                    "evidence_id": "fact:revenue",
                    "kind": "financial_fact",
                }
            },
            "en",
        )
        self.assertFalse(verification["passed"])
        self.assertEqual(trusted["_verification_state"], "failed_verification")
        self.assertEqual(trusted["opportunities"], [])

    def test_forecast_scenarios_use_partial_assumption_channel(self) -> None:
        trusted, verification = _gate_stage_output(
            {
                "scenarios": [
                    {
                        "name": "base",
                        "probability_range": [0.3, 0.7],
                        "assumption": "Demand remains stable.",
                    }
                ]
            },
            set(),
            {},
            "en",
        )
        self.assertFalse(verification["passed"])
        self.assertEqual(trusted["_verification_state"], "completed_partial")
        self.assertEqual(trusted["scenarios"][0]["name"], "base")

    def test_reference_only_growth_claim_is_partial_but_retained(self) -> None:
        trusted, verification = _gate_stage_output(
            {
                "opportunities": [
                    {
                        "title": "Potential expansion",
                        "claim": "A new market may expand.",
                        "supporting_evidence_ids": ["fact:revenue"],
                        "contradicting_evidence_ids": [],
                    }
                ]
            },
            {"fact:revenue"},
            {
                "fact:revenue": {
                    "evidence_id": "fact:revenue",
                    "kind": "financial_fact",
                }
            },
            "en",
        )
        self.assertFalse(verification["passed"])
        self.assertEqual(trusted["_verification_state"], "completed_partial")
        self.assertEqual(len(trusted["opportunities"]), 1)

    def test_context_budget_subtracts_output_and_repair_reserve_without_floor(self) -> None:
        capability = ProviderContextCapability(
            max_input_tokens=3_000,
            reserved_output_tokens=1_000,
            reserved_repair_tokens=1_000,
        )
        self.assertEqual(capability.max_input_bytes, 4_000)
        self.assertLess(capability.max_input_bytes, 16_384)

    def test_large_synthesis_keeps_unique_claim_and_sections_without_byte_clipping(self) -> None:
        unique_claim = {"text": "UNIQUE CLAIM RETAIN", "evidence_ids": ["fact:unique"]}
        projected = _synthesis_prior_artifacts(
            {"analyses": {"financial_quality": "x" * 100_000, "claims": [unique_claim]}},
            {"opportunities": ["y" * 100_000]},
            {"strongest_counterarguments": ["z" * 100_000]},
            {"scenarios": [{"name": "base", "value": 1.0}]},
        )
        self.assertIn("financial_quality", projected["base_analyses"])
        self.assertIn(unique_claim, projected["base_analyses"]["claims"])
        self.assertEqual(projected["forecast"]["scenarios"][0]["name"], "base")

    def test_oversized_synthesis_uses_lossless_named_sections(self) -> None:
        projected = _synthesis_prior_artifacts(
            {"analyses": {"financial_quality": ["Q" * 3_000 for _ in range(3)]}},
            {"opportunities": [{"text": "GROWTH UNIQUE"}]},
            {"strongest_counterarguments": ["RISK UNIQUE"], "notes": ["R" * 3_000 for _ in range(3)]},
            {"scenarios": [{"name": "base"}]},
        )
        sectioned = _partition_synthesis_context(
            projected, ProviderContextCapability(max_input_tokens=8_000)
        )
        self.assertGreater(len(sectioned), 1)
        self.assertEqual(
            {part["name"] for part in sectioned},
            {"base_analyses", "growth_opportunities", "counter_analysis", "forecast"},
        )
        self.assertIn("financial_quality", sectioned[0]["content"])

    def test_oversized_nested_claims_are_partitioned_without_loss(self) -> None:
        claims = [
            {"text": f"UNIQUE CLAIM {index} " + ("detail " * 60), "evidence_ids": [f"fact:{index}"]}
            for index in range(12)
        ]
        projected = _synthesis_prior_artifacts(
            {"analyses": {"financial_quality": "Q", "claims": claims}},
            {},
            {},
            {},
        )
        capability = ProviderContextCapability(
            max_input_tokens=2_600,
            reserved_output_tokens=1_000,
            reserved_repair_tokens=1_000,
        )
        parts = _partition_synthesis_context(projected, capability)
        self.assertGreater(len(parts), 1)
        serialized = json.dumps(parts, ensure_ascii=False)
        for claim in claims:
            self.assertIn(claim["text"], serialized)
        self.assertTrue(all(len(json.dumps(part, ensure_ascii=False).encode("utf-8")) <= capability.max_input_bytes for part in parts))

    def test_oversized_synthesis_rejects_before_provider_call(self) -> None:
        class RecordingProvider:
            context_window_tokens = 8_000

            def __init__(self) -> None:
                self.payload_sizes: list[int] = []

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                self.payload_sizes.append(len(user_prompt.encode("utf-8")))
                return {"claims": [{"text": "section result", "kind": "inference", "evidence_ids": []}]}

        with tempfile.TemporaryDirectory() as directory:
            provider = RecordingProvider()
            workflow = ResearchWorkflow(
                Storage(Path(directory)),
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
            )
            context = ResearchContext(DEMO_COMPANY, [], [], [], [])
            with self.assertRaises(SynthesisContextLimitError) as error:
                workflow._run_synthesis_with_budget(
                    context,
                    {"analyses": {"financial_quality": ["Q" * 2_000 for _ in range(5)]}},
                    {"opportunities": ["G" * 2_000 for _ in range(5)]},
                    {"strongest_counterarguments": ["R" * 2_000 for _ in range(5)]},
                    {"scenarios": [{"name": "base"}]},
                )
            self.assertEqual(provider.payload_sizes, [])
            self.assertGreater(error.exception.required_bytes, error.exception.available_bytes)

    def test_run_context_capacity_saves_complete_staged_fallback_without_final_call(self) -> None:
        class CapacityProvider:
            context_window_tokens = 100_000

            def __init__(self) -> None:
                self.calls: list[str] = []
                self.max_input_bytes = 100_000

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                agent = str(json.loads(user_prompt).get("agent", ""))
                self.calls.append(agent)
                if len(self.calls) == 6:
                    # Make only the final synthesis envelope too small.  All
                    # preceding stage calls retain their normal capability.
                    self.max_input_bytes = 1
                if agent == "growth-opportunity-analyst":
                    return _valid_growth_output()
                return {
                    "analysis": f"preserved {agent}",
                    "claims": [{
                        "text": f"Stage output from {agent}",
                        "kind": "inference",
                        "evidence_ids": [],
                    }],
                    "scenarios": ["A bounded scenario"] if agent == "forecast-analyst" else [],
                }

        with tempfile.TemporaryDirectory() as directory:
            provider = CapacityProvider()
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.capacity", role="primary"),
                parallel_agents=False,
            )
            run = workflow.run(DEMO_COMPANY, demo_facts())
            report = next(
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "research-report"
            )
            content = report["content"]
            self.assertEqual(content["mode"], "staged-fallback")
            self.assertEqual(provider.calls.count("research-synthesizer"), 0)
            self.assertEqual(
                content["report"]["cross_section_synthesis_status"],
                "not_completed_context_capacity",
            )
            self.assertTrue(content["report"]["research_complete"])
            self.assertGreater(
                content["report"]["context_budget"]["required_bytes"],
                content["report"]["context_budget"]["available_bytes"],
            )
            self.assertTrue(content["report"]["context_budget"]["counting_mode"])
            required = {
                "executive_summary", "business_model", "financial_quality",
                "balance_sheet", "competitive_position", "growth_opportunities",
                "counterarguments", "scenarios", "thesis", "claims",
            }
            self.assertTrue(required.issubset(content["report"]))
            self.assertTrue(content["report"]["financial_quality"])

    def test_compiled_custom_ot_executes_its_own_dependency_graph(self) -> None:
        class OtProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.assertTrue(json_mode)
                agent = str(json.loads(user_prompt).get("agent", ""))
                self.calls.append(agent)
                return {
                    "executive_summary": "Custom OT workflow output.",
                    "business_model": "Bounded by supplied evidence.",
                    "financial_quality": "Deterministic calculations remain separate.",
                    "competitive_position": "Requires additional evidence.",
                    "growth_opportunities": ["Evidence-bounded scenario"],
                    "counterarguments": ["The evidence set is synthetic."],
                    "scenarios": ["Continue monitoring."],
                    "thesis": "Custom OT thesis, not investment advice.",
                    "invalidation_conditions": ["Contradicting evidence appears."],
                    "leading_indicators": ["Evidence coverage"],
                    "unresolved_questions": ["How does broader evidence change the result?"],
                    "claims": [{
                        "text": "Further interpretation is required.",
                        "kind": "inference",
                        "confidence": 0.5,
                        "evidence_ids": [],
                    }],
                }

            @staticmethod
            def assertTrue(value: bool) -> None:
                if not value:
                    raise AssertionError("structured output must be enabled")

        raw, _ = compile_studio_draft(minimal_studio_draft())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = root / "custom.ot"
            package_path.write_bytes(raw)
            storage = Storage(root / "data")
            storage.save_company(DEMO_COMPANY)
            provider = OtProvider()
            workflow = ResearchWorkflow(
                storage,
                load_pack(package_path),
                provider,
                ModelConfig(configured_model_id="test.ot", role="primary"),
                report_language="en",
                parallel_agents=True,
            )

            run = workflow.run(DEMO_COMPANY, demo_facts())

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(provider.calls, ["company-analysis", "verification"])
            artifacts = storage.get_artifacts(run.run_id)
            ot_steps = [item for item in artifacts if item["artifact_type"] == "ot-agent-analysis"]
            self.assertEqual([item["content"]["step_id"] for item in ot_steps], provider.calls)
            report = next(item for item in artifacts if item["artifact_type"] == "research-report")
            self.assertEqual(report["content"]["mode"], "ot-workflow")
            self.assertEqual(report["content"]["workflow"]["pack_id"], "my.company-research")
            self.assertTrue(report["content"]["verification"]["passed"])
            self.assertEqual(run.research_configuration["ot_workflow"]["step_ids"], provider.calls)

    def test_growth_empty_response_retries_once_and_can_be_retried_in_isolation(self) -> None:
        class GrowthRetryProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.growth_calls = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.assertTrue(json_mode)
                agent = str(json.loads(user_prompt).get("agent", ""))
                self.calls.append(agent)
                if agent == "growth-opportunity-analyst":
                    self.growth_calls += 1
                    if self.growth_calls <= 2:
                        return {
                            "opportunities": [],
                            "structured_output_valid": False,
                            "_response_error": "empty_content",
                        }
                    return {
                        "opportunities": [
                            {
                                "title": "New product platform",
                                "category": "product expansion",
                                "mechanism": "The addressable market expands.",
                                "evidence_grade": "C",
                                "maturity_stage": "early",
                                "time_horizon_years": 3,
                                "probability_range": [0.3, 0.5],
                                "supporting_evidence_ids": ["fact:808ac9481ae812762bdc728c"],
                                "contradicting_evidence_ids": [],
                                "scenario_eligibility": ["base"],
                            }
                        ]
                    }
                return {
                    "executive_summary": "Synthetic verified research.",
                    "business_model": "Synthetic business model.",
                    "financial_quality": "Synthetic financial quality.",
                    "balance_sheet": "Synthetic balance sheet.",
                    "competitive_position": "Synthetic competitive position.",
                    "growth_opportunities": [],
                    "counterarguments": ["Synthetic counterargument."],
                    "scenarios": ["Synthetic scenario."],
                    "thesis": "Synthetic thesis.",
                    "invalidation_conditions": ["Synthetic invalidation condition."],
                    "leading_indicators": ["Synthetic leading indicator."],
                    "unresolved_questions": ["Synthetic unresolved question."],
                    "claims": [
                        {
                            "text": "The supplied data needs further interpretation.",
                            "kind": "inference",
                            "evidence_ids": [],
                        }
                    ],
                }

            @staticmethod
            def assertTrue(value: bool) -> None:
                if not value:
                    raise AssertionError("structured output must be enabled")

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = GrowthRetryProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                report_language="en",
            )

            run = workflow.run(DEMO_COMPANY, demo_facts())
            self.assertEqual(provider.growth_calls, 2, "initial run gets one bounded growth retry")
            before = {agent: provider.calls.count(agent) for agent in set(provider.calls)}

            workflow.retry_growth(
                run,
                storage.get_artifacts(run.run_id),
                demo_facts(),
            )

            self.assertEqual(provider.growth_calls, 3)
            self.assertEqual(provider.calls.count("research-synthesizer"), before["research-synthesizer"] + 1)
            for agent in (
                "financial-analyst",
                "business-analyst",
                "accounting-risk-analyst",
                "skeptical-analyst",
                "forecast-analyst",
            ):
                self.assertEqual(provider.calls.count(agent), before[agent])
            latest_growth = next(
                artifact
                for artifact in reversed(storage.get_artifacts(run.run_id))
                if artifact["artifact_type"] == "growth-opportunities"
            )
            self.assertEqual(
                latest_growth["content"]["opportunities"][0]["title"],
                "New product platform",
            )

    def test_workflow_completes_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            storage.save_facts([FinancialFact(**item) for item in facts])
            config = ModelConfig()
            workflow = ResearchWorkflow(storage, builtin_pack(), None, config)
            run = workflow.run(DEMO_COMPANY, facts)
            self.assertEqual(run.status, RunStatus.PARTIAL)
            artifacts = storage.get_artifacts(run.run_id)
            self.assertEqual(len(artifacts), 2)
            self.assertEqual(artifacts[-1]["artifact_type"], "research-report")

    def test_financial_beta_skips_standard_reverse_dcf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            company = build_company("600036.SH", "招商银行")
            storage.save_company(company)
            config = ModelConfig()
            workflow = ResearchWorkflow(storage, builtin_pack(), None, config)

            run = workflow.run(
                company,
                demo_facts(),
                valuation_inputs={"market_cap": 1_000_000_000, "discount_rate": 0.1, "terminal_growth": 0.03},
                market_snapshot={"source": "manual", "market_cap": 1_000_000_000, "currency": "CNY", "as_of": "2026-08-09"},
            )

            valuation = next(
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "deterministic-valuation"
            )
            self.assertEqual(valuation["content"]["status"], "not_applicable")
            self.assertEqual(company.industry_support, "financial_beta")

    def test_currency_mismatch_is_not_silently_valued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            company = build_company(
                "00700.HK",
                "Tencent",
                reporting_currency="CNY",
                accounting_standard="IFRS",
            )
            storage.save_company(company)
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                None,
                ModelConfig(),
            )

            run = workflow.run(
                company,
                demo_facts(),
                valuation_inputs={"market_cap": 1_000_000_000, "discount_rate": 0.1, "terminal_growth": 0.03},
                market_snapshot={"source": "manual", "market_cap": 1_000_000_000, "currency": "HKD", "as_of": "2026-08-09"},
            )

            valuation = next(
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "deterministic-valuation"
            )
            self.assertEqual(valuation["content"]["status"], "currency_mismatch")

    def test_authoritative_reverse_dcf_receives_explicit_normalized_money(self) -> None:
        directory = Path.cwd() / "tmp" / "research-dcf-typed"
        storage = Storage(directory)
        try:
            storage.save_company(DEMO_COMPANY)
            workflow = ResearchWorkflow(storage, builtin_pack(), None, ModelConfig())
            with patch("openthesis.research.reverse_dcf_analysis", return_value={"status": "ok"}) as dcf:
                workflow.run(
                    DEMO_COMPANY,
                    demo_facts(),
                    valuation_inputs={"market_cap": 1_000_000_000, "discount_rate": 0.1,
                                      "terminal_growth": 0.03, "horizon_years": 5},
                    market_snapshot={
                        "source": "verified-fixture", "market_cap": 1_000_000_000,
                        "valuation_currency": "USD", "currency": "USD",
                        "market_cap_unit_scale": 1, "market_cap_unit_provenance": "normalized",
                        "as_of": "2026-08-09",
                    },
                )
            self.assertIsInstance(dcf.call_args.args[1], NormalizedMoney)
            self.assertEqual(dcf.call_args.args[1].currency, "USD")
            self.assertEqual(dcf.call_args.args[1].normalized_value, 1_000_000_000.0)
            self.assertTrue(dcf.call_args.kwargs.get("require_typed"))
        finally:
            pass

    def test_multi_agent_workflow_with_fake_provider(self) -> None:
        class FakeProvider:
            def __init__(self) -> None:
                self.count = 0
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                with self.lock:
                    self.count += 1
                self.assertions(system_prompt, user_prompt, json_mode)
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                return {
                    "executive_summary": "Synthetic verified research.",
                    "business_model": "Synthetic business model.",
                    "financial_quality": "Synthetic financial quality.",
                    "balance_sheet": "Synthetic balance sheet.",
                    "competitive_position": "Synthetic competitive position.",
                    "growth_opportunities": ["Synthetic opportunity."],
                    "counterarguments": ["Synthetic counterargument."],
                    "scenarios": ["Synthetic scenario."],
                    "thesis": "Synthetic thesis.",
                    "invalidation_conditions": ["Synthetic invalidation condition."],
                    "leading_indicators": ["Synthetic leading indicator."],
                    "unresolved_questions": ["Synthetic unresolved question."],
                    "claims": [
                        {
                            "text": "The supplied data needs further interpretation.",
                            "kind": "inference",
                            "evidence_ids": [],
                        }
                    ],
                }

            @staticmethod
            def assertions(system_prompt: str, user_prompt: str, json_mode: bool) -> None:
                if "Never invent" not in system_prompt or "research_context" not in user_prompt:
                    raise AssertionError("Research prompts lost their evidence policy")
                if not json_mode:
                    raise AssertionError("Structured output must be enabled")

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            storage.save_facts([FinancialFact(**item) for item in facts])
            provider = FakeProvider()
            config = ModelConfig(configured_model_id="test.fake", role="primary")
            workflow = ResearchWorkflow(storage, builtin_pack(), provider, config)
            progress: list[tuple[str, int]] = []
            run = workflow.run(
                DEMO_COMPANY,
                facts,
                progress=lambda message, percent: progress.append((message, percent)),
            )
            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(provider.count, 7)
            messages = [message for message, _ in progress]
            self.assertTrue(
                any("Agent 已完成 1/3" in message for message in messages)
            )
            self.assertIn("正在合成最终长期研究报告", messages)
            self.assertEqual(progress[-1], ("研究完成", 100))
            artifacts = storage.get_artifacts(run.run_id)
            self.assertEqual(len(artifacts), 10)
            theses = storage.list_thesis_versions(DEMO_COMPANY.cik)
            self.assertEqual(len(theses), 1)

    def test_empty_final_synthesis_is_partial_and_preserves_stage_outputs(self) -> None:
        class EmptyFinalProvider:
            def __init__(self) -> None:
                self.count = 0
                self.system_prompts: list[str] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.count += 1
                self.system_prompts.append(system_prompt)
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if self.count == 7:
                    return {
                        "narrative": "",
                        "structured_output_valid": False,
                        "_response_error": "empty_content",
                    }
                # The workflow gets one bounded repair attempt (call 8), which
                # is deliberately still malformed; the explicit retry seam
                # below succeeds on call 9.
                if self.count == 9:
                    return {
                        "executive_summary": "Recovered synthesis.",
                        "business_model": "Business model.",
                        "financial_quality": "Financial quality.",
                        "balance_sheet": "Balance sheet.",
                        "competitive_position": "Competitive position.",
                        "growth_opportunities": ["Opportunity."],
                        "counterarguments": ["Counterargument."],
                        "scenarios": ["Scenario."],
                        "thesis": "Thesis.",
                        "invalidation_conditions": ["Invalidation."],
                        "leading_indicators": ["Indicator."],
                        "unresolved_questions": ["Question."],
                        "claims": [
                            {
                                "text": "Recovered inference",
                                "kind": "inference",
                                "evidence_ids": [],
                            }
                        ],
                    }
                return {
                    "analysis": f"stage {self.count}",
                    "claims": [
                        {
                            "text": "stage inference",
                            "kind": "inference",
                            "evidence_ids": [],
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = EmptyFinalProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
            )

            run = workflow.run(DEMO_COMPANY, demo_facts())

            self.assertEqual(run.status, RunStatus.PARTIAL)
            report = next(
                item
                for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "research-report"
            )
            self.assertEqual(report["content"]["mode"], "staged-fallback")
            self.assertTrue(report["content"]["retryable"])
            self.assertEqual(report["content"]["diagnostics"]["initial"]["parse_error_class"], "empty_content")
            self.assertEqual(report["content"]["diagnostics"]["repair"]["parse_error_class"], "invalid_schema")
            self.assertNotIn("prompt", report["content"]["diagnostics"])
            fallback = report["content"]["report"]
            required = {
                "executive_summary", "business_model", "financial_quality",
                "balance_sheet", "competitive_position", "growth_opportunities", "counterarguments",
                "scenarios", "thesis", "invalidation_conditions",
                "leading_indicators", "unresolved_questions", "claims",
            }
            self.assertTrue(required.issubset(fallback))
            # Claims without a cited, verified record are intentionally kept
            # out of the staged report rather than promoted as facts.
            self.assertFalse(fallback["claims"])
            self.assertNotIn("claims", fallback["business_model"])
            self.assertEqual(storage.list_thesis_versions(DEMO_COMPANY.cik), [])
            self.assertEqual(provider.count, 8, "run performs one bounded final repair call")
            repair_system_prompt = provider.system_prompts[7]
            self.assertIn("section_patches", repair_system_prompt)
            self.assertNotIn("return one complete report", repair_system_prompt.casefold())
            self.assertIn("only", repair_system_prompt.casefold())

            retried = workflow.retry_synthesis(
                run,
                storage.get_artifacts(run.run_id),
                demo_facts(),
            )
            self.assertEqual(provider.count, 9, "bounded repair plus retry must make two final-only calls")
            self.assertEqual(retried.status, RunStatus.COMPLETED)
            retried_report = next(
                item
                for item in reversed(storage.get_artifacts(run.run_id))
                if item["artifact_type"] == "research-report"
            )
            self.assertEqual(retried_report["content"]["mode"], "synthesized")
            self.assertFalse(retried_report["content"]["retryable"])

    def test_repair_provider_error_keeps_completed_stages_partial(self) -> None:
        class RepairUnavailableProvider:
            def __init__(self) -> None:
                self.count = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                self.count += 1
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if self.count == 7:
                    return {"narrative": "malformed", "structured_output_valid": False, "_response_error": "invalid_json"}
                if self.count == 8:
                    raise ProviderError("rate limited", retryable=False)
                return {"analysis": "stage", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RepairUnavailableProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
            )
            run = workflow.run(DEMO_COMPANY, demo_facts())
            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertEqual(provider.count, 8)
            report = next(item for item in storage.get_artifacts(run.run_id) if item["artifact_type"] == "research-report")
            self.assertEqual(report["content"]["diagnostics"]["parse_error_class"], "provider_error")
            self.assertEqual(report["content"]["diagnostics"]["repair"]["parse_error_class"], "provider_error")

    def test_failed_provider_persists_failed_run(self) -> None:
        class FailingProvider:
            def test_connection(self) -> str:
                return "never"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                raise RuntimeError("intentional provider failure")

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            config = ModelConfig(configured_model_id="test.failing", role="primary")
            workflow = ResearchWorkflow(
                storage, builtin_pack(), FailingProvider(), config
            )
            with self.assertRaisesRegex(RuntimeError, "intentional provider failure"):
                workflow.run(DEMO_COMPANY, facts)
            runs = storage.list_runs()
            self.assertEqual(runs[0]["status"], RunStatus.FAILED.value)

    def test_english_language_is_injected_into_every_agent(self) -> None:
        class RecordingProvider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.calls.append((system_prompt, user_prompt))
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                return {"executive_summary": "English output", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RecordingProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                report_language="en",
                ui_language="en",
            )
            progress: list[str] = []
            run = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                progress=lambda message, _percent: progress.append(message),
            )
            self.assertEqual(run.report_language, "en")
            self.assertEqual(len(provider.calls), 8)
            for system_prompt, user_prompt in provider.calls:
                self.assertIn(
                    "Write every natural-language value in English",
                    system_prompt,
                )
                payload = json.loads(user_prompt)
                self.assertEqual(payload["output_language"], "en")
                self.assertIn("English", payload["output_language_instruction"])
                self.assertIn("research_context", payload)
            self.assertTrue(any("Synthesizing" in message for message in progress))
            saved = storage.get_run(run.run_id)
            self.assertIsNotNone(saved)
            payload = json.loads(saved["payload_json"])
            self.assertEqual(payload["report_language"], "en")

    def test_traditional_language_is_injected_into_every_agent_and_persisted(self) -> None:
        class RecordingProvider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.calls.append((system_prompt, user_prompt))
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                return {"executive_summary": "繁體中文輸出", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RecordingProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                report_language="zh-Hant",
                ui_language="zh-Hant",
            )
            progress: list[str] = []
            run = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                progress=lambda message, _percent: progress.append(message),
            )
            self.assertEqual(run.report_language, "zh-Hant")
            self.assertEqual(len(provider.calls), 8)
            for system_prompt, user_prompt in provider.calls:
                self.assertIn(
                    "Write every natural-language value in Traditional Chinese",
                    system_prompt,
                )
                payload = json.loads(user_prompt)
                self.assertEqual(payload["output_language"], "zh-Hant")
                self.assertIn("Traditional Chinese", payload["output_language_instruction"])
                self.assertIn("research_context", payload)
            self.assertTrue(
                any("正在依序執行" in message or "研究完成" in message for message in progress)
            )
            saved = storage.get_run(run.run_id)
            self.assertIsNotNone(saved)
            payload = json.loads(saved["payload_json"])
            self.assertEqual(payload["report_language"], "zh-Hant")

    def test_parallel_agent_switch_controls_concurrency(self) -> None:
        class ConcurrencyProvider:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, _user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                threading.Event().wait(0.03)
                with self.lock:
                    self.active -= 1
                return {"claims": []}

        def run_with(parallel: bool) -> int:
            with tempfile.TemporaryDirectory() as directory:
                provider = ConcurrencyProvider()
                storage = Storage(Path(directory))
                storage.save_company(DEMO_COMPANY)
                storage.save_facts([FinancialFact(**item) for item in demo_facts()])
                workflow = ResearchWorkflow(
                    storage,
                    builtin_pack(),
                    provider,
                    ModelConfig(configured_model_id="test.fake", role="primary"),
                    parallel_agents=parallel,
                )
                workflow.run(DEMO_COMPANY, demo_facts())
                return provider.maximum

        self.assertEqual(run_with(False), 1)
        self.assertEqual(run_with(True), 2)

    def test_parallel_base_agent_retries_only_temporary_failure_sequentially(self) -> None:
        class RetryProvider:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.calls: dict[str, int] = {}
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                agent = str(json.loads(user_prompt)["agent"])
                with self.lock:
                    self.calls[agent] = self.calls.get(agent, 0) + 1
                    call_number = self.calls[agent]
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                threading.Event().wait(0.02)
                with self.lock:
                    self.active -= 1
                if agent == "financial-analyst" and call_number == 1:
                    raise ProviderError("temporary timeout", retryable=True)
                return {
                    "executive_summary": "Summary",
                    "business_model": "Business model",
                    "financial_quality": "Financial quality",
                    "balance_sheet": "Balance sheet",
                    "competitive_position": "Competitive position",
                    "growth_opportunities": ["Opportunity"],
                    "counterarguments": ["Counterargument"],
                    "scenarios": ["Scenario"],
                    "thesis": "Thesis",
                    "invalidation_conditions": ["Invalidation"],
                    "leading_indicators": ["Indicator"],
                    "unresolved_questions": ["Question"],
                    "claims": [
                        {
                            "text": "Supported inference",
                            "kind": "inference",
                            "confidence": 0.8,
                            "evidence_ids": [],
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RetryProvider()
            progress: list[tuple[str, int]] = []
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                parallel_agents=True,
            )

            run = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                progress=lambda message, percent: progress.append((message, percent)),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(provider.maximum, 2)
            self.assertEqual(provider.calls["financial-analyst"], 2)
            self.assertEqual(provider.calls["business-analyst"], 1)
            self.assertEqual(provider.calls["accounting-risk-analyst"], 1)
            self.assertTrue(any("单独重试" in message for message, _ in progress))

    def test_cancellation_is_persisted_without_calling_provider(self) -> None:
        class CountingProvider:
            def __init__(self) -> None:
                self.count = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.count += 1
                return {}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = CountingProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                cancel_check=lambda: True,
            )
            with self.assertRaises(ResearchCancelled) as caught:
                workflow.run(DEMO_COMPANY, demo_facts())
            self.assertTrue(caught.exception.run_id)
            self.assertEqual(provider.count, 0)
            self.assertEqual(
                storage.list_runs()[0]["status"], RunStatus.CANCELLED.value
            )


if __name__ == "__main__":
    unittest.main()
