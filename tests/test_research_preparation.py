"""Research preparation identity must ignore parser bookkeeping, not meaning."""
from copy import deepcopy
from types import SimpleNamespace

from openthesis.application.research_preparation import scope_gap_recovery_targets
from openthesis.application.research_snapshot import financial_semantics_digest


def test_financial_semantics_digest_ignores_parser_generation_but_binds_fact_meaning():
    fact = {
        "fact_id": "fact-1",
        "value": 100.0,
        "unit": "CNY",
        "scope": "consolidated",
        "parser_version": "parser-v1",
        "generation_id": "generation-1",
    }
    original = financial_semantics_digest([fact])
    reprocessed = {**fact, "parser_version": "parser-v2", "generation_id": "generation-2"}
    assert financial_semantics_digest([reprocessed]) == original

    for key, value in (("value", 101.0), ("unit", "USD"), ("scope", "segment")):
        changed = deepcopy(fact)
        changed[key] = value
        assert financial_semantics_digest([changed]) != original, key


def test_scope_gap_targets_require_exact_period_and_fiscal_period_match():
    filings = (
        SimpleNamespace(accession_number="old", period_end="2024-12-31", fiscal_period="FY"),
        SimpleNamespace(accession_number="latest-annual", period_end="2025-12-31", fiscal_period="FY"),
        SimpleNamespace(accession_number="latest-interim", period_end="2025-12-31", fiscal_period="H1"),
        SimpleNamespace(accession_number="other", period_end="2026-12-31", fiscal_period="FY"),
    )
    gaps = (
        {"period_end": "2025-12-31", "fiscal_period": "FY"},
        {"period_end": "2025-12-31", "fiscal_period": "H1"},
    )

    assert scope_gap_recovery_targets(gaps, filings) == ("latest-annual", "latest-interim")
    assert scope_gap_recovery_targets(({"period_end": "2025-12-31", "fiscal_period": "Q1"},), filings) == ()
