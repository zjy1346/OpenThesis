from decimal import Decimal
from dataclasses import replace
import tempfile
from pathlib import Path

import pytest

from openthesis.domain import (
    Company,
    EvidenceRef,
    FilingDocument,
    FinancialFact,
    FinancialNormalizationLineage,
)
from openthesis.financial_contracts import (
    CompilationPolicy,
    PDF_PARSER_VERSION,
    QuantityKind,
    SourceAmount,
    exact_decimal_arithmetic,
)
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from openthesis.storage import SCHEMA_VERSION, Storage, StorageCorruptionError


def _fixture():
    company = Company(
        "lineage-storage-co",
        "LINEAGE",
        "Lineage Storage Co.",
        market="CN_A",
        reporting_currency="CNY",
    )
    filing = FilingDocument(
        "lineage-storage-doc",
        company.security_id,
        "lineage-storage-accession",
        "ANNUAL_REPORT",
        "FY",
        "2025-12-31",
        "2026-03-01",
        "annual-report.pdf",
        "https://example.test/annual-report.pdf",
        content_hash="lineage-storage-source-hash",
    )
    raw_amount = "-9007199254740993.12345678901234567890"
    lineage = SourceAmount(
        raw_amount,
        "1",
        QuantityKind.MONEY,
        "CNY",
        ("unit:parent-equity",),
    ).normalize()
    normalized = Decimal(lineage.normalized_value)
    raw_text = f"Total shareholders’ equity {lineage.normalized_value}"
    fact = FinancialFact(
        fact_id="lineage-storage-equity",
        company_cik=company.security_id,
        concept="equity",
        reported_concept="Total shareholders’ equity",
        value=float(normalized),
        unit="CNY",
        fiscal_year=2025,
        fiscal_period="FY",
        form_type=filing.form_type,
        start_date=None,
        end_date=filing.period_end,
        filed_at=filing.filed_at,
        accession_number=filing.accession_number,
        source_url=filing.source_url,
        statement="balance_sheet",
        currency="CNY",
        unit_scale=1.0,
        unit_provenance="explicit_source_unit",
        source_document=filing.primary_document,
        source_page=1,
        source_bbox=(0.0, 0.0, 600.0, 20.0),
        raw_text=raw_text,
        parser_version=PDF_PARSER_VERSION,
        normalization_lineage=lineage,
        classification_evidence_ids=("context:nci", "context:total-equity"),
    )

    def evidence(evidence_id, excerpt, bbox=None):
        return EvidenceRef(
            evidence_id,
            filing.document_id,
            filing.source_url,
            filing.primary_document,
            "page:1",
            excerpt,
            filing.filed_at,
            filing.content_hash,
            bbox,
        )

    refs = (
        evidence("fact:lineage-storage-equity", raw_text, fact.source_bbox),
        evidence("unit:parent-equity", "RMB in yuan"),
        evidence("context:nci", "Noncontrolling interests 10", (0.0, 24.0, 600.0, 44.0)),
        evidence("context:total-equity", "Total equity 110", (0.0, 46.0, 600.0, 66.0)),
    )
    return company, filing, fact, refs


def _prepare(company, filing, facts, evidence):
    return FinancialPipeline(None).prepare(
        FinancialResearchSnapshot.from_facts(
            company,
            (filing,),
            facts,
            evidence=evidence,
        ),
        CompilationPolicy(
            filings=(filing,),
            required_concepts=frozenset({"equity"}),
            reporting_currency="CNY",
            fiscal_period="FY",
            period_range=(filing.period_end, filing.period_end),
        ),
    )


def test_promoted_generation_roundtrips_nested_exact_lineage_and_classification_proof():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        _exercise_promoted_generation_roundtrip(Path(directory))


def _exercise_promoted_generation_roundtrip(tmp_path: Path):
    company, filing, fact, evidence = _fixture()
    initial_view = _prepare(company, filing, (fact,), evidence)
    assert [item.fact_id for item in initial_view.facts] == [fact.fact_id]

    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    result = storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        list(initial_view.facts),
        validation_groups=list(initial_view.canonical_dataset.group_validations),
        evidence=list(evidence),
        generation_id="lineage-storage-generation-1",
        parser_version=PDF_PARSER_VERSION,
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
    )
    assert result["state"] == "promoted"

    snapshot = storage.get_financial_research_snapshot(company.security_id)
    assert snapshot["generations"] == {
        filing.accession_number: "lineage-storage-generation-1"
    }
    rebuilt = FinancialFact(**snapshot["facts"][0])
    assert rebuilt.normalization_lineage == fact.normalization_lineage
    assert rebuilt.normalization_lineage.raw_value == "-9007199254740993.12345678901234567890"
    assert rebuilt.normalization_lineage.normalized_value == fact.normalization_lineage.normalized_value
    assert rebuilt.classification_evidence_ids == ("context:nci", "context:total-equity")

    readmitted = _prepare(company, filing, (rebuilt,), evidence)
    assert [item.fact_id for item in readmitted.facts] == [fact.fact_id]
    assert readmitted.facts[0].normalization_lineage == fact.normalization_lineage
    assert readmitted.facts[0].classification_evidence_ids == fact.classification_evidence_ids


