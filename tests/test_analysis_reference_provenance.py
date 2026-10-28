"""Source-reference aliases must remain provenance, not a trust shortcut."""

import pytest

from openthesis.research import _supported_research_fields, verify_agent_output
from openthesis.research_numeric_assertions import numeric_prose_supported


BUSINESS_ID = "filing:business"
REVENUE_ID = "filing:revenue"


def _business_source() -> dict:
    return {
        "evidence_id": BUSINESS_ID,
        "kind": "filing_text",
        "raw_text": "公司主要从事工业制冷设备的研发、生产和销售。",
        "company_cik": "CN_TEST",
        "end_date": "2025-12-31",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "document_id": "official:business",
        "page": 7,
    }


def _revenue_source() -> dict:
    return {
        "evidence_id": REVENUE_ID,
        "kind": "filing_text",
        "raw_text": "Revenue was CNY 120 million in 2025.",
        "company_cik": "CN_TEST",
        "end_date": "2025-12-31",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "document_id": "official:revenue",
        "page": 42,
    }


def _project(report: dict, records: dict[str, dict]) -> tuple[dict, dict]:
    return _supported_research_fields(
        report,
        list(report),
        set(records),
        records,
        "en",
    )


def test_reference_aliases_bind_local_analysis_but_do_not_upgrade_wrong_fact():
    records = {BUSINESS_ID: _business_source(), REVENUE_ID: _revenue_source()}
    report = {
        "business_model": {
            "analysis": _business_source()["raw_text"],
            "supporting_evidence": [BUSINESS_ID],
            "contradicting_evidence": [],
        },
        "financial_quality": {
            "kind": "fact",
            "text": "Revenue was CNY 999 million in 2025.",
            "supporting_evidence": [REVENUE_ID],
            "contradicting_evidence": [],
        },
    }

    safe, audit = _project(report, records)

    # Legal aliases are usable for a bounded local analysis and remain visible
    # as provenance; they do not turn a wrong explicit fact into trusted data.
    assert safe["business_model"]["analysis"] == records[BUSINESS_ID]["raw_text"]
    assert safe["business_model"]["supporting_evidence"] == [BUSINESS_ID]
    assert "financial_quality" not in safe
    assert any(
        item["path"] == "financial_quality.text"
        and item["reason"] == "source_confirmed_assertion_conflict"
        for item in audit["quarantined_paths"]
    )


def test_unverified_claim_reference_can_support_local_analysis_but_claim_is_not_admitted():
    records = {BUSINESS_ID: _business_source()}
    report = {
        "business_model": {
            "claims": [{
                "claim_id": "unverified-paraphrase",
                "kind": "inference",
                "text": "主营业务覆盖工业制冷设备制造与销售。",
                "supporting_evidence": [BUSINESS_ID],
            }],
            "analysis": records[BUSINESS_ID]["raw_text"],
            "supporting_evidence": [BUSINESS_ID],
        }
    }

    safe, audit = _project(report, records)

    assert safe["business_model"]["analysis"] == records[BUSINESS_ID]["raw_text"]
    assert safe["claims"] == []
    assert any(
        item["path"] == "business_model.claims[0]"
        and item["reason"] == "claim_not_verified"
        for item in audit["quarantined_paths"]
    )


def test_wrong_numeric_analysis_and_explicit_fact_fail_with_legal_source():
    records = {REVENUE_ID: _revenue_source()}
    safe, audit = _project({
        "financial_quality": {
            "analysis": "Revenue was CNY 999 million in 2025.",
            "supporting_evidence": [REVENUE_ID],
        }
    }, records)
    assert "financial_quality" not in safe
    assert any(item["reason"] == "source_confirmed_assertion_conflict"
               for item in audit["quarantined_paths"])

    verification = verify_agent_output({"claims": [{
        "claim_id": "wrong-revenue",
        "kind": "fact",
        "text": "Revenue was CNY 999 million in 2025.",
        "evidence_ids": [REVENUE_ID],
    }]}, {REVENUE_ID}, "en", records)
    assert verification["passed"] is False
    assert verification["claim_verifications"][0]["state"] in {
        "contradicted", "insufficient_evidence"
    }
    assert verification["verified_claim_count"] == 0


def test_registered_numeric_hash_locator_is_not_an_amount_and_unknown_id_is_not_a_shield():
    registered = {
        "evidence_id": "fact:ingest:123456789",
        "kind": "financial_fact",
        "value": 42,
        "currency": "CNY",
        "unit": "CNY",
        "fiscal_year": 2025,
    }
    assert numeric_prose_supported(
        "See fact:ingest:123456789 for the source.", [registered]
    )
    assert not numeric_prose_supported(
        "See fact:ingest:987654321 for the source.", [registered]
    )


@pytest.mark.parametrize("field", ["supporting_evidence", "contradicting_evidence"])
@pytest.mark.parametrize("bad_value", [None, "filing:business", {"evidence_id": BUSINESS_ID}, 7])
def test_non_list_reference_aliases_are_quarantined_without_crashing(field, bad_value):
    records = {BUSINESS_ID: _business_source()}
    report = {
        "business_model": {
            "analysis": records[BUSINESS_ID]["raw_text"],
            "evidence_ids": [BUSINESS_ID],
            field: bad_value,
        }
    }

    safe, audit = _project(report, records)

    assert safe["business_model"]["analysis"] == records[BUSINESS_ID]["raw_text"]
    assert any(item["path"] == f"business_model.{field}"
               and item["reason"] == "unknown_or_invalid_evidence"
               for item in audit["quarantined_paths"])
