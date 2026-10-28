from copy import deepcopy
import threading
from unittest.mock import patch

import pytest

from openthesis.application.research_requests import ResearchStartRequest, ResearchResumeRequest
from openthesis.application_services import ResearchOrchestrator
from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun, RunStatus
from openthesis.providers import ModelConfig
from openthesis.research import ResearchCancelled


def saved_run(run_id="primary"):
    return ResearchRun(run_id, deepcopy(DEMO_COMPANY), "workflow", "pack", "1",
                       "gateway", "primary", "2026-10-05", RunStatus.COMPLETED)


class MemoryStore:
    def __init__(self):
        self.saved = []
        self.reassigned = []
        self.stage_attempts = []
        self._runs = {}
        self._domain_runs = {}

    def save_run(self, run):
        committed = deepcopy(run)
        self.saved.append(committed)
        self._domain_runs[run.run_id] = deepcopy(committed)
        self._runs[run.run_id] = deepcopy(committed.to_dict())

    def save_run_stage_result(self, run_id, attempt, *, snapshot_updates=None,
                              status=None, error="", completed_at=None):
        """Mirror the continuity storage seam without leaking peer failures.

        The production method returns the committed database payload, so the
        fixture keeps a separate serialized copy and only projects its
        sanitized stage error onto the existing domain run.
        """
        if run_id not in self._runs or attempt.run_id != run_id:
            raise AssertionError("stage attempt must target a seeded run")
        committed = deepcopy(self._runs[run_id])
        if snapshot_updates:
            committed.setdefault("data_snapshot", {}).update(deepcopy(snapshot_updates))
        current_status = committed.get("status")
        if status is not None and current_status not in {
                RunStatus.COMPLETED.value, RunStatus.CANCELLED.value}:
            committed["status"] = status.value if isinstance(status, RunStatus) else status
            committed["completed_at"] = completed_at
        errors = committed.setdefault("errors", [])
        if error and error not in errors:
            errors.append(error)
        self.stage_attempts.append(deepcopy(attempt))
        self._runs[run_id] = deepcopy(committed)

        domain_run = deepcopy(self._domain_runs[run_id])
        domain_run.status = RunStatus(committed["status"])
        domain_run.completed_at = committed.get("completed_at")
        domain_run.errors = deepcopy(errors)
        domain_run.data_snapshot = deepcopy(committed.get("data_snapshot", {}))
        self._domain_runs[run_id] = deepcopy(domain_run)
        self.saved.append(deepcopy(domain_run))
        return deepcopy(committed)

    def reassign_financial_recovery_cases(self, session, run_id):
        self.reassigned.append((session, run_id))


def start_request(**kwargs):
    return ResearchStartRequest(
        DEMO_COMPANY, [{"fact_id": "f1", "value": 100}], "pack",
        (ModelConfig("primary"), ModelConfig("comparison")),
        filing_evidence=({"evidence_id": "source", "text": "original"},),
        reproducibility={"data_snapshot": {"digest": "fixed"}}, **kwargs)


def test_start_uses_one_captured_source_set_without_cross_model_mutation():
    store = MemoryStore()
    request = start_request(recovery_session_id="session", existing_run=saved_run())
    frozen = request.workflow_inputs()
    calls = []
    events = []

    class Workflow:
        def __init__(self, _storage, _pack, _provider, config, **options):
            self.config = config
            assert options["cancel_check"] is not None

        def run(self, **inputs):
            calls.append((self.config.public_id, deepcopy(inputs["facts"]),
                          deepcopy(inputs["reproducibility"]), inputs["existing_run"]))
            inputs["progress"]("source-bound research", 50)
            inputs["facts"][0]["value"] = 999
            inputs["reproducibility"]["data_snapshot"]["digest"] = "changed"
            return inputs["existing_run"] or saved_run("comparison")

    # A UI callback may retain the request; its mutation also must not change
    # the private material snapshot used by subsequent comparison workflows.
    def progress(event):
        events.append(event)
        request.financials[0]["value"] = 888

    with patch("openthesis.application_services.ResearchWorkflow", Workflow), \
            patch("openthesis.application_services.compare_research_runs") as compare:
        run = ResearchOrchestrator(store, lambda _config: object()).start(request, progress=progress)
    assert run is request.existing_run
    assert len(calls) == 2
    assert calls[0][1:3] == calls[1][1:3] == (frozen["facts"], frozen["reproducibility"])
    assert calls[0][3] is request.existing_run and calls[1][3] is None
    assert [event.percent for event in events] == [25, 75]
    assert [event.model_percent for event in events] == [50, 50]
    assert compare.call_count == 1
    assert store.reassigned == [("session", "primary")]


