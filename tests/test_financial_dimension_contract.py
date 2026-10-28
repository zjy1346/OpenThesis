"""Typed source dimensions are retained, never borrowed by group totals."""
from dataclasses import replace
import json
import hashlib

import pytest

from openthesis.domain import FinancialFact
from openthesis.financial_inline_xbrl import extract_inline_xbrl, inline_source_issue
from openthesis.financial_validation import FinancialValidator
from test_inline_xbrl_source import source


def dimensioned(*, typed=False, prefix="d", value="cloud-001", children=None):
    member = (f'<di:typedMember dimension="{prefix}:BusinessAxis">'
        f'<{prefix}:BusinessDomain>{value}</{prefix}:BusinessDomain></di:typedMember>') if typed else (
        f'<di:explicitMember dimension="{prefix}:BusinessAxis">{prefix}:CloudMember</di:explicitMember>')
    if children is not None:
        member = f'<di:typedMember dimension="{prefix}:BusinessAxis">{children}</di:typedMember>'
    context = f'''<x:context id="segment" xmlns:{prefix}="https://issuer.invalid/dimensions">
      <x:entity><x:identifier scheme="http://www.sec.gov/CIK">0001045810</x:identifier>
      <x:segment>{member}</x:segment></x:entity><x:period>
      <x:startDate>2025-01-27</x:startDate><x:endDate>2026-01-25</x:endDate>
      </x:period></x:context>'''
    return source(extra_context=context, numbers=(
        '<ix:nonFraction name="g:Revenues" contextRef="segment" unitRef="u">100</ix:nonFraction>'))


def validate(fact, batch):
    refs = {item.evidence_id: item for item in batch.evidence}
    return FinancialValidator().validate_group([fact],
        (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: batch.candidates[0].evidence[0]}, required_concepts={"revenue"},
        expected_company_id=fact.company_cik, source_evidence=tuple(refs.values())).validation


def test_explicit_dimension_roundtrip_keeps_qualified_axis_member_and_source_proof():
    company, filing, raw = dimensioned()
    batch = extract_inline_xbrl(company, filing, raw)
    fact = batch.candidates[0].fact
    dimension = {"axis": "{https://issuer.invalid/dimensions}BusinessAxis",
        "member": "{https://issuer.invalid/dimensions}CloudMember", "kind": "explicit"}
    assert fact.to_dict()["dimensions"] == (dimension,)
    restored = FinancialFact(**json.loads(json.dumps(fact.to_dict())))
    assert restored.dimensions == fact.dimensions
    assert restored.consolidated_scope.startswith("dimension:")
    assert validate(restored, batch).accepted == (restored,)
    assert not validate(restored, batch).quarantined


def test_forged_dimension_metadata_cannot_inherit_valid_numeric_proof():
    company, filing, raw = dimensioned()
    batch = extract_inline_xbrl(company, filing, raw)
    fact = batch.candidates[0].fact
    forged = replace(fact, dimensions=({"axis": "{https://issuer.invalid/dimensions}BusinessAxis",
        "member": "{https://issuer.invalid/dimensions}AutomotiveMember", "kind": "explicit"},))
    assert validate(forged, batch).quarantined == (forged,)
    assert "inline_xbrl_source_proof_mismatch" in validate(forged, batch).issues


def test_typed_member_identity_is_independent_of_xml_prefix_spelling():
    facts = []
    for prefix in ("d", "business"):
        company, filing, raw = dimensioned(typed=True, prefix=prefix)
        batch = extract_inline_xbrl(company, filing, raw)
        assert batch.candidates
        fact = batch.candidates[0].fact
        assert inline_source_issue(fact, batch.candidates[0].evidence[0],
            {ref.evidence_id: ref for ref in batch.evidence}) is None
        facts.append(fact)
    assert facts[0].consolidated_scope == facts[1].consolidated_scope
    assert facts[0].dimensions == facts[1].dimensions
    assert facts[0].dimensions[0].kind == "typed"


@pytest.mark.parametrize("children", [
    "", "<d:BusinessDomain>cloud</d:BusinessDomain><d:BusinessDomain>auto</d:BusinessDomain>",
    "<d:BusinessDomain><d:Nested>cloud</d:Nested></d:BusinessDomain>",
    '<d:BusinessDomain xml:lang="en">cloud</d:BusinessDomain>',
    '<d:BusinessDomain> d:CloudMember </d:BusinessDomain>',
    '<d:BusinessDomain>' + "x" * 1025 + '</d:BusinessDomain>',
])
def test_unsupported_typed_member_shapes_are_observations_not_verified_facts(children):
    company, filing, raw = dimensioned(typed=True, children=children)
    batch = extract_inline_xbrl(company, filing, raw)
    assert not batch.candidates
    assert any("XBRL_TYPED_DIMENSION_UNSUPPORTED" in item for item in batch.diagnostics)


