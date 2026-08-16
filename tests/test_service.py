from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from openthesis.demo import demo_facts
from openthesis.domain import (
    Company,
    FilingDocument,
    FinancialFact,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)
from openthesis.model_catalog import ModelDiscoveryError
from openthesis.markets import build_company
from openthesis.market_data import MarketDataError
from openthesis.service import AppService, PreferenceValidationError, _latest_sec_verified_group, _market_snapshot, _request_secrets, _ResearchJob
from openthesis.financial_ingestion import FinancialDataset, FinancialGroupValidation, FilingManifest, FinancialIngestionEngine
from openthesis.market_financials import FinancialValidation, ValidationStatus


class _FakeSecClient:
    def __init__(self, user_agent: str, _cache_dir: Path):
        self.user_agent = user_agent

    def search_companies(self, query: str, limit: int = 15) -> list[Company]:
        if query.lower() not in {"acme", "acm"}:
            return []
        return [Company(cik="0000000001", ticker="ACME", name="Acme Corp")][:limit]

    def list_annual_filings(self, company: Company, limit: int = 5) -> list[FilingDocument]:
        return [
            FilingDocument(
                document_id="sec:test-10k",
                company_cik=company.cik,
                accession_number="test-10k",
                form_type="10-K",
                fiscal_period="FY",
                period_end="2025-12-31",
                filed_at="2026-02-01T00:00:00+00:00",
                primary_document="annual25.htm",
                source_url="https://www.sec.gov/Archives/test-10k/annual25.htm",
            )
        ][:limit]

    def get_company_facts(self, company: Company) -> list[FinancialFact]:
        return [
            FinancialFact(**{**item, "company_cik": company.cik})
            for item in demo_facts()
        ]


class _FakeMarketData:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    def resolve(self, query: str, market, *, limit: int = 15):
        self.calls.append((query, market.value, limit))
        return [build_company("832982.BJ", "Jinbo Bio")]


class _ResearchMarketData:
    def __init__(self, adapter):
        self.adapter = adapter

    def adapter_for(self, _company: Company):
        return self.adapter