@pytest.mark.parametrize("failure_point", ["factory", "run", "comparison_writer"])
def test_comparison_failure_never_discards_primary_or_retries_implicitly(failure_point):
    store = MemoryStore()
    primary = saved_run()
    store.save_run(primary)
    completed_at = primary.completed_at
    request = start_request(existing_run=primary)
    factories = []
    run_calls = []
    failures = []

    def factory(config):
        factories.append(config.configured_model_id)
        if failure_point == "factory" and config.configured_model_id == "comparison":
            raise RuntimeError("private-token-in-server-error")
        return config

    class Workflow:
        def __init__(self, _storage, _pack, provider, _config, **_options):
            self.model = provider.configured_model_id

        def run(self, **_inputs):
            run_calls.append(self.model)
            if failure_point == "run" and self.model == "comparison":
                raise RuntimeError("private-token-in-server-error")
            return primary if self.model == "primary" else saved_run("secondary")

    with patch("openthesis.application_services.ResearchWorkflow", Workflow), \
            patch("openthesis.application_services.compare_research_runs",
                  side_effect=RuntimeError("private-token-in-server-error")
                  if failure_point == "comparison_writer" else None):
        run = ResearchOrchestrator(store, factory).start(request, comparison_failure=failures.append)
    assert run is primary
    assert primary.status is RunStatus.COMPLETED
    assert primary.completed_at == completed_at
    assert primary.errors == ["comparison:1:RuntimeError"]
    assert len(failures) == 1
    assert failures[0].error_code == "RuntimeError"
    assert "private-token" not in str(store.saved)
    assert "private-token" not in str(failures)
    assert len(store.stage_attempts) == 1
    assert store.stage_attempts[0].stage == "comparison:1"
    assert "private-token" not in str(store.stage_attempts)
    assert factories == ["primary", "comparison"]
    assert run_calls == (["primary"] if failure_point == "factory" else ["primary", "comparison"])


def test_cancellation_before_start_does_not_construct_provider():
    calls = []
    with pytest.raises(ResearchCancelled):
        ResearchOrchestrator(MemoryStore(), calls.append).start(start_request(), cancel_check=lambda: True)
    assert calls == []


@pytest.mark.parametrize("target", ["model-stages", "growth", "counter-analysis", "forecast-scenarios", "synthesis"])
def test_resume_selects_only_the_shared_workflow_entry_and_target(target):
    run = saved_run()
    run.research_configuration = {"valuation_inputs": {"market_cap": 1000}}
    request = ResearchResumeRequest(run, "pack", ModelConfig("configured-new-model"),
        ({"fact_id": "f1", "value": 100},), ({"artifact_id": "a1"},), target=target)
    original = deepcopy(request.facts)
    calls = []

    class Workflow:
        def __init__(self, _storage, _pack, _provider, config, **options):
            assert config.configured_model_id == "configured-new-model"
            assert options["report_language"] == run.report_language

        def run(self, company, facts, **kwargs):
            calls.append(("run", kwargs["resume_target"], kwargs["existing_run"], kwargs["resume_artifacts"]))
            assert kwargs["valuation_inputs"] == {"market_cap": 1000}
            facts[0]["value"] = 999
            return run

        def retry_synthesis(self, received_run, artifacts, facts):
            calls.append(("synthesis", target, received_run, artifacts))
            facts[0]["value"] = 999
            return run

    with patch("openthesis.application_services.ResearchWorkflow", Workflow):
        result = ResearchOrchestrator(MemoryStore(), lambda _config: object()).resume(request)
    assert result is run
    assert calls == [("synthesis" if target == "synthesis" else "run", target, run, [{"artifact_id": "a1"}])]
    assert request.facts == original


def test_duplicate_models_and_unknown_resume_targets_fail_before_provider_creation():
    with pytest.raises(ValueError, match="RESEARCH_MODELS_NOT_UNIQUE"):
        ResearchStartRequest(DEMO_COMPANY, [], "pack", (ModelConfig("same"), ModelConfig("same")))
    with pytest.raises(ValueError, match="MODEL_RECOVERY_TARGET_INVALID"):
        ResearchResumeRequest(saved_run(), "pack", ModelConfig("model"), (), (), target="unregistered")


@pytest.mark.parametrize("point", ["before_peer", "during_peer", "after_peer"])
def test_comparison_cancellation_keeps_the_already_committed_primary(point):
    store, cancelled = MemoryStore(), threading.Event()
    primary = saved_run()
    store.save_run(primary)
    calls = []

    class Workflow:
        def __init__(self, _storage, _pack, _provider, config, **_options):
            self.model = config.configured_model_id

        def run(self, **inputs):
            calls.append(self.model)
            if self.model == "primary":
                if point == "before_peer":
                    cancelled.set()
                return primary
            if point == "during_peer":
                cancelled.set()
                raise ResearchCancelled()
            cancelled.set()
            return saved_run("peer")

    with patch("openthesis.application_services.ResearchWorkflow", Workflow), \
            patch("openthesis.application_services.compare_research_runs") as compare:
        with pytest.raises(ResearchCancelled) as caught:
            ResearchOrchestrator(store, lambda _config: object()).start(
                start_request(existing_run=primary), cancel_check=cancelled.is_set)
    assert getattr(caught.value, "primary_run", None) is primary
    assert primary.status is RunStatus.COMPLETED
    assert primary.errors == ["comparison:1:RESEARCH_CANCELLED"]
    assert store.saved[-1].status is RunStatus.COMPLETED
    assert calls == (["primary"] if point == "before_peer" else ["primary", "comparison"])
    assert compare.call_count == 0, "a cancelled peer is not a valid comparison"
