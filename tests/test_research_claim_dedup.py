from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib

import pytest

from openthesis.research_claim_dedup import deduplicate_claims


def _claim(text: str, value: object = Decimal("1234.5"), **updates: object) -> dict:
    claim = {
        "kind": "fact",
        "text": text,
        "claim_id": "claim-a",
        "confidence": 0.8,
        "concept": "revenue",
        "value": value,
        "unit": "USD",
        "currency": "USD",
        "company_id": "issuer-1",
        "period": {"start": "2024-01-01", "end": "2024-12-31", "FY": 2024},
        "scope": "consolidated",
        "revision": 1,
        "generation": 2,
        "evidence_ids": ["fact:revenue-2024"],
    }
    claim.update(updates)
    return claim


def test_default_dedup_uses_only_normalized_full_claim_equivalence() -> None:
    first = _claim("Ａnnual   revenue WAS USD 1,234.50。", display_text="First")
    second = _claim(
        "annual revenue was USD 1,234.50.", display_text="Second",
        claim_id="claim-b", confidence=0.3, section_id="overview",
    )
    output, audit = deduplicate_claims([first, second])

    assert len(output) == 1
    assert output[0]["text"] == first["text"]
    assert audit["input_count"] == 2
    assert audit["output_count"] == 1
    assert audit["removed_count"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_id", "issuer-2"),
        ("period", {"start": "2023-01-01", "end": "2023-12-31", "FY": 2023}),
        ("scope", "parent"),
        ("currency", "CNY"),
        ("evidence_ids", ["fact:other"]),
        ("formula", "a + b"),
        ("generation", 3),
        ("extra_semantic_field", {"mode": "reported"}),
    ],
)
def test_semantic_field_differences_prevent_numeric_dedup(field: str, value: object) -> None:
    first = _claim("Revenue was USD 1,234.50.")
    second = _claim("Annual sales of $1,234.500", **{field: value})

    output, _ = deduplicate_claims([first, second], verified_numeric=True)

    assert len(output) == 2


def test_direct_revenue_statements_with_display_precision_variants_merge() -> None:
    first = _claim("Revenue was USD 1,234.50.", Decimal("1234.500"), section_ids=["overview"])
    second = _claim(
        "FY2024 annual revenue of $1,234.500",
        Decimal("1234.5"),
        section_id="valuation",
        confidence=0.4,
    )

    output, audit = deduplicate_claims([first, second], verified_numeric=True)

    assert len(output) == 1
    assert output[0]["text"] == first["text"]
    assert output[0]["section_ids"] == ["overview", "valuation"]
    assert output[0]["confidence"] == 0.4
    assert audit["removed_count"] == 1
    merge = audit["merges"][0]
    assert merge["kept_index"] == 0
    assert merge["removed_indices"] == [1]
    assert merge["text_hashes"] == [
        hashlib.sha256(first["text"].encode("utf-8")).hexdigest(),
        hashlib.sha256(second["text"].encode("utf-8")).hexdigest(),
    ]
    assert merge["numeric_fingerprint"]
    assert first["text"] not in repr(audit)


@pytest.mark.parametrize(
    "first_text,first_value,second_text,second_value",
    [
        ("Revenue was USD 100.", "100", "Revenue was USD 101.", "101"),
        ("Revenue was USD 100.", "100", "Revenue was USD 100 + 1.", "101"),
        ("Revenue was USD 100.", "100", "Revenue was USD +100.", "100"),
        ("Revenue was USD 100.", "100", "Revenue was USD -100.", "-100"),
        ("Revenue was USD 100.", "100", "Revenue did not exceed USD 100.", "100"),
        ("Revenue was USD 100.", "100", "Annual revenue grew 5% to USD 100.", "100"),
        ("Revenue was USD 100.", "100", "Revenue was USD 100 due to pricing.", "100"),
    ],
)
def test_numeric_dedup_rejects_different_amounts_negation_trend_and_analysis(
    first_text: str, first_value: str, second_text: str, second_value: str
) -> None:
    output, _ = deduplicate_claims(
        [_claim(first_text, Decimal(first_value)), _claim(second_text, Decimal(second_value))],
        verified_numeric=True,
    )

    assert len(output) == 2


def test_numeric_dedup_does_not_merge_inference_claims() -> None:
    first = _claim("Revenue supports durable demand.", kind="inference")
    second = _claim("Revenue suggests durable demand.", kind="inference")

    output, _ = deduplicate_claims([first, second], verified_numeric=True)

    assert len(output) == 2


