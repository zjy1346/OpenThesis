from __future__ import annotations

import tempfile
import unittest
import sqlite3
import json
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, ResearchArtifact, ResearchRun, RunStatus, utc_now_iso, FilingDocument, EvidenceRef
from openthesis.financial_ingestion import FinancialGroupValidation
from openthesis.market_financials import FinancialValidation, ValidationStatus
from openthesis.storage import Storage
from openthesis.markets import build_company


class StorageTests(unittest.TestCase):
    def _fact(self, company_cik: str, status: str = "VERIFIED") -> FinancialFact:
        return FinancialFact(
            fact_id="rich-fact",
            company_cik=company_cik,
            concept="revenue",
            reported_concept="Revenue",
            value=123.0,
            unit="CNY",
            fiscal_year=2025,
            fiscal_period="FY",
            form_type="ANNUAL_REPORT",
            start_date="2025-01-01",
            end_date="2025-12-31",
            filed_at="2026-03-01",
            accession_number="acc-rich",
            source_url="https://example.test/report.pdf",
            entity="Example",
            market="CN_A",
            statement="income_statement",
            period_start="2025-01-01",
            consolidated_scope="consolidated",
            currency="CNY",
            unit_scale=1000.0,
            revision="original",
            source_document="report.pdf",
            source_page=4,
            source_bbox=(1.0, 2.0, 30.0, 40.0),
            raw_text="Revenue 123",
            parser_version="test",
            validation_status=status,
        )

    def test_rich_fact_bbox_and_validation_group_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            company = build_company("rich-co", "RICH", reporting_currency="CNY")
            storage.save_company(company)
            fact = self._fact(company.security_id)
            evidence = EvidenceRef("fact:rich-fact", "doc-rich", fact.source_url, "Revenue", "page:4", fact.raw_text, fact.filed_at, bbox=fact.source_bbox)
            validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (fact,), ())
            group = FinancialGroupValidation(("acc-rich", "2025-12-31", "FY", "consolidated", "CNY"), validation)
            storage.replace_financial_ingestion(company.security_id, ["acc-rich"], [fact], [], [group], [evidence])
            saved = storage.get_facts(company.security_id)
            self.assertEqual(saved[0]["source_bbox"], fact.source_bbox)
            self.assertEqual(saved[0]["statement"], "income_statement")
            groups = storage.get_validation_groups(company.security_id)
            self.assertEqual(groups[0]["status"], "VERIFIED")
            self.assertEqual(groups[0]["covered_concepts"], ["revenue"])

    def test_reparse_removes_stale_evidence_for_accession(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            company = build_company("600519.SH", "Moutai", reporting_currency="CNY")
            storage.save_company(company)
            filing = FilingDocument(
                "doc-reparse", company.security_id, "acc-reparse", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "annual.pdf", "https://example.test/report",
            )
            storage.save_filings([filing])
            fact = self._fact(company.security_id)
            evidence = EvidenceRef(
                "fact:stale", filing.document_id, filing.source_url, "Report", "page:1",
                "old excerpt", filing.filed_at,
            )
            storage.replace_financial_ingestion(
                company.security_id, [filing.accession_number], [fact], [], [], [evidence]
            )
            self.assertEqual(len(storage.get_financial_evidence(filing.document_id)), 1)
            storage.replace_financial_ingestion(
                company.security_id, [filing.accession_number], [], [], [], []
            )
            self.assertEqual(storage.get_financial_evidence(filing.document_id), [])

    def test_validation_group_id_is_scoped_by_company(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            first = build_company("600036.SH", "Moutai", reporting_currency="CNY")
            second = build_company("600519.SH", "Moutai", reporting_currency="CNY")
            storage.save_company(first)
            storage.save_company(second)
            for company in (first, second):
                fact = self._fact(company.security_id)
                validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (fact,), ())
                group = FinancialGroupValidation(("same", "2025-12-31", "FY", "consolidated", "CNY"), validation)
                storage.replace_financial_ingestion(company.security_id, ["same"], [fact], [], [group], [])
            groups = storage.get_validation_groups(first.security_id) + storage.get_validation_groups(second.security_id)
            self.assertEqual(len(groups), 2)

    def test_rejected_facts_hidden_from_normal_read_but_available_in_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            company = build_company("reject-co", "REJECT")
            storage.save_company(company)
            fact = self._fact(company.security_id, "REJECTED")
            storage.replace_financial_ingestion(company.security_id, [fact.accession_number], [], [fact])
            self.assertEqual(storage.get_facts(company.security_id), [])
            self.assertEqual(len(storage.get_facts_audit(company.security_id)), 1)

    def test_existing_schema_is_migrated_additively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            data_dir.mkdir(exist_ok=True)
            db_path = data_dir / "openthesis.db"
            db = sqlite3.connect(db_path)
            try:
                db.executescript("""
                    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE companies(cik TEXT PRIMARY KEY, ticker TEXT NOT NULL, name TEXT NOT NULL, exchange_name TEXT NOT NULL DEFAULT '');
                    CREATE TABLE filings(document_id TEXT PRIMARY KEY, company_cik TEXT NOT NULL, accession_number TEXT NOT NULL, form_type TEXT NOT NULL, fiscal_period TEXT NOT NULL, period_end TEXT NOT NULL, filed_at TEXT NOT NULL, primary_document TEXT NOT NULL, source_url TEXT NOT NULL, local_path TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', ingested_at TEXT NOT NULL);
                    CREATE TABLE financial_facts(fact_id TEXT PRIMARY KEY, company_cik TEXT NOT NULL, concept TEXT NOT NULL, reported_concept TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, fiscal_year INTEGER NOT NULL, fiscal_period TEXT NOT NULL, form_type TEXT NOT NULL, start_date TEXT, end_date TEXT NOT NULL, filed_at TEXT NOT NULL, accession_number TEXT NOT NULL, source_url TEXT NOT NULL, scope TEXT NOT NULL);
                    INSERT INTO metadata VALUES('schema_version','3');
                    INSERT INTO companies VALUES('legacy','LEG','Legacy','');
                    INSERT INTO financial_facts VALUES('legacy-fact','legacy','revenue','Revenue',1,'USD',2024,'FY','10-K','2024-01-01','2024-12-31','2025-01-01','legacy-acc','https://example.test','consolidated');
                """)
            finally:
                db.close()
            storage = Storage(data_dir)
            self.assertEqual(storage.get_facts("legacy")[0]["fact_id"], "legacy-fact")
            with storage.connect() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(financial_facts)")}
            self.assertIn("source_bbox_json", columns)
    def test_delete_run_removes_artifacts_and_generated_thesis_but_keeps_user_thesis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            run = ResearchRun(
                run_id="deletable-run",
                company=DEMO_COMPANY,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="none",
                model_id="",
                data_as_of="2026-01-01T00:00:00+00:00",
                status=RunStatus.COMPLETED,
                completed_at=utc_now_iso(),
            )
            storage.save_run(run)
            storage.save_artifact(
                ResearchArtifact(
                    artifact_id="artifact-delete",
                    run_id=run.run_id,
                    artifact_type="research-report",
                    title="Report",
                    content={"report": "delete"},
                )
            )
            storage.save_thesis_version(
                DEMO_COMPANY.cik,
                {"thesis": "generated"},
                run_id=run.run_id,
                created_by="research-synthesizer",
                created_at=utc_now_iso(),
            )
            user_thesis = storage.save_thesis_version(
                DEMO_COMPANY.cik,
                {"thesis": "keep"},
                run_id=run.run_id,
                created_by="user",
                created_at=utc_now_iso(),
            )

            self.assertTrue(storage.delete_run(run.run_id))
            self.assertIsNone(storage.get_run(run.run_id))
            self.assertEqual(storage.get_artifacts(run.run_id), [])
            remaining = storage.list_thesis_versions(DEMO_COMPANY.cik)
            self.assertEqual([item["thesis_version_id"] for item in remaining], [user_thesis["thesis_version_id"]])
            self.assertIsNone(remaining[0]["run_id"])

    def test_market_listing_and_issuer_are_stored_without_breaking_legacy_company_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            a_share = build_company(
                "300750.SZ",
                "宁德时代",
                issuer_id="CN:CATL",
                industry="电力设备",
            )
            h_share = build_company(
                "03750.HK",
                "宁德时代",
                issuer_id="CN:CATL",
                industry="电力设备",
                accounting_standard="CAS",
                reporting_currency="CNY",
            )

            storage.save_company(a_share)
            storage.save_company(h_share)

            saved_a = storage.get_security_listing(a_share.security_id)
            saved_h = storage.get_security_listing(h_share.security_id)
            self.assertEqual(saved_a["issuer_id"], "CN:CATL")
            self.assertEqual(saved_h["issuer_id"], "CN:CATL")
            self.assertEqual(saved_a["listing_currency"], "CNY")
            self.assertEqual(saved_h["listing_currency"], "HKD")
            with storage.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM issuers").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 2)

    def test_round_trip_company_and_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            storage.save_facts([FinancialFact(**item) for item in demo_facts()])
            saved = storage.get_facts(DEMO_COMPANY.cik)
            self.assertEqual(len(saved), len(demo_facts()))
            self.assertEqual(saved[0]["company_cik"], DEMO_COMPANY.cik)

    def test_setting_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_setting("model", "example")
            self.assertEqual(storage.get_setting("model"), "example")
            self.assertEqual(storage.get_setting("missing", "fallback"), "fallback")

    def test_language_settings_are_independent_and_do_not_create_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_setting("ui_language", "en")
            storage.set_setting("report_language", "zh-CN")
            self.assertEqual(storage.get_setting("ui_language"), "en")
            self.assertEqual(storage.get_setting("report_language"), "zh-CN")
            self.assertEqual(storage.get_setting("api_key", ""), "")

    def test_running_runs_are_recovered_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            run = ResearchRun(
                run_id="interrupted-run",
                company=DEMO_COMPANY,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="openai-compatible",
                model_id="test",
                data_as_of="2026-01-01T00:00:00+00:00",
                status=RunStatus.RUNNING,
            )
            storage.save_run(run)
            self.assertEqual(storage.interrupt_running_runs(), 1)
            recovered = storage.list_runs()[0]
            self.assertEqual(recovered["status"], RunStatus.CANCELLED.value)
            self.assertTrue(recovered["completed_at"])
            self.assertEqual(storage.interrupt_running_runs(), 0)

    def test_thesis_versions_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            first = storage.save_thesis_version(
                DEMO_COMPANY.cik,
                {"thesis": "first"},
                created_by="test",
                created_at=utc_now_iso(),
            )
            second = storage.save_thesis_version(
                DEMO_COMPANY.cik,
                {"thesis": "second"},
                created_by="user",
                created_at=utc_now_iso(),
            )
            self.assertEqual(first["version"], 1)
            self.assertEqual(second["version"], 2)
            versions = storage.list_thesis_versions(DEMO_COMPANY.cik)
            self.assertEqual(len(versions), 2)
            restored = storage.get_thesis_version(second["thesis_version_id"])
            self.assertIsNotNone(restored)
            self.assertEqual(restored["content"]["thesis"], "second")


if __name__ == "__main__":
    unittest.main()
