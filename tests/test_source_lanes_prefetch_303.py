"""Regression coverage for source-independent industry and risk lanes."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import ResearchRun
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import standard_model_registry
from openthesis.storage import Storage
from test_research_workflow import SevenStageProvider, _disclosure_fixture


SOURCE_INDUSTRY = "source-industry-analysis"
SOURCE_RISK = "source-risk-analysis"
SOURCE_ROOTS = {SOURCE_INDUSTRY, SOURCE_RISK}


def _source_evidence(*, revenue: int = 1000) -> list[dict]:
    return [
        {
            "evidence_id": "filing:annual:business",
            "kind": "filing_text",
            "raw_text": "The company sells industrial equipment and provides maintenance services.",
            "document_id": "official-annual-2025",
            "content_hash": "a" * 64,
            "source_url": "https://example.invalid/annual-2025.pdf",
            "page": 12,
        },
        {
            "evidence_id": "fact:revenue:2025",
            "kind": "financial_fact",
            "concept": "revenue",
            "value": revenue,
            "currency": "CNY",
            "document_id": "official-annual-2025",
            "content_hash": "a" * 64,
            "page": 56,
        },
        {
            "evidence_id": "compiled-binding:2025",
            "kind": "compiled_disclosure_binding",
            "facts_digest": "derived-financial-digest",
            "currency": "CNY",
        },
    ]


def _verified(_spec, candidate, _inputs):
    return StageValidation(True, deepcopy(candidate), "completed_verified")


def test_registry_adds_source_roots_without_financial_capability_or_replacing_late_stages():
    registry = standard_model_registry()
    specs = {spec.stage_id: spec for spec in registry.stages}

    assert SOURCE_ROOTS <= specs.keys()
    assert {"financial-analyst", "business-analyst", "accounting-risk-analyst",
            "growth-opportunities", "counter-analysis", "forecast-scenarios",
            "final-synthesis"} <= specs.keys()

    assert specs[SOURCE_INDUSTRY].role == "growth-opportunity-analyst"
    assert specs[SOURCE_INDUSTRY].prompt_path == "prompts/growth-opportunity-analyst.md"
    assert specs[SOURCE_RISK].role == "skeptical-analyst"
    assert specs[SOURCE_RISK].prompt_path == "prompts/skeptical-analyst.md"
    for stage_id in SOURCE_ROOTS:
        spec = specs[stage_id]
        assert spec.depends_on == ()
        assert "company_identity" in spec.required_capabilities
        assert "sourced_disclosures" in spec.any_capabilities
        assert "quantitative_facts" not in spec.required_capabilities
        assert "sourced_financial_observations" not in spec.required_capabilities


def test_prefetch_source_stages_targets_business_and_new_roots_with_source_only_context():
    class StorageSpy:
        def capture_research_publication(self, run):
            return SimpleNamespace(input_digest="source-lane-input")

        def get_artifacts(self, _run_id):
            return []

    workflow = ResearchWorkflow(
        StorageSpy(), builtin_pack(), object(), ModelConfig("source-lane-model")
    )
    run = ResearchRun(
        "source-lane-prefetch",
        DEMO_COMPANY,
        "workflow",
        builtin_pack().pack_id,
        builtin_pack().version,
        "source-lane-model",
        "source-lane-model",
        "2026-10-13",
    )
    captured = []

    def capture(context, captured_run, _artifacts, **kwargs):
        captured.append((context, captured_run, kwargs))
        return {}, kwargs["publication"]

    workflow._execute_official_model_graph = capture
    workflow.prefetch_source_stages(run, _source_evidence())

    assert len(captured) == 1
    context, captured_run, kwargs = captured[0]
    assert captured_run.run_id == run.run_id
    assert set(kwargs["stage_targets"]) == {"business-analyst", *SOURCE_ROOTS}
    assert context.facts == []
    assert context.metrics == []
    assert context.interim_metrics == []
    assert all(item.get("kind") == "filing_text" for item in context.evidence)
    assert {item["evidence_id"] for item in context.evidence} == {"filing:annual:business"}


def test_source_root_producers_are_reused_when_only_financial_inputs_change_and_feed_downstream():
    registry = standard_model_registry()
    calls = Counter()
    observed_inputs = {}
    observed_dependencies = {}

    def produce(spec, stage_inputs, dependencies):
        calls[spec.stage_id] += 1
        observed_inputs.setdefault(spec.stage_id, deepcopy(stage_inputs))
        observed_dependencies[spec.stage_id] = set(dependencies)
        return {
            "analysis": f"verified {spec.stage_id}",
            "evidence_ids": ["filing:annual:business"],
            "stage_id": spec.stage_id,
        }

    validators = {spec.validator_id: _verified for spec in registry.stages}
    executor = StageExecutor({"model-analysis": produce}, validators)
    source_inputs = {
        "context_json": '{"company":{"cik":"CNTEST"},"evidence":[{"evidence_id":"filing:annual:business"}]}',
        "available": ["filing:annual:business"],
        "capabilities": ["company_identity", "sourced_disclosures"],
    }
    full_inputs = {
        **source_inputs,
        "context_json": '{"company":{"cik":"CNTEST"},"facts":[{"concept":"revenue","value":1000}],"metrics":{"revenue":1000}}',
        "capabilities": ["company_identity", "quantitative_facts", "sourced_disclosures"],
        "financial_generation": "generation-a",
    }
    stage_inputs = {
        spec.stage_id: deepcopy(source_inputs if spec.stage_id in {"business-analyst", *SOURCE_ROOTS}
                                else full_inputs)
        for spec in registry.stages
    }
    first = executor.execute(
        registry,
        full_inputs,
        stage_inputs=stage_inputs,
        execution_fingerprint="source-lane-model-v1",
        max_workers=2,
    )

    changed_full_inputs = deepcopy(full_inputs)
    changed_full_inputs["financial_generation"] = "generation-b"
    changed_full_inputs["context_json"] = (
        '{"company":{"cik":"CNTEST"},"facts":[{"concept":"revenue","value":2000}],'
        '"metrics":{"revenue":2000},"currency":"USD"}'
    )
    changed_stage_inputs = {
        spec.stage_id: deepcopy(source_inputs if spec.stage_id in {"business-analyst", *SOURCE_ROOTS}
                                else changed_full_inputs)
        for spec in registry.stages
    }
    second = executor.execute(
        registry,
        changed_full_inputs,
        previous=first,
        stage_inputs=changed_stage_inputs,
        execution_fingerprint="source-lane-model-v1",
        max_workers=2,
    )

    assert all(calls[stage_id] == 1 for stage_id in {"business-analyst", *SOURCE_ROOTS})
    for stage_id in SOURCE_ROOTS:
        assert "financial_generation" not in observed_inputs[stage_id]
        assert "facts" not in observed_inputs[stage_id]["context_json"]
    downstream_keys = {
        key
        for stage_id, keys in observed_dependencies.items()
        if stage_id not in SOURCE_ROOTS
        for key in keys
    }
    assert SOURCE_ROOTS <= downstream_keys
    assert all(second[stage_id].usable for stage_id in SOURCE_ROOTS | {"business-analyst"})


def test_real_research_workflow_scopes_source_roots_and_persists_them_in_dossier(tmp_path):
    """Exercise the production graph seam, not a hand-built stage-input map."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        storage = Storage(Path(directory))
        storage.save_company(DEMO_COMPANY)
        class SourceAwareProvider(SevenStageProvider):
            def generate(self, system_prompt, user_prompt, *, json_mode=True):
                payload = json.loads(user_prompt)
                prior = payload.get("prior_artifacts", {})
                lane = prior.get("source_research_lane") if isinstance(prior, dict) else None
                if lane:
                    self.calls.append(lane)
                    if lane == SOURCE_INDUSTRY:
                        text = "The company sells equipment to industrial customers."
                        evidence_id = "filing:business"
                    else:
                        text = "Customer concentration and refinancing expose the business to risk."
                        evidence_id = "filing:risk_factors"
                    return {
                        "analysis": text,
                        "claims": [{"kind": "inference", "text": text,
                                    "evidence_ids": [evidence_id]}],
                    }
                return super().generate(system_prompt, user_prompt, json_mode=json_mode)

        provider = SourceAwareProvider()
        workflow = ResearchWorkflow(
            storage,
            builtin_pack(),
            provider,
            ModelConfig(configured_model_id="source-lane-real-model", role="primary"),
            parallel_agents=False,
        )
        captured_stage_inputs = []
        original_execute = StageExecutor.execute

        def spy_execute(executor, registry, inputs, *args, **kwargs):
            captured_stage_inputs.append(deepcopy(kwargs.get("stage_inputs", {})))
            return original_execute(executor, registry, inputs, *args, **kwargs)

        with patch("openthesis.research_execution.production.StageExecutor.execute", new=spy_execute):
            source_evidence = [
                {**item, "kind": "filing_text", "raw_text": item["excerpt"],
                 "company_cik": DEMO_COMPANY.cik, "end_date": "2025-12-31",
                 "fiscal_year": 2025, "fiscal_period": "FY"}
                for item in _disclosure_fixture()
            ]
            run = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                filing_evidence=source_evidence,
            )

        assert captured_stage_inputs
        scoped = captured_stage_inputs[-1]
        assert SOURCE_ROOTS <= scoped.keys()
        for stage_id in SOURCE_ROOTS | {"business-analyst"}:
            source_context = scoped[stage_id]["context_json"]
            assert '"facts"' not in source_context
            assert '"metrics"' not in source_context
            assert '"financial_generation"' not in source_context
            assert "filing:business" in source_context
            assert "filing:risk_factors" in source_context

        artifacts = storage.get_artifacts(run.run_id)
        dossier = next(
            artifact for artifact in artifacts
            if artifact.get("artifact_type") == "verified-research-dossier"
        )
        analyses = dossier["content"].get("analyses", {})
        assert SOURCE_ROOTS <= analyses.keys()
        assert provider.calls.count("business-analyst") == 1
        assert provider.calls.count(SOURCE_INDUSTRY) == 1
        assert provider.calls.count(SOURCE_RISK) == 1
        assert provider.calls.count("growth-opportunity-analyst") == 1
        assert provider.calls.count("skeptical-analyst") == 1

        changed_facts = deepcopy(demo_facts())
        changed_facts[0]["value"] = changed_facts[0].get("value", 0) + 123
        before = Counter(provider.calls)
        workflow.run(
            DEMO_COMPANY,
            changed_facts,
            filing_evidence=source_evidence,
            existing_run=run,
            resume_artifacts=artifacts,
        )
        after = Counter(provider.calls)
        assert after["business-analyst"] == before["business-analyst"]
        assert after[SOURCE_INDUSTRY] == before[SOURCE_INDUSTRY]
        assert after[SOURCE_RISK] == before[SOURCE_RISK]
