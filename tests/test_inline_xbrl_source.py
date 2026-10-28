"""Original XML binding, not synthetic floats, proves native XBRL candidates."""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

import pytest

from openthesis.domain import Company, FilingDocument
from openthesis.financial_inline_xbrl import extract_inline_xbrl, inline_source_issue
from openthesis.financial_validation import FinancialValidator


def source(numbers=None, *, extra_context="", unit="iso:USD", extra=""):
    company = Company("0001045810", "NVDA", "NVIDIA", market="US")
    numbers = numbers if numbers is not None else (
        '<ix:nonFraction name="g:NetIncomeLoss" contextRef="c" unitRef="u" '
        'format="ixt:num-dot-decimal" scale="6" decimals="-6">120,067</ix:nonFraction>'
    )
    raw = f'''<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:x="http://www.xbrl.org/2003/instance"
      xmlns:dei="http://xbrl.sec.gov/dei/2025" xmlns:g="http://fasb.org/us-gaap/2025"
      xmlns:iso="http://www.xbrl.org/2003/iso4217"
      xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"
      xmlns:di="http://xbrl.org/2006/xbrldi" xmlns:d="https://issuer.invalid/dimensions"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <x:context id="c"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">0001045810</x:identifier></x:entity>
        <x:period><x:startDate>2025-01-27</x:startDate><x:endDate>2026-01-25</x:endDate></x:period></x:context>
      <x:unit id="u"><x:measure>{unit}</x:measure></x:unit>
      <ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="c">2026</ix:nonNumeric>
      <ix:nonNumeric name="dei:DocumentFiscalPeriodFocus" contextRef="c">FY</ix:nonNumeric>
      {extra_context}{numbers}{extra}</html>'''.encode()
    filing = FilingDocument("sec-fixture", company.cik, "0001045810-26-000021", "10-K", "FY",
                            "2026-01-25", "2026-02-25", "nvda.htm", "https://www.sec.gov/Archives/nvda.htm",
                            content_hash=hashlib.sha256(raw).hexdigest())
    return company, filing, raw


def parsed(**kwargs):
    company, filing, raw = source(**kwargs)
    return extract_inline_xbrl(company, filing, raw)


def test_original_net_income_loss_is_parent_profit_with_explicit_exact_unit_and_fiscal_year():
    batch = parsed()
    assert not batch.diagnostics
    fact = batch.candidates[0].fact
    assert fact.concept == "parent_net_income"
    assert fact.fiscal_year == 2026 and fact.start_date == "2025-01-27"
    assert fact.normalization_lineage.normalized_value == "120067000000"
    assert fact.normalization_lineage.raw_value == "120067"
    assert fact.normalization_lineage.scale == "1000000"
    assert fact.currency == "USD"
    assert inline_source_issue(fact, batch.candidates[0].evidence[0], {ref.evidence_id: ref for ref in batch.evidence}) is None


def equity_presentation(disclosure: str):
    instant = '''<x:context id="instant"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">0001045810</x:identifier></x:entity>
      <x:period><x:instant>2026-01-25</x:instant></x:period></x:context>'''
    numbers = '''<table><tr><td>Total stockholders' equity</td><td>
      <ix:nonFraction name="g:StockholdersEquity" contextRef="instant" unitRef="u" scale="6" format="ixt:num-dot-decimal">415,265</ix:nonFraction>
      </td></tr></table>'''
    return parsed(numbers=numbers, extra_context=instant, extra=f'<div>{disclosure}</div>')


