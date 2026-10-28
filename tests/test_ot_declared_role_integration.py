"""Compiled OT packages retain node identity while enforcing declared roles."""
import json

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import RunStatus
from openthesis.ot import compile_studio_draft, minimal_studio_draft
from openthesis.packs import builtin_pack, load_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow
from openthesis.research_assertions import VERSION as SOURCE_BOUND_ASSERTIONS_VERSION
from openthesis.storage import Storage

from test_research_workflow import (
    _disclosure_fixture, _valid_research_synthesis_output, _verified_demo_revenue_claim,
)
from test_text_format_recovery import OfflineWire


def _workflow(tmp_path, responses, *, schema="ot://openthesis/agent-artifact/1"):
    draft = minimal_studio_draft()
    draft["workflow"]["steps"] = [{
        "id": "custom-combine", "role": "research-synthesizer", "depends_on": [],
        "output_schema": schema,
        "prompt": builtin_pack().prompt("prompts/research-synthesizer.md"),
    }]
    package_bytes, _ = compile_studio_draft(draft)
    package_path = tmp_path / "custom.ot"
    package_path.write_bytes(package_bytes)
    store = Storage(tmp_path / "data")
    store.save_company(DEMO_COMPANY)
    provider = OfflineWire(responses)
    workflow = ResearchWorkflow(store, load_pack(package_path), provider,
        ModelConfig(configured_model_id="offline-model"), report_language="en")
    return store, provider, workflow


def _raw_step(store, run_id):
    return next(item["content"] for item in store.get_artifacts(run_id)
                if item["artifact_type"] == "ot-agent-analysis")


def test_compiled_custom_synthesis_uses_same_field_repair_and_keeps_node_identity(tmp_path):
    original = {**_valid_research_synthesis_output(), "structured_output_valid": True,
                "extension": {"risk": "Preserve the complete original extension."}}
    del original["financial_quality"]
    patch = {"structured_output_valid": True,
             "section_patches": {"financial_quality": "Repaired financial quality."}}
    store, provider, workflow = _workflow(tmp_path, [original, patch])
    run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
    assert len(provider.wires) == 3
    # One separate, source-bound batch validates text that shape/amount
    # checking cannot prove. It is not another synthesis or field repair.
    assert json.loads(provider.wires[2]["user_prompt"])["protocol"] == SOURCE_BOUND_ASSERTIONS_VERSION
    step = _raw_step(store, run.run_id)
    result = step["result"]
    assert {key: value for key, value in result.items()
            if key not in {"_stage_material_manifest", "_material_adequacy"}} == {
        **original, "financial_quality": patch["section_patches"]["financial_quality"]}
    assert step["step_id"] == "custom-combine"
    assert result["_stage_material_manifest"]["stage"] == "custom-combine"
    first = json.loads(provider.wires[0]["user_prompt"])
    assert first["agent"] == "custom-combine"
    assert first["agent_role"] == "research-synthesizer"
    assert "research_coverage_contract" in first
    assert provider.wires[1]["user_prompt"].startswith(provider.wires[0]["user_prompt"])
    with store.connect() as db:
        assert {row[0] for row in db.execute("SELECT stage_id FROM model_request_journal")} == {"custom-combine"}
        saved = json.loads(db.execute("SELECT result_json FROM recovery_actions WHERE external_intent=1").fetchone()[0])
    assert saved["candidate"]["original_result"] == original
    assert saved["candidate"]["result"] == patch
    # A shape repair cannot manufacture the remaining substantive obligations.
    assert run.status == RunStatus.PARTIAL


def test_compiled_custom_role_cannot_replace_missing_disclosure_with_model_memory(tmp_path):
    store, provider, workflow = _workflow(tmp_path, [{"structured_output_valid": True,
        "business_model": {"summary": "The company sells remembered insurance products.",
                           "evidence_ids": ["filing:business"]}, "claims": []}])
    disclosures = [item for item in _disclosure_fixture() if item["evidence_id"] != "filing:business"]
    run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=disclosures)
    # Missing a topic cannot veto an entire role with other source material.
    assert len(provider.wires) == 2  # original plus one bounded shape repair
    candidate = _raw_step(store, run.run_id)["result"]
    assert candidate["business_model"]["summary"] == "The company sells remembered insurance products."
    assert candidate["_field_repair"]["code"] == "MODEL_REPAIR_LOCAL_PROCESSING_FAILED"
    checkpoints = [item["content"] for item in store.get_artifacts(run.run_id) if item["artifact_type"] == "stage-checkpoint"]
    assert all("remembered insurance" not in json.dumps(item.get("trusted_output", {})) for item in checkpoints)
    assert run.status == RunStatus.PARTIAL


def test_diagnostic_schema_does_not_pay_to_rewrite_itself_as_a_final_report(tmp_path):
    original = {"structured_output_valid": True, "claims": [_verified_demo_revenue_claim()], "issues": []}
    store, provider, workflow = _workflow(tmp_path, [original], schema="ot://openthesis/diagnostic/1")
    run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
    assert len(provider.wires) == 2
    assert json.loads(provider.wires[1]["user_prompt"])["protocol"] == SOURCE_BOUND_ASSERTIONS_VERSION
    step = _raw_step(store, run.run_id)
    assert step["output_schema"] == "ot://openthesis/diagnostic/1"
    assert "section_patches" not in step["result"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions WHERE external_intent=1").fetchone()[0] == 0
    assert run.status == RunStatus.PARTIAL
