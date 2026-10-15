"""Versioned, side-effect-free stage journal adaptation.

Legacy artifacts remain readable but cannot manufacture a new executor's
input/prompt identity. Only exact versioned records are reusable candidates;
the executor independently verifies them again before trusting their content.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable, Mapping

from ..resilience.contracts import candidate_document_json, digest, plain_json_value
from .executor import StageResult, StageValidation
from .registry import StageRegistry, StageSpec
from .responses import ModelResponse


SCHEMA = "openthesis.stage-checkpoint.v1"


def checkpoint_artifact_id(run_id: str, payload: dict[str, Any]) -> str:
    """One content identity for transactional and compatibility persistence."""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    suffix = hashlib.sha256(b"research-execution|" + raw).hexdigest()[:10]
    return f"{run_id}:stage-checkpoint:{suffix}"


@dataclass(frozen=True, slots=True)
class CheckpointRead:
    results: Mapping[str, StageResult]
    issues: tuple[dict[str, str], ...] = ()
    revalidation_candidates: Mapping[str, StageResult] = field(default_factory=dict)


def candidate_checkpoint(spec: StageSpec, input_digest: str, candidate: dict[str, Any],
                         *, execution_fingerprint: str = "") -> dict[str, Any]:
    diagnostics = {}
    if isinstance(candidate, ModelResponse):
        # Compatibility/direct callers may not have crossed ModelStageExecutor.
        # These are host-owned attributes, never model supplied body fields.
        diagnostics = {"response_receipt": plain_json_value(candidate.receipt),
                       "response_diagnostics": plain_json_value(candidate.diagnostics)}
    content = plain_json_value(candidate)
    result = StageResult(spec.stage_id, spec.identity, input_digest, content,
                         StageValidation(False, state="needs_action"), digest(content),
                         execution_fingerprint=execution_fingerprint, has_candidate=True,
                         diagnostics=diagnostics)
    return completed_checkpoint(result)


def completed_checkpoint(result: StageResult) -> dict[str, Any]:
    # Encode our own fields explicitly. asdict recursively reconstructs dict
    # subclasses, which crashes real gateway responses before verification.
    validation = result.validation
    payload = {"schema": SCHEMA, "result": {
        "stage_id": result.stage_id, "stage_identity": result.stage_identity,
        "input_digest": result.input_digest, "candidate": plain_json_value(result.candidate),
        "validation": {"passed": validation.passed,
                       "trusted_output": plain_json_value(validation.trusted_output),
                       "state": validation.state, "issues": list(validation.issues),
                       "record": plain_json_value(validation.record)},
        "candidate_digest": result.candidate_digest, "error_code": result.error_code,
        "retryable": result.retryable, "execution_fingerprint": result.execution_fingerprint,
        "has_candidate": result.has_candidate, "diagnostics": plain_json_value(result.diagnostics),
    }}
    # Keep the same bounded credential-free candidate journal contract as the
    # recovery repository; this does not prove any factual claim is correct.
    candidate_document_json(payload)
    return payload


def _read_result(value: Any) -> StageResult:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("STAGE_CHECKPOINT_SCHEMA_UNSUPPORTED")
    candidate_document_json(value)
    raw = value.get("result")
    if not isinstance(raw, dict):
        raise ValueError("STAGE_CHECKPOINT_CONTRACT_INVALID")
    strings = ("stage_id", "stage_identity", "input_digest", "candidate_digest",
               "error_code", "execution_fingerprint")
    if any(not isinstance(raw.get(key), str) for key in strings):
        raise ValueError("STAGE_CHECKPOINT_CONTRACT_INVALID")
    if any(len(raw[key]) != 64 or any(char not in "0123456789abcdef" for char in raw[key])
           for key in ("stage_identity", "input_digest", "candidate_digest")):
        raise ValueError("STAGE_CHECKPOINT_DIGEST_INVALID")
    if not isinstance(raw.get("candidate"), dict) or any(
        not isinstance(raw.get(key), bool) for key in ("has_candidate", "retryable")
    ):
        raise ValueError("STAGE_CHECKPOINT_CONTRACT_INVALID")
    validation = raw.get("validation")
    if (not isinstance(validation, dict) or not isinstance(validation.get("passed"), bool)
            or not isinstance(validation.get("state"), str)
            or not isinstance(validation.get("trusted_output"), dict)
            or not isinstance(validation.get("record"), dict)
            or not isinstance(validation.get("issues"), (list, tuple))
            or any(not isinstance(item, str) for item in validation["issues"])):
        raise ValueError("STAGE_CHECKPOINT_VALIDATION_INVALID")
    if digest(raw["candidate"]) != raw["candidate_digest"]:
        raise ValueError("STAGE_CHECKPOINT_CANDIDATE_CHANGED")
    diagnostics = raw.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise ValueError("STAGE_CHECKPOINT_DIAGNOSTICS_INVALID")
    proof = StageValidation(validation["passed"], deepcopy(validation["trusted_output"]),
                            validation["state"], tuple(validation["issues"]),
                            deepcopy(validation["record"]))
    return StageResult(raw["stage_id"], raw["stage_identity"], raw["input_digest"],
                       deepcopy(raw["candidate"]), proof, raw["candidate_digest"],
                       raw["error_code"], raw["retryable"], raw["execution_fingerprint"], raw["has_candidate"],
                       deepcopy(diagnostics))


def read_checkpoints(artifacts: Iterable[Mapping[str, Any]], registry: StageRegistry) -> CheckpointRead:
    """Read chronological immutable artifacts without storage or provider calls."""
    results: dict[str, StageResult] = {}
    candidates: dict[str, StageResult] = {}
    issues: list[dict[str, str]] = []
    for artifact in artifacts:
        if artifact.get("artifact_type") not in {"stage-candidate", "stage-checkpoint"}:
            continue
        try:
            result = _read_result(artifact.get("content"))
            spec = registry.get(result.stage_id)
            if result.has_candidate:
                candidates[result.stage_id] = result
            else:
                candidates.pop(result.stage_id, None)
            if result.stage_identity != spec.identity:
                # An obsolete success bit is not reusable proof. The bounded,
                # hash-checked body can still be checked under today's sources
                # and validator without repeating a paid generation call.
                results.pop(result.stage_id, None)
                issues.append({"artifact_id": str(artifact.get("artifact_id") or ""),
                               "code": "STAGE_CHECKPOINT_IMPLEMENTATION_CHANGED"})
                continue
            results[result.stage_id] = result
        except (KeyError, TypeError, ValueError) as exc:
            # A newer broken journal record is not permission to reuse an
            # older success for that stage. Keep siblings, but remove the
            # identified stage until a later valid attempt supersedes it.
            content = artifact.get("content")
            raw = content.get("result", content) if isinstance(content, dict) else {}
            stage_id = raw.get("stage_id") if isinstance(raw, dict) else None
            if isinstance(stage_id, str):
                results.pop(stage_id, None)
                candidates.pop(stage_id, None)
            issues.append({"artifact_id": str(artifact.get("artifact_id") or ""),
                           "code": str(exc) if isinstance(exc, ValueError) else "STAGE_CHECKPOINT_CONTRACT_INVALID"})
    return CheckpointRead(results, tuple(issues), candidates)