def test_dimension_member_and_exact_typed_text_are_not_interchangeable():
    facts = []
    for value in ("cloud-001", " cloud-001 ", "automotive-001"):
        company, filing, raw = dimensioned(typed=True, value=value)
        batch = extract_inline_xbrl(company, filing, raw)
        assert batch.candidates
        facts.append(batch.candidates[0].fact)
    assert len({fact.consolidated_scope for fact in facts}) == 3
    assert [json.loads(fact.dimensions[0].member)[1] for fact in facts] == [
        "cloud-001", " cloud-001 ", "automotive-001"]


def test_missing_legacy_dimensions_remain_empty_not_reverse_engineered_from_scope_hash():
    company, filing, raw = dimensioned()
    batch = extract_inline_xbrl(company, filing, raw)
    payload = batch.candidates[0].fact.to_dict()
    del payload["dimensions"]
    restored = FinancialFact(**payload)
    assert restored.dimensions == ()
    assert "inline_xbrl_source_proof_mismatch" in validate(restored, batch).issues


def disclosure_dataset(*, forge=False):
    from openthesis.financial_compiler import FinancialFactCompiler
    from openthesis.financial_contracts import CandidateBatch, CompilationPolicy

    company, filing, raw = dimensioned()
    extra = b'''<x:context id="instant"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">0001045810</x:identifier></x:entity>
      <x:period><x:instant>2026-01-25</x:instant></x:period></x:context>
      <x:unit id="shares"><x:measure>x:shares</x:measure></x:unit>
      <ix:nonFraction name="g:Revenues" contextRef="c" unitRef="u">1000</ix:nonFraction>
      <ix:nonFraction name="g:CommonStockSharesOutstanding" contextRef="instant" unitRef="shares">50</ix:nonFraction>'''
    raw = raw.replace(b'</html>', extra + b'</html>')
    filing = replace(filing, content_hash=hashlib.sha256(raw).hexdigest())
    batch = extract_inline_xbrl(company, filing, raw)
    if forge:
        batch = replace(batch, candidates=tuple(replace(candidate, fact=replace(candidate.fact,
            dimensions=())) if candidate.fact.dimensions else candidate for candidate in batch.candidates))
    dataset = FinancialFactCompiler().compile(company, (batch,), CompilationPolicy(
        filings=(filing,), reporting_currency="USD", required_concepts=frozenset({"revenue"})))
    return company, filing, dataset


def test_verified_dimensional_and_share_disclosures_are_available_without_replacing_group_totals():
    from openthesis.financial_admission import FinancialAdmission
    from openthesis.financial_evidence import build_fact_evidence

    company, filing, dataset = disclosure_dataset()
    assert [(fact.concept, fact.value) for fact in dataset.research_facts] == [("revenue", 1000)]
    assert {fact.concept for fact in dataset.supplemental_facts} == {"revenue", "shares_outstanding"}
    view = FinancialAdmission().admit(company, dataset, selected_filings=(filing,))
    assert view.profile.metrics[0]["revenue"] == 1000
    assert view.profile.disclosure_facts == dataset.supplemental_facts
    refs = build_fact_evidence([fact.to_dict() for fact in view.profile.disclosure_facts])
    dimension_ref = next(ref for ref in refs if ref["concept"] == "revenue")
    assert dimension_ref["dimensions"][0]["kind"] == "explicit"
    assert dimension_ref["scope"].startswith("dimension:")
    assert dimension_ref["normalization_lineage"]["normalized_value"] == "100"


def test_quarantined_dimensions_cannot_leak_into_supplemental_research():
    from openthesis.financial_admission import FinancialAdmission

    company, filing, dataset = disclosure_dataset(forge=True)
    assert dataset.quarantined_facts
    assert {fact.concept for fact in dataset.supplemental_facts} == {"shares_outstanding"}
    view = FinancialAdmission().admit(company, dataset, selected_filings=(filing,))
    assert not any(fact.dimensions for fact in view.profile.disclosure_facts)
    assert view.profile.metrics[0]["revenue"] == 1000


