import pytest

from openthesis.financial_contracts import missing_profile_concepts
from openthesis.financials import (
    _annual_roe_details,
    _fact_money_metadata,
    calculate_interim_metrics,
    deterministic_summary,
)
from openthesis.financial_structured_roles import companyfacts_role
from openthesis.report_document import _financial_nodes
from openthesis.sec_client import SecClient


def test_profit_availability_never_changes_the_reported_attribution():
    assert not missing_profile_concepts({"parent_net_income"}, {"net_income"})
    assert missing_profile_concepts({"net_income"}, {"parent_net_income"}) == {"parent_net_income"}
    assert companyfacts_role("us-gaap", "NetIncomeLoss") == "parent_net_income"
    assert companyfacts_role("us-gaap", "ProfitLoss") == "net_income"
    assert companyfacts_role("us-gaap", "StockholdersEquity") == "equity"
    assert companyfacts_role("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment") == "capital_expenditure_ppe"


def test_roe_pairs_matching_attribution_and_preserves_the_exact_formula():
    result = _annual_roe_details({"parent_net_income": 30, "equity": 120, "net_income": 40, "total_equity": 160}, {"equity": 80, "total_equity": 140})
    assert result["return_on_equity"] == .3
    assert result["return_on_equity_formula"] == "parent_net_income / average(opening_equity, closing_equity)"
    assert result["return_on_equity_attribution"] == {
        "profit_concept": "parent_net_income",
        "equity_concept": "equity",
    }
    mismatch = _annual_roe_details({"net_income": 40, "equity": 120}, {"equity": 80})
    assert mismatch["return_on_equity"] is None
    assert mismatch["return_on_equity_gap"] == "profit_equity_attribution_mismatch"


def test_parent_only_report_shows_parent_profit_without_mislabelling_group_profit():
    for language, label in (("en", "Profit attributable to owners"), ("zh-CN", "归母净利润"), ("zh-Hant", "歸母淨利潤")):
        text = deterministic_summary("Test", [{"year": 2025, "revenue": 100, "parent_net_income": 30, "operating_cash_flow": 40}], language, "USD")
        assert label in text and "$30" in text


_PROFIT_LABELS = {
    "en": ("Net income", "Profit attributable to owners"),
    "zh-CN": ("净利润", "归母净利润"),
    "zh-Hant": ("淨利潤", "歸母淨利潤"),
}


def _financial_report_tables(content, language):
    overview, _balance = _financial_nodes(content, language)
    tables = [node for node in overview if node.kind == "table" and node.rows]
    annual = next(node for node in tables if node.rows[0][0] in {"Fiscal year", "财年", "財年"})
    interim = next(node for node in tables if node.rows[0][0] in {"Period", "期间", "期間"})
    return annual, interim


def _display_metadata(concept):
    return {
        "currency": "USD",
        "unit_provenance": "explicit_source_unit",
        "scope": "consolidated",
        "generation_id": "accepted-generation-2025",
        "evidence_ids": [f"fact:{concept}-2025"],
    }


@pytest.mark.parametrize("language", ("en", "zh-CN", "zh-Hant"))
def test_report_document_shows_parent_profit_in_annual_and_quarterly_tables(language):
    _net_label, parent_label = _PROFIT_LABELS[language]
    content = {
        "currency": "USD",
        "metrics": [{
            "year": 2025,
            "revenue": 100,
            "parent_net_income": 15,
            "operating_cash_flow": 30,
            "money_metadata": {
                concept: _display_metadata(concept)
                for concept in ("revenue", "parent_net_income", "operating_cash_flow")
            },
        }],
        "interim_metrics": [{
            "year": 2025,
            "period": "Q2",
            "revenue": 50,
            "parent_net_income": 8,
            "operating_cash_flow": 12,
            "money_metadata": {
                concept: _display_metadata(concept)
                for concept in ("revenue", "parent_net_income", "operating_cash_flow")
            },
        }],
    }

    annual, interim = _financial_report_tables(content, language)

    assert parent_label in annual.rows[0]
    assert annual.rows[1][annual.rows[0].index(parent_label)] == "$15"
    assert parent_label in interim.rows[0]
    assert interim.rows[1][interim.rows[0].index(parent_label)] == "$8"


