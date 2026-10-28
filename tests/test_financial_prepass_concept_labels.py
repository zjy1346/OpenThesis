from __future__ import annotations

from openthesis.financial_ingestion import (
    _LABELS,
    _STATEMENT_FOR,
    _candidate_pages_from_text,
    _label_compact,
)


def test_every_financial_statement_concept_has_prepass_label_coverage() -> None:
    missing = set(_STATEMENT_FOR) - set(_LABELS)

    assert missing == set()


def test_operating_cash_and_capex_component_labels_keep_distinct_roles() -> None:
    before_tax = {_label_compact(label) for label in _LABELS["operating_cash_flow_before_tax"]}
    net_operating = {_label_compact(label) for label in _LABELS["operating_cash_flow"]}
    total_capex = {_label_compact(label) for label in _LABELS["capital_expenditure"]}
    ppe = {_label_compact(label) for label in _LABELS["capital_expenditure_ppe"]}
    intangible = {_label_compact(label) for label in _LABELS["capital_expenditure_intangible"]}

    assert _label_compact("Cash flows from (used in) operations") in before_tax
    assert _label_compact("经营活动现金流量（税前）") in before_tax
    assert _label_compact("net cash provided by operating activities") in net_operating
    assert before_tax.isdisjoint(net_operating)
    assert not any(before in net for before in before_tax for net in net_operating)
    assert not any(net in before for before in before_tax for net in net_operating)

    assert _label_compact("payments to acquire property, plant and equipment") in ppe
    assert _label_compact("购建固定资产支付的现金") in ppe
    assert _label_compact("payments to acquire intangible assets") in intangible
    assert _label_compact("购建无形资产支付的现金") in intangible
    assert ppe.isdisjoint(intangible)
    assert ppe.isdisjoint(total_capex)
    assert intangible.isdisjoint(total_capex)


def test_prepass_selects_pages_with_distinct_cash_and_asset_component_labels() -> None:
    pages = [
        "CONSOLIDATED INCOME STATEMENT",
        "CONSOLIDATED BALANCE SHEET",
        "CONSOLIDATED STATEMENT OF CASH FLOWS",
        "General narrative without financial-statement line labels.",
        "Cash flows from (used in) operations / 经营活动现金流量（税前）\n"
        "Net cash provided by operating activities / 经营活动产生的现金流量净额",
        "Payments to acquire property, plant and equipment / 购建固定资产支付的现金\n"
        "Payments to acquire intangible assets / 购建无形资产支付的现金",
        "Unrelated management discussion.",
        "Appendix with no statement amounts.",
    ]

    selected = _candidate_pages_from_text(pages, continuation_pages=0)

    assert selected is not None
    assert {5, 6}.issubset(selected)
    assert 8 not in selected
