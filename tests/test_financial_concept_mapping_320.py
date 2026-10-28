import os
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from openthesis.domain import Company, EvidenceRef, FilingDocument, FinancialFact
from openthesis.application.financials import OfficialFinancialPreparer
from openthesis.financial_compiler import CoveragePlanner
from openthesis.financial_compatibility import CompatibilityPackError, _freeze_rules
from openthesis.financial_contracts import PDF_PARSER_VERSION, STATEMENT_FOR
from openthesis.financial_checkpoint import (
    CheckpointKey, PdfWindow, WindowCheckpoint, WindowCheckpointStore,
)
from openthesis.financial_ingestion import FinancialIngestionEngine, _labels_for_rules, _positioned_pdf_rows, _manifest_for
from openthesis.financial_cell_repair import mapping_reference
from openthesis.financial_pipeline import FinancialPipeline
from openthesis.financial_validation import FinancialValidator
from openthesis.financial_sources.document_units import discover_document_units
from openthesis.financial_sources.note_disclosures import note_row_disclosures
from openthesis.financial_sources.note_disclosures import extract_note_candidates, note_source_issue
from openthesis.financials import calculate_metrics
from openthesis.financial_taxonomy import (
    FINANCIAL_LABEL_ALIASES,
    FINANCIAL_MAPPING_VERSION,
    cost_row_concept,
)
from openthesis.market_financials import parse_financial_pages
from openthesis.vision_financials import VisionPageRequest, parse_vision_json


def test_operating_cost_and_cost_of_revenue_are_mutually_exclusive():
    assert cost_row_concept("营业成本") == "cost_of_revenue"
    assert cost_row_concept("其中：营业成本") == "cost_of_revenue"
    assert cost_row_concept("營業成本") == "cost_of_revenue"
    assert cost_row_concept("cost of revenue") == "cost_of_revenue"
    assert cost_row_concept("营业总成本") == "total_operating_cost"
    assert cost_row_concept("營業總成本") == "total_operating_cost"
    assert cost_row_concept("total operating costs") == "total_operating_cost"
    assert cost_row_concept("总成本") is None
    assert cost_row_concept("营业总成本（本期金额）") == "total_operating_cost"
    assert cost_row_concept("营业成本率") is None
    assert cost_row_concept("营业总成本同比增长率") is None
    assert cost_row_concept("营业成本占营业收入比例") is None
    assert cost_row_concept("cost of revenue growth") is None


def _company():
    return Company(
        "CN_A:SZSE:000811.SZ", "000811.SZ", "冰轮环境", "SZSE",
        market="CN_A", listing_currency="CNY", reporting_currency="CNY",
        accounting_standard="CAS",
    )


def _filing(path=""):
    return FilingDocument(
        "cninfo:1225092828", _company().security_id, "1225092828",
        "ANNUAL_REPORT", "FY", "2025-12-31", "2026-04-24",
        "2025 Annual Report", "https://www.cninfo.com.cn/1225092828.pdf",
        local_path=path,
    )


def test_cost_concepts_have_distinct_contracts_and_no_compatibility_reinfection():
    assert "营业总成本" not in FINANCIAL_LABEL_ALIASES["cost_of_revenue"]
    assert "营业总成本" in FINANCIAL_LABEL_ALIASES["total_operating_cost"]
    assert "cost_of_revenue" in STATEMENT_FOR
    assert STATEMENT_FOR["total_operating_cost"] == "income_statement"
    assert FINANCIAL_MAPPING_VERSION != "canonical-taxonomy-v1"

    hostile_snapshot = type("Rules", (), {
        "taxonomy_aliases": (("cost_of_revenue", ("营业总成本", "营业总成本定制别名", "issuer cost")),),
    })()
    labels = _labels_for_rules(hostile_snapshot)
    assert "营业总成本" not in labels["cost_of_revenue"]
    assert "营业总成本定制别名" not in labels["cost_of_revenue"]
    assert "issuer cost" in labels["cost_of_revenue"]

    with pytest.raises(CompatibilityPackError, match="fixed accounting concept"):
        _freeze_rules({"taxonomy_aliases": {"cost_of_revenue": ["营业总成本定制别名"]}})
    with pytest.raises(CompatibilityPackError, match="fixed accounting concept"):
        _freeze_rules({"taxonomy_aliases": {"operating_income": ["营业总成本"]}})
    with pytest.raises(CompatibilityPackError, match="fixed accounting concept"):
        _freeze_rules({"taxonomy_aliases": {"total_operating_cost": ["营业总成本同比增长率"]}})