def test_explicit_nci_in_apic_disclosure_cannot_become_exact_parent_equity():
    batch = equity_presentation(
        'Total noncontrolling interests (NCI) in our consolidated subsidiaries were $3.4 billion. '
        'NCI and RNCI are included within additional paid-in capital.'
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.fact.concept == "total_equity"
    assert candidate.fact.normalization_lineage.normalized_value == "415265000000"
    refs = {ref.evidence_id: ref for ref in batch.evidence}
    assert inline_source_issue(candidate.fact, candidate.evidence[0], refs) is None
    assert inline_source_issue(replace(candidate.fact, concept="equity"), candidate.evidence[0], refs)


@pytest.mark.parametrize("disclosure", [
    'Noncontrolling interests may be included within additional paid-in capital.',
    'Noncontrolling interests are not included within additional paid-in capital.',
    'Noncontrolling interests are excluded from additional paid-in capital.',
    'NCI are included within additional paid-in capital.',
    'Noncontrolling interests are disclosed elsewhere.',
])
def test_hypothetical_negative_or_undefined_abbreviation_does_not_override_taxonomy(disclosure):
    batch = equity_presentation(disclosure)
    assert batch.candidates[0].fact.concept == "equity"


def test_semantic_disclosure_proof_is_required_for_parent_to_total_override():
    batch = equity_presentation(
        'Noncontrolling interests are included within additional paid-in capital.'
    )
    candidate = batch.candidates[0]
    assert candidate.fact.concept == "total_equity"
    unit = next(ref for ref in batch.evidence if ref.evidence_id.startswith("unit:"))
    proof = json.loads(unit.excerpt)
    proof.pop("presentation", None)
    refs = {ref.evidence_id: ref for ref in batch.evidence}
    refs[unit.evidence_id] = replace(unit, excerpt=json.dumps(proof))
    assert inline_source_issue(candidate.fact, candidate.evidence[0], refs)


def test_undimensioned_equity_rollforward_uses_the_same_explicit_issuer_definition():
    instant = '''<x:context id="instant"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">0001045810</x:identifier></x:entity>
      <x:period><x:instant>2026-01-25</x:instant></x:period></x:context>'''
    numbers = '''<table><tr><td>Balance as of January 25, 2026</td><td>
      <ix:nonFraction name="g:StockholdersEquity" contextRef="instant" unitRef="u" scale="6" format="ixt:num-dot-decimal">415,265</ix:nonFraction>
      </td></tr></table>'''
    batch = parsed(numbers=numbers, extra_context=instant,
                   extra='<div>Noncontrolling interests are included within additional paid-in capital.</div>')
    candidate = batch.candidates[0]
    assert candidate.fact.concept == "total_equity"
    assert inline_source_issue(candidate.fact, candidate.evidence[0], {ref.evidence_id: ref for ref in batch.evidence}) is None


def test_obsolete_inline_role_proof_cannot_skip_current_independent_validator():
    batch = equity_presentation('Noncontrolling interests are included within additional paid-in capital.')
    candidate = batch.candidates[0]
    legacy = replace(candidate.fact, parser_version="inline-xbrl-v1", concept="equity")
    assert inline_source_issue(legacy, candidate.evidence[0], {ref.evidence_id: ref for ref in batch.evidence})


@pytest.mark.parametrize("changes", [
    {"concept": "net_income"}, {"currency": "CNY"}, {"end_date": "2025-12-31"},
    {"fiscal_year": 2025}, {"statement": "balance_sheet"}, {"consolidated_scope": "parent", "scope": "parent"},
])
def test_independent_source_verifier_rejects_plausible_candidate_metadata_changes(changes):
    batch = parsed()
    candidate = batch.candidates[0]
    forged = replace(candidate.fact, **changes)
    assert inline_source_issue(forged, candidate.evidence[0], {ref.evidence_id: ref for ref in batch.evidence}) == "inline_xbrl_source_proof_mismatch"


def test_matching_forged_arithmetic_does_not_override_original_scale():
    batch = parsed()
    candidate = batch.candidates[0]
    lineage = replace(candidate.fact.normalization_lineage, scale="1000", normalized_value="120067000")
    forged = replace(candidate.fact, unit_scale=1000, value=120067000, normalization_lineage=lineage)
    assert inline_source_issue(forged, candidate.evidence[0], {ref.evidence_id: ref for ref in batch.evidence})


def test_foreign_issuer_context_and_duplicate_context_id_are_not_selectable():
    company, filing, raw = source()
    changed = raw.replace(b">0001045810<", b">0001326801<")
    filing.content_hash = hashlib.sha256(changed).hexdigest()
    assert not extract_inline_xbrl(company, filing, changed).candidates
    batch = parsed(extra='<x:context id="c"/>')
    assert not batch.candidates and "INLINE_DUPLICATE_ID:c" in batch.diagnostics


def test_nil_is_not_zero_and_unknown_transform_is_not_guessed():
    nil = '<ix:nonFraction name="g:NetIncomeLoss" contextRef="c" unitRef="u" xsi:nil="true"/>'
    batch = parsed(numbers=nil)
    assert not batch.candidates and "INLINE_NIL:0" in batch.diagnostics
    batch = parsed(numbers='<ix:nonFraction name="g:NetIncomeLoss" contextRef="c" unitRef="u" format="ixt:unknown">123</ix:nonFraction>')
    assert not batch.candidates and any("XBRL_TRANSFORM_UNSUPPORTED" in item for item in batch.diagnostics)


def test_dimensioned_revenue_is_preserved_without_becoming_consolidated_revenue():
    context = '''<x:context id="segment"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">0001045810</x:identifier>
      <x:segment><di:explicitMember dimension="d:BusinessAxis">d:CloudMember</di:explicitMember></x:segment></x:entity>
      <x:period><x:startDate>2025-01-27</x:startDate><x:endDate>2026-01-25</x:endDate></x:period></x:context>'''
    batch = parsed(extra_context=context, numbers='<ix:nonFraction name="g:Revenues" contextRef="segment" unitRef="u">100</ix:nonFraction>')
    assert len(batch.candidates) == 1
    fact = batch.candidates[0].fact
    assert fact.consolidated_scope.startswith("dimension:")
    proof = json.loads(batch.evidence[1].excerpt)
    assert "CloudMember" in proof["context_xml"]
    assert inline_source_issue(fact, batch.evidence[0], {ref.evidence_id: ref for ref in batch.evidence}) is None


def test_rebound_currency_prefix_is_resolved_at_measure_not_ancestor():
    company, filing, raw = source()
    raw = raw.replace(b'<x:measure>iso:USD', b'<x:measure xmlns:iso="https://issuer.invalid/not-currency">iso:USD')
    filing.content_hash = hashlib.sha256(raw).hexdigest()
    batch = extract_inline_xbrl(company, filing, raw)
    assert not batch.candidates and any("XBRL_UNIT_UNSUPPORTED" in item for item in batch.diagnostics)


def test_dtd_and_source_hash_mutation_do_not_trigger_entity_expansion_or_acceptance():
    company, filing, raw = source()
    assert extract_inline_xbrl(company, filing, raw + b" ").diagnostics == ("INLINE_SOURCE_HASH_MISMATCH",)
    raw = b'<!DOCTYPE html [<!ENTITY x "123">]>' + raw
    filing.content_hash = hashlib.sha256(raw).hexdigest()
    batch = extract_inline_xbrl(company, filing, raw)
    assert not batch.candidates and batch.diagnostics == ("INLINE_SOURCE_UNSUPPORTED_OR_INVALID",)


def test_explicit_document_focus_can_resolve_provisional_discovery_date_without_guessing():
    company, filing, raw = source()
    filing.period_end = ""
    batch = extract_inline_xbrl(company, filing, raw)
    assert batch.candidates[0].fact.end_date == "2026-01-25"
    assert batch.candidates[0].fact.fiscal_year == 2026
    filing.period_end = "2025-12-31"
    assert not extract_inline_xbrl(company, filing, raw).candidates


def test_known_dei_quarter_version_suffix_keeps_older_originals_readable():
    company, filing, raw = source()
    raw = raw.replace(b"http://xbrl.sec.gov/dei/2025", b"http://xbrl.sec.gov/dei/2021q4")
    filing.content_hash = hashlib.sha256(raw).hexdigest()
    batch = extract_inline_xbrl(company, filing, raw)
    assert batch.candidates and not batch.diagnostics


def test_native_candidate_uses_the_public_financial_validator_not_only_the_extractor():
    batch = parsed()
    candidate = batch.candidates[0]
    fact = candidate.fact
    validation = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: candidate.evidence[0]}, required_concepts={"parent_net_income"},
        expected_company_id=fact.company_cik, source_evidence=batch.evidence,
    ).validation
    assert len(validation.accepted) == 1 and not validation.quarantined
    forged = replace(fact, concept="net_income")
    validation = FinancialValidator().validate_group(
        [forged], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: candidate.evidence[0]}, required_concepts={"net_income"},
        expected_company_id=fact.company_cik, source_evidence=batch.evidence,
    ).validation
    assert validation.quarantined and "inline_xbrl_source_proof_mismatch" in validation.issues


