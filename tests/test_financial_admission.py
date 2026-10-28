from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact
from openthesis.financial_admission import FinancialAdmission


class FinancialAdmissionTests(unittest.TestCase):
    def test_profile_and_capabilities_are_projected_once_from_canonical_facts(self) -> None:
        facts = tuple(FinancialFact(**item) for item in demo_facts())
        canonical = SimpleNamespace(
            research_facts=facts,
            validations=(),
            group_validations=(),
            manifests=(),
            evidence=(),
            diagnostics=("canonical",),
            capabilities={
                "financial_snapshot": True,
                "annual_trend": True,
                "interim_comparison": False,
                "model_financial_analysis": True,
            },
        )
        admitted = FinancialAdmission().admit(DEMO_COMPANY, canonical)
        self.assertTrue(admitted.model_ready)
        self.assertTrue(admitted.profile.metrics)
        self.assertEqual(admitted.fact_dicts, admitted.profile.fact_dicts)
        self.assertEqual(admitted.reporting_currency, DEMO_COMPANY.reporting_currency)
        self.assertIn("canonical", admitted.diagnostics)

    def test_empty_metric_projection_revokes_model_capability(self) -> None:
        canonical = SimpleNamespace(
            research_facts=(), validations=(), group_validations=(), manifests=(),
            evidence=(), diagnostics=(),
            capabilities={"financial_snapshot": True, "model_financial_analysis": True},
        )
        admitted = FinancialAdmission().admit(DEMO_COMPANY, canonical)
        self.assertFalse(admitted.model_ready)
        self.assertFalse(admitted.capability_map["financial_snapshot"])
        self.assertFalse(admitted.capability_map["model_financial_analysis"])

    def test_persisted_facts_do_not_claim_unavailable_trend_or_interim_capabilities(self) -> None:
        fact = FinancialFact(**demo_facts()[0])
        admitted = FinancialAdmission().admit_facts(DEMO_COMPANY, (fact,))
        self.assertFalse(admitted.capability_map["annual_trend"])
        self.assertFalse(admitted.capability_map["interim_comparison"])

    def test_stale_pdf_parser_fact_is_quarantined_by_admission_contract(self) -> None:
        stale = replace(
            FinancialFact(**demo_facts()[0]),
            parser_version="financial-ingestion-ast-v7",
            validation_status="VERIFIED",
            provenance_status="verified",
            usage_status="canonical_research",
        )

        admitted = FinancialAdmission().admit_facts(DEMO_COMPANY, (stale,))

        self.assertEqual(admitted.admission_contract_version, "financial-admission-v1")
        self.assertEqual(admitted.facts, ())
        self.assertEqual(admitted.quarantined_facts, (stale,))
        self.assertIn(
            f"stale_financial_parser_contract:{stale.fact_id}", admitted.diagnostics
        )


if __name__ == "__main__":
    unittest.main()
