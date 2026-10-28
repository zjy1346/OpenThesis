"""Generated release identity is a consumed input, not the dev placeholder."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "release_snapshot", Path(__file__).resolve().parents[1] / "scripts/generate-release-snapshot.py",
)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def _identity():
    value = {
        "version": "3.0.1", "commit": "a" * 40,
        "build_time_utc": "2026-10-08T06:30:00Z", "contract_version": "2.0",
    }
    value["build_id"] = hashlib.sha256("|".join(value.values()).encode()).hexdigest()
    return value


def test_materialized_build_identity_accounts_for_both_compilation_inputs(tmp_path):
    source = tmp_path / "materialized.json"
    payload = json.dumps(_identity()).encode()
    source.write_bytes(payload)
    rows = snapshot.generated_build_info_entries(source, version="3.0.1", base_commit="a" * 40)
    assert [row["path"] for row in rows] == list(snapshot.GENERATED_BUILD_INFO_PATHS)
    assert all(row["generated"] for row in rows)
    assert all(row["bytes"] == len(payload) for row in rows)
    assert all(row["sha256"] == hashlib.sha256(payload).hexdigest() for row in rows)
    assert source.read_bytes() == payload


@pytest.mark.parametrize("field,value", (
    ("version", "3.0.0"), ("commit", "b" * 40), ("build_id", "invalid"),
    ("build_time_utc", None), ("contract_version", ""),
))
def test_stale_or_incomplete_build_identity_is_not_added_to_snapshot(tmp_path, field, value):
    identity = _identity()
    identity[field] = value
    source = tmp_path / "materialized.json"
    source.write_text(json.dumps(identity), encoding="utf-8")
    with pytest.raises(ValueError):
        snapshot.generated_build_info_entries(source, version="3.0.1", base_commit="a" * 40)