def test_market_extractor_keeps_reported_total_and_cost_of_revenue_separate():
    text = (
        "合并利润表\n单位：元\n项目 2025年度 2024年度\n"
        "二、营业总成本 6,405,235,547.94 6,049,330,568.79\n"
        "其中：营业成本 5,152,713,900.37 4,801,489,471.81\n"
    )
    facts, _evidence = parse_financial_pages([(72, text)], _filing(), _company())
    by_concept = {fact.concept: fact.value for fact in facts}
    assert by_concept["cost_of_revenue"] == pytest.approx(5_152_713_900.37)
    assert by_concept["total_operating_cost"] == pytest.approx(6_405_235_547.94)


def test_total_operating_cost_is_not_used_as_default_gross_profit_input():
    metrics = calculate_metrics([
        {"fact_id": "revenue", "concept": "revenue", "value": 100.0,
         "fiscal_year": 2025, "fiscal_period": "FY", "end_date": "2025-12-31"},
        {"fact_id": "total-cost", "concept": "total_operating_cost", "value": 60.0,
         "reported_concept": "营业总成本", "fiscal_year": 2025,
         "fiscal_period": "FY", "end_date": "2025-12-31"},
    ])[0]
    assert metrics["gross_margin"] is None
    assert metrics["gross_profit_basis"] is None


def test_gross_profit_requires_same_source_identity_period_currency_and_scope():
    def money(concept, value, **overrides):
        fact = {
            "fact_id": concept, "concept": concept, "value": value,
            "company_cik": "CN:SZSE:000811.SZ", "accession_number": "1225092828",
            "generation_id": "generation-current", "parser_version": PDF_PARSER_VERSION,
            "source_document": "1225092828.pdf", "currency": "CNY",
            "unit_provenance": "explicit", "unit_scale": 1.0,
            "scope": "consolidated", "consolidated_scope": "consolidated",
            "fiscal_year": 2025, "fiscal_period": "FY",
            "start_date": "2025-01-01", "end_date": "2025-12-31",
            "filed_at": "2026-04-24",
        }
        fact.update(overrides)
        return fact

    same = calculate_metrics([
        money("revenue", 100.0, unit_scale=1000.0),
        money("cost_of_revenue", 60.0, unit_scale=1.0),
    ])[0]
    assert same["gross_margin"] == pytest.approx(0.4)
    assert same["gross_profit_basis"] == "revenue_minus_cost_of_revenue"

    mismatches = (
        {"company_cik": "CN:SZSE:000002.SZ"},
        {"entity": "另一个报告主体"},
        {"generation_id": "generation-other"},
        {"accession_number": "other-accession", "generation_id": ""},
        {"start_date": "2024-01-01"},
        {"end_date": "2024-12-31"},
        {"currency": "USD"},
        {"consolidated_scope": "parent_only", "scope": "parent_only"},
    )
    for mismatch in mismatches:
        incompatible = calculate_metrics([
            money("revenue", 100.0),
            money("cost_of_revenue", 60.0, **mismatch),
        ])[0]
        assert incompatible["gross_margin"] is None, mismatch
        assert incompatible["gross_profit_basis"] is None, mismatch

    same_named = calculate_metrics([
        money("revenue", 100.0, entity="冰轮环境"),
        money("cost_of_revenue", 60.0, entity="冰轮环境"),
    ])[0]
    assert same_named["gross_margin"] == pytest.approx(0.4)


