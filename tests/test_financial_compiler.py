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
        self.assertTrue(dataset.allow_ai)

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
            self.assertEqual(len(net_income), 1)
            self.assertEqual(net_income[0].value, 188243000000.0)
            self.assertEqual(net_income[0].source_page, 1)
            self.assertTrue(net_income[0].source_bbox)
            self.assertEqual(net_income[0].currency, "CNY")
            self.assertEqual(net_income[0].unit_scale, 1_000_000.0)
            self.assertEqual(net_income[0].consolidated_scope, "consolidated")
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
        self.assertEqual({item.identity[1] for item in dataset.research_validations}, {"2021-12-31", "2022-12-31"})

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

        self.assertTrue(dataset.allow_ai)
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
