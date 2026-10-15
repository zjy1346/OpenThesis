"""Immutable publication guards; no analysis, rendering or external I/O here."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import sqlite3
from typing import Any


# Lifecycle fields and unknown extensions are deliberately not input identity.
# They are preserved from the current row, not restored from a worker's copy.
INPUT_FIELDS = (
    "run_id", "company", "workflow_id", "research_pack_id", "research_pack_version",
    "provider_id", "model_id", "data_as_of", "report_language", "market_snapshot",
    "model_configuration", "research_configuration", "data_snapshot",
)


class ResearchPublicationConflict(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.retryable = False


def input_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps({key: payload.get(key) for key in INPUT_FIELDS},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchPublicationSnapshot:
    run_id: str
    input_digest: str
    revision_id: str | None
    report_generation: int
    stage_heads: tuple[tuple[str, int, str], ...]


def _report_head(db: sqlite3.Connection, run_id: str) -> tuple[str | None, int]:
    row = db.execute("SELECT revision_id,generation FROM latest_report_revisions WHERE run_id=?",
                     (run_id,)).fetchone()
    return (row["revision_id"], int(row["generation"])) if row else (None, 0)


def _stage_heads(db: sqlite3.Connection, run_id: str) -> tuple[tuple[str, int, str], ...]:
    return tuple((row["stage_id"], int(row["generation"]), row["identity_digest"])
                 for row in db.execute("SELECT stage_id,generation,identity_digest "
                     "FROM model_stage_request_heads WHERE run_id=? ORDER BY stage_id", (run_id,)))


def capture_snapshot(db: sqlite3.Connection, payload: dict[str, Any]) -> ResearchPublicationSnapshot:
    run_id = payload["run_id"]
    revision_id, generation = _report_head(db, run_id)
    return ResearchPublicationSnapshot(run_id, input_digest(payload), revision_id,
                                       generation, _stage_heads(db, run_id))


def validate_snapshot(db: sqlite3.Connection, payload: dict[str, Any],
                      snapshot: ResearchPublicationSnapshot, *, check_stages: bool = True) -> None:
    if payload.get("run_id") != snapshot.run_id or input_digest(payload) != snapshot.input_digest:
        raise ResearchPublicationConflict("RESEARCH_PUBLICATION_INPUT_CONFLICT")
    if _report_head(db, snapshot.run_id) != (snapshot.revision_id, snapshot.report_generation):
        raise ResearchPublicationConflict("RESEARCH_PUBLICATION_REVISION_CONFLICT")
    if check_stages and _stage_heads(db, snapshot.run_id) != snapshot.stage_heads:
        raise ResearchPublicationConflict("RESEARCH_PUBLICATION_STAGE_CONFLICT")


def bind_stage_heads(db: sqlite3.Connection, payload: dict[str, Any],
                     snapshot: ResearchPublicationSnapshot,
                     generations: dict[str, int]) -> ResearchPublicationSnapshot:
    """Allow only this execution's explicit prepare to change its stage heads."""
    validate_snapshot(db, payload, snapshot, check_stages=False)
    heads = _stage_heads(db, snapshot.run_id)
    previous = {stage: (generation, identity) for stage, generation, identity in snapshot.stage_heads}
    current = {stage: (generation, identity) for stage, generation, identity in heads}
    if ({stage: value for stage, value in previous.items() if stage not in generations}
            != {stage: value for stage, value in current.items() if stage not in generations}
            or any(stage not in current or current[stage][0] != generation
                   for stage, generation in generations.items())
            or any(stage in previous and current[stage][0] == previous[stage][0]
                   and current[stage][1] != previous[stage][1] for stage in generations)):
        raise ResearchPublicationConflict("RESEARCH_PUBLICATION_STAGE_CONFLICT")
    return replace(snapshot, stage_heads=heads)