def test_actual_research_context_keeps_supplemental_identity_separate_from_metrics(tmp_path):
    from openthesis.financial_admission import FinancialAdmission
    from openthesis.packs import builtin_pack
    from openthesis.providers import ModelConfig
    from openthesis.research import ResearchWorkflow
    from openthesis.storage import Storage
    from test_research_workflow import SevenStageProvider, _disclosure_fixture

    class Capture(SevenStageProvider):
        def __init__(self):
            super().__init__()
            self.contexts = []

        def generate(self, system_prompt, user_prompt, *, json_mode=True):
            prompt = json.loads(user_prompt)
            self.contexts.append(prompt)
            return super().generate(system_prompt, user_prompt, json_mode=json_mode)

    company, filing, dataset = disclosure_dataset()
    profile = FinancialAdmission().admit(company, dataset, selected_filings=(filing,)).profile
    storage = Storage(tmp_path)
    storage.save_company(company)
    provider = Capture()
    run = ResearchWorkflow(storage, builtin_pack(), provider,
        ModelConfig(configured_model_id="offline.fixture", role="primary"), parallel_agents=False).run(
        company, profile, filing_evidence=_disclosure_fixture())
    assert provider.contexts
    context = provider.contexts[0]["research_context"]
    refs = context["evidence_partitions"]["quantitative_facts"]
    assert any(ref.get("dimensions") for ref in refs if ref.get("kind") == "financial_fact")
    assert any(ref.get("concept") == "shares_outstanding" for ref in refs)
    assert context["metrics"][0]["revenue"] == 1000
    summary = next(artifact for artifact in storage.get_artifacts(run.run_id)
                   if artifact["artifact_type"] == "deterministic-financial-summary")
    assert any(ref.get("dimensions") for ref in summary["content"]["evidence"])


def test_active_generation_roundtrip_revalidates_disclosures_without_exposing_them_as_totals(tmp_path):
    from openthesis.application.financials import compiler_validation_groups
    from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
    from openthesis.financial_contracts import CompilationPolicy
    from openthesis.storage import Storage, StorageCorruptionError

    company, filing, dataset = disclosure_dataset()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    storage.replace_financial_ingestion(company.cik, [filing.accession_number],
        list(dataset.research_facts), [], compiler_validation_groups(dataset.validations),
        list(dataset.evidence), list(dataset.supplemental_facts), generation_id="generation-one",
        complete_accessions={filing.accession_number})
    snapshot = storage.get_financial_research_snapshot(company.cik)
    assert {fact["value"] for fact in snapshot["facts"]} == {1000}
    assert {fact["value"] for fact in snapshot["supplemental_facts"]} == {100, 50}
    assert all(not fact["dimensions"] for fact in storage.get_facts(company.cik))
    # A physically present but inactive audit row must not leak into the view.
    with storage.connect() as db:
        stale = replace(dataset.supplemental_facts[0], fact_id="inactive-disclosure",
            generation_id="old-generation", usage_status="audit_only", value=999)
        storage._insert_facts(db, [stale])
    reopened = Storage(tmp_path)
    snapshot = reopened.get_financial_research_snapshot(company.cik)
    assert "inactive-disclosure" not in {fact["fact_id"] for fact in snapshot["supplemental_facts"]}
    rows = (*snapshot["facts"], *snapshot["supplemental_facts"])
    evidence = tuple(type(dataset.evidence[0])(**row) for row in reopened.get_financial_evidence())
    view = FinancialPipeline(engine=None).prepare(FinancialResearchSnapshot.from_facts(
        company, (filing,), tuple(FinancialFact(**row) for row in rows), evidence=evidence),
        CompilationPolicy(required_concepts=frozenset({"revenue"}), reporting_currency="USD"))
    assert view.profile.metrics[0]["revenue"] == 1000
    assert {fact.concept for fact in view.profile.disclosure_facts} == {"revenue", "shares_outstanding"}
    damaged_id = next(fact.fact_id for fact in dataset.supplemental_facts if fact.dimensions)
    with reopened.connect() as db:
        db.execute("UPDATE financial_facts SET dimensions_json='[{}]' WHERE fact_id=?", (damaged_id,))
    isolated = reopened.get_financial_research_snapshot(company.cik)
    assert damaged_id not in {fact["fact_id"] for fact in isolated["supplemental_facts"]}
    assert {fact["value"] for fact in isolated["facts"]} == {1000}
    healthy_supplemental_ids = {
        fact.fact_id for fact in dataset.supplemental_facts if fact.fact_id != damaged_id
    }
    assert healthy_supplemental_ids.issubset(
        {fact["fact_id"] for fact in isolated["supplemental_facts"]}
    )
    assert {fact["value"] for fact in isolated["supplemental_facts"]} == {
        fact.value for fact in dataset.supplemental_facts if fact.fact_id != damaged_id
    }
    assert any(
        issue.get("code") == "FINANCIAL_FACT_DIMENSIONS_INVALID"
        for issue in isolated["storage_issues"]
    )


