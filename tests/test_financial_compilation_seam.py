"""Pure compilation exercises the same frozen-candidate seam as collection."""
from dataclasses import replace
import unittest

from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import CandidateBatch, CompilationPolicy, FactCandidate, PDF_PARSER_VERSION, QuantityKind, SourceAmount
from test_financial_compiler import _facts, _filing, _subject


class FinancialCompilationSeamTests(unittest.TestCase):
    def test_original_decimal_conflict_cannot_disappear_in_compatibility_float(self):
        filing = _filing("precision-conflict", "2022-12-31", "official original")
        base = _facts(filing, net_income=188_243_000_000)
        original = next(item for item in base if item.fact.concept == "operating_cash_flow")
        options = []
        unit_refs = []
        for suffix, raw_value in (("a", "9007199254740992.01"), ("b", "9007199254740992.02")):
            fact_id = f"exact-{suffix}"
            lineage = SourceAmount(raw_value, "1", QuantityKind.MONEY, "CNY", (f"unit:{fact_id}",)).normalize()
            fact = replace(original.fact, fact_id=fact_id, value=float(raw_value), unit_scale=1,
                           unit_provenance="explicit", normalization_lineage=lineage,
                           parser_version=PDF_PARSER_VERSION, raw_text=raw_value,
                           source_bbox=(10, 20, 600, 40))
            row_ref = replace(original.evidence[0], evidence_id=f"fact:{fact_id}", excerpt=raw_value,
                              bbox=fact.source_bbox)
            unit_refs.append(replace(row_ref, evidence_id=f"unit:{fact_id}", excerpt="单位：人民币元"))
            options.append(FactCandidate(fact, (row_ref,), "original-precision-fixture"))
        self.assertEqual(options[0].fact.value, options[1].fact.value)
        candidates = tuple(item for item in base if item.fact.concept != "operating_cash_flow") + tuple(options)
        result = FinancialFactCompiler().compile(
            _subject(), (CandidateBatch(filing, candidates, tuple(unit_refs)),),
            CompilationPolicy(filings=(filing,), reporting_currency="CNY"),
        )
        self.assertFalse(any(fact.concept == "operating_cash_flow" for fact in result.resolved_facts))
        self.assertEqual({fact.fact_id for fact in result.quarantined_facts}, {"exact-a", "exact-b"})
        self.assertEqual(len(result.resolved_facts), 5)

    def test_invalid_same_value_candidate_cannot_hide_a_verified_source(self):
        filing = _filing("same-value-proof", "2022-12-31", "official original")
        base = _facts(filing, net_income=188_243_000_000)
        original = next(item for item in base if item.fact.concept == "operating_cash_flow")
        bad = FactCandidate(
            replace(original.fact, fact_id="bad-proof", parser_version=PDF_PARSER_VERSION),
            (), "aaa-unproven",
        )
        result = FinancialFactCompiler().compile(
            _subject(), (CandidateBatch(filing, base + (bad,)),),
            CompilationPolicy(filings=(filing,), reporting_currency="CNY"),
        )
        self.assertIn(original.fact.fact_id, {fact.fact_id for fact in result.resolved_facts})
        self.assertIn("bad-proof", {fact.fact_id for fact in result.quarantined_facts})
        self.assertTrue(any("normalization_lineage_missing" in group.issues for group in result.validations))

    def test_malformed_normalization_is_quarantined_before_conflict_comparison(self):
        filing = _filing("malformed-proof", "2022-12-31", "official original")
        base = _facts(filing, net_income=188_243_000_000)
        original = next(item for item in base if item.fact.concept == "operating_cash_flow")
        lineage = SourceAmount("146091", "1000000", QuantityKind.MONEY, "CNY", ("unit:bad",)).normalize()
        bad = FactCandidate(
            replace(original.fact, fact_id="bad-decimal", normalization_lineage=replace(lineage, normalized_value="corrupt")),
            original.evidence, "aaa-malformed",
        )
        result = FinancialFactCompiler().compile(
            _subject(), (CandidateBatch(filing, base + (bad,)),),
            CompilationPolicy(filings=(filing,), reporting_currency="CNY"),
        )
        self.assertIn(original.fact.fact_id, {fact.fact_id for fact in result.resolved_facts})
        self.assertIn("bad-decimal", {fact.fact_id for fact in result.quarantined_facts})
        self.assertTrue(any("normalization_lineage_invalid" in group.issues for group in result.validations))

    def test_frozen_candidate_result_is_deterministic_without_an_adapter(self):
        filing = _filing("pure-fixture", "2022-12-31", "official original")
        candidates = _facts(filing, net_income=188_243_000_000)
        batch = CandidateBatch(filing, candidates)
        policy = CompilationPolicy(filings=(filing,), reporting_currency="CNY")
        compiler = FinancialFactCompiler()
        first = compiler.compile(_subject(), (batch,), policy)
        second = compiler.compile(_subject(), (batch,), policy)
        self.assertTrue(first.allow_ai)
        self.assertEqual(first, second)
        self.assertFalse(hasattr(compiler, "compile_from_ingestion"))
        self.assertFalse(hasattr(policy, "extractors"))
        self.assertFalse(hasattr(policy, "gap_extractors"))

    def test_batch_source_cannot_change_the_declared_content_hash(self):
        filing = _filing("pure-fixture", "2022-12-31", "official original")
        wrong = replace(filing, content_hash="wrong-source-hash")
        with self.assertRaisesRegex(ValueError, "CANDIDATE_SOURCE_IDENTITY_CONFLICT"):
            FinancialFactCompiler().compile(
                _subject(), (CandidateBatch(wrong, _facts(wrong, net_income=1)),),
                CompilationPolicy(filings=(filing,), reporting_currency="CNY"),
            )

    def test_undeclared_accession_cannot_evade_issuer_validation(self):
        filing = _filing("pure-fixture", "2022-12-31", "official original")
        candidates = tuple(
            FactCandidate(replace(item.fact, accession_number="not-declared"), item.evidence, item.extractor)
            for item in _facts(filing, net_income=1)
        )
        result = FinancialFactCompiler().compile(
            _subject(), (CandidateBatch(filing, candidates),),
            CompilationPolicy(filings=(filing,), reporting_currency="CNY"),
        )
        self.assertFalse(result.allow_ai)
        self.assertEqual(result.resolved_facts, ())
        self.assertEqual(len(result.quarantined_facts), 6)
        self.assertTrue(all("issuer_identity_mismatch" in group.issues for group in result.validations))


if __name__ == "__main__":
    unittest.main()
