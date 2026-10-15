"""Evidence-bound section projection, independent of transport and storage.

The caller supplies the same claim validator used by its public research path.
No global verified-claim count can grant trust to unrelated prose. Rejected raw
content remains in the caller's candidate journal, never in this projection.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence
from .research_claim_dedup import deduplicate_claims
from .research_assertions import current_assertions, independent_sentences


SECTION_PROOF_VERSION = "evidence-bound-sections-v9-analysis-presentation"
_ASSERTION_PROOF_VERSIONS = {SECTION_PROOF_VERSION, "evidence-bound-sections-v8-assertions"}
_READABLE_PROOF_VERSIONS = {*_ASSERTION_PROOF_VERSIONS, "evidence-bound-sections-v7-source-dependencies", "evidence-bound-sections-v6-role-quantities"}
_VERIFIED = frozenset({"numeric_verified", "text_supported"})
_REFERENCES = frozenset({"evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids",
                         "supporting_evidence", "contradicting_evidence"})
_CLAIM_REFERENCES = frozenset({"claim_ids", "input_claim_ids"})
_LABELS = frozenset({"name", "title", "label", "kind", "type", "confidence", "severity", "role",
                     "applicability", "method"})
_SKIP = frozenset({"structured_output_valid", "trusted_channels"})
_NONFACTUAL = frozenset({"assumption", "forecast", "scenario", "risk", "opinion", "question", "unresolved", "method", "methodology"})
_FACTUAL_FIELDS = frozenset({"historical_facts", "reported_facts", "actuals", "source_facts"})
_HISTORICAL = re.compile(r"\b(?:was|were|reported|recorded|achieved|actual|historical)\b|实际|實際|已实现|已實現|历史|歷史", re.I)
_REPORTED_NUMBER = re.compile(r"[0-9０-９]")
_PERIOD_LABEL = re.compile(
    r"\bFY\s*(?:19|20)\d{2}\b|(?:19|20)\d{2}\s*年(?:度)?|"
    r"\b(?:19|20)\d{2}(?:\s*(?:and|or|to|-)\s*(?:19|20)\d{2})?\s+"
    r"(?:annual\s+(?:reports?|statements?)|fiscal\s+years?|reports?|statements?)\b",
    re.I,
)
_HYPOTHETICAL = re.compile(r"\b(?:assume|assuming|forecast|projected|hypothetical|would|could|future|may reach|might reach)\b|假设|假設|预测|預測|预计|預計|未来|未來|若|如果", re.I)
_ABSENT = object()


def _labelled_nonfactual_text(text: str, kind: str) -> bool:
    """An unrelated hypothetical clause cannot bless a historical quantity."""
    if not text:
        return False
    # Keep decimal points and grouped amounts intact. Each assertion-bearing
    # sentence/clause must identify its own hypothetical character.
    if kind in {"question", "unresolved", "method", "methodology"}:
        # Explicit period labels are the subjects of research questions or
        # instructions, not reported amounts. Other digits still need their
        # own evidence or hypothetical qualifier, even under these roles.
        text = _PERIOD_LABEL.sub("", text)
        intent = (r"^\s*(?:what|which|how|whether|can|could|is|are|do|does|did)\b|^\s*(?:是否|如何|哪些|什么|甚麼|待核实|待核實|需要.{0,8}(?:确认|確認|核实|核實))"
            if kind in {"question", "unresolved"} else
            r"^\s*(?:(?:the|our|this)\s+(?:analysis|method(?:ology)?)\s+)?(?:compar\w*|calculat\w*|analy[sz]\w*|assess\w*|verif\w*|review\w*)\b|^\s*(?:比较|比較|计算|計算|核对|核對|方法|评估|評估|分析)")
        # Research about reported results is not itself a historical assertion.
        # Every independent clause must be procedural/interrogative, however;
        # a question cannot grant trust to an attached factual statement.
        clauses = re.split(r"[。；;!?！？]|(?<!\d)\.(?!\d)", text)
        return all(re.search(intent, clause, re.I) and not _REPORTED_NUMBER.search(clause)
            for clause in clauses if clause.strip())
    if _HISTORICAL.search(text):
        return False
    clauses = re.split(r"[。；;!?！？]|(?<!\d)[,，]|(?<!\d)\.(?!\d)", text)
    return all(not _REPORTED_NUMBER.search(clause) or _HYPOTHETICAL.search(clause) for clause in clauses)


def content_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def content_at_path(value: Any, path: str) -> Any:
    """Read the projector's bounded field/list path without evaluating code."""
    if not isinstance(path, str) or not re.fullmatch(r"[^.\[\]]+(?:\[\d+\]|\.[^.\[\]]+)*", path):
        return _ABSENT
    try:
        for field, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path):
            value = value[field] if field else value[int(index)]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return _ABSENT


