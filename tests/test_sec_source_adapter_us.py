from __future__ import annotations

from openthesis.domain import (
    Company,
    FilingDocument,
    FinancialFact,
    FinancialNormalizationLineage,
)
from openthesis.sec_client import SecFinancialSourceAdapter


class FakeCompanyFactsClient:
    def __init__(self, facts: list[FinancialFact]) -> None:
        self.facts = facts
        self.calls: list[Company] = []

    def get_company_facts(self, company: Company) -> list[FinancialFact]:
        self.calls.append(company)
        return self.facts


class FailOnceCompanyFactsClient:
    def __init__(self, facts: list[FinancialFact]) -> None:
        self.facts = facts
        self.calls: list[Company] = []

    def get_company_facts(self, company: Company) -> list[FinancialFact]:
        self.calls.append(company)
        if len(self.calls) == 1:
            raise RuntimeError("sensitive transport detail must not be retained")
        return self.facts


def _fact(
    *,
    fact_id: str,
    company_cik: str,
    accession: str,
    end_date: str,
    currency: str = "USD",
    concept: str = "revenue",
    fiscal_year: int = 2025,
    fiscal_period: str = "FY",
    unit_ids: tuple[str, ...] = (),
) -> FinancialFact:
    lineage = None
    if unit_ids:
        inputs = ()
        method = "explicit-source-unit-v1"
        transform = "identity"
        raw_value = "123.45"
        if len(unit_ids) > 1:
            method = "sec-xbrl-arithmetic-v1"
            transform = "subtract"
            raw_value = ""
            inputs = (
                FinancialNormalizationLineage(
                    raw_value="1000",
                    scale="1",
                    normalized_value="1000",
                    quantity_kind="money",
                    currency=currency,
                    unit_evidence_ids=(unit_ids[0],),
                ),
                FinancialNormalizationLineage(
                    raw_value="876.55",
                    scale="1",
                    normalized_value="876.55",
                    quantity_kind="money",
                    currency=currency,
                    unit_evidence_ids=(unit_ids[1],),
                ),
            )
        lineage = FinancialNormalizationLineage(
            raw_value=raw_value,
            scale="1",
            normalized_value="123.45",
            quantity_kind="money",
            currency=currency,
            unit_evidence_ids=unit_ids,
            method=method,
            value_transform=transform,
            inputs=inputs,
        )
    reported_concept = (
        "us-gaap:derived-liabilities" if concept == "liabilities" else "Revenue"
    )
    return FinancialFact(
        fact_id=fact_id,
        company_cik=company_cik,
        concept=concept,
        reported_concept=reported_concept,
        value=123.45,
        unit=currency,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        form_type="10-K",
        start_date=f"{fiscal_year}-01-01",
        end_date=end_date,
        filed_at="2026-02-20",
        accession_number=accession,
        source_url=f"https://www.sec.gov/Archives/edgar/data/{company_cik}/{accession}/",
        currency=currency,
        source_document=f"SEC CompanyFacts {reported_concept}",
        raw_text=f"{reported_concept}=123.45 {currency}",
        normalization_lineage=lineage,
    )


def _filing(company: Company, accession: str, end_date: str) -> FilingDocument:
    return FilingDocument(
        document_id=f"filing:{accession}",
        company_cik=company.cik,
        accession_number=accession,
        form_type="10-K",
        fiscal_period="FY",
        period_end=end_date,
        filed_at="2026-02-20",
        primary_document="annual.htm",
        source_url=f"https://www.sec.gov/Archives/edgar/data/{company.cik}/{accession}/",
    )


def test_us_multiple_filings_reuse_company_facts_cache_without_mutating_company() -> None:
    company = Company(
        cik="0000123456",
        ticker="ACME",
        name="Acme Inc.",
        market="US",
        listing_currency="USD",
        reporting_currency="EUR",
    )
    original = company.to_dict()
    accession_a = "0000123456-25-000001"
    accession_b = "0000123456-26-000001"
    fact_a = _fact(
        fact_id="a", company_cik=company.cik, accession=accession_a,
        end_date="2024-12-31", unit_ids=("unit:row-a",),
    )
    fact_b = _fact(
        fact_id="b", company_cik=company.cik, accession=accession_b,
        end_date="2025-12-31", concept="liabilities",
        unit_ids=("unit:input-a", "unit:input-b"),
    )
    fact_a_before = fact_a.to_dict()
    fact_b_before = fact_b.to_dict()
    filing_a = _filing(company, accession_a, "2024-12-31")
    filing_b = _filing(company, accession_b, "2025-12-31")
    filing_a_before = filing_a.to_dict()
    filing_b_before = filing_b.to_dict()
    client = FakeCompanyFactsClient([fact_a, fact_b])
    adapter = SecFinancialSourceAdapter(client)  # type: ignore[arg-type]

    first, first_refs, first_error = adapter.fetch(company, filing_a)
    second, second_refs, second_error = adapter.fetch(company, filing_b)

    assert first_error is None and second_error is None
    assert first == [fact_a] and second == [fact_b]
    assert len(client.calls) == 1
    assert client.calls[0] is company
    assert company.to_dict() == original
    assert fact_a.to_dict() == fact_a_before and fact_b.to_dict() == fact_b_before
    assert filing_a.to_dict() == filing_a_before and filing_b.to_dict() == filing_b_before
    assert {ref.evidence_id for ref in first_refs} == {"fact:a", "unit:row-a"}
    assert {ref.evidence_id for ref in second_refs} == {
        "fact:b", "unit:input-a", "unit:input-b",
    }


