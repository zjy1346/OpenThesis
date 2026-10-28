"""Audited, concept-local parser compatibility; never a legacy trust flag.

The v32 -> v33 change corrected relative/interim comparative headers and
numeric year spans (release 3.1.2). Only original instant balance cells with
an exact dated column and full positioned/amount proof can be revalidated.
All other old observations require a local re-extraction. Compatible rows
retain their original producer version and pass every current validator.
"""
from __future__ import annotations

import re
from .financial_contracts import PDF_PARSER_VERSION


PARSER_COMPATIBILITY_MANIFEST = {
    "contract": "financial-parser-compatibility-v1",
    "current": PDF_PARSER_VERSION,
    "financial-ingestion-ast-v32": {
        "concepts": ("assets", "liabilities", "equity", "total_equity"),
        "rule": "instant-original-exact-date-positioned-amount-v1",
        "excluded": "comparators, relative/interim headers, ratios and undated columns",
    },
}


def pdf_rules_compatible(fact) -> bool:
    if fact.parser_version == PDF_PARSER_VERSION:
        return True
    rule = PARSER_COMPATIBILITY_MANIFEST.get(fact.parser_version)
    if not isinstance(rule, dict) or fact.concept not in rule["concepts"]:
        return False
    if (fact.statement != "balance_sheet" or fact.usage_status == "comparator"
            or fact.revision != "original" or fact.normalization_lineage is None
            or not fact.source_bbox or not fact.source_page or not fact.raw_text
            or not fact.classification_evidence_ids):
        return False
    dates = re.findall(r"((?:19|20)\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", fact.source_column)
    return bool(len(dates) == 1 and "-".join((dates[0][0], dates[0][1].zfill(2),
        dates[0][2].zfill(2))) == fact.end_date and str(fact.fiscal_year) == fact.end_date[:4])
