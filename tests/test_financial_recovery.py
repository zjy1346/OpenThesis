from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from openthesis.domain import FilingDocument, FinancialFact
from openthesis.financial_ingestion import FinancialGroupValidation
from openthesis.financial_recovery import (
    FinancialRecoveryController,
    RecoveryState,
)
from openthesis.markets import build_company
from openthesis.market_financials import FinancialValidation, ValidationStatus
from openthesis.storage import Storage


class FinancialRecoveryControllerTests(unittest.TestCase):
    def test_discovery_error_uses_complete_local_snapshot_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root)
            company = build_company("300001.SZ", "Local Company")
            storage.save_company(company)
            pdf = root / "local.pdf"
            pdf.write_bytes(b"stable financial report")
            filing = FilingDocument(
                "doc-local", company.security_id, "acc-local", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "local.pdf", "https://example.test/local.pdf",
                local_path=str(pdf), content_hash=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            )
            storage.save_filings([filing])
            validation = FinancialValidation(
                ValidationStatus.VERIFIED, (),
                frozenset({"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"}),
                (), (),
            )
            storage.replace_financial_ingestion(
                company.security_id, [filing.accession_number], [], [],
                [FinancialGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"), validation)],
            )

            class Adapter:
                def list_financial_filings(self, _company, *, limit=5):
                    raise RuntimeError("official discovery unavailable")

            outcome = FinancialRecoveryController(storage).discover(
                Adapter(), company, annual_limit=1
            )
            self.assertEqual(outcome.state, RecoveryState.RESOLVED_STALE)
            self.assertEqual([item.accession_number for item in outcome.filings], ["acc-local"])
            self.assertEqual(outcome.freshness, "freshness_unverified")
            self.assertEqual(outcome.error_code, "FILING_FETCH_FAILED")

    def test_discovery_error_keeps_unhealthy_local_target_for_reparse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root)
            company = build_company("300002.SZ", "Unhealthy Company")
            storage.save_company(company)
            pdf = root / "unhealthy.pdf"
            pdf.write_bytes(b"unhealthy but readable financial report")
            filing = FilingDocument(
                "doc-unhealthy", company.security_id, "acc-unhealthy", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "unhealthy.pdf", "https://example.test/unhealthy.pdf",
                local_path=str(pdf), content_hash=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            )
            storage.save_filings([filing])
            validation = FinancialValidation(
                ValidationStatus.READY_WITH_WARNINGS, ("net_income_missing",),
                frozenset({"revenue"}), (), (),
            )
            storage.replace_financial_ingestion(
                company.security_id, [filing.accession_number], [], [],
                [FinancialGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"), validation)],
            )

            class Adapter:
                def list_financial_filings(self, _company, *, limit=5):
                    raise RuntimeError("official discovery unavailable")

            outcome = FinancialRecoveryController(storage).discover(
                Adapter(), company, annual_limit=1
            )
            self.assertEqual(outcome.state, RecoveryState.RETRYABLE_EXTERNAL_FAILURE)
            self.assertEqual(outcome.targets, ("acc-unhealthy",))
            self.assertEqual(outcome.next_action, "retry_local_parse")
            self.assertEqual(outcome.failed_accessions, ("acc-unhealthy",))
            self.assertIn("net_income", outcome.failed_fields)
            self.assertIn("net_income_missing", outcome.failed_fields)

    def test_verified_group_merges_total_equity_fact_as_equity_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root)
            company = build_company("300003.SZ", "Alias Company")
            storage.save_company(company)
            pdf = root / "alias.pdf"
            pdf.write_bytes(b"verified financial report")
            filing = FilingDocument(
                "doc-alias", company.security_id, "acc-alias", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "alias.pdf", "https://example.test/alias.pdf",
                local_path=str(pdf), content_hash=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            )
            storage.save_filings([filing])
            alias_fact = FinancialFact(
                "fact-total-equity", company.security_id, "total_equity", "Total equity",
                100.0, "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01",
                "2025-12-31", "2026-03-01", "acc-alias", filing.source_url,
                currency="CNY", statement="balance_sheet", validation_status="VERIFIED",
            )
            validation = FinancialValidation(
                ValidationStatus.VERIFIED, (), frozenset({
                    "revenue", "net_income", "operating_cash_flow", "assets", "liabilities",
                }), (), (),
            )
            storage.replace_financial_ingestion(
                company.security_id, [filing.accession_number], [alias_fact], [],
                [FinancialGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"), validation)],
            )

            class Adapter:
                def list_financial_filings(self, _company, *, limit=5):
                    raise RuntimeError("official discovery unavailable")

            outcome = FinancialRecoveryController(storage).discover(
                Adapter(), company, annual_limit=1
            )
            self.assertEqual(outcome.state, RecoveryState.RESOLVED_STALE)

    def test_parent_or_wrong_currency_fact_cannot_fill_equity_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root)
            company = build_company("300004.SZ", "Scoped Company")
            storage.save_company(company)
            pdf = root / "scoped.pdf"
            pdf.write_bytes(b"scoped financial report")
            filing = FilingDocument(
                "doc-scoped", company.security_id, "acc-scoped", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "scoped.pdf", "https://example.test/scoped.pdf",
                local_path=str(pdf), content_hash=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            )
            storage.save_filings([filing])
            scoped_facts = [
                FinancialFact(
                    "fact-parent-equity", company.security_id, "total_equity", "Total equity",
                    100.0, "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01",
                    "2025-12-31", "2026-03-01", "acc-scoped", filing.source_url,
                    scope="parent", consolidated_scope="parent", currency="CNY",
                    statement="balance_sheet", validation_status="VERIFIED",
                ),
                FinancialFact(
                    "fact-foreign-equity", company.security_id, "total_equity", "Total equity",
                    100.0, "USD", 2025, "FY", "ANNUAL_REPORT", "2025-01-01",
                    "2025-12-31", "2026-03-01", "acc-scoped", filing.source_url,
                    currency="USD", statement="balance_sheet", validation_status="VERIFIED",
                ),
            ]
            validation = FinancialValidation(
                ValidationStatus.VERIFIED, (), frozenset({
                    "revenue", "net_income", "operating_cash_flow", "assets", "liabilities",
                }), (), (),
            )
            storage.replace_financial_ingestion(
                company.security_id, [filing.accession_number], scoped_facts, [],
                [FinancialGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"), validation)],
            )

            class Adapter:
                def list_financial_filings(self, _company, *, limit=5):
                    raise RuntimeError("official discovery unavailable")

            outcome = FinancialRecoveryController(storage).discover(
                Adapter(), company, annual_limit=1
            )
            self.assertEqual(outcome.state, RecoveryState.RETRYABLE_EXTERNAL_FAILURE)
            self.assertEqual(outcome.next_action, "retry_local_parse")
            self.assertIn("equity", outcome.failed_fields)
