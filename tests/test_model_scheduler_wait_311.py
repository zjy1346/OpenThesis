from __future__ import annotations

import threading
import unittest

from openthesis.model_scheduler import ModelRequestScheduler


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCondition:
    """Deterministic wait boundary that records timeouts and controls wakeups."""

    def __init__(self, on_wait) -> None:
        self.lock = threading.RLock()
        self.on_wait = on_wait
        self.timeouts: list[float | None] = []
        self.notifications = 0
        self.wake_notifications = 0
        self._waiting = False

    def __enter__(self) -> FakeCondition:
        self.lock.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.lock.release()

    def wait(self, timeout: float | None = None) -> None:
        self.timeouts.append(timeout)
        self._waiting = True
        try:
            self.on_wait(timeout)
        finally:
            self._waiting = False

    def notify_all(self) -> None:
        self.notifications += 1
        if self._waiting:
            self.wake_notifications += 1


class ModelRequestSchedulerWaitTests(unittest.TestCase):
    def test_expired_cooldown_with_full_slot_checks_cancellation_after_positive_wait(self) -> None:
        clock = FakeClock(1000.0)
        state = {"cancelled": False}
        scheduler = ModelRequestScheduler(limit=1, clock=clock)
        scheduler._active = 1
        scheduler._cooldown_until = 0.0
        condition = FakeCondition(lambda _timeout: state.__setitem__("cancelled", True))
        scheduler._condition = condition

        with self.assertRaisesRegex(InterruptedError, "model request cancelled while waiting"):
            with scheduler.slot(lambda: state["cancelled"]):
                self.fail("a cancelled waiter must not acquire the slot")

        self.assertEqual(condition.timeouts, [0.1])
        self.assertGreater(condition.timeouts[0], 0.0)
        self.assertGreaterEqual(condition.notifications, 1)

    def test_expired_cooldown_without_cancel_waits_for_slot_notification(self) -> None:
        clock = FakeClock(1000.0)
        scheduler = ModelRequestScheduler(limit=1, clock=clock)
        scheduler._active = 1
        scheduler._cooldown_until = 0.0
        condition = FakeCondition(lambda _timeout: None)

        def release_slot_and_notify(_timeout: float | None) -> None:
            scheduler._active = 0
            condition.notify_all()

        condition.on_wait = release_slot_and_notify
        scheduler._condition = condition

        acquired = False
        with scheduler.slot():
            acquired = True

        self.assertTrue(acquired)
        self.assertEqual(condition.timeouts, [None])
        self.assertEqual(condition.wake_notifications, 1)
        self.assertGreaterEqual(condition.notifications, 1)

    def test_active_cooldown_without_cancel_waits_only_for_remaining_cooldown(self) -> None:
        clock = FakeClock(1000.0)
        scheduler = ModelRequestScheduler(limit=1, clock=clock)
        scheduler._active = 1
        scheduler._cooldown_until = 1000.35

        def wake_after_cooldown(timeout: float | None) -> None:
            self.assertIsNotNone(timeout)
            clock.advance(float(timeout))
            scheduler._active = 0

        condition = FakeCondition(wake_after_cooldown)
        scheduler._condition = condition

        with scheduler.slot():
            self.assertEqual(scheduler.snapshot().active, 1)

        self.assertEqual(len(condition.timeouts), 1)
        self.assertAlmostEqual(condition.timeouts[0], 0.35)
        self.assertGreater(condition.timeouts[0], 0.0)

    def test_active_cooldown_with_cancel_waits_for_minimum_interval_then_cancels(self) -> None:
        for remaining, expected_wait in ((0.05, 0.05), (0.5, 0.1)):
            with self.subTest(remaining=remaining):
                clock = FakeClock(1000.0)
                state = {"cancelled": False}
                scheduler = ModelRequestScheduler(limit=1, clock=clock)
                scheduler._active = 1
                scheduler._cooldown_until = clock.now + remaining

                def cancel_on_wake(timeout: float | None) -> None:
                    self.assertIsNotNone(timeout)
                    clock.advance(float(timeout))
                    state["cancelled"] = True

                condition = FakeCondition(cancel_on_wake)
                scheduler._condition = condition

                with self.assertRaises(InterruptedError):
                    with scheduler.slot(lambda: state["cancelled"]):
                        self.fail("a cancelled waiter must not acquire the slot")

                self.assertEqual(len(condition.timeouts), 1)
                self.assertAlmostEqual(condition.timeouts[0], expected_wait)
                self.assertGreater(condition.timeouts[0], 0.0)
                self.assertGreaterEqual(condition.notifications, 1)


if __name__ == "__main__":
    unittest.main()
