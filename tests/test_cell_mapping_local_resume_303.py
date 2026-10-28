"""Local cell-repair diagnostics stay distinct from financial invariants."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from openthesis.domain import ResearchRun
from openthesis.financial_cell_repair import reread_cell_mappings
from openthesis.financial_contracts import CandidateBatch
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.storage import Storage
from openthesis.vision_financials import (
    GatewayVisionAdapter,
    VisionFallbackConfig,
    VisionPageRequest,
    VisionTaskCoordinator,
)

from test_original_cell_mapping_303 import source


def _config(**changes) -> VisionFallbackConfig:
    values = {
        "enabled": True,
        "consent": True,
        "provider": "configured_model",
        "configured_model_id": "cell-repair-fixture",
        "configuration_version": 3,
        "require_page_approval": True,
        "approve_upload": lambda _plan: True,
        "timeout_seconds": 60,
        "configured_timeout_seconds": 60,
    }
    values.update(changes)
    return VisionFallbackConfig(**values)


def _pending_action(store: Storage) -> dict:
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM recovery_actions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.parametrize("local_code", ["CELL_REPAIR_TIMEOUT", "CELL_REPAIR_LOCAL_UNAVAILABLE"])
def test_local_cell_repair_code_is_pending_and_resumes_without_second_provider_call(
    tmp_path, local_code
):
    company, filing, mapping = source(tmp_path)
    store = Storage(tmp_path / "data")
    store.save_company(company)
    store.save_run(ResearchRun(
        "mapping-run", company, "wf", "pack", "1", "", "", "2026-10-13"
    ))
    calls = []

    class Provider:
        def generate_vision_once(self, system, prompt, image, *, json_mode):
            calls.append((system, prompt, image, json_mode))
            return {"facts": [], "cell_mappings": [mapping]}

    config = _config()
    page = VisionPageRequest(1, Path(filing.local_path).read_bytes())
    gateway = GatewayVisionAdapter(Provider(), image_renderer=lambda _bytes: b"png")
    adapter = DurableCloudVisionAdapter(gateway, store, "mapping-run")

    with patch(
        "openthesis.financial_cell_repair.reread_cell_mappings",
        return_value=CandidateBatch(filing, diagnostics=(local_code,)),
    ):
        first = VisionTaskCoordinator(adapter, journal=store).extract(
            company, filing, (page,), config
        )

    assert first.error_code == local_code
    assert len(calls) == 1
    action = _pending_action(store)
    validation = json.loads(action["validation_json"] or "{}")
    assert validation.get("pending") is True
    assert validation.get("passed") is False
    assert action["result_json"]
    assert "CLOUD_CANDIDATE_FINANCIAL_INVARIANT_FAILED" not in str(
        action["diagnostic_code"]
    )

    restarted = Storage(store.data_dir)
    resumed = DurableCloudVisionAdapter(
        GatewayVisionAdapter(Provider(), image_renderer=lambda _bytes: b"png"),
        restarted,
        "mapping-run",
    )
    with patch("openthesis.financial_cell_repair.reread_cell_mappings", wraps=reread_cell_mappings):
        second = VisionTaskCoordinator(adapter=resumed, journal=restarted).extract(
            company, filing, (page,), replace(config, timeout_seconds=45)
        )

    assert second.error_code is None, second.diagnostics
    assert second.facts and second.facts[0].value == 100000
    assert len(calls) == 1
    assert not restarted.get_facts(company.security_id)