def test_non_revenue_concepts_need_normalized_text_equivalence() -> None:
    first = _claim("Revenue was USD 100.", concept="operating_cash_flow")
    second = _claim("Sales of USD 100", concept="operating_cash_flow")

    output, _ = deduplicate_claims([first, second], verified_numeric=True)

    assert len(output) == 2


def test_decimal_key_is_exact_even_with_low_global_precision() -> None:
    first = _claim(
        "Revenue was USD 12345678901234567890.1200.",
        Decimal("12345678901234567890.1200"),
    )
    second = _claim(
        "Annual revenue of USD 12345678901234567890.12",
        Decimal("12345678901234567890.12"),
    )
    with localcontext() as ctx:
        ctx.prec = 2
        output, _ = deduplicate_claims([first, second], verified_numeric=True)

    assert len(output) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("-Infinity")])
def test_nonfinite_values_do_not_receive_numeric_dedup_keys(value: object) -> None:
    output, audit = deduplicate_claims(
        [_claim("Revenue was USD 100.", value), _claim("Annual sales of $100", value)],
        verified_numeric=True,
    )

    assert len(output) == 2
    assert audit["merges"] == []


def test_merge_preserves_stable_section_union_and_unknown_confidence() -> None:
    first = _claim("Revenue was USD 1,234.50.", section_ids=["overview", "shared"], confidence=None)
    second = _claim("Sales of $1,234.500 USD", section_id="valuation", confidence=0.2)
    third = _claim("营业收入为USD 1,234.50。", section="financials", confidence=0.1)

    output, _ = deduplicate_claims([first, second, third], verified_numeric=True)

    assert len(output) == 1
    assert output[0]["section_ids"] == ["overview", "shared", "valuation", "financials"]
    assert output[0]["confidence"] is None


def test_unverified_numeric_claims_do_not_hide_different_prose() -> None:
    first = _claim("Revenue was USD 100.")
    second = _claim("Revenue was USD 100, reflecting a recovery in demand.")

    output, _ = deduplicate_claims([first, second], verified_numeric=False)

    assert len(output) == 2


def test_default_text_normalization_does_not_remove_decimal_or_negative_sign() -> None:
    output, _ = deduplicate_claims([
        _claim("Revenue was USD 1.2."),
        _claim("Revenue was USD 12."),
        _claim("Revenue was USD -12."),
    ])

    assert len(output) == 3


def test_evidence_ids_are_compared_as_sets_but_not_ignored() -> None:
    first = _claim("Revenue was USD 1,234.50.", evidence_ids=["fact:a", "fact:b"])
    reordered = _claim("Sales of $1,234.500 USD", evidence_ids=["fact:b", "fact:a"])
    different = _claim("Sales of $1,234.500 USD", evidence_ids=["fact:a", "fact:c"])

    output, _ = deduplicate_claims([first, reordered, different], verified_numeric=True)

    assert len(output) == 2


def test_output_is_detached_and_input_is_unchanged() -> None:
    first = _claim("Revenue was USD 1,234.50.", section_ids=["overview"], extra={"nested": [1]})
    second = _claim("Annual sales of $1,234.500", section_id="valuation", extra={"nested": [1]})
    original = deepcopy([first, second])

    output, _ = deduplicate_claims([first, second], verified_numeric=True)
    output[0]["extra"]["nested"].append(2)

    assert [first, second] == original
    assert output[0]["extra"]["nested"] == [1, 2]


def test_claims_must_be_dicts() -> None:
    with pytest.raises(TypeError):
        deduplicate_claims([{"kind": "fact"}, "not a claim"])  # type: ignore[list-item]


def test_verified_numeric_requires_explicit_boolean_attestation() -> None:
    with pytest.raises(TypeError):
        deduplicate_claims([_claim("Revenue was USD 100.")], verified_numeric=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["conclusion", "argument"])
def test_conclusion_and_argument_use_text_normalization_within_their_field(field: str) -> None:
    first = _claim("", **{field: "Ａnnual   revenue is stable。"})
    second = _claim("", **{field: "annual revenue IS stable."}, claim_id="claim-b")

    output, _ = deduplicate_claims([first, second])

    assert len(output) == 1
    assert output[0][field] == first[field]


def test_conclusion_and_argument_do_not_match_across_fields() -> None:
    first = _claim("", conclusion="Revenue is stable.")
    second = _claim("", argument="revenue is stable")

    output, _ = deduplicate_claims([first, second])

    assert len(output) == 2


