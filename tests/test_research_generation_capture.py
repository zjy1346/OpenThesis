"""A concurrent repair must not relabel already captured research material."""
import pytest

from openthesis.application.financials import OfficialFinancialRequest
from openthesis.application.research_preparation import NewResearchPreparer, ResearchPreparationRequest
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from test_official_financial_preparation import setup_preparer


def test_repair_after_material_capture_does_not_replace_its_generation_identity(tmp_path):
    company, filing, source, official, storage = setup_preparer(tmp_path)
    request = OfficialFinancialRequest(company, source, annual_history_years=1)
    captured = {}

    class RepairImmediatelyAfterCapture:
        def prepare(self, *args, **kwargs):
            material = official.prepare(*args, **kwargs)
            captured["before"] = storage.get_financial_research_snapshot(company.cik)
            repaired = official.prepare(OfficialFinancialRequest(
                company, source, annual_history_years=1, force=True))
            captured["after"] = storage.get_financial_research_snapshot(company.cik)
            assert repaired.view.model_ready
            return material

    prepared = NewResearchPreparer(storage, RepairImmediatelyAfterCapture(), None).prepare(
        ResearchPreparationRequest(company, builtin_pack(), (ModelConfig("model"),),
            official=request, annual_history_years=1))
    before, after = captured["before"], captured["after"]
    assert before["generations"] != after["generations"]
    assert prepared.request.reproducibility["data_snapshot"]["financial_generations"] == before["generations"]
    facts = prepared.request.financials.facts
    assert {fact.generation_id for fact in facts} == {before["generations"][filing.accession_number]}
    assert {item["content_hash"] for item in prepared.request.filing_evidence
            if item.get("content_hash")} == {filing.content_hash}
    assert all(fact.source_document == filing.primary_document
               and fact.source_url == filing.source_url for fact in facts)
    assert {fact.concept: fact.value for fact in facts}["parent_net_income"] == 100


def test_official_material_reports_only_the_generations_captured_with_its_rows(tmp_path):
    company, filing, source, official, storage = setup_preparer(tmp_path)
    result = official.prepare(OfficialFinancialRequest(company, source, annual_history_years=1))
    expected = storage.get_financial_research_snapshot(company.cik)["generations"]
    assert dict(result.financial_generations) == expected
    official.prepare(OfficialFinancialRequest(company, source, annual_history_years=1, force=True))
    assert dict(result.financial_generations) == expected
    assert dict(result.financial_generations) != storage.get_financial_research_snapshot(company.cik)["generations"]
    assert {fact.generation_id for fact in result.view.facts} == {expected[filing.accession_number]}


def test_material_preparation_cannot_read_proofs_from_a_later_database_view(tmp_path):
    company, filing, source, official, storage = setup_preparer(tmp_path)
    def unrelated_read(*_args, **_kwargs):
        pytest.fail("FACTS_AND_PROOFS_READ_SEPARATELY")
    storage.get_financial_evidence = unrelated_read
    prepared = official.prepare(OfficialFinancialRequest(company, source, annual_history_years=1))
    assert prepared.view.model_ready
    assert {ref.content_hash for ref in prepared.view.evidence} == {filing.content_hash}
    assert all(fact.generation_id == dict(prepared.financial_generations)[fact.accession_number]
               for fact in prepared.view.facts)
