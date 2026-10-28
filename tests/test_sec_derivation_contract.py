from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from openthesis.domain import Company
from openthesis.sec_client import SecClient


def _row(
    value: int | Decimal,
    year: int = 2025,
    *,
    accession: str | None = None,
) -> dict[str, object]:
    accession = accession or f"0001-{str(year)[-2:]}-001"
    return {
        "val": value,
        "fy": year,
        "fp": "FY",
        "form": "10-K",
        "start": None,
        "end": f"{year}-12-31",
        "filed": f"{year + 1}-02-01",
        "accn": accession,
    }


def _companyfacts(tags: dict[str, dict[str, dict[str, list[dict[str, object]]]]]) -> dict:
    return {"facts": {"us-gaap": tags, "dei": {}}}


def _client() -> SecClient:
    # get_company_facts uses only the patched JSON boundary and stateless
    # selectors, so avoid creating a cache or depending on pytest temp paths.
    return object.__new__(SecClient)


def _company() -> Company:
    return Company(
        cik="0000001234",
        ticker="TEST",
        name="Synthetic issuer",
        reporting_currency="USD",
    )


def _liabilities(client: SecClient, payload: dict) -> list:
    with patch.object(SecClient, "_get_json", return_value=payload):
        facts = client.get_company_facts(_company())
    return [fact for fact in facts if fact.concept == "liabilities"]


def test_parent_equity_without_nci_is_not_zero_across_multiple_fy() -> None:
    rows = [_row(100, year) for year in (2023, 2024, 2025)]
    equity = [_row(40, year) for year in (2023, 2024, 2025)]
    payload = _companyfacts(
        {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": rows}},
            "StockholdersEquity": {"units": {"USD": equity}},
        }
    )

    derived = _liabilities(_client(), payload)

    assert derived == []


def test_close_complete_formula_candidates_are_not_auto_reconciled() -> None:
    payload = _companyfacts(
        {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [_row(200)]}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {
                "units": {"USD": [_row(100)]}
            },
            "LiabilitiesCurrent": {"units": {"USD": [_row(60)]}},
            "LiabilitiesNoncurrent": {"units": {"USD": [_row(Decimal("40.5"))]}},
        }
    )

    derived = _liabilities(_client(), payload)

    assert derived == []


def test_explicit_zero_nci_supports_complete_parent_equity_formula() -> None:
    payload = _companyfacts(
        {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [_row(100)]}},
            "StockholdersEquity": {"units": {"USD": [_row(40)]}},
            "MinorityInterestInConsolidatedEntity": {"units": {"USD": [_row(0)]}},
        }
    )

    derived = _liabilities(_client(), payload)

    assert len(derived) == 1
    assert derived[0].value == 60
    assert derived[0].normalization_lineage.normalized_value == "60"
    assert "MinorityInterestInConsolidatedEntity" in derived[0].reported_concept


def test_large_decimal_formula_conflict_of_one_is_not_hidden_by_float() -> None:
    base = 2**53
    payload = _companyfacts(
        {
            "LiabilitiesAndStockholdersEquity": {"units": {"USD": [_row(Decimal(base + 1000))]}},
            "StockholdersEquity": {"units": {"USD": [_row(Decimal(900))]}},
            "MinorityInterestInConsolidatedEntity": {"units": {"USD": [_row(Decimal(100))]}},
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {
                "units": {"USD": [_row(Decimal(1000))]}
            },
            "LiabilitiesCurrent": {"units": {"USD": [_row(Decimal(base - 1))]}},
            "LiabilitiesNoncurrent": {"units": {"USD": [_row(Decimal(2))]}},
        }
    )

    derived = _liabilities(_client(), payload)

    assert derived == []
