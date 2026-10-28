"""Resume requests keep persisted run identity separate from a new model choice."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import FinancialFact, ResearchArtifact, ResearchRun, RunStatus
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow
from openthesis.service import AppService


def _old_run(run_id: str = "resume-identity") -> ResearchRun:
    run = ResearchRun(
        run_id, deepcopy(DEMO_COMPANY), "workflow", "pack", "1", "gateway", "old-model",
        "2026-10-07", RunStatus.PARTIAL,
    )
    run.model_configuration = {
        "configured_model_id": "old-model",
        "configuration_version": 1,
        "role": "primary",
    }
    run.research_configuration = {"parallel_agents": False}
    run.data_snapshot = {"facts_generation": "old-generation"}
    return run


def _report(run: ResearchRun, suffix: str = "old-report") -> ResearchArtifact:
    return ResearchArtifact(
        f"{run.run_id}:report:{suffix}", run.run_id, "research-report", "Report",
        {"report": {"claims": [{"text": suffix}]}},
        model_id=run.model_id, agent_id="synthesis",
    )


def _fact() -> FinancialFact:
    return FinancialFact(
        "resume-fact", DEMO_COMPANY.cik, "revenue", "Revenue", 100.0, "USD", 2025,
        "FY", "DEMO", "2025-01-01", "2025-12-31", "2026-02-01", "resume-accession",
        "openthesis://resume", statement="income_statement", scope="consolidated",
        consolidated_scope="consolidated", currency="USD", unit_scale=1.0,
        unit_provenance="structured_normalized", source_document="resume-source",
        source_page=1, raw_text="Revenue 100", parser_version="fixture",
        validation_status="VERIFIED", extraction_status="extracted",
        usage_status="canonical_research", provenance_status="verified",
    )


def _context(store, revision_id: str):
    with store.connect() as db:
        row = db.execute(
            "SELECT context_json,context_sha256 FROM report_revision_contexts WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
    return tuple(row) if row is not None else None


@pytest.mark.parametrize("target", ["synthesis", "growth"])
def test_service_resume_keeps_old_run_identity_and_separates_new_model(tmp_path, target):
    provider_calls: list[object] = []
    service = AppService(
        tmp_path / "service",
        provider_factory=lambda config: provider_calls.append(config) or object(),
    )
    service.storage.save_company(DEMO_COMPANY)
    run = _old_run()
    service.storage.save_run(run)
    service.storage.save_artifact(_report(run))
    old_revision = service.storage.latest_report_revision(run.run_id)
    old_context = _context(service.storage, old_revision["revision_id"])
    captured = []

    class FakeOrchestrator:
        def resume(self, request):
            captured.append(request)
            # This is the identity CAS seam: a request carrying a new model
            # must still carry the persisted run envelope at this point.
            service.storage.capture_research_publication(request.run)
            return request.run

    service._research_orchestrator = FakeOrchestrator()
    service._select_pack = lambda _pack_id: builtin_pack()
    service.get_report = lambda run_id, **_kwargs: {"run_id": run_id, "dispatched": True}

    new_model = {
        "configured_model_id": "new-model",
        "configuration_version": 2,
        "role": "primary",
    }
    with patch("openthesis.service._canonical_retry_snapshot", return_value=[_fact()]):
        result = service._retry_research_model_stages_once(
            run.run_id, new_model, resume_target=target,
        )

    assert result == {"run_id": run.run_id, "dispatched": True}
    assert len(captured) == 1
    request = captured[0]
    assert request.run.provider_id == "gateway"
    assert request.run.model_id == "old-model"
    assert request.run.model_configuration == run.model_configuration
    assert request.model.configured_model_id == "new-model"
    assert request.model.configuration_version == 2
    assert provider_calls == []
    assert service.storage.latest_report_revision(run.run_id) == old_revision
    assert _context(service.storage, old_revision["revision_id"]) == old_context


def test_workflow_capture_updates_current_model_but_not_old_revision_context(tmp_path):
    service = AppService(tmp_path / "service")
    service.storage.save_company(DEMO_COMPANY)
    run = _old_run("workflow-capture")
    service.storage.save_run(run)
    service.storage.save_artifact(_report(run))
    old_revision = service.storage.latest_report_revision(run.run_id)
    old_context = _context(service.storage, old_revision["revision_id"])

    workflow = ResearchWorkflow(
        service.storage, builtin_pack(), None,
        ModelConfig("new-model", configuration_version=2), parallel_agents=False,
    )
    workflow._capture_resume_publication(run)

    persisted = json.loads(service.storage.get_run(run.run_id)["payload_json"])
    assert persisted["provider_id"] == "gateway"
    assert persisted["model_id"] == "new-model"
    assert persisted["model_configuration"] == {
        "configured_model_id": "new-model", "configuration_version": 2, "role": "primary",
    }
    assert run.model_id == "new-model"
    assert service.storage.latest_report_revision(run.run_id) == old_revision
    assert _context(service.storage, old_revision["revision_id"]) == old_context

