from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FilingDocument, FinancialFact
from openthesis.financial_admission import FinancialAdmission
from openthesis.financial_contracts import (
    FinancialGroupValidation,
    FinancialValidation,
    ValidationStatus,
)
from openthesis.financial_ingestion import FilingManifest


def _filing(year: int, month_day: str) -> FilingDocument:
    return FilingDocument(
        document_id=f"period-coverage-{year}",
        company_cik=DEMO_COMPANY.cik,
        accession_number=f"DEMO-{year}",
        form_type="10-K",
        fiscal_period="FY",
        period_end=f"{year}{month_day}",
        filed_at=f"{year + 1}-02-15",
        primary_document=f"report-{year}.pdf",
        source_url=f"https://example.invalid/report-{year}.pdf",
    )


def _manifest(filing: FilingDocument) -> FilingManifest:
    return FilingManifest(
        document_id=filing.document_id,
        accession_number=filing.accession_number,
        source_url=filing.source_url,
        primary_document=filing.primary_document,
        form_type=filing.form_type,
        fiscal_period=filing.fiscal_period,
        period_end=filing.period_end,
        revision=filing.revision,
        supersedes_document_id=filing.supersedes_document_id,
        content_hash=filing.content_hash,
    )


def _canonical_annuals(month_day: str) -> tuple[SimpleNamespace, tuple[FilingDocument, ...]]:
    filings = tuple(_filing(year, month_day) for year in range(2021, 2026))
    facts: list[FinancialFact] = []
    groups: list[FinancialGroupValidation] = []
    demo_rows = demo_facts()
    for year in range(2021, 2024):
        year_facts = tuple(
            FinancialFact(
                **{
                    **row,
                    "end_date": f"{year}{month_day}",
                    "start_date": (
                        f"{year - 1}-04-01" if month_day == "-03-31"
                        else row["start_date"]
                    ),
                }
            )
            for row in demo_rows
            if row["fiscal_year"] == year
        )
        facts.extend(year_facts)
        filing = filings[year - 2021]
        groups.append(
            FinancialGroupValidation(
                (
                    filing.accession_number,
                    filing.period_end,
                    "FY",
                    "consolidated",
                    "USD",
                ),
                FinancialValidation(
                    ValidationStatus.VERIFIED,
                    (),
                    frozenset(fact.concept for fact in year_facts),
                    accepted=year_facts,
                ),
                role="target",
            )
        )

    # An older comparative column is present in the data but is not an
    # independently discovered official annual filing.
    comparator = replace(
        facts[0],
        fact_id="demo-older-comparator",
        fiscal_year=2019,
        end_date=f"2019{month_day}",
        accession_number="DEMO-COMPARATOR-2019",
        usage_status="comparator",
    )
    canonical = SimpleNamespace(
        research_facts=tuple(facts) + (comparator,),
        group_validations=tuple(groups),
        manifests=(_manifest(filings[3]),),
        evidence=(),
        diagnostics=(),
        capabilities={"financial_snapshot": True, "model_financial_analysis": True},
    )
    return canonical, filings


