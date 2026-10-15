"""Immutable, bounded SQLite chunks for lossless candidate documents.

This adapter stores bytes, not trust decisions. The original security guard and
full-content SHA remain mandatory on write and read; callers own transactions.
"""
from __future__ import annotations

import hashlib
import json

from .contracts import MAX_CANDIDATE_JSON_BYTES, candidate_document_json, candidate_json, digest


SCHEMA = "openthesis.candidate-chunks.v1"
MAX_BYTES = 64 * 1024 * 1024
CHUNK_CHARS = 64 * 1024
SCHEMA_STATEMENT = """CREATE TABLE IF NOT EXISTS candidate_payload_chunks (
    payload_digest TEXT NOT NULL, ordinal INTEGER NOT NULL, content TEXT NOT NULL,
    content_digest TEXT NOT NULL, PRIMARY KEY(payload_digest,ordinal))"""


def strict_loads(encoded):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("RECOVERY_PAYLOAD_DUPLICATE_KEY")
            value[key] = item
        return value

    def nonfinite(_value):
        raise ValueError("RECOVERY_PAYLOAD_NONFINITE")

    return json.loads(encoded, object_pairs_hook=pairs, parse_constant=nonfinite)


def store_payload(db, value):
    encoded = candidate_document_json(value, max_bytes=MAX_BYTES)
    if len(encoded.encode("utf-8")) <= MAX_CANDIDATE_JSON_BYTES:
        if isinstance(value, dict) and value.get("schema") == SCHEMA:
            raise ValueError("RECOVERY_PAYLOAD_RESERVED_SCHEMA")
        return encoded
    payload_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    chunks = [encoded[offset:offset + CHUNK_CHARS] for offset in range(0, len(encoded), CHUNK_CHARS)]
    for index, content in enumerate(chunks):
        existing = db.execute("SELECT content,content_digest FROM candidate_payload_chunks WHERE payload_digest=? AND ordinal=?",
                              (payload_digest, index)).fetchone()
        if existing and (existing["content"] != content or existing["content_digest"] != digest(content)):
            raise ValueError("RECOVERY_PAYLOAD_INTEGRITY_INVALID")
        db.execute("INSERT OR IGNORE INTO candidate_payload_chunks VALUES(?,?,?,?)",
                   (payload_digest, index, content, digest(content)))
    return candidate_json({"schema": SCHEMA, "payload_digest": payload_digest,
                           "count": len(chunks), "bytes": len(encoded.encode("utf-8"))})


def load_payload(db, encoded):
    value = strict_loads(encoded)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return value
    fingerprint = value.get("payload_digest")
    if (set(value) != {"schema", "payload_digest", "count", "bytes"}
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
            or type(value.get("count")) is not int or not 1 <= value["count"] <= MAX_BYTES // CHUNK_CHARS
            or type(value.get("bytes")) is not int or not MAX_CANDIDATE_JSON_BYTES < value["bytes"] <= MAX_BYTES):
        raise ValueError("RECOVERY_PAYLOAD_MANIFEST_INVALID")
    rows = db.execute("SELECT ordinal,content,content_digest FROM candidate_payload_chunks WHERE payload_digest=? ORDER BY ordinal",
                      (fingerprint,)).fetchall()
    if len(rows) != value["count"]:
        raise ValueError("RECOVERY_PAYLOAD_INTEGRITY_INVALID")
    for index, row in enumerate(rows):
        if (row["ordinal"] != index or len(row["content"]) > CHUNK_CHARS
                or digest(row["content"]) != row["content_digest"]):
            raise ValueError("RECOVERY_PAYLOAD_INTEGRITY_INVALID")
    complete = "".join(row["content"] for row in rows)
    if (len(complete.encode("utf-8")) != value["bytes"]
            or hashlib.sha256(complete.encode("utf-8")).hexdigest() != fingerprint):
        raise ValueError("RECOVERY_PAYLOAD_INTEGRITY_INVALID")
    decoded = strict_loads(complete)
    if candidate_document_json(decoded) != complete:
        raise ValueError("RECOVERY_PAYLOAD_INTEGRITY_INVALID")
    return decoded