def test_explicit_non_cost_row_is_not_rejected_by_an_unrelated_cost_ratio_heading():
    company, filing = _company(), _filing()
    filing.content_hash = "f" * 64
    page = VisionPageRequest(72, b"original-page")
    row = {
        "concept": "revenue", "label": "营业收入", "value": "100",
        "unit_scale": 1, "period_end": "2025-12-31", "currency": "CNY",
        "statement": "income_statement", "scope": "consolidated",
        "unit_text": "单位：元", "scope_text": "合并利润表",
        "period_text": "2025年度", "original_page": 72,
        "raw_text": "2025年度 营业成本增长率 3% 合并利润表 单位：元 营业收入 100",
    }
    parsed = parse_vision_json({"facts": [row]}, company, filing, [page])
    assert len(parsed.facts) == 1
    fact = parsed.facts[0]
    row_ref = next(ref for ref in parsed.evidence if ref.evidence_id == f"fact:{fact.fact_id}")
    validation = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period, "consolidated", "CNY"),
        evidence_map={fact.fact_id: row_ref}, expected_company_id=fact.company_cik,
        source_evidence=parsed.evidence,
    )
    assert "cost_source_non_amount_label" not in validation.validation.issues
    assert "cost_source_concept_mismatch" not in validation.validation.issues


def test_cost_ratio_labels_are_rejected_at_vision_cell_and_validator_boundaries():
    company, filing = _company(), _filing()
    filing.content_hash = "f" * 64
    page = VisionPageRequest(72, b"original-page")
    base = {
        "concept": "cost_of_revenue", "label": "营业成本",
        "value": "5152713900.37", "unit_scale": 1,
        "period_end": "2025-12-31", "currency": "CNY",
        "statement": "income_statement", "scope": "consolidated",
        "unit_text": "单位：元", "scope_text": "合并利润表",
        "period_text": "2025年度", "original_page": 72,
        # The header is wider context, not the source row label. A percentage
        # heading elsewhere on the page must not invalidate an explicit row.
        "raw_text": "2025年度 毛利率25% 合并利润表 单位：元 二、营业成本 5,152,713,900.37",
    }
    parsed = parse_vision_json({"facts": [base]}, company, filing, [page])
    assert len(parsed.facts) == 1
    fact = parsed.facts[0]
    row_ref = next(ref for ref in parsed.evidence if ref.evidence_id == f"fact:{fact.fact_id}")
    invalid_label = replace(fact, reported_concept="营业成本同比增长率")
    validation = FinancialValidator().validate_group(
        [invalid_label],
        (fact.accession_number, fact.end_date, fact.fiscal_period, "consolidated", "CNY"),
        evidence_map={fact.fact_id: row_ref}, expected_company_id=fact.company_cik,
        source_evidence=parsed.evidence,
    )
    assert invalid_label not in validation.validation.accepted
    assert "cost_source_non_amount_label" in validation.validation.issues

    ratio = {**base, "label": "营业成本同比增长率", "raw_text":
             "2025年度 合并利润表 单位：元 营业成本同比增长率 6.8%"}
    assert parse_vision_json({"facts": [ratio]}, company, filing, [page]).facts == ()
    mislabeled = {**ratio, "concept": "operating_income"}
    assert parse_vision_json({"facts": [mislabeled]}, company, filing, [page]).facts == ()
    no_label = {key: value for key, value in ratio.items() if key != "label"}
    no_label["concept"] = "operating_income"
    assert parse_vision_json({"facts": [no_label]}, company, filing, [page]).facts == ()

    assert mapping_reference({
        "concept": "cost_of_revenue", "label": "营业成本同比增长率",
        "row_quote": "营业成本同比增长率 6.8%", "original_page": 72,
    }, filing, {72}) is None


