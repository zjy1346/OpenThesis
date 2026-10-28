from openthesis.financial_taxonomy import (
    FINANCIAL_LABEL_ALIASES,
    equity_row_concept,
    profit_row_concept,
    profit_loss_value_transform,
)


def test_equity_labels_distinguish_group_total_from_attributable_parent_equity():
    assert equity_row_concept("Total equity") == "total_equity"
    assert equity_row_concept("Total shareholders' equity") == "total_equity"
    assert equity_row_concept("Total equity attributable to owners") == "equity"
    assert equity_row_concept("Equity attributable to equity holders of the Company") == "equity"
    assert equity_row_concept("Equity holders of the Company", "Attributable to:") == "equity"


def test_equity_liability_totals_and_non_equity_rows_are_not_equity_facts():
    for label in (
        "Total equity and liabilities",
        "Total liabilities and equity",
        "Statement of changes in equity",
        "Changes in equity",
        "Share capital",
        "Issued share capital",
        "Non-controlling interests",
        "Total equity attributable to non-controlling interests",
    ):
        assert equity_row_concept(label) is None, label


def test_profit_before_tax_aliases_cover_ifrs_income_tax_labels():
    aliases = FINANCIAL_LABEL_ALIASES["profit_before_tax"]
    assert "profit before income tax" in aliases
    assert "(Loss)/profit before income tax" in aliases
    assert "loss before tax" in aliases
    assert "loss before income tax" in aliases


def test_loss_only_operating_and_before_tax_rows_supply_sign_but_mixed_labels_do_not():
    assert profit_loss_value_transform("Loss before income tax", "profit_before_tax") == "negative_absolute_value"
    assert profit_loss_value_transform("Loss before tax", "profit_before_tax") == "negative_absolute_value"
    assert profit_loss_value_transform("Operating loss", "operating_income") == "negative_absolute_value"
    assert profit_loss_value_transform("(Loss)/profit before income tax", "profit_before_tax") == "identity"
    assert profit_loss_value_transform("Operating income/(loss)", "operating_income") == "identity"
    assert profit_loss_value_transform("Profit before income tax", "profit_before_tax") == "identity"


def test_earnings_per_share_label_and_inherited_context_are_not_profit_facts():
    earnings_per_share = (
        "Earnings/(loss) per share for profit/(loss) for the year "
        "attributable to the equity holders of the Company"
    )
    assert profit_row_concept(earnings_per_share) is None
    assert profit_row_concept(
        "Equity holders of the Company",
        "Attributable to: " + earnings_per_share,
    ) is None
    assert profit_row_concept(
        "Profit attributable to equity holders",
        "Earnings per ADS attributable to equity holders",
    ) is None
    assert profit_row_concept("Profit/(loss) for the year") == "net_income"


def test_unattributed_shareholders_equity_is_parent_only_with_exact_local_nci_then_total():
    for label, unstructured_concept in (
        ("Total shareholders’ equity", "total_equity"),
        ("Total stockholders' equity", None),
    ):
        assert equity_row_concept(
            label,
            following_labels=("Noncontrolling interests", "Total equity"),
        ) == "equity"
        assert equity_row_concept(label) == unstructured_concept
        assert equity_row_concept(
            label,
            following_labels=("Noncontrolling interests were 57,539", "Total equity was 1,118,425"),
        ) == unstructured_concept
        assert equity_row_concept(
            label,
            following_labels=("Total equity", "Noncontrolling interests"),
        ) == unstructured_concept
        assert equity_row_concept(
            label,
            following_labels=("Noncontrolling interests", "Total liabilities and equity"),
        ) == unstructured_concept
        assert equity_row_concept(
            label,
            following_labels=("Other reserve", "Other payables", "Noncontrolling interests", "Total equity"),
        ) == unstructured_concept
