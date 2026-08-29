from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
from openthesis.markets import build_company
from openthesis.market_data import MarketDataError
from openthesis.ot import compile_studio_draft, minimal_studio_draft
from openthesis.service import (
    AppService,
    PreferenceValidationError,
    _bounded_download_filings,
    _cached_us_annual_window_is_complete,
    _latest_sec_verified_group,
    _market_snapshot,
    _request_secrets,
    _vision_config_from_request,
    _research_history_years,
    _research_data_message,
    _ResearchJob,
    _FinancialReportRefreshError,
    FinancialRetryResult,
    _financial_status,
    _canonical_retry_snapshot,
    _canonical_snapshot_digest,
)
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
    def test_unsafe_financial_write_has_localized_fail_closed_guidance(self) -> None:
        for language, marker in (("zh-CN", "安全软件"), ("zh-Hant", "安全軟體"), ("en", "Security software")):
            message = _research_data_message("FILING_CONTENT_UNSAFE", language)
            self.assertIn(marker, message)
            self.assertNotIn("FILING_CONTENT_UNSAFE", message)

    def test_filing_fetch_failure_gets_one_model_free_automatic_retry(self) -> None:
        calls = 0
        provider_calls: list[str] = []

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5):
                nonlocal calls
                calls += 1
                raise MarketDataError("temporary official source failure")

        with tempfile.TemporaryDirectory() as directory:
            company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter()),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            started = service.start_research({
                "mode": "company", "company": company.to_dict(),
                "download_filings": True,
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
            })
            deadline = time.monotonic() + 3
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["error_code"], "FILING_FETCH_FAILED")
            self.assertEqual(calls, 2, "initial discovery plus exactly one automatic retry")
            self.assertEqual(provider_calls, [])

    def test_financial_retry_job_keeps_operation_result_when_report_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.retry_financials = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                _FinancialReportRefreshError({
                    "mode": "retry", "status": "partial", "accepted": ["fact:revenue"],
                    "error": "FILING_REPORT_REFRESH_FAILED",
                })
            )
            job = _ResearchJob("refresh-failed")
            service._run_financial_retry_job(job, "run-refresh-failed", False)
            snapshot = job.snapshot()
            self.assertEqual(snapshot["state"], "failed")
            self.assertEqual(snapshot["stage"], "report-refresh")
            self.assertEqual(snapshot["error_code"], "FILING_REPORT_REFRESH_FAILED")
            self.assertEqual(snapshot["operation_result"]["status"], "partial")
            self.assertEqual(snapshot["operation_result"]["accepted"], ["fact:revenue"])

    def test_job_records_per_stage_timings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            job = _ResearchJob("timed")
            service._update_job(job, state="running", stage="filing-download")
            time.sleep(0.01)
            service._update_job(job, stage="filing-parse")
            snapshot = job.snapshot()

            self.assertGreaterEqual(snapshot["stage_timings"]["filing-download"], 0.009)
            self.assertEqual(snapshot["stage"], "filing-parse")
            self.assertIn("stage_elapsed_seconds", snapshot)

    def test_download_helper_enforces_global_and_per_host_limits_with_deterministic_results(self) -> None:
        started: list[str] = []
        active = 0
        active_by_host: dict[str, int] = {}
        maximum = [0, 0]
        lock = threading.Lock()

        class Adapter:
            def download_filing(self, filing: FilingDocument, _target: Path) -> FilingDocument:
                nonlocal active
                host = filing.source_url.split("//", 1)[1].split("/", 1)[0]
                with lock:
                    started.append(filing.document_id)
                    active += 1
                    active_by_host[host] = active_by_host.get(host, 0) + 1
                    maximum[0] = max(maximum[0], active)
                    maximum[1] = max(maximum[1], active_by_host[host])
                time.sleep(0.01)
                with lock:
                    active -= 1
                    active_by_host[host] -= 1
                return filing

        filings = [_download_filing(f"f{index}", "a.example.test" if index < 4 else "b.example.test") for index in range(6)]
        progress: list[tuple[int, int]] = []
        downloaded, failures = _bounded_download_filings(
            Adapter(), filings, Path("."), progress=lambda done, total: progress.append((done, total))
        )

        self.assertEqual([item.document_id for item in downloaded], [item.document_id for item in filings])
        self.assertEqual(failures, [])
        self.assertLessEqual(maximum[0], 3)
        self.assertLessEqual(maximum[1], 2)
        self.assertEqual(progress[-1], (len(filings), len(filings)))

    def test_download_helper_deduplicates_urls_retries_once_and_keeps_partial_success(self) -> None:
        calls: dict[str, int] = {}

        class Adapter:
            def download_filing(self, filing: FilingDocument, _target: Path) -> FilingDocument:
                calls[filing.source_url] = calls.get(filing.source_url, 0) + 1
                if filing.document_id == "retry" and calls[filing.source_url] == 1:
                    raise MarketDataError("temporary", code="FILING_FETCH_FAILED")
                if filing.document_id == "bad":
                    raise OSError("permanent")
                return filing

        duplicate = _download_filing("duplicate", "a.example.test", url="https://a.example.test/shared.pdf")
        filings = [
            duplicate,
            _download_filing("retry", "a.example.test"),
            _download_filing("bad", "b.example.test"),
            _download_filing("other", "b.example.test"),
        ]
        downloaded, failures = _bounded_download_filings(Adapter(), filings, Path("."))

        self.assertEqual([item.document_id for item in downloaded], ["duplicate", "retry", "other"])
        self.assertEqual([item.document_id for item, _ in failures], ["bad"])
        self.assertEqual(calls["https://a.example.test/shared.pdf"], 1)
        self.assertEqual(calls["https://a.example.test/retry.pdf"], 2)
        self.assertEqual(calls["https://b.example.test/bad.pdf"], 1)

    def test_download_helper_cancel_prevents_queued_work(self) -> None:
        cancel = threading.Event()
        cancel.set()
        calls: list[str] = []

        class Adapter:
            def download_filing(self, filing: FilingDocument, _target: Path) -> FilingDocument:
                calls.append(filing.document_id)
                return filing

        downloaded, failures = _bounded_download_filings(
            Adapter(), [_download_filing("one", "a.example.test"), _download_filing("two", "b.example.test")], Path("."), cancel_event=cancel
        )
        self.assertEqual(downloaded, [])
        self.assertEqual(failures, [])
        self.assertEqual(calls, [])

    def test_research_history_years_is_bounded_and_defaults_to_five(self) -> None:
        self.assertEqual(_research_history_years({}), 5)
        self.assertEqual(_research_history_years({"evidence_policy": {"annual_history_years": 2}}), 2)
        self.assertEqual(_research_history_years({"evidence_policy": {"annual_history_years": 100}}), 10)
        self.assertEqual(_research_history_years({"evidence_policy": {"annual_history_years": 1}}), 2)

    def test_research_history_uses_pack_policy_and_request_override(self) -> None:
        class Pack:
            workflow = {"settings": {"evidence_policy": {"annual_history_years": 7}}}

        self.assertEqual(_research_history_years({}, Pack()), 7)
        self.assertEqual(
            _research_history_years(
                {"evidence_policy": {"annual_history_years": 3}}, Pack()
            ),
            3,
        )
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
                "model": {},
            })
            self.assertTrue(download_entered.wait(timeout=2), "download did not start")
            downloading = service.get_research_status(started["job_id"])
            self.assertEqual(downloading["stage"], "filing-download")
            self.assertEqual(downloading["stage_current"], 0)
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

    def test_vision_requires_consent_without_accepting_session_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory_calls = []
            service = AppService(
                Path(directory),
                vision_adapter_factory=lambda config: factory_calls.append(config) or object(),
            )
            request = {
                "mode": "demo",
                "model": {},
                "vision_fallback": {
                    "enabled": True,
                    "consent": False,
                    "model": {
                        "configured_model_id": "test.vision",
                        "configuration_version": 2,
                        "role": "vision",
                    },
                },
            }
            self.assertEqual(_request_secrets(request), ())
            job = service.start_research(request)
            deadline = time.time() + 3
            status = service.get_research_status(job["job_id"])
            while status["state"] in {"queued", "running"} and time.time() < deadline:
                time.sleep(0.01)
                status = service.get_research_status(job["job_id"])
            self.assertEqual(status["error_code"], "VISION_CONSENT_REQUIRED")
            self.assertEqual(factory_calls, [])

    def test_mineru_flash_request_has_no_secret_or_model_and_forces_page_approval(self) -> None:
        config = _vision_config_from_request({
            "enabled": True,
            "consent": True,
            "provider": "mineru_flash",
            "require_page_approval": True,
        })
        self.assertIsNotNone(config)
        self.assertEqual(config.provider, "mineru_flash")
        self.assertEqual(config.configured_model_id, "")
        self.assertTrue(config.require_page_approval)
        self.assertFalse(hasattr(config, "token"))
        self.assertFalse(hasattr(config, "api_key"))

        for secret_name in ("token", "api_key", "endpoint", "model_id"):
            with self.subTest(secret_name=secret_name):
                with self.assertRaises(ValueError):
                    _vision_config_from_request({
                        "enabled": True,
                        "consent": True,
                        "provider": "mineru_flash",
                        "require_page_approval": True,
                        secret_name: "must-not-enter-python",
                    })
        with self.assertRaises(ValueError):
            _vision_config_from_request({"enabled": True, "consent": True, "provider": "mineru_flash", "require_page_approval": False})

            self.assertNotIn("api_key", json.dumps(request))
    def test_vision_decision_is_sidecar_safe_and_snapshot_hides_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            job = _ResearchJob("vision-job")
            job.stage = "vision-approval"
            job.vision_approval_pending = True
            job.vision_upload_preview = {"provider": "mineru_flash", "pages": (3,), "total_bytes": 10, "source_document": "annual.pdf", "filing_hash": "abc"}
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
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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
                fid, company.security_id, concept, concept,
                2.0 if concept == "assets" else 1.0, currency, 2025,
                "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31", "2026-03-01", "latest",
                filing.source_url, consolidated_scope=scope, currency=currency,
                statement=("cash_flow" if concept == "operating_cash_flow" else "balance_sheet" if concept in {"assets", "liabilities", "equity"} else "income_statement"),
                raw_text=f"{concept} 1", validation_status="VERIFIED",
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
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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
                raw_text=f"{concept} {value}",
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
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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

        requested_limits: list[int] = []

        class BadFilingAdapter:
            def list_financial_filings(self, _company: Company, *, limit: int = 5):
                requested_limits.append(limit)
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
                    "evidence_policy": {"annual_history_years": 2},
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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
            self.assertEqual(requested_limits, [5, 8])
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
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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

            self.assertEqual(result["contract_version"], "2.0")
            self.assertEqual(result["app_version"], "1.0.0-alpha.1")
            self.assertIn(result["preferences"]["ui_language"], {"zh-CN", "zh-Hant", "en"})
            self.assertEqual(result["preferences"]["ui_language_mode"], "system")
            self.assertEqual(result["preferences"]["report_language"], result["preferences"]["ui_language"])
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

    def test_refresh_financial_report_is_deterministic_and_does_not_download_or_create_provider(self) -> None:
        provider_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            company = Company(cik="0000000003", ticker="REF", name="Refresh Corp")
            run = ResearchRun(
                run_id="refresh-only", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
            )
            service = AppService(
                Path(directory),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            with patch.object(
                service, "_rebuild_financial_artifacts", return_value=("artifact-1",)
            ) as rebuild, patch.object(
                service, "get_report", return_value={"run_id": run.run_id}
            ) as get_report:
                result = service.refresh_financial_report(run.run_id, language="en")
            rebuild.assert_called_once()
            get_report.assert_called_once_with(run.run_id, language="en")
            self.assertEqual(result["financial_report_refresh"]["status"], "succeeded")
            self.assertEqual(provider_calls, [])

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

    def test_report_exposes_missing_financial_periods_and_zero_token_retry_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = build_company("09988.HK", "Alibaba", reporting_currency="CNY")
            run = ResearchRun(
                run_id="financial-status",
                company=company,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="test",
                model_id="test",
                data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
                research_configuration={"annual_history_years": 2},
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_filings([
                FilingDocument(
                    "doc-2026", company.security_id, "acc-2026", "ANNUAL_REPORT", "FY",
                    "2026-03-31", "2026-06-01", "2026.pdf", "https://example.test/2026.pdf",
                ),
                FilingDocument(
                    "doc-2024", company.security_id, "acc-2024", "ANNUAL_REPORT", "FY",
                    "2024-03-31", "2024-06-01", "2024.pdf", "https://example.test/2024.pdf",
                ),
            ])

            status = service.get_report(run.run_id, language="en")["financial_status"]

            self.assertEqual(status["state"], "incomplete")
            self.assertEqual(status["missing_periods"], ["2025"])
            self.assertTrue(status["retryable"])
            self.assertEqual(status["next_action"], "retry_missing_periods")
            self.assertEqual(status["model_calls"], 0)
            self.assertEqual(status["token_delta"], 0)

    def test_retry_financials_refreshes_only_unhealthy_nodes_without_provider(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        provider_calls: list[str] = []
        download_calls: list[str] = []

        def facts_for(filing: FilingDocument, value: float) -> tuple[FinancialFact, ...]:
            values = {"revenue": value, "net_income": value / 10, "operating_cash_flow": value / 8, "assets": value * 2, "liabilities": value, "equity": value}
            return tuple(FinancialFact(
                fact_id=f"{filing.accession_number}:{concept}", company_cik=company.security_id,
                concept=concept, reported_concept=concept, value=amount, unit="CNY",
                fiscal_year=int(filing.period_end[:4]), fiscal_period="FY", form_type=filing.form_type,
                start_date=f"{filing.period_end[:4]}-01-01", end_date=filing.period_end,
                filed_at=filing.filed_at, accession_number=filing.accession_number,
                source_url=filing.source_url,
                statement="income_statement" if concept in {"revenue", "net_income"} else "cash_flow" if concept == "operating_cash_flow" else "balance_sheet",
                currency="CNY", raw_text=f"{concept} {amount}", validation_status="VERIFIED",
            ) for concept, amount in values.items())

        with tempfile.TemporaryDirectory() as directory:
            good_path = Path(directory) / "good.pdf"
            good_path.write_bytes(b"official-good")
            good = FilingDocument("good-doc", company.security_id, "good-2024", "ANNUAL_REPORT", "FY", "2024-12-31", "2025-03-01", "2024.pdf", "https://example.test/good.pdf", local_path=str(good_path), content_hash="good-hash")
            unhealthy = FilingDocument("bad-doc", company.security_id, "bad-2025", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-01", "2025.pdf", "https://example.test/bad.pdf")

            class Adapter:
                def list_financial_filings(self, _company, *, limit=5):
                    return [unhealthy, good][:limit]
                def download_filing(self, filing, target):
                    download_calls.append(filing.accession_number)
                    target.mkdir(parents=True, exist_ok=True)
                    path = target / f"{filing.accession_number}.pdf"
                    path.write_bytes(b"official-repaired")
                    filing.local_path, filing.content_hash = str(path), f"hash-{filing.accession_number}"
                    return filing

            class Engine:
                def ingest(self, _company, filings, **_kwargs):
                    filing = filings[0]
                    facts = facts_for(filing, 200.0)
                    validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset(item.concept for item in facts), facts, ())
                    group = FinancialGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"), validation)
                    manifest = FilingManifest(filing.document_id, filing.accession_number, filing.source_url, filing.primary_document, filing.form_type, filing.fiscal_period, filing.period_end, filing.revision, filing.supersedes_document_id, filing.content_hash)
                    return FinancialDataset(facts, (), (manifest,), validation, (), (group,))

            service = AppService(Path(directory), market_data=_ResearchMarketData(Adapter()), financial_ingestion_engine=Engine(), provider_factory=lambda config: provider_calls.append(config.public_id))
            service.storage.save_company(company)
            service.storage.save_run(ResearchRun(run_id="financial-retry", company=company, workflow_id="test", research_pack_id="test", research_pack_version="1", provider_id="unused", model_id="unused", data_as_of=utc_now_iso(), status=RunStatus.PARTIAL))
            good_facts = facts_for(good, 100.0)
            good_validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset(item.concept for item in good_facts), good_facts, ())
            bad_fact = facts_for(unhealthy, 1.0)[0]
            bad_validation = FinancialValidation(ValidationStatus.REJECTED, ("core_coverage_insufficient",), frozenset({"revenue"}), (), (bad_fact,))
            service.storage.save_filings([good, unhealthy])
            service.storage.replace_financial_ingestion(company.security_id, [good.accession_number, unhealthy.accession_number], list(good_facts), [bad_fact], [FinancialGroupValidation((good.accession_number, good.period_end, "FY", "consolidated", "CNY"), good_validation), FinancialGroupValidation((unhealthy.accession_number, unhealthy.period_end, "FY", "consolidated", "CNY"), bad_validation)])

            result = service.retry_financials("financial-retry")
            self.assertEqual(result["run_id"], "financial-retry")
            self.assertEqual(download_calls, [unhealthy.accession_number])
            self.assertEqual(provider_calls, [])
            self.assertTrue(any(item["accession_number"] == unhealthy.accession_number for item in service.storage.get_facts(company.security_id)))
            self.assertEqual(result["financial_retry"]["targets"], [unhealthy.accession_number])
            second = service.retry_financials("financial-retry")
            self.assertEqual(second["financial_retry"]["targets"], [])
            self.assertEqual(second["financial_retry"]["status"], "succeeded")
            self.assertEqual(download_calls, [unhealthy.accession_number])
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                service.rebuild_financials("financial-retry")
            service.rebuild_financials("financial-retry", confirmed=True)
            self.assertEqual(
                set(download_calls[1:]),
                {unhealthy.accession_number, good.accession_number},
            )
            self.assertEqual(provider_calls, [])

    def test_retry_financials_keeps_success_when_another_node_fails(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        download_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            good = FilingDocument("good", company.security_id, "good-2025", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-01", "good.pdf", "https://example.test/good.pdf")
            bad = FilingDocument("bad", company.security_id, "bad-2024", "ANNUAL_REPORT", "FY", "2024-12-31", "2025-03-01", "bad.pdf", "https://example.test/bad.pdf")
            class Adapter:
                def list_financial_filings(self, _company, *, limit=5): return [good, bad][:limit]
                def download_filing(self, filing, target):
                    download_calls.append(filing.accession_number)
                    target.mkdir(parents=True, exist_ok=True)
                    path = target / f"{filing.accession_number}.pdf"
                    path.write_bytes(b"official")
                    filing.local_path, filing.content_hash = str(path), filing.accession_number
                    return filing
            class Engine:
                def ingest(self, _company, filings, **_kwargs):
                    filing = filings[0]
                    if filing.accession_number == bad.accession_number: raise OSError("bad filing")
                    fact = FinancialFact(f"{filing.accession_number}:revenue", company.security_id, "revenue", "Revenue", 10.0, "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", filing.period_end, filing.filed_at, filing.accession_number, filing.source_url, statement="income_statement", currency="CNY", raw_text="Revenue 10", validation_status="VERIFIED")
                    validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (fact,), ())
                    group = FinancialGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"), validation)
                    manifest = FilingManifest(filing.document_id, filing.accession_number, filing.source_url, filing.primary_document, filing.form_type, filing.fiscal_period, filing.period_end, filing.revision, filing.supersedes_document_id, filing.content_hash)
                    return FinancialDataset((fact,), (), (manifest,), validation, (), (group,))
            service = AppService(Path(directory), market_data=_ResearchMarketData(Adapter()), financial_ingestion_engine=Engine())
            service.storage.save_company(company)
            service.storage.save_run(ResearchRun(run_id="financial-retry-partial", company=company, workflow_id="test", research_pack_id="test", research_pack_version="1", provider_id="unused", model_id="unused", data_as_of=utc_now_iso(), status=RunStatus.PARTIAL))
            service.storage.save_filings([good, bad])
            result = service.retry_financials("financial-retry-partial")
            self.assertEqual(result["run_id"], "financial-retry-partial")
            self.assertEqual(result["financial_retry"]["status"], "partial")
            self.assertNotEqual(result["financial_retry"]["status"], "succeeded")
            self.assertTrue(result["financial_retry"]["accepted"])
            self.assertTrue(result["financial_retry"]["error"])
            self.assertEqual(set(download_calls), {good.accession_number, bad.accession_number})
            self.assertTrue(any(item["accession_number"] == good.accession_number for item in service.storage.get_facts_audit(company.security_id)))
            self.assertEqual(service.storage.get_financial_retry_state(company.security_id)["last_stage"], "filing-parse")

    def test_us_retry_is_local_and_idempotent_when_annual_window_is_complete(self) -> None:
        company = Company(
            cik="0000000002", ticker="CACHE", name="Cached Corp", exchange="NASDAQ",
            market="US", security_id="US:NASDAQ:CACHE", listing_currency="USD",
            reporting_currency="USD", accounting_standard="US_GAAP",
        )
        client_calls = {"factory": 0, "list": 0, "facts": 0, "download": 0}

        class Client:
            def list_annual_filings(self, _company, *, limit=5):
                client_calls["list"] += 1
                return []
            def get_company_facts(self, _company):
                client_calls["facts"] += 1
                return []

        def client_factory(*_args):
            client_calls["factory"] += 1
            return Client()

        core = {
            "revenue": "income_statement",
            "net_income": "income_statement",
            "operating_cash_flow": "cash_flow",
            "assets": "balance_sheet",
            "liabilities": "balance_sheet",
            "equity": "balance_sheet",
        }
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), sec_client_factory=client_factory)
            service.storage.save_company(company)
            service.storage.save_run(ResearchRun(
                run_id="us-cache-status", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
                research_configuration={"annual_history_years": 2},
                data_snapshot={"financial_fact_ids_sha256": "original-snapshot"},
            ))
            for year in (2025, 2024, 2023):
                path = Path(directory) / f"{year}.htm"
                path.write_text("cached annual filing", encoding="utf-8")
                accession = f"cached-{year}"
                filing = FilingDocument(
                    f"doc-{year}", company.cik, accession, "10-K", "FY",
                    f"{year}-12-31", f"{year + 1}-02-01", f"annual-{year}.htm",
                    f"https://www.sec.gov/Archives/{accession}/annual.htm",
                    local_path=str(path), content_hash=f"hash-{year}",
                )
                service.storage.save_filings([filing])
                facts = [FinancialFact(
                    fact_id=f"{accession}:{concept}", company_cik=company.cik,
                    concept=concept, reported_concept=concept, value=float(year),
                    unit="USD", fiscal_year=year, fiscal_period="FY", form_type="10-K",
                    start_date=f"{year}-01-01", end_date=f"{year}-12-31",
                    filed_at=filing.filed_at, accession_number=accession,
                    source_url=filing.source_url, scope="consolidated",
                    statement=statement, consolidated_scope="consolidated",
                    currency="USD", source_document=filing.primary_document,
                    raw_text=f"{concept} {year}", parser_version="sec-companyfacts-1",
                    validation_status="VERIFIED",
                ) for concept, statement in core.items()]
                service.storage.save_facts(facts)

            payload = {"research_configuration": {"annual_history_years": 2}}
            self.assertEqual(service._retry_us_financials(company, payload), [])
            self.assertEqual(service._retry_us_financials(company, payload), [])
            self.assertEqual(client_calls, {"factory": 0, "list": 0, "facts": 0, "download": 0})
            status = service.get_report("us-cache-status", language="en")["financial_status"]
            self.assertEqual(status["state"], "complete")
            self.assertEqual(status["available_periods"], ["2025", "2024", "2023"])
            self.assertTrue(status["snapshot_stale"])

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
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
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

    def test_python_service_has_no_model_connection_or_discovery_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            self.assertFalse(hasattr(service, "test_model_connection"))
            self.assertFalse(hasattr(service, "discover_models_for_session"))
            self.assertFalse(hasattr(service, "model_catalog"))
    def test_unknown_research_job_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(KeyError, "research job not found"):
                service.get_research_status("missing")

    def test_bootstrap_exposes_common_companies_and_ot_packs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            result = service.bootstrap()
            self.assertIn("company.search", result["capabilities"])
            self.assertIn("ot.compile", result["capabilities"])
            self.assertNotIn("models.discover", result["capabilities"])
            self.assertNotIn("model_catalog", result)
            self.assertEqual(result["common_companies"][0]["ticker"], "AAPL")
            self.assertTrue(result["research_packs"])
            self.assertTrue(all(item["content_hash"] for item in result["research_packs"]))
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

    def test_model_discovery_and_credentials_are_owned_by_rust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            self.assertFalse(hasattr(service, "discover_models_for_session"))
            self.assertNotIn("models.discover", service.hello()["capabilities"])
            self.assertNotIn("models.catalog", service.hello()["capabilities"])
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
                    "model": {},
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

    def test_installs_declarative_ot_from_protocol_payload(self) -> None:
        draft = minimal_studio_draft()
        draft["package"]["id"] = "service.pack"
        draft["package"]["name"] = "Service Pack"
        raw, compiled = compile_studio_draft(draft)
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            installed = service.install_research_pack(
                "service.ot", base64.b64encode(raw).decode("ascii")
            )
            self.assertEqual(installed["pack_id"], "service.pack")
            self.assertEqual(installed["content_hash"], compiled.content_identity)
            self.assertTrue(any(pack["pack_id"] == "service.pack" for pack in service.research_packs()))
    def test_legacy_raw_model_credentials_are_rejected_before_provider_creation(self) -> None:
        created = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), provider_factory=lambda config: created.append(config)
            )
            with self.assertRaisesRegex(ValueError, "legacy model configuration"):
                service.start_research({
                    "mode": "demo",
                    "model": {
                        "provider": "openai",
                        "model": "gpt-test",
                        "base_url": "https://api.example.test/v1",
                        "api_key": "must-not-enter-python",
                    },
                })
            self.assertEqual(created, [])
            self.assertNotIn("must-not-enter-python", str(service.__dict__))
    def test_model_comparison_requires_configured_unique_comparison_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            with self.assertRaisesRegex(ValueError, "comparison_models must contain between one and four models"):
                service.start_research({
                    "mode": "demo",
                    "model": {
                        "configured_model_id": "test.primary",
                        "configuration_version": 1,
                        "role": "primary",
                    },
                    "compare_enabled": True,
                    "comparison_models": [],
                })
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

    def test_sec_latest_wrong_scope_or_currency_is_not_researched(self) -> None:
        company = build_company("AAPL", "Apple")
        values = {
            "revenue": 100.0,
            "net_income": 20.0,
            "operating_cash_flow": 25.0,
            "assets": 200.0,
            "liabilities": 100.0,
            "equity": 100.0,
        }
        statements = {
            "revenue": "income_statement", "net_income": "income_statement",
            "operating_cash_flow": "cash_flow", "assets": "balance_sheet",
            "liabilities": "balance_sheet", "equity": "balance_sheet",
        }
        facts = [FinancialFact(
            f"wrong:{concept}", company.cik, concept, concept, value, "EUR", 2025,
            "FY", "10-K", "2025-01-01" if concept not in {"assets", "liabilities", "equity"} else None,
            "2025-12-31", "2026-02-01", "wrong", "https://www.sec.gov/Archives/wrong",
            scope="parent", statement=statements[concept], consolidated_scope="parent",
            currency="EUR", source_document="wrong.htm", raw_text=f"{concept} {value}",
            parser_version="sec-fixture",
        ) for concept, value in values.items()]
        self.assertIsNone(
            _latest_sec_verified_group(
                facts, FinancialIngestionEngine(), company=company,
                expected_period_end="2025-12-31",
            )
        )


