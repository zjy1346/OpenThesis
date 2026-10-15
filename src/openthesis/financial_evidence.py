from typing import Any


def build_fact_evidence(facts: list[dict[str, Any]], *,
                        source_records: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()) -> list[dict[str, Any]]:
    """Project admitted amounts without losing an actually bound original row.

    An accession/title is not a document hash. Optional original references
    come from the host admission snapshot, never from model output or guessed
    provenance. Legacy facts without a reference remain unlocated.
    """
    evidence: list[dict[str, Any]] = []
    for fact in facts:
        evidence.append(
            {
                "evidence_id": f"fact:{fact['fact_id']}",
                "kind": "financial_fact",
                "company_cik": fact.get("company_cik", ""),
                "entity": fact.get("entity", ""),
                "market": fact.get("market", ""),
                "concept": fact["concept"],
                "value": fact["value"],
                "unit": fact["unit"],
                "unit_scale": fact.get("unit_scale", 1.0),
                "unit_provenance": fact.get("unit_provenance", "unknown"),
                "scope": fact.get("scope", ""),
                "consolidated_scope": fact.get("consolidated_scope", ""),
                "fiscal_year": fact["fiscal_year"],
                "fiscal_period": fact.get("fiscal_period", ""),
                "form_type": fact.get("form_type", ""),
                "start_date": fact.get("start_date", ""),
                "end_date": fact.get("end_date", ""),
                "filed_at": fact["filed_at"],
                "source_url": fact["source_url"],
                "raw_text": fact.get("raw_text", ""),
                "accession_number": fact.get("accession_number", ""),
                "source_document": fact.get("source_document", ""),
                "dimensions": fact.get("dimensions", ()),
                "normalization_lineage": fact.get("normalization_lineage"),
                "classification_evidence_ids": fact.get("classification_evidence_ids", ()),
                "usage_status": fact.get("usage_status", ""),
            }
        )
        record = evidence[-1]
        for key in ("source_page", "source_bbox", "source_column", "generation_id", "validation_status", "currency"):
            if fact.get(key) is not None and fact.get(key) != "":
                record[key] = fact[key]
        aliases = {f"fact:{fact['fact_id']}"}
        if str(fact['fact_id']).startswith("ingest:"):
            aliases.add(f"fact:{str(fact['fact_id'])[7:]}")
        matches = [ref for ref in source_records
            if isinstance(ref, dict) and ref.get("evidence_id") in aliases
            and ref.get("source_url") == fact.get("source_url")
            and ref.get("excerpt") == fact.get("raw_text") and ref.get("excerpt")
            and ref.get("document_id") and isinstance(ref.get("content_hash"), str)
            and len(ref["content_hash"]) == 64
            and all(char in "0123456789abcdefABCDEF" for char in ref["content_hash"])
            and (fact.get("source_page") is None or ref.get("locator") == f"page:{fact['source_page']}")
            and (fact.get("source_bbox") is None or tuple(ref.get("bbox") or ()) == tuple(fact["source_bbox"]))]
        identities = {(ref["document_id"], ref["content_hash"], ref.get("locator")) for ref in matches}
        if len(identities) == 1:
            ref = matches[0]
            record.update(document_id=ref["document_id"], document_sha256=ref["content_hash"],
                locator=ref.get("locator", ""), source_row_evidence_id=ref["evidence_id"])
            if str(ref.get("locator", "")).startswith("page:"):
                record["page"] = fact.get("source_page")
    return evidence
