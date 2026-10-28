from decimal import Decimal, localcontext
from dataclasses import replace
import unittest

from openthesis.domain import EvidenceRef, FinancialFact, FinancialNormalizationLineage
from openthesis.financial_contracts import QuantityKind, SourceAmount, exact_decimal_arithmetic
from openthesis.financial_validation import FinancialValidator


class SourceAmountTests(unittest.TestCase):
    def test_preserves_source_precision_even_with_small_global_context(self):
        with localcontext() as context:
            context.prec = 6
            result = SourceAmount(
                "(1,234,567,890,123,456,789.125)", "1000000", QuantityKind.MONEY,
                "CNY", ("page:12:unit-row",),
            ).normalize()
        self.assertEqual(result.normalized_value, "-1234567890123456789125000.000")
        self.assertEqual(result.raw_value, "(1,234,567,890,123,456,789.125)")
        self.assertEqual(result.unit_evidence_ids, ("page:12:unit-row",))

    def test_missing_or_unknown_units_and_currency_cannot_be_imputed(self):
        examples = (
            SourceAmount("0.85", "1", QuantityKind.UNKNOWN),
            SourceAmount("0.85", "1", "unknown", "", ("source",)),
            SourceAmount("100", "1000", QuantityKind.MONEY, "", ("source",)),
            SourceAmount("100", "1000", QuantityKind.MONEY, "CNY"),
            SourceAmount(100.0, "1", QuantityKind.MONEY, "CNY", ("source",)),
        )
        for amount in examples:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                amount.normalize()

    def test_scale_non_finite_and_ambiguous_numeric_formats_are_rejected(self):
        for value, scale in (("NaN", "1"), ("1", "0"), ("1", "Infinity"), ("1.234,56", "1"), ("12,34", "1")):
            with self.subTest(value=value, scale=scale), self.assertRaises(ValueError):
                SourceAmount(value, scale, QuantityKind.MONEY, "CNY", ("source",)).normalize()

    def test_ratios_and_shares_have_separate_quantity_identity(self):
        ratio = SourceAmount("85", "0.01", QuantityKind.RATIO, unit_evidence_ids=("percent-header",)).normalize()
        shares = SourceAmount("1,000", "1000", QuantityKind.SHARES, unit_evidence_ids=("shares-header",)).normalize()
        self.assertEqual(ratio.normalized_value, "0.85")
        self.assertEqual(shares.normalized_value, "1000000")
        self.assertNotEqual(ratio.quantity_kind, shares.quantity_kind)
        self.assertEqual(shares.currency, "")

    def test_absolute_outflow_convention_is_explicit_in_lineage(self):
        with localcontext() as context:
            context.prec = 6
            result = SourceAmount(
                "(1,234.50)", "1000", QuantityKind.MONEY, "CNY", ("unit-page",),
                "absolute_value",
            ).normalize()
        self.assertEqual(result.normalized_value, "1234500.00")
        self.assertEqual(result.value_transform, "absolute_value")

    def test_derived_arithmetic_preserves_aligned_source_decimal_places(self):
        values = (Decimal("1234567890123456789.125"), Decimal("1.001"))
        with localcontext() as context:
            context.prec = 6
            added = exact_decimal_arithmetic(values)
            subtracted = exact_decimal_arithmetic(values, "subtract")
        self.assertEqual(str(added), "1234567890123456790.126")
        self.assertEqual(str(subtracted), "1234567890123456788.124")


