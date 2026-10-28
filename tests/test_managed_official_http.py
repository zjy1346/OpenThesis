"""Real official clients are single-attempt adapters under the journal owner."""
from email.message import Message
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from openthesis.market_data import OfficialDisclosureHttpClient
from openthesis.sec_client import SecClient, _ManagedSecRedirectHandler
from openthesis.resilience.contracts import AuthorizationScope, RecoveryLimits, Usage
from openthesis.resilience.remote import remote_request_scope, retry_after_seconds
from openthesis.resilience.strategies import CandidateResult, RecoveryError
import test_resilience_core as _resilience


class Response:
    headers = {}
    def __init__(self):
        self.parts = [b"original", b""]
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, _size=-1): return self.parts.pop(0)
    def geturl(self): return "https://static.cninfo.com.cn/annual.pdf"


@pytest.mark.parametrize("source", ["sec", "cn_a"])
def test_nested_http_retries_are_owned_and_recorded_by_one_coordinator(source, tmp_path):
    harness = _resilience.ResilienceCoreTests()
    harness.setUp()
    try:
        headers = Message()
        headers["Retry-After"] = "7"
        failures = [HTTPError("https://static.cninfo.com.cn/annual.pdf", 429, "limited", headers, None)
                    for _ in range(2)]
        client = (SecClient("Test offline@example.test", tmp_path / "sec-cache", min_interval=0)
                  if source == "sec" else OfficialDisclosureHttpClient())
        call = (lambda: client._request_bytes("https://www.sec.gov/Archives/annual.htm")) if source == "sec" else (
            lambda: client._request("https://static.cninfo.com.cn/annual.pdf"))
        opener = "openthesis.sec_client.SecClient._open_managed" if source == "sec" else (
            "openthesis.market_data.OfficialDisclosureHttpClient._open_once")
        sleeper = "openthesis.sec_client.time.sleep" if source == "sec" else "openthesis.market_data.time.sleep"
        def produce(context):
            with remote_request_scope(context):
                payload = call()
            assert payload == b"original"
            return CandidateResult({"source_receipt": "original"})
        harness.register(produce=produce)
        action = harness.action(provider_id="official", configuration_fingerprint="public-config",
            estimated_usage=Usage(calls=1, seconds=30))
        authorization = AuthorizationScope(("official",), (), ("public-config",))
        plan = harness.plan(action, limits=RecoveryLimits(calls=3), authorization_scope=authorization)
        with patch(opener, side_effect=[*failures, Response()]) as requests, patch(sleeper) as inner_sleep:
            outcome = harness.execute(plan, authorization)
        status = harness.repository.status(plan.plan_id)
        assert outcome["state"] == "committed"
        assert requests.call_count == 3
        assert inner_sleep.call_count == 0
        assert status["action_count"] == status["actions"][0]["attempt"] == 3
        assert status["usage"]["calls"] == 3
        assert harness.clock.now >= 1014
    finally:
        harness.tearDown()


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "broken date"])
def test_invalid_retry_after_never_creates_non_finite_waits(value):
    assert retry_after_seconds(value) == 0


def test_parallel_requests_consume_the_same_atomic_usage_counter():
    harness = _resilience.ResilienceCoreTests()
    harness.setUp()
    try:
        plus = Usage.plus
        def slower_plus(value, added):
            # Expose the actual read/modify/write race, not a forged journal.
            time.sleep(0.03)
            return plus(value, added)
        def produce(context):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _index: context.consume(Usage(calls=1)), range(4)))
            assert context.usage.calls == 4
            return CandidateResult({"request_count": context.usage.calls})
        harness.register(produce=produce)
        action = harness.action(provider_id="official", configuration_fingerprint="public",
            estimated_usage=Usage(calls=4, seconds=30))
        auth = AuthorizationScope(("official",), (), ("public",))
        plan = harness.plan(action, limits=RecoveryLimits(calls=4), authorization_scope=auth)
        with patch.object(Usage, "plus", slower_plus):
            result = harness.execute(plan, auth)
        assert result["state"] == "committed"
        assert harness.repository.status(plan.plan_id)["usage"]["calls"] == 4
    finally:
        harness.tearDown()


@pytest.mark.parametrize("url", [
    "http://www.sec.gov/Archives/annual.htm", "https://outside.example/annual.htm",
    "https://www.sec.gov@outside.example/annual.htm", "https://www.sec.gov:8443/annual.htm",
])
def test_managed_sec_redirect_rejects_non_official_targets_before_a_wire_request(url):
    request = Request("https://www.sec.gov/Archives/annual.htm")
    with patch("openthesis.resilience.remote.before_remote_request") as wire:
        with pytest.raises(RecoveryError, match="OFFICIAL_SOURCE_REDIRECT_UNSAFE"):
            _ManagedSecRedirectHandler().redirect_request(request, None, 302, "Found", Message(), url)
    wire.assert_not_called()


def test_official_sec_redirects_consume_the_actual_shared_call_budget():
    harness = _resilience.ResilienceCoreTests()
    harness.setUp()
    try:
        request = Request("https://www.sec.gov/Archives/annual.htm")
        def produce(context):
            with remote_request_scope(context):
                context.consume(Usage(calls=1))  # The first wire request.
                redirected = _ManagedSecRedirectHandler().redirect_request(
                    request, None, 302, "Found", Message(), "https://data.sec.gov/annual.htm")
                assert redirected.full_url == "https://data.sec.gov/annual.htm"
            return CandidateResult({"source_receipt": "redirected official bytes"})
        harness.register(produce=produce)
        action = harness.action(provider_id="official", configuration_fingerprint="public",
            estimated_usage=Usage(calls=2, seconds=30))
        auth = AuthorizationScope(("official",), (), ("public",))
        result = harness.execute(harness.plan(action, limits=RecoveryLimits(calls=2), authorization_scope=auth), auth)
        assert result["state"] == "committed"
        assert result["usage"]["calls"] == 2
    finally:
        harness.tearDown()


def test_sec_streaming_response_stops_at_its_deadline_without_nested_retries(tmp_path):
    harness = _resilience.ResilienceCoreTests()
    harness.setUp()
    try:
        now = [100.0]
        client = SecClient("Test offline@example.test", tmp_path / "cache", min_interval=0,
                           request_deadline_seconds=1)
        class SlowResponse(Response):
            reads = 0
            def read(self, _size=-1):
                self.reads += 1
                now[0] += 2
                return b"late bytes"
        response = SlowResponse()
        def produce(context):
            with remote_request_scope(context):
                client._request_bytes("https://www.sec.gov/Archives/annual.htm")
            raise AssertionError("a late response cannot produce an accepted receipt")
        harness.register(produce=produce)
        action = harness.action(provider_id="official", configuration_fingerprint="public",
            estimated_usage=Usage(calls=1, seconds=30))
        auth = AuthorizationScope(("official",), (), ("public",))
        plan = harness.plan(action, limits=RecoveryLimits(calls=1, transient_retries=0), authorization_scope=auth)
        with patch.object(client, "_open_managed", return_value=response) as opener, \
                patch("openthesis.sec_client.time.monotonic", side_effect=lambda: now[0]):
            result = harness.execute(plan, auth)
        # This plan deliberately allows no automatic retries; it must return
        # an explicit retry action, never publish the late bytes or self-loop.
        assert result["state"] == "needs_action"
        assert result["diagnostic_code"] == "REMOTE_TRANSPORT_FAILED"
        assert result["next_actions"] == ("retry_official_source",)
        assert opener.call_count == response.reads == 1
        assert not result["committed_revision_id"]
    finally:
        harness.tearDown()
