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
import pickle
import inspect
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterable, Sequence

from .domain import EvidenceRef, FinancialFact
from .financial_contracts import ROE_ROW_MAPPING_VERSION
from .financial_taxonomy import FINANCIAL_MAPPING_VERSION, cost_row_mentions


CHECKPOINT_SCHEMA_VERSION = "financial-window-checkpoint-v1"
EXHAUSTED_SCHEMA_VERSION = "financial-window-exhausted-v1"
PAGE_WINDOW_INDEX_BASE = 1 << 65


def _needs_roe_remap(payload: dict[str, Any]) -> bool:
    return payload.get("roe_row_mapping_version") != ROE_ROW_MAPPING_VERSION and any(
        isinstance(fact, dict)
        and fact.get("concept") in {"reported_roe", "reported_roe_excluding_nonrecurring"}
        and str(fact.get("parser_version", "")).startswith("financial-ingestion-ast")
        for fact in payload.get("facts", ()))


def _needs_cost_remap(payload: dict[str, Any]) -> bool:
    """Reparse legacy windows containing either fixed cost family.

    Old single-family caches cannot establish that a correct-looking COGS row
    was parsed under the new independent total-operating-cost mapping. Only
    those old windows are invalidated; cost-free and current-marker windows
    keep their normal checkpoint reuse path.
    """
    if payload.get("financial_mapping_version") == FINANCIAL_MAPPING_VERSION:
        return False
    return any(
        isinstance(fact, dict)
        and (
            fact.get("concept") in {"cost_of_revenue", "total_operating_cost"}
            or any(cost_row_mentions(fact.get(key, ""))
                   for key in ("reported_concept", "raw_text", "excerpt"))
        )
        for fact in (*payload.get("facts", ()), *payload.get("evidence", ()))
    )


