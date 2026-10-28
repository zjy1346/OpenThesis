"""An unknown required wire survives configuration/generation changes."""
import threading

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun
from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageRequest
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.storage import Storage


def setup(tmp_path, config="model-a", clock=lambda: 1000):
    store = Storage(tmp_path / "wire-identity")
    store.save_company(DEMO_COMPANY)
    store.save_run(ResearchRun(run_id="identity-run", company=DEMO_COMPANY,
        workflow_id="w", research_pack_id="p", research_pack_version="1",
        provider_id="p", model_id="m", data_as_of="2026-10-06"))
    registry = StageRegistry((StageSpec("analysis", "v1", "handler", "validator", (),
                                       output_contract="analysis-output"),))
    journal = ModelRequestJournal(store, "identity-run", config, timeout_seconds=2,
                                  cancel_check=lambda: False, clock=clock)
    generation = journal.prepare(registry, "material-v1")["analysis"]
    return store, registry, journal, ("analysis", generation)


@pytest.mark.parametrize("new_configuration", [False, True])
def test_unknown_required_wire_is_not_reset_by_stage_or_configuration(tmp_path, new_configuration):
    store, registry, journal, scope = setup(tmp_path)
    request = ModelStageRequest("analysis", "original system", "all original evidence")
    calls = []

    def uncertain():
        calls.append("submitted")
        raise TimeoutError("wire may have been accepted")

    with pytest.raises(TimeoutError):
        journal.execute(request, uncertain, scope)
    original = ModelRequestJournal(store, "identity-run",
        "model-b" if new_configuration else "model-a", timeout_seconds=2,
        cancel_check=lambda: False, clock=lambda: 1020)
    generation = original.prepare(registry, "material-v1", invalidated=("analysis",))["analysis"]
    with pytest.raises(ProviderError) as caught:
        original.execute(request, uncertain, ("analysis", generation))
    assert caught.value.code == "MODEL_SUBMISSION_UNKNOWN"
    assert caught.value.retryable is False
    assert calls == ["submitted"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_request_journal").fetchone()[0] == 1


def test_inflight_required_wire_survives_configuration_switch_without_resubmission(tmp_path):
    store, registry, journal, scope = setup(tmp_path)
    request = ModelStageRequest("analysis", "system", "evidence")
    entered, released = threading.Event(), threading.Event()
    failures = []
    calls = []

    def producer():
        calls.append("submitted")
        entered.set()
        assert released.wait(5)
        return {"finding": "saved but no longer current"}

    def worker():
        try:
            journal.execute(request, producer, scope)
        except ProviderError as error:
            failures.append(error.code)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        replacement = ModelRequestJournal(store, "identity-run", "model-b", timeout_seconds=2,
            cancel_check=lambda: False, clock=lambda: 1000)
        generation = replacement.prepare(registry, "material-v1")["analysis"]
        with pytest.raises(ProviderError) as caught:
            replacement.execute(request, lambda: pytest.fail("duplicate paid request"),
                                ("analysis", generation))
        assert caught.value.code == "MODEL_REQUEST_IN_PROGRESS"
    finally:
        released.set()
        thread.join(5)
    assert not thread.is_alive()
    assert calls == ["submitted"]
    assert failures == ["MODEL_REQUEST_GENERATION_STALE"]
    with store.connect() as db:
        rows = db.execute("SELECT state,candidate_json FROM model_request_journal").fetchall()
    assert len(rows) == 1 and rows[0]["state"] == "late_candidate"
    assert "saved but no longer current" in rows[0]["candidate_json"]


def test_a_different_complete_prompt_is_not_the_same_unknown_wire(tmp_path):
    store, registry, journal, scope = setup(tmp_path)
    request = ModelStageRequest("analysis", "system", "first evidence")
    with pytest.raises(TimeoutError):
        journal.execute(request, lambda: (_ for _ in ()).throw(TimeoutError()), scope)
    replacement = ModelRequestJournal(store, "identity-run", "model-b", timeout_seconds=2,
        cancel_check=lambda: False, clock=lambda: 1020)
    generation = replacement.prepare(registry, "material-v2")["analysis"]
    result = replacement.execute(ModelStageRequest("analysis", "system", "new evidence"),
        lambda: {"finding": "new evidence candidate"}, ("analysis", generation))
    assert result == {"finding": "new evidence candidate"}


def test_confirmed_rejection_does_not_invent_an_unknown_submission(tmp_path):
    store, registry, journal, scope = setup(tmp_path)
    request = ModelStageRequest("analysis", "system", "evidence")
    with pytest.raises(ProviderError):
        journal.execute(request, lambda: (_ for _ in ()).throw(ProviderError(
            "rejected credentials", code="MODEL_AUTHENTICATION_ERROR", http_status=401)), scope)
    replacement = ModelRequestJournal(store, "identity-run", "explicitly-corrected-model", timeout_seconds=2,
        cancel_check=lambda: False, clock=lambda: 1020)
    generation = replacement.prepare(registry, "material-v1")["analysis"]
    assert replacement.execute(request, lambda: {"finding": "confirmed corrected credentials"},
                               ("analysis", generation))["finding"] == "confirmed corrected credentials"
