"""A semantic hint reaches research through the actual financial pipeline."""
from pathlib import Path

from openthesis.domain import ResearchRun
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.financial_contracts import CompilationPolicy, GapStageKind
from openthesis.financial_ingestion import FinancialIngestionEngine
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from openthesis.storage import Storage
from openthesis.vision_financials import GatewayVisionAdapter, VisionFallbackConfig, VisionTaskCoordinator
from test_original_cell_mapping_303 import source


def test_native_unknown_label_uses_one_authorized_mapping_and_reaches_admission(tmp_path):
    company, filing, mapping = source(tmp_path)
    store = Storage(tmp_path / "store")
    store.save_company(company)
    store.save_run(ResearchRun("pipeline-run", company, "wf", "pack", "1", "", "", "2026-10-13"))
    calls, approvals = [], []

    class Provider:
        def generate_vision_once(self, _system, _prompt, _image, *, json_mode):
            calls.append(json_mode)
            return {"facts": [], "cell_mappings": [mapping]}

    def approve(plan):
        approvals.append(plan)
        return True

    config = VisionFallbackConfig(enabled=True, consent=True, configured_model_id="fixture",
        timeout_seconds=60, require_page_approval=True, approve_upload=approve)
    gateway = GatewayVisionAdapter(Provider(), image_renderer=lambda _bytes: b"no-upload-fixture-image")
    adapter = VisionTaskCoordinator(DurableCloudVisionAdapter(gateway, store, "pipeline-run"), journal=store)
    engine = FinancialIngestionEngine(max_workers=1, parse_timeout_seconds=15, batch_timeout_seconds=60)
    view = FinancialPipeline(engine).prepare(FinancialResearchSnapshot(company, (filing,),
        vision_config=config, vision_fallback=adapter,
        authorized_source_stages=frozenset({GapStageKind.MINERU})),
        CompilationPolicy(required_concepts=frozenset({"revenue"}), reporting_currency="CNY"))
    assert calls == [True], view.diagnostics
    assert approvals and all(tuple(plan["pages"]) == (1,) for plan in approvals)
    rows = [fact for fact in view.facts if fact.concept == "revenue" and fact.usage_status != "comparator"]
    assert len(rows) == 1 and rows[0].value == 100000, "\n".join(
        issue for group in view.canonical_dataset.validations for issue in group.issues)
    assert rows[0].normalization_lineage.raw_value == "100"
    assert any(item.evidence_id.startswith("cell-reread:") for item in view.profile.source_evidence)
    assert view.profile.metrics[0]["revenue"] == 100000
    assert Path(filing.local_path).exists()
    assert not store.get_facts(company.security_id), "only OfficialFinancialPreparer owns final facts publication"
