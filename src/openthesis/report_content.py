"""Typed semantic metadata shared by report assembly and renderers.

These values describe the origin and validation evidence carried by a report
node.  In particular, presentation text is never treated as evidence of its
own validation state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from .research_section_validation import SECTION_PROOF_VERSION, content_digest, proven_content_nodes


CONTENT_ROLES = frozenset({
    "analysis",
    "verified_analysis",
    "assumption",
    "fact_table",
    "source",
    "source_quote",
    "question",
    "methodology",
    "notice",
    "placeholder",
    "unknown",
})
MATERIALITY_VALUES = frozenset({"material", "supporting", "non_material", "unknown"})
VALIDATION_STATES = frozenset({"verified", "partial", "rejected", "unknown"})


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    """Immutable summary of the validator evidence attached to a node."""

    state: str = "unknown"
    source_artifact_id: str = ""
    validator: str = ""
    passed: bool | None = None
    node_id: str = ""
    validator_version: str = ""
    body_sha256: str = ""
    evidence_ids: tuple[str, ...] = ()
    generation: str = ""


TRUSTED_SNAPSHOT_VERSION = "openthesis.trusted-content-snapshot.v1"
CONTENT_INVALIDATION_VERSION = "openthesis.content-invalidation.v1"


def read_content_invalidations(artifacts: Any, *, run_id: str) -> tuple[dict[str, Any], ...]:
    """Read immutable, financial-publication-owned corrections, not model flags."""
    result = []
    for row in artifacts:
        if (not isinstance(row, dict) or row.get("run_id") != run_id
                or row.get("artifact_type") != "trusted-content-invalidation"
                or row.get("agent_id") != "trusted-financial-publication"):
            continue
        content = row.get("content")
        if not isinstance(content, dict) or content.get("schema") != CONTENT_INVALIDATION_VERSION:
            continue
        try:
            payload = {key: value for key, value in content.items() if key != "record_sha256"}
            if content_digest(payload) != content.get("record_sha256") or content.get("run_id") != run_id:
                continue
            affected = content.get("affected_nodes")
            if not isinstance(affected, list) or any(not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not item[key]
                    for key in ("node_id", "source_artifact_id", "generation")) for item in affected):
                continue
            corrections = content.get("corrections")
            if (not isinstance(corrections, list) or not corrections or any(
                    not isinstance(item, dict) or not isinstance(item.get("new_fact"), dict)
                    or not isinstance(item["new_fact"].get("fact_id"), str) or not item["new_fact"]["fact_id"]
                    for item in corrections)):
                continue
        except (TypeError, ValueError):
            continue
        result.append(content)
    return tuple(result)


def financial_correction_invalidation(artifacts: Any, *, run_id: str, company_cik: str,
        old_facts: Any, accepted_facts: Any, validation_records: Any,
        source_hashes: dict[str, str], generation_id: str) -> dict[str, Any] | None:
    """Identify corrections only from fully proven replacement facts.

    Missing fields, failed reparses and changed parser labels are not evidence
    of a correction. Bind the affected *old proofs*, not all future uses of a
    fact ID. The caller appends this record in the financial publication CAS.
    """
    identity_fields = ("company_cik", "accession_number", "concept", "start_date", "end_date",
                       "fiscal_period", "consolidated_scope", "dimensions")

    def identity(value):
        return content_digest({key: value.get(key) for key in identity_fields})

    replacements = {}
    for fact in accepted_facts:
        value = fact.to_dict()
        record = validation_records.get(fact.fact_id, {})
        components = record.get("components", {})
        source_hash = source_hashes.get(fact.accession_number, "")
        if (record.get("accepted") is not True or record.get("fact_id") != fact.fact_id
                or not isinstance(components, dict) or any(
                    not isinstance(components.get(key), dict) or components[key].get("state") != "verified"
                    for key in ("amount", "scale", "currency", "period", "scope"))
                or len(source_hash) != 64 or any(char not in "0123456789abcdef" for char in source_hash)):
            continue
        replacements.setdefault(identity(value), []).append((value, record, source_hash))
    corrections = []
    affected_evidence = set()
    for old in old_facts:
        matches = replacements.get(identity(old), [])
        # Ambiguous competing replacements cannot revoke a known sibling.
        if len(matches) != 1:
            continue
        new, record, source_hash = matches[0]
        try:
            old_amount, new_amount = Decimal(str(old.get("value"))), Decimal(str(new.get("value")))
            if not old_amount.is_finite() or not new_amount.is_finite():
                continue
        except (InvalidOperation, ValueError):
            continue
        if old_amount == new_amount and old.get("unit") == new.get("unit"):
            continue
        old_id = str(old.get("fact_id") or "")
        if not old_id:
            continue
        ids = {"fact:" + old_id}
        if old_id.startswith("ingest:"):
            ids.add("fact:" + old_id.removeprefix("ingest:"))
        affected_evidence.update(ids)
        corrections.append({"old_fact": old, "new_fact": new, "validation_record": record,
                            "source_hash": source_hash, "evidence_ids": sorted(ids)})
    if not corrections:
        return None
    # Audit every frozen proof, including older same-input revisions. Otherwise
    # a failed newer attempt could resurrect a corrected node from history.
    affected = {}
    sources = {row.get("artifact_id"): row for row in artifacts if isinstance(row, dict)}
    for row in artifacts:
        if row.get("artifact_type") != "trusted-content-snapshot" or not isinstance(row.get("content"), dict):
            continue
        source_id = row["content"].get("source_artifact_id")
        source = sources.get(source_id)
        if source is None:
            continue
        for node in read_trusted_snapshots([source, row], run_id=run_id):
            dependencies = (set(node.get("depends_on", ())) | set(node.get("evidence_ids", ()))
                            | set(node.get("dependency_evidence_ids", ())))
            if not dependencies.intersection(affected_evidence):
                continue
            binding = {key: node[key] for key in ("node_id", "source_artifact_id", "generation")}
            binding.update({"source_path": node["source_path"], "section_id": node.get("display_section_id", node["section_id"]),
                            "context_sha256": node.get("context_sha256")})
            affected[content_digest(binding)] = binding
    payload = {"schema": CONTENT_INVALIDATION_VERSION, "run_id": run_id, "company_cik": company_cik,
               "financial_generation": generation_id, "reason": "source_confirmed_fact_correction",
               "affected_evidence_ids": sorted(affected_evidence), "corrections": corrections,
               "affected_nodes": list(affected.values())}
    return {**payload, "record_sha256": content_digest(payload)}


def invalidated_content_sections(artifacts: Any, *, run_id: str) -> set[str]:
    records = read_content_invalidations(artifacts, run_id=run_id)
    bindings = {tuple(item[key] for key in ("node_id", "source_artifact_id", "generation"))
                for record in records for item in record["affected_nodes"]}
    return {node.get("display_section_id", node["section_id"])
            for row in artifacts if row.get("artifact_type") == "trusted-content-snapshot"
            and isinstance(row.get("content"), dict) for node in row["content"].get("nodes", ())
            if isinstance(node, dict) and tuple(node.get(key) for key in (
                "node_id", "source_artifact_id", "generation")) in bindings}


def unresolved_content_invalidations(artifacts: Any, *, run_id: str) -> tuple[dict[str, Any], ...]:
    """A new source-bound proof can close a correction, never erase its audit."""
    nodes = read_trusted_snapshots(artifacts, run_id=run_id)
    pending = []
    financial = next((row for row in reversed(artifacts) if row.get("run_id") == run_id
                      and row.get("artifact_type") == "deterministic-financial-summary"), None)
    for record in read_content_invalidations(artifacts, run_id=run_id):
        replacement_ids = {"fact:" + item["new_fact"]["fact_id"] for item in record["corrections"]}
        replacement_ids.update("fact:" + item["new_fact"]["fact_id"].removeprefix("ingest:")
                               for item in record["corrections"] if item["new_fact"]["fact_id"].startswith("ingest:"))
        if ((financial is not None and project_financial_source_corrections([financial,
                {"run_id": run_id, "artifact_type": "trusted-content-invalidation",
                 "agent_id": "trusted-financial-publication", "content": record}], run_id=run_id)[0]
                .get("content", {}).get("_source_correction_fields"))
                or any(not any(node["source_path"] == affected.get("source_path")
                and node.get("validation_state") == "verified"
                and node.get("display_section_id", node["section_id"]) == affected.get("section_id")
                and node.get("context_sha256") == affected.get("context_sha256")
                and replacement_ids.intersection(set(node.get("evidence_ids", ())) |
                                                 set(node.get("depends_on", ())) |
                                                 set(node.get("dependency_evidence_ids", ())))
                for node in nodes) for affected in record["affected_nodes"])):
            pending.append(record)
    return tuple(pending)


def project_financial_source_corrections(artifacts: Any, *, run_id: str) -> list[dict[str, Any]]:
    """Withdraw only bound old numeric inputs/derivations from the read view.

    Source artifacts and previous revisions are never edited. A new summary
    using the corrected facts is unaffected, even when fact IDs are reused.
    """
    from copy import deepcopy
    corrections = [item for record in read_content_invalidations(artifacts, run_id=run_id)
                   for item in record["corrections"]]
    if not corrections:
        return list(artifacts)
    derivatives = {
        "revenue": {"revenue_growth", "operating_margin", "gross_margin", "net_margin", "parent_net_margin"},
        "net_income": {"net_income_growth", "net_margin", "cash_conversion"},
        "parent_net_income": {"parent_net_income_growth", "parent_net_margin"},
        "operating_cash_flow": {"operating_cash_flow_growth", "cash_conversion", "free_cash_flow", "free_cash_flow_money"},
        "assets": {"debt_to_assets"}, "liabilities": {"debt_to_assets"},
    }
    values = []
    for row in artifacts:
        if row.get("run_id") != run_id or row.get("artifact_type") != "deterministic-financial-summary":
            values.append(row)
            continue
        projected = deepcopy(row)
        content = projected.get("content", {})
        changed = []
        for collection in ("metrics", "interim_metrics"):
            for index, metric in enumerate(content.get(collection, ())):
                if not isinstance(metric, dict):
                    continue
                metadata = metric.get("money_metadata", {})
                if not isinstance(metadata, dict):
                    continue
                affected = set()
                for correction in corrections:
                    old = correction["old_fact"]
                    concept, old_id = old.get("concept"), old.get("fact_id")
                    cited = [item for item in content.get("evidence", ()) if isinstance(item, dict)
                             and item.get("evidence_id") in correction["evidence_ids"]]
                    if cited and all(item.get("value") != old.get("value") or item.get("unit") != old.get("unit")
                                     for item in cited):
                        # A newly recomputed input can retain the same ID.
                        continue
                    info = metadata.get(concept, {})
                    if (isinstance(info, dict) and info.get("fact_id") == old_id
                            and metric.get(concept) == old.get("value")
                            and info.get("currency") == old.get("unit")):
                        affected.update({concept, *derivatives.get(concept, ())})
                        attribution = metric.get("return_on_equity_attribution", {})
                        if (isinstance(attribution, dict) and concept in attribution.values()
                                and metric.get("return_on_equity_formula") != "reported_roe"):
                            affected.update({"return_on_equity", "return_on_equity_inputs"})
                    if cited and old_id in metric.get("comparison_fact_ids", ()):
                        growth = str(concept) + "_growth"
                        if growth in metric:
                            affected.add(growth)
                    for field, binding in metadata.items():
                        if (isinstance(binding, dict) and old_id in binding.get("input_fact_ids", ())
                                and old.get("end_date") == metric.get("period_end")):
                            affected.update({field, field + "_money"})
                if not affected:
                    continue
                for field in sorted(affected):
                    if field in metric:
                        metric[field] = None
                        changed.append(f"{collection}[{index}].{field}")
                    if field + "_status" in metric:
                        metric[field + "_status"] = "source_corrected_input"
                # These observations are calculated from this period's inputs.
                if "financial_tensions" in metric:
                    metric["financial_tensions"] = []
        if changed:
            content["_source_correction_fields"] = changed
        values.append(projected)
    return values


def minimum_content(body: Any, source_path: str) -> dict[str, Any]:
    """Freeze a locale-neutral, renderer-independent text/table representation.

    Values remain exact JSON scalars. This is presentation of an existing
    proof, never a new verification or a fallback for unproven raw output.
    """
    if isinstance(body, str):
        return {"kind": "text", "text": body}
    from .report_projection import format_report_semantic_value
    cells = []

    def visit(value: Any, field: str) -> None:
        leaf = field.rsplit(".", 1)[-1]
        # Fractional range pairs are one semantic value, not two unlabelled
        # decimal cells. Other arrays keep their original indexed positions.
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{field}.{key}" if field else str(key))
        elif isinstance(value, list) and format_report_semantic_value(value, leaf) is None:
            for index, child in enumerate(value):
                visit(child, f"{field}[{index}]")
        else:
            # Freeze the locale-neutral semantic spelling at commit time.
            # A read after a formatter failure must not call that formatter
            # again or expose a known ratio as an unlabelled raw float.
            cells.append({"field": field, "value": value,
                          "semantic_text": format_report_semantic_value(value, leaf)})

    visit(body, "" if isinstance(body, dict) else source_path.rsplit(".", 1)[-1])
    return {"kind": "table", "cells": cells}


def minimum_presentation(node: dict[str, Any], language: str) -> dict[str, Any]:
    """Format only frozen primitive cells; no complex ReportDocument dependency."""
    import json
    import re
    from .i18n import EN, ZH_HANT, normalize_language
    from .report_projection import format_report_semantic_value, report_field_label
    locale = normalize_language(language)
    minimum = node.get("minimal_content")
    if minimum is None:
        # Read-only compatibility for an old independently proven snapshot.
        minimum = minimum_content(node["body"], node["source_path"])
    if minimum["kind"] == "text":
        return {"kind": "text", "text": minimum["text"]}
    rows = [("Field", "Value") if locale == EN else ("項目", "內容") if locale == ZH_HANT else ("项目", "内容")]
    for cell in minimum["cells"]:
        field, value = cell["field"], cell["value"]
        parts = field.split(".")
        label = " · ".join(report_field_label(re.sub(r"\[\d+\]", "", part), locale)
            + "".join(re.findall(r"\[\d+\]", part)) for part in parts)
        formatted = (cell["semantic_text"] if "semantic_text" in cell else
                     format_report_semantic_value(value, re.sub(r"\[\d+\]", "", parts[-1])))
        if formatted is None:
            if isinstance(value, str):
                formatted = value
            elif isinstance(value, bool):
                formatted = ("Yes" if value else "No") if locale == EN else ("是" if value else "否")
            elif isinstance(value, (int, float)):
                # No precision loss for a minimum snapshot of a verified cell.
                formatted = str(value)
            else:
                formatted = json.dumps(value, ensure_ascii=False, allow_nan=False)
        rows.append((label, formatted))
    return {"kind": "table", "rows": tuple(rows)}


def trusted_snapshot_for_artifact(artifact: dict[str, Any], *, evidence_records: Any = (),
                                  retained_nodes: Any = None) -> dict[str, Any] | None:
    """Freeze independently projected text at the repository commit seam.

    Raw agent outputs and their `_validation` labels are deliberately absent.
    The same record is consumed by normal and emergency report reads.
    """
    content = artifact.get("content")
    if not isinstance(content, dict):
        return None
    kind = artifact.get("artifact_type")
    if kind == "stage-checkpoint":
        result = content.get("result", {})
        validation = result.get("validation", {})
        if validation.get("state") not in {"completed_verified", "completed_partial"}:
            return None
        value = validation.get("trusted_output", {})
        audit = value.get("_section_validation") if isinstance(value, dict) else None
        if audit is not None:
            # Stage projection validates the explicit `stage` envelope, then
            # exposes only its trusted body to downstream handlers.
            value = {"stage": value}
        # Final-stage output carries the section record in the independent
        # validation envelope rather than provider-controlled content.
        if audit is None:
            audit = validation.get("record", {}).get("verification", {}).get("section_validation")
        target = "stage:" + str(result.get("stage_id") or "")
        generation = str(result.get("input_digest") or "")
    elif kind == "research-report":
        value = content.get("report", {})
        audit = content.get("verification", {}).get("section_validation")
        target, generation = "report", str(artifact.get("artifact_id") or "")
    else:
        return None
    nodes = []
    references = {}
    dependency_records = {reference["evidence_id"]: reference for reference in evidence_records
                          if isinstance(reference, dict) and isinstance(reference.get("evidence_id"), str)}

    def dependency_closure(ids):
        pending, found = list(ids), set()
        while pending:
            key = pending.pop()
            if key in found:
                continue
            found.add(key)
            inputs = dependency_records.get(key, {}).get("input_evidence_ids", ())
            if isinstance(inputs, (tuple, list)):
                pending.extend(item for item in inputs if isinstance(item, str) and item in dependency_records)
        return sorted(found)
    for reference in evidence_records:
        if not isinstance(reference, dict) or not isinstance(reference.get("evidence_id"), str):
            continue
        url = str(reference.get("source_url") or reference.get("url") or "")
        try:
            parsed = urlsplit(url)
            safe = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            safe = False
        if safe:
            references[reference["evidence_id"]] = {"evidence_id": reference["evidence_id"], "source_url": url,
                "locator": str(reference.get("locator") or ""), "content_hash": str(reference.get("content_hash") or "")}
    for node in proven_content_nodes(value, audit):
        if node.get("body") in (None, "", [], {}):
            continue
        display_section = node["section_id"]
        if display_section == "stage":
            display_section = {"business-analyst": "business_model", "financial-analyst": "financial_quality",
                "source-industry-analysis": "industry_changes_and_value_allocation",
                "source-risk-analysis": "source_risk_review",
                "accounting-risk-analyst": "financial_quality", "growth-opportunities": "growth_opportunities",
                "counter-analysis": "counterarguments", "forecast-scenarios": "scenarios"}.get(
                    result.get("stage_id"), "unresolved_questions")
        sources = [references[key] for key in node.get("evidence_ids", ()) if key in references]
        dependencies = dependency_closure([*node.get("evidence_ids", ()), *node.get("depends_on", ())])
        if content.get("mode") == "progressive-local" and retained_nodes is not None:
            # A locally rebound node must match a currently committed proof.
            # Freeze that proof's links, not a later source with the same ID.
            original = next((item for item in retained_nodes
                if item.get("node_id") == node.get("node_id")
                and item.get("source_artifact_id") == node.get("origin_artifact_id")
                and item.get("generation") == node.get("origin_generation")
                and all(item.get(key) == node.get(key) for key in (
                    "body_sha256", "context_sha256", "evidence_ids", "role", "validator_version"))), None)
            if original is None:
                continue
            sources = original.get("sources", [])
            dependencies = original.get("dependency_evidence_ids", original.get("evidence_ids", []))
        nodes.append({**node, "sources": sources,
                      "dependency_evidence_ids": dependencies,
                      "minimal_content": minimum_content(node["body"], node["source_path"]),
                      "source_artifact_id": str(artifact.get("artifact_id") or ""),
                      "generation": generation, "display_section_id": display_section})
    if not nodes:
        return None
    payload = {"schema": TRUSTED_SNAPSHOT_VERSION, "run_id": artifact.get("run_id"),
               "target": target, "generation": generation, "nodes": nodes,
               "source_artifact_id": artifact.get("artifact_id"),
               "source_sha256": content_digest(content)}
    return {**payload, "snapshot_sha256": content_digest(payload)}


def read_trusted_snapshots(artifacts: Any, *, run_id: str) -> tuple[dict[str, Any], ...]:
    """Return current source-bound snapshots without rendering or writes."""
    from .research_section_validation import is_structural_content_path
    values = [row for row in artifacts if isinstance(row, dict) and row.get("run_id") == run_id]
    revoked = {tuple(item[key] for key in ("node_id", "source_artifact_id", "generation"))
               for record in read_content_invalidations(values, run_id=run_id)
               for item in record["affected_nodes"]}
    evidence_records = next((row.get("content", {}).get("evidence", []) for row in reversed(values)
        if row.get("run_id") == run_id and row.get("artifact_type") == "deterministic-financial-summary"
        and isinstance(row.get("content"), dict)), [])
    # Pre-snapshot artifacts with an independent node proof can be adapted
    # read-only. A corrupt existing snapshot is never silently regenerated.
    snapshotted_ids = {row.get("content", {}).get("source_artifact_id") for row in values
                       if row.get("artifact_type") == "trusted-content-snapshot"
                       and isinstance(row.get("content"), dict)}
    for artifact in tuple(values):
        if artifact.get("artifact_id") in snapshotted_ids:
            continue
        snapshot = trusted_snapshot_for_artifact(artifact, evidence_records=evidence_records)
        if snapshot is not None:
            values.append({"artifact_type": "trusted-content-snapshot", "run_id": artifact.get("run_id"),
                           "content": snapshot})
    by_id = {row.get("artifact_id"): row for row in values}
    heads = {}
    for row in values:
        kind, content = row.get("artifact_type"), row.get("content", {})
        if not isinstance(content, dict):
            continue
        if kind == "stage-checkpoint":
            result = content.get("result", {})
            heads["stage:" + str(result.get("stage_id") or "")] = row
        elif kind == "research-report":
            heads["report"] = row
    selected = {}
    for row in values:
        if row.get("artifact_type") != "trusted-content-snapshot" or row.get("run_id") != run_id:
            continue
        content = row.get("content", {})
        if not isinstance(content, dict) or content.get("schema") != TRUSTED_SNAPSHOT_VERSION:
            continue
        source = by_id.get(content.get("source_artifact_id"))
        head = heads.get(content.get("target"))
        if source is None or head is None:
            continue
        if source.get("artifact_id") != head.get("artifact_id"):
            # Select the latest VALID committed snapshot for the explicitly
            # unchanged input, not the latest attempt's unvalidated snapshot.
            # Legacy reports without input identity cannot authorize reuse.
            if source.get("artifact_type") == "stage-checkpoint":
                source_input = source["content"].get("result", {}).get("input_digest")
                head_input = head["content"].get("result", {}).get("input_digest")
            else:
                source_input = source["content"].get("publication_input_digest")
                head_input = head["content"].get("publication_input_digest")
            if not isinstance(source_input, str) or not source_input or source_input != head_input:
                continue
        payload = {key: child for key, child in content.items() if key != "snapshot_sha256"}
        try:
            if (content_digest(payload) != content.get("snapshot_sha256")
                    or content_digest(source.get("content")) != content.get("source_sha256")):
                continue
        except (TypeError, ValueError):
            continue
        selected.setdefault(content["target"], []).append(content)
    nodes = {}
    for target, snapshots in sorted(selected.items(), key=lambda item: item[0] != "report"):
        # An incomplete later attempt cannot erase independently committed
        # siblings. Every retained snapshot has already matched the current
        # explicit input above; changed inputs never borrow previous proofs.
        # Prefer the latest proof at a logical source position, while keeping
        # separately named list contexts distinct. This is not permission to
        # merge raw candidates or reuse content after source correction.
        positions = set()
        for snapshot in reversed(snapshots):
            for node in snapshot.get("nodes", ()):
                if not isinstance(node, dict):
                    continue
                if is_structural_content_path(str(node.get("source_path") or "")):
                    continue
                if tuple(node.get(key) for key in ("node_id", "source_artifact_id", "generation")) in revoked:
                    continue
                position = (node.get("display_section_id", node["section_id"]),
                            node["source_path"], node.get("context_sha256"))
                if position in positions:
                    continue
                positions.add(position)
                identity = (node.get("display_section_id", node["section_id"]), node["body_sha256"],
                            tuple(node.get("evidence_ids", ())), node.get("role"), node.get("context_sha256"))
                nodes.setdefault(identity, node)
    return _replay_saved_assertion_proofs(tuple(nodes.values()), values, evidence_records)


def _replay_saved_assertion_proofs(nodes, artifacts, evidence_records):
    """Read-only proof replay for old snapshots whose local gate lost results.

    Only host checkpoint records are a proof source. Exact text/source IDs,
    current source bytes and quote spans are checked again. Neither an old
    stage-wide success flag nor a model-supplied verdict can bless a node.
    """
    from .research_assertions import AssertionSession, VERSION, validate_response
    from .research_numeric_assertions import numeric_prose_supported
    from copy import deepcopy
    proofs = {}
    for row in artifacts:
        if row.get('artifact_type') != 'stage-checkpoint':
            continue
        content = row.get('content')
        result = content.get('result') if isinstance(content, dict) else None
        validation = result.get('validation') if isinstance(result, dict) else None
        record = validation.get('record') if isinstance(validation, dict) else None
        if not isinstance(record, dict):
            continue
        if record.get('assertion_protocol') != VERSION:
            continue
        for proof in record.get('assertion_results', ()):
            if (isinstance(proof, dict) and proof.get('protocol') == VERSION
                    and proof.get('state') in {'supported', 'contradicted'}):
                proofs[proof.get('assertion_id')] = proof
    if not proofs:
        return nodes
    sources = {item.get('evidence_id'): item for item in evidence_records if isinstance(item, dict)}
    result = []
    for original in nodes:
        node = original
        text = node.get('body')
        refs = node.get('evidence_ids', ())
        if (node.get('role') == 'analysis' and node.get('validation_state') == 'partial'
                and node.get('issue_code') in {None, '', 'CONTENT_PENDING_VERIFICATION', 'CONTENT_NUMERIC_PREMISE_UNVERIFIED'}
                and isinstance(text, str) and refs and all(key in sources for key in refs)):
            records = [sources[key] for key in refs]
            session = AssertionSession()
            session.classify({'kind': 'inference', 'text': text}, records)
            queries = list(session.queries.values())
            valid = validate_response({'structured_output_valid': True, 'results': [proofs[query['assertion_id']] for query in queries if query['assertion_id'] in proofs]}, queries,
                                      'historical-host-proof-replay') if queries else {}
            if (queries and all(valid.get(query['assertion_id'], {}).get('state') == 'supported'
                                for query in queries) and numeric_prose_supported(text, records)):
                node = deepcopy(node)
                judgments = [deepcopy(proofs[query['assertion_id']]) for query in queries]
                node.update(role='verified_analysis', validation_state='verified', issue_code='',
                            assertion_results=judgments, assertions_sha256=content_digest(judgments))
                identity = {key: node[key] for key in ('source_path', 'body_sha256', 'evidence_ids', 'role',
                    'validator_version', 'context_sha256', 'depends_on', 'assertions_sha256')}
                node['node_id'] = 'node:' + content_digest(identity)
        result.append(node)
    return tuple(result)


def coverage_content_view(artifact, artifacts, *, trusted_nodes=None):
    """Bind read-time obligations to the exact host proofs used for display.

    This is an ephemeral view, not a rewritten report or a stage-wide passed
    flag. Existing prose must match its own current report node. Only absent
    sections can use committed stage nodes, with every pending sibling kept.
    """
    from copy import deepcopy
    from .research_section_validation import content_at_path, SECTION_PROOF_VERSION
    content = artifact.get("content", {})
    report = deepcopy(content.get("report", content))
    verification = deepcopy(content.get("verification", {}))
    if not isinstance(report, dict) or not isinstance(verification, dict):
        return report, verification
    existing = list(proven_content_nodes(report, verification.get("section_validation")))
    nodes = (read_trusted_snapshots(artifacts, run_id=str(artifact.get("run_id") or ""))
             if trusted_nodes is None else trusted_nodes)
    # Recovered proofs replace only the same current report/body position.
    replacements = {}
    for node in nodes:
        if node.get("source_artifact_id") != artifact.get("artifact_id"):
            continue
        try:
            matches = content_digest(content_at_path(report, node["path"])) == node["body_sha256"]
        except (TypeError, ValueError):
            matches = False
        if matches:
            replacements[node["path"]] = node
    rebound = [deepcopy(replacements.get(node["path"], node)) for node in existing]
    absent = {node.get("display_section_id", node["section_id"]) for node in nodes
              if report.get(node.get("display_section_id", node["section_id"])) in (None, "", [], {})}
    for section in absent:
        report[section] = []
    for node in nodes:
        section = node.get("display_section_id", node["section_id"])
        if section not in absent:
            continue
        entries = report.setdefault(section, [])
        path = f"{section}[{len(entries)}].body"
        entry = {"body": deepcopy(node["body"]), "role": node["role"],
                 "evidence_ids": list(node.get("evidence_ids", ()))}
        labels = [item["text"] for item in node.get("context", ())
                  if item.get("field") in {"name", "title"} and isinstance(item.get("text"), str)]
        if labels:
            entry["name"] = labels[-1]
        entries.append(entry)
        rebound.append({**deepcopy(node), "path": path, "section_id": section})
    verification["section_validation"] = {"schema": SECTION_PROOF_VERSION, "nodes": rebound}
    return report, verification


def node_validation_record(node: dict[str, Any]) -> ValidationRecord:
    """Adapt a separately verified node, not an artifact-wide passed flag."""
    verified = node.get("validation_state") == "verified"
    return ValidationRecord(state="verified" if verified else "partial",
        source_artifact_id=str(node.get("source_artifact_id") or ""), validator="section-content-proof",
        passed=verified, node_id=node["node_id"], validator_version=node["validator_version"],
        body_sha256=node["body_sha256"], evidence_ids=tuple(node.get("evidence_ids", ())),
        generation=str(node.get("generation") or ""))


def trusted_node_is_substantive(node: dict[str, Any]) -> bool:
    """Emergency readability must not promote quotes/notices into analysis.

    Only call on nodes from read_trusted_snapshots. Structured minimum tables
    carry no typed MoneyFacts, just like the normal projection, and cannot be
    counted as a financial fact table merely because their cells are nonempty.
    """
    if not isinstance(node.get("body"), str) or not node["body"].strip():
        return False
    if node.get("role") == "assumption":
        return node.get("validation_state") == "labelled"
    return (node.get("role") in {"analysis", "verified_analysis"} and node.get("validation_state") == "verified"
            or node.get("role") == "analysis" and node.get("validation_state") == "partial"
            and node.get("validator_version") == SECTION_PROOF_VERSION
            and node.get("display_eligible") is True)


def assemble_trusted_report_payload(artifacts: Any, *, run_id: str,
                                    input_digest: str = "") -> dict[str, Any] | None:
    """Local assembly consumes proofs; it does not re-run claim verification.

    The source node and exact body survive a presentation-path change. This
    progressive revision is never a declaration that all research is complete.
    """
    from .research_coverage import CONTRACT_VERSION
    values = artifacts_for_trusted_input(artifacts, input_digest)
    nodes = read_trusted_snapshots(values, run_id=run_id)
    if not nodes:
        return None
    report: dict[str, Any] = {"claims": []}
    rebound = []
    for node in nodes:
        section = node.get("display_section_id", node["section_id"])
        entries = report.setdefault(section, [])
        path = f"{section}[{len(entries)}].body"
        entries.append({"body": node["body"], "role": node["role"], "evidence_ids": node.get("evidence_ids", [])})
        rebound.append({**node, "path": path, "section_id": section,
                        "origin_artifact_id": node["source_artifact_id"],
                        "origin_generation": node["generation"]})
    report["research_complete"] = False
    report["cross_section_synthesis_status"] = "progressive_local_assembly"
    stage_states = {row.get("content", {}).get("result", {}).get("stage_id"):
                    {"state": row["content"]["result"].get("validation", {}).get("state"),
                     "issues": row["content"]["result"].get("validation", {}).get("issues", [])}
                    for row in artifacts if isinstance(row, dict) and row.get("artifact_type") == "stage-checkpoint"
                    and isinstance(row.get("content"), dict) and isinstance(row["content"].get("result"), dict)}
    return {"mode": "progressive-local", "report": report, "stage_states": stage_states,
            "publication_input_digest": input_digest,
            "research_coverage_contract": CONTRACT_VERSION,
            "verification": {"passed": False, "section_validation": {"schema": SECTION_PROOF_VERSION,
                              "nodes": rebound}}, "readiness": {"state": "partial", "complete": False}}


def artifacts_for_trusted_input(artifacts: Any, input_digest: str) -> list[dict[str, Any]]:
    """A prior report from another captured input cannot shadow current stage nodes."""
    return [row for row in artifacts if not input_digest or row.get("artifact_type") != "research-report"
            or row.get("content", {}).get("publication_input_digest") == input_digest]


def content_role(value: Any, *, default: str = "unknown") -> str:
    role = value.strip().casefold() if isinstance(value, str) else ""
    return role if role in CONTENT_ROLES else default


def materiality(value: Any, *, default: str = "unknown") -> str:
    level = value.strip().casefold() if isinstance(value, str) else ""
    return level if level in MATERIALITY_VALUES else default


def validation_record_for_artifact(
    artifact: Any,
    *,
    artifact_type: str = "",
) -> ValidationRecord:
    """Read a validation state only from the source artifact's typed record."""
    if not isinstance(artifact, dict):
        return ValidationRecord()
    content = artifact.get("content")
    if not isinstance(content, dict):
        return ValidationRecord()

    validation: Any = None
    validator = ""
    if artifact_type in {"research-report", "deterministic-financial-summary"}:
        validation = content.get("verification")
        validator = "verification"
    else:
        validation = content.get("_validation")
        validator = "validation"
        if not isinstance(validation, dict):
            audit = content.get("_audit")
            validation = audit.get("verification") if isinstance(audit, dict) else None
            validator = "audit.verification"

    artifact_id = str(artifact.get("artifact_id") or "")
    if artifact_type not in {"deterministic-financial-summary", "deterministic-valuation"}:
        # Numeric/local section verification is represented by individual
        # committed nodes. A legacy global passed bit cannot authorize an
        # unrelated section; retain its body with explicitly unknown trust.
        return ValidationRecord(source_artifact_id=artifact_id)
    if isinstance(validation, dict) and isinstance(validation.get("passed"), bool):
        passed = validation["passed"]
        return ValidationRecord(
            state="verified" if passed else "partial",
            source_artifact_id=artifact_id,
            validator=validator,
            passed=passed,
        )

    if artifact_type == "deterministic-financial-summary" and str(
        content.get("verification_state") or ""
    ).upper() == "VERIFIED":
        return ValidationRecord(
            state="verified",
            source_artifact_id=artifact_id,
            validator="verification_state",
            passed=True,
        )

    if artifact_type == "deterministic-valuation" and content.get("status") == "ok":
        return ValidationRecord(
            state="verified",
            source_artifact_id=artifact_id,
            validator="deterministic-status",
            passed=True,
        )

    return ValidationRecord(source_artifact_id=artifact_id)