@pytest.mark.parametrize(
    "confidence",
    [-0.01, 1.01, float("nan"), float("inf"), True, "0.2"],
)
def test_out_of_range_or_unknown_confidence_stays_unknown(confidence: object) -> None:
    first = _claim("Revenue was USD 100.", confidence=0.5)
    second = _claim("revenue was USD 100!", confidence=confidence)

    output, _ = deduplicate_claims([first, second])

    assert len(output) == 1
    assert output[0]["confidence"] is None


def test_audit_has_explicit_schema_version() -> None:
    _, audit = deduplicate_claims([_claim("Revenue was USD 100.")])

    assert audit["schema_version"] == "research-claim-dedup-v1"


def test_safe_claim_id_alias_is_audited_without_rewriting_references() -> None:
    kept = _claim("Revenue was USD 100.", Decimal("100"), claim_id="revenue-verified")
    removed = _claim("Sales of $100", Decimal("100"), claim_id="revenue-display")
    dependent = _claim(
        "Revenue supports the model.",
        kind="inference",
        claim_id="downstream",
        input_claim_ids=["revenue-display"],
        claim_ids=["revenue-display"],
    )
    original_dependent = deepcopy(dependent)

    output, audit = deduplicate_claims([kept, removed, dependent], verified_numeric=True)

    assert len(output) == 2
    assert output[1]["input_claim_ids"] == ["revenue-display"]
    assert output[1]["claim_ids"] == ["revenue-display"]
    assert dependent == original_dependent
    assert audit["claim_id_aliases"] == {"revenue-display": "revenue-verified"}


@pytest.mark.parametrize("conflict", ["removed_id", "kept_id", "missing_kept_id"])
def test_claim_id_conflict_or_missing_target_blocks_merge_when_referenced(conflict: str) -> None:
    kept = _claim("Revenue was USD 100.", Decimal("100"), claim_id="revenue-kept")
    removed = _claim("Sales of $100", Decimal("100"), claim_id="revenue-removed")
    if conflict == "kept_id":
        other = _claim("Other fact", claim_id="revenue-kept", kind="inference")
    elif conflict == "removed_id":
        other = _claim("Other fact", claim_id="revenue-removed", kind="inference")
    else:
        kept.pop("claim_id")
        other = None
    dependent = _claim(
        "A downstream calculation depends on the displayed fact.",
        kind="calculation",
        claim_id="downstream",
        input_claim_ids=["revenue-removed"],
    )
    inputs = [kept, removed]
    if other is not None:
        inputs.append(other)
    inputs.append(dependent)

    output, audit = deduplicate_claims(inputs, verified_numeric=True)

    assert len(output) == len(inputs)
    assert audit["claim_id_aliases"] == {}
    assert output[-1]["input_claim_ids"] == ["revenue-removed"]


def test_ambiguous_removed_id_without_dependents_may_merge_but_has_no_alias() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), claim_id="revenue-kept")
    duplicate = _claim("Sales of $100", Decimal("100"), claim_id="revenue-removed")
    collision = _claim("An unrelated narrative", claim_id="revenue-removed", kind="inference")

    output, audit = deduplicate_claims([first, duplicate, collision], verified_numeric=True)

    assert len(output) == 2
    assert audit["claim_id_aliases"] == {}


def test_same_claim_id_collision_with_dependency_blocks_merge() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), claim_id="duplicate-id")
    second = _claim("Sales of $100", Decimal("100"), claim_id="duplicate-id")
    dependent = _claim(
        "A downstream narrative.",
        kind="inference",
        claim_id="downstream",
        claim_ids=["duplicate-id"],
    )

    output, audit = deduplicate_claims([first, second, dependent], verified_numeric=True)

    assert len(output) == 2
    assert audit["claim_id_aliases"] == {}
    assert output[-1]["claim_ids"] == ["duplicate-id"]


def test_numeric_verified_indices_limit_cross_text_keys_to_external_subset() -> None:
    model_self_attested = _claim(
        "Revenue was USD 100.",
        Decimal("100"),
        claim_id="claim-model-label",
        _verification_state="numeric_verified",
    )
    verified = _claim("Sales of $100", Decimal("100"), claim_id="claim-validator-approved")

    output, audit = deduplicate_claims(
        [model_self_attested, verified],
        verified_numeric=True,
        numeric_verified_indices={1},
    )

    assert len(output) == 2
    assert audit["merges"] == []


def test_numeric_verified_indices_allow_merge_only_for_selected_pair() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), claim_id="verified-a")
    second = _claim("Sales of $100", Decimal("100"), claim_id="verified-b")
    third = _claim("Sales of $100", Decimal("100"), claim_id="not-selected")

    output, audit = deduplicate_claims(
        [first, second, third],
        verified_numeric=True,
        numeric_verified_indices={0, 1},
    )

    assert len(output) == 2
    assert audit["claim_id_aliases"] == {"verified-b": "verified-a"}


