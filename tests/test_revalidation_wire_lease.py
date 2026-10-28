import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun
from openthesis.providers import ProviderError
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.storage import Storage


@pytest.mark.parametrize("state", ["running", "received"])
def test_local_prepare_cannot_advance_head_behind_active_wire(tmp_path, state):
    store = Storage(tmp_path)
    store.save_company(DEMO_COMPANY)
    store.save_run(ResearchRun("lease-run", DEMO_COMPANY, "workflow", "test", "1", "test", "model", "2025-12-31"))
    journal = ModelRequestJournal(store, "lease-run", "configured:1", timeout_seconds=2,
                                  cancel_check=lambda: False, clock=lambda: 100.0)
    registry = StageRegistry([StageSpec("base", "1", "generate", "verify", output_contract="body")])
    assert journal.prepare(registry, "old-material") == {"base": 1}
    with store.connect() as db:
        db.execute("""INSERT INTO model_request_journal(request_id,run_id,stage_id,generation,input_digest,
            configuration_fingerprint,state,owner_id,lease_until,created_at,updated_at)
            VALUES('active','lease-run','base',1,'input','configured:1',?,'owner',110,99,99)""", (state,))
    with pytest.raises(ProviderError) as raised:
        journal.prepare(registry, "changed-material", local_only=True)
    assert raised.value.code == "MODEL_REQUEST_IN_PROGRESS"
    with store.connect() as db:
        assert db.execute("SELECT generation FROM model_stage_request_heads").fetchone()[0] == 1
