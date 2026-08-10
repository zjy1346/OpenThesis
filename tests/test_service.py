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
from openthesis.model_catalog import ModelDiscoveryError
from openthesis.markets import build_company
from openthesis.market_data import MarketDataError
from openthesis.service import AppService, PreferenceValidationError, _market_snapshot


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
        with tempfile.TemporaryDirectory() as directory, patch(
            "openthesis.service.ingest_official_pdf",
            return_value=([bad_fact], [], []),
        ):
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(BadFilingAdapter()),
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


if __name__ == "__main__":
    unittest.main()
