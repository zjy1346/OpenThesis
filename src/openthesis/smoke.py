from __future__ import annotations

import tempfile
from pathlib import Path

from .demo import DEMO_COMPANY, demo_facts
from .domain import FinancialFact, RunStatus
from .packs import builtin_pack
from .providers import ModelConfig
from .research import ResearchWorkflow
from .storage import Storage


def run_smoke_test() -> None:
    """Exercise the frozen application's deterministic vertical slice.

    The function intentionally raises on failure so the packaged executable
    returns a non-zero exit code to build and release automation.
    """

    with tempfile.TemporaryDirectory(prefix="openthesis-smoke-") as directory:
        storage = Storage(Path(directory))
        storage.save_company(DEMO_COMPANY)
        facts = demo_facts()
        storage.save_facts([FinancialFact(**item) for item in facts])
        config = ModelConfig()
        workflow = ResearchWorkflow(storage, builtin_pack(), None, config)
        run = workflow.run(DEMO_COMPANY, facts)
        if run.status != RunStatus.PARTIAL:
            raise RuntimeError(f"Unexpected smoke-test status: {run.status}")
        artifacts = storage.get_artifacts(run.run_id)
        if len(artifacts) != 2:
            raise RuntimeError(f"Expected 2 smoke-test artifacts, got {len(artifacts)}")

