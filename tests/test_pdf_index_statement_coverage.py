"""Additional necessary statements must not invalidate a complete PDF index."""
from openthesis.financial_ingestion import _candidate_pages_from_text


def test_equity_and_comprehensive_statements_preserve_core_index_and_all_starts():
    texts = [""] * 20
    titles = {2: "Consolidated Balance Sheet", 4: "Consolidated Income Statement",
              6: "Consolidated Statement of Cash Flows",
              8: "Consolidated Statement of Comprehensive Income",
              10: "Consolidated Statement of Changes in Equity"}
    for page, title in titles.items():
        texts[page - 1] = title
    indexed = _candidate_pages_from_text(texts, continuation_pages=0)
    assert indexed is not None
    assert set(titles).issubset(indexed)
    assert 20 not in indexed


def test_additional_statements_cannot_fill_a_missing_core_statement():
    texts = ["Consolidated Balance Sheet", "Consolidated Income Statement",
             "Consolidated Statement of Comprehensive Income",
             "Consolidated Statement of Changes in Equity"]
    assert _candidate_pages_from_text(texts, continuation_pages=0) is None


def test_additional_statements_alone_do_not_claim_complete_core_coverage():
    texts = ["Consolidated Statement of Comprehensive Income",
             "Consolidated Statement of Changes in Equity"]
    assert _candidate_pages_from_text(texts, continuation_pages=0) is None
