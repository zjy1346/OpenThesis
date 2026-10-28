"""Red tests for refusing unsafe synchronous fallback on production workers.

These tests intentionally describe the isolation contract before the
production implementation changes: a pickle-safe worker that cannot create
or start its child process must fail closed, rather than run in the caller.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openthesis.financial_checkpoint import (
    CheckpointKey,
    PdfWindow,
    WindowCheckpoint,
    WindowCheckpointStore,
    _invoke_worker,
    run_checkpointed_windows,
)


_CALLS: list[int] = []


def _picklable_recording_worker(window: PdfWindow, context: dict | None = None):
    """A module-level callable matching the production worker shape."""
    _CALLS.append(window.index)
    return (), (), dict(context or {})


class _PipeDeniedContext:
    def Pipe(self, duplex=False):  # noqa: N802 - mirrors multiprocessing API
        raise PermissionError(5, "access denied")


class _Endpoint:
    def close(self):
        return None


class _StartDeniedProcess:
    daemon = False

    def start(self):
        raise OSError(5, "access denied")

    def is_alive(self):
        return False

    def join(self, _timeout=None):
        return None

    def terminate(self):
        return None


class _StartDeniedContext:
    def Pipe(self, duplex=False):  # noqa: N802 - mirrors multiprocessing API
        return _Endpoint(), _Endpoint()

    def Process(self, **_kwargs):  # noqa: N802 - mirrors multiprocessing API
        return _StartDeniedProcess()


def _key() -> CheckpointKey:
    return CheckpointKey("document", "parser", "rules", identity_digest="identity")


class CheckpointIsolationFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        _CALLS.clear()

    def test_pipe_permission_error_never_runs_picklable_worker_synchronously(self) -> None:
        with patch(
            "openthesis.financial_checkpoint.mp.get_context",
            return_value=_PipeDeniedContext(),
        ):
            with self.assertRaises(Exception) as raised:
                _invoke_worker(_picklable_recording_worker, PdfWindow(0, (1,)), 1.0, {})

        message = str(raised.exception).lower()
        self.assertTrue("worker" in message and ("start" in message or "isolation" in message))
        self.assertEqual(_CALLS, [])

    def test_process_start_error_never_runs_picklable_worker_synchronously(self) -> None:
        with patch(
            "openthesis.financial_checkpoint.mp.get_context",
            return_value=_StartDeniedContext(),
        ):
            with self.assertRaises(Exception) as raised:
                _invoke_worker(_picklable_recording_worker, PdfWindow(0, (1,)), 1.0, {})

        message = str(raised.exception).lower()
        self.assertTrue("worker" in message and ("start" in message or "isolation" in message))
        self.assertEqual(_CALLS, [])

    def test_existing_checkpoint_survives_worker_isolation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(Path(directory))
            key = _key()
            first = PdfWindow(0, (1,))
            second = PdfWindow(1, (2,))
            store.save(WindowCheckpoint(key, first, context={"last_page": 1}))

            with patch(
                "openthesis.financial_checkpoint.mp.get_context",
                return_value=_PipeDeniedContext(),
            ):
                completed = run_checkpointed_windows(
                    store,
                    key,
                    (first, second),
                    _picklable_recording_worker,
                    timeout_seconds=1.0,
                )

        self.assertEqual([item.window.index for item in completed], [0])
        self.assertEqual(_CALLS, [])


if __name__ == "__main__":
    unittest.main()
