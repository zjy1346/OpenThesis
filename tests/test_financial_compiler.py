from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from openthesis.domain import Company, EvidenceRef, FilingDocument, FinancialFact
from openthesis.financial_compiler import (
    CandidateBatch,
    CoveragePlanner,
    concepts_cover_profile,
    FactCandidate,
    GapStageKind,
    GapResolver,
    FinancialFactCompiler,
    InMemoryFactExtractor,
    StaticFilingSource,
    StructuredFactExtractor,
    CompilerPolicy,
)
from openthesis.financial_ast_adapter import PdfAstFactExtractor
from openthesis.vision_financials import VisionAdapterError, VisionFallbackConfig


def _subject() -> Company:
    return Company(
        "HK:SEHK:00700.HK", "00700.HK", "Tencent", "SEHK",
        "HK:TENCENT", "HK", "HK:SEHK:00700.HK", "CNY", "CNY", "IFRS-HKFRS",
    )


def _filing(accession: str, end: str, title: str) -> FilingDocument:
    return FilingDocument(
        f"hkex:{accession}", _subject().security_id, accession, "ANNUAL_REPORT", "FY",
        end, "2023-04-06", title, f"https://example.test/{accession}.pdf",
        content_hash=f"sha-{accession}",
    )


def _facts(filing: FilingDocument, *, net_income: float) -> tuple[FactCandidate, ...]:
    values = {
        "revenue": 554_552_000_000.0,
        "net_income": net_income,
        "operating_cash_flow": 146_091_000_000.0,
        "assets": 1_578_131_000_000.0,
        "liabilities": 795_271_000_000.0,
        "equity": 782_860_000_000.0,
    }
    statements = {
        "revenue": "income_statement", "net_income": "income_statement",
        "operating_cash_flow": "cash_flow", "assets": "balance_sheet",
        "liabilities": "balance_sheet", "equity": "balance_sheet",
    }
    result: list[FactCandidate] = []
    for concept, value in values.items():
        fact_id = f"candidate:{filing.accession_number}:{concept}"
        fact = FinancialFact(
            fact_id, filing.company_cik, concept, concept, value, "CNY", int(filing.period_end[:4]),
            "FY", filing.form_type, f"{int(filing.period_end[:4]) - 1}-01-01" if concept not in {"assets", "liabilities", "equity"} else None,
            filing.period_end, filing.filed_at, filing.accession_number, filing.source_url,
            scope="consolidated", entity="Tencent", market="HK", statement=statements[concept],
            period_start=f"{int(filing.period_end[:4]) - 1}-01-01" if concept not in {"assets", "liabilities", "equity"} else None,
            consolidated_scope="consolidated", currency="CNY", unit_scale=1_000_000,
            unit_provenance="structured_normalized",
            source_document=filing.primary_document, source_page=132,
            raw_text="Attributable to: Equity holders of the Company " + str(value),
            parser_version="fixture-official-tencent-v1",
        )
        evidence = EvidenceRef(
            f"fact:{fact_id}", filing.document_id, filing.source_url,
            "Consolidated Income Statement", "page:132",
            fact.raw_text, filing.filed_at, filing.content_hash,
        )
        result.append(FactCandidate(fact, (evidence,), "official-fixture"))
    return tuple(result)


