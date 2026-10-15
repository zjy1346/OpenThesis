"""Bind official text to already compiled rows; never parse a quote as money."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Sequence

from .financial_admission import FinancialResearchView
from .financial_contracts import FinancialDataset, fact_row_evidence_ids

DISCLOSURE_BINDING_VERSION = "compiled-disclosure-binding-v1"


def _literal_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def bind_compiled_disclosures(
    view: FinancialResearchView, records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose row aliases only after FinancialPipeline compilation/admission.

    Same URL alone is not provenance. Hash, document, physical page and the
    complete literal source row must all agree. Ambiguous/uncompiled prose
    stays a disclosure and cannot supply canonical amounts or inferred units.
    """
    if not isinstance(view, FinancialResearchView):
        raise TypeError("COMPILED_FINANCIAL_RESEARCH_VIEW_REQUIRED")
    result = deepcopy(list(records))
    for record in result:
        record.pop("compiled_disclosure_binding", None)
    if not isinstance(view.canonical_dataset, FinancialDataset):
        return result
    references = {ref.evidence_id: ref for ref in view.evidence}
    rows = []
    for fact in (*view.facts, *view.profile.disclosure_facts):
        ref = next((references[key] for key in fact_row_evidence_ids(fact)
                    if key in references), None)
        if ref is None or not re.fullmatch(r"[0-9a-fA-F]{64}", ref.content_hash or ""):
            continue
        page = re.fullmatch(r"page:(\d+)", ref.locator)
        if page and ref.excerpt.strip():
            rows.append((fact, ref, int(page[1])))
    for record in result:
        # Never retain an untrusted or stale enrichment supplied by a cache.
        record.pop("compiled_disclosure_binding", None)
        if record.get("kind") != "filing_text":
            continue
        page = re.match(r"^page:(\d+)(?:;|$)", str(record.get("locator", "")))
        if not page:
            continue
        excerpt = _literal_text(str(record.get("excerpt", "")))
        bound = []
        for fact, row, row_page in rows:
            if (row_page == int(page[1]) and row.document_id == record.get("document_id")
                    and row.source_url == record.get("source_url")
                    and row.content_hash == record.get("content_hash")
                    and fact.company_cik == record.get("company_cik")
                    and _literal_text(row.excerpt) in excerpt):
                bound.append(f"fact:{fact.fact_id}")
        if bound:
            record["compiled_disclosure_binding"] = {
                "version": DISCLOSURE_BINDING_VERSION,
                "content_hash": record["content_hash"],
                "locator": record["locator"],
                "evidence_ids": list(dict.fromkeys(bound)),
            }
    return result


def cited_compiled_rows(record: dict[str, Any], evidence: dict[str, dict]) -> list[dict]:
    binding = record.get("compiled_disclosure_binding")
    if (record.get("kind") != "filing_text" or not isinstance(binding, dict)
            or re.fullmatch(r"[0-9a-fA-F]{64}", str(record.get("content_hash", ""))) is None
            or re.match(r"^page:\d+(?:;|$)", str(record.get("locator", ""))) is None
            or binding.get("version") != DISCLOSURE_BINDING_VERSION
            or binding.get("content_hash") != record.get("content_hash")
            or binding.get("locator") != record.get("locator")):
        return []
    ids = binding.get("evidence_ids")
    if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
        return []
    return [evidence[key] for key in ids if key in evidence
            and evidence[key].get("kind") == "financial_fact"
            and evidence[key].get("company_cik") == record.get("company_cik")
            and evidence[key].get("source_url") == record.get("source_url")
            and evidence[key].get("usage_status") not in {"rejected", "quarantined", "unverified", "audit_only"}]