class DiscoveredPeriodCoverageTests(unittest.TestCase):
    def test_rejected_currency_lane_is_parsed_but_never_admitted(self):
        canonical, filings = _canonical_annuals("-12-31")
        missing = replace(canonical.research_facts[0], fact_id="2024-unproved-currency",
            fiscal_year=2024, accession_number=filings[3].accession_number,
            end_date=filings[3].period_end, currency="", unit="",
            validation_status="REJECTED")
        canonical.group_validations += (FinancialGroupValidation(
            (filings[3].accession_number, filings[3].period_end, "FY", "consolidated", "UNKNOWN"),
            FinancialValidation(ValidationStatus.REJECTED, ("currency_unproven",),
                frozenset(), quarantined=(missing,)), role="target"),)
        view = FinancialAdmission().admit(DEMO_COMPANY, canonical, selected_filings=filings,
            requested_annual_count=5)
        assert view.profile.period_coverage['parsed_annual_years'] == (2021, 2022, 2023, 2024)
        assert 2024 not in view.profile.period_coverage['admitted_annual_years']
        gap = next(row for row in view.profile.period_coverage['missing_or_rejected_years'] if row['year'] == 2024)
        assert gap['status'] == 'parsed_unadmitted'
        assert gap['reason_code'] == 'OFFICIAL_ANNUAL_NOT_ADMITTED'
        assert not any(fact.fiscal_year == 2024 for fact in view.facts)

    def test_restart_observation_recovers_only_parse_metadata(self):
        canonical, filings = _canonical_annuals("-12-31")
        observation = {'accession_number': filings[3].accession_number,
            'period_end': filings[3].period_end, 'fiscal_period': 'FY', 'source_hash': 'a' * 64}
        view = FinancialAdmission().admit(DEMO_COMPANY, canonical, selected_filings=filings,
            requested_annual_count=5, parsing_observations=(observation,))
        assert view.profile.period_coverage['parsed_annual_years'] == (2021, 2022, 2023, 2024)
        assert 2024 not in view.profile.period_coverage['admitted_annual_years']
        assert not any(fact.fiscal_year == 2024 for fact in view.facts)

    def test_admission_plans_from_discovered_fy_without_admitting_empty_years(self) -> None:
        canonical, filings = _canonical_annuals("-12-31")

        admitted = FinancialAdmission().admit(
            DEMO_COMPANY,
            canonical,
            selected_filings=(filings[0], filings[1], filings[2], filings[4]),
            requested_annual_count=5,
        )

        coverage = admitted.profile.period_coverage
        self.assertEqual(coverage["discovered_annual_years"], (2021, 2022, 2023, 2024, 2025))
        self.assertEqual(coverage["parsed_annual_years"], (2021, 2022, 2023))
        self.assertEqual(coverage["admitted_annual_years"], (2021, 2022, 2023))
        self.assertEqual(coverage["available_annual_years"], (2021, 2022, 2023))
        self.assertEqual(coverage["displayed_annual_years"], (2021, 2022, 2023))
        self.assertEqual(coverage["latest_official_fy"], 2025)
        self.assertEqual(coverage["latest_admitted_fy"], 2023)
        self.assertEqual(coverage["requested_annual_range"], ("2021-12-31", "2025-12-31"))
        self.assertEqual(
            [(item["year"], item["status"]) for item in coverage["missing_or_rejected_years"]],
            [(2024, "missing"), (2025, "missing")],
        )
        self.assertEqual(
            {
                fact.fiscal_year for fact in admitted.facts
                if fact.usage_status != "comparator"
            },
            {2021, 2022, 2023},
        )
        status_by_end = {
            item["period_end"]: item["status"]
            for item in admitted.profile.period_continuity
        }
        self.assertEqual(status_by_end["2024-12-31"], "no_facts")
        self.assertEqual(status_by_end["2025-12-31"], "no_facts")
        self.assertNotIn(2019, coverage["discovered_annual_years"])
        self.assertNotIn(2025, coverage["available_annual_years"])

    def test_requested_range_preserves_discovered_noncalendar_fy_end(self) -> None:
        canonical, filings = _canonical_annuals("-03-31")

        admitted = FinancialAdmission().admit(
            DEMO_COMPANY,
            canonical,
            selected_filings=(filings[0], filings[1], filings[2], filings[4]),
            requested_annual_count=5,
        )

        coverage = admitted.profile.period_coverage
        self.assertEqual(coverage["discovered_annual_years"], (2021, 2022, 2023, 2024, 2025))
        self.assertEqual(coverage["latest_official_fy"], 2025)
        self.assertEqual(coverage["latest_admitted_fy"], 2023)
        self.assertEqual(coverage["requested_annual_range"], ("2021-03-31", "2025-03-31"))
        self.assertEqual(coverage["admitted_annual_years"], (2021, 2022, 2023))


if __name__ == "__main__":
    unittest.main()
