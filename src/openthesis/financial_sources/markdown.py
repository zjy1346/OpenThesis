"""Collect cloud Markdown cells and their original-document evidence only."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256

from ..domain import EvidenceRef, FinancialFact
from ..financial_contracts import QuantityKind, SourceAmount
from ..financial_cloud_proof import (
    canonical_cloud_source, cloud_cell_id, cloud_period_start, cloud_source_id,
    cloud_unit_for_table, cloud_value_transform,
)
from ..financial_table_text import CLOUD_TABLE_VERSION, observe_cells, read_text_tables


def collect_markdown_cells(markdown, company, filing, page, *, source_structure=None):
    if not markdown.strip():
        return (), (), "VISION_EMPTY_MARKDOWN"
    if not filing.content_hash:
        return (), (), "VISION_DOCUMENT_HASH_REQUIRED"
    try:
        tables = read_text_tables(markdown)
        cells = observe_cells(tables, filing.period_end, filing.fiscal_period)
        payload = {
            "schema": CLOUD_TABLE_VERSION, "markdown": markdown,
            "document_hash": filing.content_hash, "original_page": page.original_page,
            "page_hash": page.content_hash, "period_end": filing.period_end,
            "fiscal_period": filing.fiscal_period,
            "unit_contexts": [asdict(item) for item in page.unit_contexts],
        }
        if source_structure is not None:
            payload['source_structure'] = source_structure
        proof_id = cloud_source_id(filing.document_id, payload)
        proof = EvidenceRef(proof_id, filing.document_id, filing.source_url,
            "Cloud table source / candidate only", f"page:{page.original_page}",
            canonical_cloud_source(payload), filing.filed_at, filing.content_hash)
        facts, refs = [], {proof_id: proof}
        for cell in cells:
            try:
                unit = cloud_unit_for_table(tables[cell.table_index], payload["unit_contexts"], filing.content_hash,
                                            column_index=cell.column_index)
            except ValueError:
                # One unresolved column must not erase other independently
                # bound cells or the complete unverified source structure.
                continue
            if unit is None:
                continue
            unit_text, currency, scale, unit_page = unit
            if currency != company.reporting_currency.upper():
                continue
            unit_id = "vision-unit:" + sha256(f"{filing.document_id}|{unit_page or page.original_page}|{unit_text}".encode()).hexdigest()
            lineage = SourceAmount(cell.raw_value, scale, QuantityKind.MONEY, currency, (unit_id,),
                cloud_value_transform(cell.label, cell.concept)).normalize()
            fact_id = cloud_cell_id(filing.document_id, page.original_page, page.content_hash, cell)
            start = cloud_period_start(filing.fiscal_period, cell.period_end, cell.statement)
            row = " | ".join(tables[cell.table_index].rows[cell.row_index])
            facts.append(FinancialFact(
                fact_id, company.security_id, cell.concept, cell.label,
                float(Decimal(lineage.normalized_value)), currency, int(cell.period_end[:4]),
                filing.fiscal_period, filing.form_type, start, cell.period_end,
                filing.filed_at, filing.accession_number, filing.source_url,
                entity=company.name, market=company.market, statement=cell.statement,
                scope=cell.scope, consolidated_scope=cell.scope,
                period_start=start, currency=currency, unit_scale=float(Decimal(scale)),
                unit_provenance="vision_explicit", revision=cell.revision,
                source_document=filing.primary_document, source_page=page.original_page,
                raw_text=row, source_column=cell.column_text, parser_version=CLOUD_TABLE_VERSION,
                normalization_lineage=lineage, classification_evidence_ids=(proof_id,),
            ))
            refs[f"fact:{fact_id}"] = EvidenceRef(f"fact:{fact_id}", filing.document_id,
                filing.source_url, filing.primary_document, f"page:{page.original_page}",
                row, filing.filed_at, filing.content_hash)
            refs[unit_id] = EvidenceRef(unit_id, filing.document_id, filing.source_url,
                "Explicit financial-statement unit", f"page:{unit_page or page.original_page}",
                unit_text, filing.filed_at, filing.content_hash)
        if not facts:
            return (), tuple(refs.values()), "VISION_CONTEXT_NOT_EXPLICIT" if not cells else "VISION_UNIT_CONTEXT_REQUIRED"
        return tuple(facts), tuple(refs.values()), None
    except (TypeError, ValueError, OverflowError):
        return (), (), "VISION_TABLE_SOURCE_INVALID"
