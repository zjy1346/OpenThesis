"""Bounded, immutable storage for official disclosure payloads.

The downloader deliberately never replaces an existing disclosure object.  A
content digest is part of the stored filename, so a corrected filing becomes a
new auditable object while an identical retry reuses the existing bytes.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import BinaryIO


class UnsafeDisclosurePayload(ValueError):
    """The downloaded bytes are not safe to enter the filing cache."""


def store_immutable_payload(
    target: Path,
    payload: bytes,
    *,
    maximum_bytes: int = 100_000_000,
    require_pdf: bool = False,
) -> Path:
    """Atomically create a digest-addressed object without overwriting history.

    ``target`` remains the compatibility basename (usually accession + suffix),
    while the returned path always includes the payload digest.  The temporary
    file is created beside the destination and is removed on every failure.
    """

    if not isinstance(payload, bytes) or not payload:
        raise UnsafeDisclosurePayload("empty disclosure payload")
    return store_immutable_stream(
        target,
        BytesIO(payload),
        maximum_bytes=maximum_bytes,
        require_pdf=require_pdf,
    )


def store_immutable_stream(
    target: Path,
    source: BinaryIO,
    *,
    maximum_bytes: int = 150_000_000,
    require_pdf: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    chunk_size: int = 1024 * 1024,
) -> Path:
    """Stream an official disclosure into immutable storage with bounded RAM."""

    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower() or ".bin"
    stem = target.stem or "disclosure"
    limit = max(1, int(maximum_bytes))
    block_size = max(4096, min(4 * 1024 * 1024, int(chunk_size)))
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{stem}-", suffix=".tmp",
            dir=target.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            digest_state = hashlib.sha256()
            total = 0
            prefix = b""
            tail = b""
            while True:
                if cancel_check is not None and cancel_check():
                    raise InterruptedError("disclosure download cancelled")
                chunk = source.read(block_size)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise UnsafeDisclosurePayload("disclosure stream returned non-bytes data")
                total += len(chunk)
                if total > limit:
                    raise UnsafeDisclosurePayload("disclosure payload exceeds the size limit")
                if len(prefix) < 5:
                    prefix = (prefix + chunk)[:5]
                tail = (tail + chunk)[-8192:]
                digest_state.update(chunk)
                handle.write(chunk)
            if total == 0:
                raise UnsafeDisclosurePayload("empty disclosure payload")
            if require_pdf and (prefix != b"%PDF-" or b"%%EOF" not in tail):
                raise UnsafeDisclosurePayload("downloaded content is not a complete PDF")
            handle.flush()
            os.fsync(handle.fileno())
        digest = digest_state.hexdigest()
        destination = target.with_name(f"{stem}-{digest[:16]}{suffix}")
        if destination.is_file():
            existing_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if existing_digest == digest:
                return destination
            raise UnsafeDisclosurePayload("content-addressed object is inconsistent")
        try:
            # os.rename does not replace an existing destination on Windows;
            # this is the desired no-overwrite behavior on all supported hosts.
            os.rename(temporary, destination)
            temporary = None
        except FileExistsError:
            if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
                return destination
            raise UnsafeDisclosurePayload("content-addressed object is inconsistent")
        return destination
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Never remove a historical object while cleaning a failed
                # attempt; the next retry can safely ignore the orphan.
                pass
