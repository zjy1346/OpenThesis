from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import FilingDocument, FinancialFact
from openthesis.financial_ingestion import FinancialGroupValidation
from openthesis.financial_recovery import (
    FinancialRecoveryController,
    RecoveryState,
    financial_input_fingerprint,
)
from openthesis.markets import build_company
from openthesis.market_financials import FinancialValidation, ValidationStatus
from openthesis.storage import Storage


class FinancialRecoveryControllerTests(unittest.TestCase):
    def test_source_discovery_never_uses_persisted_quality_as_current_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root)
            company = build_company("300005.SZ", "Source Only")
            storage.save_company(company)
            path = root / "source.pdf"
            path.write_bytes(b"captured bytes; numerical validation is downstream")
            filing = FilingDocument("source-only", company.security_id, "source-only", "ANNUAL_REPORT",
                "FY", "2025-12-31", "2026-03-01", "source.pdf", "https://example.test/source.pdf",
                local_path=str(path), content_hash=hashlib.sha256(path.read_bytes()).hexdigest())
            storage.save_filings([filing])
            class Adapter:
                def list_financial_filings(self, _company, *, limit):
                    raise TimeoutError("offline discovery timeout")
            with patch.object(storage, "get_validation_groups", side_effect=AssertionError("stale summaries are not proof")), \
                    patch.object(storage, "get_facts", side_effect=AssertionError("discovery must not adjudicate facts")):
                outcome = FinancialRecoveryController(storage).discover(Adapter(), company, annual_limit=1)
            self.assertEqual(outcome.filings, (filing,))
            self.assertEqual(outcome.state, RecoveryState.RESOLVED_STALE)
            self.assertEqual(outcome.next_action, "continue_local_validation")
            self.assertEqual(outcome.failed_fields, ())
            self.assertEqual(outcome.freshness, "freshness_unverified")

    def test_input_fingerprint_is_stable_and_changes_with_parser_inputs(self) -> None:
        company = build_company("300000.SZ", "Fingerprint Company")
        first = FilingDocument(
            "doc-a", company.security_id, "acc-a", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "a.pdf", "https://example.test/a",
            content_hash="ABC123",
        )
        second = FilingDocument(
            "doc-b", company.security_id, "acc-b", "ANNUAL_REPORT", "FY",
            "2024-12-31", "2025-03-01", "b.pdf", "https://example.test/b",
            content_hash="DEF456",
        )
        one = financial_input_fingerprint(
            company, [first, second], parser_version="parser-v1", rules_version="rules-v1",
        )
        reordered = financial_input_fingerprint(
            company, [second, first], parser_version="parser-v1", rules_version="rules-v1",
        )
        changed = financial_input_fingerprint(
            company, [first, second], parser_version="parser-v2", rules_version="rules-v1",
        )
        self.assertEqual(one, reordered)
        self.assertNotEqual(one, changed)

    def test_retry_state_is_owned_by_the_durable_core_not_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = FinancialRecoveryController(Storage(Path(directory)))
            self.assertFalse(hasattr(controller, "_retry_attempts"))
            self.assertFalse(hasattr(controller, "retry_decision"))
            # Execution/cooldown/exhaustion are exercised against the actual
            # repository and clients in test_official_source_recovery_contract
            # and test_managed_official_http, including process-owner restart.
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
            # This is a source receipt, not a claim that net income exists.
            self.assertEqual(outcome.state, RecoveryState.RESOLVED_STALE)
            self.assertEqual(outcome.targets, ("acc-unhealthy",))
            self.assertEqual(outcome.next_action, "continue_local_validation")
            self.assertEqual(outcome.failed_accessions, ())
            self.assertEqual(storage.get_facts(company.security_id), [])
            self.assertEqual(storage.get_validation_groups(company.security_id)[0]["issues"], ["net_income_missing"])

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
            self.assertEqual(outcome.state, RecoveryState.RESOLVED_STALE)
            self.assertEqual(outcome.next_action, "continue_local_validation")
            # Source recovery cannot upgrade either scoped numerical candidate.
            self.assertEqual(storage.get_facts(company.security_id), [])
            retained = storage.get_facts_audit(company.security_id)
            self.assertEqual({(fact["currency"], fact["consolidated_scope"]) for fact in retained},
                             {("CNY", "parent"), ("USD", "consolidated")})