class AppServiceTests(unittest.TestCase):
    def test_job_leaves_download_stage_before_financial_ingestion_blocks(self) -> None:
        company = build_company("002594.SZ", "BYD")
        filing = FilingDocument(
            "q1-26", company.security_id, "q1-26", "QUARTERLY_REPORT", "Q1",
            "2026-03-31", "2026-04-28", "2026 Q1.pdf", "https://example.test/q1-26.pdf",
        )
        entered = threading.Event()
        release = threading.Event()
        download_entered = threading.Event()
        download_release = threading.Event()

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [filing][:limit]
            def download_filing(self, item, _target):
                download_entered.set()
                download_release.wait(timeout=2)
                return item

        class BlockingEngine:
            def ingest(self, _company, _filings, **_kwargs):
                entered.set()
                release.wait(timeout=2)
                validation = FinancialValidation(
                    ValidationStatus.REJECTED, ("no_financial_facts",), frozenset(), (), ()
                )
                manifest = FilingManifest(
                    filing.document_id, filing.accession_number, filing.source_url,
                    filing.primary_document, filing.form_type, filing.fiscal_period,
                    filing.period_end, filing.revision, filing.supersedes_document_id,
                    filing.content_hash,
                )
                return FinancialDataset((), (), (manifest,), validation, (), ())

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(Adapter()),
                financial_ingestion_engine=BlockingEngine(),
            )
            started = service.start_research({
                "mode": "company",
                "company": company.to_dict(),
                "download_filings": True,
                "model": {"preset_id": "none"},
            })
            self.assertTrue(download_entered.wait(timeout=2), "download did not start")
            downloading = service.get_research_status(started["job_id"])
            self.assertEqual(downloading["stage"], "filing-download")
            self.assertEqual(downloading["stage_current"], 1)
            self.assertEqual(downloading["stage_total"], 1)
            download_release.set()
            self.assertTrue(entered.wait(timeout=2), "ingestion did not start")
            status = service.get_research_status(started["job_id"])
            self.assertEqual(status["stage"], "filing-parse")
            self.assertEqual(status["stage_current"], 0)
            self.assertEqual(status["stage_total"], 1)
            self.assertGreaterEqual(status["percent"], 18)
            release.set()
            deadline = time.monotonic() + 3
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "blocked ingestion did not settle")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

    def test_job_progress_is_monotonic_and_stage_counts_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            job = _ResearchJob("progress-job", percent=40)

            service._update_job(
                job,
                stage="filing-parse",
                percent=18,
                stage_current=8,
                stage_total=6,
            )

            self.assertEqual(job.percent, 40)
            self.assertEqual(job.stage_current, 6)
            self.assertEqual(job.stage_total, 6)
            service._update_job(job, percent=100)
            self.assertEqual(job.percent, 100)

    def test_vision_requires_consent_and_clears_session_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory_calls = []
            service = AppService(
                Path(directory),
                vision_adapter_factory=lambda config: factory_calls.append(config) or object(),
            )
            request = {
                "mode": "demo",
                "model": {"preset_id": "none"},
                "vision_fallback": {"enabled": True, "consent": False, "provider": "mineru_lite", "token": "session-secret"},
            }
            self.assertEqual(_request_secrets(request), ("session-secret",))
            job = service.start_research(request)
            deadline = time.time() + 3
            status = service.get_research_status(job["job_id"])
            while status["state"] in {"queued", "running"} and time.time() < deadline:
                time.sleep(0.01)
                status = service.get_research_status(job["job_id"])
            self.assertEqual(status["error_code"], "VISION_CONSENT_REQUIRED")
            self.assertEqual(factory_calls, [])
            self.assertEqual(request["vision_fallback"]["token"], "")

    def test_vision_decision_is_sidecar_safe_and_snapshot_hides_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            job = _ResearchJob("vision-job")
            job.stage = "vision-approval"
            job.vision_approval_pending = True
            job.vision_upload_preview = {"provider": "mineru_lite", "pages": (3,), "total_bytes": 10, "source_document": "annual.pdf", "filing_hash": "abc"}
            with service._jobs_lock:
                service._jobs[job.job_id] = job
            snapshot = service.vision_decision(job.job_id, True)
            self.assertTrue(snapshot["vision_approval"])
            self.assertNotIn("local_path", snapshot["vision_upload_preview"])
            self.assertNotIn("source_url", snapshot["vision_upload_preview"])
            self.assertNotIn("api_key", snapshot["vision_upload_preview"])
            with self.assertRaises(ValueError):
                service.vision_decision(job.job_id, False)
    def test_latest_annual_rejected_does_not_fall_back_to_old_verified_group(self) -> None:
        company = build_company("300750.SZ", "CATL")
        old = FilingDocument(
            document_id="old", company_cik=company.security_id, accession_number="old",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2024-12-31",
            filed_at="2025-03-01", primary_document="old.pdf", source_url="https://example.test/old",
        )
        latest = FilingDocument(
            document_id="latest", company_cik=company.security_id, accession_number="latest",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2025-12-31",
            filed_at="2026-03-01", primary_document="latest.pdf", source_url="https://example.test/latest",
        )
        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [old, latest][:limit]
            def download_filing(self, item, _target): return item
        rejected = FinancialValidation(
            ValidationStatus.REJECTED, ("core_coverage_insufficient",), frozenset(), (), ()
        )
        verified = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (), ())
        class FakeEngine:
            def ingest(self, _company, _filings):
                manifests = tuple(
                    FilingManifest(f.document_id, f.accession_number, f.source_url, f.primary_document,
                                   f.form_type, f.fiscal_period, f.period_end, f.revision,
                                   f.supersedes_document_id, f.content_hash)
                    for f in (old, latest)
                )
                return FinancialDataset(
                    (), (), manifests, FinancialValidation(ValidationStatus.READY_WITH_WARNINGS, (), frozenset(), (), ()), (),
                    (FinancialGroupValidation(("old", "2024-12-31", "FY", "consolidated", "CNY"), verified),
                     FinancialGroupValidation(("latest", "2025-12-31", "FY", "consolidated", "CNY"), rejected)),
                )
        provider_calls = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter()),
                financial_ingestion_engine=FakeEngine(),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            started = service.start_research({
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"preset_id": "custom", "model": "test", "base_url": "https://example.test/v1", "api_key": "session"},
            })
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])
            self.assertEqual(status["error_code"], "FILING_DATA_QUALITY_FAILED")
            self.assertEqual(provider_calls, [])

    def test_latest_verified_but_missing_core_stops_before_provider(self) -> None:
        company = build_company("300750.SZ", "CATL")
        filing = FilingDocument(
            "latest", company.security_id, "latest", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "latest.pdf", "https://example.test/latest",
        )
        fact = FinancialFact(
            "revenue-only", company.security_id, "revenue", "Revenue", 100.0, "CNY", 2025,
            "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31", "2026-03-01", "latest",
            filing.source_url, currency="CNY", validation_status="VERIFIED",
        )

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [filing][:limit]
            def download_filing(self, item, _target): return item

        class Engine:
            def ingest(self, _company, _filings):
                manifest = FilingManifest(
                    filing.document_id, filing.accession_number, filing.source_url,
                    filing.primary_document, filing.form_type, filing.fiscal_period,
                    filing.period_end, filing.revision, filing.supersedes_document_id,
                    filing.content_hash,
                )
                validation = FinancialValidation(
                    ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (fact,), ()
                )
                group = FinancialGroupValidation(
                    ("latest", "2025-12-31", "FY", "consolidated", "CNY"), validation
                )
                return FinancialDataset((fact,), (), (manifest,), validation, (), (group,))

        provider_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter()),
                financial_ingestion_engine=Engine(),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            started = service.start_research({
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"preset_id": "custom", "model": "test", "base_url": "https://example.test/v1", "api_key": "session"},
            })
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])
            self.assertEqual(status["error_code"], "FILING_DATA_QUALITY_FAILED")
            self.assertEqual(provider_calls, [])

    def test_parent_and_wrong_currency_groups_never_enter_research_facts(self) -> None:
        company = build_company("300750.SZ", "CATL")
        filing = FilingDocument(
            "latest", company.security_id, "latest", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "latest.pdf", "https://example.test/latest",
        )
        def fact(fid: str, concept: str, *, scope="consolidated", currency="CNY") -> FinancialFact:
            return FinancialFact(
                fid, company.security_id, concept, concept, 1.0, currency, 2025,
                "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31", "2026-03-01", "latest",
                filing.source_url, consolidated_scope=scope, currency=currency,
                validation_status="VERIFIED",
            )
        core = tuple(fact(f"core-{name}", name) for name in (
            "revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"
        ))
        parent = fact("parent-revenue", "revenue", scope="parent")
        foreign = fact("foreign-revenue", "revenue", currency="USD")
        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [filing][:limit]
            def download_filing(self, item, _target): return item
        class Engine:
            def ingest(self, _company, _filings):
                manifest = FilingManifest(
                    filing.document_id, filing.accession_number, filing.source_url,
                    filing.primary_document, filing.form_type, filing.fiscal_period,
                    filing.period_end, filing.revision, filing.supersedes_document_id, filing.content_hash,
                )
                def group(items, scope, currency):
                    validation = FinancialValidation(
                        ValidationStatus.VERIFIED, (), frozenset(item.concept for item in items), items, ()
                    )
                    return FinancialGroupValidation(("latest", "2025-12-31", "FY", scope, currency), validation)
                groups = (group(core, "consolidated", "CNY"), group((parent,), "parent", "CNY"), group((foreign,), "consolidated", "USD"))
                aggregate = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset(item.concept for item in (*core, parent, foreign)), (*core, parent, foreign), ())
                return FinancialDataset((*core, parent, foreign), (), (manifest,), aggregate, (), groups)
        class Provider:
            def generate(self, *_args, **_kwargs): return {"claims": []}
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter()),
                financial_ingestion_engine=Engine(), provider_factory=lambda _config: Provider(),
            )
            started = service.start_research({
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"preset_id": "custom", "model": "test", "base_url": "https://example.test/v1", "api_key": "session"},
            })
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])
            self.assertEqual(status["state"], "completed")
            saved = service.storage.get_facts(company.security_id)
            self.assertEqual({item["fact_id"] for item in saved}, {item.fact_id for item in core})
            self.assertEqual({item["fact_id"] for item in service.storage.get_facts_audit(company.security_id)}, {item.fact_id for item in (*core, parent, foreign)})

    def test_latest_annual_verified_allows_old_rejected_but_only_accepted_facts_continue(self) -> None:
        company = build_company("300750.SZ", "CATL")
        old = FilingDocument(
            document_id="old", company_cik=company.security_id, accession_number="old",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2024-12-31",
            filed_at="2025-03-01", primary_document="old.pdf", source_url="https://example.test/old",
        )
        latest = FilingDocument(
            document_id="latest", company_cik=company.security_id, accession_number="latest",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2025-12-31",
            filed_at="2026-03-01", primary_document="latest.pdf", source_url="https://example.test/latest",
        )
        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [old, latest][:limit]
            def download_filing(self, item, _target): return item
        def core_fact(fact_id: str, concept: str, value: float) -> FinancialFact:
            return FinancialFact(
                fact_id, company.security_id, concept, concept, value, "CNY", 2025,
                "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31", "2026-03-01", "latest",
                latest.source_url, statement=(
                    "balance_sheet" if concept in {"assets", "liabilities", "equity"}
                    else "cash_flow" if concept == "operating_cash_flow" else "income_statement"
                ), currency="CNY", validation_status="VERIFIED",
            )
        latest_core = tuple(
            core_fact(f"accepted-{concept}", concept, value)
            for concept, value in (
                ("revenue", 100.0), ("net_income", 20.0),
                ("operating_cash_flow", 25.0), ("assets", 200.0),
                ("liabilities", 80.0), ("equity", 120.0),
            )
        )
        rejected_fact = FinancialFact(
            "rejected-old", company.security_id, "revenue", "Revenue", 90.0, "CNY", 2024,
            "FY", "ANNUAL_REPORT", "2024-01-01", "2024-12-31", "2025-03-01", "old",
            old.source_url, validation_status="REJECTED",
        )
        class FakeEngine:
            def ingest(self, _company, _filings):
                manifests = tuple(
                    FilingManifest(f.document_id, f.accession_number, f.source_url, f.primary_document,
                                   f.form_type, f.fiscal_period, f.period_end, f.revision,
                                   f.supersedes_document_id, f.content_hash)
                    for f in (old, latest)
                )
                old_validation = FinancialValidation(ValidationStatus.REJECTED, ("bad_old",), frozenset(), (), (rejected_fact,))
                latest_validation = FinancialValidation(
                    ValidationStatus.VERIFIED, (), frozenset(item.concept for item in latest_core), latest_core, ()
                )
                groups = (
                    FinancialGroupValidation(("old", "2024-12-31", "FY", "consolidated", "CNY"), old_validation),
                    FinancialGroupValidation(("latest", "2025-12-31", "FY", "consolidated", "CNY"), latest_validation),
                )
                aggregate = FinancialValidation(
                    ValidationStatus.READY_WITH_WARNINGS, ("bad_old",),
                    frozenset(item.concept for item in latest_core), latest_core, (rejected_fact,)
                )
                return FinancialDataset(latest_core, (), manifests, aggregate, ("bad_old",), groups)
        class Provider:
            def generate(self, *_args, **_kwargs): return {"claims": []}
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter()),
                financial_ingestion_engine=FakeEngine(), provider_factory=lambda _config: Provider(),
            )
            started = service.start_research({
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"preset_id": "custom", "model": "test", "base_url": "https://example.test/v1", "api_key": "session"},
            })
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])
            saved = service.storage.get_facts(company.security_id)
            audit = service.storage.get_facts_audit(company.security_id)
            self.assertEqual(
                {item["fact_id"] for item in saved},
                {f"accepted-{concept}" for concept in ("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity")},
            )
            self.assertEqual(
                {item["fact_id"] for item in audit},
                {f"accepted-{concept}" for concept in ("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity")} | {"rejected-old"},
            )
    def test_delete_research_run_removes_only_the_requested_finished_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(build_company("688981.SH", "SMIC"))
            company = build_company("688981.SH", "SMIC")
            service.storage.save_run(
                ResearchRun(
                    run_id="run-delete-service",
                    company=company,
                    workflow_id="test",
                    research_pack_id="test",
                    research_pack_version="1",
                    provider_id="none",
                    model_id="",
                    data_as_of="2026-08-09T00:00:00+00:00",
                    status=RunStatus.FAILED,
                    completed_at=utc_now_iso(),
                )
            )

            self.assertEqual(
                service.delete_research_run("run-delete-service"),
                {"run_id": "run-delete-service", "deleted": True},
            )
            self.assertEqual(service.list_research_runs(), [])

    def test_verified_no_filings_stops_before_model_provider_creation(self) -> None:
        class NoFilingsAdapter:
            def list_financial_filings(self, _company: Company, *, limit: int = 5):
                return []

        provider_calls: list[str] = []

        def provider_factory(config):
            provider_calls.append(config.public_id)
            raise AssertionError("model provider must not be created without a financial report")

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(NoFilingsAdapter()),
                provider_factory=provider_factory,
            )
            company = build_company("688825.SH", "长鑫科技")
            started = service.start_research(
                {
                    "mode": "company",
                    "company": company.to_dict(),
                    "download_filings": True,
                    "model": {
                        "preset_id": "custom",
                        "model": "test-model",
                        "base_url": "https://example.test/v1",
                        "api_key": "session-only",
                    },
                }
            )

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "no-filings research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error_code"], "NO_FILINGS_AVAILABLE")
            self.assertEqual(status["market"], "CN_A")
            self.assertEqual(status["disclosure_url"], "https://www.cninfo.com.cn/new/index")
            self.assertNotIn("不代表", status["message"])
            self.assertEqual(provider_calls, [])

    def test_rejected_filing_quality_stops_before_provider_and_storage_write(self) -> None:
        company = build_company("832982.BJ", "Jinbo Bio")
        filing = FilingDocument(
            document_id="official:jinbo-bad",
            company_cik=company.security_id,
            accession_number="jinbo-bad",
            form_type="ANNUAL_REPORT",
            fiscal_period="FY",
            period_end="2023-12-31",
            filed_at="2024-04-25T00:00:00+00:00",
            primary_document="2023 Annual Report",
            source_url="https://example.invalid/jinbo.pdf",
        )

        class BadFilingAdapter:
            def list_financial_filings(self, _company: Company, *, limit: int = 5):
                return [filing][:limit]

            def download_filing(self, item: FilingDocument, _target: Path) -> FilingDocument:
                return item

        provider_calls: list[str] = []

        def provider_factory(config):
            provider_calls.append(config.public_id)
            raise AssertionError("provider must not be created for rejected facts")

        bad_fact = FinancialFact(
            fact_id="bad:revenue",
            company_cik=company.security_id,
            concept="revenue",
            reported_concept="revenue",
            value=100.0,
            unit="CNY",
            fiscal_year=2023,
            fiscal_period="FY",
            form_type="ANNUAL_REPORT",
            start_date="2023-01-01",
            end_date="2023-12-31",
            filed_at=filing.filed_at,
            accession_number=filing.accession_number,
            source_url=filing.source_url,
        )
        class BadEngine:
            def ingest(self, _company, _filings):
                manifest = FilingManifest(
                    filing.document_id, filing.accession_number, filing.source_url,
                    filing.primary_document, filing.form_type, filing.fiscal_period,
                    filing.period_end, filing.revision, filing.supersedes_document_id,
                    filing.content_hash,
                )
                validation = FinancialValidation(
                    ValidationStatus.REJECTED, ("income_statement_core_missing",),
                    frozenset({"revenue"}), (), (bad_fact,),
                )
                group = FinancialGroupValidation(
                    (filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"),
                    validation,
                )
                return FinancialDataset(
                    (), (), (manifest,), validation, ("income_statement_core_missing",), (group,)
                )

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(BadFilingAdapter()),
                financial_ingestion_engine=BadEngine(),
                provider_factory=provider_factory,
            )
            started = service.start_research(
                {
                    "mode": "company",
                    "company": company.to_dict(),
                    "download_filings": True,
                    "model": {
                        "preset_id": "custom",
                        "model": "test-model",
                        "base_url": "https://example.test/v1",
                        "api_key": "session-only",
                    },
                }
            )
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "quality-gate research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error_code"], "FILING_DATA_QUALITY_FAILED")
            self.assertEqual(provider_calls, [])
            self.assertEqual(service.storage.get_facts(company.security_id), [])

    def test_unverified_official_response_is_distinct_and_skips_model(self) -> None:
        class UnverifiedAdapter:
            def list_financial_filings(self, _company: Company, *, limit: int = 5):
                raise MarketDataError(
                    "ambiguous response",
                    code="FILING_STATUS_UNVERIFIED",
                )

        provider_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(UnverifiedAdapter()),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            company = build_company("688825.SH", "长鑫科技")
            started = service.start_research(
                {
                    "mode": "company",
                    "company": company.to_dict(),
                    "model": {
                        "preset_id": "custom",
                        "model": "test-model",
                        "base_url": "https://example.test/v1",
                        "api_key": "session-only",
                    },
                }
            )

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "unverified research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error_code"], "FILING_STATUS_UNVERIFIED")
            self.assertNotIn("ambiguous response", status["message"])
            self.assertEqual(provider_calls, [])

    def test_bootstrap_exposes_stable_contract_and_safe_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), app_version="1.0.0-alpha.1")

            result = service.bootstrap()

            self.assertEqual(result["contract_version"], "1.0")
            self.assertEqual(result["app_version"], "1.0.0-alpha.1")
            self.assertEqual(result["preferences"]["ui_language"], "zh-CN")
            self.assertEqual(result["preferences"]["report_language"], "zh-CN")
            self.assertEqual(result["preferences"]["parallel_agents"], "false")
            self.assertEqual(result["recent_runs"], [])
            self.assertEqual(
                {item["market"] for item in result["market_catalog"]},
                {"US", "CN_A", "HK"},
            )
            self.assertTrue(any(item["exchange"] == "BSE" for item in result["common_companies"]))

    def test_preferences_persist_only_allowlisted_non_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            service = AppService(data_dir)

            saved = service.update_preferences(
                {"ui_language": "en", "report_language": "zh-CN"}
            )

            self.assertEqual(saved["ui_language"], "en")
            self.assertEqual(AppService(data_dir).bootstrap()["preferences"], saved)
            with self.assertRaises(PreferenceValidationError):
                service.update_preferences({"api_key": "never-store-this"})
            self.assertEqual(service.storage.get_setting("api_key", ""), "")

    def test_unknown_report_has_a_stable_not_found_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(KeyError, "research run not found"):
                service.get_report("missing")

    def test_report_technical_details_are_explicitly_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000001", ticker="ACME", name="Acme Corp")
            run = ResearchRun(
                run_id="technical-report",
                company=company,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="test",
                model_id="test",
                data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_artifact(
                ResearchArtifact(
                    artifact_id="growth-1",
                    run_id=run.run_id,
                    artifact_type="growth-opportunities",
                    title="Growth opportunities",
                    content={
                        "opportunities": [
                            {
                                "title": "New platform",
                                "mechanism": "Expand the addressable market.",
                                "evidence_grade": "C",
                                "supporting_evidence_ids": ["fact:technical-only"],
                            }
                        ]
                    },
                    model_id="test:model",
                    agent_id="growth-opportunity-analyst",
                )
            )

            default_report = service.get_report(run.run_id, language="en")
            technical_report = service.get_report(
                run.run_id, language="en", include_technical=True
            )

            self.assertNotIn("fact:technical-only", default_report["markdown"])
            self.assertIn("fact:technical-only", technical_report["markdown"])
            self.assertNotIn("fact:technical-only", default_report["html"])
            self.assertIn("fact:technical-only", technical_report["html"])

    def test_report_marks_empty_growth_model_output_as_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000002", ticker="GROW", name="Growth Corp")
            run = ResearchRun(
                run_id="growth-retryable",
                company=company,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="test",
                model_id="test",
                data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_artifact(
                ResearchArtifact(
                    artifact_id="growth-empty",
                    run_id=run.run_id,
                    artifact_type="growth-opportunities",
                    title="Growth opportunities",
                    content={
                        "opportunities": [],
                        "structured_output_valid": False,
                        "_response_error": "empty_content",
                        "_validation": {"passed": False, "issues": ["empty"]},
                    },
                    model_id="test:model",
                    agent_id="growth-opportunity-analyst",
                )
            )

            report = service.get_report(run.run_id, language="en")

            self.assertTrue(report["retryable_growth"])
            self.assertIn("growth-opportunity model returned no usable content", report["markdown"])

    def test_demo_research_job_reports_progress_and_produces_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            started = service.start_research({"mode": "demo"})
            self.assertEqual(started["state"], "queued")
            self.assertNotIn("api_key", str(started))

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "demo research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["percent"], 100)
            self.assertTrue(status["run_id"])
            report = service.get_report(status["run_id"])
            self.assertIn("OpenThesis", report["markdown"])

    def test_research_job_exposes_agent_progress_and_cancel_acknowledgement(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider:
            def test_connection(self) -> str:
                return "connected"

            def generate(self, _system_prompt: str, _user_prompt: str, *, json_mode: bool = True):
                entered.set()
                release.wait(timeout=2)
                return {"claims": []}

        def provider_factory(_config):
            return BlockingProvider()

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), provider_factory=provider_factory)
            started = service.start_research(
                {
                    "mode": "demo",
                    "model": {
                        "preset_id": "custom",
                        "model": "blocking",
                        "base_url": "https://example.test/v1",
                        "api_key": "session-only",
                    },
                }
            )
            self.assertIn("agent_states", started)
            self.assertIn("total_agents", started)
            deadline = time.monotonic() + 3
            while not entered.is_set():
                self.assertLess(time.monotonic(), deadline, "provider did not start")
                time.sleep(0.01)
            cancelling = service.cancel_research(started["job_id"])
            self.assertEqual(cancelling["state"], "cancelling")
            self.assertTrue(cancelling["cancel_requested"])
            self.assertNotEqual(cancelling["message"], started["message"])
            release.set()
            deadline = time.monotonic() + 3
            status = service.get_research_status(started["job_id"])
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "cancelled job did not settle")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

    def test_model_timeout_is_forwarded_to_provider(self) -> None:
        seen: dict[str, int] = {}

        class Provider:
            def test_connection(self) -> str:
                return "connected"

        def provider_factory(config):
            seen["timeout"] = config.timeout_seconds
            return Provider()

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), provider_factory=provider_factory)
            result = service.test_model_connection(
                {
                    "preset_id": "custom",
                    "model": "timeout-model",
                    "base_url": "https://example.test/v1",
                    "api_key": "session-only",
                    "timeout_seconds": 300,
                }
            )
            self.assertTrue(result["ok"])
            self.assertEqual(seen["timeout"], 300)

    def test_unknown_research_job_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(KeyError, "research job not found"):
                service.get_research_status("missing")

    def test_bootstrap_exposes_common_companies_models_and_research_packs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            result = service.bootstrap()

            self.assertIn("company.search", result["capabilities"])
            self.assertIn("models.discover", result["capabilities"])
            self.assertEqual(result["common_companies"][0]["ticker"], "AAPL")
            preset_ids = {item["preset_id"] for item in result["model_catalog"]}
            self.assertEqual(
                preset_ids,
                {
                    "none",
                    "deepseek",
                    "qwen",
                    "kimi",
                    "kimi-global",
                    "glm",
                    "openai",
                    "gemini",
                    "openrouter",
                    "ollama",
                    "custom",
                },
            )
            self.assertTrue(result["research_packs"])

    def test_company_search_uses_saved_sec_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), sec_client_factory=_FakeSecClient)
            service.update_preferences(
                {"sec_contact_profile": "personal", "sec_contact_email": "me@example.com"}
            )

            matches = service.search_companies("acme")

            self.assertEqual(matches[0]["ticker"], "ACME")

    def test_a_share_search_does_not_require_sec_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            market_data = _FakeMarketData()
            service = AppService(Path(directory), market_data=market_data)

            matches = service.search_companies("832982", market="CN_A")

            self.assertEqual(matches[0]["ticker"], "832982.BJ")
            self.assertEqual(matches[0]["listing_currency"], "CNY")
            self.assertEqual(market_data.calls, [("832982", "CN_A", 15)])

    def test_manual_market_snapshot_is_explicit_and_currency_aware(self) -> None:
        company = build_company("00700.HK", "Tencent")

        snapshot = _market_snapshot(
            {
                "price": 555.5,
                "market_cap_billions": 5_200,
                "currency": "HKD",
                "as_of": "2026-08-09",
            },
            company,
        )

        self.assertEqual(snapshot["source"], "manual")
        self.assertEqual(snapshot["market_cap"], 5_200_000_000_000)
        self.assertEqual(snapshot["currency"], "HKD")
        with self.assertRaisesRegex(ValueError, "as-of date"):
            _market_snapshot({"price": 1}, company)

    def test_model_discovery_merges_recommendations_without_retaining_key(self) -> None:
        seen: dict[str, str] = {}

        def discoverer(preset, base_url: str, api_key: str):
            seen.update(preset=preset.preset_id, base_url=base_url, api_key=api_key)
            return ("remote-model", preset.recommended_models[0])

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), model_discoverer=discoverer)

            result = service.discover_models_for_session(
                {"preset_id": "deepseek", "api_key": "sk-session-only"}
            )

            self.assertEqual(result["models"][0], "deepseek-v4-pro")
            self.assertIn("remote-model", result["models"])
            self.assertEqual(seen["api_key"], "sk-session-only")
            self.assertNotIn("sk-session-only", str(service.bootstrap()))
            self.assertNotIn("sk-session-only", str(service.__dict__))

    def test_model_discovery_failure_keeps_recommended_models(self) -> None:
        def failing_discoverer(*_args, **_kwargs):
            raise ModelDiscoveryError("catalog unavailable")

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), model_discoverer=failing_discoverer)

            result = service.discover_models_for_session(
                {"preset_id": "openai", "api_key": "secret"}
            )

            self.assertEqual(result["models"][0], "gpt-5.6-terra")
            self.assertEqual(result["warning"], "catalog unavailable")
            self.assertNotIn("secret", str(result))

    def test_legacy_kimi_endpoint_selects_matching_region_preset(self) -> None:
        seen: dict[str, str] = {}

        def discoverer(preset, base_url: str, api_key: str):
            seen.update(preset=preset.preset_id, base_url=base_url)
            return ("kimi-k2.7-code",)

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), model_discoverer=discoverer)
            result = service.discover_models_for_session(
                {
                    "preset_id": "kimi",
                    "base_url": "https://api.moonshot.ai/v1",
                    "api_key": "session-only",
                }
            )

            self.assertEqual(seen, {
                "preset": "kimi-global",
                "base_url": "https://api.moonshot.ai/v1",
            })
            self.assertIn("kimi-k2.7-code", result["models"])

    def test_real_company_research_uses_sec_data_and_produces_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), sec_client_factory=_FakeSecClient)
            service.update_preferences(
                {"sec_contact_profile": "personal", "sec_contact_email": "me@example.com"}
            )
            started = service.start_research(
                {
                    "mode": "company",
                    "company": {
                        "cik": "0000000001",
                        "ticker": "ACME",
                        "name": "Acme Corp",
                        "exchange": "",
                    },
                    "download_filings": False,
                    "model": {"preset_id": "none", "api_key": ""},
                }
            )

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "company research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            report = service.get_report(status["run_id"])
            self.assertEqual(report["ticker"], "ACME")
            self.assertIn("OpenThesis", report["markdown"])

    def test_thesis_edits_append_versions_through_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000001", ticker="ACME", name="Acme Corp")
            service.storage.save_company(company)
            service.storage.save_thesis_version(
                company.cik,
                {"thesis": "initial"},
                created_by="test",
                created_at=utc_now_iso(),
            )

            saved = service.save_thesis_version(
                "0000000001", {"thesis": "revised"}
            )

            self.assertEqual(saved["version"], 2)
            self.assertEqual(service.list_theses()[0]["version"], 2)
            loaded = service.get_thesis(saved["thesis_version_id"])
            self.assertEqual(loaded["content"]["thesis"], "revised")

    def test_bootstrap_recovers_research_left_running_by_previous_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = AppService(data_dir)
            run = ResearchRun(
                run_id="interrupted-run",
                company=Company(cik="0000000001", ticker="ACME", name="Acme Corp"),
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="none",
                model_id="",
                data_as_of=utc_now_iso(),
                status=RunStatus.RUNNING,
            )
            first.storage.save_company(run.company)
            first.storage.save_run(run)

            recovered = AppService(data_dir)

            self.assertEqual(recovered.bootstrap()["interrupted_runs"], 1)
            self.assertEqual(
                recovered.storage.get_run(run.run_id)["status"],
                RunStatus.CANCELLED.value,
            )

    def test_installs_declarative_research_pack_from_protocol_payload(self) -> None:
        manifest = {
            "api_version": "openthesis.io/v1alpha1",
            "kind": "ResearchPack",
            "metadata": {"id": "service.pack", "name": "Service Pack", "version": "1"},
            "permissions": {"network": False, "filesystem": False, "execute_code": False},
        }
        workflow = {
            "workflow": {
                "id": "service",
                "steps": [{"id": "one", "prompt": "prompts/one.md"}],
            }
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("manifest.yaml", json.dumps(manifest))
            package.writestr("workflow.yaml", json.dumps(workflow))
            package.writestr("prompts/one.md", "Return JSON.")

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            installed = service.install_research_pack(
                "service.othesis", base64.b64encode(archive.getvalue()).decode("ascii")
            )

            self.assertEqual(installed["pack_id"], "service.pack")
            self.assertTrue(any(pack["pack_id"] == "service.pack" for pack in service.research_packs()))

    def test_model_connection_test_uses_session_key_without_persisting_it(self) -> None:
        seen: dict[str, str] = {}

        class Provider:
            def test_connection(self) -> str:
                return "connected"

        def provider_factory(config):
            seen["api_key"] = config.api_key
            return Provider()

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), provider_factory=provider_factory)
            result = service.test_model_connection(
                {
                    "preset_id": "openai",
                    "model": "gpt-test",
                    "base_url": "https://api.example.test/v1",
                    "api_key": "sk-session-model-test",
                }
            )

            self.assertEqual(result, {"ok": True, "message": "connected"})
            self.assertEqual(seen["api_key"], "sk-session-model-test")
            self.assertNotIn("sk-session-model-test", str(service.__dict__))

    def test_model_connection_test_trims_session_key_before_provider(self) -> None:
        seen: dict[str, str] = {}

        class Provider:
            def test_connection(self) -> str:
                return "connected"

        def provider_factory(config):
            seen["api_key"] = config.api_key
            return Provider()

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), provider_factory=provider_factory)
            service.test_model_connection(
                {
                    "preset_id": "kimi",
                    "model": "kimi-k3",
                    "base_url": "https://api.moonshot.ai/v1",
                    "api_key": " platform-key\n",
                }
            )

            self.assertEqual(seen["api_key"], "platform-key")

    def test_model_comparison_requires_two_enabled_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(
                ValueError, "both models require an enabled provider"
            ):
                service.start_research(
                    {
                        "mode": "demo",
                        "model": {"preset_id": "none"},
                        "compare_enabled": True,
                        "comparison_model": {
                            "preset_id": "custom",
                            "model": "comparison-model",
                            "base_url": "https://api.example.test/v1",
                            "api_key": "session-only",
                        },
                    }
                )

    def test_sec_latest_invalid_group_does_not_fallback_to_prior_year(self) -> None:
        company = build_company("AAPL", "Apple")

        def make_fact(accession: str, end: str, concept: str, value: float) -> FinancialFact:
            statement = (
                "cash_flow" if concept == "operating_cash_flow"
                else "balance_sheet" if concept in {"assets", "liabilities", "equity", "total_equity"}
                else "income_statement"
            )
            year = end[:4]
            return FinancialFact(
                f"{accession}:{concept}", company.cik, concept, concept, value, "USD", int(year),
                "FY", "10-K", f"{year}-01-01", end, f"{int(year) + 1}-02-01", accession,
                f"https://www.sec.gov/Archives/{accession}", statement=statement,
                scope="consolidated", consolidated_scope="consolidated", currency="USD",
                raw_text=f"{concept} {value}", validation_status="VERIFIED",
            )

        concepts = ("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity")
        old_values = (100.0, 20.0, 25.0, 200.0, 80.0, 120.0)
        latest_values = (110.0, 22.0, 25.0, 200.0, 80.0, 10.0)  # broken balance equation
        facts = [
            *(make_fact("old-10k", "2024-12-31", concept, value) for concept, value in zip(concepts, old_values)),
            *(make_fact("latest-10k", "2025-12-31", concept, value) for concept, value in zip(concepts, latest_values)),
        ]
        result = _latest_sec_verified_group(
            facts, FinancialIngestionEngine(), expected_period_end="2025-12-31"
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
