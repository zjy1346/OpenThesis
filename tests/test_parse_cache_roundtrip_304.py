"""JSON hydration must preserve the geometry used in source-evidence digests."""
from openthesis.financial_ingestion import FinancialIngestionEngine
from test_statutory_presentation_currency_303 import parse, compile_rows


def test_persistent_parse_cache_roundtrip_keeps_source_identity_and_admission(tmp_path):
    company, filing, facts, evidence = parse()
    before = compile_rows(company, filing, facts, evidence)
    assert len(before.accepted_facts) == 2
    cache = tmp_path / "cache"
    FinancialIngestionEngine(cache_dir=cache)._store_parse_cache("source-bound", facts, evidence)
    loaded = FinancialIngestionEngine(cache_dir=cache)._load_parse_cache("source-bound")
    assert loaded is not None
    recovered, refs = loaded
    assert recovered == facts
    assert refs == evidence
    after = compile_rows(company, filing, recovered, refs)
    assert [fact.fact_id for fact in after.accepted_facts] == [fact.fact_id for fact in before.accepted_facts]
    assert not after.quarantined_facts