def test_compatibility_fact_write_paths_roundtrip_lineage():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        _exercise_compatibility_fact_write_paths(Path(directory))


def _exercise_compatibility_fact_write_paths(data_dir: Path):
    company, _, fact, _ = _fixture()
    storage = Storage(data_dir)
    storage.save_company(company)
    saved_fact = replace(
        fact,
        fact_id="lineage-save-fact",
        accession_number="lineage-save-accession",
    )
    replaced_fact = replace(
        fact,
        fact_id="lineage-replace-fact",
        accession_number="lineage-replace-accession",
    )
    storage.save_facts([saved_fact])
    storage.replace_facts_for_filings(
        company.security_id,
        [replaced_fact.accession_number],
        [replaced_fact],
    )

    stored = {
        item["fact_id"]: item for item in storage.get_facts(company.security_id)
    }
    assert set(stored) == {saved_fact.fact_id, replaced_fact.fact_id}
    for fact_id, expected in (
        (saved_fact.fact_id, saved_fact),
        (replaced_fact.fact_id, replaced_fact),
    ):
        rebuilt = FinancialFact(**stored[fact_id])
        assert rebuilt.normalization_lineage == expected.normalization_lineage
        assert rebuilt.classification_evidence_ids == expected.classification_evidence_ids


def test_nested_sec_liabilities_lineage_roundtrips_exact_operands():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        _exercise_nested_sec_lineage(Path(directory))


def _exercise_nested_sec_lineage(data_dir: Path):
    company, _, base_fact, _ = _fixture()
    assets = SourceAmount(
        "9007199254740993.12345678901234567890",
        "1",
        QuantityKind.MONEY,
        "CNY",
        ("unit:assets",),
    ).normalize()
    equity = SourceAmount(
        "1234567890123.00000000000000000001",
        "1",
        QuantityKind.MONEY,
        "CNY",
        ("unit:equity",),
    ).normalize()
    normalized = exact_decimal_arithmetic(
        (Decimal(assets.normalized_value), Decimal(equity.normalized_value)),
        "subtract",
    )
    lineage = FinancialNormalizationLineage(
        raw_value="",
        scale="1",
        normalized_value=format(normalized, "f"),
        quantity_kind="money",
        currency="CNY",
        unit_evidence_ids=("unit:assets", "unit:equity"),
        method="sec-xbrl-arithmetic-v1",
        value_transform="subtract",
        inputs=(assets, equity),
    )
    fact = replace(
        base_fact,
        fact_id="sec-derived-liabilities-storage-fact",
        concept="liabilities",
        reported_concept="Liabilities",
        value=float(normalized),
        raw_text=f"Liabilities {lineage.normalized_value}",
        parser_version="sec-companyfacts-v3",
        normalization_lineage=lineage,
        classification_evidence_ids=(),
    )
    storage = Storage(data_dir)
    storage.save_company(company)
    storage.save_facts([fact])

    rebuilt = FinancialFact(**storage.get_facts(company.security_id)[0])
    assert rebuilt.normalization_lineage == lineage
    assert [item.raw_value for item in rebuilt.normalization_lineage.inputs] == [
        "9007199254740993.12345678901234567890",
        "1234567890123.00000000000000000001",
    ]
    assert rebuilt.normalization_lineage.value_transform == "subtract"


def test_unpromoted_generation_audit_roundtrips_lineage_and_tuple_refs():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        _exercise_unpromoted_generation_audit(Path(directory))


