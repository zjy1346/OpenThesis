"""Stable financial-ingestion boundary.

The application historically imported the PDF parser directly from
``market_financials``.  This module provides a small, provider-neutral facade
for structured facts and official-document evidence while keeping that import
compatible.  Providers may return ``FinancialFact`` instances from XBRL or an
official API; PDF extraction remains the evidence fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol
import re

from .domain import Company, FilingDocument, FinancialFact, EvidenceRef
from .market_financials import (
    FinancialExtractionError,
    FinancialValidation,
    StatementContext,
    ValidationStatus,
    extract_pdf_financials,
    financial_quality_issues,
    parse_financial_pages,
    validate_financial_facts,
)


@dataclass(frozen=True, slots=True)
class OfficialSource:
    """Identity/provenance for a structured or document-backed source."""

    entity: str
    market: str
    statement: str
    concept: str
    fiscal_period: str
    period_start: str | None
    period_end: str
    consolidated_scope: str
    currency: str
    unit: str
    revision: str
    source_url: str
    document: str = ""
    page: int | None = None
    raw_text: str = ""
    parser_version: str = ""
    validation_status: str = ValidationStatus.READY_WITH_WARNINGS.value


class StructuredFinancialProvider(Protocol):
    """Optional structured source (XBRL or user-configured data service)."""

    def fetch(self, company: Company, filing: FilingDocument) -> list[FinancialFact]: ...


def ingest_official_pdf(
    filing: FilingDocument,
    company: Company,
) -> tuple[list[FinancialFact], list[EvidenceRef], list[str]]:
    """Official-PDF fallback with page-level evidence and quality metadata."""

    return extract_pdf_financials(filing, company)


def parse_structured_snapshot(raw_excerpt: str) -> dict[str, float]:
    """Parse a minimal provider/XBRL snapshot used by deterministic fixtures."""

    return {concept: float(value) for concept, value in re.findall(r"([a-z_]+)=([0-9]+(?:\.[0-9]+)?)", raw_excerpt)}


def prepare_facts_for_ai(facts: list[FinancialFact]) -> tuple[list[FinancialFact], FinancialValidation]:
    """Return only accepted facts; rejected groups are never AI inputs."""
    validation = validate_financial_facts(facts)
    return list(validation.accepted), validation


__all__ = [
    "FinancialExtractionError",
    "FinancialValidation",
    "OfficialSource",
    "StatementContext",
    "StructuredFinancialProvider",
    "ValidationStatus",
    "financial_quality_issues",
    "ingest_official_pdf",
    "parse_financial_pages",
    "parse_structured_snapshot",
    "prepare_facts_for_ai",
    "validate_financial_facts",
]