def test_cost_mapping_cache_miss_is_document_scoped_and_prevents_healthy_short_circuit(tmp_path):
    company = _company()

    def cached_fact(accession, concept, label):
        return FinancialFact(
            fact_id=f"{accession}-{concept}", company_cik=company.security_id,
            concept=concept, reported_concept=label, value=1.0, unit="CNY",
            fiscal_year=2025, fiscal_period="FY", form_type="ANNUAL_REPORT",
            start_date="2025-01-01", end_date="2025-12-31", filed_at="2026-04-24",
            accession_number=accession, source_url=f"https://example.test/{accession}.pdf",
            statement="income_statement", parser_version=PDF_PARSER_VERSION,
            source_document=f"{accession}.pdf", raw_text=f"二、{label} 1.00",
        )

    affected = _filing()
    affected.content_hash = "a" * 64
    unaffected = FilingDocument(
        "cninfo:unaffected", company.security_id, "unaffected", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-04-24", "Annual Report",
        "https://example.test/unaffected.pdf", content_hash="b" * 64,
    )
    no_cost = FilingDocument(
        "cninfo:no-cost", company.security_id, "no-cost", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-04-24", "Annual Report",
        "https://example.test/no-cost.pdf", content_hash="c" * 64,
    )
    engine = FinancialIngestionEngine(cache_dir=tmp_path)
    affected_key = engine._parse_cache_key(affected.local_path, company, affected, affected.content_hash)
    unaffected_key = engine._parse_cache_key(unaffected.local_path, company, unaffected, unaffected.content_hash)
    no_cost_key = engine._parse_cache_key(no_cost.local_path, company, no_cost, no_cost.content_hash)
    engine._store_parse_cache(
        affected_key, [cached_fact(affected.accession_number, "cost_of_revenue", "营业总成本")], [],
    )
    engine._store_parse_cache(
        unaffected_key, [cached_fact(unaffected.accession_number, "cost_of_revenue", "营业成本")], [],
    )
    engine._store_parse_cache(no_cost_key, [cached_fact(no_cost.accession_number, "revenue", "营业收入")], [])
    for key in (affected_key, unaffected_key, no_cost_key):
        path = tmp_path / f"{key}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["financial_mapping_version"] = "canonical-taxonomy-v1"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    fresh_engine = FinancialIngestionEngine(cache_dir=tmp_path)
    assert fresh_engine._load_parse_cache(affected_key) is None
    # A correct-looking old COGS result is still stale: it predates the
    # separate total-operating-cost mapping and must be locally recompiled.
    assert fresh_engine._load_parse_cache(unaffected_key) is None
    assert fresh_engine._load_parse_cache(no_cost_key) is not None
    assert fresh_engine.pending_row_mappings(company, [affected]) == {affected.accession_number}
    assert fresh_engine.pending_row_mappings(company, [unaffected]) == {unaffected.accession_number}
    assert fresh_engine.pending_row_mappings(company, [no_cost]) == frozenset()

    # A cache entry emitted under the independent current mapping marker may
    # contain both cost families and remains a normal hit.
    current = FilingDocument(
        "cninfo:current", company.security_id, "current", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-04-24", "Annual Report",
        "https://example.test/current.pdf", content_hash="d" * 64,
    )
    current_key = engine._parse_cache_key(current.local_path, company, current, current.content_hash)
    engine._store_parse_cache(current_key, [
        cached_fact(current.accession_number, "cost_of_revenue", "营业成本"),
        cached_fact(current.accession_number, "total_operating_cost", "营业总成本"),
    ], [])
    assert fresh_engine._load_parse_cache(current_key) is not None

    # The same affected result may already be resident when the healthy-result
    # shortcut is queried; it must still request this exact source locally.
    in_memory = FinancialIngestionEngine()
    memory_key = in_memory._parse_cache_key(unaffected.local_path, company, unaffected, unaffected.content_hash)
    in_memory._parse_cache_memory[memory_key] = (
        [cached_fact(unaffected.accession_number, "cost_of_revenue", "营业成本")], [],
    )
    in_memory._parse_cache_mapping_versions[memory_key] = "canonical-taxonomy-v1"
    assert in_memory.pending_row_mappings(company, [unaffected]) == {unaffected.accession_number}

    # A failed legacy mapping can retain only its source-row evidence. Do not
    # certify an empty old cache as current merely because no fact survived.
    evidence_only = FinancialIngestionEngine(cache_dir=tmp_path / "evidence-only")
    source_ref = EvidenceRef("statement:cost", affected.document_id, affected.source_url,
        "original row", "page:72", "二、营业总成本 12.00 11.00", affected.filed_at, affected.content_hash)
    evidence_only._store_parse_cache(affected_key, [], [source_ref])
    path = tmp_path / "evidence-only" / f"{affected_key}.json"
    old_payload = json.loads(path.read_text("utf-8"))
    old_payload.pop("financial_mapping_version")
    path.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")
    reopened = FinancialIngestionEngine(cache_dir=tmp_path / "evidence-only")
    assert reopened._load_parse_cache(affected_key) is None
    assert reopened.pending_row_mappings(company, [affected]) == {affected.accession_number}


