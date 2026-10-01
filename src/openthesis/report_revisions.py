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
    base_id = str(content.get("base_report_artifact_id") or "")
    base = by_id.get(base_id)
    if base is None or not isinstance(base.get("content"), dict):
        merged: dict[str, Any] = {
            "mode": "financial-refresh", "report": {}, "research_complete": False
        }
    else:
        merged = deepcopy(base["content"])
        merged["mode"] = "financial-refresh"
    merged["financial_refresh"] = deepcopy(content.get("financial_refresh", {}))
    merged["base_report_artifact_id"] = base_id
    return {**current, "content": merged}
