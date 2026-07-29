from __future__ import annotations

import hashlib
from typing import Any

from .domain import Company


DEMO_COMPANY = Company(
    cik="0000000000",
    ticker="DEMO",
    name="Example Cloud Systems（合成演示公司）",
    exchange="DEMO",
)


DEMO_VALUES = {
    2021: {
        "revenue": 4_200_000_000,
        "operating_income": 630_000_000,
        "net_income": 470_000_000,
        "operating_cash_flow": 760_000_000,
        "capital_expenditure": 260_000_000,
        "assets": 6_800_000_000,
        "liabilities": 3_100_000_000,
        "equity": 3_700_000_000,
        "cash": 1_050_000_000,
        "accounts_receivable": 520_000_000,
        "inventory": 90_000_000,
    },
    2022: {
        "revenue": 4_830_000_000,
        "operating_income": 773_000_000,
        "net_income": 560_000_000,
        "operating_cash_flow": 870_000_000,
        "capital_expenditure": 310_000_000,
        "assets": 7_450_000_000,
        "liabilities": 3_350_000_000,
        "equity": 4_100_000_000,
        "cash": 1_180_000_000,
        "accounts_receivable": 610_000_000,
        "inventory": 95_000_000,
    },
    2023: {
        "revenue": 5_600_000_000,
        "operating_income": 952_000_000,
        "net_income": 690_000_000,
        "operating_cash_flow": 1_020_000_000,
        "capital_expenditure": 390_000_000,
        "assets": 8_420_000_000,
        "liabilities": 3_720_000_000,
        "equity": 4_700_000_000,
        "cash": 1_290_000_000,
        "accounts_receivable": 730_000_000,
        "inventory": 110_000_000,
    },
    2024: {
        "revenue": 6_380_000_000,
        "operating_income": 1_085_000_000,
        "net_income": 780_000_000,
        "operating_cash_flow": 1_090_000_000,
        "capital_expenditure": 520_000_000,
        "assets": 9_650_000_000,
        "liabilities": 4_250_000_000,
        "equity": 5_400_000_000,
        "cash": 1_180_000_000,
        "accounts_receivable": 940_000_000,
        "inventory": 145_000_000,
    },
    2025: {
        "revenue": 7_150_000_000,
        "operating_income": 1_180_000_000,
        "net_income": 835_000_000,
        "operating_cash_flow": 1_010_000_000,
        "capital_expenditure": 710_000_000,
        "assets": 11_200_000_000,
        "liabilities": 5_050_000_000,
        "equity": 6_150_000_000,
        "cash": 1_020_000_000,
        "accounts_receivable": 1_260_000_000,
        "inventory": 205_000_000,
    },
}


def demo_facts() -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for year, concepts in DEMO_VALUES.items():
        for concept, value in concepts.items():
            key = f"demo|{year}|{concept}"
            facts.append(
                {
                    "fact_id": hashlib.sha256(key.encode()).hexdigest()[:24],
                    "company_cik": DEMO_COMPANY.cik,
                    "concept": concept,
                    "reported_concept": f"demo:{concept}",
                    "value": float(value),
                    "unit": "USD",
                    "fiscal_year": year,
                    "fiscal_period": "FY",
                    "form_type": "DEMO",
                    "start_date": f"{year}-01-01",
                    "end_date": f"{year}-12-31",
                    "filed_at": f"{year + 1}-02-15",
                    "accession_number": f"DEMO-{year}",
                    "source_url": "openthesis://synthetic-demo-data",
                    "scope": "consolidated",
                }
            )
    return facts

