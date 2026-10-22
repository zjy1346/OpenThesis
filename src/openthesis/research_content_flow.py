"""Observability of content reduction; counts never grant research trust."""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


VERSION = "openthesis.research-content-flow.v1"
_LABELS = {"evidence_ids", "source_evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids", "claim_ids",
    "input_claim_ids", "id", "claim_id", "name", "title", "label", "kind", "type", "confidence",
    "severity", "role", "method", "applicability", "structured_output_valid", "as_of",
    "supporting_evidence_count", "contradicting_evidence_count"}


def parsed_paths(value: Any, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        if path == "$":
            from .research_response import response_parts
            value, _metadata, _diagnostics = response_parts(value)
        return [child for key, item in value.items() if not str(key).startswith("_") and key not in _LABELS
            for child in parsed_paths(item, path + "." + str(key))]
    if isinstance(value, (list, tuple)):
        return [child for index, item in enumerate(value) for child in parsed_paths(item, f"{path}[{index}]")]
    return [path] if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != "" else []


def _proofs(value: Any):
    if not isinstance(value, Mapping):
        return
    if str(value.get("schema", "")).startswith("evidence-bound-sections-"):
        yield value
        return
    for item in value.values():
        if isinstance(item, Mapping):
            yield from _proofs(item)


def stage_flow(result, sources, *, generation: int, request_rows=()) -> dict[str, Any]:
    source_ids = sorted({str(item["evidence_id"]) for item in sources if isinstance(item, dict)
        and item.get("kind") != "material_gap" and item.get("evidence_id")})
    # Base-stage and OT validators attach their section proof to the trusted
    # output; final synthesis also carries it in the validation record. Read
    # both contracts, never provider candidate metadata, and deduplicate by
    # host-generated node identity.
    proofs = [*_proofs(result.validation.record), *_proofs(result.validation.trusted_output)]
    nodes = {node["node_id"]: node for proof in proofs for node in proof.get("nodes", ())
        if isinstance(node, dict) and isinstance(node.get("node_id"), str)}
    reductions = [dict(item) for proof in proofs for item in proof.get("content_flow", {}).get("reductions", ())
        if isinstance(item, dict)]
    if result.error_code:
        reductions.append({"code": result.error_code, "path": "stage:" + result.stage_id})
    roles = Counter(str(node.get("role") or "unknown") for node in nodes.values())
    paths = parsed_paths(result.candidate)
    return {"schema": VERSION, "stage_id": result.stage_id, "generation": generation,
        "input_digest": result.input_digest, "state": result.validation.state,
        "source_fragments": len(source_ids), "source_ids": source_ids,
        "requested_materials": len(source_ids) if request_rows else 0,
        "request_attempts": len(request_rows), "received_responses": sum(row["response_received"] == 1 for row in request_rows),
        "candidate_available": result.has_candidate,
        "parsed_leaf_count": len(paths), "parsed_leaf_paths": paths[:512], "path_display_limit": 512,
        "accepted_nodes": len(nodes),
        "accepted_by_role": dict(roles), "reductions": reductions}


def report_flow(artifacts, visible_sections) -> dict[str, Any]:
    stages = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        content = artifact.get("content") if isinstance(artifact, dict) else None
        if artifact.get("artifact_type") == "stage-content-flow" and isinstance(content, dict) and content.get("schema") == VERSION:
            stages[str(content["stage_id"])] = content
    roles = Counter(item.get("role", "unknown") for section in visible_sections
        for item in section.get("content_semantics", ()) if item.get("role") not in {"notice", "unknown"}
        and item.get("validation_state") in {"verified", "assumption"})
    return {"schema": VERSION, "latest_stage_attempts": list(stages.values()),
        "presented_semantic_nodes": sum(roles.values()), "presented_by_role": dict(roles),
        "counting_note": "Attempt leaves, proof nodes and rendered nodes are different units; counts are not a completeness score."}
