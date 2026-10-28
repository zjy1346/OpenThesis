"""Valuation validation may isolate nodes, never rewrite provider prose."""
from copy import deepcopy
import hashlib
import json

from openthesis.valuation_claims import enforce_valuation_consistency


def valuation():
    return {"status": "ok", "implied_fcf_growth": "0.05321",
            "equity_market_value": 1000, "base_free_cash_flow": 100,
            "currency": "USD", "discount_rate": "0.10", "terminal_growth": "0.03"}


def test_rejected_sentence_span_preserves_neighbor_text_and_binds_original_leaf():
    text = "Supported business observation; current price implies 90% annual FCF growth."
    raw = {"thesis": {"analysis": [text, "Durable cash generation remains important."],
                      "evidence_ids": ["filing:original"]}}
    frozen = deepcopy(raw)
    checked, audit = enforce_valuation_consistency(raw, valuation(), "en")
    assert checked["thesis"]["analysis"] == [
        "Supported business observation;",
        "Durable cash generation remains important.",
    ]
    assert "90%" not in " ".join(checked["thesis"]["analysis"])
    assert raw == frozen
    encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode()
    assert audit["input_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert {item["path"] for item in audit["quarantined_nodes"]} == {"thesis.analysis[0]"}
    rejected = audit["quarantined_nodes"][0]
    assert rejected["reason"] == "unsupported_valuation_assertion"
    assert rejected["original_content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert len(rejected["removed_spans"]) == 1
    span = rejected["removed_spans"][0]
    assert text[span["start"]:span["end"]] == " current price implies 90% annual FCF growth."


def test_pure_unsupported_valuation_sentence_is_still_removed():
    text = "Current price implies 90% annual FCF growth."
    checked, audit = enforce_valuation_consistency({"thesis": text}, valuation(), "en")

    assert "thesis" not in checked
    assert audit["quarantined_count"] == 1
    rejected = audit["quarantined_nodes"][0]
    assert rejected["original_content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert text[rejected["removed_spans"][0]["start"]:rejected["removed_spans"][0]["end"]] == text


def test_discount_and_terminal_rates_are_not_compared_to_implied_growth():
    text = "Reverse DCF uses a discount rate of 10%, terminal growth of 3%, and implies 5.32% annual FCF growth."
    checked, audit = enforce_valuation_consistency({"thesis": text}, valuation(), "en")
    assert checked["thesis"] == text
    assert audit["quarantined_count"] == 0


def test_swapped_rate_does_not_pass_merely_because_it_matches_another_field():
    text = "Reverse DCF uses a discount rate of 5.32% and implies 10% annual FCF growth."
    checked, audit = enforce_valuation_consistency({"thesis": text}, valuation(), "en")
    assert "thesis" not in checked
    assert audit["quarantined_count"] == 1


def test_references_and_labels_are_not_sanitized_as_prose():
    raw = {"thesis": {"analysis": "Cash generation depends on the operating business.",
                      "title": "Reverse DCF", "evidence_ids": ["filing:reverse dcf 90%"]}}
    checked, audit = enforce_valuation_consistency(raw, None, "en")
    assert checked["thesis"] == raw["thesis"]
    assert audit["quarantined_count"] == 0


def test_nested_analytical_scalars_survive_engine_field_projection():
    raw = {"implied_expectations": {"analysis": {"text": "Growth depends on cash reinvestment.",
                                                 "confidence": 0.6, "sensitivity_rank": 2},
                                    "implied_growth": 0.9, "evidence_ids": ["fact:fcf"]}}
    checked, _ = enforce_valuation_consistency(raw, valuation(), "en")
    assert checked["implied_expectations"]["analysis"] == raw["implied_expectations"]["analysis"]
    assert checked["implied_expectations"]["implied_growth"] == "0.05321"


def test_unrelated_negation_cannot_bless_an_assertion_but_safe_neighbor_survives():
    cases = (
        ("The company has a wide margin of safety. It cannot be independently assessed.",
         "It cannot be independently assessed."),
        ("Current price does not imply 5% growth. Current price implies 90% annual FCF growth.",
         "Current price does not imply 5% growth."),
    )
    for text, expected in cases:
        checked, audit = enforce_valuation_consistency({"thesis": text}, valuation(), "en")
        assert checked["thesis"] == expected
        assert "wide margin of safety" not in checked["thesis"]
        assert "90%" not in checked["thesis"]
        assert audit["quarantined_count"] == 1


def test_mixed_language_leaf_removes_only_decimal_valuation_sentence():
    text = (
        "Business outlook remains steady. "
        "当前价格隐含90.5% annual FCF growth. "
        "Customer concentration remains a material risk."
    )
    raw = {"thesis": text}
    frozen = deepcopy(raw)
    checked, audit = enforce_valuation_consistency(raw, valuation(), "en")

    assert checked["thesis"] == (
        "Business outlook remains steady. Customer concentration remains a material risk."
    )
    assert "90.5%" not in checked["thesis"]
    assert raw == frozen
    rejected = audit["quarantined_nodes"][0]
    span = rejected["removed_spans"][0]
    assert text[span["start"]:span["end"]] == " 当前价格隐含90.5% annual FCF growth."
    assert rejected["original_content_sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_genuine_valuation_limitation_is_retained_without_a_computation():
    text = "A margin of safety cannot be established from reverse DCF alone."
    checked, audit = enforce_valuation_consistency({"thesis": text}, None, "en")
    assert checked["thesis"] == text
    assert audit["quarantined_count"] == 0


def test_nonfinite_or_non_json_candidate_remains_invalid_without_prose_repair():
    for value in (float("nan"), {1, 2}):
        checked, audit = enforce_valuation_consistency({"thesis": {"confidence": value}}, None, "en")
        assert checked == {}
        assert audit["quarantined_nodes"] == [{"path": "$", "reason": "invalid_json_value"}]