class FinancialRetryContractTests(unittest.TestCase):
    def test_canonical_retry_snapshot_requires_matching_saved_hash(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        filing = FilingDocument(
            "complete-retry", company.security_id, "complete-retry", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "2025.pdf", "https://example.test/2025.pdf",
        )
        values = {
            "revenue": (100.0, "income_statement"),
            "net_income": (20.0, "income_statement"),
            "operating_cash_flow": (25.0, "cash_flow"),
            "assets": (200.0, "balance_sheet"),
            "liabilities": (80.0, "balance_sheet"),
            "equity": (120.0, "balance_sheet"),
        }
        facts = [FinancialFact(
            f"complete-retry:{concept}", company.security_id, concept, concept, value, "CNY", 2025,
            "FY", "ANNUAL_REPORT", "2025-01-01" if statement != "balance_sheet" else None,
            "2025-12-31", "2026-03-01", "complete-retry", filing.source_url,
            statement=statement, scope="consolidated", consolidated_scope="consolidated",
            currency="CNY", source_document="2025.pdf", source_page=1,
            raw_text=f"{concept} {value}", parser_version="fixture-parser",
            validation_status="VERIFIED",
        ) for concept, (value, statement) in values.items()]
        digest = _canonical_snapshot_digest(sorted(item.fact_id for item in facts))
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(company)
            service.storage.save_filings([filing])
            service.storage.save_facts(facts)
            for snapshot, expected_error in (
                ({}, "FINANCIAL_SNAPSHOT_STALE"),
                ({"financial_fact_ids_sha256": "wrong"}, "FINANCIAL_SNAPSHOT_STALE"),
            ):
                with self.assertRaisesRegex(ValueError, expected_error):
                    _canonical_retry_snapshot(service.storage, company, {"data_snapshot": snapshot})
            retry_facts = _canonical_retry_snapshot(
                service.storage, company,
                {"data_snapshot": {"financial_fact_ids_sha256": digest}},
            )
            self.assertEqual(
                {item.concept for item in retry_facts}, set(values)
            )

    def test_model_retry_rejects_incomplete_canonical_snapshot_before_provider(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        provider_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            filing = FilingDocument(
                "retry-incomplete", company.security_id, "retry-incomplete", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "2025.pdf", "https://example.test/2025.pdf",
            )
            fact = FinancialFact(
                "retry-incomplete:revenue", company.security_id, "revenue", "Revenue", 10.0,
                "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31",
                "2026-03-01", "retry-incomplete", filing.source_url,
                statement="income_statement", scope="consolidated", currency="CNY",
                raw_text="Revenue 10", validation_status="VERIFIED",
            )
            service.storage.save_company(company)
            service.storage.save_filings([filing])
            service.storage.save_facts([fact])
            service.storage.save_run(ResearchRun(
                run_id="retry-incomplete-run", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1", provider_id="unused",
                model_id="unused", data_as_of=utc_now_iso(), status=RunStatus.PARTIAL,
            ))
            model = {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"}
            for method in (service.retry_research_synthesis, service.retry_research_growth):
                with self.assertRaisesRegex(ValueError, "FINANCIAL_DATA_QUALITY_FAILED"):
                    method("retry-incomplete-run", model)
            self.assertEqual(provider_calls, [])

    def test_profile_aware_sec_cache_latest_and_status_paths(self) -> None:
        """Financial service gates follow the declared industry profile.

        The specialized profiles require four balance/income concepts and do
        not inherit the industrial revenue/operating-cash-flow gate.  Exercise
        the cache, latest-group, and user-facing status projections together so
        a service-side shortcut cannot silently reintroduce the old rule.
        """
        concepts = {
            "net_income": "income_statement",
            "assets": "balance_sheet",
            "liabilities": "balance_sheet",
            "total_equity": "balance_sheet",
        }
        for industry in ("banking", "insurance", "securities brokerage"):
            with self.subTest(industry=industry), tempfile.TemporaryDirectory() as directory:
                company = Company(
                    f"US:profile-{industry}", "PROFILE", "Profile issuer",
                    market="US", listing_currency="USD", reporting_currency="USD",
                    industry=industry,
                )
                path = Path(directory) / "annual.htm"
                path.write_text("official filing", encoding="utf-8")
                filing = FilingDocument(
                    f"doc-{industry}", company.cik, f"acc-{industry}", "10-K", "FY",
                    "2025-12-31", "2026-02-01", "annual.htm",
                    "https://www.sec.gov/Archives/profile/annual.htm",
                    local_path=str(path), content_hash="profile-hash",
                )
                facts = []
                for concept, statement in concepts.items():
                    facts.append(FinancialFact(
                        f"{filing.accession_number}:{concept}", company.cik, concept,
                        concept,
                        100.0 if concept == "net_income" else 2_000.0 if concept == "assets" else 1_000.0,
                        "USD", 2025, "FY", "10-K", "2025-01-01" if concept == "net_income" else None,
                        "2025-12-31", filing.filed_at, filing.accession_number,
                        filing.source_url, scope="consolidated", statement=statement,
                        consolidated_scope="consolidated", currency="USD",
                        source_document=filing.primary_document,
                        raw_text=f"{concept} 1000", parser_version="profile-fixture",
                    ))
                self.assertTrue(_cached_us_annual_window_is_complete(
                    company, [filing], [item.to_dict() for item in facts], required_count=1,
                ))
                latest = _latest_sec_verified_group(
                    facts, FinancialIngestionEngine(), company=company,
                    expected_period_end="2025-12-31",
                )
                self.assertIsNotNone(latest)

                groups = [
                    {
                        "period_end": period,
                        "status": "VERIFIED",
                        "consolidated_scope": "consolidated",
                        "currency": "USD",
                        "covered_concepts": list(concepts),
                    }
                    for period in ("2025-12-31", "2024-12-31", "2023-12-31")
                ]
                class StorageStub:
                    def get_filings(self, _key):
                        return [
                            FilingDocument(f"{industry}-{year}", company.cik, f"{industry}-{year}", "10-K", "FY", f"{year}-12-31", "", "annual.htm", filing.source_url)
                            for year in (2025, 2024, 2023)
                        ]
                    def get_facts(self, _key):
                        return []
                    def get_validation_groups(self, _key):
                        return groups
                    def get_financial_retry_state(self, _key):
                        return {"attempt_count": 0, "last_stage": "", "last_error": "", "updated_at": ""}
                status = _financial_status(
                    StorageStub(),
                    {**company.to_dict(), "company_type": industry},
                    {"research_configuration": {"annual_history_years": 2}},
                )
                self.assertEqual(status["state"], "complete")

    def test_financial_retry_job_keeps_status_and_cancel_rpc_responsive(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        entered = threading.Event()
        release = threading.Event()
        provider_calls: list[str] = []

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5):
                return [FilingDocument(
                    "slow-doc", company.security_id, "slow-2025", "ANNUAL_REPORT", "FY",
                    "2025-12-31", "2026-03-01", "slow.pdf", "https://example.test/slow.pdf",
                )][:limit]

            def download_filing(self, filing, target):
                entered.set()
                release.wait(2)
                target.mkdir(parents=True, exist_ok=True)
                path = target / "slow.pdf"
                path.write_bytes(b"official")
                filing.local_path = str(path)
                return filing

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter()),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            service.storage.save_company(company)
            service.storage.save_run(ResearchRun(
                run_id="financial-job", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.PARTIAL,
            ))

            started_at = time.monotonic()
            started = service.start_financial_retry("financial-job")
            self.assertLess(time.monotonic() - started_at, 0.2)
            self.assertTrue(entered.wait(1))
            live = service.get_research_status(started["job_id"])
            self.assertIn(live["state"], {"queued", "running"})
            cancelling = service.cancel_research(started["job_id"])
            self.assertEqual(cancelling["state"], "cancelling")
            release.set()
            deadline = time.monotonic() + 2
            terminal = cancelling
            while terminal["state"] not in {"cancelled", "failed", "completed"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                terminal = service.get_research_status(started["job_id"])
            self.assertEqual(terminal["state"], "cancelled")
            self.assertEqual(provider_calls, [])

    def test_financial_retry_result_is_explicit_and_serializable(self) -> None:
        result = FinancialRetryResult(
            mode="retry",
            targets=("fy-2025",),
            downloaded=("fy-2025",),
            accepted=("fy-2025:revenue",),
            rejected=("fy-2024:assets",),
            status="partial",
            error="FILING_DATA_QUALITY_FAILED",
            updated_artifacts=("deterministic-financial-summary", "research-report"),
        ).to_dict()
        self.assertEqual(result["mode"], "retry")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["updated_artifacts"], [
            "deterministic-financial-summary", "research-report"
        ])
        self.assertNotEqual(result["status"], "succeeded")


def _download_filing(document_id: str, host: str, *, url: str | None = None) -> FilingDocument:
    return FilingDocument(
        document_id=document_id,
        company_cik="fixture",
        accession_number=document_id,
        form_type="ANNUAL_REPORT",
        fiscal_period="FY",
        period_end="2025-12-31",
        filed_at="2026-03-01T00:00:00+00:00",
        primary_document=document_id,
        source_url=url or f"https://{host}/{document_id}.pdf",
    )


if __name__ == "__main__":
    unittest.main()
