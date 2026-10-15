from __future__ import annotations

from copy import deepcopy
from typing import Any


def resolve_report_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Fold immutable report overlays without rewriting qualitative artifacts."""

    reports = [item for item in artifacts if item.get("artifact_type") == "research-report"]
    if not reports:
        return None
    by_id = {str(item.get("artifact_id")): item for item in reports}
    current = reports[-1]
    content = current.get("content")
    if not isinstance(content, dict) or content.get("mode") != "financial-refresh-overlay":
        return current
    # Follow historical chains iteratively. One-level folding silently loses
    # original analysis after a second financial-only refresh. Never borrow
    # an unrelated report when a bound base is missing, and never loop on a
    # corrupt/cyclic chain. Original artifacts remain untouched.
    base = current
    base_id = ""
    visited = set()
    diagnostic = ""
    while isinstance(base.get("content"), dict) and base["content"].get("mode") == "financial-refresh-overlay":
        identity = str(base.get("artifact_id") or "")
        if identity in visited:
            diagnostic = "REPORT_OVERLAY_CHAIN_INVALID"
            break
        visited.add(identity)
        base_id = str(base["content"].get("base_report_artifact_id") or "")
        base = by_id.get(base_id)
        if base is None or not isinstance(base.get("content"), dict):
            diagnostic = "REPORT_OVERLAY_BASE_MISSING"
            break
    if diagnostic:
        merged: dict[str, Any] = {
            "mode": "financial-refresh", "report": {}, "research_complete": False,
            "diagnostic_code": diagnostic,
        }
    else:
        merged = deepcopy(base["content"])
        merged["mode"] = "financial-refresh"
    merged["financial_refresh"] = deepcopy(content.get("financial_refresh", {}))
    merged["base_report_artifact_id"] = base_id
    return {**current, "content": merged}
