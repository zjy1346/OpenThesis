from __future__ import annotations

import json

from openthesis.domain import EvidenceRef, FinancialFact, FilingDocument, ResearchArtifact, ResearchRun, RunStatus
from openthesis.financial_ingestion import FinancialGroupValidation
from openthesis.market_financials import FinancialValidation, ValidationStatus
from openthesis.markets import build_company
from openthesis.storage import Storage, StorageReadResult


def _fact(company_cik: str, fact_id: str, concept: str = "revenue") -> FinancialFact:
    return FinancialFact(
        fact_id=fact_id,
        company_cik=company_cik,
        concept=concept,
        reported_concept=concept,
        value=123.0,
        unit="CNY",
        fiscal_year=2025,
        fiscal_period="FY",
        form_type="ANNUAL_REPORT",
        start_date="2025-01-01",
        end_date="2025-12-31",
        filed_at="2026-03-01",
        accession_number="acc-row-isolation",
        source_url="https://example.test/report.pdf",
        statement="income_statement",
        period_start="2025-01-01",
        consolidated_scope="consolidated",
        currency="CNY",
        unit_provenance="explicit",
        validation_status="VERIFIED",
    )


def _run(storage: Storage, company_cik: str) -> ResearchRun:
    company = build_company("688981.SH", company_cik, reporting_currency="CNY")
    storage.save_company(company)
    run = ResearchRun(
        run_id=f"run-{company_cik}",
        company=company,
        workflow_id="row-isolation",
        research_pack_id="pack",
        research_pack_version="1",
        provider_id="offline",
        model_id="fixture",
        data_as_of="2026-10-11",
        status=RunStatus.CREATED,
    )
    storage.save_run(run)
    return run


def test_read_artifacts_isolates_corrupt_payload_row_and_compat_get_is_healthy_only(tmp_path):
    storage = Storage(tmp_path)
    run = _run(storage, "artifact-row-company")
    storage.save_artifact(
        ResearchArtifact(
            artifact_id="healthy-artifact",
            run_id=run.run_id,
            artifact_type="stage-checkpoint",
            title="Healthy",
            content={"ok": "保留"},
        )
    )
    with storage.connect() as db:
        db.execute(
            "INSERT INTO artifacts(artifact_id,run_id,artifact_type,title,payload_json,model_id,agent_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "corrupt-artifact",
                run.run_id,
                "stage-checkpoint",
                "Corrupt",
                json.dumps({
                    "schema": "openthesis.candidate-chunks.v1",
                    "payload_digest": "0" * 64,
                    "count": 1,
                    "bytes": 100,
                }),
                "fixture",
                "fixture",
                "2026-10-11T00:00:01Z",
            ),
        )

    result = storage.read_artifacts(run.run_id)

    assert isinstance(result, StorageReadResult)
    assert [item["artifact_id"] for item in result.records] == ["healthy-artifact"]
    issue = next(item for item in result.issues if item["artifact_id"] == "corrupt-artifact")
    assert issue["path"] == "artifacts/corrupt-artifact/content"
    assert issue["code"] == "RECOVERY_PAYLOAD_MANIFEST_INVALID"
    assert "0" * 20 not in json.dumps(issue)
    assert [item["artifact_id"] for item in storage.get_artifacts(run.run_id)] == [
        "healthy-artifact"
    ]


def test_financial_snapshot_isolates_bad_fact_and_evidence_rows(tmp_path):
    storage = Storage(tmp_path)
    company = build_company("688981.SH", "financial-row-company", reporting_currency="CNY")
    storage.save_company(company)
    storage.save_filings([FilingDocument("doc-row-isolation", company.security_id,
        "acc-row-isolation", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-01",
        "report.pdf", "https://example.test/report.pdf")])
    healthy = _fact(company.security_id, "healthy-fact")
    corrupt = _fact(company.security_id, "corrupt-fact", "net_income")
    evidence = (
        EvidenceRef(
            "fact:healthy-fact",
            "doc-row-isolation",
            healthy.source_url,
            "Revenue",
            "page:1",
            "Revenue 123",
            healthy.filed_at,
        ),
        EvidenceRef(
            "fact:corrupt-fact",
            "doc-row-isolation",
            corrupt.source_url,
            "Net income",
            "page:2",
            "Net income 123",
            corrupt.filed_at,
        ),
    )
    validation = FinancialValidation(
        ValidationStatus.VERIFIED,
        (),
        frozenset({"revenue", "net_income"}),
        (healthy, corrupt),
        (),
    )
    group = FinancialGroupValidation(
        (healthy.accession_number, healthy.end_date, "FY", "consolidated", "CNY"),
        validation,
    )
    storage.replace_financial_ingestion(
        company.security_id,
        [healthy.accession_number],
        [healthy, corrupt],
        [],
        [group],
        evidence,
    )
    with storage.connect() as db:
        db.execute(
            "UPDATE financial_facts SET normalization_lineage_json = ? WHERE fact_id = ?",
            ("{broken", corrupt.fact_id),
        )
        db.execute(
            "UPDATE financial_evidence SET bbox_json = ? WHERE evidence_id = ?",
            ("[broken", "fact:corrupt-fact"),
        )

    snapshot = storage.get_financial_research_snapshot(
        company.security_id, include_evidence=True
    )

    assert [item["fact_id"] for item in snapshot["facts"]] == [healthy.fact_id]
    assert [item["evidence_id"] for item in snapshot["evidence"]] == [
        "fact:healthy-fact"
    ]
    issues = snapshot["storage_issues"]
    assert {item["path"] for item in issues} == {
        "financial_facts/corrupt-fact",
        "financial_evidence/fact:corrupt-fact",
    }
    assert all("broken" not in json.dumps(item) for item in issues)
    assert [item["fact_id"] for item in storage.get_facts(company.security_id)] == [
        healthy.fact_id
    ]


def test_corrupt_validation_group_is_unverifiable_and_healthy_group_survives(tmp_path):
    storage = Storage(tmp_path)
    company = build_company("688981.SH", "validation-row-company", reporting_currency="CNY")
    storage.save_company(company)
    healthy = _fact(company.security_id, "healthy-group-fact")
    validation = FinancialValidation(
        ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (healthy,), ()
    )
    groups = [
        FinancialGroupValidation(
            (healthy.accession_number, "2025-12-31", "FY", "consolidated", "CNY"),
            validation,
        ),
        FinancialGroupValidation(
            ("acc-bad-group", "2024-12-31", "FY", "consolidated", "CNY"),
            validation,
        ),
    ]
    storage.replace_financial_ingestion(
        company.security_id,
        [healthy.accession_number, "acc-bad-group"],
        [healthy],
        [],
        groups,
    )
    bad_group_id = "|".join((company.security_id, "acc-bad-group", "2024-12-31", "FY", "consolidated", "CNY", "target"))
    with storage.connect() as db:
        db.execute(
            "UPDATE financial_validation_groups SET issues_json = ?, covered_concepts_json = ? WHERE group_id = ?",
            ("{broken", '"not-a-list"', bad_group_id),
        )

    loaded = storage.get_validation_groups(company.security_id)
    healthy_row = next(item for item in loaded if item["group_id"] != bad_group_id)
    corrupt_row = next(item for item in loaded if item["group_id"] == bad_group_id)
    assert healthy_row["status"] == ValidationStatus.VERIFIED.value
    assert corrupt_row["status"] == "UNVERIFIABLE"
    assert corrupt_row["covered_concepts"] == []
    assert corrupt_row["storage_issues"]
    assert all("broken" not in json.dumps(item) for item in corrupt_row["storage_issues"])
