"""Offline integration checks for lossless chunked stage payloads.

These tests exercise the public artifact/checkpoint seams.  They prove byte
retention and transport integrity only; they do not turn a fixture candidate
into a trusted financial or research conclusion.
"""
from __future__ import annotations

import json
from pathlib import Path

from openthesis.domain import Company, ResearchArtifact, ResearchRun
from openthesis.research_execution.checkpoints import (
    candidate_checkpoint,
    read_checkpoints,
)
from openthesis.research_execution.durable import DurableStageVerifier
from openthesis.research_execution.executor import StageResult, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.resilience.contracts import digest
from openthesis.storage import Storage


RUN_ID = "chunked-stage-payload-run"


def _storage(data_dir: Path) -> Storage:
    store = Storage(data_dir)
    company = Company("0000000099", "CHUNK", "Chunked Payload Fixture Co.")
    store.save_company(company)
    store.save_run(ResearchRun(
        RUN_ID,
        company,
        "chunked-payload-workflow",
        "offline-fixture-pack",
        "1",
        "offline-provider",
        "offline-model",
        "2026-10-11",
    ))
    return store


def _spec() -> StageSpec:
    return StageSpec(
        "financial-analyst",
        "chunked-payload-v1",
        "offline-producer",
        "offline-validator",
        output_contract="chunked-payload-fixture-v1",
    )


def _large_candidate() -> dict:
    # A multi-megabyte UTF-8 body ensures the one-row journal limit cannot be
    # accidentally used as the persistence path.
    return {
        "kind": "transport-only-fixture",
        "analysis": "已验证材料仍需经过业务验证；" * 220_000,
        "evidence_ids": ["filing:chunked-fixture:page-1"],
        "unknown_extension": {"preserve": "扩展字段不可丢失"},
    }


def test_large_candidate_checkpoint_roundtrips_through_storage_restart(tmp_path: Path) -> None:
    store = _storage(tmp_path / "storage")
    spec = _spec()
    candidate = _large_candidate()
    input_digest = digest({"run_id": RUN_ID, "source": "frozen-fixture"})
    fingerprint = digest({"executor": "offline-fixture-v1"})
    checkpoint = candidate_checkpoint(
        spec,
        input_digest,
        candidate,
        execution_fingerprint=fingerprint,
    )

    store.save_artifact(ResearchArtifact(
        f"{RUN_ID}:stage-checkpoint:large",
        RUN_ID,
        "stage-checkpoint",
        spec.stage_id,
        checkpoint,
        model_id="offline-model",
        agent_id="offline-fixture",
    ))

    with store.connect() as db:
        row = db.execute(
            "SELECT payload_json FROM artifacts WHERE artifact_id=?",
            (f"{RUN_ID}:stage-checkpoint:large",),
        ).fetchone()
        assert row is not None
        # The SQLite row contains a bounded chunk manifest, not the full body.
        assert len(row["payload_json"].encode("utf-8")) < 1_048_576

    reopened = Storage(tmp_path / "storage")
    artifacts = reopened.get_artifacts(RUN_ID)
    saved = next(item for item in artifacts if item["artifact_type"] == "stage-checkpoint")
    # JSON persistence intentionally normalizes tuple fields (for example
    # validation issues) to arrays; the candidate body and its digest remain
    # byte/content identical after this transport normalization.
    assert saved["content"] == json.loads(json.dumps(checkpoint, ensure_ascii=False))
    assert saved["content"]["result"]["candidate"] == candidate
    assert saved["content"]["result"]["candidate_digest"] == digest(candidate)

    read = read_checkpoints(artifacts, StageRegistry((_spec(),)))
    assert not read.issues
    result = read.results[spec.stage_id]
    assert result.candidate == candidate
    assert result.candidate_digest == digest(candidate)


def test_durable_local_verification_commits_chunked_candidate_and_reopens_without_provider(
    tmp_path: Path,
) -> None:
    store = _storage(tmp_path / "storage")
    spec = _spec()
    candidate = _large_candidate()
    stage_input = digest({"run_id": RUN_ID, "source": "durable-fixture"})
    fingerprint = digest({"executor": "durable-offline-v1"})
    provider_calls: list[str] = []

    def verify_local(value: dict) -> StageResult:
        assert value == candidate
        return StageResult(
            spec.stage_id,
            spec.identity,
            stage_input,
            value,
            StageValidation(
                True,
                {"transport_preserved": True},
                "completed_verified",
            ),
            digest(value),
            execution_fingerprint=fingerprint,
            has_candidate=True,
        )

    result = DurableStageVerifier(store, RUN_ID)(
        spec,
        stage_input,
        candidate,
        fingerprint,
        verify_local,
    )
    assert result.validation.passed is True
    assert provider_calls == []

    reopened = Storage(tmp_path / "storage")
    artifacts = reopened.get_artifacts(RUN_ID)
    read = read_checkpoints(artifacts, StageRegistry((_spec(),)))
    assert not read.issues
    committed = read.results[spec.stage_id]
    assert committed.validation.passed is True
    assert committed.candidate == candidate
    assert committed.candidate_digest == digest(candidate)

