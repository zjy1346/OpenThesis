from __future__ import annotations

import hashlib
import importlib.metadata
import json

import pytest

from openthesis.financial_checkpoint import PositionedPageStore


def _words():
    return [{
        "text": "Revenue",
        "x0": 12.5,
        "x1": 61.75,
        "top": 103,
        "bottom": 114.25,
        "doctop": 815.5,
        "fontname": "Example-Regular",
        "size": 9.0,
        "upright": True,
        "extra": {"source": "positioned-word", "flags": [1, "kept"]},
    }]


def _store(tmp_path, source_hash="a" * 64, backend_version="pdfplumber-backend-v1"):
    return PositionedPageStore(tmp_path, source_hash, backend_version)


def _write_envelope(path, payload, *, checksum=None):
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=True,
    ).encode("utf-8")
    envelope = {
        "payload": payload,
        "checksum": checksum or hashlib.sha256(payload_bytes).hexdigest(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")


def test_positioned_page_round_trip_preserves_text_and_all_word_fields_atomically(tmp_path):
    store = _store(tmp_path)
    words = _words()
    text = "Revenue 2025\n"
    legacy_path = tmp_path / "legacy-window.json"
    legacy_path.write_text("keep unchanged", encoding="utf-8")

    store.save(4, words, text)

    page_path = store.cache_directory / "4.json"
    assert page_path.is_file()
    assert page_path == tmp_path / "positioned-pages" / f"{store.identity_hash[:32]}" / "4.json"
    assert store.load(4) == {"page": 4, "words": words, "text": text}
    assert legacy_path.read_text(encoding="utf-8") == "keep unchanged"
    assert list(store.cache_directory.glob("*.tmp")) == []


def test_positioned_page_identity_invalidates_changed_source_or_backend_version(tmp_path):
    original = _store(tmp_path)
    original.save(1, _words(), "page text")

    changed_source = _store(tmp_path, source_hash="b" * 64)
    changed_backend = _store(tmp_path, backend_version="pdfplumber-backend-v2")

    assert changed_source.load(1) is None
    assert changed_backend.load(1) is None
    assert original.load(1) == {"page": 1, "words": _words(), "text": "page text"}


def test_positioned_page_uses_explicit_backend_version_without_distribution_metadata(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda *_args, **_kwargs: pytest.fail("cache identity must use its explicit backend version"),
    )

    store = _store(tmp_path, backend_version="pdfplumber-0.11.9")
    store.save(1, _words(), "page text")

    assert store.load(1) == {"page": 1, "words": _words(), "text": "page text"}
    assert store.identity["backend_version"] == "pdfplumber-0.11.9"
    assert "pdfplumber_version" not in store.identity


@pytest.mark.parametrize(
    "corruption",
    [
        "json", "checksum", "nan-coordinate", "infinite-coordinate",
        "invalid-coordinate-type", "non-string-word-text", "non-string-page-text", "wrong-page",
    ],
)
def test_positioned_page_corruption_is_a_cache_miss(tmp_path, corruption):
    store = _store(tmp_path)
    store.save(2, _words(), "page text")
    source_path = store.cache_directory / "2.json"

    if corruption == "json":
        source_path.write_text("{not json", encoding="utf-8")
    elif corruption == "checksum":
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        envelope["checksum"] = "0" * 64
        source_path.write_text(json.dumps(envelope), encoding="utf-8")
    elif corruption == "nan-coordinate":
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        envelope["payload"]["words"][0]["x0"] = float("nan")
        _write_envelope(source_path, envelope["payload"])
    elif corruption == "infinite-coordinate":
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        envelope["payload"]["words"][0]["bottom"] = float("inf")
        _write_envelope(source_path, envelope["payload"])
    elif corruption == "invalid-coordinate-type":
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        envelope["payload"]["words"][0]["x1"] = "61.75"
        _write_envelope(source_path, envelope["payload"])
    elif corruption == "non-string-word-text":
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        envelope["payload"]["words"][0]["text"] = 123
        _write_envelope(source_path, envelope["payload"])
    elif corruption == "non-string-page-text":
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        envelope["payload"]["text"] = None
        _write_envelope(source_path, envelope["payload"])
    else:
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        wrong_page = dict(envelope["payload"], page=1)
        _write_envelope(store.cache_directory / "3.json", wrong_page)
        assert store.load(3) is None

    if corruption != "wrong-page":
        assert store.load(2) is None


def test_positioned_page_reuse_is_independent_of_financial_interpretation_rules(tmp_path):
    words = _words()
    original = _store(tmp_path)
    original.save(7, words, "same extracted text")

    reopened = _store(tmp_path)

    assert reopened.identity == original.identity
    assert "financial_rules_version" not in reopened.identity
    assert "unit_version" not in reopened.identity
    assert "scope_version" not in reopened.identity
    assert reopened.load(7) == {"page": 7, "words": words, "text": "same extracted text"}


@pytest.mark.parametrize(
    "words,text",
    [
        pytest.param([{**_words()[0], "x0": float("inf")}], "page", id="infinite-coordinate"),
        pytest.param([{**_words()[0], "top": float("nan")}], "page", id="nan-coordinate"),
        pytest.param([{**_words()[0], "bottom": True}], "page", id="boolean-coordinate"),
        pytest.param(_words(), None, id="non-string-text"),
    ],
)
def test_positioned_page_save_rejects_non_json_or_invalid_geometry(tmp_path, words, text):
    with pytest.raises(ValueError, match="POSITIONED_PAGE_CACHE_CONTENT_INVALID"):
        _store(tmp_path).save(1, words, text)
