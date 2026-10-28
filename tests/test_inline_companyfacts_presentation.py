"""The compiler must not reintroduce a taxonomy role contradicted by its filing."""
from dataclasses import replace

from openthesis.domain import EvidenceRef
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import CandidateBatch, CompilationPolicy, FactCandidate, QuantityKind, SourceAmount
from test_inline_xbrl_source import equity_presentation


def candidates(*, valid_proof=True, different_period=False):
    native = equity_presentation('Noncontrolling interests are included within additional paid-in capital.')
    item = native.candidates[0]
    fact = item.fact
    structured_id = 'companyfacts-equity'
    unit_id = 'unit:companyfacts-equity'
    lineage = SourceAmount('415265000000', '1', QuantityKind.MONEY, 'USD', (unit_id,)).normalize()
    structured = replace(fact, fact_id=structured_id, concept='equity', reported_concept='StockholdersEquity',
                         parser_version='sec-companyfacts-v4', unit_provenance='structured_normalized',
                         unit_scale=1, raw_text='us-gaap:StockholdersEquity=415265000000 USD',
                         normalization_lineage=lineage, classification_evidence_ids=(),
                         source_document='SEC CompanyFacts us-gaap:StockholdersEquity',
                         end_date='2025-01-25' if different_period else fact.end_date,
                         fiscal_year=2025 if different_period else fact.fiscal_year)
    row = EvidenceRef('fact:' + structured_id, native.filing.document_id, fact.source_url, 'SEC row',
                      'accession:' + fact.accession_number, structured.raw_text, fact.filed_at, '')
    unit = replace(row, evidence_id=unit_id, locator='xbrl:us-gaap/StockholdersEquity/units/USD',
                   excerpt='CompanyFacts raw value=415265000000; unit=USD; scale=1')
    if not valid_proof:
        item = replace(item, fact=replace(item.fact, concept='equity'))
    batch = CandidateBatch(native.filing, (item, FactCandidate(structured, (row,), 'structured:SEC')),
                           (*native.evidence, row, unit))
    from openthesis.domain import Company
    return Company(fact.company_cik, 'NVDA', 'NVIDIA', market='US', reporting_currency='USD'), batch


def compile_pair(**kwargs):
    company, batch = candidates(**kwargs)
    return FinancialFactCompiler().compile(company, (batch,), CompilationPolicy(
        filings=(batch.filing,), reporting_currency='USD', required_concepts=frozenset({'total_equity'})))


def test_original_explicit_presentation_cannot_be_reintroduced_as_parent_by_companyfacts():
    dataset = compile_pair()
    assert {fact.concept for fact in dataset.resolved_facts} == {'total_equity'}
    assert any(fact.fact_id == 'companyfacts-equity' for fact in dataset.quarantined_facts)
    assert any(item['policy'] == 'original-issuer-presentation-v1'
               for item in dataset.coverage.get('candidate_resolutions', ()))


def test_invalid_native_presentation_never_overrules_independent_structured_row():
    dataset = compile_pair(valid_proof=False)
    assert any(fact.fact_id == 'companyfacts-equity' for fact in dataset.resolved_facts)
    assert not any(item.get('policy') == 'original-issuer-presentation-v1'
                   for item in dataset.coverage.get('candidate_resolutions', ()))


def test_presentation_scope_does_not_transfer_to_an_unproven_different_period():
    dataset = compile_pair(different_period=True)
    assert any(fact.fact_id == 'companyfacts-equity' for fact in dataset.resolved_facts)
