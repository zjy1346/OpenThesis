"""Original CAS/legal identity is required, never ticker -> currency."""
from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from openthesis.domain import Company, FilingDocument
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_ingestion import FinancialIngestionEngine, _manifest_for
from test_pdf_statement_semantics import Page, Pdf


IDENTITY = "冰轮环境技术股份有限公司 2025年年度报告全文 股票代码：000811"
BASIS = "公司营业执照统一社会信用代码：91370000163099420E。本公司财务报表按照财政部颁布的企业会计准则编制。"


def parse(*, identity=IDENTITY, basis=BASIS, unit="单位：元", market="CN_A", currency="CNY", period="FY", name="冰轮环境"):
    company = Company("000811.SZ", "000811.SZ", name, "SZSE", market=market,
                      reporting_currency=currency, accounting_standard="CAS")
    filing = FilingDocument("original", company.cik, "original", "ANNUAL_REPORT", period,
                            "2025-12-31", "2026-04-15T00:00:00+00:00", "2025 年年度报告", "https://example.invalid/original.pdf",
                            content_hash="a" * 64)
    pages = [Page([(identity,)]), Page([("合并利润表",), ("2025年12月31日",), (unit,),
                                      ("项目", "2025", "2024"), ("营业收入", "100", "90")]),
             Page([(basis,)])]
    with patch("pdfplumber.open", return_value=Pdf(pages)):
        facts, refs = FinancialIngestionEngine()._parse_pdf_ast(
            "nonexistent-statutory-fixture.pdf", company, filing, _manifest_for(filing),
            index_precomputed=True, candidate_pages=frozenset({2}))
    return company, filing, facts, refs


def compile_rows(company, filing, facts, refs):
    return FinancialFactCompiler().compile_facts(company, (filing,), facts, evidence=refs,
                                                required_concepts=frozenset({"revenue"}))


def test_cas_original_legal_basis_recovers_yuan_without_rewriting_original():
    company, filing, facts, refs = parse(currency="")
    assert len(facts) == 2
    assert all(fact.currency == "CNY" and fact.unit_scale == 1 for fact in facts)
    assert {fact.value for fact in facts} == {100, 90}
    assert all(ref.excerpt == "单位：元" for ref in refs if ref.evidence_id.startswith("unit:")
               and not ref.evidence_id.startswith("unit:statutory:"))
    proof = next(ref for ref in refs if ref.evidence_id.startswith("unit:statutory:"))
    payload = json.loads(proof.excerpt)
    assert payload["law_version"] == "prc-accounting-law-2024-art12"
    assert payload["company_id"] == company.security_id
    result = compile_rows(company, filing, facts, refs)
    assert len(result.accepted_facts) == 2
    assert not result.quarantined_facts


@pytest.mark.parametrize("change", [
    {"market": "HK"}, {"market": "US"}, {"name": "另一家公司"},
    {"identity": IDENTITY.replace("000811", "000812")},
    {"identity": IDENTITY.replace("年度报告", "摘要")},
    {"basis": "本公司记账本位币为人民币。本附注金额单位均为人民币元。"},
    {"basis": BASIS.replace("公司营业执照", "子公司营业执照")},
    {"basis": BASIS + "本公司注册于香港。"},
    {"basis": BASIS.replace("企业会计准则", "国际财务报告准则")},
    {"unit": "单位：千元"},
])
def test_nonapplicable_or_ambiguous_basis_never_fills_currency_or_scale(change):
    company, filing, facts, refs = parse(**change)
    result = compile_rows(company, filing, facts, refs)
    assert not result.accepted_facts


def test_explicit_usd_header_keeps_usd_and_never_uses_cny_statute():
    company, filing, facts, refs = parse(unit="单位：美元元", currency="USD")
    assert all(fact.currency == "USD" for fact in facts)
    assert all(not any(key.startswith("unit:statutory:") for key in fact.normalization_lineage.unit_evidence_ids)
               for fact in facts)
    result = compile_rows(company, filing, facts, refs)
    assert len(result.accepted_facts) == 2


def test_company_without_this_prefix_is_the_same_bound_issuer_not_a_subsidiary():
    company, filing, facts, refs = parse(basis=BASIS.replace("本公司财务报表", "公司财务报表"))
    assert len(compile_rows(company, filing, facts, refs).accepted_facts) == 2


@pytest.mark.parametrize("mutation", ["proof_currency", "proof_version", "foreign_source", "missing_basis", "forged_amount"])
def test_compiler_independently_rejects_forged_or_unbound_legal_proof(mutation):
    company, filing, facts, refs = parse()
    current = next(fact for fact in facts if fact.fiscal_year == 2025)
    changed = list(refs)
    if mutation in {"proof_currency", "proof_version"}:
        proof = next(ref for ref in refs if ref.evidence_id.startswith("unit:statutory:"))
        value = json.loads(proof.excerpt)
        value["currency" if mutation == "proof_currency" else "law_version"] = "wrong"
        changed = [replace(ref, excerpt=json.dumps(value)) if ref.evidence_id == proof.evidence_id else ref for ref in refs]
    elif mutation == "foreign_source":
        changed = [replace(ref, content_hash="b" * 64) if ref.evidence_id.startswith("statutory-source:") else ref for ref in refs]
    elif mutation == "missing_basis":
        changed = [ref for ref in refs if ref.title != "domestic-cas-basis"]
    else:
        current = replace(current, value=1000, normalization_lineage=replace(
            current.normalization_lineage, raw_value="1000", normalized_value="1000"))
    result = compile_rows(company, filing, (current,), changed)
    assert not result.accepted_facts
