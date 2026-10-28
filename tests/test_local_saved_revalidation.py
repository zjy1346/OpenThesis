from dataclasses import replace

from openthesis.research_execution.checkpoints import completed_checkpoint, read_checkpoints
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.providers import ModelConfig
from openthesis.application_services import ResearchOrchestrator
from openthesis.application.research_requests import ResearchResumeRequest
from test_research_orchestrator_execution import saved_run, MemoryStore


def test_changed_contract_candidates_are_revalidated_without_using_old_success_or_provider():
    original = StageRegistry([StageSpec("base", "old", "generate", "verify", output_contract="analysis")])
    candidate = {"body": "saved original"}
    saved = StageExecutor({"generate": lambda *_: candidate}, {"verify": lambda *_:
        StageValidation(True, {"body": "WRONG OLD PROJECTION"}, "completed_verified")}).execute(original, {})["base"]
    registry = StageRegistry([replace(original.get("base"), version="new")])
    read = read_checkpoints([{"artifact_type": "stage-checkpoint", "content": completed_checkpoint(saved)}], registry)
    assert not read.results and read.revalidation_candidates["base"].candidate == candidate
    def cannot_generate(*_):
        raise AssertionError("local action must never invoke a provider")
    results = StageExecutor({"generate": cannot_generate}, {"verify": lambda _spec, value, _inputs:
        StageValidation(True, value, "completed_verified")}).execute(registry, {}, local_only=True,
            revalidation_candidates=read.revalidation_candidates)
    assert results["base"].validation.trusted_output == candidate
    assert results["base"].stage_identity == registry.get("base").identity


def test_no_saved_body_is_honest_needs_action_not_a_new_generation():
    registry = StageRegistry([StageSpec("base", "new", "generate", "verify", output_contract="analysis")])
    calls = []
    results = StageExecutor({"generate": lambda *_: calls.append("wire")}, {"verify": lambda *_: None}).execute(registry, {}, local_only=True)
    assert not calls
    assert results["base"].error_code == "STAGE_SAVED_CANDIDATE_UNAVAILABLE"
    assert not results["base"].validation.trusted_output


def test_local_resume_does_not_even_construct_provider(monkeypatch):
    class Workflow:
        def __init__(self, _store, _pack, provider, config, **_kwargs):
            assert provider is None
        def run(self, *_args, **kwargs):
            assert self.local_revalidation is True
            return kwargs["existing_run"]
    monkeypatch.setattr("openthesis.application_services.ResearchWorkflow", Workflow)
    def cannot_construct(_config):
        raise AssertionError("a local action must not create the provider")
    orchestrator = ResearchOrchestrator(MemoryStore(), provider_factory=cannot_construct)
    run = saved_run()
    request = ResearchResumeRequest(run, "pack", ModelConfig("configured"), (), (), local_only=True)
    assert orchestrator.resume(request).run_id == run.run_id
