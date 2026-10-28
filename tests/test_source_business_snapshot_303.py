"""Regression tests for source-independent business-stage input identity.

These tests intentionally exercise the production projection helper and the
real StageExecutor reuse seam.  Financial facts and parser observations stay
available to their owning stages, but must not change the business-only
source snapshot after a later financial compilation pass.
"""

from __future__ import annotations

import json
from copy import deepcopy

from openthesis.resilience.contracts import digest
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.production import _source_business_inputs
from openthesis.research_execution.registry import StageRegistry, StageSpec


def _evidence(*, compiled: bool = False, source_hash: str = "a" * 64,
              raw_text: str = "公司主营工业制冷设备，收入来自设备销售和服务。") -> list[dict]:
    filing = {
        "evidence_id": "filing:annual:page-12",
        "kind": "filing_text",
        "raw_text": raw_text,
        "excerpt": raw_text,
        "document_id": "official-filing-2025",
        "content_hash": source_hash,
        "source_url": "https://example.invalid/filing-2025.pdf",
        "page": 12,
        "company_cik": "CNTEST0001",
        "end_date": "2025-12-31",
    }
    if compiled:
        filing["compiled_disclosure_binding"] = {
            "version": "financial-v29",
            "facts_digest": "derived-facts-digest",
            "currency": "USD",
        }
    return [
        filing,
        {
            "evidence_id": "disclosure:business:page-13",
            "kind": "filing_text",
            "raw_text": "公司继续扩大售后服务网络。",
            "document_id": "official-filing-2025",
            "content_hash": source_hash,
            "source_url": "https://example.invalid/filing-2025.pdf",
            "page": 13,
        },
        {
            "evidence_id": "fact:revenue:2025",
            "kind": "financial_fact",
            "raw_text": "营业收入 1000",
            "document_id": "official-filing-2025",
            "content_hash": source_hash,
            "source_url": "https://example.invalid/filing-2025.pdf",
            "page": 56,
            "value": 1000,
        },
        {
            "evidence_id": "table:income:2025",
            "kind": "filing_text",
            "raw_text": "合并利润表（单位：人民币千元）",
            "document_id": "official-filing-2025",
            "content_hash": source_hash,
            "source_url": "https://example.invalid/filing-2025.pdf",
            "page": 56,
        },
        {
            "evidence_id": "gap:unit:2025",
            "kind": "material_gap",
            "raw_text": "单位证据待补充",
            "document_id": "official-filing-2025",
            "content_hash": source_hash,
            "source_url": "https://example.invalid/filing-2025.pdf",
            "page": 56,
        },
    ]


