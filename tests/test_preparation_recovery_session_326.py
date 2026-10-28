"""First-run source incidents must follow the same session into orchestration."""
import time

import pytest

from openthesis.domain import Company
from openthesis.research import ResearchCancelled
from openthesis.service import AppService


@pytest.mark.parametrize('session', [None, 'existing-recovery-session'])
def test_formal_start_binds_official_and_orchestrator_recovery_sessions(tmp_path, session):
    service = AppService(tmp_path,
        sec_client_factory=lambda *_args: pytest.fail('unexpected SEC transport'),
        provider_factory=lambda *_args: pytest.fail('unexpected model provider'))
    company = Company('0000000001', 'ACME', 'Acme')
    service._fetch_official_companies = lambda _query, _market: [company]
    service.update_preferences({'sec_contact_profile': 'OpenThesis QA',
        'sec_contact_email': 'qa@example.com'})
    received = []

    def capture(request, **_kwargs):
        received.append(request)
        raise ResearchCancelled('offline capture before acquisition or providers')

    service._research_orchestrator.start = capture
    request = {'mode': 'company', 'company': company.to_dict(),
        'model': {'configured_model_id': 'test.primary', 'configuration_version': 1, 'role': 'primary'}}
    if session is not None:
        request['_financial_recovery_session_id'] = session
    started = service.start_research(request)
    deadline = time.monotonic() + 5
    while True:
        status = service.get_research_status(started['job_id'])
        if status['state'] in {'completed', 'partial', 'failed', 'cancelled'}:
            break
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert received, status
    assert status['state'] == 'cancelled', status
    captured = received[0]
    # The formal entrypoint allocates its own run before preparation; a
    # caller-supplied internal recovery identifier must not bind another run.
    assert captured.official.run_id == started['run_id']
    assert captured.existing_run.run_id == started['run_id']
    assert captured.recovery_session_id == captured.official.run_id