def test_numeric_verified_indices_require_verified_numeric_flag() -> None:
    with pytest.raises(ValueError):
        deduplicate_claims(
            [_claim("Revenue was USD 100.")],
            verified_numeric=False,
            numeric_verified_indices={0},
        )


@pytest.mark.parametrize("index", [True, False, -1, 1, 0.0, "0"])
def test_numeric_verified_indices_reject_invalid_members(index: object) -> None:
    with pytest.raises(ValueError):
        deduplicate_claims(
            [_claim("Revenue was USD 100.")],
            verified_numeric=True,
            numeric_verified_indices={index},  # type: ignore[dict-item]
        )


def test_numeric_verified_indices_reject_non_set_container() -> None:
    with pytest.raises(ValueError):
        deduplicate_claims(
            [_claim("Revenue was USD 100.")],
            verified_numeric=True,
            numeric_verified_indices=[0],  # type: ignore[arg-type]
        )


def test_numeric_subset_does_not_hide_global_id_conflict_context() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), claim_id="keep")
    candidate = _claim("Sales of $100", Decimal("100"), claim_id="old")
    outside_subset_collision = _claim("Unrelated material", claim_id="old", kind="inference")
    dependent = _claim("Downstream claim", kind="inference", input_claim_ids=["old"])

    output, audit = deduplicate_claims(
        [first, candidate, outside_subset_collision, dependent],
        verified_numeric=True,
        numeric_verified_indices={0, 1},
    )

    assert len(output) == 4
    assert audit["claim_id_aliases"] == {}


def test_repeated_removed_id_within_same_equivalent_group_keeps_local_references() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), claim_id="claim-a")
    second = _claim("Sales of $100", Decimal("100"), claim_id="claim-b")
    third = _claim("Annual revenue of USD 100", Decimal("100"), claim_id="claim-b")
    dependent = _claim(
        "Downstream narrative.",
        kind="inference",
        claim_id="dependent",
        input_claim_ids=["claim-b"],
    )

    output, audit = deduplicate_claims(
        [first, second, third, dependent],
        verified_numeric=True,
        referenced_claim_ids={"claim-b"},
    )

    assert len(output) == 2
    assert audit["claim_id_aliases"] == {"claim-b": "claim-a"}
    assert output[-1]["input_claim_ids"] == ["claim-b"]


def test_report_reference_and_cross_group_id_conflict_block_alias_and_merge() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), claim_id="claim-a")
    cross_group_collision = _claim(
        "Unrelated narrative",
        claim_id="claim-b",
        kind="inference",
    )
    third = _claim("Sales of $100", Decimal("100"), claim_id="claim-b")

    output, audit = deduplicate_claims(
        [first, cross_group_collision, third],
        verified_numeric=True,
        referenced_claim_ids={"claim-b"},
    )

    assert len(output) == 3
    assert audit["claim_id_aliases"] == {}
    assert [claim["text"] for claim in output] == [
        "Revenue was USD 100.", "Unrelated narrative", "Sales of $100"
    ]


def test_nonmerging_interleaved_group_preserves_global_input_order() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), claim_id="claim-a")
    middle = _claim("Middle item", kind="inference", claim_id="middle")
    third = _claim("Sales of $100", Decimal("100"), claim_id="claim-b")
    id_collision = _claim("Other group with b", kind="inference", claim_id="claim-b")

    output, audit = deduplicate_claims(
        [first, middle, third, id_collision],
        verified_numeric=True,
        referenced_claim_ids={"claim-b"},
    )

    assert len(output) == 4
    assert [claim["text"] for claim in output] == [
        "Revenue was USD 100.", "Middle item", "Sales of $100", "Other group with b"
    ]
    assert audit["claim_id_aliases"] == {}


@pytest.mark.parametrize(
    "value",
    [
        ["claim-a"],
        {1},
        {""},
    ],
)
def test_referenced_claim_ids_validate_container_and_members(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        deduplicate_claims(
            [_claim("Revenue was USD 100.")],
            referenced_claim_ids=value,  # type: ignore[arg-type]
        )


def test_unverified_reference_only_claims_do_not_use_numeric_merge() -> None:
    first = _claim("Revenue was USD 100.", Decimal("100"), _verification_state="reference_only")
    second = _claim("Sales of $100", Decimal("100"), _verification_state="reference_only")

    output, audit = deduplicate_claims([first, second], verified_numeric=True)

    assert len(output) == 2
    assert audit["claim_id_aliases"] == {}
