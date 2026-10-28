"""Offline public-start regressions for the captured preparation entry point."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openthesis.application.financials import OfficialFinancialRequest
from openthesis.application.research_preparation import (
    NewResearchPreparer,
    ResearchPreparationRequest,
    ResearchPreparationUnavailable,
)
from openthesis.application_services import ResearchOrchestrator
from openthesis.disclosure_coverage import DisclosureCoverage
from openthesis.domain import ResearchRun, RunStatus
from openthesis.financial_admission import ReadinessLevel
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchCancelled
from openthesis.sec_client import SecFinancialSourceAdapter

from test_official_financial_preparation import setup_preparer


class _LocalDisclosure:
    def capture(self, company, policy, manual=None):
        payload = deepcopy(manual) if manual is not None else {
            "status": "UNAVAILABLE", "currency": company.reporting_currency,
        }
        return SimpleNamespace(to_dict=lambda: payload)


def _model(model_id: str, role: str, version: int = 3) -> ModelConfig:
    return ModelConfig(configured_model_id=model_id, role=role,
                       configuration_version=version)


def _run(config, inputs):
    return ResearchRun(
        run_id=f"run-{config.configured_model_id}",
        company=deepcopy(inputs["company"]),
        workflow_id="offline-prepared",
        research_pack_id=inputs["reproducibility"]["research_configuration"]["research_pack_id"],
        research_pack_version=inputs["reproducibility"]["research_configuration"]["research_pack_version"],
        provider_id="offline.fixture",
        model_id=config.configured_model_id,
        data_as_of="2026-10-06",
        status=RunStatus.COMPLETED,
    )


def test_start_prepares_once_shares_exact_material_and_notifies_before_providers(tmp_path):
    company, filing, adapter, official, storage = setup_preparer(tmp_path)
    request = ResearchPreparationRequest(
        company=company,
        selected_pack=builtin_pack(),
        models=(_model("offline.primary", "primary"),
                _model("offline.comparison", "comparison", 4)),
        official=OfficialFinancialRequest(
            company, adapter,
            candidate_sources=(SecFinancialSourceAdapter(adapter),),
            annual_history_years=1,
        ),
        annual_history_years=1,
        vision_policy={"enabled": True, "approval_mode": "review_each_plan"},
    )
    order = []
    captured = []

    class Workflow:
        def __init__(self, _storage, _pack, _provider, config, **_options):
            self.config = config

        def run(self, **inputs):
            captured.append((self.config.public_id, deepcopy(inputs)))
            # Simulate one peer mutating all three nested material classes.
            inputs["company"].name = "mutated-peer"
            inputs["reproducibility"]["research_configuration"]["vision_policy"]["enabled"] = False
            facts = inputs["facts"]
            object.__setattr__(facts, "fact_dicts", ())
            return _run(self.config, inputs)

    def coverage_callback(coverage):
        order.append(("coverage", coverage.requested_count, coverage.found_periods))

    def provider_factory(config):
        order.append(("provider", config.public_id))
        return object()

    with patch("openthesis.application_services.ResearchWorkflow", Workflow), \
            patch("openthesis.application_services.compare_research_runs"):
        run = ResearchOrchestrator(
            storage, provider_factory,
            new_research_preparer=NewResearchPreparer(
                storage, official, _LocalDisclosure()
            ),
        ).start(request, disclosure_coverage=coverage_callback)

    assert run.status is RunStatus.COMPLETED
    assert len(captured) == 2
    assert captured[0][1]["facts"].fact_dicts
    assert captured[0][1]["facts"].fact_dicts == captured[1][1]["facts"].fact_dicts
    assert captured[0][1]["company"].issuer_id == captured[1][1]["company"].issuer_id == company.issuer_id
    assert captured[0][1]["company"].name == captured[1][1]["company"].name == company.name
    assert captured[0][1]["reproducibility"] == captured[1][1]["reproducibility"]
    active_heads = storage.get_financial_research_snapshot(company.cik)["generations"]
    for _public_id, inputs in captured:
        profile = inputs["facts"]
        assert inputs["reproducibility"]["data_snapshot"]["financial_generations"] == active_heads
        assert inputs["reproducibility"]["research_configuration"]["vision_policy"] == {
            "enabled": True, "approval_mode": "review_each_plan"
        }
        assert {
            str(fact.generation_id) for fact in profile.facts
        } == set(active_heads.values())
        assert all(
            fact.source_document == filing.primary_document
            and fact.source_url == filing.source_url
            and fact.generation_id == active_heads[str(fact.accession_number)]
            for fact in profile.facts
        )
        assert {
            item["content_hash"] for item in inputs["filing_evidence"]
            if item.get("content_hash")
        } == {filing.content_hash}
    assert next(
        item["value"] for item in captured[0][1]["facts"].fact_dicts
        if item["concept"] == "parent_net_income"
    ) == 100
    assert any(
        item.get("content_hash") == filing.content_hash
        for item in captured[0][1]["filing_evidence"]
    )
    assert adapter.facts_calls == 1
    assert adapter.download_calls == 0
    assert order[0][0] == "coverage"
    assert all(item[0] == "provider" for item in order[1:])


def test_cancelled_and_typed_preparation_failure_never_construct_provider(tmp_path):
    company, _filing, adapter, official, storage = setup_preparer(tmp_path)
    request = ResearchPreparationRequest(
        company=company, selected_pack=builtin_pack(), models=(_model("offline", "primary"),),
        official=OfficialFinancialRequest(company, adapter, annual_history_years=1),
        annual_history_years=1,
    )
    providers = []
    coverage_events = []
    orchestrator = ResearchOrchestrator(
        storage, providers.append,
        new_research_preparer=NewResearchPreparer(storage, official, _LocalDisclosure()),
    )
    with pytest.raises(ResearchCancelled):
        orchestrator.start(
            request, cancel_check=lambda: True,
            disclosure_coverage=coverage_events.append,
        )
    assert providers == []
    assert coverage_events == []
    assert adapter.list_limits == []
    assert adapter.facts_calls == adapter.download_calls == 0

    coverage = DisclosureCoverage(
        requested_count=1, report_types=("10-K",), authority="SEC",
        found_periods=(), missing_periods=("2026",), exhausted=True,
        failures=("official_source_unavailable",),
    )
    typed_failure = ResearchPreparationUnavailable(
        "FILING_FORMAT_UNSUPPORTED", coverage=coverage,
    )

    class FailingPreparer:
        def prepare(self, *_args, **_kwargs):
            raise typed_failure

    providers = []
    with pytest.raises(ResearchPreparationUnavailable) as raised:
        ResearchOrchestrator(
            storage, providers.append, new_research_preparer=FailingPreparer()
        ).start(request)
    assert raised.value.code == "FILING_FORMAT_UNSUPPORTED"
    assert raised.value.coverage is coverage
    assert providers == []


def test_unverified_original_without_sourced_evidence_still_blocks_start(tmp_path):
    company, _filing, adapter, official, storage = setup_preparer(tmp_path)
    request = ResearchPreparationRequest(
        company=company, selected_pack=builtin_pack(), models=(_model("offline", "primary"),),
        official=OfficialFinancialRequest(company, adapter, annual_history_years=1),
        annual_history_years=1,
    )
    original_prepare = official.prepare

    def remove_trusted_material(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        Path(result.filings[0].local_path).write_bytes(b"not the captured official original")
        profile = replace(result.view.profile, facts=(), fact_dicts=(), metrics=(), interim_metrics=())
        readiness = replace(result.view.readiness, level=ReadinessLevel.BLOCKED, source_document_count=0)
        view = replace(result.view, facts=(), fact_dicts=(), evidence=(), canonical_dataset=None,
            profile=profile, readiness=readiness,
            capabilities=tuple((key, False) for key, _value in result.view.capabilities))
        return replace(result, view=view)

    official.prepare = remove_trusted_material
    providers = []
    with pytest.raises(ResearchPreparationUnavailable) as raised:
        ResearchOrchestrator(storage, providers.append,
            new_research_preparer=NewResearchPreparer(storage, official, _LocalDisclosure())).start(request)

    assert raised.value.code.startswith("FILING_")
    assert raised.value.code != "FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED"
    assert providers == []


def test_missing_preparer_fails_explicitly_without_source_or_provider_io(tmp_path):
    company, _filing, adapter, _official, storage = setup_preparer(tmp_path)
    request = ResearchPreparationRequest(
        company=company, selected_pack=builtin_pack(), models=(_model("offline", "primary"),),
        official=OfficialFinancialRequest(company, adapter, annual_history_years=1),
        annual_history_years=1,
    )
    providers = []
    with pytest.raises(ValueError, match="RESEARCH_PREPARER_NOT_CONFIGURED"):
        ResearchOrchestrator(storage, providers.append).start(request)
    assert providers == []
    assert adapter.list_limits == []
    assert adapter.facts_calls == adapter.download_calls == 0
    assert storage.get_facts(company.cik) == []


def test_preparation_progress_cannot_mutate_company_source_or_model_for_peers(tmp_path):
    company, _filing, adapter, official, storage = setup_preparer(tmp_path)
    request = ResearchPreparationRequest(
        company=company, selected_pack=builtin_pack(),
        models=(_model("offline.primary", "primary", 3),
                _model("offline.comparison", "comparison", 4)),
        official=OfficialFinancialRequest(company, adapter, annual_history_years=1),
        annual_history_years=1,
    )
    captured_progress = []

    def mutate_public_request(*args):
        captured_progress.append(args[0])
        object.__setattr__(
            request.official, "company",
            replace(request.official.company, cik="9999999999", issuer_id="mutated-source"),
        )
        object.__setattr__(request.models[0], "configuration_version", 99)

    seen = []

    class Workflow:
        def __init__(self, _storage, _pack, _provider, config, **_options):
            self.config = config

        def run(self, **inputs):
            seen.append((self.config.public_id, inputs["company"].issuer_id))
            return _run(self.config, inputs)

    with patch("openthesis.application_services.ResearchWorkflow", Workflow), \
            patch("openthesis.application_services.compare_research_runs"):
        ResearchOrchestrator(
            storage, lambda _config: object(),
            new_research_preparer=NewResearchPreparer(
                storage, official, _LocalDisclosure()
            ),
        ).start(request, preparation_progress=mutate_public_request)

    assert captured_progress
    assert seen == [("gateway:offline.primary@3", company.issuer_id),
                    ("gateway:offline.comparison@4", company.issuer_id)]
    assert adapter.download_calls == 0
