"""Nested page coordinator must not restore an unbounded shutdown wait."""
import threading
import time

from openthesis.vision_financials import VisionExtractionResult, VisionTaskCoordinator
from test_vision_financials import MemoryVisionJournal, _company, _config, _filing, _page


def test_page_worker_that_ignores_cancel_cannot_hang_or_cache_late_result():
    release = threading.Event()
    journal = MemoryVisionJournal()

    class Blocked:
        def extract(self, *_args, **_kwargs):
            release.wait(2)
            return VisionExtractionResult(error_code="late-return")

    safety = threading.Timer(.6, release.set)
    safety.start()
    started = time.monotonic()
    try:
        result = VisionTaskCoordinator(Blocked(), journal=journal).extract(
            _company(), _filing(), (_page(),), _config(timeout_seconds=.03))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        safety.cancel()
    assert result.error_code == "VISION_TIMEOUT"
    assert elapsed < .4, "page shutdown cannot outlive the task's deadline indefinitely"
    assert not journal.results


def test_page_cancel_is_observed_while_future_is_not_done():
    release, started, cancelled, finished = (threading.Event() for _ in range(4))
    observed = []

    class Blocked:
        def extract(self, *_args, **_kwargs):
            started.set()
            release.wait(2)
            return VisionExtractionResult(error_code="late-return")

    def collect():
        observed.append(VisionTaskCoordinator(Blocked()).extract(
            _company(), _filing(), (_page(),), _config(timeout_seconds=2), cancel_check=cancelled.is_set))
        finished.set()

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert started.wait(1)
        cancelled.set()
        assert finished.wait(.4), "cancel must wake the collector, not wait for as_completed"
    finally:
        release.set()
        thread.join(3)
    assert observed[0].error_code == "VISION_CANCELLED"
