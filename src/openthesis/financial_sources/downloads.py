"""Bounded official filing downloads and cache identity checks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass
import hashlib
import random
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from ..domain import FilingDocument
from ..market_data import MarketDataError
from ..sec_client import SecClientError
from ..resilience.remote import current_remote_context


_LEGACY_PERIOD_REVISIONS = frozenset({
    "period_end_provisional",
    "period_end_verified",
})


@dataclass(frozen=True, slots=True)
class FilingIdentityAssessment:
    """Separate byte identity from the metadata used to interpret a filing."""

    same_original: bool
    metadata_compatible: bool
    period_state: str
    diagnostic_code: str
    resolved_period_end: str
    actual_revision: str


def bounded_download_filings(
    adapter: Any,
    filings: list[FilingDocument],
    target: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    max_workers: int = 3,
    per_host: int = 2,
    retry_delay: Callable[[], None] | None = None,
) -> tuple[list[FilingDocument], list[tuple[FilingDocument, Exception]]]:
    """Download unique filings with bounded host/global concurrency.

    Futures may finish in any order, but results and failures are assembled in
    first-input order.  Only transient source exceptions get one immediate
    retry; model providers are never involved here.
    """
    unique: list[FilingDocument] = []
    seen_urls: set[str] = set()
    for filing in filings:
        url = str(filing.source_url).strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique.append(filing)
    if not unique:
        return [], []

    cancel_event = cancel_event or threading.Event()
    host_locks: dict[str, threading.BoundedSemaphore] = {}
    host_lock_guard = threading.Lock()

    def semaphore_for(filing: FilingDocument) -> threading.BoundedSemaphore:
        parsed = urllib.parse.urlparse(filing.source_url)
        host = (parsed.hostname or parsed.netloc or "unknown").casefold()
        with host_lock_guard:
            return host_locks.setdefault(host, threading.BoundedSemaphore(max(1, per_host)))

    def download_one(index: int, filing: FilingDocument) -> tuple[int, FilingDocument | None, Exception | None]:
        if cancel_event.is_set():
            return index, None, None
        gate = semaphore_for(filing)
        with gate:
            if cancel_event.is_set():
                return index, None, None
            attempts = 0
            while True:
                try:
                    return index, adapter.download_filing(filing, target), None
                except (MarketDataError, SecClientError, OSError) as exc:
                    if current_remote_context() is not None or attempts >= 1 or not download_error_is_transient(exc):
                        return index, None, exc
                    if cancel_event.is_set():
                        return index, None, None
                    attempts += 1
                    if retry_delay is not None:
                        retry_delay()
                    else:
                        time.sleep(random.uniform(0.05, 0.15) * (2 ** (attempts - 1)))
                except Exception as exc:
                    return index, None, exc

    successes: dict[int, FilingDocument] = {}
    failures: dict[int, tuple[FilingDocument, Exception]] = {}
    completed = 0
    worker_count = max(1, min(max_workers, 3, len(unique)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="filing-download") as executor:
        futures = [executor.submit(copy_context().run, download_one, index, filing)
                   for index, filing in enumerate(unique)]
        for future in as_completed(futures):
            index, downloaded, error = future.result()
            completed += 1
            if downloaded is not None:
                successes[index] = downloaded
            elif error is not None:
                failures[index] = (unique[index], error)
            if progress is not None:
                progress(completed, len(unique))

    return (
        [successes[index] for index in range(len(unique)) if index in successes],
        [failures[index] for index in range(len(unique)) if index in failures],
    )


def _filing_field(filing: FilingDocument, name: str) -> Any:
    if isinstance(filing, dict):
        return filing.get(name)
    return getattr(filing, name, None)


def _actual_revision(filing: FilingDocument) -> str:
    revision = str(_filing_field(filing, "revision") or "original").strip().casefold()
    return "original" if not revision or revision in _LEGACY_PERIOD_REVISIONS else revision


def _explicit_period_end(filing: FilingDocument) -> str:
    revision = str(_filing_field(filing, "revision") or "original").strip().casefold()
    period_end = str(_filing_field(filing, "period_end") or "").strip()
    # These legacy values were used to carry date certainty, not a new PDF
    # revision.  A provisional date must not become evidence merely because it
    # was persisted in the old column.
    return "" if revision == "period_end_provisional" or _filing_field(filing, "period_metadata_status") == "unresolved" else period_end


def _normalise_official_url(value: Any) -> str | None:
    """Canonicalise only URL syntax that cannot change the source resource."""

    raw = str(value or "").strip()
    if not raw or any(ord(char) <= 32 for char in raw):
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname
        if not host:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if port is not None and port == (443 if scheme == "https" else 80):
        port = None
    host = host.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    # Path, query, and fragment are deliberately preserved byte-for-byte in
    # their parsed string form: case and trailing slash can identify a
    # different official object, and query parameters can select a revision.
    return urllib.parse.urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def assess_filing_identity(
    discovered: FilingDocument, stored: FilingDocument
) -> FilingIdentityAssessment:
    """Assess original-byte identity separately from disclosure metadata.

    A metadata conflict never turns identical bytes into a different original,
    but it does prevent cache reuse through ``filing_identity_matches``.  Empty
    period metadata can be filled by an explicit counterpart; provisional
    legacy dates cannot.
    """

    source_discovered = _normalise_official_url(_filing_field(discovered, "source_url"))
    source_stored = _normalise_official_url(_filing_field(stored, "source_url"))
    source_valid = source_discovered is not None and source_stored is not None

    identity_pairs = (
        ("company_cik", _filing_field(discovered, "company_cik"), _filing_field(stored, "company_cik")),
        ("document_id", _filing_field(discovered, "document_id"), _filing_field(stored, "document_id")),
        ("accession_number", _filing_field(discovered, "accession_number"), _filing_field(stored, "accession_number")),
    )
    identity_equal = all(
        str(left or "").strip()
        and str(left or "").strip() == str(right or "").strip()
        for _, left, right in identity_pairs
    )
    identity_equal = identity_equal and source_valid and source_discovered == source_stored
    revision_discovered = _actual_revision(discovered)
    revision_stored = _actual_revision(stored)
    identity_equal = identity_equal and revision_discovered == revision_stored
    supersedes_discovered = str(_filing_field(discovered, "supersedes_document_id") or "").strip()
    supersedes_stored = str(_filing_field(stored, "supersedes_document_id") or "").strip()
    if supersedes_discovered or supersedes_stored:
        identity_equal = identity_equal and supersedes_discovered == supersedes_stored

    hash_discovered = str(_filing_field(discovered, "content_hash") or "").strip().casefold()
    hash_stored = str(_filing_field(stored, "content_hash") or "").strip().casefold()
    # Discovery may legitimately be metadata-only while the verified cache
    # carries the byte hash.  Compare hashes only when both sides provide one;
    # local_cache_diagnostic remains the gate that verifies cached bytes.
    hash_mismatch = bool(hash_discovered and hash_stored) and hash_discovered != hash_stored
    identity_equal = identity_equal and not hash_mismatch

    discovered_end = _explicit_period_end(discovered)
    stored_end = _explicit_period_end(stored)
    metadata_conflict = False
    for field_name in ("form_type", "fiscal_period"):
        discovered_value = str(_filing_field(discovered, field_name) or "").strip().casefold()
        stored_value = str(_filing_field(stored, field_name) or "").strip().casefold()
        if discovered_value and stored_value and discovered_value != stored_value:
            metadata_conflict = True
    if discovered_end and stored_end and discovered_end != stored_end:
        metadata_conflict = True

    if metadata_conflict:
        period_state = "conflict"
        resolved_period_end = ""
    elif stored_end and discovered_end:
        period_state = "source_confirmed"
        resolved_period_end = stored_end
    elif stored_end:
        period_state = "source_confirmed"
        resolved_period_end = stored_end
    elif discovered_end:
        period_state = "discovered"
        resolved_period_end = discovered_end
    else:
        period_state = "unresolved"
        resolved_period_end = ""

    same_original = identity_equal
    if not source_valid:
        diagnostic_code = "FILING_SOURCE_URL_INVALID"
    elif hash_mismatch:
        diagnostic_code = "FILING_CONTENT_HASH_MISMATCH"
    elif not same_original:
        diagnostic_code = "FILING_IDENTITY_MISMATCH"
    elif metadata_conflict:
        diagnostic_code = "FILING_PERIOD_METADATA_CONFLICT"
    else:
        diagnostic_code = ""
    return FilingIdentityAssessment(
        same_original=same_original,
        metadata_compatible=not metadata_conflict,
        period_state=period_state,
        diagnostic_code=diagnostic_code,
        resolved_period_end=resolved_period_end,
        actual_revision=revision_discovered,
    )


def local_cache_diagnostic(filing: FilingDocument) -> str:
    """Return a stable cache diagnostic without exposing local I/O details."""

    local_path = _filing_field(filing, "local_path")
    if not local_path:
        return "FILING_LOCAL_FILE_MISSING"
    expected_hash = str(_filing_field(filing, "content_hash") or "").strip().casefold()
    if not expected_hash:
        return "FILING_CONTENT_HASH_MISSING"
    path = Path(str(local_path))
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return "FILING_LOCAL_FILE_MISSING"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "OFFICIAL_LOCAL_IO_UNAVAILABLE"
    return "" if digest.hexdigest().casefold() == expected_hash else "FILING_CONTENT_HASH_MISMATCH"


def filing_cache_valid(filing: FilingDocument) -> bool:
    """Verify a local filing object before allowing cache reuse."""

    return local_cache_diagnostic(filing) == ""


def filing_identity_matches(discovered: FilingDocument, stored: FilingDocument) -> bool:
    """Compatibility bool for callers that only need a cache-match answer."""

    assessment = assess_filing_identity(discovered, stored)
    return assessment.same_original and assessment.metadata_compatible


def download_error_is_transient(error: Exception) -> bool:
    if isinstance(error, MarketDataError):
        return str(getattr(error, "code", "")).upper() in {
            "FILING_FETCH_FAILED",
            "FILING_DOWNLOAD_FAILED",
            "FILING_TIMEOUT",
            "RATE_LIMITED",
        }
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, SecClientError):
        message = str(error).casefold()
        return any(token in message for token in ("timeout", "temporar", "429", "rate limit", " 500", " 502", " 503", " 504"))
    if isinstance(error, OSError):
        return getattr(error, "errno", None) in {54, 60, 10053, 10054, 10060, 110, 111}
    return False
