"""A transport failure is not an unverified business conclusion."""
import json

import pytest

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow
from openthesis.storage import Storage
from test_research_workflow import SevenStageProvider, _disclosure_fixture


@pytest.mark.parametrize("failed_agent", [
    "business-analyst", "growth-opportunity-analyst", "skeptical-analyst",
    "forecast-analyst", "research-synthesizer",
])
def test_received_transport_envelope_remains_in_journal_not_downstream_or_report(tmp_path, failed_agent):
    sentinel = "CONTROLLED_TRANSPORT_ENVELOPE_NOT_BUSINESS_CONTENT"

    class EnvelopeProvider(SevenStageProvider):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def generate(self, system_prompt, user_prompt, *, json_mode=True):
            payload = json.loads(user_prompt)
            self.payloads.append(payload)
            if payload.get("agent") == failed_agent:
                self.calls.append(failed_agent)
                return {"_response_error": "empty_content", "narrative": sentinel, "claims": []}
            return super().generate(system_prompt, user_prompt, json_mode=json_mode)

    storage = Storage(tmp_path)
    storage.save_company(DEMO_COMPANY)
    provider = EnvelopeProvider()
    workflow = ResearchWorkflow(storage, builtin_pack(), provider,
        ModelConfig(configured_model_id="test.envelope.311"), parallel_agents=False)
    run = workflow.run(DEMO_COMPANY, demo_facts(), filing_evidence=_disclosure_fixture())
    assert provider.calls.count(failed_agent) >= 1
    # No later model receives the host-controlled failure object as analysis.
    assert all(sentinel not in json.dumps(payload) for payload in provider.payloads)
    artifacts = storage.get_artifacts(run.run_id)
    reports = [item for item in artifacts if item["artifact_type"] == "research-report"]
    assert reports
    assert sentinel not in json.dumps(reports[-1]["content"])
    # Preserve the received candidate for audit/local recovery, not as report prose.
    with storage.connect() as db:
        received = db.execute("SELECT candidate_json FROM model_request_journal WHERE run_id=? AND response_received=1",
            (run.run_id,)).fetchall()
    assert any(sentinel in (row["candidate_json"] or "") for row in received)
