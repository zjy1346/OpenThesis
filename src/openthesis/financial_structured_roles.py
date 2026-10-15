"""Pure source role proof for the official SEC CompanyFacts namespaces.

No cross-role fallbacks: a parent result, total result, tax-before cash flow
or PPE payment remains that exact role even when another requested role is absent.
"""
from __future__ import annotations

import re

from .financial_xbrl_semantics import CONCEPTS

_US_EXTRA = {
    "PaymentsForAdditionsToPropertyPlantAndEquipment": "capital_expenditure_ppe",
    "CashAndCashEquivalentsAtCarryingValue": "cash",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": "cash",
    "AccountsReceivableNetCurrent": "accounts_receivable",
    "AccountsNotesAndLoansReceivableNetCurrent": "accounts_receivable",
    "InventoryNet": "inventory",
}
_IFRS = {
    "Revenue": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "ProfitLoss": "net_income",
    "ProfitLossAttributableToOwnersOfParent": "parent_net_income",
    "ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntity": "ordinary_shareholder_net_income",
    "ProfitLossFromOperatingActivities": "operating_income",
    "OperatingProfitLoss": "operating_income",
    "CashFlowsFromUsedInOperatingActivities": "operating_cash_flow",
    "CashFlowsFromUsedInOperations": "operating_cash_flow_before_tax",
    "PurchaseOfPropertyPlantAndEquipment": "capital_expenditure_ppe",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capital_expenditure_ppe",
    "Assets": "assets", "Liabilities": "liabilities",
    "EquityAttributableToOwnersOfParent": "equity", "Equity": "total_equity",
}


def companyfacts_role(namespace: str, tag: str) -> str | None:
    if namespace == "us-gaap":
        return CONCEPTS[tag][0] if tag in CONCEPTS else _US_EXTRA.get(tag)
    if namespace == "ifrs-full":
        return _IFRS.get(tag)
    if namespace == "dei" and tag == "EntityCommonStockSharesOutstanding":
        return "shares_outstanding"
    return None


def companyfacts_source_issue(fact, row) -> str | None:
    """Bind adapter metadata to the namespace/tag retained in its source row."""
    if fact.parser_version != "sec-companyfacts-v4":
        return None
    if fact.normalization_lineage is not None and fact.normalization_lineage.inputs:
        if "no reported NCI" in fact.reported_concept:
            return "structured_derivation_unsupported_absence_as_zero"
        # Registered arithmetic is checked separately against all retained leaves.
        return None
    match = re.match(r"^(us-gaap|ifrs-full|dei):([A-Za-z][A-Za-z0-9]*)=", fact.raw_text)
    if (row is None or row.excerpt != fact.raw_text or match is None
            or match[2] != fact.reported_concept
            or companyfacts_role(match[1], match[2]) != fact.concept):
        return "structured_source_role_mismatch"
    return None
