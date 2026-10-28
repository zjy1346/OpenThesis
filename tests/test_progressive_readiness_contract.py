"""Local assembly keeps the canonical coverage/action contract on formal reads."""
from openthesis.service import AppService
from openthesis.storage import Storage
from test_compound_report_retention import BODY, _seed_partial_run, _persist_failed_candidate_and_local_delta


def test_progressive_revision_has_specific_recovery_action_after_restart(tmp_path):
    store = Storage(tmp_path)
    run = _seed_partial_run(store, 'progressive-readiness')
    _persist_failed_candidate_and_local_delta(store, run, failure_code='MODEL_CAPACITY_EXHAUSTED')
    value = AppService(tmp_path, provider_factory=lambda _config: None).get_report(run.run_id, language='en')
    assert value['report_readiness']['complete'] is False
    assert value['report_readiness']['recovery_action'] == 'complete_missing_or_unverified_sections'
    assert value['report_readiness']['coverage']['gaps']
    assert value['action_required'] is True
    assert BODY in value['markdown']
