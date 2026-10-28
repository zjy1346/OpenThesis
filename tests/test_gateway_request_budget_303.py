"""A vision deadline reaches the real provider without shared-state mutation."""
import json
import subprocess
import threading
import time
from unittest.mock import patch

import pytest

from openthesis.providers import ModelConfig, ProviderError, RustModelGatewayProvider


def provider(tmp_path):
    path = tmp_path / "gateway.exe"
    path.write_bytes(b"MZ")
    return RustModelGatewayProvider(ModelConfig(configured_model_id="model", timeout_seconds=600), gateway_path=path)


def test_expired_request_has_no_gateway_launch(tmp_path):
    original = provider(tmp_path)
    scoped = original.for_request_budget(time.monotonic() - 1)
    with patch("openthesis.providers.subprocess.Popen") as launched:
        with pytest.raises(ProviderError) as error:
            scoped.generate_vision_once("system", "full prompt", b"\x89PNG\r\n\x1a\nfixture")
    assert error.value.code == "MODEL_TIMEOUT"
    launched.assert_not_called()
    assert original.config.timeout_seconds == 600
    assert original._request_deadline is None and original._cancel_check is None


def test_native_gateway_is_killed_at_request_budget_not_model_timeout(tmp_path):
    original = provider(tmp_path)
    invocations = []

    class HangingGateway:
        returncode = None
        killed = False

        def communicate(self, input=None, timeout=None):
            invocations.append(timeout)
            if self.killed:
                return b"", b""
            time.sleep(min(timeout, .01))
            raise subprocess.TimeoutExpired("gateway", timeout)

        def kill(self):
            self.killed = True

    child = HangingGateway()
    scoped = original.for_request_budget(time.monotonic() + .04)
    started = time.monotonic()
    with patch("openthesis.providers.subprocess.Popen", return_value=child):
        with pytest.raises(ProviderError) as error:
            scoped.generate_vision_once("system", "full prompt", b"\x89PNG\r\n\x1a\nfixture")
    assert error.value.code == "MODEL_TIMEOUT"
    assert child.killed and time.monotonic() - started < .5
    # Deadline subtraction can round by a few binary-float ULPs; this is
    # not another model/network allowance. The child must still be killed.
    assert max(value for value in invocations if value is not None) <= .040001


def test_independent_requests_do_not_share_cancel_or_budget(tmp_path):
    original = provider(tmp_path)
    cancelled = threading.Event()
    first = original.for_request_budget(time.monotonic() + 1, cancelled.is_set)
    second = original.for_request_budget(time.monotonic() + 30)
    cancelled.set()
    assert first._cancel_check()
    assert not second._cancel_check()
    assert second._request_deadline > first._request_deadline
    assert first.config is second.config is original.config