def _context(evidence: list[dict], *, reporting_currency: str = "CNY",
             compiled: bool = False) -> str:
    payload = {
        "company": {
            "cik": "CNTEST0001",
            "ticker": "000001.SZ",
            "name": "测试制冷公司",
            "reporting_currency": reporting_currency,
        },
        "evidence": deepcopy(evidence),
        "facts": [{"fact_id": "fact:revenue:2025", "value": 1000, "currency": "CNY"}],
        "accepted_facts": [{"fact_id": "fact:revenue:2025"}],
        "metrics": {"revenue": 1000, "net_income": 100},
        "latest_interim_metrics": {"revenue": 900},
        "valuation": {"equity_market_value": 9_500_000_000},
        "market_snapshot": {"currency": "USD", "market_cap": 9_500_000_000},
        "financial_period_coverage": {"displayed_annual_years": [2021, 2022, 2023, 2024, 2025]},
        "financial_capabilities": ["quantitative_facts"],
        "repair_observations": [{"fact_id": "fact:revenue:2025", "raw_value": 1000}],
        "issues": [{"code": "UNIT_PROVENANCE_UNCERTAIN"}],
        "business_note": "这段原件相关说明必须完整保留。",
    }
    if compiled:
        payload["compiled_disclosure_binding"] = {
            "version": "financial-v29",
            "accepted_fact_ids": ["fact:revenue:2025"],
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _inputs() -> dict:
    return {
        "report_language": "zh-CN",
        "ui_language": "zh-CN",
        "pack_content_identity": "pack-content-sha-1",
        "research_coverage_contract": "coverage-v3",
        "research_verification_contract": "verification-v4",
        "available": ["filing:annual:page-12", "fact:revenue:2025"],
        "evidence_records": {"fact:revenue:2025": {"value": 1000}},
        "capabilities": ["quantitative_facts", "sourced_disclosures"],
    }


def test_source_business_inputs_filters_to_canonical_filing_disclosures_without_mutation():
    inputs = _inputs()
    evidence = _evidence(compiled=True)
    context_json = _context(evidence)
    inputs_before = deepcopy(inputs)
    evidence_before = deepcopy(evidence)
    context_before = context_json

    projected = _source_business_inputs(inputs, context_json, evidence)

    assert inputs == inputs_before
    assert evidence == evidence_before
    assert context_json == context_before
    context = json.loads(projected["context_json"])
    assert context["business_note"] == "这段原件相关说明必须完整保留。"
    assert "reporting_currency" not in context["company"]
    for key in (
        "facts", "accepted_facts", "metrics", "latest_interim_metrics", "valuation",
        "market_snapshot", "financial_period_coverage", "financial_capabilities",
        "repair_observations", "issues", "compiled_disclosure_binding",
    ):
        assert key not in context
    assert "compiled_disclosure_binding" not in projected
    assert [item["evidence_id"] for item in context["evidence"]] == [
        "disclosure:business:page-13", "filing:annual:page-12",
    ]
    assert all(
        item["evidence_id"].startswith("filing:") or item["kind"] == "filing_text"
        for item in context["evidence"]
    )
    assert all("compiled_disclosure_binding" not in item for item in context["evidence"])
    assert "fact:revenue:2025" not in projected["available"]
    assert "quantitative_facts" not in projected["capabilities"]
    assert "sourced_disclosures" in projected["capabilities"]
    assert projected["material_scope_policy"] == "source-independent-analysis-v4-canonical-originals"
    assert len(json.dumps(projected, ensure_ascii=False)) > 100


def test_financial_compilation_and_reporting_currency_do_not_change_business_snapshot_digest():
    early_evidence = _evidence(compiled=False)
    final_evidence = _evidence(compiled=True)
    inputs = _inputs()
    early = _source_business_inputs(inputs, _context(early_evidence), early_evidence)
    final = _source_business_inputs(inputs, _context(final_evidence, reporting_currency="USD", compiled=True), final_evidence)

    assert digest(early) == digest(final)
    assert json.loads(early["context_json"]) == json.loads(final["context_json"])
    assert early["evidence_records"] == final["evidence_records"]
    assert early["capabilities"] == final["capabilities"]

    changed_raw = _evidence(raw_text="公司主营工业制冷设备，并新增海外工程服务。")
    changed_hash = _evidence(source_hash="b" * 64)
    changed_language = {**inputs, "report_language": "en"}
    changed_pack = {**inputs, "pack_content_identity": "pack-content-sha-2"}
    assert digest(_source_business_inputs(inputs, _context(changed_raw), changed_raw)) != digest(early)
    assert digest(_source_business_inputs(inputs, _context(changed_hash), changed_hash)) != digest(early)
    assert digest(_source_business_inputs(changed_language, _context(early_evidence), early_evidence)) != digest(early)
    assert digest(_source_business_inputs(changed_pack, _context(early_evidence), early_evidence)) != digest(early)


def test_source_period_metadata_correction_keeps_original_and_invalidates_scope():
    original = _evidence()
    corrected = deepcopy(original)
    for item in original:
        if item.get("evidence_id", "").startswith("filing:"):
            item.update(end_date="2026-12-31", fiscal_year=2026, fiscal_period="FY")
    for item in corrected:
        if item.get("evidence_id", "").startswith("filing:"):
            item.update(end_date="2026-06-30", fiscal_year=2026, fiscal_period="H1")
    before = _source_business_inputs(_inputs(), _context(original), original)
    after = _source_business_inputs(_inputs(), _context(corrected), corrected)
    assert digest(before) != digest(after), "changed attribution must not reuse an old success flag"
    assert json.loads(before["context_json"])["evidence"][1]["raw_text"] == original[0]["raw_text"]
    assert json.loads(after["context_json"])["evidence"][1]["end_date"] == "2026-06-30"


def test_real_stage_executor_reuses_previous_business_input_without_second_producer_call():
    evidence = _evidence(compiled=True)
    business_inputs = _source_business_inputs(_inputs(), _context(evidence, compiled=True), evidence)
    spec = StageSpec("business-analyst", "business-v1", "produce-business", "verify-business",
                     output_contract="business-analysis-v1")
    registry = StageRegistry((spec,))
    calls: list[dict] = []

    def produce(_spec, stage_inputs, _dependencies):
        calls.append(deepcopy(stage_inputs))
        return {"analysis": "公司主营工业制冷设备。", "source_digest": digest(stage_inputs)}

    def verify(_spec, candidate, _stage_inputs):
        return StageValidation(True, deepcopy(candidate), "completed_verified")

    executor = StageExecutor(
        {"produce-business": produce},
        {"verify-business": verify},
    )
    first = executor.execute(registry, business_inputs, execution_fingerprint="business-model-v1")
    second = executor.execute(
        registry,
        deepcopy(business_inputs),
        previous=first,
        execution_fingerprint="business-model-v1",
    )

    assert len(calls) == 1
    assert second[spec.stage_id].validation.state == "completed_verified"
    assert second[spec.stage_id].candidate == first[spec.stage_id].candidate
    assert second[spec.stage_id].input_digest == first[spec.stage_id].input_digest