class DerivedLineageValidationTests(unittest.TestCase):
    def test_sec_xbrl_subtraction_requires_and_validates_every_input_unit(self):
        document_id = "sec:0001:2025"
        source_url = "https://www.sec.gov/Archives/edgar/data/1/000125001/"
        unit_ids = (
            "unit:sec-input:0000000001:us-gaap:LiabilitiesAndStockholdersEquity:USD:0001-25-001:2025-12-31",
            "unit:sec-input:0000000001:us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest:USD:0001-25-001:2025-12-31",
        )
        inputs = (
            SourceAmount("100", "1", QuantityKind.MONEY, "USD", (unit_ids[0],)).normalize(),
            SourceAmount("45", "1", QuantityKind.MONEY, "USD", (unit_ids[1],)).normalize(),
        )
        lineage = FinancialNormalizationLineage(
            raw_value="", scale="1", normalized_value="55", quantity_kind="money",
            currency="USD", unit_evidence_ids=unit_ids,
            method="sec-xbrl-arithmetic-v1", value_transform="subtract", inputs=inputs,
        )
        fact = FinancialFact(
            fact_id="derived-liabilities", company_cik="0000000001", concept="liabilities",
            reported_concept="derived liabilities (AssetsLessEquity)", value=55.0,
            unit="USD", fiscal_year=2025, fiscal_period="FY", form_type="10-K",
            start_date=None, end_date="2025-12-31", filed_at="2026-02-01",
            accession_number="0001-25-001", source_url=source_url,
            statement="balance_sheet", currency="USD", unit_scale=1.0,
            unit_provenance="structured_normalized", source_document="SEC CompanyFacts us-gaap:derived-liabilities",
            raw_text="Derived liabilities = 100 - 45", parser_version="sec-companyfacts-v3",
            normalization_lineage=lineage,
        )
        row_ref = EvidenceRef(
            "fact:derived-liabilities", document_id, source_url, "derived liabilities",
            "accession:0001-25-001", fact.raw_text, fact.filed_at,
        )
        unit_refs = tuple(
            EvidenceRef(
                evidence_id, document_id, source_url, "SEC CompanyFacts XBRL unit",
                f"xbrl:us-gaap/input-{index}/units/USD/accession/0001-25-001/end/2025-12-31",
                f"CompanyFacts formula input unit {index}: USD", fact.filed_at,
            )
            for index, evidence_id in enumerate(unit_ids, start=1)
        )
        result = FinancialValidator().validate_group(
            [fact],
            ("0001-25-001", "2025-12-31", "FY", "consolidated", "USD"),
            evidence_map={fact.fact_id: row_ref},
            required_concepts={"liabilities"},
            expected_company_id="0000000001",
            source_evidence=(row_ref, *unit_refs),
        )
        self.assertEqual(result.validation.status.value, "VERIFIED")
        self.assertEqual(result.validation.accepted, (fact,))

    def test_visual_fact_lineage_is_required_and_value_bound(self):
        from openthesis.vision_financials import parse_vision_json
        from test_vision_financials import RecordingVisionProvider, _company, _filing, _page

        payload = RecordingVisionProvider().generate_vision("", "original_page 130", b"png")
        observation = payload["facts"][0]
        observation.update(value="1.25", unit_scale="1000000", unit_text="CNY million",
            raw_text="Consolidated Income Statement; Period ended 2025-12-31; Unit: CNY million; Revenue 1.25")
        parsed = parse_vision_json(payload, _company(), _filing(), (_page(),))
        fact = parsed.facts[0]
        self.assertEqual(fact.value, 1_250_000.0)
        self.assertEqual(fact.normalization_lineage.raw_value, "1.25")
        row_ref = next(item for item in parsed.evidence if item.evidence_id == f"fact:{fact.fact_id}")
        identity = (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency)
        validator = FinancialValidator()
        valid = validator.validate_group(
            [fact], identity, evidence_map={fact.fact_id: row_ref},
            required_concepts={"revenue"}, expected_company_id=fact.company_cik,
            source_evidence=parsed.evidence,
        )
        invalid = validator.validate_group(
            [replace(fact, value=1_250_001.0)], identity,
            evidence_map={fact.fact_id: row_ref}, required_concepts={"revenue"},
            expected_company_id=fact.company_cik, source_evidence=parsed.evidence,
        )
        self.assertEqual(valid.validation.status.value, "VERIFIED")
        self.assertIn("normalized_amount_does_not_match_fact", invalid.validation.issues)


if __name__ == "__main__":
    unittest.main()
