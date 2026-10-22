from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from collections import Counter
from unittest.mock import patch
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, ResearchRun, RunStatus
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
    _trusted_prior_base_results,
    _trusted_prior_stage_result,
    _synthesis_prior_artifacts,
    _synthesis_repair_input,
    _partition_synthesis_context,
    _skeptical_prior_artifacts,
    _deduplicated_stage_context,
    _gate_stage_output,
    ProviderContextCapability,
    SynthesisContextLimitError,
    provider_context_capability,
    verify_agent_output,
    build_fact_evidence,
    _agent_prompt_bundle,
    _isolate_unknown_evidence_references,
)
from openthesis.growth import normalize_growth_output
from openthesis.report_readiness import REQUIRED_REPORT_SECTIONS
from openthesis.report_content import read_trusted_snapshots
from openthesis.storage import Storage


def _disclosure_fixture():
    """Actual role-material preconditions for provider/orchestration tests."""
    return [{'evidence_id': f'filing:{topic}', 'document_id': 'demo-annual',
             'source_url': 'https://example.test/annual', 'title': f'Annual · {topic}',
             'excerpt': text} for topic, text in (
                 ('business', 'The company sells equipment to industrial customers.'),
                 ('management_discussion', 'Management plans new products and capacity investment.'),
                 ('risk_factors', 'Customer concentration and refinancing expose the business to risk.'),
                 ('audit', 'Independent auditor reports an unqualified opinion on the consolidated statements.'),
             )]


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


def _verified_demo_revenue_claim() -> dict[str, object]:
    return {
        "text": "Annual revenue is recorded in the official financial data.",
        "kind": "fact",
        "concept": "revenue",
        "value": 4_200_000_000.0,
        "unit": "USD",
        "currency": "USD",
        "fiscal_year": 2021,
        "fiscal_period": "FY",
        "end_date": "2021-12-31",
        "evidence_ids": ["fact:808ac9481ae812762bdc728c"],
    }


def _valid_research_synthesis_output() -> dict[str, object]:
    return {
        "executive_summary": "Synthetic verified research summary.",
        "business_model": "Synthetic business model.",
        "financial_quality": "Synthetic financial quality.",
        "balance_sheet": "Synthetic balance sheet.",
        "competitive_position": "Synthetic competitive position.",
        "growth_opportunities": ["Synthetic opportunity."],
        "counterarguments": ["Synthetic counterargument."],
        "scenarios": ["Synthetic scenario."],
        "thesis": "Synthetic long-term thesis.",
        "invalidation_conditions": ["Synthetic invalidation condition."],
        "leading_indicators": ["Synthetic leading indicator."],
        "unresolved_questions": ["Synthetic unresolved question."],
        "implied_expectations": {"summary": "Valuation assumptions remain explicit."},
        "claims": [_verified_demo_revenue_claim()],
    }


def _assert_partial_coverage(
    testcase: unittest.TestCase,
    artifact: dict[str, object],
    *,
    expected_state: str = "substantive_partial",
) -> dict[str, object]:
    content = artifact["content"]
    testcase.assertIsInstance(content, dict)
    readiness = content["readiness"]
    testcase.assertIsInstance(readiness, dict)
    coverage = readiness.get("coverage")
    testcase.assertIsInstance(coverage, dict)
    testcase.assertFalse(readiness.get("complete"))
    testcase.assertEqual(readiness.get("state"), expected_state)
    testcase.assertFalse(coverage.get("complete"))
    gaps = coverage.get("gaps")
    testcase.assertIsInstance(gaps, list)
    testcase.assertTrue(gaps)
    # These fixtures supply neither a verified market quote nor a verified
    # deterministic valuation/sensitivity artifact. A model-written price
    # narrative cannot discharge either obligation.
    testcase.assertIn("current_price_expectations", gaps)
    testcase.assertIn("valuation_sensitivity", gaps)
    return content


def _latest_report_artifact(artifacts: list[dict[str, object]]) -> dict[str, object]:
    return next(
        item for item in reversed(artifacts)
        if item.get("artifact_type") == "research-report"
    )