def section_content_metadata(
    raw_content: Any,
    *,
    validation: ValidationRecord,
    generated_placeholder: bool = False,
) -> tuple[str, tuple[str, ...], str, str]:
    """Return role, source IDs, materiality, and completeness conservatively.

    The normalizer creates localized prose when a required typed field is
    absent.  The caller passes that fact explicitly, so classification does
    not parse or blacklist any displayed sentence.
    """
    if generated_placeholder:
        role = "placeholder"
    else:
        explicit_role = raw_content.get("role") if isinstance(raw_content, dict) else None
        role = content_role(
            explicit_role,
            default="analysis" if explicit_role in (None, "") else "unknown",
        )
        if role == "verified_analysis" and validation.state != "verified":
            # A raw content label cannot manufacture validator evidence.
            role = "analysis"

    raw_sources = ()
    if isinstance(raw_content, dict):
        raw_sources = raw_content.get("source_ids", raw_content.get("evidence_ids", ()))
    source_ids = tuple(dict.fromkeys(
        str(item) for item in raw_sources
        if isinstance(item, (str, int)) and str(item).strip()
    )) if isinstance(raw_sources, (list, tuple)) else ()

    explicit_materiality = raw_content.get("materiality") if isinstance(raw_content, dict) else None
    if role in {"notice", "placeholder", "source", "unknown"}:
        level = "non_material"
    elif explicit_materiality in (None, ""):
        level = "material"
    else:
        level = materiality(explicit_materiality, default="unknown")
    completeness = "incomplete" if role == "placeholder" else "unknown"
    return role, source_ids, level, completeness