def test_payment_component_sign_is_registered_without_becoming_total_capex():
    batch = parsed(numbers='<ix:nonFraction name="g:PaymentsToAcquirePropertyPlantAndEquipment" contextRef="c" unitRef="u" sign="-">100</ix:nonFraction>')
    candidate = batch.candidates[0]
    fact = candidate.fact
    assert fact.concept == "capital_expenditure_ppe" and fact.value == 100
    validation = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: candidate.evidence[0]}, required_concepts={fact.concept},
        expected_company_id=fact.company_cik, source_evidence=batch.evidence,
    ).validation
    assert validation.accepted == (fact,) and not validation.quarantined
    assert inline_source_issue(
        replace(fact, normalization_lineage=replace(fact.normalization_lineage, value_transform="identity")),
        candidate.evidence[0], {ref.evidence_id: ref for ref in batch.evidence},
    )


@pytest.mark.parametrize("change", ["focus_dtd", "focus_name", "invalid_scale"])
def test_untrusted_proof_fails_closed_without_expansion_or_decimal_exception(change):
    batch = parsed()
    candidate = batch.candidates[0]
    refs = {ref.evidence_id: ref for ref in batch.evidence}
    key = candidate.fact.normalization_lineage.unit_evidence_ids[0]
    proof = json.loads(refs[key].excerpt)
    fact = candidate.fact
    if change == "focus_dtd":
        proof["focus"]["year_xml"] = '<!DOCTYPE x [<!ENTITY x "2026">]><x>&x;</x>'
    elif change == "focus_name":
        proof["focus"]["year_xml"] = proof["focus"]["year_xml"].replace("DocumentFiscalYearFocus", "DocumentType")
    else:
        fact = replace(fact, normalization_lineage=replace(fact.normalization_lineage, scale="not-a-number"))
    refs[key] = replace(refs[key], excerpt=json.dumps(proof))
    assert inline_source_issue(fact, candidate.evidence[0], refs) == "inline_xbrl_source_proof_mismatch"