def proven_content_nodes(report: Mapping[str, Any], audit: Any) -> tuple[dict[str, Any], ...]:
    """Consume an independent validator record, never a model's passed flag.

    The caller supplies the audit separately from the candidate. Each body is
    bound individually, so a changed/rejected sibling cannot revoke this proof.
    """
    if not isinstance(audit, Mapping) or audit.get("schema") not in _READABLE_PROOF_VERSIONS:
        return ()
    result = []
    for node in audit.get("nodes", ()):
        if not isinstance(node, dict) or node.get("validator_version") not in _READABLE_PROOF_VERSIONS:
            continue
        identity = {key: node.get(key) for key in
                    ("source_path", "body_sha256", "evidence_ids", "role", "validator_version", "context_sha256")}
        if node["validator_version"] in _ASSERTION_PROOF_VERSIONS | {"evidence-bound-sections-v7-source-dependencies"}:
            dependencies = node.get("depends_on")
            if (not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies)
                    or dependencies != sorted(set(dependencies))):
                continue
            identity["depends_on"] = dependencies
        if node["validator_version"] in _ASSERTION_PROOF_VERSIONS:
            assertions = node.get("assertion_results")
            try:
                assertions_valid = isinstance(assertions, list) and content_digest(assertions) == node.get("assertions_sha256")
            except (TypeError, ValueError):
                assertions_valid = False
            if not assertions_valid:
                continue
            identity["assertions_sha256"] = node["assertions_sha256"]
        try:
            identity_valid = node.get("node_id") == "node:" + content_digest(identity)
        except (TypeError, ValueError):
            identity_valid = False
        if not identity_valid:
            continue
        value = content_at_path(report, str(node.get("path") or ""))
        if value is _ABSENT:
            continue
        try:
            if (content_digest(value) != node.get("body_sha256")
                    or content_digest(node.get("body")) != node.get("body_sha256")
                    or content_digest(node.get("context", [])) != node.get("context_sha256")):
                continue
        except (TypeError, ValueError):
            continue
        result.append(deepcopy(node))
    return tuple(result)


def rewrite_claim_references(value: Any, aliases: Mapping[str, str]) -> Any:
    """Apply proven ID aliases only to declared reference fields, not prose."""
    if isinstance(value, dict):
        return {key: (list(dict.fromkeys(aliases.get(item, item) for item in child))
                      if key in _CLAIM_REFERENCES and isinstance(child, list)
                      and all(isinstance(item, str) for item in child)
                      else rewrite_claim_references(child, aliases)) for key, child in value.items()}
    if isinstance(value, list):
        return [rewrite_claim_references(item, aliases) for item in value]
    return deepcopy(value)


