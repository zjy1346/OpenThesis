"""The real preparation/orchestrator can save source work before parsing ends."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace
from unittest.mock import patch
import time

import pytest

from openthesis.application.financials import OfficialFinancialRequest
from openthesis.application.research_preparation import NewResearchPreparer, ResearchPreparationRequest, PreparedResearchStart
from openthesis.application.research_requests import ResearchStartRequest
from openthesis.application_services import ResearchOrchestrator
from openthesis.domain import ResearchRun
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchCancelled
from openthesis.storage_research_publication import ResearchPublicationConflict
from test_official_financial_preparation import setup_preparer
from test_research_workflow import SevenStageProvider


def fixture(tmp_path, *, timeout=30):
    company, filing, adapter, official, storage = setup_preparer(tmp_path)
    path = Path(filing.local_path)
    body = path.read_bytes().replace(b"</html>", b"""<p>Business overview. The company manufactures specialized equipment and sells
        maintenance services to industrial customers. Customer concentration and refinancing
        expose the business to risk. Management plans new products and capacity investment.</p></html>""")
    path.write_bytes(body)
    filing = replace(filing, content_hash=sha256(body).hexdigest())
    adapter.filing = filing
    run = ResearchRun("overlap-run", company, "wf", builtin_pack().pack_id, builtin_pack().version,
                      "fixture", "fixture", "2026-10-13")
    storage.save_run(run)
    request = ResearchPreparationRequest(company, builtin_pack(), (ModelConfig("fixture", timeout_seconds=timeout),),
        official=OfficialFinancialRequest(company, adapter, annual_history_years=1),
        annual_history_years=1, existing_run=run)
    return company, filing, official, storage, request


def test_formal_entry_saves_business_while_financial_collection_waits_and_does_not_generate_it_twice(tmp_path):
    company, filing, official, store, request = fixture(tmp_path)
    financial_started, source_saved = Event(), Event()
    collection = official.pipeline.engine.collect_candidate_batches
    save = store.save_artifact
    events = []

    def waiting_collection(*args, **kwargs):
        financial_started.set()
        events.append("financial_wait")
        assert source_saved.wait(8), "source work was not saved during the financial wait"
        events.append("financial_continue")
        return collection(*args, **kwargs)

    def saving(artifact):
        result = save(artifact)
        if artifact.artifact_type == "stage-checkpoint" and artifact.content["result"]["stage_id"] == "business-analyst":
            events.append("business_saved")
            source_saved.set()
        return result

    class Provider(SevenStageProvider):
        def generate(self, *args, **kwargs):
            assert financial_started.wait(3)
            return super().generate(*args, **kwargs)

    provider = Provider()
    official.pipeline.engine.collect_candidate_batches = waiting_collection
    store.save_artifact = saving
    preparer = NewResearchPreparer(store, official, SimpleNamespace())
    run = ResearchOrchestrator(store, lambda _config: provider, new_research_preparer=preparer).start(request)
    assert run.run_id == request.existing_run.run_id
    assert events.index("business_saved") < events.index("financial_continue")
    assert provider.calls.count("business-analyst") == 1, provider.calls
    assert store.get_financial_research_snapshot(company.cik)["facts"]
    assert any(item["artifact_type"] == "research-report" for item in store.get_artifacts(run.run_id))


@pytest.mark.parametrize("mode", ["budget", "cancel", "publication_conflict"])
def test_prefetch_wait_is_bounded_and_does_not_swallow_cancellation_or_cas_conflicts(tmp_path, mode):
    company, filing, _official, store, request = fixture(tmp_path, timeout=30)
    entered, release, cancelled = Event(), Event(), Event()
    finished = Event()
    calls = []

    class Preparer:
        def prepare(self, request, *, sources_ready, **kwargs):
            sources_ready(company, (filing,))
            assert entered.wait(3)
            if mode == "cancel":
                cancelled.set()
            return PreparedResearchStart(ResearchStartRequest(company, [], request.selected_pack, request.models,
                existing_run=request.existing_run))

    class Workflow:
        def __init__(self, *_args, **_kwargs):
            pass

        def prefetch_source_stages(self, run, evidence):
            entered.set()
            if mode == "publication_conflict":
                raise ResearchPublicationConflict("RESEARCH_PUBLICATION_INPUT_CONFLICT")
            release.wait(3)
            finished.set()

        def run(self, **kwargs):
            calls.append("full_graph")
            return request.existing_run

    evidence = [{"evidence_id": "filing:business", "kind": "filing_text", "raw_text": "Original business disclosure.",
        "document_id": filing.document_id, "source_url": filing.source_url, "content_hash": filing.content_hash}]
    timer = Timer(1, release.set)
    timer.start()
    started = time.monotonic()
    try:
        with patch("openthesis.application_services.cached_filing_evidence", return_value=evidence), \
                patch("openthesis.application_services.ResearchWorkflow", Workflow):
            orchestrator = ResearchOrchestrator(store, lambda _config: object(), new_research_preparer=Preparer(),
                source_task_budget_seconds=.05 if mode == "budget" else 30)
            if mode == "budget":
                orchestrator.start(request)
                assert time.monotonic() - started < .6
                assert calls == ["full_graph"]
                assert any(item["content"].get("code") == "SOURCE_PREFETCH_TASK_TIMEOUT"
                    for item in store.get_artifacts(request.existing_run.run_id))
            else:
                with pytest.raises(ResearchCancelled if mode == "cancel" else ResearchPublicationConflict):
                    orchestrator.start(request, cancel_check=cancelled.is_set)
                assert calls == []
                assert time.monotonic() - started < .6
    finally:
        release.set()
        timer.cancel()
