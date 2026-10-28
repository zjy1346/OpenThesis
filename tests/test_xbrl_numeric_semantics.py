import pytest

from openthesis.financial_xbrl_semantics import numeric_token, standard_concept


Ixt2011 = "http://www.xbrl.org/inlineXBRL/transformation/2011-07-31"
Ixt2020 = "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"
Ixt2022 = "http://www.xbrl.org/inlineXBRL/transformation/2022-02-16"
UsGaap2025 = "http://fasb.org/us-gaap/2025"


@pytest.mark.parametrize(
    ("text", "transform", "scale", "expected"),
    [
        ("1,234.50", f"{{{Ixt2022}}}num-dot-decimal", "6", ("1234.50", "1000000")),
        ("1.234,50", f"{{{Ixt2022}}}num-comma-decimal", "-3", ("1234.50", "0.001")),
    ],
)
def test_numeric_token_applies_registered_decimal_format_and_scale(
    text, transform, scale, expected
):
    assert numeric_token(text, transform, "", scale) == expected


def test_numeric_token_accepts_plain_decimal_but_not_unformatted_grouping():
    assert numeric_token("1234.5", "", "", "0") == ("1234.5", "1")
    with pytest.raises(ValueError, match="XBRL_DECIMAL_VALUE_INVALID"):
        numeric_token("1,234.50", "", "", "0")


def test_numeric_token_applies_sign_and_rejects_a_second_negative_sign():
    assert numeric_token("1234.50", "", "-", "0") == ("-1234.50", "1")
    with pytest.raises(ValueError, match="XBRL_DOUBLE_SIGN_INVALID"):
        numeric_token("-1234.50", "", "-", "0")


@pytest.mark.parametrize("scale", ["not-an-integer", "1.5", "19", "-19", "999"])
def test_numeric_token_rejects_invalid_or_unsupported_scales(scale):
    with pytest.raises(ValueError, match="XBRL_SCALE_UNSUPPORTED"):
        numeric_token("1", "", "", scale)


def test_numeric_token_only_converts_dash_to_zero_with_declared_transform():
    assert numeric_token("—", f"{{{Ixt2022}}}fixed-zero", "", "0") == ("0", "1")
    with pytest.raises(ValueError, match="XBRL_DECIMAL_VALUE_INVALID"):
        numeric_token("—", "", "", "0")


def test_numeric_token_scopes_zerodash_to_the_registry_version_that_defines_it():
    with pytest.raises(ValueError, match="XBRL_TRANSFORM_UNSUPPORTED"):
        numeric_token("—", f"{{{Ixt2022}}}zerodash", "", "0")
    assert numeric_token("—", f"{{{Ixt2011}}}zerodash", "", "0") == ("0", "1")


def test_numeric_token_scopes_decimal_transform_names_to_their_registry_version():
    with pytest.raises(ValueError, match="XBRL_TRANSFORM_UNSUPPORTED"):
        numeric_token("1,234.50", f"{{{Ixt2020}}}numdotdecimal", "", "0")
    assert numeric_token(
        "1,234.50", f"{{{Ixt2020}}}num-dot-decimal", "", "0"
    ) == ("1234.50", "1")


@pytest.mark.parametrize(
    "transform",
    [
        "{https://example.invalid/ixt}num-dot-decimal",
        f"{{{Ixt2022}}}unknown-transform",
    ],
)
def test_numeric_token_rejects_unknown_transform_or_namespace(transform):
    with pytest.raises(ValueError, match="XBRL_TRANSFORM_UNSUPPORTED"):
        numeric_token("1.25", transform, "", "0")


@pytest.mark.parametrize(
    ("local_name", "category"),
    [
        ("NetIncomeLoss", "parent_net_income"),
        ("ProfitLoss", "net_income"),
        ("StockholdersEquity", "equity"),
        (
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "total_equity",
        ),
    ],
)
def test_standard_concept_distinguishes_parent_and_total_income_and_equity(
    local_name, category
):
    concept = standard_concept(f"{{{UsGaap2025}}}{local_name}")
    assert concept is not None
    assert concept[0] == category


def test_standard_concept_does_not_trust_an_issuer_extension_local_name():
    assert (
        standard_concept(
            "{https://example.com/issuer/2025}NetIncomeLoss"
        )
        is None
    )