def _latest_stage_checkpoints(artifacts: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for artifact in artifacts:
        if artifact.get("artifact_type") != "stage-checkpoint":
            continue
        content = artifact.get("content")
        result = content.get("result") if isinstance(content, dict) else None
        if isinstance(result, dict) and isinstance(result.get("stage_id"), str):
            latest[result["stage_id"]] = result
    return latest


def _assert_progressive_report_has_trusted_body(
    testcase: unittest.TestCase,
    artifacts: list[dict[str, object]],
    run_id: str,
) -> dict[str, object]:
    """Check local assembly against separately persisted, source-bound proofs."""
    progressive = next(
        item for item in reversed(artifacts)
        if item.get("artifact_type") == "research-report"
        and item.get("content", {}).get("mode") == "progressive-local"
    )
    snapshots = read_trusted_snapshots(artifacts, run_id=run_id)
    trusted_bodies = {
        (node.get("body_sha256"), json.dumps(node.get("body"), sort_keys=True, ensure_ascii=False))
        for node in snapshots
        if node.get("validation_state") in {"verified", "labelled"}
        and node.get("body") not in (None, "", [], {})
    }
    section_validation = progressive["content"]["verification"]["section_validation"]
    testcase.assertTrue(section_validation.get("nodes"))
    report = progressive["content"]["report"]
    rendered_bodies = [
        entry.get("body")
        for section, entries in report.items()
        if isinstance(entries, list) and section not in {"claims"}
        for entry in entries
        if isinstance(entry, dict) and "body" in entry
    ]
    matched = [
        node for node in section_validation["nodes"]
        if node.get("validation_state") in {"verified", "labelled"}
        and (node.get("body_sha256"), json.dumps(node.get("body"), sort_keys=True, ensure_ascii=False))
        in trusted_bodies
        and node.get("body") in rendered_bodies
    ]
    testcase.assertTrue(
        matched,
        "progressive report must render an independently trusted stage body; "
        f"nodes={[(node.get('section_id'), node.get('validation_state'), node.get('body_sha256'), node.get('body')) for node in section_validation['nodes']]!r}; "
        f"snapshots={[(node.get('display_section_id'), node.get('validation_state'), node.get('body_sha256'), node.get('body')) for node in snapshots]!r}; "
        f"rendered={rendered_bodies!r}",
    )
    testcase.assertFalse(report.get("research_complete"))
    testcase.assertFalse(progressive["content"]["readiness"].get("complete"))
    testcase.assertEqual(progressive["content"]["readiness"].get("state"), "partial")
    return progressive


class SevenStageProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.semantic_calls = 0

    def test_connection(self) -> str:
        return "ok"

    def generate(
        self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
    ) -> dict[str, object]:
        if not json_mode:
            raise AssertionError("structured output must be enabled")
        payload = json.loads(user_prompt)
        if "assertions" in payload:
            self.semantic_calls += 1
            return {"structured_output_valid": True, "results": [
                {"assertion_id": assertion["assertion_id"], "state": "unresolved", "source_spans": []}
                for assertion in payload["assertions"]]}
        agent = str(payload.get("agent", ""))
        self.calls.append(agent)
        if agent == "growth-opportunity-analyst":
            return _valid_growth_output()
        if agent == "research-synthesizer":
            return _valid_research_synthesis_output()
        return {"kind": "opinion", "analysis": f"Stage analysis from {agent}.", "claims": []}


class DeterministicWorkflowTests(unittest.TestCase):
    def test_official_graph_checkpoints_and_restarts_without_model_calls(self) -> None:
        Path("D:/githubmax/tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = SevenStageProvider()
            config = ModelConfig(configured_model_id="test.fake", role="primary")
            workflow = ResearchWorkflow(storage, builtin_pack(), provider, config, parallel_agents=False)

            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())

            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertEqual(len(provider.calls), 9)
            semantic_calls = provider.semantic_calls
            artifacts = storage.get_artifacts(run.run_id)
            checkpoints = [item for item in artifacts if item["artifact_type"] == "stage-checkpoint"]
            self.assertEqual(len({item["content"]["result"]["stage_id"] for item in checkpoints}), 9)
            self.assertTrue(all(
                item["content"].get("schema") == "openthesis.stage-checkpoint.v1"
                for item in checkpoints
            ))

            resumed = workflow.run(
                DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture(),
                existing_run=run, resume_artifacts=artifacts, resume_target="model-stages",
            )

            self.assertEqual(resumed.status, RunStatus.PARTIAL)
            self.assertEqual(len(provider.calls), 9, "verified checkpoints must not regenerate model stages")
            self.assertEqual(provider.semantic_calls, semantic_calls, "cached source-bound judgments must not be repeated")
            _assert_partial_coverage(self, _latest_report_artifact(storage.get_artifacts(run.run_id)))

    def test_scoped_resume_keeps_independent_stages_and_replans_changed_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            pack = builtin_pack()
            old_provider = SevenStageProvider()
            old_config = ModelConfig(configured_model_id="test.resume.old", role="primary")
            old_workflow = ResearchWorkflow(
                storage, pack, old_provider, old_config, parallel_agents=False,
            )
            run = old_workflow.run(
                DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture(),
            )
            self.assertEqual(len(old_provider.calls), 9)
            original = _latest_stage_checkpoints(storage.get_artifacts(run.run_id))
            self.assertEqual(len(original), 9)
            self.assertTrue(all(
                item["execution_fingerprint"] == old_config.public_id
                for item in original.values()
            ))

            # This fixture exercises several explicitly requested replay/model
            # changes, not the finite automatic budget of a single first pass.
            # Declare that larger test envelope up front and prove production
            # replay never changes it. Exhaustion/approval is covered separately.
            with storage.connect() as db:
                plan = json.loads(db.execute("SELECT plan_json FROM model_run_call_plans WHERE run_id=?",
                    (run.run_id,)).fetchone()[0])
                plan.update(max_calls=50, max_input_estimate=20_000_000,
                    max_output_tokens=4_000_000, verification_batches=30)
                db.execute("UPDATE model_run_call_plans SET plan_json=? WHERE run_id=?", (json.dumps(plan), run.run_id))
            replay_plan_digest = storage.model_call_plan(run.run_id)["plan_digest"]

            current_provider = SevenStageProvider()
            current_config = ModelConfig(configured_model_id="test.resume.current", role="primary")
            current_workflow = ResearchWorkflow(
                storage, pack, current_provider, current_config, parallel_agents=False,
            )
            final_resume = current_workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                filing_evidence=_disclosure_fixture(),
                existing_run=run,
                resume_artifacts=storage.get_artifacts(run.run_id),
                resume_target="final-synthesis",
            )
            self.assertEqual(current_provider.calls, ["research-synthesizer"])
            after_final = _latest_stage_checkpoints(storage.get_artifacts(run.run_id))
            self.assertEqual(set(after_final), set(original))
            for stage_id in set(original) - {"final-synthesis"}:
                self.assertEqual(
                    after_final[stage_id]["execution_fingerprint"],
                    old_config.public_id,
                )
                self.assertEqual(
                    after_final[stage_id]["candidate_digest"],
                    original[stage_id]["candidate_digest"],
                )
                self.assertEqual(
                    after_final[stage_id]["input_digest"],
                    original[stage_id]["input_digest"],
                )
            self.assertEqual(
                after_final["final-synthesis"]["execution_fingerprint"],
                current_config.public_id,
            )

            current_provider.calls.clear()
            growth_resume = current_workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                filing_evidence=_disclosure_fixture(),
                existing_run=final_resume,
                resume_artifacts=storage.get_artifacts(run.run_id),
                resume_target="growth",
            )
            downstream = [
                "growth-opportunity-analyst", "skeptical-analyst",
                "forecast-analyst",
            ]
            self.assertCountEqual(current_provider.calls, downstream)
            # The fake producer returns identical findings; the final model's
            # physical prompt is therefore unchanged and must reuse its body.
            with storage.connect() as db:
                reused = db.execute("""SELECT r.input_digest,j.input_digest,j.configuration_fingerprint
                    FROM model_result_reuse r JOIN model_request_journal j ON j.request_id=r.source_request_id
                    WHERE r.run_id=? AND r.stage_id='final-synthesis'""", (run.run_id,)).fetchone()
            self.assertIsNotNone(reused)
            self.assertEqual(reused[0], reused[1])
            self.assertEqual(reused[2], current_config.public_id)
            after_growth = _latest_stage_checkpoints(storage.get_artifacts(run.run_id))
            self.assertEqual(after_growth["final-synthesis"]["candidate_digest"], after_final["final-synthesis"]["candidate_digest"])
            self.assertEqual(storage.model_call_plan(run.run_id)["plan_digest"], replay_plan_digest)
            base_stage_ids = {
                "financial-analyst", "business-analyst", "accounting-risk-analyst",
                "source-industry-analysis", "source-risk-analysis",
            }
            for stage_id in base_stage_ids:
                self.assertEqual(
                    after_growth[stage_id]["execution_fingerprint"],
                    old_config.public_id,
                )
                self.assertEqual(
                    after_growth[stage_id]["candidate_digest"],
                    original[stage_id]["candidate_digest"],
                )
            for stage_id in set(after_growth) - base_stage_ids:
                self.assertEqual(
                    after_growth[stage_id]["execution_fingerprint"],
                    current_config.public_id,
                )

            changed_evidence = [
                *_disclosure_fixture(),
                {
                    "evidence_id": "filing:additional-market-risk",
                    "document_id": "demo-annual",
                    "source_url": "https://example.test/annual",
                    "title": "Additional market risk",
                    "excerpt": "The company faces additional market competition.",
                },
            ]
            changed_provider = SevenStageProvider()
            changed_config = ModelConfig(configured_model_id="test.resume.changed", role="primary")
            changed_workflow = ResearchWorkflow(
                storage, pack, changed_provider, changed_config, parallel_agents=False,
            )
            changed_resume = changed_workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                filing_evidence=changed_evidence,
                existing_run=growth_resume,
                resume_artifacts=storage.get_artifacts(run.run_id),
                resume_target="final-synthesis",
            )
            self.assertEqual(len(changed_provider.calls), 9)
            after_material_change = _latest_stage_checkpoints(storage.get_artifacts(run.run_id))
            self.assertTrue(all(
                item["execution_fingerprint"] == changed_config.public_id
                for item in after_material_change.values()
            ))
            self.assertTrue(all(
                after_material_change[stage_id]["input_digest"]
                != after_growth[stage_id]["input_digest"]
                for stage_id in after_growth
            ))
            self.assertEqual(storage.model_call_plan(run.run_id)["plan_digest"], replay_plan_digest)

    def test_interrupted_candidate_is_revalidated_without_recalling_its_producer(self) -> None:
        class SimulatedProcessCrash(BaseException):
            pass

        Path("D:/githubmax/tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = SevenStageProvider()
            config = ModelConfig(configured_model_id="test.fake", role="primary")
            pack = builtin_pack()
            run = ResearchRun(
                run_id="candidate-resume-run", company=DEMO_COMPANY,
                workflow_id="complete-fundamental-research", research_pack_id=pack.pack_id,
                research_pack_version=pack.version, provider_id=config.provider,
                model_id=config.model, data_as_of="2026-01-01", status=RunStatus.RUNNING,
            )

            def interrupt_after_candidate(agent_id: str, state: str) -> None:
                if agent_id == "financial-analyst" and state == "validating":
                    raise SimulatedProcessCrash()

            interrupted = ResearchWorkflow(
                storage, pack, provider, config, parallel_agents=False,
                agent_progress=interrupt_after_candidate,
            )
            with self.assertRaises(SimulatedProcessCrash):
                interrupted.run(
                    DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture(),
                    existing_run=run,
                )

            candidate = next(
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "stage-candidate"
            )
            self.assertEqual(candidate["content"].get("schema"), "openthesis.stage-checkpoint.v1")
            self.assertTrue(candidate["content"]["result"]["has_candidate"])
            candidate_stage_id = candidate["content"]["result"]["stage_id"]
            self.assertFalse(any(
                item["artifact_type"] == "stage-checkpoint"
                and item["content"]["result"]["stage_id"] == candidate_stage_id
                for item in storage.get_artifacts(run.run_id)
            ), "a durable candidate must not be presented as a committed checkpoint")
            self.assertEqual(provider.calls, ["financial-analyst"])

            resumed = ResearchWorkflow(storage, pack, provider, config, parallel_agents=False)
            resumed.run(
                DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture(),
                existing_run=run, resume_artifacts=storage.get_artifacts(run.run_id),
                resume_target="model-stages",
            )

            self.assertEqual(provider.calls.count("financial-analyst"), 1)
            self.assertEqual(len(provider.calls), 9)
            self.assertTrue(any(
                item["artifact_type"] == "stage-checkpoint"
                and item["content"]["result"]["stage_id"] == candidate_stage_id
                for item in storage.get_artifacts(run.run_id)
            ), "the candidate must become a checkpoint only after local revalidation")

    def test_changed_evidence_and_prompt_invalidate_only_matching_work(self) -> None:
        Path("D:/githubmax/tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = SevenStageProvider()
            config = ModelConfig(configured_model_id="test.fake", role="primary")
            pack = builtin_pack()
            workflow = ResearchWorkflow(storage, pack, provider, config, parallel_agents=False)
            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
            self.assertEqual(len(provider.calls), 9)

            changed_evidence = [
                *_disclosure_fixture(),
                {
                    "evidence_id": "filing:additional-market-risk",
                    "document_id": "demo-annual",
                    "source_url": "https://example.test/annual",
                    "title": "Additional market risk",
                    "excerpt": "The company faces additional market competition.",
                },
            ]
            resumed = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                filing_evidence=changed_evidence,
                existing_run=run,
                resume_artifacts=storage.get_artifacts(run.run_id),
                resume_target="model-stages",
            )
            self.assertEqual(len(provider.calls), 18, "changed evidence cannot reuse frozen inputs")

            original_prompt = pack.prompt

            def revised_prompt(current_pack, path: str) -> str:
                prompt = original_prompt(path)
                if path == "prompts/research-synthesizer.md":
                    return prompt + "\nAdditional synthesis instruction revision."
                return prompt

            with patch.object(type(pack), "prompt", new=revised_prompt):
                workflow.run(
                    DEMO_COMPANY,
                    demo_facts(),
                    filing_evidence=changed_evidence,
                    existing_run=resumed,
                    resume_artifacts=storage.get_artifacts(run.run_id),
                    resume_target="model-stages",
                )

            self.assertEqual(len(provider.calls), 19)
            self.assertEqual(provider.calls.count("financial-analyst"), 2)
            self.assertEqual(provider.calls.count("business-analyst"), 2)
            self.assertEqual(provider.calls.count("accounting-risk-analyst"), 2)
            # The two source-independent roots reuse these role identifiers,
            # so each role runs twice per graph generation.
            self.assertEqual(provider.calls.count("growth-opportunity-analyst"), 4)
            self.assertEqual(provider.calls.count("skeptical-analyst"), 4)
            self.assertEqual(provider.calls.count("forecast-analyst"), 2)
            self.assertEqual(provider.calls.count("research-synthesizer"), 3)

    def test_legacy_claim_contract_alias_is_only_sent_to_packs_that_request_it(self) -> None:
        _, current_user_prompt = _agent_prompt_bundle(
            "research-synthesizer", "Current pack instructions", "en", "{}", {}
        )
        current_payload = json.loads(current_user_prompt)
        self.assertIn("claim_contract", current_payload)
        self.assertNotIn("required_claim_shape", current_payload)

        _, legacy_user_prompt = _agent_prompt_bundle(
            "research-synthesizer",
            "Legacy pack explicitly reads required_claim_shape",
            "en",
            "{}",
            {},
        )
        legacy_payload = json.loads(legacy_user_prompt)
        self.assertEqual(
            legacy_payload["required_claim_shape"], legacy_payload["claim_contract"]
        )

    def test_skeptical_capacity_failure_is_checkpointed_and_independent_dependents_continue(self) -> None:
        class SkepticCapacityProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.payloads: list[dict[str, object]] = []

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                payload = json.loads(user_prompt)
                agent = str(payload.get("agent", ""))
                self.calls.append(agent)
                self.payloads.append(payload)
                if agent == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if agent == "skeptical-analyst":
                    raise ProviderError(
                        "context too large", retryable=True, code="MODEL_CONTEXT_CAPACITY"
                    )
                if agent == "forecast-analyst":
                    return {
                        "kind": "fact",
                        "analysis": "UNTRUSTED_FORECAST_SENTINEL: Revenue was 9876 CNY in 2025.",
                        "claims": [{
                            "kind": "fact",
                            "text": "UNTRUSTED_FORECAST_SENTINEL: Revenue was 9876 CNY in 2025.",
                            "evidence_ids": ["fact:missing-untrusted-stage"],
                        }],
                    }
                return {"kind": "opinion", "analysis": "stage", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = SkepticCapacityProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
            )

            run = workflow.run(
                DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture()
            )

            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertIn("skeptical-analyst", provider.calls)
            self.assertIn("forecast-analyst", provider.calls)
            self.assertIn("research-synthesizer", provider.calls)
            artifacts = storage.get_artifacts(run.run_id)
            self.assertTrue(any(item["artifact_type"] == "growth-opportunities" for item in artifacts))
            forecast_payload = next(
                payload for payload in provider.payloads
                if payload.get("agent") == "forecast-analyst"
            )
            forecast_prior = forecast_payload["prior_artifacts"]
            self.assertEqual(
                forecast_prior["growth_opportunities"]["opportunities"][0]["title"],
                "Synthetic opportunity",
            )
            self.assertTrue(any(
                item.get("stage_id") == "counter-analysis"
                for item in forecast_prior["research_dossier"]["dependency_gaps"]
            ))
            synthesis_payload = next(
                payload for payload in provider.payloads
                if payload.get("agent") == "research-synthesizer"
            )
            self.assertNotIn("UNTRUSTED_FORECAST_SENTINEL", json.dumps(synthesis_payload["prior_artifacts"]))
            latest = _latest_report_artifact(artifacts)
            self.assertNotIn("UNTRUSTED_FORECAST_SENTINEL", json.dumps(latest["content"]["report"]))
            _assert_partial_coverage(self, latest)
            outcome = next(
                item for item in reversed(artifacts)
                if item["artifact_type"] == "stage-outcome"
                and item["content"].get("stage") == "counterarguments"
            )
            # The confirmed capacity rejection has already entered partition
            # recovery. An indivisible remainder needs changed conditions,
            # not another identical-input automatic retry.
            self.assertEqual(outcome["content"]["outcome"], "needs_action")
            self.assertEqual(outcome["content"]["error_code"], "MODEL_CONTEXT_CAPACITY")

    def test_all_base_agents_failed_still_runs_independent_followups_with_gaps(self) -> None:
        class BaseUnavailableProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.payloads: list[dict[str, object]] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                payload = json.loads(user_prompt)
                agent = str(payload.get("agent", ""))
                self.calls.append(agent)
                self.payloads.append(payload)
                raise ProviderError(
                    "gateway timed out", retryable=True, code="MODEL_TIMEOUT"
                )

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = BaseUnavailableProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                parallel_agents=True,
            )

            run = workflow.run(
                DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture()
            )

            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertCountEqual(
                provider.calls,
                [
                    "financial-analyst", "business-analyst", "accounting-risk-analyst",
                    "growth-opportunity-analyst", "skeptical-analyst",
                    "growth-opportunity-analyst", "skeptical-analyst", "forecast-analyst",
                    "research-synthesizer",
                ],
            )
            forecast_payload = next(
                payload for payload in provider.payloads
                if payload.get("agent") == "forecast-analyst"
            )
            dossier = forecast_payload["prior_artifacts"]["research_dossier"]
            self.assertEqual(set(dossier["analyses"]), {
                "financial-analyst", "business-analyst", "accounting-risk-analyst",
                "source-industry-analysis", "source-risk-analysis",
            })
            self.assertTrue(all(not value for value in dossier["analyses"].values()))
            self.assertEqual(set(dossier["verified_analyses"]), set(dossier["analyses"]))
            self.assertTrue(all(not value for value in dossier["verified_analyses"].values()))
            gaps = {item["stage_id"] for item in dossier["dependency_gaps"]}
            self.assertTrue({
                "financial-analyst", "business-analyst", "accounting-risk-analyst",
                "source-industry-analysis", "source-risk-analysis",
                "growth-opportunities", "counter-analysis",
            }.issubset(gaps))
            self.assertEqual(
                forecast_payload["prior_artifacts"]["growth_opportunities"], {}
            )
            self.assertEqual(forecast_payload["prior_artifacts"]["counter_analysis"], {})
            artifacts = storage.get_artifacts(run.run_id)
            latest = _latest_report_artifact(artifacts)
            self.assertFalse(latest["content"]["readiness"]["complete"])
            self.assertFalse(latest["content"]["report"]["research_complete"])
            aggregate = next(
                item for item in reversed(artifacts)
                if item["artifact_type"] == "stage-outcome"
                and item["content"].get("stage") == "base-agents"
            )
            self.assertEqual(aggregate["content"]["outcome"], "waiting_retryable")
            self.assertTrue(aggregate["content"]["retryable"])

    def test_one_optional_base_failure_keeps_trusted_source_material_and_gap(self) -> None:
        class OneOptionalBaseFailureProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.payloads: list[dict[str, object]] = []
                self.semantic_requests: list[dict[str, object]] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                if not json_mode:
                    raise AssertionError("structured output must be enabled")
                payload = json.loads(user_prompt)
                agent = str(payload.get("agent", ""))
                self.payloads.append(payload)
                # The workflow may issue source/assertion verification requests
                # without a stage ``agent``. Keep those separate so a
                # semantic check cannot be mistaken for a hidden stage retry.
                if agent:
                    self.calls.append(agent)
                else:
                    self.semantic_requests.append(payload)
                if agent == "business-analyst":
                    return {
                        "kind": "fact",
                        "analysis": "UNTRUSTED_OPTIONAL_BASE_SENTINEL: unsupported revenue claim.",
                        "claims": [{
                            "kind": "fact",
                            "text": "UNTRUSTED_OPTIONAL_BASE_SENTINEL: revenue was 999.",
                            "evidence_ids": ["fact:missing-optional-base"],
                        }],
                    }
                if agent == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if agent == "research-synthesizer":
                    return _valid_research_synthesis_output()
                return {
                    "kind": "opinion",
                    "analysis": f"Stage analysis from {agent}.",
                    "claims": [],
                }

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = OneOptionalBaseFailureProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.optional-gap", role="primary"),
                parallel_agents=False,
            )

            run = workflow.run(
                DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture()
            )

            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertCountEqual(provider.calls, [
                "financial-analyst", "business-analyst", "accounting-risk-analyst",
                "growth-opportunity-analyst", "skeptical-analyst",
                "growth-opportunity-analyst", "skeptical-analyst", "forecast-analyst",
                "research-synthesizer",
            ])
            self.assertEqual(len(provider.semantic_requests), 2)
            self.assertTrue(all(
                not payload.get("agent")
                and isinstance(payload.get("protocol"), str)
                and isinstance(payload.get("assertions"), list)
                and bool(payload["assertions"])
                for payload in provider.semantic_requests
            ), repr(provider.semantic_requests))
            growth_payload = next(
                payload for payload in provider.payloads
                if payload.get("agent") == "growth-opportunity-analyst"
                and "research_dossier" in payload.get("prior_artifacts", {})
            )
            serialized_growth = json.dumps(growth_payload, ensure_ascii=False)
            self.assertNotIn("UNTRUSTED_OPTIONAL_BASE_SENTINEL", serialized_growth)
            self.assertIn(
                "The company sells equipment to industrial customers.",
                json.dumps(growth_payload["research_context"], ensure_ascii=False),
            )
            dossier = growth_payload["prior_artifacts"]["research_dossier"]
            self.assertEqual(dossier["analyses"]["business-analyst"], {})
            self.assertEqual(dossier["verified_analyses"]["business-analyst"], {})
            self.assertTrue(any(
                item.get("stage_id") == "business-analyst"
                for item in dossier["dependency_gaps"]
            ))
            report = _latest_report_artifact(storage.get_artifacts(run.run_id))
            self.assertNotIn(
                "UNTRUSTED_OPTIONAL_BASE_SENTINEL",
                json.dumps(report["content"], ensure_ascii=False),
            )

    def test_primary_synthesis_provider_error_preserves_stages_without_hidden_token_retry(self) -> None:
        class FinalUnavailableProvider:
            def __init__(self) -> None:
                self.calls: dict[str, int] = {}

            def test_connection(self) -> str:
                return "ok"

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                agent = str(json.loads(user_prompt).get("agent", ""))
                self.calls[agent] = self.calls.get(agent, 0) + 1
                if agent == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if agent == "research-synthesizer":
                    raise ProviderError("provider unavailable", retryable=True, code="MODEL_HTTP_503")
                if agent == "research-synthesizer-repair":
                    raise AssertionError("provider failures must not trigger a hidden token-consuming repair")
                return {"kind": "opinion", "analysis": "stage", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = FinalUnavailableProvider()
            states: list[tuple[str, str]] = []
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                agent_progress=lambda agent, state: states.append((agent, state)),
            )

            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())

            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertEqual(provider.calls["research-synthesizer"], 1)
            self.assertNotIn("research-synthesizer-repair", provider.calls)
            report = next(
                item for item in reversed(storage.get_artifacts(run.run_id))
                if item["artifact_type"] == "research-report"
            )
            self.assertEqual(report["content"]["mode"], "synthesis-incomplete")
            self.assertTrue(report["content"]["retryable"])
            self.assertEqual(report["content"]["diagnostics"]["provider_error_code"], "MODEL_HTTP_503")
            for agent in (
                "growth-opportunity-analyst",
                "skeptical-analyst",
                "forecast-analyst",
                "research-synthesizer",
            ):
                self.assertIn((agent, "queued"), states)
                self.assertIn((agent, "running"), states)
            self.assertIn(("research-synthesizer", "waiting_retryable"), states)

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

    def test_unique_fact_evidence_hydrates_omitted_identity_but_not_calculation(self) -> None:
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
        self.assertEqual(fact["claim_verifications"][0]["state"], "numeric_verified")
        self.assertEqual(calc["claim_verifications"][0]["state"], "insufficient_evidence")

    def test_unverified_vision_observation_cannot_support_a_claim(self) -> None:
        records = {
            "observation:segment-margin": {
                "kind": "unverified_visual_observation",
                "evidence_id": "observation:segment-margin",
                "raw_text": "Segment margin increased to 24.6%.",
            }
        }
        result = verify_agent_output(
            {"claims": [{"kind": "inference", "text": "Segment margin increased.",
                         "evidence_ids": ["observation:segment-margin"]}]},
            set(records), "en", records,
        )
        self.assertEqual(result["claim_verifications"][0]["state"], "reference_only")
        self.assertEqual(result["verified_claim_count"], 0)

    def test_semantic_reviewer_only_assists_qualitative_inference(self) -> None:
        records = {"filing:text": {
            "evidence_id": "filing:text",
            "kind": "filing_text",
            "raw_text": "Demand remains stable.",
            "company_cik": DEMO_COMPANY.cik,
            "entity": DEMO_COMPANY.name,
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": "2025-12-31",
            "scope": "consolidated",
            "document_id": "demo-annual",
            "document_sha256": "a" * 64,
            "source_url": "https://example.test/annual",
        }}
        calls: list[str] = []

        def reviewer(
            claim: dict[str, object], _records: list[dict[str, object]]
        ) -> dict[str, object]:
            calls.append(str(claim.get("kind")))
            from openthesis.research_assertions import AssertionSession, assertion_session

            probe = AssertionSession()
            with assertion_session(probe):
                probe.classify(claim, _records)
            query = next(iter(probe.queries.values()))
            source = query["sources"][0]
            return {
                "structured_output_valid": True,
                "results": [{
                    "assertion_id": query["assertion_id"],
                    "state": "supported",
                    "source_spans": [{
                        "source_block_id": source["source_block_id"],
                        "quote": source["raw_text"],
                    }],
                }],
            }

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

    def test_skeptical_input_references_one_canonical_evidence_registry_and_claim_graph(self) -> None:
        evidence = [{"evidence_id": f"fact:{index}"} for index in range(41)]
        dossier = {"trusted_channels": {"verified_facts": [{"text": "Revenue", "evidence_ids": ["fact:40"]}]}}
        context = ResearchContext(DEMO_COMPANY, [], [], [], evidence + _disclosure_fixture())
        payload = _skeptical_prior_artifacts(context, dossier, {})
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("fact:40", encoded)
        self.assertTrue(payload["thesis_claim_graph"])
        self.assertNotIn("canonical_evidence", payload)

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
        prompt_payload = json.loads(provider.user_prompt)
        routed_context = prompt_payload["research_context"]
        fact_registry = routed_context["evidence_partitions"]["quantitative_facts"]
        expected_fact_ids = {f"fact:{index}" for index in range(41)}
        self.assertEqual(len(fact_registry), 41)
        self.assertEqual(
            {item["evidence_id"] for item in fact_registry}, expected_fact_ids
        )

        claim_graph = prompt_payload["prior_artifacts"]["thesis_claim_graph"]
        self.assertTrue(claim_graph)
        self.assertEqual(claim_graph[0]["evidence_ids"], ["fact:40"])

        def fact_40_evidence_id_fields(value: object) -> list[str]:
            if isinstance(value, dict):
                matches = [
                    item for key, item in value.items()
                    if key == "evidence_id" and item == "fact:40"
                ]
                return matches + [
                    match
                    for child in value.values()
                    for match in fact_40_evidence_id_fields(child)
                ]
            if isinstance(value, list):
                return [
                    match
                    for child in value
                    for match in fact_40_evidence_id_fields(child)
                ]
            return []

        self.assertEqual(fact_40_evidence_id_fields(prompt_payload), ["fact:40"])

    def test_stage_material_manifest_removes_only_exact_duplicates(self) -> None:
        context = ResearchContext(
            DEMO_COMPANY,
            [], [], [],
            [
                {"evidence_id": "fact:one", "value": 1},
                {"evidence_id": "fact:one", "value": 1},
                {"evidence_id": "fact:one", "value": 2},
            ],
        )

        compact, manifest = _deduplicated_stage_context(
            context.compact_json(), "skeptical-analyst"
        )

        projected = json.loads(compact)
        self.assertEqual(len(projected["evidence"]), 2)
        self.assertEqual(manifest.exact_duplicates_removed, 1)
        self.assertEqual(manifest.unique_evidence_ids, ("fact:one",))

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

    def test_mislabeled_qualitative_fact_demotes_only_with_matching_filing_context(self) -> None:
        record = {
            "evidence_id": "filing:business-model",
            "kind": "filing_text",
            "company_cik": "CN:SZSE:002594",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": "2025-12-31",
            "raw_text": "The company sells vehicles through an established dealer network.",
        }
        records = {record["evidence_id"]: record}
        claim = {
            "kind": "fact",
            "text": "The company sells vehicles through an established dealer network.",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "evidence_ids": [record["evidence_id"]],
        }
        result = verify_agent_output(
            {"claims": [claim]}, set(records), "en", records
        )
        normalized = result["normalized_claims"][0]
        self.assertEqual(result["claim_verifications"][0]["state"], "text_supported")
        self.assertEqual(normalized["kind"], "inference")
        self.assertEqual(normalized["claim_channel"], "filing_disclosure_inference")

        wrong_period = {**claim, "fiscal_year": 2024}
        rejected = verify_agent_output(
            {"claims": [wrong_period]}, set(records), "en", records
        )
        self.assertEqual(
            rejected["claim_verifications"][0]["state"], "insufficient_evidence"
        )

        numeric = {**claim, "text": "Revenue grew 25% because of vehicle sales."}
        rejected_numeric = verify_agent_output(
            {"claims": [numeric]}, set(records), "en", records
        )
        self.assertEqual(
            rejected_numeric["claim_verifications"][0]["state"], "insufficient_evidence"
        )

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
        synthesis = {key: "ok" for key in ("executive_summary", "business_model", "financial_quality", "balance_sheet", "competitive_position", "growth_opportunities", "scenarios", "implied_expectations", "thesis", "invalidation_conditions", "leading_indicators", "unresolved_questions", "claims")}
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
                "company_cik": "CN:SZSE:002594",
                "entity": "Demo Company",
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "end_date": "2025-12-31",
                "scope": "consolidated",
                "document_id": "demo-annual",
                "document_sha256": "a" * 64,
                "source_url": "https://example.test/annual",
                "raw_text": "营业收入同比下降20%。",
            }
        }

        def host_review(
            claim: dict[str, object], records: list[dict[str, object]]
        ) -> dict[str, object]:
            """Return a source-bound contradiction through the host protocol."""
            from openthesis.research_assertions import AssertionSession, assertion_session

            probe = AssertionSession()
            with assertion_session(probe):
                probe.classify(claim, records)
            query = next(iter(probe.queries.values()))
            source = query["sources"][0]
            return {
                "structured_output_valid": True,
                "results": [{
                    "assertion_id": query["assertion_id"],
                    "state": "contradicted",
                    "source_spans": [{
                        "source_block_id": source["source_block_id"],
                        "quote": source["raw_text"],
                    }],
                }],
            }

        result = verify_agent_output(
            {
                "claims": [{
                    "kind": "inference",
                    "text": "营业收入同比增长80%。",
                    "fiscal_year": 2025,
                    "fiscal_period": "FY",
                    "evidence_ids": ["filing:text:revenue"],
                }]
            },
            set(evidence),
            "zh-CN",
            evidence,
            semantic_reviewer=host_review,
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

    def test_resume_uses_latest_trusted_base_artifacts_and_ignores_later_failed_roles(self) -> None:
        verified_opinion = {
            "kind": "opinion",
            "analysis": "Retained role analysis.",
            "claims": [],
        }
        verification = {"passed": True, "claim_verifications": []}
        artifacts = [
            {
                "artifact_type": "agent-analysis",
                "agent_id": "business-analyst",
                "content": {
                    "result": verified_opinion,
                    "verification": verification,
                    "trusted_state": "completed_partial",
                },
            },
            {
                "artifact_type": "agent-analysis",
                "agent_id": "financial-analyst",
                "content": {
                    "result": verified_opinion,
                    "verification": verification,
                    "trusted_state": "completed_partial",
                },
            },
            {
                "artifact_type": "agent-analysis",
                "agent_id": "financial-analyst",
                "content": {"trusted_state": "failed_verification"},
            },
        ]

        results, verifications = _trusted_prior_base_results(
            artifacts,
            {"financial-analyst", "business-analyst", "accounting-risk-analyst"},
        )

        self.assertEqual(set(results), {"business-analyst"})
        self.assertEqual(set(verifications), {"business-analyst"})

    def test_recovery_reuses_only_latest_verified_stage_content(self) -> None:
        artifacts = [
            {
                "artifact_type": "growth-opportunities",
                "content": {
                    "opportunities": [{"title": "prior"}],
                    "_validation": {"passed": True},
                    "_audit": {"secret": "must-not-reach-model"},
                },
            },
        ]
        self.assertEqual(
            _trusted_prior_stage_result(artifacts, "growth-opportunities"),
            {"opportunities": [{"title": "prior"}]},
        )
        artifacts.append({
            "artifact_type": "growth-opportunities",
            "content": {"opportunities": [], "_validation": {"passed": False}},
        })
        self.assertIsNone(
            _trusted_prior_stage_result(artifacts, "growth-opportunities")
        )

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

    def test_declared_small_context_uses_bounded_lossless_coverage_calls(self) -> None:
        class RecordingProvider:
            # Complete 18-obligation/source-binding instructions now have a
            # larger immutable envelope. Keep this a small but feasible window;
            # a smaller impossible envelope must not be made to fit by dropping
            # necessary source data or weakening the quality instructions.
            context_window_tokens = 12_000

            def __init__(self) -> None:
                self.payload_sizes: list[int] = []

            def generate(self, system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                self.payload_sizes.append(
                    len(system_prompt.encode("utf-8"))
                    + len(user_prompt.encode("utf-8"))
                )
                return {"claims": [{"text": "section result", "kind": "inference", "evidence_ids": []}]}

        with tempfile.TemporaryDirectory() as directory:
            provider = RecordingProvider()
            workflow = ResearchWorkflow(
                Storage(Path(directory)),
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
            )
            context = ResearchContext(
                DEMO_COMPANY,
                [],
                [],
                [],
                [
                    {"evidence_id": "fact:test", "kind": "financial_fact"},
                    *_disclosure_fixture(),
                ],
            )
            result = workflow._run_synthesis_with_budget(
                context,
                {"analyses": {"financial_quality": ["Q" * 2_000 for _ in range(5)]}},
                {"opportunities": ["G" * 2_000 for _ in range(5)]},
                {"strongest_counterarguments": ["R" * 2_000 for _ in range(5)]},
                {"scenarios": [{"name": "base"}]},
            )
            self.assertGreater(len(provider.payload_sizes), 1)
            capability = provider_context_capability(provider)
            self.assertEqual(capability.max_input_bytes, 23_424)
            self.assertTrue(
                all(size <= capability.max_input_bytes for size in provider.payload_sizes)
            )
            self.assertTrue(result["claims"])

    def test_retry_reuses_successful_coverage_partitions_by_digest(self) -> None:
        class MergeFailureProvider:
            context_window_tokens = 12_000

            def __init__(self) -> None:
                self.coverage_calls = 0
                self.merge_calls = 0

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                payload = json.loads(user_prompt)
                if payload.get("target_agent"):
                    self.coverage_calls += 1
                    return {
                        "claims": [{"text": "covered", "kind": "inference", "evidence_ids": []}],
                        "source_evidence_ids": [],
                    }
                self.merge_calls += 1
                raise ProviderError(
                    "merge unavailable", retryable=True, code="MODEL_UNAVAILABLE"
                )

        with tempfile.TemporaryDirectory() as directory:
            provider = MergeFailureProvider()
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            run = ResearchRun(
                run_id="coverage-resume",
                company=DEMO_COMPANY,
                workflow_id="long-term-fundamentals",
                research_pack_id="builtin",
                research_pack_version="1",
                provider_id="test",
                model_id="small",
                data_as_of="2026-09-21",
            )
            storage.save_run(run)
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.small", role="primary"),
            )
            self.assertEqual(provider_context_capability(provider).max_input_bytes, 23_424)
            context = ResearchContext(
                DEMO_COMPANY,
                [],
                [],
                [],
                [{"evidence_id": "fact:test", "kind": "financial_fact"}, *_disclosure_fixture()],
            )
            prior = {
                "analyses": {"financial_quality": ["Q" * 2_000 for _ in range(5)]},
                "opportunities": ["G" * 2_000 for _ in range(5)],
                "counterarguments": ["R" * 2_000 for _ in range(5)],
            }

            for _ in range(2):
                with self.assertRaises(ProviderError):
                    workflow._run_agent_adaptive(
                        "research-synthesizer",
                        "prompts/research-synthesizer.md",
                        context.compact_json(),
                        prior,
                        run=run,
                    )
                if _ == 0:
                    first_coverage_calls = provider.coverage_calls
                    first_checkpoint_count = len([
                        item for item in storage.get_artifacts(run.run_id)
                        if item["artifact_type"] == "stage-material-coverage"
                    ])

            self.assertGreater(first_coverage_calls, 0)
            self.assertEqual(
                len([
                    item for item in storage.get_artifacts(run.run_id)
                    if item["artifact_type"] == "stage-material-coverage"
                ]),
                first_checkpoint_count,
            )
            self.assertEqual(provider.coverage_calls, first_coverage_calls)
            self.assertEqual(provider.merge_calls, 2)

    def test_true_eight_thousand_token_limit_keeps_coverage_checkpoints_and_fails_closed(self) -> None:
        class EightKProvider:
            context_window_tokens = 8_000

            def __init__(self) -> None:
                self.request_sizes: list[int] = []
                self.coverage_calls = 0
                self.final_synthesis_calls = 0

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                self.request_sizes.append(
                    len(_system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
                )
                payload = json.loads(user_prompt)
                if payload.get("target_agent"):
                    self.coverage_calls += 1
                    return {
                        "claims": [{"text": "Partition was reviewed.", "kind": "inference", "evidence_ids": []}],
                        "source_evidence_ids": [],
                    }
                if payload.get("agent") == "research-synthesizer":
                    self.final_synthesis_calls += 1
                return {"claims": []}

        with tempfile.TemporaryDirectory() as directory:
            provider = EightKProvider()
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            pack = builtin_pack()
            config = ModelConfig(configured_model_id="test.eight-k", role="primary")
            run = ResearchRun(
                run_id="coverage-insufficient-8k",
                company=DEMO_COMPANY,
                workflow_id="long-term-fundamentals",
                research_pack_id=pack.pack_id,
                research_pack_version=pack.version,
                provider_id="test",
                model_id="eight-k",
                data_as_of="2026-09-21",
                status=RunStatus.RUNNING,
            )
            storage.save_run(run)
            workflow = ResearchWorkflow(storage, pack, provider, config)
            context = ResearchContext(
                DEMO_COMPANY,
                [],
                [],
                [],
                [{"evidence_id": "fact:test", "kind": "financial_fact"}, *_disclosure_fixture()],
            )
            prior = {
                "analyses": {"financial_quality": ["Q" * 2_000 for _ in range(5)]},
                "opportunities": ["G" * 2_000 for _ in range(5)],
                "counterarguments": ["R" * 2_000 for _ in range(5)],
            }

            capability = provider_context_capability(provider)
            self.assertEqual(capability.max_input_bytes, 7_424)
            with self.assertRaises(SynthesisContextLimitError) as caught:
                workflow._run_agent_adaptive(
                    "research-synthesizer",
                    "prompts/research-synthesizer.md",
                    context.compact_json(),
                    prior,
                    run=run,
                )

            self.assertEqual(caught.exception.available_bytes, 7_424)
            self.assertGreater(caught.exception.required_bytes, 7_424)
            self.assertGreater(provider.coverage_calls, 0)
            self.assertEqual(provider.final_synthesis_calls, 0)
            self.assertTrue(provider.request_sizes)
            self.assertTrue(all(size <= 7_424 for size in provider.request_sizes))
            checkpoints = [
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "stage-material-coverage"
            ]
            self.assertTrue(checkpoints, "every reviewed coverage capsule must be checkpointed before merge failure")
            checkpointed_ids = {
                evidence_id
                for item in checkpoints
                for evidence_id in item["content"]["capsule"]["source_evidence_ids"]
            }
            expected_ids = {"fact:test", *(item["evidence_id"] for item in _disclosure_fixture())}
            self.assertTrue(expected_ids.issubset(checkpointed_ids))

    def test_length_finish_reason_is_capacity_only_when_required_sections_are_missing(self) -> None:
        complete = {
            key: (["ok"] if key in {
                "growth_opportunities", "counterarguments", "scenarios",
                "invalidation_conditions", "leading_indicators", "unresolved_questions",
            } else [] if key == "claims" else "ok")
            for key in REQUIRED_REPORT_SECTIONS
        }
        complete["claims"] = [{"text": "supported conclusion", "kind": "inference", "evidence_ids": []}]

        class LengthProvider:
            def __init__(self, payload): self.payload = payload; self.calls = 0
            def generate(self, *_args, **_kwargs):
                self.calls += 1
                return {**self.payload, "_response_meta": {"finish_reason": "length"}}

        context = ResearchContext(
            DEMO_COMPANY, [], [], [],
            [{"evidence_id": "fact:test", "kind": "financial_fact"}, *_disclosure_fixture()],
        )
        with tempfile.TemporaryDirectory() as directory:
            provider = LengthProvider(complete)
            workflow = ResearchWorkflow(
                Storage(Path(directory)), builtin_pack(), provider,
                ModelConfig(configured_model_id="test.length", role="primary"),
            )
            result = workflow._run_synthesis_with_budget(context, {}, {}, {}, {})
            self.assertEqual(result["executive_summary"], "ok")
            self.assertEqual(provider.calls, 1)

            provider.payload = {"executive_summary": "truncated"}
            with self.assertRaises(SynthesisContextLimitError):
                workflow._run_synthesis_with_budget(context, {}, {}, {}, {})
            self.assertEqual(provider.calls, 2)

    def test_run_context_capacity_preserves_stages_without_fake_complete_report(self) -> None:
        class CapacityProvider:
            context_window_tokens = 100_000

            def __init__(self) -> None:
                self.calls: list[str] = []
                self.max_input_bytes = 100_000

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                agent = str(json.loads(user_prompt).get("agent", ""))
                self.calls.append(agent)
                if agent == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if agent == "research-synthesizer":
                    raise ProviderError(
                        "provider rejected complete context",
                        code="MODEL_CONTEXT_CAPACITY",
                    )
                return {
                    "kind": "opinion",
                    "analysis": f"preserved {agent}",
                    "claims": [],
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
            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
            artifacts = storage.get_artifacts(run.run_id)
            report = _latest_report_artifact(artifacts)
            content = report["content"]
            self.assertEqual(content["mode"], "synthesis-incomplete")
            self.assertEqual(
                provider.calls.count("research-synthesizer"), 0,
                "strict capacity exhaustion must fail before a provider synthesis call",
            )
            self.assertEqual(
                content["report"]["cross_section_synthesis_status"],
                "not_completed_context_capacity",
            )
            self.assertFalse(content["report"]["research_complete"])
            self.assertEqual(
                content["report"]["context_budget"]["counting_mode"],
                "conservative_utf8_byte_upper_bound",
            )
            _assert_progressive_report_has_trusted_body(self, artifacts, run.run_id)

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
                    "kind": "opinion",
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
                    "claims": [],
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

            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())

            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertEqual(provider.calls, ["company-analysis", "verification"])
            artifacts = storage.get_artifacts(run.run_id)
            ot_steps = [item for item in artifacts if item["artifact_type"] == "ot-agent-analysis"]
            self.assertEqual([item["content"]["step_id"] for item in ot_steps], provider.calls)
            report = next(item for item in artifacts if item["artifact_type"] == "research-report")
            self.assertEqual(report["content"]["mode"], "ot-workflow")
            self.assertEqual(report["content"]["workflow"]["pack_id"], "my.company-research")
            self.assertFalse(report["content"]["verification"]["passed"])
            _assert_partial_coverage(self, report, expected_state="action_required")
            self.assertEqual(run.research_configuration["ot_workflow"]["step_ids"], provider.calls)

    def test_growth_empty_response_requires_explicit_isolated_retry(self) -> None:
        class GrowthRetryProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.growth_calls = 0
                self.payloads: list[dict[str, object]] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.assertTrue(json_mode)
                payload = json.loads(user_prompt)
                agent = str(payload.get("agent", ""))
                self.calls.append(agent)
                self.payloads.append(payload)
                if agent == "growth-opportunity-analyst":
                    if payload.get("prior_artifacts", {}).get("source_research_lane"):
                        return {
                            "kind": "opinion",
                            "analysis": "Source-only industry disclosure review.",
                            "claims": [],
                        }
                    self.growth_calls += 1
                    if self.growth_calls <= 1:
                        return {
                            "opportunities": [],
                            "structured_output_valid": False,
                            "_response_error": "empty_content",
                            "analysis": "UNTRUSTED_EMPTY_GROWTH_SENTINEL",
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
                if agent != "research-synthesizer":
                    return {"kind": "opinion", "analysis": "Stage output.", "claims": []}
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
                    "claims": [_verified_demo_revenue_claim()],
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

            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
            self.assertEqual(provider.growth_calls, 1, "initial run must not spend a hidden retry")
            for agent in ("skeptical-analyst", "forecast-analyst", "research-synthesizer"):
                self.assertIn(agent, provider.calls)
            counter_payload = next(
                payload for payload in provider.payloads
                if payload.get("agent") == "skeptical-analyst"
                and "research_dossier" in payload.get("prior_artifacts", {})
            )
            self.assertEqual(counter_payload["prior_artifacts"]["growth_opportunities"], {})
            self.assertTrue(any(
                item.get("stage_id") == "growth-opportunities"
                for item in counter_payload["prior_artifacts"]["research_dossier"]["dependency_gaps"]
            ))
            for payload in provider.payloads:
                if payload.get("agent") in {
                    "skeptical-analyst", "forecast-analyst", "research-synthesizer"
                }:
                    self.assertNotIn(
                        "UNTRUSTED_EMPTY_GROWTH_SENTINEL",
                        json.dumps(payload.get("prior_artifacts", {})),
                    )
            initial_artifacts = storage.get_artifacts(run.run_id)
            initial_report = _latest_report_artifact(initial_artifacts)
            self.assertNotIn(
                "UNTRUSTED_EMPTY_GROWTH_SENTINEL",
                json.dumps(initial_report["content"]["report"]),
            )
            initial_dossier = next(
                item for item in reversed(storage.get_artifacts(run.run_id))
                if item["artifact_type"] == "verified-research-dossier"
            )
            self.assertEqual(
                set(initial_dossier["content"]["analyses"]),
                {
                    "financial-analyst", "business-analyst", "accounting-risk-analyst",
                    "source-industry-analysis", "source-risk-analysis",
                },
                "a rejected downstream stage must not erase its healthy independent base siblings",
            )
            before = {agent: provider.calls.count(agent) for agent in set(provider.calls)}

            workflow.retry_growth(
                run,
                storage.get_artifacts(run.run_id),
                demo_facts(),
            )

            self.assertEqual(provider.growth_calls, 2)
            self.assertEqual(
                provider.calls.count("research-synthesizer"),
                before.get("research-synthesizer", 0) + 1,
            )
            for agent in (
                "financial-analyst",
                "business-analyst",
                "accounting-risk-analyst",
            ):
                self.assertEqual(provider.calls.count(agent), before[agent])
            self.assertEqual(provider.calls.count("skeptical-analyst"), before.get("skeptical-analyst", 0) + 1)
            self.assertEqual(provider.calls.count("forecast-analyst"), before.get("forecast-analyst", 0) + 1)
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
            run = workflow.run(DEMO_COMPANY, facts, filing_evidence=_disclosure_fixture())
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
                market_snapshot={"source": "manual", "status": "MANUAL", "market_cap": 1_000_000_000, "currency": "HKD", "as_of": "2026-08-09"},
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
                        "source": "verified-fixture", "status": "MANUAL", "market_cap": 1_000_000_000,
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

    def test_provider_429_retries_only_the_same_request_with_a_small_bound(self) -> None:
        class TransientProvider:
            def __init__(self, failures: int) -> None:
                self.failures = failures
                self.calls = 0

            def generate(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls <= self.failures:
                    raise ProviderError(
                        "rate limited", code="MODEL_RATE_LIMITED", retry_after_seconds=1
                    )
                return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            config = ModelConfig(configured_model_id="test.429", role="primary")
            provider = TransientProvider(failures=2)
            workflow = ResearchWorkflow(storage, builtin_pack(), provider, config)
            # Do not make the unit test wait through a real provider cooldown;
            # it still exercises slot release, same-request requeue, and bound.
            workflow.model_scheduler.mark_rate_limited = lambda _seconds=None: None  # type: ignore[method-assign]

            self.assertEqual(
                workflow._generate_with_cancellation("system", "request"),
                {"ok": True},
            )
            self.assertEqual(provider.calls, 3)

            exhausted = TransientProvider(failures=5)
            workflow.provider = exhausted
            with self.assertRaises(ProviderError) as caught:
                workflow._generate_with_cancellation("system", "request")
            self.assertEqual(caught.exception.code, "MODEL_RATE_LIMITED")
            self.assertEqual(exhausted.calls, 3)

    def test_multi_agent_workflow_with_fake_provider(self) -> None:
        events: list[tuple[str, str]] = []

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
                payload = json.loads(user_prompt)
                if "assertions" not in payload:
                    self.assertions(system_prompt, user_prompt, json_mode)
                if "assertions" in payload:
                    return {
                        "structured_output_valid": True,
                        "results": [
                            {
                                "assertion_id": assertion["assertion_id"],
                                "state": "supported",
                                "source_spans": [
                                    {
                                        "source_block_id": source["source_block_id"],
                                        "quote": source["raw_text"],
                                    }
                                    for source in assertion.get("sources", [])[:1]
                                    if isinstance(source, dict)
                                    and isinstance(source.get("source_block_id"), str)
                                    and isinstance(source.get("raw_text"), str)
                                ],
                            }
                            for assertion in payload["assertions"]
                            if isinstance(assertion, dict) and assertion.get("assertion_id")
                        ],
                    }
                agent = str(payload.get("agent", ""))
                events.append(("provider", agent))
                if agent == "growth-opportunity-analyst":
                    return _valid_growth_output()
                refs = ["filing:business"]
                analysis = {
                    "executive_summary": "Verified revenue anchors this synthetic research.",
                    "business_model": "Because the company sells equipment to industrial customers, its business model is industrial equipment.",
                    "financial_quality": "Because the company sells equipment to industrial customers, financial quality is reviewed against the disclosed operating context.",
                    "balance_sheet": "Because the company sells equipment to industrial customers, balance-sheet resilience remains a material research question.",
                    "competitive_position": "Because the company sells equipment to industrial customers, competitive position depends on durable customer value.",
                    "thesis": "Because the company sells equipment to industrial customers, the thesis remains conditional on durable demand.",
                    "invalidation_conditions": "If customer demand weakens materially, the thesis may fail.",
                    "leading_indicators": "Customer demand may indicate whether the thesis is strengthening.",
                }
                result = {
                    key: {"analysis": text, "evidence_ids": refs}
                    for key, text in analysis.items()
                }
                result.update({
                    "model_valuation": "Current price implies future FCF growth of 8%-12%.",
                    "growth_opportunities": [{
                        "analysis": "Because the company sells equipment to industrial customers, product expansion may create growth.",
                        "evidence_ids": refs,
                    }],
                    "counterarguments": [{
                        "analysis": "Customer concentration may create risk for the thesis.",
                        "evidence_ids": refs,
                    }],
                    "scenarios": [{
                        "analysis": "Demand may remain durable if industrial customers continue purchasing equipment.",
                        "evidence_ids": refs,
                    }],
                    "unresolved_questions": [{
                        "analysis": "The durability of customer demand remains unresolved.",
                        "evidence_ids": refs,
                    }],
                    "implied_expectations": {
                        "summary": "Current market price implies 8%-12% FCF growth.",
                        "discount_rate": 0.10,
                    },
                    "claims": [_verified_demo_revenue_claim()],
                })
                return result

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
            source_evidence = [
                {
                    **item,
                    "kind": "filing_text",
                    "raw_text": item["excerpt"],
                    "company_cik": DEMO_COMPANY.cik,
                    "entity": DEMO_COMPANY.name,
                    "fiscal_year": 2021,
                    "fiscal_period": "FY",
                    "end_date": "2021-12-31",
                    "scope": "consolidated",
                    "document_sha256": "a" * 64,
                }
                for item in _disclosure_fixture()
            ]
            progress: list[tuple[str, int]] = []

            def on_progress(message: str, percent: int) -> None:
                events.append(("progress", message))
                progress.append((message, percent))

            run = workflow.run(
                DEMO_COMPANY,
                facts,
                filing_evidence=source_evidence,
                progress=on_progress,
            )
            self.assertEqual(run.status, RunStatus.PARTIAL)
            # Nine official stage executions now include the two independent
            # source-analysis roots; the fake provider also receives the
            # source-bound assertion requests for the validated stage claims.
            # All nine generation stages remain. Five later consumers now
            # share the three source-bound verifier requests instead of
            # paying for identical unresolved premises again. This fake does
            # not return valid verdicts: their consumers must remain partial.
            with storage.connect() as db:
                purposes = dict(db.execute("SELECT purpose,COUNT(*) FROM model_request_journal WHERE run_id=? GROUP BY purpose", (run.run_id,)))
            self.assertEqual(purposes, {"generation": 9, "semantic_verification": 3})
            self.assertEqual(provider.count, 12)
            messages = [message for message, _ in progress]
            self.assertTrue(any("1/5" in message for message in messages))
            self.assertIn("正在合成最终长期研究报告", messages)
            self.assertIn("综合报告生成不完整", progress[-1][0])
            self.assertEqual(progress[-1][1], 100)
            self.assertEqual([percent for _, percent in progress], sorted(percent for _, percent in progress))
            for agent, message in (
                ("growth-opportunity-analyst", "正在研究公司与行业增长机会"),
                ("skeptical-analyst", "正在进行反方审查与压力测试"),
                ("forecast-analyst", "正在生成长期经营情景"),
                ("research-synthesizer", "正在合成最终长期研究报告"),
            ):
                progress_index = events.index(("progress", message))
                provider_indices = [
                    index for index, event in enumerate(events)
                    if event == ("provider", agent)
                ]
                self.assertTrue(provider_indices)
                self.assertTrue(
                    any(progress_index < index for index in provider_indices),
                    f"{message} must precede its downstream {agent} call",
                )
            artifacts = storage.get_artifacts(run.run_id)
            artifact_counts = Counter(item["artifact_type"] for item in artifacts)
            # Count stable artifact contracts independently; report snapshots
            # are additional evidence records, not duplicate final heads.
            self.assertEqual(artifact_counts["stage-candidate"], 9)
            self.assertEqual(artifact_counts["stage-checkpoint"], 9)
            # The official graph publishes one canonical final report.  Stage
            # candidate/checkpoint artifacts above retain the individual
            # evidence and restart boundaries; a second report would be a
            # duplicate head rather than an additional stage result.
            # Each committed trusted stage may leave a local report snapshot;
            # the latest report revision remains the single canonical head.
            self.assertGreaterEqual(artifact_counts["research-report"], 1)
            final = _latest_report_artifact(artifacts)
            latest_revision = storage.latest_report_revision(run.run_id)
            self.assertIsNotNone(latest_revision)
            self.assertEqual(latest_revision["payload"], final["content"])
            _assert_partial_coverage(self, final, expected_state="substantive_partial")
            report = final["content"]["report"]
            self.assertFalse(report["research_complete"])
            # The final synthesis candidate is retained in the current report
            # head once source-bound filing identity is complete, while the
            # incomplete status still prevents it from posing as complete.
            self.assertEqual(report["executive_summary"]["analysis"],
                "Verified revenue anchors this synthetic research.")
            self.assertEqual(
                report["cross_section_synthesis_status"],
                "generated_pending_verification",
            )
            self.assertNotIn("8%-12%", str(report))
            synthesis_candidate = next(
                item for item in artifacts
                if item["artifact_type"] == "stage-candidate"
                and item["content"]["result"]["stage_id"] == "final-synthesis"
            )
            self.assertEqual(
                synthesis_candidate["content"]["result"]["candidate"]["model_valuation"],
                "Current price implies future FCF growth of 8%-12%.",
            )
            self.assertFalse(final["content"]["verification"]["valuation_consistency"]["valuation_available"])
            self.assertGreater(final["content"]["verification"]["valuation_consistency"]["quarantined_count"], 0)
            theses = storage.list_thesis_versions(DEMO_COMPANY.cik)
            self.assertEqual(theses, [], "a coverage-incomplete report must not publish a final thesis")

    def test_empty_final_synthesis_is_partial_and_preserves_stage_outputs(self) -> None:
        class EmptyFinalProvider:
            def __init__(self) -> None:
                self.count = 0
                self.synthesis_calls = 0
                self.system_prompts: list[str] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.count += 1
                self.system_prompts.append(system_prompt)
                payload = json.loads(user_prompt)
                if "assertions" in payload:
                    return {
                        "structured_output_valid": True,
                        "results": [
                            {
                                "assertion_id": assertion["assertion_id"],
                                "state": "unresolved",
                                "source_spans": [],
                            }
                            for assertion in payload["assertions"]
                            if isinstance(assertion, dict) and assertion.get("assertion_id")
                        ],
                    }
                agent = payload.get("agent")
                if agent == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if agent == "research-synthesizer":
                    self.synthesis_calls += 1
                    # The initial synthesis is intentionally malformed.  The
                    # explicit retry is the next synthesis request and is
                    # valid, independent of source-lane call-count changes.
                    if self.synthesis_calls == 1:
                        return {
                            "narrative": "",
                            "structured_output_valid": False,
                            "_response_error": "empty_content",
                        }
                    refs = ["filing:business"]
                    sections = {
                        "executive_summary": "Synthetic verified research summary.",
                        "business_model": "Because the company sells equipment to industrial customers, its business model is industrial equipment.",
                        "financial_quality": "Because the company sells equipment to industrial customers, financial quality is reviewed against the disclosed operating context.",
                        "balance_sheet": "Because the company sells equipment to industrial customers, balance-sheet resilience remains a material research question.",
                        "competitive_position": "Because the company sells equipment to industrial customers, competitive position depends on durable customer value.",
                        "thesis": "Because the company sells equipment to industrial customers, the thesis remains conditional on durable demand.",
                        "invalidation_conditions": "If customer demand weakens materially, the thesis may fail.",
                        "leading_indicators": "Customer demand may indicate whether the thesis is strengthening.",
                    }
                    repaired = {
                        key: {"analysis": text, "evidence_ids": refs}
                        for key, text in sections.items()
                    }
                    repaired.update({
                        "growth_opportunities": [{
                            "analysis": "Because the company sells equipment to industrial customers, product expansion may create growth.",
                            "evidence_ids": refs,
                        }],
                        "counterarguments": [{
                            "analysis": "Customer concentration may create risk for the thesis.",
                            "evidence_ids": refs,
                        }],
                        "scenarios": [{
                            "analysis": "Demand may remain durable if industrial customers continue purchasing equipment.",
                            "evidence_ids": refs,
                        }],
                        "unresolved_questions": [{
                            "analysis": "The durability of customer demand remains unresolved.",
                            "evidence_ids": refs,
                        }],
                        "implied_expectations": {"summary": "Valuation assumptions remain explicit."},
                        "claims": [_verified_demo_revenue_claim()],
                    })
                    return repaired
                return {
                    # Exercise retention of source-supported content, not an
                    # unbound numbered assertion admitted via kind=opinion.
                    "analysis": "The company sells equipment to industrial customers.",
                    "evidence_ids": ["filing:business"],
                    "claims": [],
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

            # The body is evidence-supported only when its disclosure has
            # independently captured issuer and reporting-period identity.
            disclosures = [{**item, 'kind': 'filing_text', 'company_cik': DEMO_COMPANY.cik,
                'end_date': '2025-12-31', 'fiscal_year': 2025, 'fiscal_period': 'FY'}
                for item in _disclosure_fixture()]
            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=disclosures)

            self.assertEqual(run.status, RunStatus.PARTIAL)
            artifacts = storage.get_artifacts(run.run_id)
            report = _latest_report_artifact(artifacts)
            self.assertEqual(report["content"]["mode"], "synthesis-incomplete")
            self.assertTrue(report["content"]["retryable"])
            self.assertEqual(report["content"]["diagnostics"]["parse_error_class"], "empty_content")
            self.assertNotIn("prompt", report["content"]["diagnostics"])
            _assert_partial_coverage(self, report)
            fallback = report["content"]["report"]
            self.assertFalse(fallback["research_complete"])
            self.assertFalse(fallback["claims"])
            self.assertIn("The company sells equipment to industrial customers.",
                          str(fallback["business_model"]))
            self.assertIn("The company sells equipment to industrial customers.",
                          str(fallback["financial_quality"]))
            growth = next(
                item for item in reversed(artifacts)
                if item["artifact_type"] == "growth-opportunities"
            )
            self.assertIn("Synthetic opportunity", str(growth["content"]["opportunities"]))
            self.assertEqual(storage.list_thesis_versions(DEMO_COMPANY.cik), [])
            self.assertEqual(provider.count, 10, "run performs exactly one final synthesis call")
            _assert_progressive_report_has_trusted_body(self, artifacts, run.run_id)

            retried = workflow.retry_synthesis(
                run,
                storage.get_artifacts(run.run_id),
                demo_facts(),
            )
            self.assertEqual(provider.count, 12, "explicit retry performs one final-only call plus its source-bound verification")
            self.assertEqual(retried.status, RunStatus.PARTIAL)
            retried_artifacts = storage.get_artifacts(run.run_id)
            retried_report = _latest_report_artifact(retried_artifacts)
            self.assertEqual(retried_report["content"]["mode"], "synthesis-incomplete")
            self.assertTrue(retried_report["content"]["retryable"])
            _assert_partial_coverage(self, retried_report)
            _assert_progressive_report_has_trusted_body(self, retried_artifacts, run.run_id)
            self.assertEqual(
                retried_report["content"]["report"]["executive_summary"]["analysis"],
                "Synthetic verified research summary.",
            )

    def test_invalid_synthesis_does_not_trigger_hidden_repair(self) -> None:
        class RepairUnavailableProvider:
            def __init__(self) -> None:
                self.count = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                self.count += 1
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if json.loads(user_prompt).get("agent") == "research-synthesizer":
                    return {"narrative": "malformed", "structured_output_valid": False, "_response_error": "invalid_json"}
                return {"kind": "opinion", "analysis": "stage", "claims": []}

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
            run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertEqual(provider.count, 10)
            report = _latest_report_artifact(storage.get_artifacts(run.run_id))
            self.assertEqual(report["content"]["mode"], "synthesis-incomplete")
            self.assertEqual(report["content"]["diagnostics"]["parse_error_class"], "invalid_json")

    def test_failed_provider_persists_partial_checkpoint_without_downstream_calls(self) -> None:
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
            run = workflow.run(DEMO_COMPANY, facts, filing_evidence=_disclosure_fixture())
            runs = storage.list_runs()
            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertEqual(runs[0]["status"], RunStatus.PARTIAL.value)
            outcome = next(
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "stage-outcome"
                and item["content"].get("stage") == "base-agents"
            )
            self.assertEqual(outcome["content"]["outcome"], "needs_action")
            self.assertFalse(outcome["content"]["retryable"])
            self.assertEqual(outcome["content"]["completed"], 0)

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
                return {"kind": "opinion", "analysis": "English output", "claims": []}

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
                filing_evidence=_disclosure_fixture(),
                progress=lambda message, _percent: progress.append(message),
            )
            self.assertEqual(run.report_language, "en")
            self.assertEqual(len(provider.calls), 10)
            stage_calls = [
                item for item in provider.calls
                if json.loads(item[1]).get("agent")
            ]
            self.assertEqual(len(stage_calls), 9)
            for system_prompt, user_prompt in stage_calls:
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
                return {"kind": "opinion", "analysis": "繁體中文輸出", "claims": []}

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
                filing_evidence=_disclosure_fixture(),
                progress=lambda message, _percent: progress.append(message),
            )
            self.assertEqual(run.report_language, "zh-Hant")
            self.assertEqual(len(provider.calls), 10)
            stage_calls = [
                item for item in provider.calls
                if json.loads(item[1]).get("agent")
            ]
            self.assertEqual(len(stage_calls), 9)
            for system_prompt, user_prompt in stage_calls:
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

    def test_parallel_agent_switch_controls_effective_request_concurrency(self) -> None:
        config = ModelConfig(
            configured_model_id=f"test.parallel.agent.switch.{uuid.uuid4().hex}",
            role="primary",
        )

        class ConcurrencyProvider:
            def __init__(self, parallel: bool) -> None:
                self.parallel = parallel
                self.active = 0
                self.maximum = 0
                self.barrier_calls = 0
                self.barrier_broken = False
                self.barrier = threading.Barrier(2)
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, _user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                wait_for_peer = False
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                    if self.parallel and self.barrier_calls < 2:
                        self.barrier_calls += 1
                        wait_for_peer = True
                if wait_for_peer:
                    try:
                        self.barrier.wait(timeout=3)
                    except threading.BrokenBarrierError:
                        self.barrier_broken = True
                with self.lock:
                    self.active -= 1
                return {"claims": []}

        def run_with(parallel: bool) -> tuple[int, bool]:
            Path("D:/githubmax/tmp").mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
                provider = ConcurrencyProvider(parallel)
                storage = Storage(Path(directory))
                storage.save_company(DEMO_COMPANY)
                storage.save_facts([FinancialFact(**item) for item in demo_facts()])
                workflow = ResearchWorkflow(
                    storage,
                    builtin_pack(),
                    provider,
                    config,
                    parallel_agents=parallel,
                )
                workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
                return provider.maximum, provider.barrier_broken

        # The graph enforces single-worker execution when disabled. The shared
        # provider scheduler allows the approved two-request ceiling when the
        # graph is enabled.
        self.assertEqual(run_with(False), (1, False))
        self.assertEqual(run_with(True), (2, False))

    def test_parallel_base_agent_failure_does_not_retry_or_serialize_siblings(self) -> None:
        class RetryProvider:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.calls: dict[str, int] = {}
                self.concurrent_base_agents: set[str] = set()
                self.base_barrier = threading.Barrier(2)
                self.base_barrier_broken = False
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                agent = str(json.loads(user_prompt)["agent"])
                wait_at_barrier = False
                with self.lock:
                    self.calls[agent] = self.calls.get(agent, 0) + 1
                    call_number = self.calls[agent]
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                    if (
                        agent != "financial-analyst"
                        and call_number == 1
                        and len(self.concurrent_base_agents) < 2
                        and agent not in self.concurrent_base_agents
                    ):
                        self.concurrent_base_agents.add(agent)
                        wait_at_barrier = True
                if wait_at_barrier:
                    try:
                        self.base_barrier.wait(timeout=3)
                    except threading.BrokenBarrierError:
                        self.base_barrier_broken = True
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

        Path("D:/githubmax/tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
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
                filing_evidence=_disclosure_fixture(),
                progress=lambda message, percent: progress.append((message, percent)),
            )

            self.assertEqual(run.status, RunStatus.PARTIAL)
            # The failed request is not retried implicitly; its healthy
            # siblings still use the two-slot shared scheduler.
            self.assertEqual(provider.maximum, 2)
            self.assertEqual(len(provider.concurrent_base_agents), 2)
            self.assertFalse(provider.base_barrier_broken)
            self.assertEqual(provider.calls["financial-analyst"], 1)
            outcomes = [
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "stage-outcome"
            ]
            self.assertTrue(any(item["content"].get("agent_id") == "financial-analyst" for item in outcomes))
            self.assertEqual(provider.calls["business-analyst"], 1)
            self.assertEqual(provider.calls["accounting-risk-analyst"], 1)
            self.assertFalse(any("单独重试" in message for message, _ in progress))

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
                workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
            self.assertTrue(caught.exception.run_id)
            self.assertEqual(provider.count, 0)
            self.assertEqual(
                storage.list_runs()[0]["status"], RunStatus.CANCELLED.value
            )

    def test_claim_contract_hydrates_unique_canonical_fact(self) -> None:
        evidence = {
            "fact:revenue": {
                "evidence_id": "fact:revenue", "kind": "financial_fact",
                "concept": "revenue", "value": 120.0, "unit": "CNY",
                "fiscal_year": 2025, "fiscal_period": "FY",
                "end_date": "2025-12-31", "scope": "consolidated",
                "raw_text": "营业收入 120",
            }
        }
        output = {"claims": [{
            "kind": "fact", "text": "营业收入为120元。",
            "evidence_ids": ["fact:revenue"],
        }]}
        verification = verify_agent_output(output, set(evidence), "zh-CN", evidence)
        self.assertTrue(verification["passed"], verification["issues"])
        normalized = verification["normalized_claims"][0]
        self.assertEqual(normalized["concept"], "revenue")
        self.assertEqual(normalized["deterministic_fields_source"], "unique_canonical_evidence")

    def test_unrelated_direction_words_are_not_a_contradiction(self) -> None:
        evidence = {
            "filing:expense": {
                "evidence_id": "filing:expense", "kind": "filing_text",
                "raw_text": "销售费用同比下降10%。",
            }
        }
        verification = verify_agent_output({"claims": [{
            "kind": "inference", "text": "营业收入同比增长8%。",
            "evidence_ids": ["filing:expense"],
        }]}, set(evidence), "zh-CN", evidence)
        self.assertNotEqual(verification["claim_verifications"][0]["state"], "contradicted")

    def test_partial_trust_preserves_narrative_around_verified_claims(self) -> None:
        result = {
            "summary": "Verified revenue supports the current scale assessment.",
            "claims": [
                {"kind": "fact", "text": "Revenue", "evidence_ids": ["fact:1"]},
                {"kind": "fact", "text": "Invented", "evidence_ids": ["fact:missing"]},
            ],
        }
        trusted = _trusted_stage_result(result, {
            "passed": False,
            "claim_verifications": [
                {"index": 0, "state": "numeric_verified"},
                {"index": 1, "state": "insufficient_evidence"},
            ],
            "normalized_claims": result["claims"],
        })
        self.assertEqual(trusted["summary"], result["summary"])
        self.assertEqual(len(trusted["claims"]), 1)

    def test_coverage_isolates_only_cross_partition_claim(self) -> None:
        content = {"claims": [
            {"text": "valid", "evidence_ids": ["fact:local"]},
            {"text": "invalid", "evidence_ids": ["fact:other"]},
        ], "observations": ["retained"]}
        safe, isolated = _isolate_unknown_evidence_references(content, {"fact:local"})
        self.assertEqual([item["text"] for item in safe["claims"]], ["valid"])
        self.assertEqual(safe["observations"], ["retained"])
        self.assertEqual(isolated, ["fact:other"])

    def test_prompt_and_verifier_share_claim_contract(self) -> None:
        _system, user = _agent_prompt_bundle("agent", "role", "en", "{}", {})
        payload = json.loads(user)
        self.assertEqual(payload["claim_contract"]["schema"], "openthesis.claim.v2")


if __name__ == "__main__":
    unittest.main()
