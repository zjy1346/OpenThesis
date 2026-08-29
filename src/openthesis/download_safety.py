"""Bounded, immutable storage for official disclosure payloads.

The downloader deliberately never replaces an existing disclosure object.  A
content digest is part of the stored filename, so a corrected filing becomes a
new auditable object while an identical retry reuses the existing bytes.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


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
    if len(payload) > max(1, int(maximum_bytes)):
        raise UnsafeDisclosurePayload("disclosure payload exceeds the size limit")
    if require_pdf and (
        not payload.startswith(b"%PDF-")
        or b"%%EOF" not in payload[-8192:]
    ):
        raise UnsafeDisclosurePayload("downloaded content is not a complete PDF")

    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    suffix = target.suffix.lower() or ".bin"
    stem = target.stem or "disclosure"
    destination = target.with_name(f"{stem}-{digest[:16]}{suffix}")
    if destination.is_file():
        # A digest collision is not expected, but do not trust a path alone.
        existing_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if existing_digest == digest:
            return destination
        raise UnsafeDisclosurePayload("content-addressed object is inconsistent")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{stem}-", suffix=".tmp",
            dir=target.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
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