def test_us_companyfacts_selection_requires_official_cik_and_exact_accession() -> None:
    company = Company(
        cik="0000123456", ticker="ACME", name="Acme Inc.", market="US"
    )
    accession = "0000123456-25-000001"
    matching = _fact(
        fact_id="matching", company_cik=company.cik, accession=accession,
        end_date="2025-12-31",
    )
    wrong_accession = _fact(
        fact_id="wrong-accession", company_cik=company.cik,
        accession="0000123456-24-000009", end_date="2025-12-31",
    )
    wrong_company = _fact(
        fact_id="wrong-company", company_cik="0000654321",
        accession=accession, end_date="2025-12-31",
    )
    client = FakeCompanyFactsClient([matching, wrong_accession, wrong_company])

    facts, _refs, error = SecFinancialSourceAdapter(client).fetch(
        company, _filing(company, accession, "2025-12-31")  # type: ignore[arg-type]
    )

    assert error is None
    assert facts == [matching]


def test_us_same_accession_comparison_columns_ignore_filing_end_and_listing_currency() -> None:
    company = Company(
        cik="0000123456",
        ticker="ACME",
        name="Acme Inc.",
        market="US",
        listing_currency="USD",
        reporting_currency="EUR",
    )
    accession = "0000123456-25-000001"
    current = _fact(
        fact_id="current", company_cik=company.cik, accession=accession,
        end_date="2025-12-31", currency="USD", fiscal_year=2025,
    )
    comparison = _fact(
        fact_id="comparison", company_cik=company.cik, accession=accession,
        end_date="2024-12-31", currency="CNY", fiscal_year=2024,
    )
    client = FakeCompanyFactsClient([current, comparison])

    facts, _refs, error = SecFinancialSourceAdapter(client).fetch(
        company, _filing(company, accession, "2025-12-31")  # type: ignore[arg-type]
    )

    assert error is None
    assert facts == [current, comparison]
    assert comparison.currency == "CNY"
    assert company.listing_currency == "USD"


def test_hk_mapping_keeps_existing_period_currency_and_fiscal_period_selection() -> None:
    company = Company(
        cik="HK:SEHK:00005.HK",
        ticker="00005.HK",
        name="HSBC",
        market="HK",
        reporting_currency="GBP",
    )
    accession = "0001-26-001"
    matching = _fact(
        fact_id="hk-match", company_cik="0001089113", accession=accession,
        end_date="2025-12-31", currency="USD", fiscal_year=2025,
    )
    wrong_end = _fact(
        fact_id="hk-wrong-end", company_cik="0001089113", accession=accession,
        end_date="2024-12-31", currency="USD", fiscal_year=2024,
    )
    wrong_currency = _fact(
        fact_id="hk-wrong-currency", company_cik="0001089113", accession=accession,
        end_date="2025-12-31", currency="HKD", fiscal_year=2025,
    )
    wrong_period = _fact(
        fact_id="hk-wrong-period", company_cik="0001089113", accession=accession,
        end_date="2025-12-31", currency="USD", fiscal_year=2025,
        fiscal_period="Q4",
    )
    wrong_company = _fact(
        fact_id="hk-wrong-company", company_cik="0000654321", accession=accession,
        end_date="2025-12-31", currency="USD", fiscal_year=2025,
    )
    client = FakeCompanyFactsClient(
        [matching, wrong_end, wrong_currency, wrong_period, wrong_company]
    )

    facts, _refs, error = SecFinancialSourceAdapter(client).fetch(
        company, _filing(company, accession, "2025-12-31")  # type: ignore[arg-type]
    )

    assert error is None
    assert facts == [matching]
    assert len(client.calls) == 1
    assert client.calls[0].cik == "0001089113"


def test_failed_company_facts_fetch_is_cached_until_a_new_adapter_instance() -> None:
    company = Company(
        cik="0000123456", ticker="ACME", name="Acme Inc.", market="US"
    )
    accession_a = "0000123456-25-000001"
    accession_b = "0000123456-26-000001"
    facts = [
        _fact(
            fact_id="a", company_cik=company.cik, accession=accession_a,
            end_date="2024-12-31",
        ),
        _fact(
            fact_id="b", company_cik=company.cik, accession=accession_b,
            end_date="2025-12-31",
        ),
    ]
    client = FailOnceCompanyFactsClient(facts)
    adapter = SecFinancialSourceAdapter(client)  # type: ignore[arg-type]

    _first, _first_refs, first_error = adapter.fetch(
        company, _filing(company, accession_a, "2024-12-31")
    )
    _second, _second_refs, second_error = adapter.fetch(
        company, _filing(company, accession_b, "2025-12-31")
    )

    assert first_error == "sec_structured_source_failed:RuntimeError"
    assert second_error == first_error
    assert "sensitive transport detail" not in (first_error or "")
    assert len(client.calls) == 1

    recovered, _recovered_refs, recovery_error = SecFinancialSourceAdapter(
        client  # type: ignore[arg-type]
    ).fetch(company, _filing(company, accession_b, "2025-12-31"))

    assert recovery_error is None
    assert recovered == [facts[1]]
    assert len(client.calls) == 2
