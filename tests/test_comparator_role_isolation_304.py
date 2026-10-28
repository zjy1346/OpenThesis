"""One supplemental prior-year cell cannot turn a comparative group into a new filing."""
from dataclasses import replace
from openthesis.domain import EvidenceRef
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import CandidateBatch, CompilationPolicy, FactCandidate
from test_financial_compiler import _facts, _filing, _subject
from openthesis.storage import Storage
from openthesis.financial_contracts import FinancialGroupValidation, FinancialValidation, ValidationStatus


def test_partial_comparator_never_displaces_the_original_verified_annual_cohort():
    old = replace(_filing("old", "2024-12-31", "2024 annual report"), filed_at="2025-04-06")
    new = replace(_filing("new", "2025-12-31", "2025 annual report"), filed_at="2026-04-06")
    historical = _facts(old, net_income=100)
    current = _facts(new, net_income=110)
    comparative = tuple(FactCandidate(replace(item.fact,
        fact_id=item.fact.fact_id + ":prior", fiscal_year=2024, end_date="2024-12-31",
        usage_status="comparator"), item.evidence, item.extractor) for item in current)
    extra_fact = replace(comparative[0].fact, fact_id="prior-disclosure",
        concept="gross_profit", reported_concept="Gross profit", value=10,
        usage_status="audit_only", raw_text="Gross profit 10")
    extra_ref = EvidenceRef("fact:prior-disclosure", new.document_id, new.source_url,
        "Prior year disclosure", "page:132", extra_fact.raw_text, new.filed_at, new.content_hash)
    extra = FactCandidate(extra_fact, (extra_ref,), "summary")
    result = FinancialFactCompiler().compile(_subject(),
        [CandidateBatch(old, historical), CandidateBatch(new, current + comparative + (extra,))],
        CompilationPolicy(filings=(old, new), reporting_currency="CNY"))
    assert any(f.accession_number == "old" and f.concept == "revenue" for f in result.annual_facts)
    assert not any(f.accession_number == "new" and f.end_date == "2024-12-31" for f in result.annual_facts)
    assert any(f.fact_id == "prior-disclosure" for f in result.resolved_facts)


def test_accepted_storage_roundtrip_preserves_comparative_role(tmp_path):
    storage = Storage(tmp_path / "data")
    subject = _subject()
    storage.save_company(subject)
    source = replace(_filing("new", "2025-12-31", "2025 annual report"), filed_at="2026-04-06")
    fact = replace(_facts(source, net_income=100)[0].fact,
        usage_status="comparator", fiscal_year=2024, end_date="2024-12-31")
    storage.save_facts([fact])
    saved = storage.get_financial_research_snapshot(subject.cik)["facts"]
    assert saved and saved[0]["usage_status"] == "comparator"


def test_legacy_flattened_role_is_read_from_usable_ledger_without_rewriting_the_row(tmp_path):
    storage = Storage(tmp_path / "data")
    subject = _subject()
    storage.save_company(subject)
    source = replace(_filing("new", "2025-12-31", "2025 annual report"), filed_at="2026-04-06")
    fact = replace(_facts(source, net_income=100)[0].fact,
        usage_status="comparator", fiscal_year=2024, end_date="2024-12-31")
    validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (fact,), ())
    storage.replace_financial_ingestion(subject.cik, [source.accession_number], [fact], [],
        [FinancialGroupValidation((source.accession_number, fact.end_date, "FY", "consolidated", "CNY"),
                                  validation, "comparator")])
    with storage.connect() as db:
        db.execute("UPDATE financial_facts SET usage_status='canonical_research' WHERE fact_id=?", (fact.fact_id,))
    saved = storage.get_financial_research_snapshot(subject.cik)["facts"]
    assert saved[0]["usage_status"] == "comparator"
    assert saved[0]["value"] == fact.value
    with storage.connect() as db:
        assert db.execute("SELECT usage_status FROM financial_facts WHERE fact_id=?", (fact.fact_id,)).fetchone()[0] == "canonical_research"
