import ast
from copy import deepcopy
from pathlib import Path

from openthesis.financial_evidence import build_fact_evidence
from openthesis.research import build_fact_evidence as research_build_fact_evidence


def test_build_fact_evidence_preserves_all_existing_fields_and_values():
    facts = [{
        "fact_id": "fact-1",
        "company_cik": "0000123456",
        "entity": "Example Corp",
        "market": "US",
        "concept": "revenue",
        "value": "123.45",
        "unit": "USD",
        "unit_scale": 1000.0,
        "unit_provenance": "source_header",
        "scope": "consolidated",
        "consolidated_scope": "consolidated",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "form_type": "10-K",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "filed_at": "2026-02-01",
        "source_url": "https://example.test/filing.htm",
        "raw_text": "Revenue USD 123.45",
        "accession_number": "0000123456-26-000001",
        "source_document": "filing.htm",
    }]
    original = deepcopy(facts)

    evidence = build_fact_evidence(facts)

    assert evidence == [{
        "evidence_id": "fact:fact-1",
        "kind": "financial_fact",
        "company_cik": "0000123456",
        "entity": "Example Corp",
        "market": "US",
        "concept": "revenue",
        "value": "123.45",
        "unit": "USD",
        "unit_scale": 1000.0,
        "unit_provenance": "source_header",
        "scope": "consolidated",
        "consolidated_scope": "consolidated",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "form_type": "10-K",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "filed_at": "2026-02-01",
        "source_url": "https://example.test/filing.htm",
        "raw_text": "Revenue USD 123.45",
        "accession_number": "0000123456-26-000001",
        "source_document": "filing.htm",
        "dimensions": (),
        "normalization_lineage": None,
        "classification_evidence_ids": (),
        "usage_status": "",
    }]
    assert facts == original


def test_build_fact_evidence_preserves_existing_defaults():
    evidence = build_fact_evidence([{
        "fact_id": "minimal",
        "concept": "revenue",
        "value": 10,
        "unit": "USD",
        "fiscal_year": 2025,
        "filed_at": "2026-02-01",
        "source_url": "https://example.test/minimal.htm",
    }])[0]

    assert evidence == {
        "evidence_id": "fact:minimal",
        "kind": "financial_fact",
        "company_cik": "",
        "entity": "",
        "market": "",
        "concept": "revenue",
        "value": 10,
        "unit": "USD",
        "unit_scale": 1.0,
        "unit_provenance": "unknown",
        "scope": "",
        "consolidated_scope": "",
        "fiscal_year": 2025,
        "fiscal_period": "",
        "form_type": "",
        "start_date": "",
        "end_date": "",
        "filed_at": "2026-02-01",
        "source_url": "https://example.test/minimal.htm",
        "raw_text": "",
        "accession_number": "",
        "source_document": "",
        "dimensions": (),
        "normalization_lineage": None,
        "classification_evidence_ids": (),
        "usage_status": "",
    }


def test_research_public_import_is_the_same_function():
    assert research_build_fact_evidence is build_fact_evidence


def test_build_fact_evidence_preserves_real_lineage_and_does_not_mutate_fact():
    fact = {
        "fact_id": "lineage-fact",
        "concept": "revenue",
        "value": "123450.00",
        "unit": "USD",
        "fiscal_year": 2025,
        "filed_at": "2026-02-01",
        "source_url": "https://example.test/lineage.htm",
        "dimensions": ({
            "axis": "{https://example.test/taxonomy}BusinessSegmentsAxis",
            "member": "{https://example.test/taxonomy}ServicesMember",
            "kind": "explicit",
        },),
        "normalization_lineage": {
            "raw_value": "123.45",
            "scale": "1000",
            "normalized_value": "123450.00",
            "quantity_kind": "monetary",
            "currency": "USD",
            "unit_evidence_ids": ("unit:filing-header-1",),
            "method": "explicit-source-unit-v1",
            "value_transform": "identity",
            "inputs": (),
        },
        "classification_evidence_ids": ("classification:statement-title-1",),
        "usage_status": "research",
    }
    original = deepcopy(fact)

    evidence = build_fact_evidence([fact])[0]

    assert evidence["dimensions"] == original["dimensions"]
    assert evidence["normalization_lineage"] == original["normalization_lineage"]
    assert evidence["classification_evidence_ids"] == original["classification_evidence_ids"]
    assert evidence["usage_status"] == original["usage_status"]
    assert fact == original


def test_financial_evidence_module_imports_only_typing():
    module_path = Path(__file__).resolve().parents[1] / "src" / "openthesis" / "financial_evidence.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert imported_modules == {"typing"}
    assert not imported_modules.intersection({
        "openthesis.providers",
        "openthesis.research",
        "openthesis.service",
        "openthesis.storage",
    })
