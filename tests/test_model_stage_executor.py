from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from openthesis.providers import ProviderError
from openthesis.research_execution.model import (
    ModelStageCancelled,
    ModelStageExecutor,
    ModelStageRequest,
    SynthesisContextLimitError,
    provider_context_capability,
    provider_declares_context_capability,
)


class ImmediateScheduler:
    """Local stand-in; verifies request lifetime without sleeping through 429."""

    def __init__(self):
        self.active = 0
        self.successes = 0
        self.delays = []

    @contextmanager
    def slot(self, cancel_check):
        if cancel_check():
            raise InterruptedError()
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    def mark_rate_limited(self, delay):
        self.delays.append(delay)

    def mark_success(self):
        self.successes += 1


class RecordingProvider:
    def __init__(self, response=None, failures=()):
        self.response = response if response is not None else {"claims": [{"text": "original"}]}
        self.failures = list(failures)
        self.requests = []

    def generate(self, system_prompt, user_prompt, *, json_mode=True):
        self.requests.append((system_prompt, user_prompt, json_mode))
        if self.failures:
            raise self.failures.pop(0)
        return self.response


class ModelStageExecutorTests(unittest.TestCase):
    def executor(self, provider, cancel_check=None):
        scheduler = ImmediateScheduler()
        return ModelStageExecutor(provider, scheduler, cancel_check=cancel_check), scheduler

    def test_candidate_and_gate_are_detached_without_fabricating_validation(self):
        provider = RecordingProvider()
        executor, _ = self.executor(provider)
        gate = {"status": "ready", "counts": {"quantitative_facts": 1}}
        result = executor.execute(ModelStageRequest("financial", "system", "complete material"),
                                  material_adequacy=gate)
        result["claims"][0]["text"] = "edited"
        result["_material_adequacy"]["counts"]["quantitative_facts"] = 2
        self.assertEqual(provider.response["claims"][0]["text"], "original")
        self.assertEqual(gate["counts"]["quantitative_facts"], 1)
        self.assertNotIn("_verification_state", result)
        self.assertEqual(provider.requests, [("system", "complete material", True)])

    def test_provider_call_is_held_inside_the_scheduler_slot(self):
        scheduler = ImmediateScheduler()

        class SlotObservingProvider(RecordingProvider):
            def __init__(self):
                super().__init__()
                self.active_slots_at_call = []

            def generate(self, system_prompt, user_prompt, *, json_mode=True):
                self.active_slots_at_call.append(scheduler.active)
                return super().generate(system_prompt, user_prompt, json_mode=json_mode)

        provider = SlotObservingProvider()
        executor = ModelStageExecutor(provider, scheduler)

        executor.execute(ModelStageRequest("scheduled", "system", "source-backed input"))

        self.assertEqual(provider.active_slots_at_call, [1])
        self.assertEqual(provider.requests, [("system", "source-backed input", True)])
        self.assertEqual(scheduler.active, 0)
        self.assertEqual(scheduler.successes, 1)

    def test_insufficient_material_never_calls_provider_even_when_over_budget(self):
        provider = RecordingProvider()
        executor, _ = self.executor(provider)
        gate = {"status": "insufficient", "missing": ["business_disclosures"]}
        for language in ("en", "zh-CN", "zh-Hant"):
            result = executor.execute(ModelStageRequest("business", "system", "oversized", max_input_bytes=1),
                                      material_adequacy=gate, language=language)
            self.assertEqual(result["_response_error"], "insufficient_material")
            self.assertEqual(result["claims"], [])
        self.assertEqual(provider.requests, [])

    def test_utf8_limit_rejects_complete_input_without_truncation(self):
        provider = RecordingProvider()
        executor, _ = self.executor(provider)
        request = ModelStageRequest("stage", "财务", "🙂", max_input_bytes=9)
        self.assertEqual(request.input_bytes, 10)
        with self.assertRaises(SynthesisContextLimitError) as caught:
            executor.execute(request)
        self.assertEqual(caught.exception.required_bytes, 10)
        self.assertEqual(caught.exception.sections, [{"name": "stage", "bytes": 10}])
        self.assertEqual(provider.requests, [])
        executor.execute(ModelStageRequest("stage", "财务", "🙂", max_input_bytes=10))
        self.assertEqual(provider.requests, [("财务", "🙂", True)])

    def test_byte_only_capability_is_honored_and_bool_is_not_a_capacity(self):
        provider = SimpleNamespace(max_input_bytes=200)
        self.assertTrue(provider_declares_context_capability(provider))
        self.assertEqual(provider_context_capability(provider).max_input_bytes, 200)
        invalid = SimpleNamespace(max_input_bytes=True, context_window_tokens=True)
        self.assertFalse(provider_declares_context_capability(invalid))
        self.assertGreater(provider_context_capability(invalid).max_input_bytes, 200)

    def test_request_identity_is_unambiguous_and_has_no_prompt_text(self):
        first = ModelStageRequest("stage", "ab", "c")
        second = ModelStageRequest("stage", "a", "bc")
        self.assertNotEqual(first.input_digest, second.input_digest)
        self.assertEqual(len(first.input_digest), 64)
        self.assertEqual(first.input_digest, ModelStageRequest("stage", "ab", "c").input_digest)
        self.assertNotEqual(first.input_digest, ModelStageRequest("stage", "ab", "c", False).input_digest)

    def test_legacy_initial_rate_limit_policy_has_two_retries_on_identical_request(self):
        errors = [ProviderError("limited", code="MODEL_RATE_LIMITED", retry_after_seconds=1)] * 2
        provider = RecordingProvider(failures=errors)
        executor, scheduler = self.executor(provider)
        executor.execute(ModelStageRequest("stage", "system", "all material"))
        self.assertEqual(provider.requests, [("system", "all material", True)] * 3)
        self.assertEqual(scheduler.delays, [1, 1])
        self.assertEqual(scheduler.active, 0)

    def test_single_attempt_does_not_multiply_recovery_coordinator_retries(self):
        provider = RecordingProvider(failures=[ProviderError("limited", code="MODEL_RATE_LIMITED")])
        executor, scheduler = self.executor(provider)
        with self.assertRaises(ProviderError):
            executor.execute(ModelStageRequest("stage", "system", "source"), single_attempt=True)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(scheduler.delays, [30])

    def test_non_rate_limit_errors_are_not_blindly_repeated(self):
        for code in ("MODEL_UNAUTHORIZED", "MODEL_CONTEXT_LIMIT", "MODEL_TIMEOUT"):
            provider = RecordingProvider(failures=[ProviderError("hidden", code=code, retryable=True)])
            executor, _ = self.executor(provider)
            with self.assertRaises(ProviderError) as caught:
                executor.execute(ModelStageRequest("stage", "system", "source"))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(provider.requests), 1)

    def test_malformed_provider_candidate_is_a_stable_failure_without_retry(self):
        provider = RecordingProvider(response=["invalid shape"])
        executor, scheduler = self.executor(provider)
        with self.assertRaisesRegex(ValueError, "MODEL_STAGE_CANDIDATE_INVALID"):
            executor.execute(ModelStageRequest("stage", "system", "source"))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(scheduler.successes, 0)

    def test_cancel_before_admission_never_calls_provider(self):
        provider = RecordingProvider()
        executor, _ = self.executor(provider, lambda: True)
        with self.assertRaises(ModelStageCancelled):
            executor.execute(ModelStageRequest("stage", "system", "source"))
        self.assertEqual(provider.requests, [])

    def test_cancel_acknowledged_before_blocking_provider_returns_and_late_result_is_not_success(self):
        started, release, cancelled, returned = (threading.Event() for _ in range(4))

        class BlockingProvider:
            def generate(self, *args, **kwargs):
                started.set()
                release.wait(2)
                return {"late": True}

        executor, scheduler = self.executor(BlockingProvider(), cancelled.is_set)
        outcome = []

        def call():
            try:
                outcome.append(executor.execute(ModelStageRequest("stage", "system", "source")))
            except ModelStageCancelled:
                outcome.append("cancelled")
            finally:
                returned.set()

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            cancelled.set()
            self.assertTrue(returned.wait(1), "caller did not acknowledge cancellation")
            self.assertEqual(outcome, ["cancelled"])
            self.assertFalse(release.is_set())
        finally:
            release.set()
            caller.join(1)
        self.assertEqual(scheduler.successes, 0)


if __name__ == "__main__":
    unittest.main()
