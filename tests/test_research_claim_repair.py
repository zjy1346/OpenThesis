from copy import deepcopy

import pytest

from openthesis.research import verify_agent_output, RESEARCH_VERIFICATION_VERSION
from openthesis.research_claim_repair import ClaimRepairContract


def fact(evidence_id="fact:current", *, year=2025, value=120, concept="revenue", **extra):
    return {"evidence_id": evidence_id, "kind": "financial_fact", "concept": concept,
        "value": value, "unit": "CNY", "currency": "CNY", "unit_scale": 1,
        "company_cik": "issuer", "entity": "issuer", "market": "CN_A",
        "scope": "consolidated", "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31", "fiscal_year": year, "fiscal_period": "FY",
        "raw_text": f"Revenue {value} CNY", **extra}


def verify(claim, records):
    by_id = {record["evidence_id"]: record for record in records}
    return verify_agent_output({"claims": [claim]}, set(by_id), "en", by_id)


def contract(records):
    by_id = {record["evidence_id"]: record for record in records}
    return ClaimRepairContract(lambda value: verify_agent_output(value, set(by_id), "en", by_id),
                               RESEARCH_VERIFICATION_VERSION)


def ambiguous_candidate():
    return {"structured_output_valid": True, "executive_summary": "Healthy section unchanged.",
        "business_model": {"text": "Healthy model", "claims": []},
        "claims": [{"kind": "fact", "text": "Revenue 120 CNY", "concept": "revenue",
            "evidence_ids": ["fact:current", "fact:previous"]}]}


def patch_response(plan, fields):
    target = plan.targets[0]
    return {"structured_output_valid": True, "claim_patches": [
        {"path": target.path, "claim_id": target.claim_id, "fields": fields}]}


def test_unique_explicit_period_disambiguates_without_overwriting_original():
    claim = {"kind": "fact", "text": "Revenue 120 CNY", "fiscal_year": 2025,
        "evidence_ids": ["fact:current", "fact:previous"]}
    original = deepcopy(claim)
    result = verify(claim, [fact(), fact("fact:previous", year=2024, value=100)])
    assert result["passed"]
    assert result["normalized_claims"][0]["value"] == 120
    assert claim == original


def test_ambiguity_produces_typed_patch_and_preserves_healthy_sections():
    repair = contract([fact(), fact("fact:previous", year=2024, value=100)])
    candidate = ambiguous_candidate()
    original = deepcopy(candidate)
    plan = repair.plan(candidate)
    assert plan is not None
    assert plan.targets[0].fields == ("value", "unit", "end_date")
    response = patch_response(plan, {"value": 120, "unit": "CNY", "end_date": "2025-12-31"})
    result = repair.merge(candidate, response, plan)
    assert result is not None
    assert result["executive_summary"] == original["executive_summary"]
    assert result["business_model"] == original["business_model"]
    assert result["claims"][0]["evidence_ids"] == original["claims"][0]["evidence_ids"]
    assert candidate == original


@pytest.mark.parametrize("mutation", ["extra_section", "extra_field", "drop_claim", "wrong_id", "wrong_path", "wrong_value", "refusal", "truncated"])
def test_invalid_or_out_of_scope_patch_is_not_published(mutation):
    repair = contract([fact(), fact("fact:previous", year=2024, value=100)])
    candidate = ambiguous_candidate()
    plan = repair.plan(candidate)
    response = patch_response(plan, {"value": 120, "unit": "CNY", "end_date": "2025-12-31"})
    if mutation == "extra_section":
        response["executive_summary"] = "Changed"
    elif mutation == "extra_field":
        response["claim_patches"][0]["fields"]["evidence_ids"] = ["fact:other"]
    elif mutation == "drop_claim":
        response["claim_patches"] = []
    elif mutation == "wrong_id":
        response["claim_patches"][0]["claim_id"] = "claim:other"
    elif mutation == "wrong_path":
        response["claim_patches"][0]["path"] = "/business_model"
    elif mutation == "wrong_value":
        response["claim_patches"][0]["fields"]["value"] = 999
    elif mutation == "refusal":
        response["_response_meta"] = {"refusal": "not allowed"}
    else:
        response["_response_meta"] = {"finish_reason": "length"}
    result = repair.merge(candidate, response, plan)
    if mutation == "extra_section":
        # A non-target section cannot be written, but must not veto the
        # independently valid, verified claim patch.
        assert result["executive_summary"] == candidate["executive_summary"]
        assert result["claims"][0]["value"] == 120
    else:
        assert result is None


def test_changed_candidate_or_validator_rejects_old_plan():
    repair = contract([fact(), fact("fact:previous", year=2024, value=100)])
    candidate = ambiguous_candidate()
    plan = repair.plan(candidate)
    response = patch_response(plan, {"value": 120, "unit": "CNY", "end_date": "2025-12-31"})
    changed = {**candidate, "executive_summary": "New generation"}
    assert repair.merge(changed, response, plan) is None
    repair.validator_version = "new-verifier"
    assert repair.merge(candidate, response, plan) is None


