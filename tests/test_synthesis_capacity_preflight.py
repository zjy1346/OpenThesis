from __future__ import annotations

import json

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun, RunStatus
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import (
    ResearchContext,
    ResearchWorkflow,
    SynthesisContextLimitError,
    _agent_input_size,
)
from openthesis.research_execution.model import ProviderContextCapability
from openthesis.storage import Storage


def _disclosure_fixture() -> list[dict[str, str]]:
    return [
        {
            "evidence_id": f"filing:{topic}",
            "kind": "filing_excerpt",
            "document_id": "demo-annual",
            "source_url": "https://example.test/annual",
            "title": f"Annual · {topic}",
            "excerpt": text,
        }
        for topic, text in (
            ("business", "The company sells equipment to industrial customers."),
            ("management_discussion", "Management plans new products and capacity investment."),
            ("risk_factors", "Customer concentration and refinancing expose the business to risk."),
            ("audit", "Independent auditor reports an unqualified opinion on the consolidated statements."),
        )
    ]


class RecordingCoverageProvider:
    """Offline provider that records the exact bytes admitted to each call."""

    def __init__(self, max_input_bytes: int | None) -> None:
        self.max_input_bytes = max_input_bytes
        self.requests: list[dict[str, object]] = []

    def generate(
        self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
    ) -> dict[str, object]:
        payload = json.loads(user_prompt)
        self.requests.append(
            {
                "payload": payload,
                "bytes": len(system_prompt.encode("utf-8"))
                + len(user_prompt.encode("utf-8")),
            }
        )
        if payload.get("target_agent"):
            material = payload.get("material")
            evidence_ids: list[str] = []
            if isinstance(material, dict):
                evidence_ids = sorted(
                    {
                        str(item["evidence_id"])
                        for item in material.get("evidence", [])
                        if isinstance(item, dict) and item.get("evidence_id")
                    }
                )
                for value in material.values():
                    if isinstance(value, dict):
                        evidence_ids.extend(
                            str(item["evidence_id"])
                            for item in value.get("evidence", [])
                            if isinstance(item, dict) and item.get("evidence_id")
                        )
            return {
                "claims": [
                    {"text": "Partition reviewed without dropping evidence.", "kind": "inference", "evidence_ids": []}
                ],
                "source_evidence_ids": sorted(set(evidence_ids)),
            }
        return {"claims": []}


def _scenario(tmp_path, provider: RecordingCoverageProvider):
    storage = Storage(tmp_path / "capacity-store")
    storage.save_company(DEMO_COMPANY)
    pack = builtin_pack()
    run = ResearchRun(
        run_id="coverage-preflight",
        company=DEMO_COMPANY,
        workflow_id="long-term-fundamentals",
        research_pack_id=pack.pack_id,
        research_pack_version=pack.version,
        provider_id="offline-test",
        model_id="capacity-test",
        data_as_of="2026-10-04",
        status=RunStatus.RUNNING,
    )
    storage.save_run(run)
    workflow = ResearchWorkflow(
        storage,
        pack,
        provider,
        ModelConfig(configured_model_id="test.capacity-preflight", role="primary"),
    )
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
        "scenarios": [{"name": "base"}],
    }
    return storage, run, workflow, context, prior


def _call(workflow, context, prior, run):
    return workflow._run_agent_adaptive(
        "research-synthesizer",
        "prompts/research-synthesizer.md",
        context.compact_json(),
        prior,
        run=run,
    )


def _coverage_checkpoints(storage: Storage, run: ResearchRun):
    return [
        item
        for item in storage.get_artifacts(run.run_id)
        if item["artifact_type"] == "stage-material-coverage"
    ]


def test_derived_provider_capacity_bounds_requests_and_preserves_partition_checkpoints(tmp_path):
    provider = RecordingCoverageProvider(max_input_bytes=None)
    storage, run, workflow, context, prior = _scenario(tmp_path, provider)
    context_json = context.compact_json()
    direct_request_bytes = _agent_input_size(
        "research-synthesizer",
        workflow.pack.prompt("prompts/research-synthesizer.md"),
        workflow.report_language,
        context_json,
        prior,
    )
    provider.max_input_bytes = direct_request_bytes - 1

    try:
        _call(workflow, context, prior, run)
    except SynthesisContextLimitError as error:
        assert error.available_bytes == provider.max_input_bytes
        assert error.required_bytes > error.available_bytes
    assert provider.requests
    assert direct_request_bytes == provider.max_input_bytes + 1
    assert all(item["bytes"] <= provider.max_input_bytes for item in provider.requests)

    checkpoints = _coverage_checkpoints(storage, run)
    assert checkpoints, "completed material partitions must be durable before preflight fails"
    checkpointed_ids = {
        evidence_id
        for item in checkpoints
        for evidence_id in item["content"]["capsule"]["source_evidence_ids"]
    }
    expected_ids = {"fact:test", *(item["evidence_id"] for item in _disclosure_fixture())}
    assert expected_ids.issubset(checkpointed_ids)


def test_minimum_merge_gate_uses_exact_serialized_utf8_request_bytes(tmp_path):
    provider = RecordingCoverageProvider(max_input_bytes=None)
    _storage, _run_value, workflow, context, _prior = _scenario(tmp_path, provider)
    context_json = context.compact_json()
    prompt = workflow.pack.prompt("prompts/research-synthesizer.md")
    ledger = {
        "schema": "openthesis.stage-coverage.v1",
        "complete": True,
        "partition_count": 0,
        "covered_evidence_ids": [],
    }
    merge_prior = {"coverage_ledger": ledger, "coverage_capsules": []}
    required_bytes = _agent_input_size(
        "research-synthesizer", prompt, workflow.report_language, context_json, merge_prior,
    )

    with pytest.raises(SynthesisContextLimitError) as below_boundary:
        workflow._consolidate_coverage_capsules(
            agent_id="research-synthesizer",
            role_prompt=prompt,
            capsules=[],
            lean_context_json=context_json,
            compact_ledger=ledger,
            capability=ProviderContextCapability(explicit_max_input_bytes=required_bytes - 1),
            input_overhead_bytes=0,
        )
    assert below_boundary.value.available_bytes == required_bytes - 1
    assert below_boundary.value.required_bytes == required_bytes

    assert workflow._consolidate_coverage_capsules(
        agent_id="research-synthesizer",
        role_prompt=prompt,
        capsules=[],
        lean_context_json=context_json,
        compact_ledger=ledger,
        capability=ProviderContextCapability(explicit_max_input_bytes=required_bytes),
        input_overhead_bytes=0,
    ) == ([], 0)
    assert provider.requests == []

