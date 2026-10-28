"""Offline service-to-durable-cloud composition contracts."""
from __future__ import annotations

import json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from openthesis.domain import ResearchRun, RunStatus
from openthesis.market_data import MarketDataError
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.service import AppService, _ResearchJob
from openthesis.vision_financials import (
    MineruFlashAdapter,
    VisionFallbackConfig,
    VisionHttpResponse,
    VisionTaskCoordinator,
    VisionUploadPlan,
)
from test_vision_financials import (
    QueueVisionTransport,
    _company,
    _explicit_financial_markdown,
    _filing,
    _page,
)
from test_official_financial_preparation import setup_preparer


def test_first_start_and_public_repair_capture_the_same_cloud_policy(tmp_path, monkeypatch):
    company, _filing, source, _preparer, storage = setup_preparer(tmp_path)
    def unavailable(*_args, **_kwargs):
        raise MarketDataError("source permission denied", code="FILING_ACCESS_DENIED")
    source.list_annual_filings = unavailable
    network = QueueVisionTransport()
    configs, captures, providers = [], [], []
    def vision_factory(config):
        configs.append(config)
        return MineruFlashAdapter(network)
    service = AppService(storage.data_dir, sec_client_factory=lambda *_args: source,
        vision_adapter_factory=vision_factory, provider_factory=lambda config: providers.append(config))
    service.update_preferences({"sec_contact_email": "offline@example.test", "vision_fallback_policy": _policy()})
    capture = service._financial_recognition_capabilities
    def observed(*args, **kwargs):
        captures.append(kwargs.get("run_id") or kwargs["job"].run_id)
        return capture(*args, **kwargs)
    monkeypatch.setattr(service, "_financial_recognition_capabilities", observed)
    first = service.start_research({"mode": "company", "company": company.to_dict(),
        "evidence_policy": {"annual_history_years": 3}})
    def finished(started):
        deadline = time.monotonic() + 6
        status = started
        while not status["attempt_complete"]:
            assert time.monotonic() < deadline
            time.sleep(.01)
            status = service.get_research_status(started["job_id"])
        assert status["error_code"] == "FILING_ACCESS_DENIED"
        return status
    first = finished(first)
    # The injected official directory resolved the requested company before
    # the filing failure. The financial retry reuses the durable run and must
    # not repeat the identity search or activate a cloud/model fallback.
    assert source.identity_search_calls == 1
    saved = json.loads(service.storage.get_run(first["run_id"])["payload_json"])
    assert saved["research_configuration"]["annual_history_years"] == 3
    assert captures == [first["run_id"]]
    second = finished(service.start_financial_retry(first["run_id"]))
    assert source.identity_search_calls == 1
    assert captures == [first["run_id"], first["run_id"]]
    assert second["recovery_outcome"]["state"] == "needs_action"
    assert len(configs) == 2
    for key in ("provider", "approval_mode", "configuration_version", "max_pages", "max_bytes"):
        assert getattr(configs[0], key) == getattr(configs[1], key)
    assert providers == [] and network.calls == []


def _policy(*, enabled: bool = True, provider: str = "mineru_flash") -> str:
    value = {
        "schema_version": 1,
        "policy_version": 1,
        "scope_version": 1,
        "provider_terms_version": 1,
        "enabled": enabled,
        "provider": provider,
        "approval_mode": "approve_current_research",
        "standing_authorization": enabled,
        "authorization_scope": "financial_failed_pages",
        "authorized_at": "2026-10-04T00:00:00Z" if enabled else "",
    }
    return json.dumps(value)


class ServiceCloudCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory(dir=workspace / "tmp")
        self.company = _company()
        self.filing = _filing()
        self.page = _page()
        self.transport = QueueVisionTransport()
        self.factory_configs = []

        def factory(config):
            self.factory_configs.append(config)
            return MineruFlashAdapter(self.transport)

        self.service = AppService(Path(self.temp.name), vision_adapter_factory=factory)
        self.service.storage.save_company(self.company)
        self.service.storage.save_run(ResearchRun(
            run_id="vision-composition-run",
            company=self.company,
            workflow_id="test",
            research_pack_id="offline-pack",
            research_pack_version="1",
            provider_id="unused",
            model_id="unused",
            data_as_of="2026-10-04",
            status=RunStatus.CREATED,
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def enable_flash_policy(self) -> None:
        self.service.update_preferences({"vision_fallback_policy": _policy()})

    def recognition(self, run_id: str = "vision-composition-run"):
        return self.service._financial_recognition_capabilities(
            self.company, (self.filing,), run_id=run_id,
        )

    def test_atomic_flash_policy_builds_durable_page_stack_with_same_storage_and_run(self):
        self.enable_flash_policy()
        with patch.object(
            self.service,
            "_vision_adapter_for_run",
            wraps=self.service._vision_adapter_for_run,
        ) as wire:
            _sources, coordinator, config = self.recognition()

        self.assertIsInstance(coordinator, VisionTaskCoordinator)
        self.assertEqual(config.provider, "mineru_flash")
        wrapper = coordinator.adapter
        self.assertIsInstance(wrapper, DurableCloudVisionAdapter)
        self.assertIsInstance(wrapper.adapter, MineruFlashAdapter)
        self.assertIs(wrapper.store, self.service.storage)
        self.assertIs(wrapper.adapter.journal, self.service.storage)
        self.assertIs(coordinator.journal, self.service.storage)
        self.assertEqual(wrapper.run_id, "vision-composition-run")
        self.assertIsNotNone(wrapper.budget_scope)
        self.assertTrue(wrapper.automatic_budget_scope)
        wire.assert_called_once()

    def test_service_composition_compiles_and_publishes_candidate_without_writing_research_facts(self):
        self.enable_flash_policy()
        self.transport.responses.extend((
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {
                "task_id": "service-task", "file_url": "https://upload.example/page",
            }}).encode()),
            VisionHttpResponse(200),
            VisionHttpResponse(200, json.dumps({"state": "done", "data": {
                "markdown_url": "https://download.example/result.md",
            }}).encode()),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        ))
        _sources, coordinator, config = self.recognition()

        result = coordinator.extract(self.company, self.filing, (self.page,), config)

        self.assertIsNone(result.error_code)
        self.assertEqual([call["method"] for call in self.transport.calls], ["POST", "PUT", "GET", "GET"])
        self.assertEqual([fact.concept for fact in result.facts], ["revenue"])
        compiled = FinancialFactCompiler().compile_facts(
            self.company,
            (self.filing,),
            result.facts,
            required_concepts=frozenset({"revenue"}),
            evidence=result.evidence,
        )
        self.assertIn("revenue", {fact.concept for fact in compiled.accepted_facts})

        candidate_sets = [
            item for item in self.service.storage.get_artifacts("vision-composition-run")
            if item["artifact_type"] == "vision-candidate-set"
        ]
        self.assertEqual(len(candidate_sets), 1)
        self.assertTrue(candidate_sets[0]["content"]["result"]["facts"])
        self.assertEqual(self.service.storage.get_facts(self.company.security_id), [])

    def test_revoked_policy_blocks_a_previously_constructed_wrapper_before_http(self):
        self.enable_flash_policy()
        _sources, coordinator, config = self.recognition()
        self.service.update_preferences({"vision_fallback_policy": _policy(enabled=False)})

        result = coordinator.adapter.extract(
            self.company, self.filing, (self.page,), config,
        )

        self.assertEqual(result.error_code, "RECOVERY_CANCELLED")
        self.assertEqual(self.transport.calls, [])

    def test_configured_model_fake_adapter_is_preserved_without_provider_switch(self):
        fake_adapter = object()
        observed = []
        self.service._vision_adapter_factory = lambda config: observed.append(config) or fake_adapter
        config = VisionFallbackConfig(
            enabled=True,
            consent=True,
            provider="configured_model",
            configured_model_id="configured.fake",
        )

        adapter = self.service._vision_adapter_for_run(config, "model-run", {})

        self.assertIs(adapter, fake_adapter)
        self.assertEqual(observed[0].provider, "configured_model")
        self.assertEqual(observed[0].configured_model_id, "configured.fake")

    def test_default_disabled_policy_does_not_construct_any_vision_adapter(self):
        _sources, adapter, config = self.recognition()
        self.assertIsNone(adapter)
        self.assertFalse(config.enabled)
        self.assertEqual(self.factory_configs, [])

    def test_live_job_and_saved_run_keep_distinct_cloud_recovery_identities(self):
        self.enable_flash_policy()
        self.service.storage.save_run(ResearchRun(
            run_id="live-research-run",
            company=self.company,
            workflow_id="test",
            research_pack_id="offline-pack",
            research_pack_version="1",
            provider_id="unused",
            model_id="unused",
            data_as_of="2026-10-04",
            status=RunStatus.RUNNING,
        ))
        self.service.storage.save_run(ResearchRun(
            run_id="saved-research-run",
            company=self.company,
            workflow_id="test",
            research_pack_id="offline-pack",
            research_pack_version="1",
            provider_id="unused",
            model_id="unused",
            data_as_of="2026-10-04",
            status=RunStatus.CREATED,
        ))
        live_job = _ResearchJob(job_id="live-job", run_id="live-research-run", state="running")

        _sources, live_coordinator, live_config = self.service._financial_recognition_capabilities(
            self.company, (self.filing,), job=live_job,
        )
        _sources, saved_coordinator, saved_config = self.service._financial_recognition_capabilities(
            self.company, (self.filing,), run_id="saved-research-run",
        )

        live_wrapper = live_coordinator.adapter
        saved_wrapper = saved_coordinator.adapter
        self.assertIsInstance(live_wrapper, DurableCloudVisionAdapter)
        self.assertIsInstance(saved_wrapper, DurableCloudVisionAdapter)
        self.assertEqual(live_wrapper.run_id, "live-research-run")
        self.assertEqual(saved_wrapper.run_id, "saved-research-run")
        self.assertEqual(
            (live_config.provider, live_config.enabled, live_config.consent,
             live_config.approval_mode, live_config.configuration_version),
            (saved_config.provider, saved_config.enabled, saved_config.consent,
             saved_config.approval_mode, saved_config.configuration_version),
        )
        self.assertTrue(live_wrapper.authorization_check())
        self.assertTrue(saved_wrapper.authorization_check())
        self.assertEqual(self.transport.calls, [])

    def test_revoked_policy_also_rejects_approval_for_an_injected_adapter(self):
        self.enable_flash_policy()
        self.service._vision_adapter_factory = lambda _config: object()
        _sources, _coordinator, config = self.recognition()
        plan = VisionUploadPlan(provider=config.provider, filing_hash=self.filing.content_hash,
            source_document=self.filing.primary_document, pages=(self.page,))
        self.service.update_preferences({"vision_fallback_policy": _policy(enabled=False)})
        self.assertFalse(config.approve_upload(plan.safe_summary()))
        self.assertEqual(self.transport.calls, [])

    def test_concurrent_plans_share_a_run_cap_and_terminal_progress_cannot_reopen_it(self):
        self.enable_flash_policy()
        job = _ResearchJob(job_id="progress-job", run_id="vision-composition-run", state="running")
        _sources, coordinator, config = self.service._financial_recognition_capabilities(
            self.company, (self.filing,), job=job)
        plans = tuple(VisionUploadPlan(provider=config.provider, filing_hash=self.filing.content_hash,
            source_document=self.filing.primary_document,
            pages=tuple(replace(self.page, original_page=number) for number in range(start, start + 12)))
            for start in (1, 13))
        with ThreadPoolExecutor(max_workers=2) as pool:
            approved = tuple(pool.map(lambda plan: config.approve_upload(plan.safe_summary()), plans))
        self.assertEqual(sorted(approved), [False, True])
        self.assertEqual(job.stage, "vision-processing")
        self.assertEqual((job.stage_current, job.stage_total), (0, 12))
        self.service._update_job(job, state="completed", stage="completed", percent=100)
        before = self.service.storage.get_research_job(job.job_id)
        coordinator.progress(12, 12)
        self.assertEqual(self.service.storage.get_research_job(job.job_id), before)
        self.assertEqual((job.state, job.stage), ("completed", "completed"))
        self.assertFalse(config.approve_upload(plans[0].safe_summary()))
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