def project_supported_sections(
    report: Mapping[str, Any], section_keys: Sequence[str],
    verify_claims: Callable[[list[dict[str, Any]]], Mapping[str, Any]],
    prose_supported: Callable[[str, list[str]], bool],
    available_evidence: set[str],
    *, computed_fields: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return independently supported content and a source-bound audit.

    Explicit local claims/references bind descendants. Legacy top-level claims
    bind a section only via ``section``/``section_id`` or exact claim text, not
    merely because another section somewhere in the report was verified.
    """
    rejected: list[dict[str, str]] = []
    kept: list[str] = []
    collected: list[dict[str, Any]] = []
    assumptions: list[str] = []
    computed: list[str] = []
    atoms: list[dict[str, Any]] = []
    verified_indices: set[int] = set()
    numeric_verified_indices: set[int] = set()
    checked_by_path: dict[str, list[dict[str, Any]]] = {}
    raw_claim_identities: dict[str, set[str]] = {}
    referenced_claim_ids: set[str] = set()
    try:
        encoded = json.dumps(report, sort_keys=True, ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return {"claims": []}, {"schema": SECTION_PROOF_VERSION, "input_sha256": None,
            "kept_paths": [], "quarantined_paths": [{"path": "$", "reason": "invalid_json_value"}],
            "verified_claim_count": 0}

    def reject(path: str, reason: str) -> None:
        rejected.append({"path": path, "reason": reason})

    def accept(path: str, value: Any, refs: list[str], claims: list[dict], role: str) -> None:
        # Freeze presentation ancestry as non-substantive metadata. Flattening
        # trusted leaves must not lose opportunity/scenario names or combine
        # different list items. Titles never become evidence for this body.
        context = []
        parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
        parent_path = ""
        for part in parts[:-1]:
            parent_path += part if part.startswith("[") or not parent_path else "." + part
            parent = content_at_path(report, parent_path)
            if isinstance(parent, Mapping):
                for key in ("name", "title", "label"):
                    label = parent.get(key)
                    if isinstance(label, str) and label.strip():
                        context.append({"path": parent_path, "field": key, "text": label})
        dependencies = set(refs)
        for claim in claims:
            inputs = claim.get("inputs")
            if isinstance(inputs, dict):
                dependencies.update(item for item in inputs.values()
                                    if isinstance(item, str) and item in available_evidence)
        identity = {"source_path": path, "body_sha256": content_digest(value),
                    "evidence_ids": sorted(set(refs)), "role": role,
                    "validator_version": SECTION_PROOF_VERSION, "context_sha256": content_digest(context),
                    "depends_on": sorted(dependencies)}
        assertions = list(current_assertions(str(value), refs, include_unreviewed=role == "analysis"))
        if not assertions and isinstance(value, str):
            assertions = [item for sentence in independent_sentences(value)
                          for item in current_assertions(sentence, refs, include_unreviewed=role == "analysis")]
        if role == "verified_analysis" and assertions and all(item.get("role") == "source_quote" for item in assertions):
            role = "source_quote"
            identity["role"] = role
        identity["assertions_sha256"] = content_digest(assertions)
        atoms.append({**identity, "node_id": "node:" + content_digest(identity),
                      "section_id": path.split(".")[0].split("[")[0],
                      "body": deepcopy(value), "context": context, "claim_ids": sorted({claim["claim_id"] for claim in claims
                          if isinstance(claim.get("claim_id"), str)}),
                      "assertion_results": assertions,
                      "validation_state": "partial" if role == "analysis" else "verified" if role != "assumption" else "labelled"})

    def checked_claims(value: Any, path: str) -> list[dict[str, Any]]:
        if path in checked_by_path:
            return checked_by_path[path]
        if not isinstance(value, list):
            reject(path, "claims_not_array")
            checked_by_path[path] = []
            return []
        for claim in value:
            if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
                continue
            # An invalid competing claim with the same ID also makes an ID
            # reference ambiguous. A successful sibling cannot bless it.
            identity = {key: child for key, child in claim.items()
                        if key not in {"claim_id", "confidence", "section", "section_id", "section_ids"}}
            raw_claim_identities.setdefault(claim["claim_id"], set()).add(
                json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False))
        check = verify_claims(value)
        normalized = check.get("normalized_claims", value)
        states = {item.get("index"): item.get("state")
                  for item in check.get("claim_verifications", []) if isinstance(item, dict)}
        valid = []
        for index, claim in enumerate(normalized):
            if isinstance(claim, dict) and states.get(index) in _VERIFIED:
                valid.append(deepcopy(claim))
                verified_indices.add(len(collected) + len(valid) - 1)
                if states.get(index) == "numeric_verified":
                    numeric_verified_indices.add(len(collected) + len(valid) - 1)
            elif (isinstance(claim, dict) and states.get(index) == "reference_only"
                  and str(claim.get("kind", "")).casefold() in _NONFACTUAL):
                valid.append(deepcopy(claim))
                assumptions.append(f"{path}[{index}]")
            else:
                reject(f"{path}[{index}]", "claim_not_verified")
        collected.extend(valid)
        checked_by_path[path] = valid
        return valid

    top_claims = checked_claims(report.get("claims", []), "claims")

    def register(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            if "claims" in value:
                checked_claims(value["claims"], path + ".claims")
            for key, child in value.items():
                if key in _CLAIM_REFERENCES and isinstance(child, list):
                    referenced_claim_ids.update(item for item in child if isinstance(item, str) and item.strip())
                elif key != "claims" and not str(key).startswith("_"):
                    register(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                register(child, f"{path}[{index}]")

    # Verification and ID registration precede rendering-order traversal.
    # Forward references use the same independently checked claims as local
    # references; no second provider call or blanket section trust is added.
    for key in section_keys:
        if key != "claims" and key in report:
            register(report[key], key)
    referenceable = {claim["claim_id"]: claim for index, claim in enumerate(collected)
                     if index in verified_indices and isinstance(claim.get("claim_id"), str)
                     and len(raw_claim_identities.get(claim["claim_id"], ())) == 1}

    def references(claims: list[dict[str, Any]]) -> list[str]:
        return list(dict.fromkeys(ref for claim in claims
                                 for ref in claim.get("evidence_ids", []) if isinstance(ref, str)))

    def source_references(claims):
        # Provenance is not factual support. Unreviewed local claims may locate
        # material for an independently checked, labelled analysis paragraph;
        # they remain absent from accepted claims and all calculators.
        return list(dict.fromkeys(ref for claim in claims if isinstance(claim, dict)
            for key in _REFERENCES
            for ref in (claim.get(key) if isinstance(claim.get(key), (list, tuple)) else ())
            if isinstance(ref, str) and ref in available_evidence))

    def visit(value: Any, path: str, inherited: list[str], owned: list[dict[str, Any]],
              nonfactual: str = "", declared_assumption: bool = False, factual: bool = False) -> Any:
        # This map is supplied by the deterministic engine, never by provider
        # metadata. Match the exact value/path; no tolerance or blanket trust
        # for the entire valuation section or its model-written analysis.
        if computed_fields is not None and path in computed_fields:
            actual = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
            expected = json.dumps(computed_fields[path], sort_keys=True, ensure_ascii=False, allow_nan=False)
            if actual == expected:
                computed.append(path)
                accept(path, value, [], [], "fact_table")
                return deepcopy(value)
        if isinstance(value, Mapping):
            declared_role = value.get("role")
            role_aliases = {"numeric_fact": "fact", "source_summary": "inference",
                            "analysis_inference": "analysis", "hypothesis": "assumption"}
            if isinstance(declared_role, str):
                declared_role = role_aliases.get(declared_role.casefold(), declared_role.casefold())
            if declared_role is not None and (not isinstance(declared_role, str)
                    or declared_role.casefold() not in _NONFACTUAL | {
                        "fact", "calculation", "inference", "analysis", "verified_analysis", "source_quote", "fact_table"}):
                reject(path + ".role", "content_role_unclassified")
                # The unknown role governs this object's own assertions, not
                # a separately sourced child. Do not inherit its locators,
                # claims or role into children, nor grant its scalar body
                # trust by silently dropping the unclassified role.
                independent = {}
                for key, child in value.items():
                    if key in _LABELS | _REFERENCES | _CLAIM_REFERENCES | _SKIP | {"claims"} or str(key).startswith("_"):
                        continue
                    if isinstance(child, Mapping):
                        item = visit(child, f"{path}.{key}", [], [])
                    elif isinstance(child, list):
                        items = [visit(nested, f"{path}.{key}[{index}]", [], [])
                                 for index, nested in enumerate(child) if isinstance(nested, Mapping)]
                        kept_items = [item for item in items if item is not _ABSENT]
                        item = kept_items if kept_items else _ABSENT
                    else:
                        continue
                    if item is not _ABSENT:
                        independent[key] = item
                return independent if independent else _ABSENT
            kind = str(value.get("kind") or declared_role or "").casefold()
            factual = factual or kind in {"fact", "numeric_fact", "calculation", "fact_table"}
            if kind:
                nonfactual = kind if kind in _NONFACTUAL else ""
            local_claims = checked_claims(value["claims"], path + ".claims") if "claims" in value else owned
            refs = references(local_claims) or list(inherited)
            if isinstance(value.get("claims"), list):
                refs = list(dict.fromkeys([*refs, *source_references(value["claims"])]))
            valid_evidence_references = {}
            for key in _REFERENCES:
                if key not in value:
                    continue
                declared = value[key]
                if not isinstance(declared, list):
                    reject(path + "." + key, "unknown_or_invalid_evidence")
                    continue
                valid = list(dict.fromkeys(ref for ref in declared
                    if isinstance(ref, str) and ref in available_evidence))
                if len(valid) != len(declared):
                    reject(path + "." + key, "unknown_or_invalid_evidence")
                # A broken parent locator is not a verdict about independently
                # sourced children. Only valid locators propagate; every leaf
                # still has to prove its own assertion against the remainder.
                valid_evidence_references[key] = valid
                refs = list(dict.fromkeys([*refs, *valid]))
            valid_claim_references = {}
            for key in _CLAIM_REFERENCES:
                if key not in value:
                    continue
                declared = value[key]
                if (not isinstance(declared, list) or any(not isinstance(item, str)
                        or item not in referenceable for item in declared)):
                    reject(path + "." + key, "unknown_or_invalid_claim_reference")
                    continue
                valid_claim_references[key] = deepcopy(declared)
                refs = list(dict.fromkeys([*refs, *references([referenceable[item] for item in declared])]))
            projected = {}
            substantive = False
            has_computed = False
            for key, child in value.items():
                if str(key).startswith("_") or key in _SKIP:
                    continue
                if key == "claims":
                    if local_claims:
                        projected[key] = local_claims
                    continue
                if key in _CLAIM_REFERENCES:
                    if key in valid_claim_references:
                        projected[key] = valid_claim_references[key]
                    continue
                if key in _REFERENCES:
                    if valid_evidence_references.get(key):
                        projected[key] = valid_evidence_references[key]
                    continue
                if key in _LABELS:
                    projected[key] = deepcopy(child)
                    continue
                item = visit(child, f"{path}.{key}", refs, local_claims,
                             nonfactual if key not in _FACTUAL_FIELDS else "",
                             (declared_assumption or key == "assumptions")
                             and nonfactual in {"assumption", "forecast", "scenario"}
                             and key not in _FACTUAL_FIELDS, factual or key in _FACTUAL_FIELDS)
                if item is not _ABSENT:
                    projected[key] = item
                    if f"{path}.{key}" in computed:
                        has_computed = True
                    else:
                        substantive = True
            if not substantive and not has_computed:
                reject(path, "no_supported_analysis")
                return _ABSENT
            return projected
        if isinstance(value, list):
            projected = []
            for index, child in enumerate(value):
                item = visit(child, f"{path}[{index}]", inherited, owned, nonfactual, declared_assumption, factual)
                if item is not _ABSENT:
                    projected.append(item)
            return projected if projected else _ABSENT
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if declared_assumption and isinstance(value, (int, float)):
                # A numeric assumption is explicitly typed by its containing
                # forecast/scenario and `assumptions` field. It is labelled,
                # never admitted as a reported fact or engine calculation.
                assumptions.append(path)
                accept(path, value, inherited, owned, "assumption")
                return deepcopy(value)
            if nonfactual and _labelled_nonfactual_text(text, nonfactual):
                assumptions.append(path)
                role = "question" if nonfactual in {"question", "unresolved"} else "methodology" if nonfactual in {"method", "methodology"} else "assumption"
                accept(path, value, inherited, owned, role)
                return deepcopy(value)
            refs = list(inherited)
            # Inline locators in legacy/string-shaped sections are host-bound
            # only if their exact identity exists in this material snapshot.
            # Unknown IDs are never guessed, fetched, or treated as support.
            inline = re.findall(r"(?:fact|filing|disclosure|table|observation):[A-Za-z0-9:._-]+", text)
            refs = list(dict.fromkeys([*refs, *(ref.rstrip('.,') for ref in inline
                if ref.rstrip('.,') in available_evidence)]))
            if not refs:
                exact = [claim for claim in top_claims if text == str(
                    claim.get("text") or claim.get("conclusion") or claim.get("argument") or "").strip()]
                refs = references(exact)
                if not refs:
                    # Collect downstream prose questions in the SAME semantic
                    # batch even while their exact claim dependency is pending.
                    # Raw references never grant trust: this call's result is
                    # ignored; only independently checked claims above can
                    # supply refs for admission on the resolved pass.
                    raw_claims = report.get("claims", [])
                    pending = [claim for claim in raw_claims if isinstance(claim, dict) and text == str(
                        claim.get("text") or claim.get("conclusion") or claim.get("argument") or "").strip()] if isinstance(raw_claims, list) else []
                    pending_refs = tuple(ref for ref in references(pending) if ref in available_evidence)
                    if pending_refs:
                        prose_supported(text, pending_refs)
            support = prose_supported(text, refs) if text and refs else False
            # Explicit historical/fact roles never inherit the new analysis
            # presentation policy. Their proof requirement remains unchanged.
            # Unreviewed historical summaries stay pending. Explicit analysis
            # fields identify the model's reasoning, not a claimed source fact.
            analysis_field = path.rsplit(".", 1)[-1] in {"analysis", "rationale", "reasoning", "counterargument",
                "thesis", "description", "summary", "outlook", "competitive_assessment", "investment_logic", "mechanism"}
            labelled_reasoning = bool(_HYPOTHETICAL.search(text) or re.search(
                r"\b(?:because|therefore|suggests?|risk|implies?|may|might)\b|意味着|意味著|风险|風險|因此|有助于|有助於", text, re.I))
            can_present = not factual and (analysis_field or labelled_reasoning)
            if support and (support != "source_bound_analysis" or can_present):
                kept.append(path)
                accept(path, value, refs, owned, "analysis" if support == "source_bound_analysis" else "verified_analysis")
                return deepcopy(value)
            if isinstance(value, str) and text and refs:
                parts = independent_sentences(text)
                if len(parts) > 1:
                    retained = []
                    partial = False
                    for index, sentence in enumerate(parts):
                        supported = prose_supported(sentence, refs)
                        if supported and (supported != "source_bound_analysis" or can_present):
                            retained.append(sentence)
                            partial = partial or supported == "source_bound_analysis"
                        else:
                            reject(f"{path}#sentence[{index}]", "assertion_not_bound_to_supporting_evidence")
                    if retained:
                        # Join only complete, independently checked sentences;
                        # the original unsplit candidate remains in the journal.
                        body = "\n".join(retained)
                        kept.append(path)
                        accept(path, body, refs, owned, "analysis" if partial else "verified_analysis")
                        return body
            reject(path, "analysis_not_bound_to_supporting_evidence")
            return _ABSENT
        reject(path, "not_analysis")
        return _ABSENT

    safe = {}
    for key in section_keys:
        if key == "claims" or key not in report:
            continue
        owned = [claim for claim in top_claims
                 if claim.get("section") == key or claim.get("section_id") == key
                 or isinstance(claim.get("section_ids"), list) and key in claim["section_ids"]]
        nonfactual = "question" if key == "unresolved_questions" else "methodology" if key in {"evidence_and_methodology", "methodology"} else ""
        item = visit(report[key], key, references(owned), owned, nonfactual)
        if item is not _ABSENT:
            safe[key] = item
    claims, deduplication = deduplicate_claims(
        collected, verified_numeric=True, numeric_verified_indices=numeric_verified_indices,
        referenced_claim_ids=referenced_claim_ids)
    safe["claims"] = claims
    safe = rewrite_claim_references(safe, deduplication["claim_id_aliases"])
    # Rejected list siblings change projected indices. Bind the retained atom
    # to its final path while retaining the original locator and stable ID.
    available_atoms = list(atoms)
    nodes = []

    def bind(value: Any, path: str) -> None:
        for index, node in enumerate(available_atoms):
            if (node["section_id"] == path.split(".")[0].split("[")[0]
                    and re.sub(r"\[\d+\]", "[]", node["source_path"]) == re.sub(r"\[\d+\]", "[]", path)
                    and node["body_sha256"] == content_digest(value)):
                accepted = available_atoms.pop(index)
                accepted["claim_ids"] = sorted({deduplication["claim_id_aliases"].get(item, item)
                                                for item in accepted["claim_ids"]})
                nodes.append({**accepted, "path": path})
                return
        if isinstance(value, dict):
            for key, child in value.items():
                if key != "claims":
                    bind(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                bind(child, f"{path}[{index}]")

    for section in section_keys:
        if section != "claims" and section in safe:
            bind(safe[section], section)
    removed_indices = {index for merge in deduplication["merges"] for index in merge["removed_indices"]}
    return safe, {
        "schema": SECTION_PROOF_VERSION,
        "input_sha256": hashlib.sha256(encoded).hexdigest(),
        "kept_paths": kept, "quarantined_paths": rejected,
        "assumption_paths": assumptions,
        "computed_paths": computed,
        "nodes": nodes,
        "content_flow": {"schema": "research-content-flow-v1",
            "accepted_nodes": len(nodes),
            "accepted_by_role": {role: sum(item["role"] == role for item in nodes)
                                 for role in sorted({item["role"] for item in nodes})},
            "unresolved_paths": len(rejected),
            "reductions": [{"path": item["path"], "code": item["reason"]} for item in rejected]},
        "verified_claim_count": len(verified_indices - removed_indices),
        "claim_deduplication": deduplication,
    }
