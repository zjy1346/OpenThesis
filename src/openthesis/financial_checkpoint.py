"""Small, content-addressed checkpoints for resumable PDF windows.

The checkpoint layer deliberately knows nothing about statement labels or
quality decisions.  It stores only successful, already-parsed window output;
the normal financial compiler remains the sole acceptance boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from concurrent.futures import CancelledError
import hashlib
import json
import multiprocessing as mp
import os
import pickle
import inspect
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterable, Sequence

from .domain import EvidenceRef, FinancialFact


CHECKPOINT_SCHEMA_VERSION = "financial-window-checkpoint-v1"
EXHAUSTED_SCHEMA_VERSION = "financial-window-exhausted-v1"


@dataclass(frozen=True, slots=True)
class CheckpointKey:
    document_hash: str
    parser_version: str
    rules_version: str
    schema_version: str = CHECKPOINT_SCHEMA_VERSION
    identity_digest: str = ""

    @property
    def digest(self) -> str:
        material = "|".join(
            (
                self.document_hash,
                self.parser_version,
                self.rules_version,
                self.schema_version,
                self.identity_digest,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PdfWindow:
    index: int
    pages: tuple[int, ...]

    def __post_init__(self) -> None:
        pages = tuple(sorted({int(page) for page in self.pages if int(page) > 0}))
        if not pages:
            raise ValueError("a PDF window must contain at least one page")
        object.__setattr__(self, "pages", pages)


@dataclass(frozen=True, slots=True)
class WindowCheckpoint:
    key: CheckpointKey
    window: PdfWindow
    facts: tuple[FinancialFact, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    context: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.key.schema_version,
            "key": asdict(self.key),
            "window": asdict(self.window),
            "facts": [fact.to_dict() for fact in self.facts],
            "evidence": [item.to_dict() for item in self.evidence],
            "context": dict(self.context or {}),
        }


class WindowCheckpointStore:
    """Atomic local store; corrupt, partial and stale entries are misses."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: CheckpointKey, window_index: int) -> Path:
        return self.directory / f"{key.digest}-{int(window_index):06d}.json"

    def exhausted_path(self, key: CheckpointKey) -> Path:
        """Return the deterministic-failure marker for one exact input."""
        return self.directory / f"{key.digest}-exhausted.json"

    def save_exhausted(
        self, key: CheckpointKey, *, reason: str, input_fingerprint: str | None = None,
    ) -> None:
        """Atomically remember a deterministic failure, never a transient one."""
        payload = {
            "schema_version": EXHAUSTED_SCHEMA_VERSION,
            "key_digest": key.digest,
            "input_fingerprint": input_fingerprint or key.digest,
            "reason": str(reason)[:240],
        }
        destination = self.exhausted_path(key)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f".{destination.stem}-",
                suffix=".tmp", dir=self.directory, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def load_exhausted(
        self, key: CheckpointKey, *, input_fingerprint: str | None = None,
    ) -> dict[str, str] | None:
        """Load a marker only when it belongs to the exact current input."""
        try:
            payload = json.loads(self.exhausted_path(key).read_text(encoding="utf-8"))
            expected = input_fingerprint or key.digest
            if (
                payload.get("schema_version") != EXHAUSTED_SCHEMA_VERSION
                or payload.get("key_digest") != key.digest
                or payload.get("input_fingerprint") != expected
            ):
                return None
            reason = payload.get("reason")
            return {"reason": str(reason)[:240]} if reason else {"reason": "deterministic_failure"}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def clear_exhausted(self, key: CheckpointKey) -> None:
        try:
            self.exhausted_path(key).unlink(missing_ok=True)
        except OSError:
            pass

    def save(self, checkpoint: WindowCheckpoint) -> None:
        payload = checkpoint.payload()
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        envelope = {
            "payload": payload,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
        data = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        destination = self.path_for(checkpoint.key, checkpoint.window.index)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.stem}-", suffix=".tmp",
                dir=self.directory, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def load(
        self, key: CheckpointKey, window_index: int,
        *, expected_pages: Sequence[int] | None = None,
    ) -> WindowCheckpoint | None:
        path = self.path_for(key, window_index)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = envelope["payload"]
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if envelope.get("sha256") != hashlib.sha256(encoded).hexdigest():
                return None
            if payload.get("schema_version") != key.schema_version:
                return None
            if payload.get("key") != asdict(key):
                return None
            window = payload["window"]
            if int(window["index"]) != int(window_index):
                return None
            if expected_pages is not None:
                actual_pages = tuple(sorted(int(page) for page in window.get("pages", ())))
                if actual_pages != tuple(sorted(int(page) for page in expected_pages)):
                    return None
            facts = tuple(self._decode_fact(item) for item in payload.get("facts", ()))
            evidence = tuple(self._decode_evidence(item) for item in payload.get("evidence", ()))
            context = payload.get("context", {})
            if not isinstance(context, dict):
                return None
            return WindowCheckpoint(
                key, PdfWindow(int(window["index"]), tuple(window["pages"])),
                facts, evidence, context,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    @staticmethod
    def _decode_fact(item: dict[str, Any]) -> FinancialFact:
        value = dict(item)
        bbox = value.get("source_bbox")
        if bbox is not None:
            value["source_bbox"] = tuple(float(part) for part in bbox)
        return FinancialFact(**value)

    @staticmethod
    def _decode_evidence(item: dict[str, Any]) -> EvidenceRef:
        value = dict(item)
        bbox = value.get("bbox")
        if bbox is not None:
            value["bbox"] = tuple(float(part) for part in bbox)
        return EvidenceRef(**value)


def plan_page_windows(
    page_count: int,
    candidate_pages: Iterable[int],
    *,
    window_size: int = 8,
) -> tuple[PdfWindow, ...]:
    """Create deterministic windows covering every indexed candidate page."""

    if int(page_count) < 0:
        raise ValueError("page_count must be non-negative")
    size = max(1, int(window_size))
    pages = sorted({int(page) for page in candidate_pages if 1 <= int(page) <= int(page_count)})
    return tuple(
        PdfWindow(index, tuple(pages[offset : offset + size]))
        for index, offset in enumerate(range(0, len(pages), size))
    )


def run_checkpointed_windows(
    store: WindowCheckpointStore,
    key: CheckpointKey,
    windows: Sequence[PdfWindow],
    worker: Callable[[PdfWindow], Any],
    *,
    max_retries: int = 1,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    timeout_seconds: float | None = None,
    input_fingerprint: str | None = None,
    deterministic_failure: Callable[[Exception], bool] | None = None,
) -> tuple[WindowCheckpoint, ...]:
    """Resume successful windows and retry only failed windows deterministically.

    ``worker`` returns ``(facts, evidence, context)``.  A worker exception is
    isolated to its window; successful checkpoints are never re-run.  When a
    timeout is supplied, pickle-safe workers run in killable child processes;
    non-pickleable fixture closures retain a synchronous seam.
    """

    ordered = tuple(sorted(windows, key=lambda item: item.index))
    exhausted = store.load_exhausted(key, input_fingerprint=input_fingerprint) if input_fingerprint else None
    if exhausted is not None:
        if progress is not None:
            progress(0, len(ordered), "exhausted_same_input")
        return ()
    results: list[WindowCheckpoint] = []
    current_context: dict[str, Any] = {}
    retry_limit = max(0, int(max_retries))
    for position, window in enumerate(ordered, start=1):
        if cancel_check is not None and cancel_check():
            break
        cached = store.load(key, window.index, expected_pages=window.pages)
        if cached is not None:
            results.append(cached)
            current_context = dict(cached.context or {})
            if progress is not None:
                progress(position, len(ordered), "cache-hit")
            continue
        last_error: Exception | None = None
        for _attempt in range(retry_limit + 1):
            if cancel_check is not None and cancel_check():
                break
            try:
                produced = _invoke_worker(
                    worker, window, timeout_seconds, current_context, cancel_check
                )
                facts, evidence, context = produced
                checkpoint = WindowCheckpoint(
                    key, window, tuple(facts), tuple(evidence), dict(context or {})
                )
                store.save(checkpoint)
                results.append(checkpoint)
                current_context = dict(checkpoint.context or {})
                last_error = None
                if progress is not None:
                    progress(position, len(ordered), "checkpointed")
                break
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            if deterministic_failure is not None and deterministic_failure(last_error):
                store.save_exhausted(
                    key,
                    reason=f"{type(last_error).__name__}",
                    input_fingerprint=input_fingerprint or key.digest,
                )
            if progress is not None:
                progress(position, len(ordered), "failed")
            # Later windows may depend on the table context at this boundary.
            # Stop rather than presenting a context-broken suffix as complete;
            # callers can explicitly plan a new independent table as a new run.
            break
    return tuple(results)


def _worker_accepts_context(worker: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(worker)
        signature.bind(object(), object())
        return True
    except (TypeError, ValueError):
        return False


def _checkpoint_worker_entry(
    worker: Callable[[PdfWindow], Any], window: PdfWindow,
    context: dict[str, Any], result_sender: Any,
) -> None:
    try:
        result = worker(window, context) if _worker_accepts_context(worker) else worker(window)
        result_sender.send(("result", result))
    except BaseException as exc:
        try:
            result_sender.send(("error", f"{type(exc).__name__}: {exc}"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        result_sender.close()


def _invoke_worker(
    worker: Callable[[PdfWindow], Any], window: PdfWindow,
    timeout_seconds: float | None, context: dict[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Any:
    """Use a killable child only when a hard timeout is requested.

    Callable closures remain a useful synchronous fixture seam; they still
    receive the prior context even when a timeout was requested.  They cannot
    be hard-killed because they are intentionally test-only/non-pickleable.
    Pickle-safe production workers receive real child-process cancellation and
    timeout termination rather than leaving a background thread behind.
    """

    if timeout_seconds is None:
        return worker(window, dict(context or {})) if _worker_accepts_context(worker) else worker(window)
    try:
        pickle.dumps(worker)
    except (pickle.PickleError, TypeError, AttributeError):
        return worker(window, dict(context or {})) if _worker_accepts_context(worker) else worker(window)
    mp_context = mp.get_context("spawn")
    # A Queue uses a feeder thread in the child.  Waiting for that process to
    # exit before reading can deadlock when a real table payload exceeds the
    # Windows pipe buffer: the feeder waits for a reader while the parent
    # waits for the feeder.  A one-way Pipe lets the parent drain the payload
    # while the worker is still alive and keeps the timeout/cancel boundary.
    result_receiver, result_sender = mp_context.Pipe(duplex=False)
    process = None
    try:
        process = mp_context.Process(
            target=_checkpoint_worker_entry,
            args=(worker, window, dict(context or {}), result_sender),
            name=f"financial-window-{window.index}",
        )
        process.daemon = True
        try:
            process.start()
        except (OSError, RuntimeError):
            # Resource-constrained hosts (and frozen Windows launchers with a
            # temporarily unavailable spawn context) still need a quality-
            # preserving path.  Run the same worker synchronously instead of
            # returning an apparently parsed-but-empty window.  The caller's
            # normal compiler/quality gate remains in force.
            result_sender.close()
            result_receiver.close()
            return worker(window, dict(context or {})) if _worker_accepts_context(worker) else worker(window)
        result_sender.close()
        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        kind: str | None = None
        value: Any = None
        while kind is None:
            if cancel_check is not None and cancel_check():
                raise CancelledError(f"window {window.index} cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"window {window.index} exceeded timeout")
            wait_for = min(0.05, remaining)
            if result_receiver.poll(wait_for):
                try:
                    kind, value = result_receiver.recv()
                except EOFError as exc:
                    raise RuntimeError("window worker exited without a result") from exc
                break
            if not process.is_alive():
                if result_receiver.poll(0.2):
                    try:
                        kind, value = result_receiver.recv()
                    except EOFError as exc:
                        raise RuntimeError("window worker exited without a result") from exc
                    break
                raise RuntimeError("window worker exited without a result")
        process.join(1.0)
        if process.is_alive():
            raise RuntimeError("window worker did not exit after returning a result")
        if kind == "error":
            raise RuntimeError(str(value))
        return value
    finally:
        if process is not None:
            try:
                if process.is_alive():
                    process.terminate()
                process.join(1.0)
            except (AssertionError, OSError):
                pass
        result_sender.close()
        result_receiver.close()
