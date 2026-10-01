from __future__ import annotations

import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    limit: int
    active: int
    waiting: int
    cooldown_remaining_seconds: float = 0.0


class ModelRequestScheduler:
    """Share bounded, adaptive provider concurrency without hidden retries."""

    def __init__(self, limit: int = 2, *, clock: Callable[[], float] = time.monotonic):
        self._maximum_limit = max(1, int(limit))
        self._limit = self._maximum_limit
        self._clock = clock
        self._condition = threading.Condition()
        self._active = 0
        self._waiting = 0
        self._cooldown_until = 0.0

    @property
    def limit(self) -> int:
        with self._condition:
            return self._limit

    @contextmanager
    def slot(self, cancel_check: Callable[[], bool] | None = None) -> Iterator[None]:
        with self._condition:
            self._waiting += 1
            try:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise InterruptedError("model request cancelled while waiting")
                    now = self._clock()
                    cooldown = self._cooldown_until - now
                    if cooldown <= 0 and self._active < self._limit:
                        self._waiting -= 1
                        self._active += 1
                        break
                    timeout = min(0.1, cooldown) if cancel_check is not None else (cooldown or None)
                    self._condition.wait(timeout=timeout)
            except BaseException:
                self._waiting -= 1
                self._condition.notify_all()
                raise
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def mark_rate_limited(self, retry_after_seconds: float | int | None = None) -> None:
        """Cool down sibling work and reduce future concurrency after HTTP 429."""
        try:
            seconds = float(retry_after_seconds) if retry_after_seconds is not None else 30.0
        except (TypeError, ValueError):
            seconds = 30.0
        seconds = max(1.0, min(300.0, seconds))
        with self._condition:
            self._limit = max(1, self._limit // 2)
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)
            self._condition.notify_all()

    def mark_success(self) -> None:
        """Recover concurrency gradually after the provider cooldown has elapsed."""
        with self._condition:
            if self._clock() >= self._cooldown_until and self._limit < self._maximum_limit:
                self._limit += 1
                self._condition.notify_all()

    def snapshot(self) -> SchedulerSnapshot:
        with self._condition:
            return SchedulerSnapshot(
                self._limit,
                self._active,
                self._waiting,
                max(0.0, self._cooldown_until - self._clock()),
            )


_shared_schedulers: weakref.WeakValueDictionary[str, ModelRequestScheduler] = weakref.WeakValueDictionary()
_shared_schedulers_lock = threading.Lock()


def shared_model_scheduler(key: str, limit: int = 2) -> ModelRequestScheduler:
    """Return the process-wide scheduler shared by workflows using one model ID."""
    normalized = str(key).strip() or "unconfigured-model"
    with _shared_schedulers_lock:
        scheduler = _shared_schedulers.get(normalized)
        if scheduler is None:
            scheduler = ModelRequestScheduler(limit=limit)
            _shared_schedulers[normalized] = scheduler
        return scheduler