def test_dimension_claim_metadata_is_checked_case_sensitively_and_not_as_a_group_total():
    from openthesis.financial_evidence import build_fact_evidence
    from openthesis.research import verify_agent_output

    company, filing, dataset = disclosure_dataset()
    fact = next(fact for fact in dataset.supplemental_facts if fact.dimensions)
    ref = build_fact_evidence([fact.to_dict()])[0]
    refs = {ref["evidence_id"]: ref}
    claim = {"text": "Dimension revenue is 100 USD.", "kind": "fact", "value": 100,
        "concept": "revenue", "evidence_ids": [ref["evidence_id"]]}
    valid = verify_agent_output({"claims": [claim]}, set(refs), evidence_records=refs)
    assert valid["verified_claim_count"] == 1
    scoped = valid["normalized_claims"][0]
    assert scoped["scope"] == fact.scope and scoped["dimensions"] == ref["dimensions"]
    for mutation in ({"scope": "consolidated"}, {"dimensions": [{**ref["dimensions"][0],
            "member": ref["dimensions"][0]["member"].lower()}]}):
        invalid = verify_agent_output({"claims": [{**claim, **mutation}]}, set(refs), evidence_records=refs)
        assert invalid["verified_claim_count"] == 0


def test_schema16_upgrade_does_not_invent_legacy_dimensions_or_modify_original_history(tmp_path):
    from openthesis.storage import SCHEMA_VERSION, Storage

    company, filing, dataset = disclosure_dataset()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    legacy = replace(dataset.research_facts[0], dimensions=(), usage_status="audit_only")
    storage.save_facts([legacy])
    before = storage.get_facts_audit(company.cik)[0]
    with storage.connect() as db:
        db.execute("ALTER TABLE financial_facts DROP COLUMN dimensions_json")
        db.execute("UPDATE metadata SET value='16' WHERE key='schema_version'")
    upgraded = Storage(tmp_path)
    after = upgraded.get_facts_audit(company.cik)[0]
    assert upgraded.migration_backup is not None and upgraded.migration_backup.exists()
    assert after["dimensions"] == ()
    assert after == before
    with upgraded.connect() as db:
        assert int(db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]) == SCHEMA_VERSION


@pytest.mark.parametrize("damaged_dimensions", [
    [{}], {"axis": "not-a-list"},
    [{"axis": "{urn:axis}Segment", "member": "{urn:member}One", "kind": "explicit"}] * 2,
])
def test_candidate_audit_obeys_the_same_dimension_identity_contract_as_active_rows(tmp_path, damaged_dimensions):
    from openthesis.storage import Storage, StorageCorruptionError

    company, filing, dataset = disclosure_dataset()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    result = storage.replace_financial_ingestion(company.cik, [filing.accession_number],
        [dataset.research_facts[0]], generation_id="candidate-without-promotion", complete_accessions=set())
    assert result["state"] == "incomplete"
    assert storage.get_facts(company.cik) == []
    # Legacy absence is unknown, not a reconstructed hash-based dimension.
    with storage.connect() as db:
        payload = json.loads(db.execute("SELECT candidate_json FROM financial_ingestion_candidates").fetchone()[0])
        payload["accepted_facts"][0].pop("dimensions", None)
        db.execute("UPDATE financial_ingestion_candidates SET candidate_json=?", (json.dumps(payload),))
    assert storage.get_facts_audit(company.cik)[0]["dimensions"] == ()
    with storage.connect() as db:
        payload["accepted_facts"][0]["dimensions"] = damaged_dimensions
        db.execute("UPDATE financial_ingestion_candidates SET candidate_json=?", (json.dumps(payload),))
    with pytest.raises(StorageCorruptionError, match="FINANCIAL_FACT_DIMENSIONS_INVALID"):
        storage.get_facts_audit(company.cik)
