import pytest

from openthesis.resilience.contracts import candidate_json


def test_candidate_json_persists_integer_model_usage_counts():
    payload = {
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "total_tokens": 33,
            "input_tokens": 11,
            "output_tokens": 22,
        }
    }

    assert candidate_json(payload) == (
        '{"usage":{"completion_tokens":22,"input_tokens":11,'
        '"output_tokens":22,"prompt_tokens":11,"total_tokens":33}}'
    )
    assert candidate_json({"usage": {"prompt_tokens": 0}}) == (
        '{"usage":{"prompt_tokens":0}}'
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_tokens", "11"),
        ("completion_tokens", {"value": 22}),
        ("total_tokens", -1),
        ("input_tokens", True),
        ("output_tokens", 1.5),
    ],
)
def test_candidate_json_rejects_usage_fields_that_are_not_nonnegative_ints(field, value):
    with pytest.raises(ValueError, match="RECOVERY_CANDIDATE_SECRET_FIELD"):
        candidate_json({"usage": {field: value}})


@pytest.mark.parametrize(
    "field",
    [
        "api_key",
        "api_token",
        "access_token",
        "refresh_token",
        "token_count",
        "prompt_tokens_estimate",
        "input_tokens_extra",
        "session_token",
        "authorization",
        "private_key",
        "cookie",
        "bearer",
    ],
)
def test_candidate_json_still_rejects_secrets_and_nonexact_token_names(field):
    with pytest.raises(ValueError, match="RECOVERY_CANDIDATE_SECRET_FIELD"):
        candidate_json({"usage": {field: 7}})


def test_candidate_json_uses_exact_normalized_usage_names_recursively():
    assert candidate_json({"usage": {"PROMPT-TOKENS": 4}}) == (
        '{"usage":{"PROMPT-TOKENS":4}}'
    )
