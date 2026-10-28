"""Document-local semantic hints; amounts are reread, never patched by a model.

The optional visual Adapter can return a concept/label/row mapping instead of
a numeric fact. This module resolves that hint against original positioned
rows, reparses only their local statement context, and returns candidates to
the existing compiler. No alias is installed, and no model call lives here.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import re
import time

from .domain import EvidenceRef
from .financial_contracts import CandidateBatch, FactCandidate, STATEMENT_FOR, fact_row_evidence_ids
from .financial_taxonomy import (
    cost_row_concept, cost_row_is_non_amount, cost_row_mentions,
    cost_row_source_is_non_amount,
)

CELL_REPAIR_VERSION = "original-cell-mapping-v1"
_KEYS = {"concept", "label", "row_quote", "original_page"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value):
    # Reversible whitespace only: no OCR digit, sign or word correction.
    return re.sub(r"\s+", " ", value).strip()


def mapping_reference(mapping, filing, allowed_pages):
    """Capture an untrusted hint under the already approved page manifest."""
    if (not isinstance(mapping, dict) or set(mapping) != _KEYS
            or mapping.get("concept") not in STATEMENT_FOR
            or mapping["concept"] == "reported_roe"
            or type(mapping.get("original_page")) is not int
            or mapping["original_page"] not in allowed_pages
            or any(not isinstance(mapping.get(key), str) or not mapping[key].strip()
                   for key in ("label", "row_quote"))
            or len(mapping["label"]) > 300 or len(mapping["row_quote"]) > 4000
            or not re.fullmatch(r"[0-9a-f]{64}", filing.content_hash)):
        return None
    label, row_quote, concept = mapping["label"], mapping["row_quote"], mapping["concept"]
    if (
        (cost_row_mentions(label) and cost_row_is_non_amount(label))
        or cost_row_source_is_non_amount(row_quote)
    ):
        return None
    source_cost_concept = cost_row_concept(label) or cost_row_concept(row_quote)
    if source_cost_concept is not None and source_cost_concept != concept:
        return None
    body = {"schema": CELL_REPAIR_VERSION, "document_hash": filing.content_hash,
            "mapping": mapping}
    excerpt = _canonical(body)
    return EvidenceRef("cell-map:" + sha256(excerpt.encode()).hexdigest(), filing.document_id,
        filing.source_url, "Untrusted original-cell mapping / not a financial fact",
        f"page:{mapping['original_page']}", excerpt, filing.filed_at, filing.content_hash)


def _decode_reference(ref, filing):
    try:
        body = json.loads(ref.excerpt)
        mapping = body["mapping"]
        expected = mapping_reference(mapping, filing, {mapping["original_page"]})
        if (set(body) == {"schema", "document_hash", "mapping"}
                and body["schema"] == CELL_REPAIR_VERSION and expected == ref):
            return mapping
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _reread_worker(subject, filing, refs, original_rules):
    import pdfplumber
    from .financial_ingestion import (
        FinancialIngestionEngine, _manifest_for, _manifest_from_observed_pages,
        _labels_for_rules, _label_compact, _positioned_pdf_rows, _row_label_source_text,
    )
    from .financial_sources.document_units import discover_document_units
    from .financial_statutory_currency import bind_statutory_presentation

    diagnostics, mappings = [], []
    for ref in refs:
        mapping = _decode_reference(ref, filing)
        if mapping is None:
            diagnostics.append("CELL_REPAIR_MAPPING_INVALID")
        else:
            mappings.append((mapping, ref))
    if not mappings:
        return CandidateBatch(filing, diagnostics=tuple(diagnostics))
    # Hash and native reading are inside the same killable worker allowance.
    if FinancialIngestionEngine._file_sha256(filing.local_path) != filing.content_hash:
        return CandidateBatch(filing, diagnostics=("CELL_REPAIR_SOURCE_CHANGED",))
    candidates, evidence = [], []
    with pdfplumber.open(filing.local_path) as pdf:
        texts = tuple(page.extract_text() or "" for page in pdf.pages)
        manifest = _manifest_from_observed_pages(_manifest_for(filing), filing, texts)
        if not manifest.period_end or manifest.period_end != filing.period_end:
            return CandidateBatch(filing, diagnostics=("CELL_REPAIR_PERIOD_UNRESOLVED",))
        document_units = bind_statutory_presentation(discover_document_units(texts), texts, subject, filing)
        label_registry = _labels_for_rules(original_rules)
        selected_by_page = {}
        for mapping, ref in mappings:
            page = mapping["original_page"]
            if page > len(pdf.pages):
                diagnostics.append("CELL_REPAIR_PAGE_UNAVAILABLE")
                continue
            rows = _positioned_pdf_rows(pdf.pages[page - 1].extract_words(
                keep_blank_chars=False, use_text_flow=False) or [])
            matches = [row for row in rows if _text(row.text) == _text(mapping["row_quote"])]
            label = _label_compact(mapping["label"])
            if (len(matches) != 1 or not label
                    or label != _label_compact(_row_label_source_text(matches[0]))):
                diagnostics.append("CELL_REPAIR_ROW_NOT_UNIQUE")
                continue
            source_cost_concept = cost_row_concept(mapping["label"])
            if (
                (cost_row_mentions(mapping["label"])
                 and cost_row_is_non_amount(mapping["label"]))
                 or cost_row_source_is_non_amount(matches[0].text)
                or (source_cost_concept is not None and source_cost_concept != mapping["concept"])
            ):
                diagnostics.append("CELL_REPAIR_KNOWN_LABEL_CONTRADICTION")
                continue
            known = {concept for concept, aliases in label_registry.items()
                     if any(_label_compact(alias) == label for alias in aliases)}
            if known and mapping["concept"] not in known:
                diagnostics.append("CELL_REPAIR_KNOWN_LABEL_CONTRADICTION")
                continue
            selected_by_page.setdefault(page, []).append((mapping, ref, matches[0]))
        for page, entries in selected_by_page.items():
            by_label = {}
            for mapping, ref, row in entries:
                by_label.setdefault(_label_compact(mapping["label"]), set()).add(mapping["concept"])
            if any(len(concepts) != 1 for concepts in by_label.values()):
                diagnostics.append("CELL_REPAIR_MAPPING_CONFLICT")
            entries = [(mapping, ref, row) for mapping, ref, row in entries
                       if len(by_label[_label_compact(mapping["label"])]) == 1]
            aliases = dict(original_rules.taxonomy_aliases)
            for mapping, _ref, _row in entries:
                concept = mapping["concept"]
                aliases[concept] = tuple(dict.fromkeys((*aliases.get(concept, ()), mapping["label"])))
            rules = replace(original_rules, taxonomy_aliases=tuple(sorted(aliases.items())))
            engine = FinancialIngestionEngine(compatibility_rules=rules)
            # Include title/unit/column headers and adjacent continuation pages;
            # keep the full original document-unit declarations available.
            pages = frozenset(range(max(1, page - 3), min(len(pdf.pages), page + 1) + 1))
            facts, reread_refs = engine._parse_pdf_ast(filing.local_path, subject, filing, manifest,
                candidate_pages=pages, index_precomputed=True, document_units=document_units)
            by_id = {ref.evidence_id: ref for ref in reread_refs}
            for mapping, proposal, row in entries:
                matched = [fact for fact in facts
                    if fact.source_page == page and fact.source_bbox == row.bbox
                    and _text(fact.raw_text) == _text(row.text)
                    and fact.concept == mapping["concept"] and fact.end_date == filing.period_end
                    and fact.usage_status != "comparator"]
                if len(matched) != 1:
                    diagnostics.append("CELL_REPAIR_REREAD_UNRESOLVED")
                    continue
                fact = matched[0]
                proof_body = {"schema": CELL_REPAIR_VERSION, "mapping_id": proposal.evidence_id,
                    "document_hash": filing.content_hash, "page": page, "bbox": list(row.bbox),
                    "row_quote": row.text, "concept": fact.concept, "label": mapping["label"],
                    "fact_id": fact.fact_id}
                excerpt = _canonical(proof_body)
                proof = EvidenceRef("cell-reread:" + sha256(excerpt.encode()).hexdigest(), filing.document_id,
                    filing.source_url, "Host reread of original cell / compiler candidate",
                    f"page:{page}", excerpt, filing.filed_at, filing.content_hash, bbox=row.bbox)
                fact = replace(fact, classification_evidence_ids=(*fact.classification_evidence_ids, proof.evidence_id))
                candidates.append(FactCandidate(fact, tuple(by_id[key] for key in fact_row_evidence_ids(fact)
                    if key in by_id), CELL_REPAIR_VERSION))
                evidence.extend((*reread_refs, proposal, proof))
    return CandidateBatch(filing, tuple(candidates), tuple({ref.evidence_id: ref for ref in evidence}.values()),
        tuple(dict.fromkeys(diagnostics)))


def reread_cell_mappings(subject, filing, refs, *, rules=None, deadline, cancel_check=None):
    """One bounded local attempt; its output still has no admission authority."""
    from .financial_compatibility import FinancialRulesSnapshot
    from .vision_financials import _run_pdfium_isolated
    mappings = tuple(ref for ref in refs if ref.evidence_id.startswith("cell-map:"))
    if not mappings:
        return CandidateBatch(filing)
    if len(mappings) > 200:
        return CandidateBatch(filing, diagnostics=("CELL_REPAIR_MAPPING_LIMIT",))
    if not filing.local_path or filing.company_cik not in {subject.cik, subject.security_id, subject.issuer_id}:
        return CandidateBatch(filing, diagnostics=("CELL_REPAIR_SOURCE_UNAVAILABLE",))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return CandidateBatch(filing, diagnostics=("CELL_REPAIR_TIMEOUT",))
    result, error = _run_pdfium_isolated(_reread_worker, subject, filing, mappings,
        rules or FinancialRulesSnapshot.empty(), timeout_seconds=remaining, cancel_check=cancel_check)
    if error or not isinstance(result, CandidateBatch):
        code = {"PDFIUM_WORKER_CANCELLED": "VISION_CANCELLED", "PDFIUM_WORKER_TIMEOUT": "CELL_REPAIR_TIMEOUT"}.get(error,
            "CELL_REPAIR_LOCAL_UNAVAILABLE")
        return CandidateBatch(filing, diagnostics=(code,))
    return result


def cell_repair_issue(fact, row, evidence):
    """Recheck the host's document-local mapping proof on cache/compiler replay."""
    ids = [key for key in fact.classification_evidence_ids if key.startswith("cell-reread:")]
    if not ids:
        return None
    try:
        if len(ids) != 1 or row is None:
            raise ValueError
        proof = evidence[ids[0]]
        body = json.loads(proof.excerpt)
        proposal = evidence[body["mapping_id"]]
        mapping = json.loads(proposal.excerpt)["mapping"]
        if (body["schema"] != CELL_REPAIR_VERSION
                or set(body) != {"schema", "mapping_id", "document_hash", "page", "bbox", "row_quote", "concept", "label", "fact_id"}
                or set(mapping) != _KEYS
                or "cell-map:" + sha256(proposal.excerpt.encode()).hexdigest() != proposal.evidence_id
                or json.loads(proposal.excerpt)["schema"] != CELL_REPAIR_VERSION
                or json.loads(proposal.excerpt)["document_hash"] != row.content_hash
                or "cell-reread:" + sha256(_canonical(body).encode()).hexdigest() != proof.evidence_id
                or body["document_hash"] != row.content_hash or body["page"] != fact.source_page
                or tuple(body["bbox"]) != row.bbox or row.bbox != fact.source_bbox
                or body["row_quote"] != row.excerpt or row.excerpt != fact.raw_text
                or body["concept"] != fact.concept or body["fact_id"] != fact.fact_id
                or _text(body["label"]) != _text(fact.reported_concept) or mapping["concept"] != fact.concept
                or _text(mapping["row_quote"]) != _text(row.excerpt)
                or mapping["label"] != body["label"] or mapping["original_page"] != fact.source_page
                or (cost_row_mentions(mapping["label"])
                    and cost_row_is_non_amount(mapping["label"]))
                or cost_row_source_is_non_amount(row.excerpt)
                or (cost_row_concept(mapping["label"]) is not None
                    and cost_row_concept(mapping["label"]) != fact.concept)
                or any(ref.document_id != row.document_id or ref.content_hash != row.content_hash
                       or ref.source_url != row.source_url or ref.locator != row.locator for ref in (proof, proposal))):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        return "cell_repair_source_binding_mismatch"
    return None


def unconsumed_mappings(facts, refs):
    """Do not reopen an already host-reread cell after durable validation."""
    evidence = {ref.evidence_id: ref for ref in refs}
    consumed = set()
    for fact in facts:
        rows = [evidence[key] for key in fact_row_evidence_ids(fact) if key in evidence]
        if len(rows) != 1 or cell_repair_issue(fact, rows[0], evidence) is not None:
            continue
        for key in fact.classification_evidence_ids:
            if key.startswith("cell-reread:"):
                try:
                    consumed.add(json.loads(evidence[key].excerpt)["mapping_id"])
                except (KeyError, ValueError, TypeError):
                    pass
    return tuple(ref for ref in refs if ref.evidence_id not in consumed)
