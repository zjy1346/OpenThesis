import json
from copy import deepcopy
from unittest.mock import patch
from openthesis.research_materials import canonical_request_materials
from openthesis.research_execution.source_compiler import compiled_role_sources


def expand(value, root, seen=()):
    if isinstance(value, dict):
        if set(value) == {"request_path_ref"}:
            path = tuple(value["request_path_ref"])
            assert path not in seen, "cyclic request alias"
            selected = root
            for part in path:
                selected = selected[part]
            return expand(selected, root, (*seen, path))
        if set(value) == {"material_ref"}:
            return root["material_registry"][value["material_ref"]]["text"]
        return {key: expand(item, root, seen) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, root, seen) for item in value]
    return value


def test_frozen_source_aliases_are_lossless_and_dynamic_findings_do_not_churn_prefix():
    source = {"evidence_id": "filing:313", "raw_text": "Complete original source and counterevidence. " * 80,
              "currency": "CNY", "period": "FY2025", "scope": "consolidated"}
    original = {"a": [deepcopy(source)], "b": [deepcopy(source)], "risk": {"complete_footnotes": "Uncertainty persists. " * 30}}
    context, findings, registry = canonical_request_materials(original, {})
    wire = {"research_context": context, "prior_artifacts": findings, "material_registry": registry}
    assert expand(context, wire) == original
    changed, _, registry2 = canonical_request_materials(original, {"new": {"text": "Different conclusion. " * 100}})
    assert changed == context and registry2 == registry
    assert len(json.dumps(wire, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))


def test_compiler_reuses_exact_snapshot_and_invalidates_changed_original():
    context = {"company": {"cik": "compiler-313"}, "evidence": [{"evidence_id": "filing:compiler313", "topic": "business",
        "kind": "filing_text", "document_id": "official", "source_url": "https://example.test/313", "raw_text": "Original qualifier retained."}]}
    encoded = json.dumps(context)
    first = compiled_role_sources(encoded, "business-analyst")
    with patch("openthesis.research_materials.prompt_materials", side_effect=AssertionError("recompiled unchanged snapshot")):
        assert compiled_role_sources(encoded, "business-analyst") == first
    context["evidence"][0]["raw_text"] = "Original changed: activity ceased."
    assert compiled_role_sources(json.dumps(context), "business-analyst") != first
