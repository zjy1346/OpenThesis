"""Open analysis content with an explicit envelope/control-field boundary.

This is classification, not trust. It never edits the stored raw candidate or
turns an unknown analysis field into a verified fact. Root identity/transport
fields are metadata; unfamiliar professional analysis remains open content.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


ROOT_METADATA_FIELDS = frozenset({
    "agent", "agent_id", "agent_role", "as_of", "generated_at", "created_at", "updated_at",
    "company", "company_id", "company_cik", "schema", "schema_version", "run_id", "request_id",
    "generation_id", "model", "model_id", "provider_id", "usage", "billing", "billing_info",
    "metadata", "diagnostics", "receipt", "response_metadata", "structured_output_valid",
    "analysis_refs", "analysis_registry", "analysis_reference_contract",
})


def response_parts(value: Mapping[str, Any]):
    content, metadata, diagnostics = {}, {}, {}
    for key, child in value.items():
        if key == "diagnostics":
            diagnostics[key] = deepcopy(child)
        elif key in ROOT_METADATA_FIELDS or str(key).startswith("_"):
            metadata[key] = deepcopy(child)
        else:
            content[key] = deepcopy(child)
    # A declared envelope is not an opaque extra section. Retain sibling
    # professional fields too; a content key alone is not assumed an envelope.
    if isinstance(content.get("content"), dict) and any(key in value for key in ("metadata", "diagnostics")):
        nested, nested_meta, nested_diag = response_parts(content.pop("content"))
        for key, child in nested.items():
            if key not in content:
                content[key] = child
            elif content[key] != child:
                diagnostics.setdefault("ambiguous_content_fields", {})[key] = child
        metadata["content_metadata"] = nested_meta
        diagnostics["content_diagnostics"] = nested_diag
    return content, metadata, diagnostics