def _exercise_unpromoted_generation_audit(data_dir: Path):
    company, filing, fact, evidence = _fixture()
    initial_view = _prepare(company, filing, (fact,), evidence)
    storage = Storage(data_dir)
    storage.save_company(company)
    storage.save_filings([filing])
    result = storage.replace_financial_ingestion(
        company.security_id,
        [filing.accession_number],
        list(initial_view.facts),
        evidence=list(evidence),
        generation_id="lineage-storage-unpromoted-generation",
        complete_accessions=set(),
    )
    assert result["state"] == "incomplete"

    audit = storage.get_facts_audit(company.security_id)
    assert len(audit) == 1 and audit[0]["usage_status"] == "candidate_audit"
    rebuilt = FinancialFact(**audit[0])
    assert rebuilt.normalization_lineage == fact.normalization_lineage
    assert rebuilt.classification_evidence_ids == fact.classification_evidence_ids


def test_schema_14_migration_adds_lineage_columns_without_reclassifying_legacy_facts():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        _exercise_schema_14_migration(Path(directory))


def _exercise_schema_14_migration(data_dir: Path):
    company, _, fact, _ = _fixture()
    storage = Storage(data_dir)
    storage.save_company(company)
    legacy_fact = replace(
        fact,
        fact_id="legacy-lineage-storage-fact",
        normalization_lineage=None,
        classification_evidence_ids=(),
        validation_status="VERIFIED",
    )
    storage.save_facts([legacy_fact])
    before = storage.get_facts_audit(company.security_id)[0]

    with storage.connect() as db:
        db.execute("ALTER TABLE financial_facts DROP COLUMN normalization_lineage_json")
        db.execute("ALTER TABLE financial_facts DROP COLUMN classification_evidence_ids_json")
        db.execute("UPDATE metadata SET value = '14' WHERE key = 'schema_version'")

    upgraded = Storage(data_dir)
    with upgraded.connect() as db:
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(financial_facts)")
        }
        assert {"normalization_lineage_json", "classification_evidence_ids_json"} <= columns
        assert db.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        recovery_tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "recovery_incidents",
            "recovery_plans",
            "recovery_actions",
            "recovery_events",
            "recovery_target_leases",
            "stage_verification_snapshots",
            "model_request_journal",
            "model_stage_request_heads",
        } <= recovery_tables
    assert upgraded.migration_backup is not None and upgraded.migration_backup.exists()

    after = upgraded.get_facts_audit(company.security_id)[0]
    assert after["normalization_lineage"] is None
    assert after["classification_evidence_ids"] == ()
    for key in (
        "fact_id", "value", "validation_status", "provenance_status",
        "usage_status", "derived_version",
    ):
        assert after[key] == before[key]


@pytest.mark.parametrize(
    ("column", "corrupt_value", "error_code"),
    (
        (
            "normalization_lineage_json",
            "{broken-json",
            "FINANCIAL_FACT_NORMALIZATION_LINEAGE_JSON_INVALID",
        ),
        (
            "normalization_lineage_json",
            '{"raw_value":1,"scale":"1","normalized_value":"1",'
            '"quantity_kind":"money","currency":"CNY",'
            '"unit_evidence_ids":[],"inputs":[]}',
            "FINANCIAL_FACT_NORMALIZATION_LINEAGE_SHAPE_INVALID",
        ),
        (
            "classification_evidence_ids_json",
            "{}",
            "FINANCIAL_FACT_CLASSIFICATION_EVIDENCE_SHAPE_INVALID",
        ),
    ),
)
def test_corrupt_lineage_or_classification_json_fails_closed(
    column, corrupt_value, error_code,
):
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        _exercise_corrupt_json(Path(directory), column, corrupt_value, error_code)


def _exercise_corrupt_json(data_dir: Path, column: str, corrupt_value: str, error_code: str):
    company, _, fact, _ = _fixture()
    storage = Storage(data_dir)
    storage.save_company(company)
    healthy = replace(fact, fact_id="healthy-lineage-sibling")
    storage.save_facts([fact, healthy])
    with storage.connect() as db:
        db.execute(
            f"UPDATE financial_facts SET {column} = ? WHERE fact_id = ?",
            (corrupt_value, fact.fact_id),
        )

    snapshot = storage.get_financial_research_snapshot(company.security_id)
    assert [item["fact_id"] for item in snapshot["facts"]] == [healthy.fact_id]
    assert snapshot["storage_issues"] == [{"code": error_code,
        "path": "financial_facts/" + fact.fact_id, "row_id": fact.fact_id}]
    with storage.connect() as db:
        row = db.execute("SELECT * FROM financial_facts WHERE fact_id=?", (fact.fact_id,)).fetchone()
    # Isolation must neither fabricate a repaired lineage nor rewrite the
    # captured corrupt bytes. The individual-row decoder remains fail-closed.
    assert row[column] == corrupt_value
    with pytest.raises(StorageCorruptionError, match=error_code):
        storage._fact_row(row)
