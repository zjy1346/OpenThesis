"""Native optional cells must survive a successful structured core feed."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from openthesis.domain import EvidenceRef
from openthesis.financial_contracts import CandidateBatch, CompilationPolicy, FactCandidate, GapStageKind, QuantityKind, SourceAmount
from openthesis.financial_ingestion import FinancialCandidateCollection
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from openthesis.vision_financials import VisionExtractionResult, VisionPageRequest
from test_financial_pipeline import _fact, _filing, _subject


def prepare(*, capex=True, unproven=False, foreign=False):
    filing = replace(_filing(), content_hash="a" * 64)
    revenue = _fact(filing)
    purchase = replace(revenue, fact_id="original-capex", concept="capital_expenditure",
        reported_concept="Purchase of property, plant and equipment", value=25,
        statement="cash_flow", source_page=4, raw_text="Purchase of property, plant and equipment 25 CNY FY2025",
        unit_provenance="unknown" if unproven else "structured_normalized")
    if foreign:
        purchase = replace(purchase, company_cik="different-issuer")
    if unproven:
        # An actual malformed normalization proof, not a legacy structured
        # amount merely relabelled with an unused provenance string.
        purchase = replace(purchase, normalization_lineage=SourceAmount(
            "250", "1", QuantityKind.MONEY, "CNY", ("unit:missing-capex",)).normalize())
    reference = EvidenceRef("fact:" + purchase.fact_id, filing.document_id, filing.source_url,
        purchase.reported_concept, "page:4", purchase.raw_text, filing.filed_at, filing.content_hash)

    class Collector:
        def collect_candidate_batches(self, *_args, **_kwargs):
            native = CandidateBatch(filing, (FactCandidate(purchase, (reference,), "pdf-ast"),) if capex else (),
                                    (reference,) if capex else ())
            return FinancialCandidateCollection((), {filing.document_id: (
                CandidateBatch(filing, (FactCandidate(revenue),)), native)})

    calls = []

    class Vision:
        def extract(self, _company, _filing, pages, *_args, **_kwargs):
            calls.append(tuple(page.original_page for page in pages))
            return VisionExtractionResult()

    config = SimpleNamespace(enabled=True, consent=True, timeout_seconds=1, validate=lambda: None)
    snapshot = FinancialResearchSnapshot(_subject(), (filing,), vision_config=config,
        vision_fallback=Vision(), authorized_source_stages=frozenset({GapStageKind.MINERU}))
    with patch("openthesis.financial_ingestion._vision_failed_pages", return_value=(VisionPageRequest(4, b"page"),)) as plan:
        result = FinancialPipeline(Collector()).prepare(snapshot, CompilationPolicy(required_concepts=frozenset({"revenue"})))
    return result, calls, plan


def test_precollected_native_optional_cell_is_not_lost_when_primary_core_is_complete():
    result, calls, _ = prepare()
    assert result.canonical_dataset.allow_ai
    assert {fact.concept for fact in result.facts} == {"revenue", "capital_expenditure"}
    assert next(fact.value for fact in result.facts if fact.concept == "capital_expenditure") == 25
    assert not calls, "valid local cells require no cloud request"


def test_disclosed_but_unadmitted_optional_cell_can_repair_without_failing_the_whole_core():
    result, calls, plan = prepare(unproven=True)
    assert not result.canonical_dataset.allow_ai, "the incomplete audit group is not complete research"
    assert {fact.concept for fact in result.facts} == {"revenue"}
    assert calls == [(4,)], "dataset.allow_ai must not suppress an actual disclosed local gap"
    assert "cash_flow" in " ".join(plan.call_args.args[2])
    assert any(fact.concept == "capital_expenditure" for fact in result.quarantined_facts)


def test_undisclosed_optional_cell_or_foreign_candidate_never_triggers_an_upload():
    for options in ({"capex": False}, {"unproven": True, "foreign": True}):
        result, calls, plan = prepare(**options)
        assert {fact.concept for fact in result.facts} == {"revenue"}
        if options.get("foreign"):
            assert any("issuer_identity_mismatch" in item.issues
                       for item in result.canonical_dataset.validations)
        assert not calls
        plan.assert_not_called()