@pytest.mark.parametrize("invalid", [float("nan"), object()])
def test_non_json_mutation_of_saved_candidate_rejects_patch_without_crashing(invalid):
    repair = contract([fact(), fact("fact:previous", year=2024, value=100)])
    candidate = ambiguous_candidate()
    plan = repair.plan(candidate)
    response = patch_response(plan, {"value": 120, "unit": "CNY", "end_date": "2025-12-31"})
    candidate["executive_summary"] = invalid
    assert repair.merge(candidate, response, plan) is None


def test_conflicting_fact_and_numeric_prose_never_auto_repair():
    repair = contract([fact()])
    for changes in ({"value": 999}, {"text": "Revenue 999 CNY"}, {"evidence_ids": ["fact:missing"]}):
        claim = {"kind": "fact", "text": "Revenue 120 CNY", "evidence_ids": ["fact:current"], **changes}
        candidate = {"structured_output_valid": True, "claims": [claim]}
        assert repair.plan(candidate) is None
        assert not verify(claim, [fact()])["passed"]


def calculation(**changes):
    return {"kind": "calculation", "text": "Revenue growth 20%", "concept": "revenue_growth",
        "value": 0.2, "unit": "ratio", "formula": "revenue_growth",
        "fiscal_year": 2025, "fiscal_period": "FY", "end_date": "2025-12-31",
        "evidence_ids": ["fact:current", "fact:previous"],
        "inputs": {"current": "fact:current", "previous": "fact:previous"}, **changes}


def test_registered_formula_and_verified_inputs_prove_every_prose_number():
    records = [fact(), fact("fact:previous", year=2024, value=100)]
    assert verify(calculation(), records)["passed"]
    result = verify(calculation(text="Revenue growth 20% and 999 CNY"), records)
    assert result["claim_verifications"][0]["issue_code"] == "CLAIM_PROSE_NUMERIC_CONFLICT"
    assert not result["passed"]


@pytest.mark.parametrize("field,value,code", [
    ("scope", "unknown", "CLAIM_CALCULATION_IDENTITY_CONFLICT"),
    ("end_date", "", "CLAIM_CALCULATION_PERIOD_CONFLICT"),
])
def test_calculation_cannot_use_unknown_identity_even_when_both_inputs_match(field, value, code):
    from openthesis.research_calculations import prove_calculation
    records = [
        fact(**{field: value}),
        fact("fact:previous", year=2024, value=100, **{field: value}),
    ]
    assert prove_calculation(calculation(), records).issue_code == code


@pytest.mark.parametrize("changes,code", [
    ({"formula": "current / previous - 1"}, "CLAIM_FORMULA_UNSUPPORTED"),
    ({"inputs": {"current": 120, "previous": 100}}, "CLAIM_CALCULATION_INPUT_REFERENCE_INVALID"),
    ({"value": 20}, "CLAIM_CALCULATION_VALUE_CONFLICT"),
    ({"unit": "CNY"}, "CLAIM_CALCULATION_UNIT_CONFLICT"),
    ({"inputs": {"current": "fact:current", "previous": "fact:unknown"}}, "CLAIM_CALCULATION_INPUT_REFERENCE_INVALID"),
])
def test_calculation_errors_are_typed_not_prose_parsed(changes, code):
    result = verify(calculation(**changes), [fact(), fact("fact:previous", year=2024, value=100)])
    assert not result["passed"]
    assert result["claim_verifications"][0]["issue_code"] == code


@pytest.mark.parametrize("changes", [{"year": 2023}, {"scope": "parent"}, {"unit": "USD", "currency": "USD"}, {"company_cik": "another"}])
def test_calculations_cannot_mix_year_currency_scope_or_issuer(changes):
    result = verify(calculation(), [fact(), fact("fact:previous", **{"year": 2024, "value": 100, **changes})])
    assert not result["passed"]


def test_missing_formula_is_patchable_only_with_existing_verified_inputs():
    records = [fact(), fact("fact:previous", year=2024, value=100)]
    repair = contract(records)
    claim = calculation()
    del claim["formula"]
    candidate = {"structured_output_valid": True, "claims": [claim]}
    plan = repair.plan(candidate)
    assert plan.targets[0].fields == ("formula",)
    assert repair.merge(candidate, patch_response(plan, {"formula": "revenue_growth"}), plan) is not None
    assert repair.merge(candidate, patch_response(plan, {"formula": "invented"}), plan) is None


def test_nested_claim_paths_are_escaped_and_do_not_replace_dict_fields():
    repair = contract([fact(), fact("fact:previous", year=2024, value=100)])
    candidate = ambiguous_candidate()
    candidate["section/a~b"] = {"claims": candidate.pop("claims")}
    plan = repair.plan(candidate)
    assert plan.targets[0].path == "/section~1a~0b/claims/0"
    assert repair.merge(candidate, patch_response(plan, {"value": 120, "unit": "CNY", "end_date": "2025-12-31"}), plan) is not None
