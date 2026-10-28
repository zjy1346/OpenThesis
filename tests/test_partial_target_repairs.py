"""Target-level repair contracts: partial progress without unsafe widening.

These tests exercise pure, I/O-free repair seams.  They do not assert that a
repair is a trusted research result; the supplied verifier states model the
separate business-validation gate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from openthesis.resilience.contracts import digest
from openthesis.research_claim_repair import (
    ClaimRepairContract,
    ClaimRepairPlan,
    ClaimRepairTarget,
)
from openthesis.research_execution.model_output import (
    ModelOutputContract,
    OutputField,
    merge_field_repair,
    merge_field_targets,
)


FIELD_CONTRACT = ModelOutputContract(
    "partial-target-fields-v1",
    (
        OutputField("financial_quality", ("object",)),
        OutputField("claims", ("array",)),
    ),
)


def _field_original() -> dict:
    return {
        "structured_output_valid": True,
        "claims": {"wrong": "shape"},
        "unknown_extension": {"preserve": "opaque"},
    }


def _claim_candidate() -> dict:
    return {
        "structured_output_valid": True,
        "claims": [
            {
                "claim_id": "claim:revenue",
                "kind": "fact",
                "concept": "revenue",
                "evidence_ids": ["fact:revenue"],
                "text": "Revenue is 100 CNY.",
            },
            {
                "claim_id": "claim:margin",
                "kind": "calculation",
                "concept": "margin",
                "evidence_ids": ["fact:margin"],
                "text": "Margin is 20%.",
            },
        ],
        "safe_extension": {"retain": True},
    }


def _claim_contract(states: dict[str, str] | None = None) -> ClaimRepairContract:
    states = states or {}

    def verify(value: dict) -> dict:
        checks = []
        for index, claim in enumerate(value.get("claims", ())):
            claim_id = claim.get("claim_id", "")
            state = states.get(claim_id) if ("value" in claim or "unit" in claim) else None
            if state is None:
                if claim.get("value") == 100 and claim.get("unit") == "CNY":
                    state = "numeric_verified"
                elif claim.get("value") == 0.2 and claim.get("unit") == "ratio":
                    state = "text_supported"
                else:
                    state = "insufficient_evidence"
            missing_fields = tuple(
                field for field in ("value", "unit") if field not in claim
            )
            checks.append({
                "index": index,
                "claim_id": claim_id,
                "state": state,
                "issue_code": "" if state in {"numeric_verified", "text_supported"}
                else "CLAIM_REPAIR_TARGET_UNRESOLVED",
                "missing_fields": missing_fields,
                "repairability": "field_patch" if missing_fields else "none",
            })
        return {
            "passed": all(item["state"] in {"numeric_verified", "text_supported"}
                           for item in checks),
            "claim_verifications": checks,
        }

    return ClaimRepairContract(verify, "partial-claim-validator-v1")


def _claim_plan(candidate: dict, *, validator_version: str = "partial-claim-validator-v1"):
    return ClaimRepairPlan(
        candidate_digest=digest(candidate),
        validator_version=validator_version,
        targets=(
            ClaimRepairTarget(
                "/claims/0", "claim:revenue", ("value", "unit"),
                ("fact:revenue",), "CLAIM_REPAIR_TARGET_UNRESOLVED",
            ),
            ClaimRepairTarget(
                "/claims/1", "claim:margin", ("value", "unit"),
                ("fact:margin",), "CLAIM_REPAIR_TARGET_UNRESOLVED",
            ),
        ),
    )


def test_field_merge_accepts_independent_valid_target_and_records_safe_extension() -> None:
    original = _field_original()
    repair = {
        "structured_output_valid": True,
        "section_patches": {
            "financial_quality": {"status": "verified"},
            "claims": {"still": "wrong"},
        },
        "safe_explanation": "The claims shape could not be repaired.",
    }

    result = merge_field_targets(
        original, repair, FIELD_CONTRACT, ("financial_quality", "claims"),
    )

    assert result.progress is True
    assert result.candidate["financial_quality"] == {"status": "verified"}
    assert result.candidate["claims"] == original["claims"]
    assert result.accepted_patches == ("financial_quality",)
    assert result.unresolved_targets == ("claims",)
    assert result.ignored_extensions == ("safe_explanation",)
    assert result.issues[0]["path"] == "claims"
    assert original == _field_original()


def test_field_wrapper_returns_none_without_progress_and_does_not_widen_targets() -> None:
    original = _field_original()
    repair = {
        "structured_output_valid": True,
        "section_patches": {
            "claims": {"wrong": "type"},
            "not_requested": {"value": "ignored"},
        },
    }

    assert merge_field_repair(
        original, repair, FIELD_CONTRACT, ("financial_quality",),
    ) is None
    result = merge_field_targets(
        original, repair, FIELD_CONTRACT, ("financial_quality",),
    )
    assert result.progress is False
    assert result.candidate == original
    assert result.accepted_patches == ()
    assert result.unresolved_targets == ("financial_quality",)


def test_claim_merge_keeps_valid_sibling_when_other_atomic_target_is_incomplete() -> None:
    original = _claim_candidate()
    plan = _claim_plan(original)
    repair = {
        "structured_output_valid": True,
        "claim_patches": [
            {
                "path": "/claims/0",
                "claim_id": "claim:revenue",
                "fields": {"value": 100, "unit": "CNY"},
            },
            {
                "path": "/claims/1",
                "claim_id": "claim:margin",
                "fields": {"value": 0.2},
            },
        ],
        "safe_note": "margin unit remains unresolved",
    }

    result = _claim_contract().merge_targets(original, repair, plan)

    assert result.progress is True
    assert result.candidate["claims"][0]["value"] == 100
    assert "unit" not in result.candidate["claims"][1]
    assert result.accepted_patches == ("/claims/0",)
    assert result.unresolved_targets == ("/claims/1",)
    assert result.ignored_extensions == ("safe_note",)
    assert original == _claim_candidate()
    # Replanning the accepted result removes the accepted target; it is not
    # sent through the same repair lane again.
    remaining = _claim_contract().plan(result.candidate)
    assert remaining is not None
    assert [target.path for target in remaining.targets] == ["/claims/1"]


def test_claim_path_id_evidence_and_control_field_forgery_is_target_local() -> None:
    original = _claim_candidate()
    plan = _claim_plan(original)
    repair = {
        "structured_output_valid": True,
        "claim_patches": [
            {
                "path": "/claims/0",
                "claim_id": "claim:wrong",
                "fields": {"value": 100, "unit": "CNY", "evidence_ids": ["fact:other"]},
                "structured_output_valid": False,
            },
            {
                "path": "/claims/1",
                "claim_id": "claim:margin",
                "fields": {"value": 0.2, "unit": "ratio"},
            },
        ],
        "safe_explanation": "the first target is intentionally forged",
    }

    result = _claim_contract().merge_targets(original, repair, plan)

    assert result.progress is True
    assert result.accepted_patches == ("/claims/1",)
    assert result.unresolved_targets == ("/claims/0",)
    assert result.candidate["claims"][0] == original["claims"][0]
    assert result.candidate["claims"][1]["value"] == 0.2
    assert result.candidate["claims"][1]["evidence_ids"] == ["fact:margin"]


def test_duplicate_conflicting_claim_target_is_not_order_overwritten() -> None:
    original = _claim_candidate()
    plan = _claim_plan(original)
    repair = {
        "structured_output_valid": True,
        "claim_patches": [
            {"path": "/claims/0", "claim_id": "claim:revenue",
             "fields": {"value": 100, "unit": "CNY"}},
            {"path": "/claims/0", "claim_id": "claim:revenue",
             "fields": {"value": 999, "unit": "CNY"}},
            {"path": "/claims/1", "claim_id": "claim:margin",
             "fields": {"value": 0.2, "unit": "ratio"}},
        ],
    }

    result = _claim_contract().merge_targets(original, repair, plan)

    assert result.progress is True
    assert result.accepted_patches == ("/claims/1",)
    assert result.unresolved_targets == ("/claims/0",)
    assert result.candidate["claims"][0] == original["claims"][0]


def test_stale_claim_plan_and_reference_only_state_make_zero_submission() -> None:
    original = _claim_candidate()
    plan = _claim_plan(original)
    repair = {
        "structured_output_valid": True,
        "claim_patches": [{
            "path": "/claims/0", "claim_id": "claim:revenue",
            "fields": {"value": 100, "unit": "CNY"},
        }],
    }
    changed = deepcopy(original)
    changed["safe_extension"] = {"changed": True}
    stale = _claim_contract().merge_targets(changed, repair, plan)
    assert stale.progress is False
    assert stale.accepted_patches == ()
    assert stale.candidate == changed
    assert stale.issues[0]["code"] == "CLAIM_REPAIR_PLAN_STALE"

    changed_validator = _claim_contract()
    changed_validator.validator_version = "partial-claim-validator-v2"
    validator_stale = changed_validator.merge_targets(original, repair, plan)
    assert validator_stale.progress is False
    assert validator_stale.accepted_patches == ()
    assert validator_stale.issues[0]["code"] == "CLAIM_REPAIR_PLAN_STALE"

    changed_plan = replace(plan, targets=plan.targets[:1])
    target_stale = _claim_contract().merge_targets(original, repair, changed_plan)
    assert target_stale.progress is False
    assert target_stale.accepted_patches == ()
    assert target_stale.issues[0]["code"] == "CLAIM_REPAIR_PLAN_STALE"

    reference_only = _claim_contract({"claim:revenue": "reference_only"})
    result = reference_only.merge_targets(original, repair, plan)
    assert result.progress is False
    assert result.candidate == original
    assert result.unresolved_targets == ("/claims/0", "/claims/1")

