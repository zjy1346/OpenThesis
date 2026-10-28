"""A real short-name issuer has legal identity and CAS basis on separate pages."""
from hashlib import sha256
from dataclasses import replace
import json
from pathlib import Path

import pdfplumber
import pytest

from openthesis.domain import Company, FilingDocument, FinancialFact
from openthesis.financial_sources.document_units import DocumentUnitResolution
from openthesis.financial_statutory_currency import (
    _LAWS,
    _LEGACY_VERSION,
    _proof_id,
    _basis_proves_cny,
    bind_statutory_presentation,
)


def test_wuliangye_original_cross_page_legal_evidence_is_captured():
    path = Path("build/acceptance/cn-filings/000858-2025FY.pdf")
    original_hash = sha256(path.read_bytes()).hexdigest()
    assert original_hash == "09133e1f44b3bb4b2cebe211529ad68f5b04b6be10b43870f2a78c947d5910a4"
    company = Company("CN_A:SZSE:000858.SZ", "000858.SZ", "五粮液", "SZSE",
        market="CN_A", security_id="CN_A:SZSE:000858.SZ", accounting_standard="CAS")
    filing = FilingDocument("szse:wuliangye-original", company.security_id, "wuliangye-original",
        "ANNUAL_REPORT", "FY", "2025-12-31", "2026-04-30", "2025年度报告全文",
        "https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-30/7b179575-3e07-4607-8d7e-d1bb9c4c8786.PDF",
        local_path=str(path), content_hash=original_hash)
    with pdfplumber.open(path) as document:
        # Keep physical page identities, not a source-less concatenation.
        texts = [""] * len(document.pages)
        for index in (5, 70):
            texts[index] = document.pages[index].extract_text() or ""
    resolved = bind_statutory_presentation(DocumentUnitResolution(), texts, company, filing)
    assert resolved.statutory_presentation, "issuer identity on p6 and issuer CAS basis on p71 must compose"
    assert {ref.locator for ref in resolved.statutory_presentation} >= {"page:6", "page:71"}
    assert all(ref.content_hash == original_hash for ref in resolved.statutory_presentation)


def test_wuliangye_v2_proof_passes_the_pure_validator():
    path = Path("build/acceptance/cn-filings/000858-2025FY.pdf")
    original_hash = sha256(path.read_bytes()).hexdigest()
    company = Company(
        "CN_A:SZSE:000858.SZ", "000858.SZ", "五粮液", "SZSE",
        market="CN_A", security_id="CN_A:SZSE:000858.SZ", accounting_standard="CAS",
    )
    filing = FilingDocument(
        "szse:wuliangye-original", company.security_id, "wuliangye-original",
        "ANNUAL_REPORT", "FY", "2025-12-31", "2026-04-30", "2025年度报告全文",
        "https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-30/7b179575-3e07-4607-8d7e-d1bb9c4c8786.PDF",
        content_hash=original_hash,
    )
    with pdfplumber.open(path) as document:
        texts = ["" for _ in document.pages]
        for index in (5, 70):
            texts[index] = document.pages[index].extract_text() or ""
    resolved = bind_statutory_presentation(DocumentUnitResolution(), texts, company, filing)
    proof = next(
        ref for ref in resolved.statutory_presentation
        if ref.evidence_id.startswith("unit:statutory:")
    )
    fact = FinancialFact(
        "wuliangye-revenue-2025", company.security_id, "revenue", "营业收入",
        1.0, "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31",
        filing.filed_at, filing.accession_number, filing.source_url,
        market="CN_A", consolidated_scope="consolidated", currency="CNY",
    )
    evidence = {ref.evidence_id: ref for ref in resolved.statutory_presentation}
    assert _basis_proves_cny(proof, fact, evidence) is True


def test_v2_proof_rejects_identity_period_currency_hash_and_role_tampering():
    # Use the existing Page/Pdf-style parser fixture so this exercises the
    # same proof objects as the normal 3.0.3 statutory tests.
    from test_statutory_presentation_currency_303 import parse

    _company, filing, facts, refs = parse()
    fact = facts[0]
    proof = next(ref for ref in refs if ref.evidence_id.startswith("unit:statutory:"))
    evidence = {ref.evidence_id: ref for ref in refs}

    def valid() -> tuple[dict[str, object], dict[str, object]]:
        payload = json.loads(proof.excerpt)
        return payload, evidence.copy()

    mutations = {
        "period": lambda payload, _evidence: payload.update(period_end="2024-12-31"),
        "security_code": lambda payload, _evidence: payload.update(security_code="000812"),
        "company": lambda payload, _evidence: payload.update(company_id="other-company"),
        "currency": lambda payload, _evidence: payload.update(currency="USD"),
        "hash": lambda _payload, _evidence: None,
        "missing_role": lambda payload, _evidence: payload.update(source_ids=payload["source_ids"][1:]),
        "role_exchange": lambda _payload, current: current.update({
            next(key for key, value in current.items() if value.title == "report-identity"):
                replace(next(value for value in current.values() if value.title == "report-identity"), title="domestic-cas-basis")
        }),
    }

    for label, mutate in mutations.items():
        payload, current = valid()
        mutate(payload, current)
        changed_proof = replace(
            proof,
            evidence_id=_proof_id(payload),
            content_hash=("b" * 64 if label == "hash" else proof.content_hash),
            excerpt=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        current[changed_proof.evidence_id] = changed_proof
        assert _basis_proves_cny(changed_proof, fact, current) is False, label


@pytest.mark.parametrize(
    "basis",
    [
        "子公司营业执照统一社会信用代码：91370000163099420E。本公司财务报表按照财政部颁布的企业会计准则编制。",
        "本公司营业执照统一社会信用代码：91370000163099420E。子公司财务报表按照财政部颁布的企业会计准则编制。",
    ],
)
def test_subsidiary_only_legal_or_cas_text_never_creates_statutory_proof(basis):
    from test_statutory_presentation_currency_303 import parse

    _company, _filing, _facts, refs = parse(basis=basis)
    assert not any(ref.evidence_id.startswith("unit:statutory:") for ref in refs)


def test_legacy_v1_proof_remains_readable_without_mutating_old_source_text():
    from test_statutory_presentation_currency_303 import parse

    _company, filing, facts, refs = parse()
    fact = facts[0]
    evidence = {ref.evidence_id: ref for ref in refs}
    source_text_before = {ref.evidence_id: ref.excerpt for ref in refs}
    identity = next(ref for ref in refs if ref.title == "report-identity")
    basis = next(ref for ref in refs if ref.title == "domestic-cas-basis")
    law_version = "prc-accounting-law-2024-art12"
    payload = {
        "schema": _LEGACY_VERSION,
        "law_version": law_version,
        "law_url": _LAWS[law_version],
        "company_id": fact.company_cik,
        "company_name": _company.name,
        "security_code": _company.ticker.split(".", 1)[0],
        "document_id": filing.document_id,
        "document_hash": filing.content_hash,
        "period_end": filing.period_end,
        "source_ids": [identity.evidence_id, basis.evidence_id],
        "currency": "CNY",
    }
    legacy_proof = replace(
        next(ref for ref in refs if ref.evidence_id.startswith("unit:statutory:")),
        evidence_id=_proof_id(payload),
        excerpt=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    evidence[legacy_proof.evidence_id] = legacy_proof
    assert _basis_proves_cny(legacy_proof, fact, evidence) is True
    assert source_text_before == {ref.evidence_id: ref.excerpt for ref in refs}
