"""Semantic hints cross the real original reader, not a model-value patch."""
from dataclasses import replace
from hashlib import sha256
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from openthesis.domain import Company, FilingDocument
from openthesis.financial_cell_repair import mapping_reference, reread_cell_mappings
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import CompilationPolicy
from openthesis.financial_ingestion import FinancialIngestionEngine, _manifest_for
from openthesis.vision_financials import VisionPageRequest, parse_vision_json


def source(tmp_path, *, repeated=False, unit="Unit: RMB thousand", title="Consolidated Income Statement", label="Trading receipts"):
    path = tmp_path / "original.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    entries = [(50, 760, title), (50, 738, "Year ended December 31, 2025"),
               (50, 715, unit), (50, 690, "Item"), (350, 690, "2025"), (460, 690, "2024"),
               (50, 660, label), (350, 660, "100"), (460, 660, "90")]
    if repeated:
        entries.extend([(50, 630, label), (350, 630, "100"), (460, 630, "90")])
    stream = DecodedStreamObject()
    stream.set_data("\n".join(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET" for x, y, text in entries).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as target:
        writer.write(target)
    company = Company("HK:HKEX:00001.HK", "00001.HK", "Fixture", market="HK", reporting_currency="CNY")
    filing = FilingDocument("original-doc", company.security_id, "original-accession", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-31", "2025 Annual Report", "https://example.invalid/original.pdf",
        local_path=str(path), content_hash=sha256(path.read_bytes()).hexdigest())
    mapping = {"concept": "revenue", "label": label, "row_quote": f"{label} 100 90", "original_page": 1}
    return company, filing, mapping


def repaired(company, filing, mapping):
    ref = mapping_reference(mapping, filing, {1})
    assert ref
    return reread_cell_mappings(company, filing, (ref,), deadline=time.monotonic() + 15)


def test_unknown_label_reread_uses_original_column_unit_and_amount_then_compiles(tmp_path):
    company, filing, mapping = source(tmp_path)
    original_hash = filing.content_hash
    old, _ = FinancialIngestionEngine()._parse_pdf_ast(filing.local_path, company, filing,
        _manifest_for(filing), candidate_pages=frozenset({1}), index_precomputed=True)
    assert not any(fact.concept == "revenue" for fact in old), "the original unknown-label loss must be reproduced"
    batch = repaired(company, filing, mapping)
    assert len(batch.candidates) == 1, batch.diagnostics
    fact = batch.candidates[0].fact
    assert fact.normalization_lineage.raw_value == "100"
    assert fact.normalization_lineage.normalized_value == "100000"
    assert fact.end_date == "2025-12-31" and fact.source_column == "2025"
    dataset = FinancialFactCompiler().compile(company, (batch,), CompilationPolicy(
        required_concepts=frozenset({"revenue"}), reporting_currency="CNY"))
    assert dataset.allow_ai, dataset.diagnostics
    assert dataset.research_facts[0].value == 100000
    assert sha256(Path(filing.local_path).read_bytes()).hexdigest() == original_hash
    # The alias was not installed into any later unrelated native parse.
    again, _ = FinancialIngestionEngine()._parse_pdf_ast(filing.local_path, company, filing,
        _manifest_for(filing), candidate_pages=frozenset({1}), index_precomputed=True)
    assert not any(fact.concept == "revenue" for fact in again)


@pytest.mark.parametrize("options,code", [({"repeated": True}, "CELL_REPAIR_ROW_NOT_UNIQUE"),
    ({"label": "Total assets"}, "CELL_REPAIR_KNOWN_LABEL_CONTRADICTION")])
def test_ambiguous_or_known_contradictory_rows_do_not_gain_admission(tmp_path, options, code):
    company, filing, mapping = source(tmp_path, **options)
    batch = repaired(company, filing, mapping)
    assert not batch.candidates
    assert code in batch.diagnostics


def test_parent_source_is_preserved_but_cannot_satisfy_a_consolidated_request(tmp_path):
    company, filing, mapping = source(tmp_path, title="Parent Company Income Statement")
    batch = repaired(company, filing, mapping)
    assert batch.candidates and all(item.fact.consolidated_scope == "parent" for item in batch.candidates)
    dataset = FinancialFactCompiler().compile(company, (batch,), CompilationPolicy(
        required_concepts=frozenset({"revenue"}), reporting_currency="CNY", scope="consolidated"))
    assert not dataset.allow_ai and not dataset.research_facts


def test_invalid_numeric_override_and_wrong_page_are_not_mapping_contracts(tmp_path):
    company, filing, mapping = source(tmp_path)
    assert mapping_reference({**mapping, "value": "1000000"}, filing, {1}) is None
    assert mapping_reference({**mapping, "original_page": 2}, filing, {1}) is None
    page = VisionPageRequest(1, b"not-uploaded")
    result = parse_vision_json({"facts": [], "cell_mappings": [mapping]}, company, filing, (page,))
    assert not result.facts
    assert any(ref.evidence_id.startswith("cell-map:") for ref in result.evidence)


def test_changed_original_or_expired_budget_is_not_read_as_trusted(tmp_path):
    company, filing, mapping = source(tmp_path)
    changed = replace(filing, content_hash="a" * 64)
    assert "CELL_REPAIR_SOURCE_CHANGED" in repaired(company, changed, mapping).diagnostics
    ref = mapping_reference(mapping, filing, {1})
    batch = reread_cell_mappings(company, filing, (ref,), deadline=time.monotonic() - 1)
    assert not batch.candidates and batch.diagnostics == ("CELL_REPAIR_TIMEOUT",)


def test_cached_mapping_amount_or_proof_tampering_cannot_skip_compiler(tmp_path):
    company, filing, mapping = source(tmp_path)
    batch = repaired(company, filing, mapping)
    assert batch.candidates
    candidate = batch.candidates[0]
    for fact in (replace(candidate.fact, value=999), replace(candidate.fact, concept="assets")):
        wrong = replace(batch, candidates=(replace(candidate, fact=fact),))
        result = FinancialFactCompiler().compile(company, (wrong,), CompilationPolicy(
            required_concepts=frozenset({"revenue"}), reporting_currency="CNY"))
        assert result.quarantined_facts and not result.research_facts
    wrong_proofs = tuple(replace(ref, excerpt=ref.excerpt.replace('"revenue"', '"assets"'))
        if ref.evidence_id.startswith("cell-reread:") else ref for ref in batch.evidence)
    result = FinancialFactCompiler().compile(company, (replace(batch, evidence=wrong_proofs),), CompilationPolicy(
        required_concepts=frozenset({"revenue"}), reporting_currency="CNY"))
    assert result.quarantined_facts and not result.research_facts


def test_actual_gateway_coordinator_durable_reread_and_restart_reuse_no_second_wire(tmp_path):
    from openthesis.domain import ResearchRun
    from openthesis.storage import Storage
    from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
    from openthesis.vision_financials import GatewayVisionAdapter, VisionTaskCoordinator, VisionFallbackConfig
    company, filing, mapping = source(tmp_path)
    store = Storage(tmp_path / "data")
    store.save_company(company)
    store.save_run(ResearchRun("mapping-run", company, "wf", "pack", "1", "", "", "2026-10-13"))
    calls = []

    class Provider:
        def generate_vision_once(self, system, prompt, image, *, json_mode):
            calls.append((json_mode, prompt))
            return {"facts": [], "cell_mappings": [mapping]}

    config = VisionFallbackConfig(enabled=True, consent=True, configured_model_id="fixture",
        timeout_seconds=60, configured_timeout_seconds=60, require_page_approval=True, approve_upload=lambda _plan: True)
    page = VisionPageRequest(1, Path(filing.local_path).read_bytes())
    gateway = GatewayVisionAdapter(Provider(), image_renderer=lambda _bytes: b"local-image-no-upload")
    adapter = DurableCloudVisionAdapter(gateway, store, "mapping-run")
    result = VisionTaskCoordinator(adapter, journal=store).extract(company, filing, (page,), config)
    assert result.error_code is None, result.diagnostics
    assert result.facts and result.facts[0].value == 100000
    assert len(calls) == 1
    # A different remaining task allowance is not a new model configuration.
    restarted = Storage(store.data_dir)
    recovered = DurableCloudVisionAdapter(gateway, restarted, "mapping-run").extract(
        company, filing, (page,), replace(config, timeout_seconds=45))
    assert recovered.error_code is None, recovered.diagnostics
    assert recovered.facts[0].value == 100000 and len(calls) == 1
    assert not restarted.get_facts(company.security_id), "cloud candidate is not an independent facts publication"
