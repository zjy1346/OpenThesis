"""Shared immutable page-text index for disclosure parsers.

Financial coordinate extraction and qualitative evidence routing need the same
PDF text layer.  Keeping it behind this small seam avoids reopening hundreds
of pages after the financial prepass while a stat-based identity prevents a
replaced file from reusing stale text.
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Sequence


_MAX_DOCUMENTS = 24
_MAX_TEXT_CHARACTERS = 16_000_000
_LOCK = RLock()
_TEXT_READER_VERSION = "native-pdf-text-v2"
_PAGE_TEXT: "OrderedDict[tuple, tuple[str, ...]]" = OrderedDict()
_PAGE_TEXT_CHARACTERS = 0


def _identity(path: str | Path) -> tuple | None:
    target = Path(path)
    try:
        stat = target.stat()
        digest = sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return str(target.resolve()), int(stat.st_size), int(stat.st_mtime_ns), digest.hexdigest(), _TEXT_READER_VERSION
    except OSError:
        return None


def remember_page_texts(path: str | Path, pages: Sequence[str]) -> tuple[str, ...]:
    global _PAGE_TEXT_CHARACTERS
    identity = _identity(path)
    value = tuple(str(item or "") for item in pages)
    if identity is None:
        return value
    character_count = sum(len(item) for item in value)
    with _LOCK:
        previous = _PAGE_TEXT.pop(identity, ())
        _PAGE_TEXT_CHARACTERS -= sum(len(item) for item in previous)
        if character_count > _MAX_TEXT_CHARACTERS:
            return value
        _PAGE_TEXT[identity] = value
        _PAGE_TEXT_CHARACTERS += character_count
        while (
            len(_PAGE_TEXT) > _MAX_DOCUMENTS
            or _PAGE_TEXT_CHARACTERS > _MAX_TEXT_CHARACTERS
        ):
            _old_identity, removed = _PAGE_TEXT.popitem(last=False)
            _PAGE_TEXT_CHARACTERS -= sum(len(item) for item in removed)
    return value


def cached_page_texts(path: str | Path) -> tuple[str, ...] | None:
    identity = _identity(path)
    if identity is None:
        return None
    with _LOCK:
        value = _PAGE_TEXT.get(identity)
        if value is not None:
            _PAGE_TEXT.move_to_end(identity)
        return value
