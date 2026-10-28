"""Integrity and security boundaries for content-addressed candidate chunks."""
from __future__ import annotations

import json
import sqlite3

import pytest

from openthesis.resilience import payloads
from openthesis.resilience.payloads import SCHEMA_STATEMENT, load_payload, store_payload
from openthesis.resilience.contracts import MAX_CANDIDATE_JSON_BYTES, candidate_json


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(SCHEMA_STATEMENT)
    return db


def _large_value() -> dict:
    return {"text": "分块正文必须保持顺序和字节完整；" * 220_000}


def test_store_load_payload_roundtrip_is_lossless_and_manifest_is_bounded() -> None:
    db = _db()
    value = _large_value()
    encoded = store_payload(db, value)

    assert len(encoded.encode("utf-8")) < MAX_CANDIDATE_JSON_BYTES
    manifest = json.loads(encoded)
    assert manifest["schema"] == payloads.SCHEMA
    assert manifest["count"] > 1
    assert load_payload(db, encoded) == value


def test_missing_or_reordered_chunks_fail_closed() -> None:
    db = _db()
    encoded = store_payload(db, _large_value())
    manifest = json.loads(encoded)
    db.execute(
        "DELETE FROM candidate_payload_chunks WHERE payload_digest=? AND ordinal=0",
        (manifest["payload_digest"],),
    )
    with pytest.raises(ValueError, match="RECOVERY_PAYLOAD_INTEGRITY_INVALID"):
        load_payload(db, encoded)

    db = _db()
    encoded = store_payload(db, _large_value())
    manifest = json.loads(encoded)
    rows = db.execute(
        "SELECT ordinal,content,content_digest FROM candidate_payload_chunks "
        "WHERE payload_digest=? ORDER BY ordinal",
        (manifest["payload_digest"],),
    ).fetchall()
    assert len(rows) > 1
    db.execute(
        "DELETE FROM candidate_payload_chunks WHERE payload_digest=?",
        (manifest["payload_digest"],),
    )
    for new_ordinal, row in enumerate(reversed(rows)):
        db.execute(
            "INSERT INTO candidate_payload_chunks VALUES(?,?,?,?)",
            (manifest["payload_digest"], new_ordinal, row["content"], row["content_digest"]),
        )
    with pytest.raises(ValueError, match="RECOVERY_PAYLOAD_INTEGRITY_INVALID"):
        load_payload(db, encoded)


def test_manifest_and_chunk_bytes_tampering_fail_closed() -> None:
    db = _db()
    encoded = store_payload(db, _large_value())
    manifest = json.loads(encoded)
    altered = dict(manifest, bytes=manifest["bytes"] + 1)
    with pytest.raises(ValueError, match="RECOVERY_PAYLOAD_INTEGRITY_INVALID"):
        load_payload(db, json.dumps(altered, ensure_ascii=False, separators=(",", ":")))

    db = _db()
    encoded = store_payload(db, _large_value())
    manifest = json.loads(encoded)
    db.execute(
        "UPDATE candidate_payload_chunks SET content=? "
        "WHERE payload_digest=? AND ordinal=0",
        ("篡改", manifest["payload_digest"]),
    )
    with pytest.raises(ValueError, match="RECOVERY_PAYLOAD_INTEGRITY_INVALID"):
        load_payload(db, encoded)


def test_credentials_are_rejected_and_default_candidate_row_limit_remains_one_mib() -> None:
    db = _db()
    with pytest.raises(ValueError, match="RECOVERY_CANDIDATE_SECRET_FIELD"):
        store_payload(db, {"api_key": "fixture-only-secret", "body": "safe"})
    with pytest.raises(ValueError, match="RECOVERY_CANDIDATE_TOO_LARGE"):
        candidate_json({"text": "x" * (MAX_CANDIDATE_JSON_BYTES + 1)})


def test_payloads_over_configured_maximum_are_rejected_without_huge_allocation(monkeypatch) -> None:
    db = _db()
    monkeypatch.setattr(payloads, "MAX_BYTES", 1_100_000)
    value = {"text": "限额测试；" * 300_000}
    with pytest.raises(ValueError, match="RECOVERY_(?:PAYLOAD|CANDIDATE)_TOO_LARGE"):
        store_payload(db, value)

