"""Offline 3.3.1 request audit. Never initializes a provider or calls a model.

Use the identical --context JSON under each frozen PYTHONPATH for an A/B
measurement. Public source bodies stay in local inputs, not in audit output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", type=Path)
    parser.add_argument("--symbol")
    parser.add_argument("--name", default="Frozen source company")
    parser.add_argument("--context", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-prefix")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if args.run_prefix:
        # This branch is deliberately plain sqlite read-only, never Storage
        # initialization/migration on a user's production research database.
        db = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
        rows = db.execute("SELECT run_id,status FROM research_runs WHERE run_id LIKE ?", (args.run_prefix + "%",)).fetchall()
        result = {"database_mode": "read_only", "run_prefix": args.run_prefix, "runs": []}
        for run_id, state in rows:
            wire = db.execute("SELECT stage_id,purpose,diagnostics_json FROM model_wire_diagnostics WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
            result["runs"].append({"run_id": run_id, "state": state, "attempts": [{"stage": stage, "purpose": purpose,
                **{key: json.loads(raw).get(key) for key in ("wire_state", "error_code", "http_status", "input_bytes",
                    "input_tokens_estimate", "context_tokens_hint", "token_counting_method", "usage", "configured_model_id")}}
                for stage, purpose, raw in wire]})
        db.close()
    else:
        from openthesis.research import ResearchContext, _agent_prompt_bundle, build_fact_evidence
        from openthesis.markets import build_company
        from openthesis.research_execution.reference_aliases import reference_aliases, supplied_bindings
        from openthesis.research_execution.registry import standard_model_registry
        from openthesis.research_execution.registry import SOURCE_ANALYSIS_STAGES
        from openthesis.research_execution.production import _source_business_inputs
        from openthesis.packs import builtin_pack
        if args.context:
            context = json.loads(args.context.read_text(encoding="utf-8"))
        else:
            original = json.loads(args.canonical.read_text(encoding="utf-8"))
            facts = original.get("research_facts", original["resolved_facts"])
            company = build_company(args.symbol, args.name, industry="银行" if args.symbol == "600036.SH" else "制造业")
            source_records = original.get("evidence", [])
            context = json.loads(ResearchContext(company, facts, [], [], [*build_fact_evidence(facts), *source_records]).compact_json())
            registry = standard_model_registry()
            context["source_reader_roles"] = sorted({spec.role for spec in registry.stages})
            context["source_reader_stages"] = [spec.stage_id for spec in registry.stages if not spec.depends_on]
            context["_audit_source_sha256"] = hashlib.file_digest(args.canonical.open("rb"), "sha256").hexdigest()
            if args.freeze:
                args.freeze.write_text(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        source_hash = context.pop("_audit_source_sha256", None)
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        independent = _source_business_inputs({}, encoded, context["evidence"])["context_json"]
        pack = builtin_pack()
        rows = []
        required_bodies = {(str(row["evidence_id"]), field, hashlib.sha256(row[field].encode()).hexdigest())
            for row in context["evidence"] if row.get("evidence_id")
            for field in ("raw_text", "excerpt", "text") if isinstance(row.get(field), str) and row[field]}
        supplied_bodies = set()
        # No fabricated model conclusions. This is the compile-time lower
        # bound with upstream bodies explicitly absent, not a quality verdict.
        for spec in standard_model_registry().stages:
            stage_context = independent if spec.stage_id in SOURCE_ANALYSIS_STAGES else encoded
            aliases = reference_aliases(stage_context)
            prior = {"source_research_lane": spec.stage_id} if spec.stage_id.startswith("source-") else {}
            system, user = _agent_prompt_bundle(spec.role, pack.prompt(spec.prompt_path), "zh-CN", stage_context, prior)
            physical = sorted(supplied_bindings(user, aliases).values())
            if not spec.depends_on:
                from openthesis.research_execution.reference_aliases import _expand_request_paths, decode_references
                root = decode_references(_expand_request_paths(json.loads(user)), aliases)
                core, source = root.get("common_core", {}), root.get("research_context", {})
                facts = {item["fact_id"]: item for item in [*core.get("accepted_facts", ()), *source.get("accepted_facts", ())]
                    if isinstance(item, dict) and item.get("fact_id")}
                lanes = [*source.get("evidence_partitions", {}).values(), root.get("late_evidence", [])]
                for lane in lanes:
                    for item in lane if isinstance(lane, list) else ():
                        if not isinstance(item, dict) or not item.get("evidence_id"):
                            continue
                        item = {**facts.get(item.get("accepted_fact_ref"), {}), **item}
                        for field in ("raw_text", "excerpt", "text"):
                            body = item.get(field)
                            if isinstance(body, dict) and set(body) == {"material_ref"}:
                                body = root.get("material_registry", {}).get(body["material_ref"], {}).get("text")
                            if isinstance(body, str) and body:
                                supplied_bodies.add((str(item["evidence_id"]), field, hashlib.sha256(body.encode()).hexdigest()))
            rows.append({"stage": spec.stage_id, "input_bytes": len(system.encode()) + len(user.encode()),
                "physically_supplied_source_ids": physical, "source_count": len(physical),
                "output_contract": spec.output_contract})
        first_read = {source for row in rows[:5] for source in row["physically_supplied_source_ids"]}
        all_ids = {str(item["evidence_id"]) for item in context["evidence"] if item.get("evidence_id")}
        result = {"schema": "offline-request-audit-v1", "mode": "compile_time_without_model_outputs",
            "input_sha256": hashlib.sha256(encoded.encode()).hexdigest(), "source_file_sha256": source_hash,
            "model_calls": 0, "actual_supplier_cache": "unverified_no_call", "stages": rows,
            "required_evidence_count": len(all_ids), "missing_first_read_sources": sorted(all_ids - first_read),
            "required_original_body_variants": len(required_bodies),
            "missing_first_read_body_variants": sorted(required_bodies - supplied_bodies),
            "max_input_bytes": max(row["input_bytes"] for row in rows),
            "total_input_bytes": sum(row["input_bytes"] for row in rows)}
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "stages"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
