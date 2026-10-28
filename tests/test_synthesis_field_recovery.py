"""Actual synthesis request wiring, not protocol repair as report acceptance."""
import json

import pytest

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import (
    ResearchContext, ResearchWorkflow, build_fact_evidence, validate_research_synthesis,
)
from openthesis.research_execution.registry import standard_model_registry

from test_model_request_journal import _journal, _store
from test_text_format_recovery import OfflineWire
from test_research_workflow import _disclosure_fixture, _valid_research_synthesis_output


def _scenario(tmp_path, responses):
    store = _store(tmp_path)
    facts = demo_facts()
    evidence = build_fact_evidence(facts) + _disclosure_fixture()
    context = ResearchContext(DEMO_COMPANY, facts, [], [], evidence)
    provider = OfflineWire(responses)
    workflow = ResearchWorkflow(store, builtin_pack(), provider,
        ModelConfig(configured_model_id="offline-model"))
    journal = _journal(store)
    generation = journal.prepare(standard_model_registry(), "frozen-material")["final-synthesis"]
    return store, context, provider, workflow, journal, generation


def _synthesize(context, workflow, journal, generation):
    with journal.bind("final-synthesis", generation), workflow._bind_request_journal(journal):
        return workflow._run_synthesis_with_budget(context, {}, {}, {}, {})


def test_actual_synthesis_request_repairs_only_missing_section_and_preserves_other_body(tmp_path):
    original = {**_valid_research_synthesis_output(), "structured_output_valid": True,
                "extension": {"risk_text": "Keep this original extension."}}
    del original["financial_quality"]
    patch = {"section_patches": {"financial_quality": "Synthetic financial quality."},
             "structured_output_valid": True}
    store, context, provider, workflow, journal, generation = _scenario(tmp_path, [original, patch])
    result = _synthesize(context, workflow, journal, generation)
    assert len(provider.wires) == 2
    assert {key: value for key, value in result.items()
            if key not in {"_stage_material_manifest", "_material_adequacy"}} == {
        **original, "financial_quality": patch["section_patches"]["financial_quality"]}
    first, repair = provider.wires
    assert repair["system_prompt"] == first["system_prompt"]
    assert not repair["user_prompt"].startswith(first["user_prompt"])
    narrowed = json.loads(repair["user_prompt"].split('\n\n[OpenThesis')[0])
    assert narrowed['target_fields'] == ['financial_quality']
    assert narrowed['research_context']['accepted_facts']
    assert "model-field-repair-v1" in repair["user_prompt"]
    assert result["claims"] == original["claims"]
    assert _synthesize(context, workflow, journal, generation) == result
    assert len(provider.wires) == 2
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM report_revisions").fetchone()[0] == 0
        saved = json.loads(db.execute("SELECT result_json FROM recovery_actions").fetchone()[0])["candidate"]
    assert saved["original_result"] == original and saved["result"] == patch


@pytest.mark.parametrize("text", [
    "Revenue was USD 42 billion [fact:808ac9481ae812762bdc728c].",
    "Revenue was USD 42 billion [fact:invented-source].",
])
def test_repaired_section_with_wrong_amount_or_unknown_evidence_is_not_accepted(tmp_path, text):
    original = {**_valid_research_synthesis_output(), "structured_output_valid": True}
    del original["financial_quality"]
    patch = {"section_patches": {"financial_quality": text}, "structured_output_valid": True}
    _store_value, context, provider, workflow, journal, generation = _scenario(tmp_path, [original, patch])
    result = _synthesize(context, workflow, journal, generation)
    assert len(provider.wires) == 2
    records = {item["evidence_id"]: item for item in context.evidence}
    verification = validate_research_synthesis(result, set(records), evidence_records=records,
                                              coverage_context=context)
    assert verification["passed"] is False
    audit = verification["section_validation"]
    assert any("financial_quality" in item["path"] for item in
               [*audit["quarantined_paths"], *audit.get("pending_paths", [])])
    assert result["claims"] == original["claims"]


def test_complete_synthesis_shape_keeps_one_original_request(tmp_path):
    original = {**_valid_research_synthesis_output(), "structured_output_valid": True}
    _store_value, context, provider, workflow, journal, generation = _scenario(tmp_path, [original])
    result = _synthesize(context, workflow, journal, generation)
    assert {key: value for key, value in result.items()
            if key not in {"_stage_material_manifest", "_material_adequacy"}} == original
    assert len(provider.wires) == 1
