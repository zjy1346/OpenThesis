"""The real preparation boundary retains genuine text, not missing-money fiction."""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest

from openthesis.application.financials import OfficialFinancialRequest
from openthesis.application.research_preparation import (
    NewResearchPreparer, ResearchPreparationRequest, ResearchPreparationUnavailable,
)
from openthesis.financial_admission import FinancialResearchView, ReadinessLevel, ResearchReadiness
from openthesis.financial_profile import build_financial_profile
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from test_official_financial_preparation import setup_preparer


@pytest.mark.parametrize("source_state", ["valid", "tampered", "foreign_issuer", "missing"])
def test_unavailable_financial_parse_only_continues_with_identity_bound_original_text(tmp_path, source_state):
    company, filing, adapter, _official, storage = setup_preparer(tmp_path)
    path = tmp_path / "source.htm"
    body = b"""<html><body><h1>Business overview</h1><p>The company manufactures
    industrial equipment and provides maintenance services to industrial customers.
    Revenue comes from equipment sales and maintenance contracts. Management plans
    new products and investment, but competition and customer concentration pose risks.</p></body></html>"""
    path.write_bytes(body)
    filing = replace(filing, local_path=str(path), content_hash=sha256(body).hexdigest())
    if source_state == "tampered":
        path.write_bytes(body + b"changed after capture")
    elif source_state == "foreign_issuer":
        filing = replace(filing, company_cik="foreign-issuer")
    elif source_state == "missing":
        filing = replace(filing, local_path=str(tmp_path / "not-present.htm"))
    profile = build_financial_profile([], reporting_currency=company.reporting_currency)
    view = FinancialResearchView((), (), (), profile, (), (), (), company.reporting_currency,
        ("DOCUMENT_CONTEXT_TIMEOUT",), ResearchReadiness(ReadinessLevel.BLOCKED, ()))
    prepared = SimpleNamespace(company=company, filings=(filing,), view=view, errors=(),
        discovery=SimpleNamespace(diagnostics=(), freshness="frozen-original"),
        publication={}, financial_generations={}, currency_lane_ambiguous=False, recovery_outcome=None)
    official = SimpleNamespace(prepare=lambda *_args, **_kwargs: prepared)
    request = ResearchPreparationRequest(company, builtin_pack(), (ModelConfig("offline"),),
        official=OfficialFinancialRequest(company, adapter, annual_history_years=1), annual_history_years=1)
    preparer = NewResearchPreparer(storage, official, None)
    if source_state != "valid":
        with pytest.raises(ResearchPreparationUnavailable):
            preparer.prepare(request)
        return
    result = preparer.prepare(request).request
    assert not result.financials.facts
    assert any("maintenance" in str(item.get("excerpt", "")).lower() for item in result.filing_evidence)
    assert all(item.get("kind") != "financial_fact" for item in result.filing_evidence)
    assert result.reproducibility["data_snapshot"]["financial_capabilities"]["financial_recovery_pending"]
