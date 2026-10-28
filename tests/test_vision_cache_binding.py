from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile

import pytest

from openthesis.financial_cloud_proof import CloudUnitContext
from openthesis.service import AppService, _vision_policy_snapshot
from openthesis.vision_financials import (
    MineruFlashAdapter,
    VisionFallbackConfig,
    VisionHttpResponse,
    VisionTaskCoordinator,
)
from test_vision_financials import (
    BoundedVisionAdapter,
    MemoryVisionJournal,
    QueueVisionTransport,
    _company,
    _config,
    _explicit_financial_markdown,
    _filing,
    _page,
)


class InvocationBoundedVisionAdapter(BoundedVisionAdapter):
    def extract(self, company, filing, pages, config, *, cancel_check=None):
        result = super().extract(
            company, filing, pages, config, cancel_check=cancel_check
        )
        return replace(
            result,
            diagnostics=(*result.diagnostics, f"ADAPTER_CALL_{len(self.calls)}"),
        )


@pytest.mark.parametrize("changed_identity", ("unit_context", "period", "configuration"))
def test_batch_candidate_is_not_reused_when_unit_period_or_config_changes(changed_identity):
    company, filing, page, config = _company(), _filing(), _page(), _config()
    journal = MemoryVisionJournal()
    adapter = InvocationBoundedVisionAdapter()
    coordinator = VisionTaskCoordinator(adapter, journal=journal)

    first = coordinator.extract(company, filing, (page,), config)
    assert first.error_code is None
    assert "ADAPTER_CALL_1" in first.diagnostics

    changed_filing, changed_page, changed_config = filing, page, config
    if changed_identity == "unit_context":
        changed_page = replace(
            page,
            unit_contexts=(CloudUnitContext(
                page.original_page, "Unit: USD millions", "local-unit-context-hash"
            ),),
        )
    elif changed_identity == "period":
        changed_filing = replace(
            filing, form_type="QUARTERLY_REPORT", fiscal_period="Q4"
        )
    else:
        changed_config = replace(config, configuration_version=config.configuration_version + 1)

    second = coordinator.extract(
        company, changed_filing, (changed_page,), changed_config
    )

    assert second.error_code is None
    assert "ADAPTER_CALL_2" in second.diagnostics
    assert adapter.calls == [page.original_page, page.original_page]


def test_same_candidate_reuses_persistent_cache_across_ingestion_metadata_changes_without_http():
    journal = MemoryVisionJournal()
    company, filing, page = _company(), _filing(), _page()
    config = _config(provider="mineru_flash", configured_model_id="")
    first_transport = QueueVisionTransport(
        VisionHttpResponse(200, json.dumps({"code": 0, "data": {
            "task_id": "cache-binding-task", "file_url": "https://upload.example/page",
        }}).encode()),
        VisionHttpResponse(200),
        VisionHttpResponse(200, json.dumps({"state": "done", "data": {
            "markdown_url": "https://download.example/result.md",
        }}).encode()),
        VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
    )
    first = VisionTaskCoordinator(
        MineruFlashAdapter(first_transport, sleep=lambda _: None, journal=journal),
        journal=journal,
    ).extract(company, filing, (page,), config)

    assert first.error_code is None
    assert [call["method"] for call in first_transport.calls] == ["POST", "PUT", "GET", "GET"]

    metadata_only_filing = replace(
        filing,
        local_path="D:/offline-cache/reingested-report.pdf",
        ingested_at="2026-10-04T12:00:00Z",
    )
    second_transport = QueueVisionTransport()
    second = VisionTaskCoordinator(
        MineruFlashAdapter(second_transport, sleep=lambda _: None, journal=journal),
        journal=journal,
    ).extract(company, metadata_only_filing, (page,), config)

    assert second.error_code is None
    assert "VISION_RESULT_CACHE_HIT" in second.diagnostics
    assert second_transport.calls == []


def test_flash_preference_snapshot_configuration_version_reaches_config():
    workspace = Path(__file__).resolve().parents[1]
    temp_root = workspace / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    observed_configs: list[VisionFallbackConfig] = []

    with tempfile.TemporaryDirectory(dir=temp_root) as directory:
        service = AppService(
            Path(directory),
            vision_adapter_factory=lambda config: observed_configs.append(config)
            or BoundedVisionAdapter(),
        )
        service.update_preferences({
            "vision_fallback_policy": "",
            "vision_enabled": True,
            "vision_provider": "mineru_flash",
            "vision_configuration_version": "23",
            "vision_standing_authorization": True,
            "vision_authorized_provider": "mineru_flash",
            "vision_authorized_scope": "failed_financial_statement_pages",
            "vision_authorized_policy_version": "1",
            "vision_authorized_at": "2026-10-04T00:00:00Z",
        })

        snapshot = _vision_policy_snapshot(service.preferences())
        _sources, coordinator, config = service._financial_recognition_capabilities(
            _company(), (_filing(),), run_id="preference-snapshot-test",
        )

        assert snapshot["configuration_version"] == 23
        assert config is not None
        assert config.provider == "mineru_flash"
        assert config.configuration_version == snapshot["configuration_version"]
        assert isinstance(coordinator, VisionTaskCoordinator)
        assert observed_configs == [config]