def test_checkpoint_cost_mapping_version_remaps_only_legacy_cost_windows(tmp_path):
    company = _company()

    def fact(concept, label):
        return FinancialFact(
            fact_id=f"{concept}-{label}", company_cik=company.security_id,
            concept=concept, reported_concept=label, value=1.0, unit="CNY",
            fiscal_year=2025, fiscal_period="FY", form_type="ANNUAL_REPORT",
            start_date="2025-01-01", end_date="2025-12-31", filed_at="2026-04-24",
            accession_number="1225092828", source_url="https://example.test/original.pdf",
            statement="income_statement", parser_version=PDF_PARSER_VERSION,
            source_document="original.pdf", raw_text=f"二、{label} 1.00",
        )

    store = WindowCheckpointStore(tmp_path)
    key = CheckpointKey("f" * 64, PDF_PARSER_VERSION, "rules-v1")
    old_cost = WindowCheckpoint(key, PdfWindow(0, (72,)), facts=(
        fact("cost_of_revenue", "营业成本"),
        fact("total_operating_cost", "营业总成本"),
    ))
    store.save(old_cost)
    assert store.load(key, 0, expected_pages=(72,)) is not None

    old_cost_path = store.path_for(key, 0)
    envelope = json.loads(old_cost_path.read_text(encoding="utf-8"))
    envelope["payload"]["financial_mapping_version"] = "canonical-taxonomy-v1"
    encoded = json.dumps(envelope["payload"], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    envelope["sha256"] = hashlib.sha256(encoded).hexdigest()
    old_cost_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    assert store.load(key, 0, expected_pages=(72,)) is None

    no_cost_key = CheckpointKey("e" * 64, PDF_PARSER_VERSION, "rules-v1")
    store.save(WindowCheckpoint(no_cost_key, PdfWindow(0, (1,)), facts=(
        fact("revenue", "营业收入"),
    )))
    no_cost_path = store.path_for(no_cost_key, 0)
    no_cost_envelope = json.loads(no_cost_path.read_text(encoding="utf-8"))
    no_cost_envelope["payload"].pop("financial_mapping_version")
    encoded = json.dumps(no_cost_envelope["payload"], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    no_cost_envelope["sha256"] = hashlib.sha256(encoded).hexdigest()
    no_cost_path.write_text(json.dumps(no_cost_envelope, ensure_ascii=False), encoding="utf-8")
    assert store.load(no_cost_key, 0, expected_pages=(1,)) is not None


def test_durable_legacy_facts_prevent_healthy_shortcut_without_parse_cache():
    company = _company()
    required = CoveragePlanner().plan(company).required_concepts
    accessions = ("legacy-cost", "legacy-no-cost", "current-cost")
    filings = tuple(FilingDocument(
        f"doc-{accession}", company.security_id, accession, "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-04-24", "Annual Report",
        f"https://example.test/{accession}.pdf", content_hash=(str(index + 1) * 64),
    ) for index, accession in enumerate(accessions))
    generations = {
        accessions[0]: "legacy-generation",
        accessions[1]: "legacy-generation-no-cost",
        accessions[2]: f"generation:financial-mapping:{FINANCIAL_MAPPING_VERSION}",
    }
    stored_facts = []
    validations = []
    for filing in filings:
        core = [SimpleNamespace(concept=concept) for concept in required]
        costs = []
        if filing.accession_number == accessions[0]:
            costs = [dict(accession_number=filing.accession_number,
                concept="cost_of_revenue", reported_concept="营业成本",
                raw_text="其中：营业成本 10 9", parser_version=PDF_PARSER_VERSION)]
            core.append(SimpleNamespace(concept="cost_of_revenue"))
        elif filing.accession_number == accessions[2]:
            costs = [
                dict(accession_number=filing.accession_number, concept="cost_of_revenue",
                     reported_concept="营业成本", raw_text="其中：营业成本 10 9",
                     parser_version=PDF_PARSER_VERSION),
                dict(accession_number=filing.accession_number, concept="total_operating_cost",
                     reported_concept="营业总成本", raw_text="二、营业总成本 12 11",
                     parser_version=PDF_PARSER_VERSION),
            ]
            core.extend((SimpleNamespace(concept="cost_of_revenue"),
                         SimpleNamespace(concept="total_operating_cost")))
        stored_facts.extend(costs)
        validations.append(SimpleNamespace(
            identity=(filing.accession_number, filing.period_end, filing.fiscal_period,
                      "consolidated", "CNY"),
            accepted=tuple(core),
        ))

    class StorageStub:
        def get_financial_research_snapshot(self, _key, *, include_evidence=False):
            return {"generations": generations, "facts": stored_facts,
                    "supplemental_facts": [], "parsing_observations": [{
                        "accession_number": filings[1].accession_number,
                        "period_end": filings[1].period_end,
                        "fiscal_period": filings[1].fiscal_period,
                        "source_hash": filings[1].content_hash,
                    }], "evidence": []}

        def get_facts_audit(self, _key):
            return []

    preparer = OfficialFinancialPreparer(
        StorageStub(), SimpleNamespace(), SimpleNamespace(verified_cache=lambda filing: filing),
        FinancialPipeline(FinancialIngestionEngine()),
    )
    view = SimpleNamespace(canonical_dataset=SimpleNamespace(validations=validations))
    # There is deliberately no parse cache. Durable generation IDs and fact
    # source labels alone schedule the old cost document for remapping, while
    # the no-cost legacy filing and current two-cost filing stay healthy.
    assert preparer._healthy_accessions(company, filings, view) == {
        "legacy-no-cost", "current-cost",
    }


@pytest.mark.parametrize("candidate_mode", ["quarantined_fact", "unverified_observation", "source_row_only"])
def test_legacy_cost_observations_trigger_only_the_bound_accession(candidate_mode):
    company = _company()
    filings = tuple(FilingDocument(
        f"doc-{accession}", company.security_id, accession, "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-04-24", "Annual Report",
        f"https://example.test/{accession}.pdf", content_hash=marker * 64,
    ) for accession, marker in (("candidate-cost", "a"), ("other", "b")))
    source_row = "二、营业总成本 12.00"
    evidence_id = ("fact:isolated-cost" if candidate_mode == "quarantined_fact" else
                   "statement-row:cost" if candidate_mode == "source_row_only" else "observation:cost")
    evidence = [{
        "evidence_id": evidence_id, "document_id": filings[0].document_id,
        "source_url": filings[0].source_url,
        "title": ("Unverified visual observation (not used in calculations): 营业总成本"
                  if candidate_mode == "unverified_observation" else "Candidate source row"),
        "locator": "page:72", "excerpt": source_row,
        "published_at": filings[0].filed_at, "content_hash": filings[0].content_hash,
        "bbox": None,
    }]
    candidate = {
        "fact_id": "isolated-cost", "accession_number": filings[0].accession_number,
        "concept": "total_operating_cost", "reported_concept": "营业总成本",
        "raw_text": source_row, "usage_status": "quarantined",
        "fact_validation_record": {"components": {"amount": {
            "evidence_ids": [evidence_id],
        }}},
    }

    class StorageStub:
        def get_financial_research_snapshot(self, _key, *, include_evidence=False):
            observations = [{
                "accession_number": filings[0].accession_number,
                "period_end": filings[0].period_end,
                "fiscal_period": filings[0].fiscal_period,
                "source_hash": filings[0].content_hash,
            }]
            return {
                "generations": {filing.accession_number: "legacy-generation"
                                for filing in filings},
                "facts": [], "supplemental_facts": [],
                "parsing_observations": observations,
                "evidence": evidence if include_evidence else [],
            }

        def get_facts_audit(self, _key):
            return [candidate] if candidate_mode == "quarantined_fact" else []

    preparer = OfficialFinancialPreparer(
        StorageStub(), SimpleNamespace(), SimpleNamespace(),
        FinancialPipeline(FinancialIngestionEngine()),
    )
    assert preparer._pending_persisted_cost_mappings(company, filings) == {
        filings[0].accession_number,
    }


def test_vision_claim_cannot_assign_total_operating_cost_to_cost_of_revenue():
    company, filing = _company(), _filing()
    page = VisionPageRequest(72, b"original-page")
    row = {
        "concept": "cost_of_revenue",
        "value": "6405235547.94",
        "unit_scale": 1,
        "period_end": "2025-12-31",
        "currency": "CNY",
        "statement": "income_statement",
        "scope": "consolidated",
        "unit_text": "单位：元",
        "scope_text": "合并利润表",
        "period_text": "2025年度",
        "raw_text": "2025年度 合并利润表 单位：元 二、营业总成本 6,405,235,547.94",
        "original_page": 72,
    }
    rejected = parse_vision_json({"facts": [row]}, company, filing, [page])
    assert rejected.facts == ()

    accepted = parse_vision_json(
        {"facts": [{**row, "concept": "total_operating_cost"}]},
        company, filing, [page],
    )
    assert len(accepted.facts) == 1
    assert accepted.facts[0].concept == "total_operating_cost"


def test_binglun_original_pages_replay_both_periods_when_configured():
    source = os.environ.get("OPENTHESIS_BINGLUN_2025_PDF", "")
    if not source or not Path(source).is_file():
        pytest.skip("set OPENTHESIS_BINGLUN_2025_PDF to the local original PDF")
    import pdfplumber

    filing = _filing(source)
    filing.content_hash = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    original_hash = filing.content_hash
    production_facts, _production_evidence = FinancialIngestionEngine()._parse_pdf_ast(
        source, _company(), filing, _manifest_for(filing),
        candidate_pages=frozenset({72}), index_precomputed=True,
    )
    expected_costs = {
        (2025, "cost_of_revenue"): 5_152_713_900.37,
        (2025, "total_operating_cost"): 6_405_235_547.94,
        (2024, "cost_of_revenue"): 4_801_489_471.81,
        (2024, "total_operating_cost"): 6_049_330_568.79,
    }
    structured = {(fact.fiscal_year, fact.concept): fact for fact in production_facts
                  if fact.concept in {"cost_of_revenue", "total_operating_cost"}
                  and fact.consolidated_scope == "consolidated"}
    assert set(structured) == set(expected_costs)
    for identity, amount in expected_costs.items():
        fact = structured[identity]
        assert fact.value == pytest.approx(amount, rel=0, abs=0.01)
        assert fact.source_page == 72 and str(identity[0]) in fact.source_column
        assert fact.end_date == f"{identity[0]}-12-31" and fact.currency == "CNY"
    with pdfplumber.open(source) as pdf:
        statement_text = pdf.pages[71].extract_text() or ""
        page_texts = [page.extract_text() or "" for page in pdf.pages]
        note_page = pdf.pages[131]
        note_text = page_texts[131]
        positioned_rows = _positioned_pdf_rows(note_page.extract_words())
    facts, _evidence = parse_financial_pages([(72, statement_text)], filing, _company())
    costs = {fact.concept: (fact.value, fact.raw_text) for fact in facts
             if fact.concept in {"cost_of_revenue", "total_operating_cost"}}
    assert costs["cost_of_revenue"][0] == pytest.approx(5_152_713_900.37)
    assert "4,801,489,471.81" in costs["cost_of_revenue"][1]
    assert costs["total_operating_cost"][0] == pytest.approx(6_405_235_547.94)
    assert "6,049,330,568.79" in costs["total_operating_cost"][1]
    note_rows = note_row_disclosures(note_text, 2025, positioned_rows=positioned_rows)
    note_cost = next(row for row in note_rows if row[0] == "营业成本")
    assert float(note_cost[1].replace(",", "")) == pytest.approx(5_152_713_900.37)
    resolution = discover_document_units(page_texts)
    region = next(region for region in resolution.note_regions
                  if region.start_page <= 132 < region.end_page)
    note_facts, note_refs = extract_note_candidates(
        _company(), filing, 132, note_text, note_page.bbox, region,
        positioned_rows=positioned_rows,
    )
    note_evidence = {ref.evidence_id: ref for ref in note_refs}
    note_cost_fact = next(fact for fact in note_facts if fact.concept == "cost_of_revenue")
    assert note_cost_fact.value == pytest.approx(5_152_713_900.37)
    assert note_source_issue(
        note_cost_fact, note_evidence[f"fact:{note_cost_fact.fact_id}"], note_evidence,
    ) is None
    assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == original_hash
