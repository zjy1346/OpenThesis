"""JSON storage identity must survive a tuple/list container round trip."""
import pytest

from openthesis.domain import Company, ResearchArtifact, ResearchRun
from openthesis.storage import Storage


def test_atomic_artifact_publication_is_idempotent_for_the_same_canonical_json(tmp_path):
    store = Storage(tmp_path / "data")
    company = Company("issuer", "TEST", "Test issuer")
    store.save_company(company)
    run = ResearchRun("run", company, "wf", "pack", "1", "unused", "unused", "2026-10-06")
    artifact = ResearchArtifact("immutable", "run", "deterministic-financial-summary", "Summary",
        {"nested": {"facts": ("a", "b")}})
    store.save_run_with_artifacts(run, [artifact])
    before = store.get_artifacts("run")
    store.save_run_with_artifacts(run, [artifact])
    assert store.get_artifacts("run") == before
    different = ResearchArtifact("immutable", "run", "deterministic-financial-summary", "Summary",
        {"nested": {"facts": ("a", "changed")}})
    with pytest.raises(RuntimeError, match="ARTIFACT_IDENTITY_CONFLICT"):
        store.save_run_with_artifacts(run, [different])
    assert store.get_artifacts("run") == before
