"""Captured metadata must survive the real, offline workflow entry."""
from copy import deepcopy
import json

import pytest

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import ResearchRun
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow
from openthesis.storage import Storage


@pytest.fixture
def captured_run(tmp_path):
    store = Storage(tmp_path / "capture-store")
    store.save_company(DEMO_COMPANY)
    pack = builtin_pack()
    envelope = ResearchRun("capture-run", deepcopy(DEMO_COMPANY), "wf", pack.pack_id,
        pack.version, "old-provider", "old-model", "2026-10-06", report_language="zh-CN")
    envelope.research_configuration = {"historical_extension": {"preserve": True}}
    store.save_run(envelope)
    reproducibility = {
        "data_snapshot": {"disclosure_coverage": {"requested_count": 5, "complete": False},
                          "official_source_state": {"freshness": "freshness_unverified"}},
        "research_configuration": {"parallel_agents": False, "annual_history_years": 7,
                                   "vision_policy": {"enabled": False},
                                   "valuation_policy": {"source": "manual_override"}},
    }
    workflow = ResearchWorkflow(store, pack, None, ModelConfig(), report_language="en",
                                parallel_agents=False)
    result = workflow.run(DEMO_COMPANY, demo_facts(), reproducibility=reproducibility,
                          existing_run=envelope)
    return store, result, reproducibility


def test_existing_envelope_keeps_the_actual_captured_configuration(captured_run):
    store, result, reproducibility = captured_run
    assert result.research_configuration["annual_history_years"] == 7
    assert result.research_configuration["valuation_policy"] == {"source": "manual_override"}
    assert result.research_configuration["vision_policy"] == {"enabled": False}
    assert result.research_configuration["historical_extension"] == {"preserve": True}
    assert result.report_language == "en"
    assert (result.provider_id, result.model_id) == ("none", "")
    assert result.model_configuration == {
        "configured_model_id": "", "configuration_version": 1, "role": "primary"}
    persisted = json.loads(store.get_run(result.run_id)["payload_json"])
    assert persisted["research_configuration"] == result.research_configuration
    assert persisted["data_snapshot"] == reproducibility["data_snapshot"]


def test_caller_mutation_cannot_change_persisted_material_identity(captured_run):
    store, result, reproducibility = captured_run
    expected = deepcopy(result.data_snapshot)
    reproducibility["data_snapshot"]["official_source_state"]["freshness"] = "changed-after-run"
    assert result.data_snapshot == expected
    assert json.loads(store.get_run(result.run_id)["payload_json"])["data_snapshot"] == expected


def test_lifecycle_metadata_is_not_research_completion(captured_run):
    _store, result, _reproducibility = captured_run
    # This fixture has no model: metadata preservation must never make a
    # deterministic-only report count as complete qualitative research.
    assert result.status.value == "partial"
