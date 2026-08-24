from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = (
    PROJECT_ROOT
    / "tools"
    / "official-pack-migration-source"
    / "official.long-term-fundamentals"
)
TARGET = (
    PROJECT_ROOT
    / "src"
    / "openthesis"
    / "resources"
    / "ot-packages"
    / "official.long-term-fundamentals.ot"
)
CONVERTER_VERSION = "2.0.0-ot-v1"


def source_identity(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def convert() -> tuple[str, str]:
    source = SOURCE_ROOT.resolve()
    if source.name != "official.long-term-fundamentals" or source.parent.name != "official-pack-migration-source":
        raise RuntimeError("converter source is outside the trusted official package path")
    if not source.is_dir():
        raise RuntimeError("trusted official package source is missing")

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from openthesis.ot import compile_studio_draft

    manifest = json.loads((source / "manifest.yaml").read_text(encoding="utf-8"))
    workflow = json.loads((source / "workflow.yaml").read_text(encoding="utf-8"))
    metadata = manifest["metadata"]
    source_hash = source_identity(source)

    steps = []
    for step in workflow["workflow"]["steps"]:
        prompt_path = source / step["prompt"]
        steps.append(
            {
                "id": step["id"],
                "role": step["agent"],
                "depends_on": list(step.get("depends_on", [])),
                "prompt": prompt_path.read_text(encoding="utf-8"),
                "prompt_path": f"resources/{step['prompt']}",
                "output_schema": "ot://openthesis/agent-artifact/1",
            }
        )

    draft = {
        "package": {
            "id": metadata["id"],
            "name": metadata["name"],
            "version": metadata["version"],
            "kind": "openthesis.research-pack",
            "description": manifest["description"],
            "license": metadata["license"],
            "compatibility": {"openthesis": ">=2.0.0,<3.0.0"},
        },
        "settings": {
            "horizon_years": 10,
            "depth": 5,
            "risk_emphasis": 5,
            "report_language": "en",
            "workflow_id": workflow["workflow"]["id"],
            "workflow_version": workflow["workflow"]["version"],
            "limits": workflow["limits"],
            "languages": metadata["languages"],
        },
        "workflow": {"steps": steps},
        "outputs": {
            "formats": ["json", "markdown", "html"],
            "include_evidence": True,
            "deterministic_transforms": [
                "openthesis.financial-summary.v1",
                "openthesis.report-projection.v1",
            ],
        },
        "ui": {
            "mode": "official",
            "controls": ["report_language", "research_depth"],
        },
        "model_requirements": {
            "capabilities": ["text_chat", "structured_json"],
            "minimum_context_tokens": manifest["model_requirements"]["minimum_context_tokens"],
        },
        "dependencies": [],
        "relationships": [],
        "optional_extensions": {
            "openthesis.official_migration": {
                "source_format": ".othesis-development-source",
                "source_content_sha256": source_hash,
                "converter_version": CONVERTER_VERSION,
            }
        },
        "provenance": {
            "created_with": f"OpenThesis official converter {CONVERTER_VERSION}",
            "source_content_sha256": source_hash,
        },
    }

    raw, package = compile_studio_draft(draft)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix(".ot.tmp")
    temporary.write_bytes(raw)
    temporary.replace(TARGET)

    target_steps = json.loads(package.read_text("resources/workflow.json"))["steps"]
    projected = [
        {
            "id": item["id"],
            "agent": item["role"],
            "depends_on": item.get("depends_on", []),
        }
        for item in target_steps
    ]
    expected = [
        {
            "id": item["id"],
            "agent": item["agent"],
            "depends_on": item.get("depends_on", []),
        }
        for item in workflow["workflow"]["steps"]
    ]
    if projected != expected:
        TARGET.unlink(missing_ok=True)
        raise RuntimeError("official workflow semantic equivalence check failed")
    return source_hash, package.content_identity


if __name__ == "__main__":
    old_hash, new_hash = convert()
    print(json.dumps({"source_sha256": old_hash, "ot_content_identity": new_hash}, indent=2))
