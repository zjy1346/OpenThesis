from copy import deepcopy

import pytest

from openthesis.providers import _parse_model_json
from openthesis.research_execution.model_output import (
    ModelOutputContract, OutputField, normalize_stage_output, recover_closed_json,
)


@pytest.mark.parametrize("tail", ['"broken', '123', '1.2e', 'true', 'null'])
def test_closed_sibling_survives_unfinished_scalar_without_guessing(tail):
    output = _parse_model_json('{"business_model":{"text":"完整正文","evidence_ids":["e1"]},"broken":' + tail)
    assert output["business_model"]["text"] == "完整正文"
    assert "broken" not in output
    assert output["_output_incomplete"] is True
    assert output["structured_output_valid"] is True  # parse candidate, not verified research


def test_complete_children_of_unclosed_array_are_preserved():
    result, issues = recover_closed_json('{"claims":[{"text":"closed","evidence_ids":["e1"]},{"text":"unfinished')
    assert result == {"claims": [{"text": "closed", "evidence_ids": ["e1"]}]}
    assert issues


def test_duplicate_target_does_not_select_first_value_or_remove_other_target():
    result, issues = recover_closed_json('{"good":"closed","value":1,"value":2,"tail":"unfinished')
    assert result == {"good": "closed"}
    assert any(item["code"] == "MODEL_OUTPUT_DUPLICATE_TARGET" for item in issues)


def test_nonfinite_and_overflow_are_not_numeric_candidates():
    for number in ["NaN", "Infinity", "1e309"]:
        result, _issues = recover_closed_json('{"good":"closed","bad":' + number + ',"tail":')
        assert result == {"good": "closed"}


def test_names_and_one_wrapper_are_mapped_without_mutating_original():
    contract = ModelOutputContract("test", (OutputField("business_model"), OutputField("claims")))
    original = {"report": {"Business Model": {"summary": "body", "evidence_ids": ["e1"]},
                             "unregistered_analysis": "keep"}, "CLAIMS": []}
    before = deepcopy(original)
    normalized = normalize_stage_output(original, contract)
    assert original == before
    assert normalized["business_model"] == original["report"]["Business Model"]
    assert normalized["claims"] == []
    assert normalized["report"]["unregistered_analysis"] == "keep"
    assert normalized["_output_normalization"]["changes"]


def test_conflicting_aliases_are_candidates_not_two_published_alternatives():
    contract = ModelOutputContract("test", (OutputField("business_model"),))
    normalized = normalize_stage_output({"business_model": "one", "Business Model": "two"}, contract)
    assert "business_model" not in normalized and "Business Model" not in normalized
    assert normalized["_output_normalization"]["issues"][0]["code"] == "MODEL_OUTPUT_ALIAS_CONFLICT"
    assert len(normalized["_output_normalization"]["original_alias_candidates"]["business_model"]) == 2
