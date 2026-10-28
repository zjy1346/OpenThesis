"""Known HTTP rejection recovery, through the real journal and model seam.

Offline wires only. Returned protocol candidates still need stage business
validation; these tests do not assert research quality or real provider billing.
"""
import json

import pytest

from openthesis.providers import ProviderError
from openthesis.resilience.contracts import RecoveryLimits
from test_model_request_journal import _journal, _store
from test_text_format_recovery import OfflineWire, execute


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def limited(delay=3):
    return ProviderError("known rejection", code="MODEL_RATE_LIMITED",
                         http_status=429, retryable=True, retry_after_seconds=delay)


def journal_for(store, clock, *, calls=2, cancelled=lambda: False):
    journal = _journal(store, clock=clock, cancelled=cancelled)
    journal.repair_limits = RecoveryLimits(calls=calls, transient_retries=1)
    # Kept injectable so the product wait is exercised without a real delay.
    journal.recovery_sleeper = clock.sleep
    return journal


def test_confirmed_429_automatically_recovers_full_prompt_and_reuses_saved_candidate(tmp_path):
    store, clock = _store(tmp_path), Clock()
    candidate = {"analysis": "source-backed candidate", "structured_output_valid": True}
    provider = OfflineWire([limited(), candidate])
    assert execute(store, provider, journal=journal_for(store, clock)) == candidate
    assert execute(store, provider, journal=journal_for(store, clock)) == candidate
    assert len(provider.wires) == 2
    assert provider.wires[0] == provider.wires[1]
    assert sum(clock.waits) >= 3
    with store.connect() as db:
        action = db.execute("SELECT * FROM recovery_actions").fetchone()
        request = db.execute("SELECT * FROM model_request_journal").fetchone()
    assert action["state"] == "committed"
    assert action["external_intent"] == 1
    assert json.loads(action["usage_json"])["calls"] == 1
    assert request["state"] == "candidate"
    with store.connect() as db:
        incident = db.execute("SELECT usage_json FROM recovery_incidents").fetchone()
    assert json.loads(incident["usage_json"])["seconds"] == 3


def test_two_additional_rejections_are_bounded_and_do_not_reset_on_restart(tmp_path):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([limited(), limited(), limited()])
    for _ in range(2):
        with pytest.raises(ProviderError) as caught:
            execute(store, provider, journal=journal_for(store, clock))
        assert caught.value.code in {"MODEL_RATE_LIMITED", "RECOVERY_INCIDENT_BUDGET_EXHAUSTED"}
    assert len(provider.wires) == 3
    with store.connect() as db:
        action = db.execute("SELECT * FROM recovery_actions").fetchone()
        incident = db.execute("SELECT * FROM recovery_incidents").fetchone()
    assert action["attempt"] == 2 and action["retry_count"] == 1
    assert action["external_intent"] == 0
    assert json.loads(incident["usage_json"])["calls"] == 2


def test_retry_after_beyond_incident_budget_never_sends_another_request(tmp_path):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([limited(601)])
    with pytest.raises(ProviderError) as caught:
        execute(store, provider, journal=journal_for(store, clock))
    assert caught.value.code == "RECOVERY_INCIDENT_TIME_EXHAUSTED"
    assert len(provider.wires) == 1 and clock.waits == []


def test_timeout_after_rejection_remains_unknown_across_reconstruction(tmp_path):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([limited(), ProviderError("unknown submission", code="MODEL_TIMEOUT", retryable=True)])
    for _ in range(2):
        with pytest.raises(ProviderError) as caught:
            execute(store, provider, journal=journal_for(store, clock))
        assert caught.value.code == "MODEL_SUBMISSION_UNKNOWN"
    assert len(provider.wires) == 2


@pytest.mark.parametrize("error", [
    ProviderError("unproven code", code="MODEL_RATE_LIMITED", retryable=True),
    ProviderError("contradictory HTTP", code="MODEL_RATE_LIMITED", http_status=500, retryable=True),
    ProviderError("auth", code="MODEL_AUTHENTICATION_ERROR", http_status=401),
    ProviderError("possibly executed", code="MODEL_HTTP_500", http_status=500, retryable=True),
])
def test_unproven_transient_or_permanent_rejection_never_opens_a_retry(tmp_path, error):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([error])
    with pytest.raises(ProviderError) as caught:
        execute(store, provider, journal=journal_for(store, clock))
    assert caught.value is error
    assert len(provider.wires) == 1 and clock.waits == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0
