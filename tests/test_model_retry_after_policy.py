"""A server's Retry-After is a minimum, not a value to shorten for speed."""
import json
import subprocess
from unittest.mock import patch

import pytest

from openthesis.model_scheduler import ModelRequestScheduler
from openthesis.providers import ModelConfig, ProviderError, RustModelGatewayProvider


def test_scheduler_does_not_shorten_retry_after_to_300_seconds():
    scheduler = ModelRequestScheduler(clock=lambda: 1000.0)
    scheduler.mark_rate_limited(900)
    assert scheduler.snapshot().cooldown_remaining_seconds == 900


def test_gateway_keeps_retry_after_for_budget_exhaustion_instead_of_early_resubmit(tmp_path):
    gateway = tmp_path / "offline.exe"
    gateway.write_bytes(b"MZ")
    reply = {"ok": False, "wire_contract": "text-single-request-v1", "error": {
        "code": "MODEL_RATE_LIMITED", "http_status": 429, "retryable": True,
        "retry_after_seconds": 900, "message": "known quota rejection"}}
    completed = subprocess.CompletedProcess([], 1, json.dumps(reply).encode(), b"")
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="offline"), gateway_path=gateway)
    with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
        with pytest.raises(ProviderError) as caught:
            provider.generate("complete system", "complete source evidence")
    assert invoked.call_count == 1
    assert caught.value.retry_after_seconds == 900
    assert caught.value.http_status == 429