class WorkerIsolationUnavailable(RuntimeError):
    """A host failure, never a deterministic failure of a financial source."""

    code = "PDF_WORKER_START_FAILED"

    def __init__(self):
        super().__init__("PDF worker process start/isolation unavailable")


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
            "roe_row_mapping_version": ROE_ROW_MAPPING_VERSION,
            "financial_mapping_version": FINANCIAL_MAPPING_VERSION,
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
        self.write_elapsed_seconds = 0.0

    def path_for(self, key: CheckpointKey, window_index: int) -> Path:
        return self.directory / f"{key.digest}-{int(window_index):06d}.json"

    def exhausted_path(self, key: CheckpointKey) -> Path:
        """Return the deterministic-failure marker for one exact input."""
        return self.directory / f"{key.digest}-exhausted.json"

    def work_path(self, key: CheckpointKey, window: PdfWindow) -> Path:
        return self.directory / "work-items" / f"{key.digest}-work-{window.index}.json"

    def save_work(self, key: CheckpointKey, window: PdfWindow, state: str,
                  *, strategy: str = "positioned", artifact: str = "", identity: dict | None = None) -> None:
        """Work state is not a fact or a successful-window checkpoint.

        The key binds bytes, parser/rules and issuer/period identity. Pages and
        strategy bind the effective input; no clock/heartbeat counts as work.
        """
        payload = {"contract": "financial-work-v1", "key": key.digest,
                   "pages": list(window.pages), "window": window.index,
                   "state": state, "strategy": strategy, "artifact": artifact}
        payload['identity'] = {**dict(identity or {}), 'source_hash': key.document_hash,
            'backend': 'positioned-pdf', 'backend_version': key.parser_version,
            'rules_version': key.rules_version, 'source_identity_digest': key.identity_digest}
        destination = self.work_path(key, window)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def load_work(self, key: CheckpointKey, window: PdfWindow) -> dict:
        try:
            item = json.loads(self.work_path(key, window).read_text(encoding="utf-8"))
            if (item.get("contract") == "financial-work-v1" and item.get("key") == key.digest
                    and item.get("pages") == list(window.pages)):
                return item
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def work_progress(self, key: CheckpointKey) -> dict:
        artifacts = []
        pending_inputs = []
        for path in (self.directory / "work-items").glob(f"{key.digest}-work-*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if (item.get('key') == key.digest and item.get('strategy') == 'split'
                        and item.get('state') == 'progressed' and len(item.get('pages', ())) > 1):
                    pages = item['pages']
                    middle = len(pages) // 2
                    for child in (pages[:middle], pages[middle:]):
                        material = json.dumps([key.digest, item['window'], child, 'positioned'], separators=(',', ':'))
                        pending_inputs.append(hashlib.sha256(material.encode()).hexdigest())
                if (item.get("key") == key.digest and item.get("state") == "ready"
                        and item.get("artifact")):
                    window = PdfWindow(item["window"], tuple(item["pages"]))
                    saved = self.load(key, window.index, expected_pages=window.pages)
                    if saved is not None:
                        payload = json.dumps(saved.payload(), sort_keys=True, separators=(",", ":"))
                        if hashlib.sha256(payload.encode()).hexdigest() == item["artifact"]:
                            artifacts.append(f"{window.index}:{item['artifact']}")
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return {"ready_items": len(artifacts), "artifacts": sorted(set(artifacts)),
                "pending_inputs": sorted(set(pending_inputs))}

    def save_exhausted(
        self, key: CheckpointKey, *, reason: str, input_fingerprint: str | None = None,
        failed_window: PdfWindow | None = None,
    ) -> None:
        """Atomically remember a deterministic failure, never a transient one."""
        payload = {
            "schema_version": EXHAUSTED_SCHEMA_VERSION,
            "roe_row_mapping_version": ROE_ROW_MAPPING_VERSION,
            "financial_mapping_version": FINANCIAL_MAPPING_VERSION,
            "key_digest": key.digest,
            "input_fingerprint": input_fingerprint or key.digest,
            "reason": str(reason)[:240],
        }
        if failed_window is not None:
            payload["failed_window"] = asdict(failed_window)
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
    ) -> dict[str, Any] | None:
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
            if payload.get("roe_row_mapping_version") != ROE_ROW_MAPPING_VERSION:
                # A deterministic stop under an affected old mapping cannot
                # fence its corrected local retry forever. Inspect only this
                # document's hash-checked windows, not the cache/library.
                for path in self.directory.glob(f"{key.digest}-[0-9]*.json"):
                    try:
                        envelope = json.loads(path.read_text(encoding="utf-8"))
                        prior = envelope["payload"]
                        encoded = json.dumps(prior, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")
                        if (envelope.get("sha256") == hashlib.sha256(encoded).hexdigest()
                                and prior.get("key") == asdict(key) and _needs_roe_remap(prior)):
                            return None
                    except (OSError, TypeError, ValueError, KeyError):
                        continue
            if payload.get("financial_mapping_version") != FINANCIAL_MAPPING_VERSION:
                # A failure marker only fences this source when it has no
                # successful checkpoint containing a row affected by the
                # mapping change. Other windows keep their original cache.
                for path in self.directory.glob(f"{key.digest}-[0-9]*.json"):
                    try:
                        envelope = json.loads(path.read_text(encoding="utf-8"))
                        prior = envelope["payload"]
                        encoded = json.dumps(prior, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")
                        if (envelope.get("sha256") == hashlib.sha256(encoded).hexdigest()
                                and prior.get("key") == asdict(key) and _needs_cost_remap(prior)):
                            return None
                    except (OSError, TypeError, ValueError, KeyError):
                        continue
            reason = payload.get("reason")
            result = {"reason": str(reason)[:240] if reason else "deterministic_failure"}
            window = payload.get("failed_window")
            if (isinstance(window, dict) and type(window.get("index")) is int
                    and window["index"] >= 0 and isinstance(window.get("pages"), list)
                    and window["pages"] and all(type(page) is int and page > 0 for page in window["pages"])):
                result["failed_window"] = {"index": window["index"], "pages": sorted(set(window["pages"]))}
            return result
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def clear_exhausted(self, key: CheckpointKey) -> None:
        try:
            self.exhausted_path(key).unlink(missing_ok=True)
        except OSError:
            pass

    def save(self, checkpoint: WindowCheckpoint) -> None:
        write_started = time.monotonic()
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
            self.write_elapsed_seconds += time.monotonic() - write_started
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
            # Keep the unchanged page windows usable. Only old windows that
            # actually contain affected ROE candidates require local reparse;
            # original files and other checkpoints are not removed.
            if _needs_roe_remap(payload):
                return None
            if _needs_cost_remap(payload):
                return None
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

    def load_all(self, key: CheckpointKey, *, before_index: int | None = None) -> tuple[WindowCheckpoint, ...]:
        """Read every valid completed window for this exact source identity.

        This is used when migrating from coarse windows to page-sized work:
        completed legacy windows and split children stay reusable without
        weakening the source, parser, rule, or checkpoint integrity checks.
        """
        checkpoints = []
        prefix = f"{key.digest}-"
        for path in self.directory.glob(f"{key.digest}-[0-9]*.json"):
            suffix = path.stem.removeprefix(prefix)
            if not suffix.isdigit():
                continue
            index = int(suffix)
            if before_index is not None and index >= before_index:
                continue
            checkpoint = self.load(key, index)
            if checkpoint is not None:
                checkpoints.append(checkpoint)
        return tuple(checkpoints)

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


def plan_checkpointed_page_windows(
    store: WindowCheckpointStore,
    key: CheckpointKey,
    page_count: int,
    candidate_pages: Iterable[int],
) -> tuple[PdfWindow, ...]:
    """Plan one-page work while preserving complete legacy window checkpoints.

    Valid pre-existing coarse windows or split children are reused as ordered
    groups. Every uncovered page receives a stable, namespaced page identity,
    so interruption/resume never replays successful pages or overwrites an old
    checkpoint merely because window sizes changed.
    """
    pages = tuple(sorted({int(page) for page in candidate_pages if 1 <= int(page) <= int(page_count)}))
    if not pages:
        return ()
    page_set = set(pages)
    selected: list[PdfWindow] = []
    covered: set[int] = set()
    legacy = store.load_all(key, before_index=PAGE_WINDOW_INDEX_BASE)
    for checkpoint in sorted(legacy, key=lambda item: (
            item.window.pages[0], -len(item.window.pages), item.window.index)):
        window_pages = set(checkpoint.window.pages)
        if window_pages.issubset(page_set) and not window_pages.intersection(covered):
            selected.append(checkpoint.window)
            covered.update(window_pages)
    for page in pages:
        if page in covered:
            continue
        material = f"{key.digest}|financial-page-v1|{page}"
        index = PAGE_WINDOW_INDEX_BASE + int(hashlib.sha256(material.encode()).hexdigest()[:16], 16)
        cached = store.load(key, index, expected_pages=(page,))
        selected.append(cached.window if cached is not None else PdfWindow(index, (page,)))
        covered.add(page)
    return tuple(sorted(selected, key=lambda item: (item.pages[0], item.pages[-1], item.index)))


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
    on_failure: Callable[[PdfWindow, Exception], None] | None = None,
    deadline: float | None = None,
    scheduling_slice: bool = False,
    adaptive: bool = False,
    independent_pages: bool = False,
    work_identity: dict | None = None,
) -> tuple[WindowCheckpoint, ...]:
    """Resume successful windows and retry only failed windows deterministically.

    ``worker`` returns ``(facts, evidence, context)``.  A worker exception is
    isolated to its window; successful checkpoints are never re-run.  When a
    timeout is supplied, pickle-safe workers run in killable child processes;
    non-pickleable fixture closures retain a synchronous seam.
    """

    ordered = tuple(windows) if independent_pages else tuple(sorted(
        windows, key=lambda item: (item.pages[0], item.pages[-1], item.index)))
    total_pages = len({page for window in ordered for page in window.pages})
    completed_pages: set[int] = set()

    try:
        detailed_progress = progress is not None and len(inspect.signature(progress).parameters) >= 4
    except (TypeError, ValueError):
        detailed_progress = False

    def emit_progress(status: str, current_page: int | None = None) -> None:
        if progress is not None:
            if detailed_progress:
                progress(len(completed_pages), total_pages, status, {"current_page": current_page})
            else:
                progress(len(completed_pages), total_pages, status)

    def mark_checkpoint(checkpoint: WindowCheckpoint, status: str) -> None:
        before = len(completed_pages)
        completed_pages.update(checkpoint.window.pages)
        if len(completed_pages) != before:
            emit_progress(status, checkpoint.window.pages[-1])

    exhausted = store.load_exhausted(key, input_fingerprint=input_fingerprint)
    if exhausted is not None and not independent_pages:
        # A deterministic failure fences the first missing window for this
        # exact input, but successful preceding windows remain useful. Return
        # that contiguous cached prefix without repeating any worker calls.
        completed_prefix: list[WindowCheckpoint] = []
        for window in ordered:
            cached = store.load(key, window.index, expected_pages=window.pages)
            if cached is None:
                if progress is not None:
                    emit_progress("exhausted_same_input")
                break
            completed_prefix.append(cached)
            mark_checkpoint(cached, "cache-hit")
        else:
            if not ordered:
                emit_progress("exhausted_same_input")
        return tuple(completed_prefix)
    results: list[WindowCheckpoint] = []
    current_context: dict[str, Any] = {}
    retry_limit = max(0, int(max_retries))
    partial_children: list[WindowCheckpoint] = []

    def produce(window, context, root_index=None):
        """Persist a binary subdivision, retaining contiguous table context.

        A timeout changes effective input rather than replaying the same large
        native unit. A completed child survives cancellation/restart. Only a
        complete parent is returned to callers; partial table context is never
        silently admitted. Technical worker timeouts remain in force.
        """
        cached = store.load(key, window.index, expected_pages=window.pages)
        if cached is not None:
            if root_index is not None and window.index != root_index:
                partial_children.append(cached)
            mark_checkpoint(cached, "cache-hit")
            return cached
        if cancel_check and cancel_check():
            raise CancelledError()
        work = store.load_work(key, window)
        if work.get("state") == "exhausted":
            raise TimeoutError("unchanged financial work item exhausted")
        split = work.get("strategy") == "split"
        if not split:
            store.save_work(key, window, "running", identity=work_identity)
            if independent_pages and detailed_progress:
                emit_progress("parsing", window.pages[0])
            try:
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise CancelledError("scheduling slice ended")
                limit = timeout_seconds if scheduling_slice or remaining is None else (
                    min(timeout_seconds, remaining) if timeout_seconds is not None else remaining)
                facts, evidence, next_context = _invoke_worker(worker, window, limit, context, cancel_check)
                saved = WindowCheckpoint(key, window, tuple(facts), tuple(evidence), dict(next_context or {}))
                store.save(saved)
                if root_index is not None and window.index != root_index:
                    partial_children.append(saved)
                mark_checkpoint(saved, "checkpointed")
                artifact = hashlib.sha256(json.dumps(saved.payload(), sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
                store.save_work(key, window, "ready", artifact=artifact, identity=work_identity)
                return saved
            except TimeoutError:
                if len(window.pages) <= 1:
                    store.save_work(key, window, "exhausted", identity=work_identity)
                    raise
                store.save_work(key, window, "progressed", strategy="split", identity=work_identity)
                split = True
        if split:
            middle = len(window.pages) // 2
            children = []
            next_context = dict(context)
            for pages in (window.pages[:middle], window.pages[middle:]):
                if deadline is not None and time.monotonic() >= deadline:
                    raise CancelledError("scheduling slice ended")
                # Stable child identities independent of traversal/window count.
                material = f"{window.index}|{','.join(map(str, pages))}"
                index = 1_000_000_000 + int(hashlib.sha256(material.encode()).hexdigest()[:16], 16)
                child = produce(PdfWindow(index, pages), next_context, root_index=root_index)
                children.append(child)
                next_context = dict(child.context or {})
            saved = WindowCheckpoint(key, window,
                tuple(fact for child in children for fact in child.facts),
                tuple(ref for child in children for ref in child.evidence), next_context)
            store.save(saved)
            mark_checkpoint(saved, "checkpointed")
            artifact = hashlib.sha256(json.dumps(saved.payload(), sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            store.save_work(key, window, "ready", strategy="split", artifact=artifact, identity=work_identity)
            return saved
    prior_page = None
    for position, window in enumerate(ordered, start=1):
        if independent_pages and prior_page is not None and window.pages[0] != prior_page + 1:
            current_context = {}
        prior_page = window.pages[-1]
        cached = store.load(key, window.index, expected_pages=window.pages)
        if cached is not None:
            results.append(cached)
            current_context = dict(cached.context or {})
            mark_checkpoint(cached, "cache-hit")
            continue
        # A spent deadline prohibits new parsing, not reading already durable
        # pages. Otherwise a resume can hide a complete cached prefix as empty.
        if deadline is not None and time.monotonic() >= deadline:
            emit_progress("budget_exhausted_resumable")
            break
        if cancel_check is not None and cancel_check():
            break
        last_error: Exception | None = None
        partial_start = len(partial_children)
        for _attempt in range(1 if adaptive else retry_limit + 1):
            if cancel_check is not None and cancel_check():
                break
            try:
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("document budget exhausted; checkpoints remain resumable")
                # A scheduling slice stops admission of the NEXT window; it
                # must not kill/restart the current bounded native unit. Its
                # independent technical timeout still isolates a hung parser.
                worker_timeout = timeout_seconds if scheduling_slice else (
                    (min(timeout_seconds, remaining) if timeout_seconds is not None else remaining) if remaining is not None else timeout_seconds)
                if adaptive:
                    checkpoint = produce(window, current_context, root_index=window.index)
                else:
                    facts, evidence, context = _invoke_worker(
                        worker, window, worker_timeout, current_context, cancel_check)
                    checkpoint = WindowCheckpoint(
                        key, window, tuple(facts), tuple(evidence), dict(context or {}))
                    store.save(checkpoint)
                    mark_checkpoint(checkpoint, "checkpointed")
                results.append(checkpoint)
                current_context = dict(checkpoint.context or {})
                del partial_children[partial_start:]
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if isinstance(exc, WorkerIsolationUnavailable):
                    # Retrying the same denied host operation cannot repair a
                    # document. Leave it resumable after the host is restored.
                    break
                from .financial_source_errors import PDF_RUNTIME_FAILURE_CODES
                if getattr(exc, 'code', '') in PDF_RUNTIME_FAILURE_CODES:
                    break
                if deterministic_failure is not None and deterministic_failure(exc):
                    store.save_exhausted(
                        key,
                        reason=f"{type(exc).__name__}",
                        input_fingerprint=input_fingerprint or key.digest,
                        failed_window=window,
                    )
                    break
        if last_error is not None:
            results.extend(partial_children[partial_start:])
            if partial_children[partial_start:]:
                current_context = dict(partial_children[-1].context or {})
            if on_failure is not None:
                on_failure(window, last_error)
            emit_progress("failed")
            if independent_pages and not isinstance(last_error, (WorkerIsolationUnavailable, CancelledError)):
                from .financial_source_errors import PDF_RUNTIME_FAILURE_CODES
                if getattr(last_error, "code", "") not in PDF_RUNTIME_FAILURE_CODES:
                    # A missing page cannot donate its header to a suffix.
                    # Independent later pages can still establish their own
                    # statement/period context and reach the same compiler.
                    store.save_work(key, window, "exhausted", identity=work_identity)
                    current_context = {}
                    continue
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


def _checkpoint_operation(worker, window, context):
    from .pdf_worker import pdf_phase
    pdf_phase('extract', backend='pdfplumber')
    return worker(window, context) if _worker_accepts_context(worker) else worker(window)


def _window_session_entry(worker, connection):
    """Run a reusable native-worker session in one killable child process."""
    from .pdf_worker import _exception_code, _local

    _local.sender = connection
    _local.phase = "open"
    session = None
    try:
        session_factory = getattr(worker, "open_session", None)
        if not callable(session_factory):
            raise TypeError("worker does not provide an isolated session factory")
        session = session_factory()
        connection.send(("ready", {
            "pid": os.getpid(),
            "session_diagnostics": dict(getattr(session, "diagnostics", {}) or {}),
        }))
        while True:
            try:
                message = connection.recv()
            except (EOFError, OSError):
                break
            if not isinstance(message, tuple) or not message:
                connection.send(("error", {"code": "PDFIUM_WORKER_PROTOCOL_ERROR"}))
                continue
            command = message[0]
            if command == "close":
                break
            if command != "run" or len(message) != 3:
                connection.send(("error", {"code": "PDFIUM_WORKER_PROTOCOL_ERROR"}))
                continue
            _local.phase = "extract"
            try:
                result = session(message[1], dict(message[2] or {}))
                connection.send(("session_diagnostics", dict(getattr(session, "diagnostics", {}) or {})))
                connection.send(("result", result))
            except BaseException as exc:
                detail = {"code": _exception_code(exc), "exception_type": type(exc).__name__}
                errno = getattr(exc, "errno", None)
                if errno is not None:
                    detail["errno"] = errno
                connection.send(("error", detail))
    except BaseException as exc:
        try:
            connection.send(("init_error", {
                "code": _exception_code(exc), "exception_type": type(exc).__name__,
                "errno": getattr(exc, "errno", None),
            }))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        try:
            connection.close()
        except OSError:
            pass
        _local.sender = None


class PdfWorkerSession:
    """Keep one isolated worker alive across the windows of one PDF.

    Each request remains individually timed and cancellable. A timed-out
    request destroys the process; the next request starts a clean session,
    while already committed checkpoints remain untouched.
    """

    _financial_window_session = True

    def __init__(self, worker, *, startup_timeout_seconds: float = 45.0):
        self._worker = worker
        self._startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self._process = None
        self._connection = None
        self.process_start_count = 0
        self.started_worker_pids = set()
        self._worker_pid = None
        self._worker_diagnostics: dict[str, Any] = {}
        self._last_start_elapsed_seconds = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _stop(self, *, graceful: bool = False):
        process, connection = self._process, self._connection
        self._process = None
        self._connection = None
        if graceful and process is not None and process.is_alive() and connection is not None:
            try:
                connection.send(("close",))
            except (BrokenPipeError, EOFError, OSError):
                pass
            process.join(timeout=2.0)
        if process is not None:
            try:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1.0)
            except (AssertionError, OSError, ValueError, TypeError):
                pass
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def close(self):
        self._stop(graceful=True)

    def _start(self, *, cancel_check=None):
        if self._process is not None and self._process.is_alive():
            return
        self._stop()
        start_started = time.monotonic()
        try:
            context = mp.get_context("spawn")
            parent, child = context.Pipe(duplex=True)
            process = context.Process(
                target=_window_session_entry,
                args=(self._worker, child),
                name="pdf-document-session",
                daemon=True,
            )
            process.start()
            child.close()
            self._process, self._connection = process, parent
            self.process_start_count += 1
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            raise WorkerIsolationUnavailable() from exc

        deadline = time.monotonic() + self._startup_timeout_seconds
        while True:
            if cancel_check is not None and cancel_check():
                self._stop()
                raise CancelledError("PDF session startup cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop()
                raise WorkerIsolationUnavailable()
            if self._connection.poll(min(0.05, remaining)):
                try:
                    kind, value = self._connection.recv()
                except (EOFError, OSError, ValueError, TypeError) as exc:
                    self._stop()
                    raise WorkerIsolationUnavailable() from exc
                if kind == "phase":
                    continue
                if kind == "ready":
                    self._worker_pid = value.get("pid") if isinstance(value, dict) else None
                    if self._worker_pid is not None:
                        self.started_worker_pids.add(self._worker_pid)
                    self._worker_diagnostics = (
                        dict(value.get("session_diagnostics") or {})
                        if isinstance(value, dict) and isinstance(value.get("session_diagnostics"), dict)
                        else {}
                    )
                    self._last_start_elapsed_seconds = round(time.monotonic() - start_started, 4)
                    return
                if kind == "init_error":
                    self._stop()
                    from .pdf_worker import PdfWorkerFailure
                    diagnostic = {"operation": "document_session_open", "phase": "open",
                        "ready_received": False, "artifact_received": False,
                        "raw_error_code": value.get("code", "PDFIUM_WORKER_ERROR"),
                        "exception_type": value.get("exception_type", "")}
                    raise PdfWorkerFailure(diagnostic["raw_error_code"], diagnostic)
            elif not self._process.is_alive():
                self._stop()
                raise WorkerIsolationUnavailable()

    @staticmethod
    def _raise_worker_error(value, diagnostic):
        code = str(value.get("code") or "PDFIUM_WORKER_ERROR")
        from .pdf_worker import PdfWorkerFailure
        if code == "PDFIUM_WORKER_START_FAILED":
            raise WorkerIsolationUnavailable()
        if code == "PDFIUM_WORKER_TIMEOUT":
            error = TimeoutError("PDF_TABLE_WINDOW_TIMEOUT")
            error.code = code
            error.diagnostic = diagnostic
            raise error
        if code == "PDFIUM_WORKER_CANCELLED":
            error = CancelledError("PDF_TABLE_WINDOW_CANCELLED")
            error.code = code
            error.diagnostic = diagnostic
            raise error
        raise PdfWorkerFailure(code, diagnostic)

    def invoke_window(self, window, timeout_seconds, context, cancel_check=None):
        if timeout_seconds is None:
            timeout_seconds = self._startup_timeout_seconds
        self._start(cancel_check=cancel_check)
        started = time.monotonic()
        deadline = started + max(0.05, float(timeout_seconds))
        diagnostic = {"operation": "table_window", "phase": "ready", "backend": "pdfplumber",
            "raw_error_code": "", "exception_type": "", "exit_code": None,
            "ready_received": True, "artifact_received": False,
            "page_range": list(window.pages), "worker_session_pid": self._worker_pid,
            "worker_session_start_count": self.process_start_count,
            "worker_session_open_elapsed_seconds": self._last_start_elapsed_seconds,
            **self._worker_diagnostics}
        try:
            self._connection.send(("run", window, dict(context or {})))
            while True:
                if cancel_check is not None and cancel_check():
                    diagnostic.update(raw_error_code="PDFIUM_WORKER_CANCELLED")
                    self._stop()
                    self._raise_worker_error({"code": "PDFIUM_WORKER_CANCELLED"}, diagnostic)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    diagnostic.update(raw_error_code="PDFIUM_WORKER_TIMEOUT")
                    self._stop()
                    self._raise_worker_error({"code": "PDFIUM_WORKER_TIMEOUT"}, diagnostic)
                if not self._connection.poll(min(0.05, remaining)):
                    if self._process is not None and not self._process.is_alive():
                        diagnostic.update(raw_error_code="PDFIUM_WORKER_EXIT",
                            exit_code=getattr(self._process, "exitcode", None))
                        self._stop()
                        self._raise_worker_error({"code": "PDFIUM_WORKER_EXIT"}, diagnostic)
                    continue
                try:
                    kind, value = self._connection.recv()
                except (EOFError, OSError, ValueError, TypeError):
                    diagnostic.update(raw_error_code="PDFIUM_WORKER_EXIT")
                    self._stop()
                    self._raise_worker_error({"code": "PDFIUM_WORKER_EXIT"}, diagnostic)
                if kind == "phase":
                    if isinstance(value, dict):
                        diagnostic.update({key: item for key, item in value.items() if item})
                    continue
                if kind == "session_diagnostics" and isinstance(value, dict):
                    self._worker_diagnostics = dict(value)
                    diagnostic.update(value)
                    continue
                if kind == "error":
                    diagnostic.update(raw_error_code=value.get("code", "PDFIUM_WORKER_ERROR"),
                        exception_type=value.get("exception_type", ""))
                    self._raise_worker_error(value, diagnostic)
                if kind != "result":
                    diagnostic.update(raw_error_code="PDFIUM_WORKER_PROTOCOL_ERROR")
                    self._raise_worker_error({"code": "PDFIUM_WORKER_PROTOCOL_ERROR"}, diagnostic)
                diagnostic.update(artifact_received=True, elapsed_seconds=round(time.monotonic() - started, 4))
                if isinstance(value, tuple) and len(value) == 3 and isinstance(value[2], dict):
                    value = (value[0], value[1], {**value[2], "pdf_worker_diagnostic": diagnostic})
                return value
        finally:
            diagnostic.setdefault("elapsed_seconds", round(time.monotonic() - started, 4))


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

    if getattr(worker, "_financial_window_session", False):
        return worker.invoke_window(window, timeout_seconds, context, cancel_check)

    def run_synchronously() -> Any:
        worker_context = dict(context or {})
        return worker(window, worker_context) if _worker_accepts_context(worker) else worker(window)

    if timeout_seconds is None:
        return run_synchronously()
    try:
        pickle.dumps(worker)
    except (pickle.PickleError, TypeError, AttributeError):
        return run_synchronously()
    from .pdf_worker import run_pdf_worker, PdfWorkerFailure
    diagnostics = []
    value, error = run_pdf_worker(_checkpoint_operation, worker, window, dict(context or {}),
        timeout_seconds=timeout_seconds, cancel_check=cancel_check, diagnostic_callback=diagnostics.append,
        operation='table_window', backend='pdfplumber', page_range=list(window.pages))
    if error:
        # Preserve the public exception contracts without discarding the IPC
        # cause. A real hung table window is not a scheduler slice that should
        # be silently retried until a generic no-progress guard fires.
        if error == 'PDFIUM_WORKER_START_FAILED':
            failure = WorkerIsolationUnavailable()
        elif error == 'PDFIUM_WORKER_TIMEOUT':
            failure = TimeoutError('PDF_TABLE_WINDOW_TIMEOUT')
        elif error == 'PDFIUM_WORKER_CANCELLED':
            failure = CancelledError('PDF_TABLE_WINDOW_CANCELLED')
        else:
            raise PdfWorkerFailure(error, diagnostics[-1])
        failure.code = error
        failure.diagnostic = dict(diagnostics[-1])
        raise failure
    if isinstance(value, tuple) and len(value) == 3 and isinstance(value[2], dict):
        value = (value[0], value[1], {**value[2], 'pdf_worker_diagnostic': diagnostics[-1]})
    return value


class PositionedPageStore:
    """Persist source-bound positioned words without parser interpretation state."""

    EXTRACTION_CONTRACT_VERSION = "positioned-words-v1"
    EXTRACT_WORDS_OPTIONS = {"keep_blank_chars": False, "use_text_flow": False}

    def __init__(self, directory: Path, source_hash: str, backend_version: str):
        if not isinstance(source_hash, str) or not source_hash:
            raise ValueError("POSITIONED_PAGE_CACHE_SOURCE_INVALID")
        if not isinstance(backend_version, str) or not backend_version:
            raise ValueError("POSITIONED_PAGE_CACHE_BACKEND_INVALID")
        self.identity = {
            "source_hash": source_hash,
            "backend_version": backend_version,
            "extraction_contract_version": self.EXTRACTION_CONTRACT_VERSION,
            "extract_words_options": dict(self.EXTRACT_WORDS_OPTIONS),
        }
        self.identity_hash = hashlib.sha256(self._canonical_json(self.identity)).hexdigest()
        self.cache_directory = Path(directory) / "positioned-pages" / self.identity_hash[:32]

    @staticmethod
    def _canonical_json(value: Any) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _json_value_is_valid(value: Any) -> bool:
        import math

        if value is None or type(value) in (str, bool, int):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, list):
            return all(PositionedPageStore._json_value_is_valid(item) for item in value)
        if isinstance(value, dict):
            return all(
                isinstance(key, str) and PositionedPageStore._json_value_is_valid(item)
                for key, item in value.items()
            )
        return False

    @classmethod
    def _words_are_valid(cls, words: Any) -> bool:
        import math

        if not isinstance(words, list):
            return False
        for word in words:
            if not isinstance(word, dict) or not cls._json_value_is_valid(word):
                return False
            for key in ("text", "x0", "x1", "top", "bottom"):
                if key not in word:
                    return False
            if not isinstance(word["text"], str):
                return False
            for key in ("x0", "x1", "top", "bottom"):
                value = word[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return False
                try:
                    if not math.isfinite(float(value)):
                        return False
                except (OverflowError, TypeError, ValueError):
                    return False
        return True

    @staticmethod
    def _valid_page(page: Any) -> bool:
        return type(page) is int and page >= 1

    def _page_path(self, page: int) -> Path:
        return self.cache_directory / f"{page}.json"

    def load(self, page: int) -> dict[str, Any] | None:
        if not self._valid_page(page):
            return None
        try:
            envelope = json.loads(self._page_path(page).read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "checksum"}:
                return None
            payload = envelope["payload"]
            checksum = envelope["checksum"]
            if not isinstance(payload, dict) or not isinstance(checksum, str):
                return None
            payload_bytes = self._canonical_json(payload)
            if hashlib.sha256(payload_bytes).hexdigest() != checksum:
                return None
            if payload.get("identity") != self.identity or payload.get("page") != page:
                return None
            if not self._valid_page(payload.get("page")):
                return None
            if not isinstance(payload.get("text"), str) or not self._words_are_valid(payload.get("words")):
                return None
            return {"page": page, "words": payload["words"], "text": payload["text"]}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def save(self, page: int, words: list[dict], text: str) -> None:
        if not self._valid_page(page):
            raise ValueError("POSITIONED_PAGE_CACHE_PAGE_INVALID")
        if not self._words_are_valid(words) or not isinstance(text, str):
            raise ValueError("POSITIONED_PAGE_CACHE_CONTENT_INVALID")
        payload = {"identity": self.identity, "page": page, "words": words, "text": text}
        payload_bytes = self._canonical_json(payload)
        envelope = {
            "payload": payload,
            "checksum": hashlib.sha256(payload_bytes).hexdigest(),
        }
        serialized = self._canonical_json(envelope)
        destination = self._page_path(page)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{page}.", suffix=".tmp", dir=destination.parent, delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