class FinancialFactCompilerTests(unittest.TestCase):
    def _compile(self, filing: FilingDocument, candidates: tuple[FactCandidate, ...]):
        batch = CandidateBatch(filing, candidates)
        extractor = InMemoryFactExtractor({filing.document_id: batch})
        return FinancialFactCompiler().compile(
            _subject(), (filing.period_end, filing.period_end),
            CompilerPolicy(
                filings=(filing,), extractors=(extractor,), reporting_currency="CNY",
            ),
        )

    def test_complete_candidate_batch_is_resolved_once_and_allows_ai(self):
        filing = _filing("2023040601848", "2022-12-31", "Tencent 2022 Annual Report")
        dataset = self._compile(filing, _facts(filing, net_income=188_243_000_000.0))
        self.assertTrue(dataset.allow_ai)
        self.assertEqual(dataset.status, "VERIFIED")
        self.assertEqual({fact.concept for fact in dataset.resolved_facts}, {
            "revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity",
        })
        self.assertFalse(dataset.quarantined_facts)
        self.assertTrue(all(ref.locator == "page:132" for ref in dataset.evidence))

    def test_same_filing_comparator_lane_is_research_visible_but_not_annual(self):
        filing = replace(
            _filing("same-filing-comparator", "2025-12-31", "Tencent 2025 Annual Report"),
            filed_at="2026-04-06",
        )
        current = _facts(filing, net_income=188_243_000_000.0)
        comparison = tuple(
            FactCandidate(
                replace(
                    item.fact,
                    fact_id=f"{item.fact.fact_id}:comparison",
                    fiscal_year=2024,
                    end_date="2024-12-31",
                    start_date=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    period_start=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    usage_status="comparator",
                ),
                item.evidence,
                item.extractor,
            )
            for item in current
        )
        dataset = self._compile(filing, current + comparison)
        self.assertEqual({fact.end_date for fact in dataset.annual_facts}, {"2025-12-31"})
        self.assertEqual({fact.end_date for fact in dataset.comparator_facts}, {"2024-12-31"})
        self.assertTrue(any(fact.end_date == "2024-12-31" for fact in dataset.research_facts))

    def test_same_filing_comparator_currency_mismatch_is_fail_closed(self):
        filing = replace(
            _filing("same-filing-currency", "2025-12-31", "Tencent 2025 Annual Report"),
            filed_at="2026-04-06",
        )
        current = _facts(filing, net_income=188_243_000_000.0)
        foreign = tuple(
            FactCandidate(
                replace(
                    item.fact,
                    fact_id=f"{item.fact.fact_id}:foreign-comparison",
                    fiscal_year=2024,
                    end_date="2024-12-31",
                    start_date=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    period_start=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    usage_status="comparator",
                    unit="USD", currency="USD",
                ),
                item.evidence,
                item.extractor,
            )
            for item in current
        )
        dataset = self._compile(filing, current + foreign)
        self.assertFalse(dataset.allow_ai)
        self.assertIn("same_filing_comparator_identity_mismatch", dataset.diagnostics)
        self.assertFalse(any(fact.currency == "USD" for fact in dataset.research_facts))

    def test_same_filing_comparator_unit_mismatch_is_fail_closed(self):
        filing = replace(
            _filing("same-filing-unit", "2025-12-31", "Tencent 2025 Annual Report"),
            filed_at="2026-04-06",
        )
        current = _facts(filing, net_income=188_243_000_000.0)
        mismatched = tuple(
            FactCandidate(
                replace(
                    item.fact,
                    fact_id=f"{item.fact.fact_id}:unit-mismatch",
                    fiscal_year=2024,
                    end_date="2024-12-31",
                    start_date=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    period_start=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    usage_status="comparator",
                    unit_scale=1,
                ),
                item.evidence,
                item.extractor,
            )
            for item in current
        )
        dataset = self._compile(filing, current + mismatched)
        self.assertFalse(dataset.allow_ai)
        self.assertIn("same_filing_comparator_unit_mismatch", dataset.diagnostics)

    def test_same_filing_restatement_and_historical_fact_are_retained_and_conflicted(self):
        filing = replace(
            _filing("same-filing-restatement", "2025-12-31", "Tencent 2025 Annual Report"),
            filed_at="2026-04-06",
        )
        historical = replace(
            _filing("independent-history", "2024-12-31", "Tencent 2024 Annual Report"),
            filed_at="2025-04-06",
        )
        current = _facts(filing, net_income=188_243_000_000.0)
        same_filing_prior = tuple(
            FactCandidate(
                replace(
                    item.fact,
                    fact_id=f"{item.fact.fact_id}:restated",
                    fiscal_year=2024,
                    end_date="2024-12-31",
                    start_date=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    period_start=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    usage_status="comparator",
                ),
                item.evidence,
                item.extractor,
            )
            for item in current
        )
        independent = _facts(historical, net_income=177_000_000_000.0)
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2024-01-01", "2025-12-31"),
            CompilerPolicy(
                filings=(filing, historical),
                extractors=(InMemoryFactExtractor({
                    filing.document_id: CandidateBatch(filing, current + same_filing_prior),
                    historical.document_id: CandidateBatch(historical, independent),
                }),),
                reporting_currency="CNY",
            ),
        )
        self.assertTrue(dataset.allow_ai, dataset.diagnostics)
        self.assertTrue(any(item.get("reason") == "restatement_conflict" for item in dataset.conflicts))
        self.assertTrue(any(fact.usage_status == "comparator" for fact in dataset.comparator_facts))
        self.assertTrue(any(
            fact.accession_number == historical.accession_number
            and fact.concept == "net_income"
            for fact in dataset.annual_facts
        ))

    def test_same_filing_interim_comparator_satisfies_like_for_like_gate(self):
        filing = replace(
            _filing("same-filing-h1", "2025-06-30", "Tencent 2025 H1 Report"),
            fiscal_period="H1", form_type="INTERIM_REPORT", filed_at="2025-08-20",
        )
        current = tuple(
            FactCandidate(
                replace(
                    item.fact,
                    fact_id=f"{item.fact.fact_id}:h1",
                    fiscal_year=2025,
                    fiscal_period="H1", form_type="INTERIM_REPORT",
                    start_date="2025-01-01", period_start="2025-01-01",
                    end_date="2025-06-30", usage_status="audit_only",
                ), item.evidence, item.extractor,
            )
            for item in _facts(filing, net_income=188_243_000_000.0)
        )
        comparison = tuple(
            FactCandidate(
                replace(
                    item.fact,
                    fact_id=f"{item.fact.fact_id}:h1-comparison",
                    fiscal_year=2024,
                    fiscal_period="H1", form_type="INTERIM_REPORT",
                    start_date="2024-01-01", period_start="2024-01-01",
                    end_date="2024-06-30", usage_status="comparator",
                ), item.evidence, item.extractor,
            )
            for item in current
        )
        dataset = FinancialFactCompiler().compile(
            _subject(), (filing.period_end, filing.period_end),
            CompilerPolicy(
                filings=(filing,),
                extractors=(InMemoryFactExtractor({
                    filing.document_id: CandidateBatch(filing, current + comparison),
                }),),
                reporting_currency="CNY", fiscal_period="H1",
            ),
        )
        self.assertTrue(dataset.allow_ai, dataset.diagnostics)
        self.assertEqual({fact.end_date for fact in dataset.comparator_facts}, {"2024-06-30"})

    def test_same_filing_comparator_period_shape_mismatch_is_fail_closed(self):
        filing = replace(
            _filing("same-filing-period", "2025-12-31", "Tencent 2025 Annual Report"),
            filed_at="2026-04-06",
        )
        current = _facts(filing, net_income=188_243_000_000.0)
        wrong_period = tuple(
            FactCandidate(
                replace(
                    item.fact,
                    fact_id=f"{item.fact.fact_id}:wrong-period",
                    fiscal_year=2024,
                    end_date="2024-06-30",
                    start_date=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    period_start=None if item.fact.statement == "balance_sheet" else "2024-01-01",
                    usage_status="comparator",
                ),
                item.evidence,
                item.extractor,
            )
            for item in current
        )
        dataset = self._compile(filing, current + wrong_period)
        self.assertFalse(dataset.allow_ai)
        self.assertIn("same_filing_comparator_identity_mismatch", dataset.diagnostics)

    def test_incomplete_candidate_batch_cannot_bypass_quality_gate(self):
        filing = _filing("2022040701694", "2021-12-31", "Tencent 2021 Annual Report")
        candidates = tuple(item for item in _facts(filing, net_income=224_822_000_000.0) if item.fact.concept != "net_income")
        dataset = self._compile(filing, candidates)
        self.assertFalse(dataset.allow_ai)
        self.assertIn("revenue", {fact.concept for fact in dataset.resolved_facts})
        self.assertFalse(dataset.quarantined_facts)
        self.assertIn("required_profile_missing:net_income", dataset.validations[0].issues)

    def test_same_value_with_different_statement_context_is_conflicted(self):
        filing = _filing("2023040601848", "2022-12-31", "Tencent 2022 Annual Report")
        candidates = list(_facts(filing, net_income=188_243_000_000.0))
        net_index = next(index for index, item in enumerate(candidates) if item.fact.concept == "net_income")
        wrong = replace(candidates[net_index].fact, fact_id="candidate:wrong-context", statement="balance_sheet")
        candidates.append(FactCandidate(wrong, candidates[net_index].evidence, "wrong-context"))
        dataset = self._compile(filing, tuple(candidates))
        self.assertFalse(dataset.allow_ai)
        self.assertTrue(dataset.conflicts)
        self.assertIn("candidate_conflict", dataset.validations[0].issues)

    def test_same_semantic_fact_from_two_bboxes_merges_evidence(self):
        filing = _filing("bbox-merge", "2022-12-31", "Tencent 2022 Annual Report")
        candidates = list(_facts(filing, net_income=188_243_000_000.0))
        original = candidates[1]
        duplicate = replace(
            original.fact,
            fact_id="candidate:bbox-duplicate",
            source_bbox=(999.0, 1.0, 1001.0, 20.0),
        )
        candidates.append(FactCandidate(duplicate, original.evidence, "pdf-duplicate"))
        dataset = self._compile(filing, tuple(candidates))
        self.assertTrue(dataset.allow_ai)
        self.assertFalse(dataset.conflicts)

    def test_tencent_official_net_income_values_are_period_bound(self):
        first = _filing("2023040601848", "2022-12-31", "Tencent 2022 Annual Report")
        second = _filing("2022040701694", "2021-12-31", "Tencent 2021 Annual Report")
        source = StaticFilingSource((first, second))
        extractor = InMemoryFactExtractor({
            first.document_id: CandidateBatch(first, _facts(first, net_income=188_243_000_000.0)),
            second.document_id: CandidateBatch(second, _facts(second, net_income=224_822_000_000.0)),
        })
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2021-01-01", "2022-12-31"),
            CompilerPolicy(filing_source=source, extractors=(extractor,), reporting_currency="CNY"),
        )
        by_end = {fact.end_date: fact.value for fact in dataset.resolved_facts if fact.concept == "net_income"}
        self.assertEqual(by_end["2022-12-31"], 188_243_000_000.0)
        self.assertEqual(by_end["2021-12-31"], 224_822_000_000.0)

    def test_dataset_exposes_annual_and_interim_cohorts_without_latest_wiping(self):
        annual = _filing("annual-cohort", "2022-12-31", "Annual Report")
        interim = replace(
            annual,
            document_id="interim-cohort",
            accession_number="interim-cohort",
            fiscal_period="H1",
            period_end="2023-06-30",
            filed_at="2023-07-06",
        )
        prior_interim = replace(
            interim,
            document_id="prior-interim-cohort",
            accession_number="prior-interim-cohort",
            period_end="2022-06-30",
            filed_at="2022-07-06",
        )
        interim_facts = tuple(
            FactCandidate(
                replace(
                    candidate.fact,
                    fact_id=candidate.fact.fact_id.replace("annual-cohort", "interim-cohort"),
                accession_number=interim.accession_number,
                fiscal_year=2023,
                fiscal_period="H1",
                start_date="2023-01-01",
                period_start="2023-01-01",
                    end_date=interim.period_end,
                ),
                candidate.evidence,
                candidate.extractor,
            )
            for candidate in _facts(interim, net_income=188_243_000_000.0)
        )
        prior_interim_facts = tuple(
            FactCandidate(
                replace(
                    candidate.fact,
                    fact_id=candidate.fact.fact_id.replace("interim-cohort", "prior-interim-cohort"),
                    accession_number=prior_interim.accession_number,
                    fiscal_year=2022,
                    fiscal_period="H1",
                    start_date="2022-01-01",
                    period_start="2022-01-01",
                    end_date=prior_interim.period_end,
                ),
                candidate.evidence,
                candidate.extractor,
            )
            for candidate in interim_facts
        )
        extractor = InMemoryFactExtractor({
            annual.document_id: CandidateBatch(annual, _facts(annual, net_income=188_243_000_000.0)),
            interim.document_id: CandidateBatch(interim, interim_facts),
            prior_interim.document_id: CandidateBatch(prior_interim, prior_interim_facts),
        })
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2022-01-01", "2023-06-30"),
            CompilerPolicy(
                filings=(annual, interim, prior_interim), extractors=(extractor,), reporting_currency="CNY",
            ),
        )
        self.assertTrue(dataset.annual_facts)
        self.assertTrue(dataset.interim_facts)
        self.assertTrue(dataset.comparator_facts)
        self.assertTrue(any(fact.fiscal_period == "FY" for fact in dataset.research_facts))
        self.assertTrue(any(fact.fiscal_period == "H1" for fact in dataset.research_facts))
        self.assertTrue(dataset.allow_ai)

    def test_interim_cohort_is_global_latest_with_same_period_comparator(self):
        annual = _filing("cohort-annual", "2022-12-31", "Annual Report")
        h1 = replace(
            annual, document_id="cohort-h1", accession_number="cohort-h1",
            fiscal_period="H1", period_end="2023-06-30", filed_at="2023-07-06",
        )
        q1 = replace(
            annual, document_id="cohort-q1", accession_number="cohort-q1",
            fiscal_period="Q1", period_end="2023-03-31", filed_at="2023-04-06",
        )
        prior_h1 = replace(
            h1, document_id="cohort-prior-h1", accession_number="cohort-prior-h1",
            period_end="2022-06-30", filed_at="2022-07-06",
        )
        batches = {}
        for filing in (annual, h1, q1, prior_h1):
            period_facts = tuple(
                FactCandidate(
                    replace(
                        candidate.fact,
                        fiscal_year=int(filing.period_end[:4]),
                        fiscal_period=filing.fiscal_period,
                        form_type=filing.form_type,
                        end_date=filing.period_end,
                    ),
                    candidate.evidence,
                    candidate.extractor,
                )
                for candidate in _facts(filing, net_income=188_243_000_000.0)
            )
            batches[filing.document_id] = CandidateBatch(filing, period_facts)
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2022-01-01", "2023-12-31"),
            CompilerPolicy(
                filings=(annual, h1, q1, prior_h1), extractors=(InMemoryFactExtractor(batches),),
                reporting_currency="CNY",
            ),
        )
        self.assertEqual({fact.fiscal_period for fact in dataset.interim_facts}, {"H1"})
        self.assertEqual({fact.end_date for fact in dataset.interim_facts}, {"2023-06-30"})
        self.assertEqual({fact.end_date for fact in dataset.comparator_facts}, {"2022-06-30"})
        self.assertNotIn("2023-03-31", {fact.end_date for fact in dataset.research_facts})

    def test_latest_incomplete_interim_cannot_be_masked_by_older_verified(self):
        annual = _filing("latest-annual", "2022-12-31", "Annual Report")
        latest = replace(
            annual, document_id="latest-h1", accession_number="latest-h1",
            fiscal_period="H1", period_end="2023-06-30", filed_at="2023-07-06",
        )
        older = replace(
            latest, document_id="older-h1", accession_number="older-h1",
            period_end="2022-06-30", filed_at="2022-07-06",
        )
        def batch(filing, omit=()):
            return CandidateBatch(
                filing,
                tuple(
                    FactCandidate(
                        replace(
                            candidate.fact,
                            fiscal_year=int(filing.period_end[:4]),
                            fiscal_period="H1",
                            form_type="INTERIM_REPORT",
                            end_date=filing.period_end,
                        ),
                        candidate.evidence,
                        candidate.extractor,
                    )
                    for candidate in _facts(filing, net_income=188_243_000_000.0)
                    if candidate.fact.concept not in omit
                ),
            )
        batches = {
            annual.document_id: CandidateBatch(annual, _facts(annual, net_income=188_243_000_000.0)),
            latest.document_id: batch(latest, {"net_income"}),
            older.document_id: batch(older),
        }
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2022-01-01", "2023-12-31"),
            CompilerPolicy(
                filings=(annual, latest, older),
                extractors=(InMemoryFactExtractor(batches),),
                reporting_currency="CNY",
            ),
        )
        self.assertFalse(dataset.allow_ai)
        self.assertEqual(dataset.interim_facts, ())
        self.assertIn("interim_comparator_missing", dataset.diagnostics)
        self.assertIn("latest_interim_incomplete", dataset.diagnostics)

    def test_annual_cohorts_are_deduplicated_and_limited_to_six(self):
        filings = tuple(
            replace(
                _filing(f"annual-{year}", f"{year}-12-31", "Annual Report"),
                filed_at=f"{year + 1}-03-30",
            )
            for year in range(2015, 2022)
        )
        batches = {
            filing.document_id: CandidateBatch(filing, _facts(filing, net_income=188_243_000_000.0))
            for filing in filings
        }
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2015-01-01", "2021-12-31"),
            CompilerPolicy(
                filings=filings, extractors=(InMemoryFactExtractor(batches),),
                reporting_currency="CNY",
            ),
        )
        self.assertEqual({fact.end_date for fact in dataset.annual_facts}, {
            f"{year}-12-31" for year in range(2016, 2022)
        })
        self.assertEqual(len({fact.end_date for fact in dataset.annual_facts}), 6)
        self.assertTrue(all(
            fact.validation_status == next(
                item.status for item in dataset.validations if item.identity[0] == fact.accession_number
            )
            for fact in dataset.annual_facts
        ))

    def test_minimal_coordinate_pdf_fixture_uses_formal_ast_and_excludes_eps(self):
        """CI fixture: PDF words -> formal AST -> candidates -> compiler gate."""
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

        class FixturePath:
            def __enter__(self):
                self.path = Path.cwd() / "build" / f"financial-compiler-{os.getpid()}.pdf"
                return str(self.path.parent)

            def __exit__(self, exc_type, exc, traceback):
                self.path.unlink(missing_ok=True)

        with FixturePath() as directory:
            path = f"{directory}/tencent-2022-fixture.pdf"
            writer = PdfWriter()

            def page(title, rows):
                pdf_page = writer.add_blank_page(width=612, height=792)
                font = DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                })
                resources = DictionaryObject({
                    NameObject("/Font"): DictionaryObject({
                        NameObject("/F1"): writer._add_object(font),
                    }),
                })
                pdf_page[NameObject("/Resources")] = resources
                commands = []

                def text(value, x, y, size=9):
                    escaped = str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                    commands.append(f"BT /F1 {size} Tf {x} {y} Td ({escaped}) Tj ET")

                text(title, 36, 780, 12)
                text("Year ended 31 December", 36, 760)
                text("2022", 220, 760)
                text("2021", 320, 760)
                text("Note RMB'Million RMB'Million", 36, 744)
                y = 720
                for label, current, previous in rows:
                    text(label, 36, y)
                    if current is not None:
                        text(current, 220, y)
                    if previous is not None:
                        text(previous, 320, y)
                    y -= 16
                stream = DecodedStreamObject()
                stream.set_data("\n".join(commands).encode("ascii"))
                pdf_page[NameObject("/Contents")] = writer._add_object(stream)

            page("Consolidated Income Statement", [
                ("Revenue", 554552, 560118),
                ("Operating profit", 235706, 271620),
                ("Profit for the year", 188709, 227810),
                ("Attributable to:", None, None),
                ("Equity holders of the Company", 188243, 224822),
                ("Earnings per share basic and diluted", None, None),
            ])
            page("Consolidated Statement of Financial Position", [
                ("Total assets", 1578131, 1612364),
                ("Equity attributable to owners", 721391, 876693),
                ("Total equity", 782860, 876693),
            ])
            page("Consolidated Statement of Financial Position", [
                ("Total liabilities", 795271, 735671),
            ])
            page("Consolidated Statements of Cash Flows", [
                ("Net cash flow from operating activities", 146091, 175186),
            ])
            with open(path, "wb") as handle:
                writer.write(handle)

            subject = _subject()
            filing = _filing("fixture-2023040601848", "2022-12-31", "Tencent 2022 Fixture")
            filing.local_path = path
            filing.content_hash = "fixture-official-tencent-2022"
            batch = PdfAstFactExtractor().extract(subject, filing)
            concepts = {item.fact.concept for item in batch.candidates}
            self.assertTrue({"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"} <= concepts)
            net_income = [item.fact for item in batch.candidates if item.fact.concept == "net_income"]
            self.assertEqual({item.fiscal_year for item in net_income}, {2022, 2021})
            current_net_income = next(item for item in net_income if item.fiscal_year == 2022)
            self.assertEqual(current_net_income.value, 188243000000.0)
            self.assertEqual(current_net_income.source_page, 1)
            self.assertTrue(current_net_income.source_bbox)
            self.assertEqual(current_net_income.currency, "CNY")
            self.assertEqual(current_net_income.unit_scale, 1_000_000.0)
            self.assertEqual(current_net_income.consolidated_scope, "consolidated")
            compiler = FinancialFactCompiler().compile(
                subject,
                (filing.period_end, filing.period_end),
                CompilerPolicy(
                    filings=(filing,),
                    reporting_currency="CNY",
                ),
            )
            self.assertTrue(compiler.allow_ai)
            self.assertEqual(compiler.status, "VERIFIED")

    def test_coverage_planner_declares_non_financial_and_financial_profiles(self):
        planner = CoveragePlanner()
        industrial = planner.plan(_subject())
        self.assertEqual(industrial.profile_id, "non_financial")
        self.assertEqual(industrial.required_concepts, {
            "revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity",
        })
        for industry, expected in (
            ("banking", "bank"),
            ("insurance", "insurance"),
            ("securities brokerage", "securities"),
        ):
            with self.subTest(industry=industry):
                subject = Company("US:1089113", "TEST", "Fixture", industry=industry)
                profile = planner.plan(subject)
                self.assertEqual(profile.profile_id, expected)
                self.assertEqual(profile.required_concepts, {
                    "net_income", "assets", "liabilities", "equity",
                })
                self.assertNotEqual(profile.required_concepts, industrial.required_concepts)

    def test_financial_profile_can_pass_without_industrial_cashflow_revenue(self):
        filing = _filing("bank-profile", "2022-12-31", "Bank Fixture")
        bank = Company("US:1089113", "BANK", "Bank Fixture", industry="banking", reporting_currency="CNY")
        candidates = [item for item in _facts(filing, net_income=188_243_000_000.0)
                      if item.fact.concept in {"net_income", "assets", "liabilities", "equity"}]
        extractor = InMemoryFactExtractor({filing.document_id: CandidateBatch(filing, tuple(candidates))})
        dataset = FinancialFactCompiler().compile(
            bank, (filing.period_end, filing.period_end),
            CompilerPolicy(filings=(filing,), extractors=(extractor,), reporting_currency="CNY"),
        )
        self.assertTrue(dataset.allow_ai)
        self.assertEqual(dataset.coverage["profile_id"], "bank")

    def test_profile_equity_alias_is_shared_by_compiler_gate(self):
        required = CoveragePlanner().plan(
            Company("US:bank", "BANK", "Bank", industry="banking")
        ).required_concepts
        self.assertTrue(concepts_cover_profile(
            {"net_income", "assets", "liabilities", "total_equity"}, required
        ))
        self.assertFalse(concepts_cover_profile(
            {"net_income", "assets", "liabilities"}, required
        ))

    def test_profile_equity_alias_reaches_compiler_allow_ai(self):
        filing = _filing("bank-total-equity", "2022-12-31", "Bank Fixture")
        bank = Company("US:1089113", "BANK", "Bank Fixture", industry="banking", reporting_currency="CNY")
        candidates = []
        for item in _facts(filing, net_income=188_243_000_000.0):
            if item.fact.concept == "revenue" or item.fact.concept == "operating_cash_flow":
                continue
            if item.fact.concept == "equity":
                fact = replace(item.fact, fact_id="candidate:bank-total-equity:total_equity", concept="total_equity")
                item = FactCandidate(fact, item.evidence, item.extractor)
            candidates.append(item)
        extractor = InMemoryFactExtractor({filing.document_id: CandidateBatch(filing, tuple(candidates))})
        dataset = FinancialFactCompiler().compile(
            bank, (filing.period_end, filing.period_end),
            CompilerPolicy(filings=(filing,), extractors=(extractor,), reporting_currency="CNY"),
        )
        self.assertTrue(dataset.allow_ai)

    def test_cross_year_typical_scale_jump_blocks_research(self):
        first = _filing("scale-2022", "2022-12-31", "Scale 2022")
        second = _filing("scale-2021", "2021-12-31", "Scale 2021")
        first_items = list(_facts(first, net_income=72_000_000.0))
        second_items = list(_facts(second, net_income=72_000_000.0))
        first_items = [replace(item, fact=replace(item.fact, unit_scale=1.0)) for item in first_items]
        second_items = [replace(item, fact=replace(item.fact, unit_scale=1000.0)) for item in second_items]
        first_items[0] = replace(first_items[0], fact=replace(first_items[0].fact, value=265_000_000_000.0))
        second_items[0] = replace(second_items[0], fact=replace(second_items[0].fact, value=265_000_000.0))
        extractor = InMemoryFactExtractor({
            first.document_id: CandidateBatch(first, tuple(first_items)),
            second.document_id: CandidateBatch(second, tuple(second_items)),
        })
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2021-01-01", "2022-12-31"),
            CompilerPolicy(filings=(first, second), extractors=(extractor,), reporting_currency="CNY"),
        )
        self.assertFalse(dataset.allow_ai)
        self.assertIn("unit_scale_continuity_failed", dataset.diagnostics)

    def test_same_scale_large_business_change_does_not_block_research(self):
        first = _filing("legal-2022", "2022-12-31", "Legal 2022")
        second = _filing("legal-2021", "2021-12-31", "Legal 2021")
        first_items = list(_facts(first, net_income=72_000_000.0))
        second_items = list(_facts(second, net_income=72_000_000.0))
        first_items = [replace(item, fact=replace(item.fact, unit_scale=1.0)) for item in first_items]
        second_items = [replace(item, fact=replace(item.fact, unit_scale=1.0)) for item in second_items]
        first_items[0] = replace(first_items[0], fact=replace(first_items[0].fact, value=265_000_000_000.0))
        second_items[0] = replace(second_items[0], fact=replace(second_items[0].fact, value=265_000_000.0))
        extractor = InMemoryFactExtractor({
            first.document_id: CandidateBatch(first, tuple(first_items)),
            second.document_id: CandidateBatch(second, tuple(second_items)),
        })
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2021-01-01", "2022-12-31"),
            CompilerPolicy(filings=(first, second), extractors=(extractor,), reporting_currency="CNY"),
        )
        self.assertNotIn("unit_scale_continuity_failed", dataset.diagnostics)

    def test_research_view_selects_target_scope_currency_and_complete_groups(self):
        filing = _filing("target-groups", "2022-12-31", "Target Groups")
        base = list(_facts(filing, net_income=188_243_000_000.0))
        foreign = [replace(
            item.fact,
            fact_id=f"foreign:{item.fact.concept}",
            currency="USD", unit="USD",
        ) for item in base]
        parent = [replace(
            item.fact,
            fact_id=f"parent:{item.fact.concept}",
            scope="parent", consolidated_scope="parent",
        ) for item in base]
        candidates = tuple(
            FactCandidate(item.fact, item.evidence, item.extractor) for item in base
        ) + tuple(
            FactCandidate(fact, base[index].evidence, "foreign")
            for index, fact in enumerate(foreign)
        ) + tuple(
            FactCandidate(fact, base[index].evidence, "parent")
            for index, fact in enumerate(parent)
        )
        dataset = self._compile(filing, candidates)
        self.assertTrue(dataset.allow_ai)
        self.assertEqual(
            {fact.fact_id for fact in dataset.research_facts},
            {item.fact.fact_id for item in candidates[:6]},
        )
        self.assertGreater(len(dataset.resolved_facts), len(dataset.research_facts))
        self.assertTrue(all(
            fact.currency == "CNY" and fact.consolidated_scope == "consolidated"
            for fact in dataset.research_facts
        ))

    def test_incomplete_target_group_blocks_ai_and_excludes_its_siblings(self):
        first = _filing("target-complete", "2022-12-31", "Target 2022")
        second = _filing("target-incomplete", "2021-12-31", "Target 2021")
        source = StaticFilingSource((first, second))
        first_batch = CandidateBatch(first, _facts(first, net_income=188_243_000_000.0))
        second_batch = CandidateBatch(
            second,
            tuple(item for item in _facts(second, net_income=224_822_000_000.0)
                  if item.fact.concept != "net_income"),
        )
        extractor = InMemoryFactExtractor({first.document_id: first_batch, second.document_id: second_batch})
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2021-01-01", "2022-12-31"),
            CompilerPolicy(
                filings=(first, second), extractors=(extractor,),
                reporting_currency="CNY", period_range=("2021-01-01", "2022-12-31"),
            ),
        )
        self.assertFalse(dataset.allow_ai)
        self.assertEqual({fact.end_date for fact in dataset.research_facts}, {"2022-12-31"})
        self.assertNotIn("2021-12-31", {fact.end_date for fact in dataset.research_facts})
        self.assertEqual({item.identity[1] for item in dataset.research_validations}, {"2022-12-31"})

    def test_period_range_limits_research_target_window(self):
        first = _filing("range-2021", "2021-12-31", "Range 2021")
        second = _filing("range-2022", "2022-12-31", "Range 2022")
        extractor = InMemoryFactExtractor({
            first.document_id: CandidateBatch(first, _facts(first, net_income=224_822_000_000.0)),
            second.document_id: CandidateBatch(second, _facts(second, net_income=188_243_000_000.0)),
        })
        dataset = FinancialFactCompiler().compile(
            _subject(), ("2021-01-01", "2022-12-31"),
            CompilerPolicy(
                filings=(first, second), extractors=(extractor,), reporting_currency="CNY",
                period_range=("2022-01-01", "2022-12-31"),
            ),
        )
        self.assertTrue(dataset.allow_ai)
        self.assertEqual({item.identity[1] for item in dataset.research_validations}, {"2022-12-31"})
        self.assertEqual({fact.end_date for fact in dataset.research_facts}, {"2022-12-31"})

    def test_gap_resolver_uses_ordered_stages_and_stops_after_coverage(self):
        filing = _filing("gap-1", "2022-12-31", "Fixture")
        calls: list[str] = []

        class Stage:
            def __init__(self, name, kind, batch):
                self.name, self.stage_kind, self.batch = name, kind, batch

            def extract(self, subject, current):
                calls.append(self.name)
                return self.batch

        revenue = _facts(filing, net_income=188_243_000_000.0)[0]
        batch = CandidateBatch(filing, (revenue,))
        result = GapResolver().resolve(
            _subject(), filing, ("revenue",),
            (Stage("structured", GapStageKind.OFFICIAL_STRUCTURED, batch), Stage("pdf", GapStageKind.PDF_AST, CandidateBatch(filing))),
        )
        self.assertEqual(calls, ["structured"])
        self.assertEqual([item.fact.concept for item in result.candidates], ["revenue"])

    def test_gap_resolver_records_failure_then_falls_back_and_honors_cancel(self):
        filing = _filing("gap-2", "2022-12-31", "Fixture")
        calls: list[str] = []

        class Failing:
            name = "structured"
            stage_kind = GapStageKind.OFFICIAL_STRUCTURED

            def extract(self, subject, current):
                calls.append(self.name)
                raise OSError("offline")

        class Empty:
            name = "pdf"
            stage_kind = GapStageKind.PDF_AST

            def extract(self, subject, current):
                calls.append(self.name)
                return CandidateBatch(current)

        result = GapResolver().resolve(_subject(), filing, ("revenue",), (Failing(), Empty()))
        self.assertEqual(calls, ["structured", "pdf"])
        self.assertIn("official_structured:failed:OSError", result.diagnostics)
        self.assertIn("gap_unresolved", result.diagnostics)
        calls.clear()
        cancelled = GapResolver().resolve(
            _subject(), filing, ("revenue",), (Failing(),), cancel_check=lambda: True,
        )
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(calls, [])

    def test_compiler_routes_missing_concepts_through_ordered_gap_stage(self):
        filing = _filing("gap-compile", "2022-12-31", "Fixture")
        all_candidates = list(_facts(filing, net_income=188_243_000_000.0))

        class Stage:
            def __init__(self, kind, candidates):
                self.stage_kind, self.candidates = kind, tuple(candidates)
                self.calls = 0

            def extract(self, subject, current):
                self.calls += 1
                return CandidateBatch(current, self.candidates)

        primary = Stage(GapStageKind.OFFICIAL_STRUCTURED, all_candidates[:1])
        fallback = Stage(GapStageKind.PDF_AST, all_candidates[1:])
        dataset = FinancialFactCompiler().compile(
            _subject(), (filing.period_end, filing.period_end),
            CompilerPolicy(filings=(filing,), extractors=(primary, fallback), reporting_currency="CNY"),
        )
        self.assertTrue(dataset.allow_ai)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_gap_stage_preserves_total_equity_needed_for_balance_validation(self):
        filing = _filing("gap-total-equity", "2022-12-31", "Fixture")
        candidates = list(_facts(filing, net_income=188_243_000_000.0))
        equity_index = next(
            index for index, item in enumerate(candidates)
            if item.fact.concept == "equity"
        )
        parent_equity = replace(
            candidates[equity_index].fact,
            value=600_000_000_000.0,
            raw_text="Equity attributable to owners 600000",
        )
        candidates[equity_index] = FactCandidate(
            parent_equity, candidates[equity_index].evidence, "pdf-ast"
        )
        total_equity = replace(
            parent_equity,
            fact_id="candidate:gap-total-equity:total_equity",
            concept="total_equity",
            reported_concept="total shareholders' equity",
            value=782_860_000_000.0,
            raw_text="Total shareholders' equity 782860",
        )
        candidates.append(FactCandidate(total_equity, (), "pdf-ast"))

        class Stage:
            def __init__(self, kind, batch):
                self.stage_kind = kind
                self.batch = batch

            def extract(self, subject, current):
                return self.batch

        empty = Stage(GapStageKind.OFFICIAL_STRUCTURED, CandidateBatch(filing))
        pdf = Stage(GapStageKind.PDF_AST, CandidateBatch(filing, tuple(candidates)))
        dataset = FinancialFactCompiler().compile(
            _subject(), (filing.period_end, filing.period_end),
            CompilerPolicy(
                filings=(filing,), extractors=(empty, pdf), reporting_currency="CNY"
            ),
        )

        self.assertTrue(dataset.allow_ai)
        self.assertEqual(dataset.status, "VERIFIED")
        self.assertIn("total_equity", {fact.concept for fact in dataset.resolved_facts})

    def test_gap_resolver_requires_explicit_order_and_mineru_authorization(self):
        filing = _filing("gap-policy", "2022-12-31", "Fixture")

        class Stage:
            def __init__(self, kind):
                self.stage_kind, self.calls = kind, 0

            def extract(self, subject, current):
                self.calls += 1
                return CandidateBatch(current)

        mineru = Stage(GapStageKind.MINERU)
        result = GapResolver().resolve(_subject(), filing, ("revenue",), (mineru,))
        self.assertEqual(mineru.calls, 0)
        self.assertIn("mineru_not_authorized", result.diagnostics)
        bad_order = GapResolver().resolve(
            _subject(), filing, ("revenue",),
            (Stage(GapStageKind.PDF_AST), Stage(GapStageKind.OFFICIAL_STRUCTURED)),
        )
        self.assertIn("invalid_gap_stage_order", bad_order.diagnostics)

    def test_structured_adapter_keeps_explicit_fact_evidence_association(self):
        filing = _filing("structured-1", "2022-12-31", "Fixture")
        fact = _facts(filing, net_income=188_243_000_000.0)[0].fact
        evidence = _facts(filing, net_income=188_243_000_000.0)[0].evidence[0]

        class Source:
            def fetch(self, subject, current):
                return [fact], [evidence], None

        batch = StructuredFactExtractor(Source()).extract(_subject(), filing)
        self.assertEqual(batch.candidates[0].fact.fact_id, fact.fact_id)
        self.assertIs(batch.candidates[0].evidence[0], evidence)

    def test_compile_from_ingestion_merges_structured_and_pdf_candidates(self):
        filing = _filing("cross-source-1", "2022-12-31", "Fixture")
        candidates = _facts(filing, net_income=188_243_000_000.0)

        class Collector:
            def collect_candidate_batches(self, subject, filings, **kwargs):
                structured = CandidateBatch(filing, candidates[:2])
                pdf = CandidateBatch(filing, candidates[2:])
                return SimpleNamespace(
                    manifests=(),
                    batches_by_document={filing.document_id: (structured, pdf)},
                    evidence=(),
                    diagnostics=(),
                )

        dataset = FinancialFactCompiler().compile_from_ingestion(
            _subject(), [filing], Collector(), reporting_currency="CNY"
        )
        self.assertTrue(dataset.allow_ai)
        self.assertEqual(
            {fact.concept for fact in dataset.research_facts},
            {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"},
        )
        self.assertEqual(
            {candidate.extractor for candidate in candidates},
            {"official-fixture", "official-fixture"},
        )

    def test_compile_from_ingestion_targets_the_filings_declared_period(self):
        filing = replace(
            _filing("q1-cross-source", "2026-03-31", "First Quarterly Report"),
            form_type="QUARTERLY_REPORT",
            fiscal_period="Q1",
            filed_at="2026-04-06",
        )
        candidates = tuple(
            replace(
                candidate,
                fact=replace(
                    candidate.fact,
                    fiscal_year=2026,
                    fiscal_period="Q1",
                    form_type="QUARTERLY_REPORT",
                    start_date="2026-01-01"
                    if candidate.fact.statement != "balance_sheet" else None,
                    period_start="2026-01-01"
                    if candidate.fact.statement != "balance_sheet" else None,
                    end_date="2026-03-31",
                ),
            )
            for candidate in _facts(filing, net_income=4_000_000_000.0)
        )

        class Collector:
            def collect_candidate_batches(self, subject, filings, **kwargs):
                return SimpleNamespace(
                    manifests=(),
                    batches_by_document={
                        filing.document_id: (
                            CandidateBatch(filing),
                            CandidateBatch(filing, candidates),
                        )
                    },
                    evidence=(),
                    diagnostics=(),
                )

        dataset = FinancialFactCompiler().compile_from_ingestion(
            _subject(), [filing], Collector(), reporting_currency="CNY"
        )

        self.assertFalse(dataset.allow_ai)
        self.assertIn("interim_comparator_missing", dataset.diagnostics)
        self.assertEqual(dataset.coverage["target_fiscal_period"], "Q1")
        self.assertEqual({fact.fiscal_period for fact in dataset.research_facts}, {"Q1"})

    def test_compile_from_ingestion_validates_vision_before_optional_stage(self):
        filing = _filing("vision-auth-1", "2022-12-31", "Fixture")
        candidates = _facts(filing, net_income=188_243_000_000.0)

        class Collector:
            def collect_candidate_batches(self, subject, filings, **kwargs):
                return SimpleNamespace(
                    manifests=(),
                    batches_by_document={
                        filing.document_id: (
                            CandidateBatch(filing, candidates[:1]),
                            CandidateBatch(filing, ()),
                        )
                    },
                    evidence=(),
                    diagnostics=(),
                )

        class Adapter:
            def extract(self, *args, **kwargs):
                raise AssertionError("vision adapter must not run before config validation")

        with self.assertRaises(VisionAdapterError) as raised:
            FinancialFactCompiler().compile_from_ingestion(
                _subject(),
                [filing],
                Collector(),
                vision_fallback=Adapter(),
                vision_config=VisionFallbackConfig(
                    enabled=True, consent=True, configured_model_id="test-model"
                ),
                reporting_currency="CNY",
            )
        self.assertEqual(raised.exception.code, "VISION_UPLOAD_APPROVAL_REQUIRED")

    def test_compile_from_ingestion_exposes_explicit_same_year_gap_stage(self):
        filing = _filing("same-year-1", "2022-12-31", "Fixture")
        candidates = _facts(filing, net_income=188_243_000_000.0)
        calls: list[str] = []

        class Collector:
            def collect_candidate_batches(self, subject, filings, **kwargs):
                return SimpleNamespace(
                    manifests=(),
                    batches_by_document={
                        filing.document_id: (
                            CandidateBatch(filing, candidates[:1]),
                            CandidateBatch(filing, ()),
                        )
                    },
                    evidence=(),
                    diagnostics=(),
                )

        class SameYearSource:
            def fetch(self, subject, current):
                calls.append("same-year")
                return (
                    [candidate.fact for candidate in candidates[1:]],
                    [candidate.evidence[0] for candidate in candidates[1:]],
                    None,
                )

        dataset = FinancialFactCompiler().compile_from_ingestion(
            _subject(),
            [filing],
            Collector(),
            same_year_sources=(SameYearSource(),),
            reporting_currency="CNY",
        )
        self.assertTrue(dataset.allow_ai)
        self.assertEqual(calls, ["same-year"])


if __name__ == "__main__":
    unittest.main()
