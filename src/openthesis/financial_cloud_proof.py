"""I/O-free, independently checked source semantics for cloud table cells.

This proves binding to OCR output, not that an OCR provider is infallible.
Native/other-source corroboration and financial invariants remain downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from functools import lru_cache
import json

from .financial_sources.document_units import discover_document_units
from .financial_table_text import CLOUD_TABLE_VERSION, explicit_table_unit, observe_cells, read_text_tables
from .financial_taxonomy import profit_loss_value_transform


@dataclass(frozen=True, slots=True)
class CloudUnitContext:
    original_page: int
    excerpt: str
    document_hash: str


def canonical_cloud_source(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def cloud_source_id(document_id: str, payload) -> str:
    return "vision-table:" + sha256((document_id + "|" + canonical_cloud_source(payload)).encode()).hexdigest()


def cloud_cell_id(document_id: str, page: int, page_hash: str, cell) -> str:
    return document_id + ":cloud-cell:" + sha256(canonical_cloud_source({
        "page": page, "page_hash": page_hash, "table": cell.table_index,
        "row": cell.row_index, "column": cell.column_index,
    }).encode()).hexdigest()


def cloud_period_start(fiscal_period: str, end: str, statement: str) -> str | None:
    if statement == "balance_sheet":
        return None
    value = date.fromisoformat(end)
    months = {"FY": 12, "Q1": 3, "H1": 6, "Q2": 6, "Q3": 9, "H2": 12, "Q4": 12}.get(fiscal_period.upper())
    if months is None:
        raise ValueError("CLOUD_FISCAL_PERIOD_UNSUPPORTED")
    index = value.year * 12 + value.month - months
    prior_year, prior_month = divmod(index, 12)
    # An end-of-month fiscal interval begins the following day one interval
    # earlier; FY March 31 is April 1, not January 1.
    following_month = date(prior_year + (prior_month == 12), prior_month % 12 + 1, 1)
    return following_month.isoformat()


def cloud_unit_for_table(table, contexts, document_hash):
    local = explicit_table_unit(table)
    if local is not None and local[1] and local[2]:
        return (*local, None)
    applicable = []
    for item in contexts:
        if not isinstance(item, dict) or item.get("document_hash") != document_hash:
            continue
        page = item.get("original_page")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            continue
        excerpt = str(item.get("excerpt", ""))
        declarations = discover_document_units((excerpt,)).declarations
        for declaration in declarations:
            if declaration.scope == "all" and declaration.excerpt == excerpt:
                applicable.append((excerpt, declaration.currency, declaration.scale, page))
    identities = {(currency, scale) for _, currency, scale, _ in applicable}
    if len(identities) != 1:
        return None
    broad = applicable[0]
    if local is not None and ((local[1] and local[1] != broad[1]) or (local[2] and local[2] != broad[2])):
        return None
    return broad


def cloud_value_transform(label, concept):
    if concept in {"capital_expenditure", "capital_expenditure_ppe", "capital_expenditure_intangible",
                   "share_repurchase", "shareholder_dividends_paid", "lease_liability_payment",
                   "deferred_asset_acquisition_payment"}:
        return "absolute_value"
    return profit_loss_value_transform(label, concept)


@lru_cache(maxsize=16)
def _source_observations(excerpt):
    # Bound memory and avoid decoding/parsing the same immutable OCR source
    # once per cell. The exact excerpt is the key, not a provider's digest.
    if len(excerpt.encode("utf-8")) > 5 * 1024 * 1024:
        raise ValueError("CLOUD_SOURCE_SIZE_LIMIT")
    payload = json.loads(excerpt)
    if not isinstance(payload, dict):
        raise ValueError("CLOUD_SOURCE_OBJECT_REQUIRED")
    tables = read_text_tables(payload["markdown"])
    cells = observe_cells(tables, payload["period_end"], payload["fiscal_period"])
    return payload, tables, cells


def cloud_table_source_issue(fact, row_ref, evidence_by_id) -> str | None:
    if fact.parser_version != CLOUD_TABLE_VERSION:
        return None
    try:
        proof_ids = [key for key in fact.classification_evidence_ids if key.startswith("vision-table:")]
        if len(proof_ids) != 1 or row_ref is None:
            return "cloud_table_source_proof_missing"
        proof = evidence_by_id[proof_ids[0]]
        payload, tables, cells = _source_observations(proof.excerpt)
        if (not isinstance(payload, dict) or payload.get("schema") != CLOUD_TABLE_VERSION
                or cloud_source_id(proof.document_id, payload) != proof.evidence_id
                or proof.document_id != row_ref.document_id or proof.source_url != row_ref.source_url
                or proof.content_hash != row_ref.content_hash or payload.get("document_hash") != proof.content_hash
                or payload.get("original_page") != fact.source_page
                or proof.locator != f"page:{fact.source_page}" or row_ref.locator != proof.locator
                or not isinstance(payload.get("page_hash"), str) or not payload["page_hash"]
                or payload.get("fiscal_period") != fact.fiscal_period):
            return "cloud_table_source_proof_unbound"
        cell = next((item for item in cells if cloud_cell_id(proof.document_id,
            fact.source_page, payload["page_hash"], item) == fact.fact_id), None)
        if cell is None:
            return "cloud_table_cell_unproven"
        unit = cloud_unit_for_table(tables[cell.table_index], payload.get("unit_contexts", []), proof.content_hash)
        if (unit is None or fact.currency != unit[1] or float(fact.unit_scale) != float(unit[2])
                or fact.reported_concept != cell.label or fact.concept != cell.concept
                or fact.statement != cell.statement or fact.source_column != cell.column_text
                or fact.end_date != cell.period_end or fact.fiscal_year != int(cell.period_end[:4])
                or fact.revision != cell.revision or fact.consolidated_scope != "consolidated"
                or fact.start_date != cloud_period_start(fact.fiscal_period, cell.period_end, cell.statement)
                or fact.period_start != fact.start_date):
            return "cloud_table_cell_identity_mismatch"
        row = " | ".join(tables[cell.table_index].rows[cell.row_index])
        lineage = fact.normalization_lineage
        if (row_ref.excerpt != row or fact.raw_text != row or row_ref.evidence_id != f"fact:{fact.fact_id}"
                or lineage is None or lineage.raw_value != cell.raw_value
                or lineage.value_transform != cloud_value_transform(cell.label, cell.concept)):
            return "cloud_table_selected_cell_mismatch"
        if len(lineage.unit_evidence_ids) != 1:
            return "cloud_table_unit_evidence_missing"
        unit_ref = evidence_by_id.get(lineage.unit_evidence_ids[0])
        if (unit_ref is None or unit_ref.excerpt != unit[0] or unit_ref.content_hash != proof.content_hash
                or unit_ref.document_id != proof.document_id or unit_ref.source_url != proof.source_url
                or unit_ref.locator != f"page:{unit[3] or fact.source_page}"):
            return "cloud_table_unit_evidence_unbound"
    except (KeyError, TypeError, ValueError, StopIteration, OverflowError):
        return "cloud_table_source_proof_invalid"
    return None
