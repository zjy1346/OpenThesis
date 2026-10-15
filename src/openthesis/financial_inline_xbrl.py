"""Read frozen single-document SEC Inline XBRL without network or admission.

The bounded native lane covers registered numeric semantics. It is not an
Arelle replacement or a full taxonomy/DTS validator. Unsupported extensions,
transforms, periods and duplicate IDs produce auditable diagnostics, not guesses.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from copy import deepcopy

from .domain import Company, EvidenceRef, FilingDocument, FinancialDimension, FinancialFact
from .financial_contracts import CandidateBatch, FactCandidate, QuantityKind, SourceAmount
from .financial_xbrl_presentation import (
    PRESENTATION_VERSION, presentation_disclosures, presentation_role,
)
from .financial_xbrl_semantics import (
    IX, XBRLI, XSI, INLINE_PARSER_VERSION, context_identity, expanded_qname,
    numeric_token, standard_concept, unit_identity,
)

MAX_INLINE_BYTES = 32 * 1024 * 1024
MAX_INLINE_ELEMENTS = 250_000
MAX_INLINE_DEPTH = 256
MAX_PROOF_BYTES = 512_000


def _proof_element(value: object) -> ET.Element:
    """Bound every revalidated subtree, including the document focus proof."""
    if (not isinstance(value, str) or len(value.encode("utf-8")) > 128_000
            or "\0" in value or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", value, re.I)):
        raise ValueError("INLINE_PROOF_XML_INVALID")
    root = ET.fromstring(value)
    stack = [(root, 1)]
    count = 0
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > 5_000 or depth > MAX_INLINE_DEPTH:
            raise ValueError("INLINE_PROOF_STRUCTURE_LIMIT")
        stack.extend((child, depth + 1) for child in element)
    return root


def _value_transform(concept: str) -> str:
    return "absolute_value" if concept in {
        "capital_expenditure", "capital_expenditure_ppe", "capital_expenditure_intangible",
        "share_repurchase", "shareholder_dividends_paid",
    } else "identity"


def _serialize(element: ET.Element) -> str:
    detached = deepcopy(element)
    detached.tail = None
    return ET.tostring(detached, encoding="unicode")


def _xml(raw: bytes) -> tuple[ET.Element, dict[int, dict[str, str]]]:
    if not raw or len(raw) > MAX_INLINE_BYTES:
        raise ValueError("INLINE_SOURCE_SIZE_INVALID")
    # ElementTree performs no resource downloads. Reject DTD/entity declarations
    # as well as UTF-16/32 bypasses before XML expansion can allocate memory.
    if b"\0" in raw or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
        raise ValueError("INLINE_DTD_OR_ENTITY_UNSUPPORTED")
    stack = []
    pending = {}
    namespace_by_element = {}
    count = 0
    parser = ET.iterparse(io.BytesIO(raw), events=("start", "end", "start-ns"))
    for event, value in parser:
        if event == "start-ns":
            prefix, namespace = value
            pending[prefix or ""] = namespace
        elif event == "start":
            current = dict(stack[-1]) if stack else {}
            current.update(pending)
            pending.clear()
            stack.append(current)
            namespace_by_element[id(value)] = current
            count += 1
            if count > MAX_INLINE_ELEMENTS or len(stack) > MAX_INLINE_DEPTH:
                raise ValueError("INLINE_STRUCTURE_LIMIT")
        else:
            stack.pop()
    return parser.root, namespace_by_element


def _namespace_packet(element: ET.Element, namespace_by_element: dict) -> list[dict]:
    # Per-element QName scopes survive ET's prefix rewriting when evidence is
    # serialized. A rebound child prefix must never borrow its ancestor's URI.
    return [namespace_by_element[id(child)] for child in element.iter()]


def _read_namespace_packet(element: ET.Element, packet: list[dict]):
    children = tuple(element.iter())
    if (not isinstance(packet, list) or len(children) != len(packet)
            or any(not isinstance(item, dict)
                   or any(not isinstance(key, str) or not isinstance(value, str)
                          for key, value in item.items()) for item in packet)):
        raise ValueError("INLINE_NAMESPACE_PROOF_INVALID")
    mapping = {id(child): namespace for child, namespace in zip(children, packet)}
    return lambda child: mapping[id(child)]


def _fiscal_identity(identity: dict, focus: dict) -> tuple[int, str]:
    end, focus_end = date.fromisoformat(identity["end"]), date.fromisoformat(focus["end"])
    if end > focus_end:
        raise ValueError("INLINE_PERIOD_AFTER_DOCUMENT_FOCUS")
    year = int(focus["year"]) - (focus_end.year - end.year)
    if identity["period_type"] == "instant":
        period = focus["period"] if end == focus_end else "FY" if focus["period"] == "FY" else "INSTANT"
    else:
        days = (end - date.fromisoformat(identity["start"])).days + 1
        # Calendar and 52/53-week full years are explicit duration contexts.
        # A transition period or quarter inside a 10-K is retained separately.
        if focus["period"] == "FY" and 363 <= days <= 371:
            period = "FY"
        elif end == focus_end and focus["period"] != "FY":
            period = focus["period"]
        else:
            period = f"DAYS_{days}"
    return year, period


def _scope(identity: dict) -> str:
    if not identity["dimensions"]:
        return "consolidated"
    encoded = json.dumps(identity["dimensions"], ensure_ascii=False, separators=(",", ":"))
    return "dimension:" + hashlib.sha256(encoded.encode()).hexdigest()


def _dimensions(identity: dict) -> tuple[FinancialDimension, ...]:
    return tuple(FinancialDimension(axis, member, kind)
                 for axis, member, kind in identity["dimensions"])


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def extract_inline_xbrl(company: Company, filing: FilingDocument, raw: bytes) -> CandidateBatch:
    """Extract exact source candidates. The compiler remains sole admission."""
    if not filing.content_hash or hashlib.sha256(raw).hexdigest() != filing.content_hash.lower():
        return CandidateBatch(filing, diagnostics=("INLINE_SOURCE_HASH_MISMATCH",))
    try:
        root, namespace_by_element = _xml(raw)
    except (ValueError, ET.ParseError):
        return CandidateBatch(filing, diagnostics=("INLINE_SOURCE_UNSUPPORTED_OR_INVALID",))
    by_id = {}
    parent_by_element = {id(child): parent for parent in root.iter() for child in parent}
    disclosures = presentation_disclosures(root)
    duplicate_ids = set()
    for element in root.iter():
        key = element.get("id")
        if key:
            if key in by_id:
                duplicate_ids.add(key)
            by_id[key] = element
    diagnostics = [f"INLINE_DUPLICATE_ID:{key}" for key in sorted(duplicate_ids)]
    focus_entries = {}
    for element in root.iter(f"{{{IX}}}nonNumeric"):
        try:
            name = expanded_qname(element.get("name", ""), namespace_by_element[id(element)])
        except ValueError:
            continue
        match = re.fullmatch(r"\{https?://xbrl\.sec\.gov/dei/20\d{2}(?:q[1-4]|-\d{2}-\d{2})?\}(DocumentFiscalYearFocus|DocumentFiscalPeriodFocus)", name)
        if match:
            focus_entries.setdefault(match[1], []).append(element)
    try:
        year_elements = focus_entries["DocumentFiscalYearFocus"]
        period_elements = focus_entries["DocumentFiscalPeriodFocus"]
        if len(year_elements) != 1 or len(period_elements) != 1:
            raise ValueError("INLINE_DOCUMENT_FOCUS_AMBIGUOUS")
        year_element, period_element = year_elements[0], period_elements[0]
        focus_id = year_element.get("contextRef", "")
        if focus_id in duplicate_ids or focus_id != period_element.get("contextRef"):
            raise ValueError("INLINE_DOCUMENT_FOCUS_AMBIGUOUS")
        focus_context = by_id[focus_id]
        focus_identity = context_identity(focus_context, namespace_by_element[id(focus_context)],
                                           namespace_at=lambda e: namespace_by_element[id(e)])
        focus_year = "".join(year_element.itertext()).strip()
        focus_period = "".join(period_element.itertext()).strip().upper()
        if (not re.fullmatch(r"20\d{2}", focus_year)
                or focus_period not in {"FY", "Q1", "Q2", "Q3", "Q4"}
                or focus_identity["dimensions"]
                or focus_identity["period_type"] != "duration"
                or focus_identity["cik"] != company.cik.zfill(10)
                or (filing.period_end and focus_identity["end"] != filing.period_end)
                or focus_period != filing.fiscal_period.upper()):
            raise ValueError("INLINE_DOCUMENT_FOCUS_MISMATCH")
        focus = {"year": focus_year, "period": focus_period, "end": focus_identity["end"],
                 "context_xml": _serialize(focus_context),
                 "context_namespaces": _namespace_packet(focus_context, namespace_by_element),
                 "year_xml": _serialize(year_element),
                 "period_xml": _serialize(period_element),
                 "year_namespaces": _namespace_packet(year_element, namespace_by_element),
                 "period_namespaces": _namespace_packet(period_element, namespace_by_element)}
    except (KeyError, TypeError, ValueError):
        return CandidateBatch(filing, diagnostics=tuple(diagnostics + ["INLINE_DOCUMENT_FOCUS_INVALID"]))
    candidates, evidence = [], []
    for ordinal, element in enumerate(root.iter(f"{{{IX}}}nonFraction")):
        try:
            namespaces = namespace_by_element[id(element)]
            name = expanded_qname(element.get("name", ""), namespaces)
            row_element = element
            while id(row_element) in parent_by_element and row_element.tag.split("}")[-1] != "tr":
                row_element = parent_by_element[id(row_element)]
            standard = standard_concept(name)
            semantics = standard
            if semantics is None:
                continue
            if element.get(f"{{{XSI}}}nil", "false") in {"true", "1"}:
                diagnostics.append(f"INLINE_NIL:{ordinal}")
                continue
            if element.get(f"{{{XSI}}}nil", "false") not in {"false", "0"}:
                raise ValueError("INLINE_NIL_INVALID")
            if element.get("continuedAt") or element.get("target"):
                raise ValueError("INLINE_MULTI_DOCUMENT_OR_CONTINUED_NUMBER_UNSUPPORTED")
            context_id, unit_id = element.get("contextRef", ""), element.get("unitRef", "")
            if context_id in duplicate_ids or unit_id in duplicate_ids or element.get("id") in duplicate_ids:
                raise ValueError("INLINE_DUPLICATE_ID")
            context, unit = by_id[context_id], by_id[unit_id]
            if context.tag != f"{{{XBRLI}}}context" or unit.tag != f"{{{XBRLI}}}unit":
                raise ValueError("INLINE_REFERENCE_TYPE_MISMATCH")
            identity = context_identity(context, namespace_by_element[id(context)],
                                        namespace_at=lambda e: namespace_by_element[id(e)])
            semantics = presentation_role(standard, row_element, disclosures,
                                           consolidated=not identity["dimensions"])
            concept, statement, period_type = semantics
            if identity["cik"] != focus_identity["cik"] or identity["period_type"] != period_type:
                raise ValueError("INLINE_CONTEXT_IDENTITY_MISMATCH")
            kind, currency = unit_identity(unit, namespace_by_element[id(unit)],
                                           namespace_at=lambda e: namespace_by_element[id(e)])
            expected_kind = "shares" if concept == "shares_outstanding" else "money"
            if kind != expected_kind:
                raise ValueError("INLINE_QUANTITY_KIND_MISMATCH")
            transform = expanded_qname(element.get("format"), namespaces) if element.get("format") else ""
            display = "".join(element.itertext())
            raw_value, scale = numeric_token(display, transform, element.get("sign", ""), element.get("scale", "0"))
            fiscal_year, fiscal_period = _fiscal_identity(identity, focus)
            fact_id = "ix:" + hashlib.sha256(f"{filing.content_hash}:{ordinal}".encode()).hexdigest()[:32]
            proof_id = f"unit:{fact_id}"
            value_transform = _value_transform(concept)
            lineage = SourceAmount(raw_value, scale, QuantityKind(kind), currency, (proof_id,), value_transform).normalize()
            value = float(lineage.normalized_value)
            if not math.isfinite(value):
                raise ValueError("INLINE_VALUE_NOT_FINITE")
            proof = {"schema": INLINE_PARSER_VERSION, "numeric_xml": _serialize(element),
                     "numeric_namespaces": _namespace_packet(element, namespace_by_element),
                     "context_xml": _serialize(context),
                     "context_namespaces": _namespace_packet(context, namespace_by_element),
                     "unit_xml": _serialize(unit),
                     "unit_namespaces": _namespace_packet(unit, namespace_by_element), "focus": focus}
            if semantics != standard:
                proof["presentation"] = {"schema": PRESENTATION_VERSION,
                    "statement_row_xml": _serialize(row_element),
                    "disclosure_xmls": [_serialize(item) for item in disclosures]}
            locator = f"xbrl:nonFraction-{ordinal}"
            row_text = _serialize(element)
            fact = FinancialFact(
                fact_id, filing.company_cik, concept, name, value, currency or "shares",
                fiscal_year, fiscal_period, filing.form_type, identity["start"], identity["end"],
                filing.filed_at, filing.accession_number, filing.source_url,
                scope=_scope(identity), entity=company.issuer_id, market=company.market,
                statement=statement, period_start=identity["start"], consolidated_scope=_scope(identity),
                currency=currency, unit_scale=float(scale), unit_provenance="inline_explicit",
                revision=filing.revision, source_document=filing.primary_document,
                raw_text=row_text, parser_version=INLINE_PARSER_VERSION, normalization_lineage=lineage,
                source_column=f"context:{context_id}", classification_evidence_ids=(proof_id,),
                dimensions=_dimensions(identity),
            )
            row = EvidenceRef(f"fact:{fact_id}", filing.document_id, filing.source_url, concept,
                              locator, row_text, filing.filed_at, filing.content_hash)
            unit_ref = EvidenceRef(proof_id, filing.document_id, filing.source_url, "Inline XBRL source proof",
                                   locator, _json(proof), filing.filed_at, filing.content_hash)
            evidence.extend((row, unit_ref))
            candidates.append(FactCandidate(fact, (row,), INLINE_PARSER_VERSION))
        except (ValueError, KeyError, TypeError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "INLINE_REFERENCE_MISSING"
            diagnostics.append(f"{code}:{ordinal}")
    return CandidateBatch(filing, tuple(candidates), tuple(evidence), tuple(dict.fromkeys(diagnostics)))


def inline_source_issue(fact: FinancialFact, row: EvidenceRef | None, evidence: dict[str, EvidenceRef]) -> str | None:
    """Reinterpret source XML rather than trusting candidate metadata/arithmetic."""
    if fact.parser_version != INLINE_PARSER_VERSION:
        return ("inline_xbrl_proof_version_obsolete"
                if str(fact.parser_version).startswith("inline-xbrl-") else None)
    try:
        lineage = fact.normalization_lineage
        if row is None or lineage is None or len(lineage.unit_evidence_ids) != 1:
            raise ValueError("proof_missing")
        ref = evidence[lineage.unit_evidence_ids[0]]
        if (not row.content_hash or ref.document_id != row.document_id
                or ref.source_url != row.source_url or ref.content_hash != row.content_hash
                or ref.locator != row.locator or row.excerpt != fact.raw_text
                or ref.evidence_id not in fact.classification_evidence_ids):
            raise ValueError("proof_unbound")
        if len(ref.excerpt.encode("utf-8")) > MAX_PROOF_BYTES:
            raise ValueError("proof_size_invalid")
        proof = json.loads(ref.excerpt)
        if proof["schema"] != INLINE_PARSER_VERSION:
            raise ValueError("proof_version")
        numeric, context, unit = (_proof_element(proof[key]) for key in ("numeric_xml", "context_xml", "unit_xml"))
        if numeric.tag != f"{{{IX}}}nonFraction" or proof["numeric_xml"] != row.excerpt:
            raise ValueError("proof_row_mismatch")
        namespace_at = _read_namespace_packet(numeric, proof["numeric_namespaces"])
        name = expanded_qname(numeric.get("name", ""), namespace_at(numeric))
        semantics = standard_concept(name)
        identity = context_identity(context, {}, namespace_at=_read_namespace_packet(context, proof["context_namespaces"]))
        presentation = proof.get("presentation")
        if presentation is not None:
            if (not isinstance(presentation, dict) or presentation.get("schema") != PRESENTATION_VERSION
                    or not isinstance(presentation.get("disclosure_xmls"), list)
                    or not 1 <= len(presentation["disclosure_xmls"]) <= 8):
                raise ValueError("proof_presentation_invalid")
            statement_row = _proof_element(presentation["statement_row_xml"])
            numeric_rows = [item for item in statement_row.iter(f"{{{IX}}}nonFraction")
                            if _serialize(item) == proof["numeric_xml"]]
            if len(numeric_rows) != 1:
                raise ValueError("proof_presentation_row_unbound")
            disclosures = tuple(_proof_element(value) for value in presentation["disclosure_xmls"])
            semantics = presentation_role(semantics, statement_row, disclosures,
                                           consolidated=not identity["dimensions"])
        kind, currency = unit_identity(unit, {}, namespace_at=_read_namespace_packet(unit, proof["unit_namespaces"]))
        focus = proof["focus"]
        focus_context = _proof_element(focus["context_xml"])
        focus_identity = context_identity(focus_context, {}, namespace_at=_read_namespace_packet(focus_context, focus["context_namespaces"]))
        year_element, period_element = (_proof_element(focus[key]) for key in ("year_xml", "period_xml"))
        for element, key, local in ((year_element, "year_namespaces", "DocumentFiscalYearFocus"),
                                    (period_element, "period_namespaces", "DocumentFiscalPeriodFocus")):
            namespaces = _read_namespace_packet(element, focus[key])(element)
            if (element.tag != f"{{{IX}}}nonNumeric"
                    or not re.fullmatch(r"\{https?://xbrl\.sec\.gov/dei/20\d{2}(?:q[1-4]|-\d{2}-\d{2})?\}" + local,
                                        expanded_qname(element.get("name", ""), namespaces))
                    or element.get("contextRef") != focus_context.get("id")
                    or element.get(f"{{{XSI}}}nil", "false") not in {"false", "0"}
                    or element.get("continuedAt") or element.get("target")):
                raise ValueError("proof_focus_mismatch")
        if (context.tag != f"{{{XBRLI}}}context" or unit.tag != f"{{{XBRLI}}}unit"
                or context.get("id") != numeric.get("contextRef")
                or unit.get("id") != numeric.get("unitRef")
                or focus_identity["cik"] != identity["cik"]
                or focus_identity["dimensions"] or focus_identity["period_type"] != "duration"
                or focus_identity["end"] != focus["end"]
                or not re.fullmatch(r"20\d{2}", focus["year"])
                or focus["period"] not in {"FY", "Q1", "Q2", "Q3", "Q4"}
                or "".join(year_element.itertext()).strip() != focus["year"]
                or "".join(period_element.itertext()).strip().upper() != focus["period"]
                or identity["cik"] != fact.company_cik.zfill(10)
                or numeric.get(f"{{{XSI}}}nil", "false") not in {"false", "0"}
                or numeric.get("continuedAt") or numeric.get("target")):
            raise ValueError("proof_identity_mismatch")
        transform = expanded_qname(numeric.get("format"), namespace_at(numeric)) if numeric.get("format") else ""
        raw_value, scale = numeric_token("".join(numeric.itertext()), transform, numeric.get("sign", ""), numeric.get("scale", "0"))
        if (semantics is None or semantics[:2] != (fact.concept, fact.statement)
                or semantics[2] != identity["period_type"] or name != fact.reported_concept
                or (identity["start"], identity["end"]) != (fact.start_date, fact.end_date)
                or _fiscal_identity(identity, focus) != (fact.fiscal_year, fact.fiscal_period)
                or _scope(identity) != fact.consolidated_scope or fact.scope != fact.consolidated_scope
                or _dimensions(identity) != fact.dimensions
                or kind != lineage.quantity_kind or currency != fact.currency
                or raw_value != lineage.raw_value or Decimal(scale) != Decimal(lineage.scale)
                or lineage.value_transform != _value_transform(fact.concept)
                or fact.unit_provenance != "inline_explicit"):
            raise ValueError("proof_semantics_mismatch")
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, OverflowError, ET.ParseError):
        return "inline_xbrl_source_proof_mismatch"
    return None