@pytest.mark.parametrize("language", ("en", "zh-CN", "zh-Hant"))
def test_report_document_keeps_group_and_parent_profit_as_distinct_columns(language):
    net_label, parent_label = _PROFIT_LABELS[language]
    content = {
        "currency": "USD",
        "metrics": [{
            "year": 2025,
            "revenue": 100,
            "net_income": 20,
            "parent_net_income": 15,
            "operating_cash_flow": 30,
            "money_metadata": {
                concept: _display_metadata(concept)
                for concept in ("revenue", "net_income", "parent_net_income", "operating_cash_flow")
            },
        }],
        "interim_metrics": [{
            "year": 2025,
            "period": "Q2",
            "revenue": 50,
            "net_income": 10,
            "parent_net_income": 8,
            "operating_cash_flow": 12,
            "money_metadata": {
                concept: _display_metadata(concept)
                for concept in ("revenue", "net_income", "parent_net_income", "operating_cash_flow")
            },
        }],
    }

    annual, interim = _financial_report_tables(content, language)

    for table, net_value, parent_value in ((annual, "$20", "$15"), (interim, "$10", "$8")):
        assert net_label in table.rows[0]
        assert parent_label in table.rows[0]
        assert table.rows[0].index(net_label) != table.rows[0].index(parent_label)
        assert table.rows[1][table.rows[0].index(net_label)] == net_value
        assert table.rows[1][table.rows[0].index(parent_label)] == parent_value


def _interim_fact(concept, value, **overrides):
    fact = {
        "fact_id": f"{concept}-2025-q2",
        "concept": concept,
        "value": value,
        "fiscal_year": 2025,
        "fiscal_period": "Q2",
        "start_date": "2025-01-01",
        "end_date": "2025-06-30",
        "filed_at": "2025-08-01",
        "accession_number": "quarterly-2025-q2",
        "currency": "USD",
        "unit_scale": 1.0,
        "unit_provenance": "explicit_xbrl_unit",
        "scope": "consolidated",
        "generation_id": "generation-2025-q2",
        "parser_version": "sec-companyfacts-v4",
        "source_document": "sha256:quarterly-source",
        "usage_status": "canonical_research",
    }
    fact.update(overrides)
    return fact


def test_interim_fcf_requires_matching_complete_source_metadata():
    facts = [
        _interim_fact("operating_cash_flow", 80),
        _interim_fact("capital_expenditure", 20),
    ]

    metric = calculate_interim_metrics(facts)[0]

    assert metric["free_cash_flow"] == 60
    for concept in ("operating_cash_flow", "capital_expenditure"):
        metadata = metric["money_metadata"][concept]
        assert metadata["currency"] == "USD"
        assert metadata["scope"] == "consolidated"
        assert metadata["generation_id"] == "generation-2025-q2"
        assert metadata["period_start"] == "2025-01-01"
        assert metadata["period_end"] == "2025-06-30"
        assert metadata["unit_provenance"] == "explicit_xbrl_unit"
        assert metadata["fact_id"] == f"{concept}-2025-q2"
        assert _fact_money_metadata(facts[0])["period_start"] == "2025-01-01"


@pytest.mark.parametrize(
    ("override", "expected_gap"),
    (
        ({"currency": "CNY"}, "currency_mismatch"),
        ({"scope": "parent"}, "scope_mismatch"),
        ({"generation_id": "generation-other"}, "generation_mismatch"),
        ({"start_date": "2025-04-01"}, "inputs_not_comparable"),
    ),
)
def test_interim_fcf_is_not_calculated_for_incomparable_inputs(override, expected_gap):
    facts = [
        _interim_fact("operating_cash_flow", 80),
        _interim_fact("capital_expenditure", 20, **override),
    ]

    metric = calculate_interim_metrics(facts)[0]

    assert facts[0]["end_date"] == facts[1]["end_date"] == "2025-06-30"
    assert metric["free_cash_flow"] is None
    assert metric["free_cash_flow_gap"] == expected_gap


def test_minority_interest_equity_uses_instant_period_semantics():
    assert SecClient._period_kind_for_concept("minority_interest_equity") == "instant"
