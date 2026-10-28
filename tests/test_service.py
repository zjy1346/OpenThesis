from __future__ import annotations

import base64
import io
import json
import hashlib
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from openthesis.application_services import ReportService, ResearchOrchestrator
from openthesis.domain import (
    Company,
    FilingDocument,
    FinancialFact,
    EvidenceRef,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)
from openthesis.markets import build_company
from openthesis.market_data import MarketDataError
from openthesis.market_snapshot import QuoteSnapshot
from openthesis.ot import compile_studio_draft, minimal_studio_draft
from openthesis.service import (
    AppService,
    PreferenceValidationError,
    _bounded_download_filings,
    _cached_us_annual_window_is_complete,
    _latest_sec_verified_group,
    _market_snapshot,
    _request_secrets,
    _vision_config_from_request,
    _vision_request_from_preferences,
    _model_config_from_request,
    _vision_batch_upload_allowed,
    VisionFallbackConfig,
    _research_history_years,
    _research_data_message,
    _ResearchJob,
    _FinancialReportRefreshError,
    FinancialRetryResult,
    _financial_status,
    _canonical_retry_snapshot,
    _canonical_snapshot_digest,
    _filing_identity_matches,
    _latest_annual_validations,
    _scanner_diagnostic_code,
    _report_retryable,
    _classify_research_exception,
    _cached_filing_evidence,
    _model_stages_retryable,
)
from openthesis.providers import ProviderError
from openthesis.research_recovery import RecoveryPlan
from openthesis.financial_ingestion import FinancialDataset, FinancialGroupValidation, FilingManifest, FinancialIngestionEngine
from openthesis.financial_compiler import FactGroupValidation
from openthesis.market_financials import FinancialValidation, ValidationStatus
from openthesis.vision_financials import VisionPageRequest, VisionUploadPlan


class _FakeSecClient:
    _ACCESSION = "0000000001-26-000001"
    _DURATION_VALUES = {
        "Revenues": {2025: 1_250_000, 2024: 1_100_000},
        "NetIncomeLoss": {2025: 240_000, 2024: 210_000},
        "NetCashProvidedByUsedInOperatingActivities": {
            2025: 310_000, 2024: 275_000,
        },
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            2025: 120_000, 2024: 100_000,
        },
    }
    _INSTANT_VALUES = {
        "Assets": {2025: 3_000_000, 2024: 2_700_000},
        "Liabilities": {2025: 1_700_000, 2024: 1_500_000},
        "StockholdersEquity": {2025: 1_300_000, 2024: 1_200_000},
    }

    def __init__(self, user_agent: str, _cache_dir: Path):
        self.user_agent = user_agent
        from openthesis.sec_client import SecClient

        self._companyfacts_client = SecClient(
            user_agent, _cache_dir, min_interval=0
        )

    def search_companies(self, query: str, limit: int = 15) -> list[Company]:
        if query.lower() not in {"acme", "acm"}:
            return []
        return [Company(cik="0000000001", ticker="ACME", name="Acme Corp")][:limit]

    def list_annual_filings(self, company: Company, limit: int = 5) -> list[FilingDocument]:
        accession = self._ACCESSION
        accession_path = accession.replace("-", "")
        return [
            FilingDocument(
                document_id=f"sec:{company.cik}:{accession}",
                company_cik=company.cik,
                accession_number=accession,
                form_type="10-K",
                fiscal_period="FY",
                period_end="2025-12-31",
                filed_at="2026-02-01T00:00:00+00:00",
                primary_document="acme-2025.htm",
                source_url=(
                    "https://www.sec.gov/Archives/edgar/data/1/"
                    f"{accession_path}/acme-2025.htm"
                ),
            )
        ][:limit]

    def get_company_facts(self, company: Company) -> list[FinancialFact]:
        with patch.object(
            self._companyfacts_client,
            "_get_json",
            return_value=self._companyfacts_payload(),
        ):
            return self._companyfacts_client.get_company_facts(company)

    def download_filing(
        self, filing: FilingDocument, target_dir: Path
    ) -> FilingDocument:
        raw = self._inline_xbrl_source(filing)
        target_dir.mkdir(parents=True, exist_ok=True)
        output = target_dir / Path(filing.primary_document).name
        output.write_bytes(raw)
        return replace(
            filing,
            local_path=str(output),
            content_hash=hashlib.sha256(raw).hexdigest(),
        )

    def _companyfacts_payload(self) -> dict:
        def row(year: int, value: int, *, instant: bool) -> dict:
            result = {
                "val": value,
                "fy": year,
                "fp": "FY",
                "form": "10-K",
                "end": f"{year}-12-31",
                "filed": "2026-02-01",
                "accn": self._ACCESSION,
            }
            if not instant:
                result["start"] = f"{year}-01-01"
            return result

        us_gaap = {
            tag: {
                "units": {
                    "USD": [
                        row(year, value, instant=False)
                        for year, value in sorted(values.items(), reverse=True)
                    ]
                }
            }
            for tag, values in self._DURATION_VALUES.items()
        }
        us_gaap.update({
            tag: {
                "units": {
                    "USD": [
                        row(year, value, instant=True)
                        for year, value in sorted(values.items(), reverse=True)
                    ]
                }
            }
            for tag, values in self._INSTANT_VALUES.items()
        })
        return {"facts": {"us-gaap": us_gaap, "dei": {}}}

    def _inline_xbrl_source(self, filing: FilingDocument) -> bytes:
        periods = (2025, 2024)
        duration_contexts = "".join(
            f"<x:context id='duration{year}'><x:entity>"
            f"<x:identifier scheme='http://www.sec.gov/CIK'>"
            f"{filing.company_cik}</x:identifier></x:entity><x:period>"
            f"<x:startDate>{year}-01-01</x:startDate>"
            f"<x:endDate>{year}-12-31</x:endDate>"
            f"</x:period></x:context>"
            for year in periods
        )
        instant_contexts = "".join(
            f"<x:context id='instant{year}'><x:entity>"
            f"<x:identifier scheme='http://www.sec.gov/CIK'>"
            f"{filing.company_cik}</x:identifier></x:entity><x:period>"
            f"<x:instant>{year}-12-31</x:instant>"
            f"</x:period></x:context>"
            for year in periods
        )

        def numeric_cells(tag: str, values: dict[int, int], *, instant: bool) -> str:
            context_prefix = "instant" if instant else "duration"
            return "".join(
                f"<td><ix:nonFraction name='g:{tag}' "
                f"contextRef='{context_prefix}{year}' unitRef='usd' "
                f"format='ixt:num-dot-decimal' decimals='0'>"
                f"{value:,}</ix:nonFraction></td>"
                for year, value in sorted(values.items(), reverse=True)
            )

        def table_rows(values: dict[str, dict[int, int]], *, instant: bool) -> str:
            return "".join(
                f"<tr><td>{tag}</td>"
                f"{numeric_cells(tag, yearly, instant=instant)}</tr>"
                for tag, yearly in values.items()
            )

        income_values = {
            tag: self._DURATION_VALUES[tag]
            for tag in ("Revenues", "NetIncomeLoss")
        }
        cash_flow_values = {
            tag: self._DURATION_VALUES[tag]
            for tag in (
                "NetCashProvidedByUsedInOperatingActivities",
                "PaymentsToAcquirePropertyPlantAndEquipment",
            )
        }
        durations = table_rows(income_values, instant=False)
        cash_flows = table_rows(cash_flow_values, instant=False)
        instants = table_rows(self._INSTANT_VALUES, instant=True)
        year_focus = filing.period_end[:4]
        source = f"""<html xmlns="http://www.w3.org/1999/xhtml"
          xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
          xmlns:x="http://www.xbrl.org/2003/instance"
          xmlns:dei="http://xbrl.sec.gov/dei/2025"
          xmlns:g="http://fasb.org/us-gaap/2025"
          xmlns:iso="http://www.xbrl.org/2003/iso4217"
          xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12">
          <head><title>Acme Corp {year_focus} Form 10-K</title></head>
          <body><ix:header><ix:resources>
            {duration_contexts}{instant_contexts}
            <x:unit id="usd"><x:measure>iso:USD</x:measure></x:unit>
            <ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="duration2025">2025</ix:nonNumeric>
            <ix:nonNumeric name="dei:DocumentFiscalPeriodFocus" contextRef="duration2025">FY</ix:nonNumeric>
          </ix:resources></ix:header>
          <h2>Consolidated Income Statement</h2>
          <table><tr><th>Fiscal year</th><th>2025</th><th>2024</th></tr>{durations}</table>
          <h2>Consolidated Statement of Cash Flows</h2>
          <table><tr><th>Fiscal year</th><th>2025</th><th>2024</th></tr>{cash_flows}</table>
          <h2>Consolidated Balance Sheet</h2>
          <table><tr><th>Fiscal year</th><th>2025</th><th>2024</th></tr>{instants}</table>
          </body></html>"""
        return source.encode("utf-8")


class _FakeMarketData:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    def resolve(self, query: str, market, *, limit: int = 15):
        self.calls.append((query, market.value, limit))
        return [build_company("832982.BJ", "Jinbo Bio")]


class _ResearchMarketData:
    def __init__(self, adapter, official_companies=()):
        self.adapter = adapter
        self.official_companies = tuple(official_companies)

    def resolve(self, _query, market, *, limit=15):
        return [company for company in self.official_companies if company.market == market.value][:limit]

    def adapter_for(self, _company: Company):
        return self.adapter


_OFFLINE_FILING_FIXTURE_BYTES = b"%PDF-1.4\n% OpenThesis offline source fixture\n%%EOF\n"


def _download_offline_filing_fixture(item: FilingDocument, target: Path) -> FilingDocument:
    target.mkdir(parents=True, exist_ok=True)
    document_key = hashlib.sha256(item.document_id.encode("utf-8")).hexdigest()[:12]
    path = target / f"offline-source-fixture-{document_key}.pdf"
    path.write_bytes(_OFFLINE_FILING_FIXTURE_BYTES)
    return replace(
        item,
        local_path=str(path),
        content_hash=hashlib.sha256(_OFFLINE_FILING_FIXTURE_BYTES).hexdigest(),
    )


def _download_bound_financial_fixture(
    item: FilingDocument,
    target: Path,
    rows: tuple[tuple[str, str, str, str, str, str, bool], ...] = (),
    *,
    disclosure: str = "",
) -> tuple[FilingDocument, tuple[FinancialFact, ...], tuple[EvidenceRef, ...]]:
    """Build a tiny HTML original whose exact bytes bind rows and unit refs.

    Each row is ``(fact_id, concept, label, raw_value, currency, scope, proven)``.
    A false ``proven`` value deliberately omits normalization/unit lineage so
    downstream compilation must quarantine it rather than trust fixture flags.
    """
    import html

    from openthesis.financial_contracts import PDF_PARSER_VERSION, QuantityKind, SourceAmount

    unit_label = {"CNY": "Unit: RMB", "USD": "Unit: USD", "HKD": "Unit: HK$"}
    currencies = tuple(dict.fromkeys(row[4] for row in rows))
    if any(currency not in unit_label for currency in currencies):
        raise AssertionError("fixture currency must have an explicit unit label")
    unit_ids = {currency: f"unit:{item.document_id}:{currency}" for currency in currencies}
    table_rows = [f"<tr><td>{html.escape(unit_label[currency])}</td></tr>" for currency in currencies]
    table_rows.extend(
        "<tr><td>{}</td><td>{}</td></tr>".format(html.escape(label), html.escape(raw_value))
        for _fact_id, _concept, label, raw_value, _currency, _scope, _proven in rows
    )
    narrative = (
        f"<h2>Business Overview</h2><p>{html.escape(disclosure)}</p>" if disclosure else ""
    )
    raw = (
        "<html><body>" + narrative + "<table>" + "".join(table_rows)
        + "</table></body></html>"
    ).encode("utf-8")
    target.mkdir(parents=True, exist_ok=True)
    document_name = f"source-{hashlib.sha256(item.document_id.encode('utf-8')).hexdigest()[:12]}.html"
    path = target / document_name
    path.write_bytes(raw)
    content_hash = hashlib.sha256(raw).hexdigest()
    filing = replace(
        item,
        primary_document=document_name,
        local_path=str(path),
        content_hash=content_hash,
    )
    statements = {
        "revenue": "income_statement",
        "net_income": "income_statement",
        "operating_cash_flow": "cash_flow",
        "assets": "balance_sheet",
        "liabilities": "balance_sheet",
        "equity": "balance_sheet",
    }
    instant = {"assets", "liabilities", "equity"}
    evidence = [
        EvidenceRef(
            evidence_id=unit_ids[currency],
            document_id=filing.document_id,
            source_url=filing.source_url,
            title="Explicit fixture unit declaration",
            locator="page:1",
            excerpt=unit_label[currency],
            published_at=filing.filed_at,
            content_hash=content_hash,
        )
        for currency in currencies
    ]
    facts = []
    for fact_id, concept, label, raw_value, currency, scope, proven in rows:
        excerpt = f"{label} | {raw_value}"
        row_ref = EvidenceRef(
            evidence_id=f"fact:{fact_id}",
            document_id=filing.document_id,
            source_url=filing.source_url,
            title=label,
            locator="page:1",
            excerpt=excerpt,
            published_at=filing.filed_at,
            content_hash=content_hash,
        )
        evidence.append(row_ref)
        lineage = (
            SourceAmount(raw_value, "1", QuantityKind.MONEY, currency, (unit_ids[currency],)).normalize()
            if proven else None
        )
        year = int(filing.period_end[:4])
        period_start = None if concept in instant else f"{year}-01-01"
        facts.append(FinancialFact(
            fact_id=fact_id,
            company_cik=filing.company_cik,
            concept=concept,
            reported_concept=label,
            value=float(lineage.normalized_value) if lineage is not None else float(raw_value),
            unit=currency,
            fiscal_year=year,
            fiscal_period=filing.fiscal_period,
            form_type=filing.form_type,
            start_date=period_start,
            end_date=filing.period_end,
            filed_at=filing.filed_at,
            accession_number=filing.accession_number,
            source_url=filing.source_url,
            scope=scope,
            entity=filing.company_cik,
            market="CN_A",
            statement=statements[concept],
            period_start=period_start,
            consolidated_scope=scope,
            currency=currency,
            unit_scale=1.0,
            unit_provenance="explicit" if lineage is not None else "unknown",
            revision=filing.revision,
            source_document=filing.primary_document,
            source_page=1,
            raw_text=excerpt,
            parser_version="service-source-fixture-v1" if proven else PDF_PARSER_VERSION,
            validation_status="unvalidated",
            normalization_lineage=lineage,
        ))
    return filing, tuple(facts), tuple(evidence)


_RETRY_INLINE_FACTS = {
    "revenue": ("Revenues", "Revenue", "duration"),
    "net_income": ("ProfitLoss", "Profit for the year", "duration"),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "Net cash provided by operating activities",
        "duration",
    ),
    "assets": ("Assets", "Assets", "instant"),
    "liabilities": ("Liabilities", "Liabilities", "instant"),
    "equity": ("StockholdersEquity", "Stockholders' equity", "instant"),
}


def _retry_inline_xbrl_bytes(
    filing: FilingDocument,
    annual_values: dict[int, dict[str, str]],
) -> bytes:
    """Create a tiny, hash-bound inline-XBRL original for real pipeline tests."""
    years = sorted(set(annual_values) | {int(filing.period_end[:4])})
    contexts = []
    for year in years:
        contexts.extend((
            f"<x:context id='duration{year}'><x:entity>"
            f"<x:identifier scheme='http://www.sec.gov/CIK'>{filing.company_cik}</x:identifier>"
            f"</x:entity><x:period><x:startDate>{year}-01-01</x:startDate>"
            f"<x:endDate>{year}-12-31</x:endDate></x:period></x:context>",
            f"<x:context id='instant{year}'><x:entity>"
            f"<x:identifier scheme='http://www.sec.gov/CIK'>{filing.company_cik}</x:identifier>"
            f"</x:entity><x:period><x:instant>{year}-12-31</x:instant>"
            f"</x:period></x:context>",
        ))
    rows = []
    for year, values in sorted(annual_values.items(), reverse=True):
        for concept, raw_value in values.items():
            tag, label, period_kind = _RETRY_INLINE_FACTS[concept]
            context_id = f"{period_kind}{year}"
            rows.append(
                f"<tr><td>{label}</td><td><ix:nonFraction name='g:{tag}' "
                f"contextRef='{context_id}' unitRef='usd' "
                f"format='ixt:num-dot-decimal' decimals='0'>{raw_value}"
                f"</ix:nonFraction></td></tr>"
            )
    focus_year = int(filing.period_end[:4])
    document = f"""<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:x="http://www.xbrl.org/2003/instance"
      xmlns:dei="http://xbrl.sec.gov/dei/2025"
      xmlns:g="http://fasb.org/us-gaap/2025"
      xmlns:iso="http://www.xbrl.org/2003/iso4217"
      xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12">
      <head><title>Fixture annual report {focus_year}</title></head>
      <body><ix:header><ix:resources>{''.join(contexts)}
        <x:unit id="usd"><x:measure>iso:USD</x:measure></x:unit>
        <ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="duration{focus_year}">{focus_year}</ix:nonNumeric>
        <ix:nonNumeric name="dei:DocumentFiscalPeriodFocus" contextRef="duration{focus_year}">FY</ix:nonNumeric>
      </ix:resources></ix:header>
      <h2>Consolidated financial statements</h2>
      <table>{''.join(rows)}</table></body></html>"""
    return document.encode("utf-8")


class _OfflineInlineSecClient:
    """Official-source fixture: offline discovery/download, no CompanyFacts rows."""

    def __init__(
        self,
        company: Company,
        report_specs: tuple[tuple[str, int, dict[int, dict[str, str]]], ...],
    ) -> None:
        self.company = company
        self.report_specs = report_specs
        self.filings = tuple(
            FilingDocument(
                document_id=f"sec:{company.cik}:{accession}",
                company_cik=company.cik,
                accession_number=accession,
                form_type="10-K",
                fiscal_period="FY",
                period_end=f"{year}-12-31",
                filed_at=f"{year + 1}-02-01T00:00:00+00:00",
                primary_document=f"fixture-{year}.htm",
                source_url=(
                    "https://www.sec.gov/Archives/edgar/data/"
                    f"{int(company.cik)}/{accession.replace('-', '')}/fixture-{year}.htm"
                ),
            )
            for accession, year, _annual_values in report_specs
        )
        self.values_by_accession = {
            accession: values for accession, _year, values in report_specs
        }
        self.list_limits: list[int] = []
        self.downloaded_accessions: list[str] = []
        self.companyfacts_calls = 0

    def list_annual_filings(self, _company: Company, limit: int = 5) -> list[FilingDocument]:
        self.list_limits.append(limit)
        return [replace(item) for item in self.filings[:limit]]

    def get_company_facts(self, _company: Company) -> list[FinancialFact]:
        self.companyfacts_calls += 1
        return []

    def download_filing(self, filing: FilingDocument, target: Path) -> FilingDocument:
        raw = _retry_inline_xbrl_bytes(
            filing, self.values_by_accession[filing.accession_number]
        )
        target.mkdir(parents=True, exist_ok=True)
        output = target / Path(filing.primary_document).name
        output.write_bytes(raw)
        self.downloaded_accessions.append(filing.accession_number)
        return replace(
            filing,
            local_path=str(output),
            content_hash=hashlib.sha256(raw).hexdigest(),
        )


def _retry_inline_company() -> Company:
    return Company(
        cik="0000030075",
        ticker="FIXTURE",
        name="Inline Fixture Corp",
        exchange="NASDAQ",
        issuer_id="0000030075",
        market="US",
        security_id="US:NASDAQ:FIXTURE",
        listing_currency="USD",
        reporting_currency="USD",
        accounting_standard="US_GAAP",
    )


def _retry_inline_annual_values(year: int) -> dict[str, str]:
    growth = year - 2023
    revenue = 1_000_000 + growth * 100_000
    return {
        "revenue": str(revenue),
        "net_income": str(180_000 + growth * 30_000),
        "operating_cash_flow": str(230_000 + growth * 40_000),
        "assets": str(2_500_000 + growth * 250_000),
        "liabilities": str(1_400_000 + growth * 150_000),
        "equity": str(1_100_000 + growth * 100_000),
    }


def _retry_inline_report_specs(
    company: Company, years: tuple[int, ...],
) -> tuple[tuple[str, int, dict[int, dict[str, str]]], ...]:
    return tuple(
        (
            f"{company.cik}-{str(year + 1)[-2:]}-000001",
            year,
            {year: _retry_inline_annual_values(year)},
        )
        for year in years
    )


def _seed_retry_inline_cohort(
    service: AppService, company: Company, *, annual_history_years: int,
) -> tuple[dict[str, set[str]], list[str]]:
    """Seed persisted facts through the real offline Inline XBRL pipeline."""
    service.update_preferences({
        "sec_contact_profile": "personal",
        "sec_contact_email": "offline@example.test",
    })
    service.storage.save_company(company)
    trace = {
        "targets": set(), "downloaded": set(), "processed": set(),
        "warnings": set(), "diagnostics": set(), "freshness": set(),
        "terminal_state": set(), "next_action": set(),
    }
    errors = service._retry_us_financials(
        company,
        {"research_configuration": {"annual_history_years": annual_history_years}},
        trace=trace,
    )
    return trace, errors


def _assert_retry_inline_cohort(
    test_case: unittest.TestCase,
    service: AppService,
    company: Company,
    expected_accessions: set[str],
) -> None:
    filings = {
        item.accession_number: item
        for item in service.storage.get_filings(company.cik)
    }
    facts = service.storage.get_facts(company.cik)
    groups = service.storage.get_validation_groups(company.cik)
    evidence = {
        item["evidence_id"]: item
        for item in service.storage.get_financial_evidence()
    }
    test_case.assertEqual(set(filings), expected_accessions)
    test_case.assertEqual({item["accession_number"] for item in facts}, expected_accessions)
    test_case.assertEqual(len(facts), len(expected_accessions) * len(_RETRY_INLINE_FACTS))
    test_case.assertEqual({item["concept"] for item in facts}, set(_RETRY_INLINE_FACTS))
    test_case.assertTrue(all(item["validation_status"] == "VERIFIED" for item in facts))
    test_case.assertEqual(
        {item["accession_number"] for item in groups if item["status"] == "VERIFIED"},
        expected_accessions,
    )
    test_case.assertTrue(all(
        item["status"] == "VERIFIED"
        for item in groups if item["accession_number"] in expected_accessions
    ))
    for filing in filings.values():
        original = Path(filing.local_path).read_bytes()
        actual_hash = hashlib.sha256(original).hexdigest()
        test_case.assertEqual(filing.content_hash, actual_hash)
    for fact in facts:
        test_case.assertTrue(fact["normalization_lineage"])
        from openthesis.financial_xbrl_semantics import INLINE_PARSER_VERSION
        test_case.assertEqual(fact["parser_version"], INLINE_PARSER_VERSION)
        unit_evidence_id = fact["normalization_lineage"]["unit_evidence_ids"][0]
        ref = evidence[unit_evidence_id]
        test_case.assertEqual(ref["document_id"], filings[fact["accession_number"]].document_id)
        test_case.assertEqual(ref["content_hash"], filings[fact["accession_number"]].content_hash)


class AppServiceTests(unittest.TestCase):
    def _start_research_and_wait_for_worker(self, service: AppService, request: dict) -> tuple[dict, dict]:
        """Start a real research worker and keep its SQLite lifetime inside the fixture.

        These tests deliberately exercise the production download/recovery path.  The
        helper only makes the test-owned worker observable so a temporary database is
        not removed while that worker is still checkpointing.
        """
        worker_threads: list[threading.Thread] = []
        real_thread = threading.Thread

        def capture_research_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if str(kwargs.get("name", "")).startswith("openthesis-research-"):
                worker_threads.append(thread)
            return thread

        started = None
        worker: threading.Thread | None = None
        started_at = time.monotonic()
        deadline = started_at + 30
        try:
            with patch("openthesis.service.threading.Thread", side_effect=capture_research_thread):
                started = service.start_research(request)
            self.assertEqual(len(worker_threads), 1)
            worker = worker_threads[0]
            status = started
            terminal = {"completed", "failed", "cancelled"}
            while status["state"] not in terminal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = service.get_research_status(started["job_id"])
                    self.fail(
                        "research worker did not reach a terminal state within 30s; "
                        f"job state={status!r}"
                    )
                time.sleep(min(0.05, remaining))
                status = service.get_research_status(started["job_id"])
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            self.assertFalse(worker.is_alive(), "research worker did not exit after terminal state")
            self.assertLess(time.monotonic() - started_at, 30)
            return started, status
        except BaseException as failure:
            if started is not None and worker is not None and worker.is_alive():
                try:
                    service.cancel_research(started["job_id"])
                finally:
                    worker.join(timeout=5)
                if worker.is_alive():
                    raise AssertionError("research worker did not stop after cancellation") from failure
            raise

    def test_generic_stage_recovery_executes_planner_target_and_rejects_stale_plan(self) -> None:
        company = build_company("NVDA", "NVIDIA", market="US", reporting_currency="USD")
        model = {"configured_model_id": "configured.alt", "configuration_version": 3, "role": "primary"}
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(company)
            run = ResearchRun(
                run_id="recover-counter", company=company, workflow_id="test",
                research_pack_id="official.long-term-fundamentals", research_pack_version="1",
                provider_id="p", model_id="m", data_as_of="2026-09-29", status=RunStatus.PARTIAL,
            )
            service.storage.save_run(run)
            artifacts = [
                ResearchArtifact(
                    artifact_id=f"agent:{agent}", run_id=run.run_id,
                    artifact_type="agent-analysis", title=agent,
                    agent_id=agent, content={"trusted_state": "completed_verified"},
                )
                for agent in ("financial-analyst", "business-analyst", "accounting-risk-analyst")
            ]
            artifacts.extend([
                ResearchArtifact("financial", run.run_id, "deterministic-financial-summary", "Financials", {
                    "company": company.to_dict(), "evidence": [{"evidence_id": "filing:business", "kind": "filing_text",
                        "company_cik": company.cik, "document_id": "official", "source_url": "https://issuer.example/report",
                        "excerpt": "The company manufactures equipment."}]}),
                ResearchArtifact("dossier", run.run_id, "verified-research-dossier", "Dossier", {"verified_analyses": {"business-analyst": {}}}),
                ResearchArtifact("growth", run.run_id, "growth-opportunities", "Growth", {"opportunities": [{"title": "verified"}], "_validation": {"passed": True}}),
                ResearchArtifact("counter-failure", run.run_id, "stage-outcome", "Counter failed", {"stage": "counterarguments", "agent_id": "skeptical-analyst", "outcome": "waiting_retryable", "retryable": True, "error_code": "MODEL_RESPONSE_INVALID"}),
            ])
            for artifact in artifacts:
                service.storage.save_artifact(artifact)
            plan = service._recovery_planner.plan(service.storage.get_artifacts(run.run_id), target="counter-analysis")
            with patch.object(service, "_retry_research_model_stages_once", return_value={"resumed": True}) as resume:
                result = service.retry_research_stage(run.run_id, "counter-analysis", model, plan.plan_hash)
            self.assertEqual(result, {"resumed": True})
            resume.assert_called_once_with(run.run_id, model, resume_target="counter-analysis", manual_authorization_id="")

            with patch.object(service, "_retry_research_model_stages_once") as stale_resume:
                with self.assertRaisesRegex(ValueError, "MODEL_RECOVERY_PLAN_STALE"):
                    service.retry_research_stage(run.run_id, "counter-analysis", model, "stale-plan")
            stale_resume.assert_not_called()

    def test_model_reference_preserves_connection_quota_identity(self) -> None:
        config = _model_config_from_request({
            "configured_model_id": "openai-main.reasoner",
            "connection_id": "openai-main",
            "configuration_version": 3,
            "role": "primary",
        })
        self.assertEqual(config.connection_id, "openai-main")
        self.assertEqual(config.configured_model_id, "openai-main.reasoner")
        with self.assertRaisesRegex(ValueError, "connection id is invalid"):
            _model_config_from_request({
                "configured_model_id": "openai-main.reasoner",
                "connection_id": "openai/main",
            })

    def test_qualitative_evidence_cache_reuses_content_hash_across_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = AppService(Path(directory)).storage
            filing = FilingDocument(
                "cached-disclosure",
                "issuer",
                "accession",
                "ANNUAL_REPORT",
                "FY",
                "2025-12-31",
                "2026-03-01",
                "annual.pdf",
                "https://example.test/annual.pdf",
                content_hash="sha256:immutable-report",
            )
            evidence = [{
                "evidence_id": "filing:business",
                "document_id": filing.document_id,
                "source_url": filing.source_url,
                "title": "Annual · business",
                "locator": "page:1",
                "excerpt": "Business disclosure",
                "published_at": filing.filed_at,
            }]
            with patch("openthesis.financial_sources.disclosures.build_filing_evidence", return_value=evidence) as parser, \
                 patch("openthesis.financial_sources.disclosures.build_disclosure_index", return_value=[]):
                first = _cached_filing_evidence(storage, [filing])
                second = _cached_filing_evidence(storage, [filing])

            first = [item for item in first if item.get("kind") != "material_gap"]
            second = [item for item in second if item.get("kind") != "material_gap"]
            self.assertEqual(len(first), 1)
            for key, value in evidence[0].items():
                self.assertEqual(first[0].get(key), value)
            self.assertEqual(first[0]["company_cik"], filing.company_cik)
            self.assertEqual(first[0]["accession_number"], filing.accession_number)
            self.assertEqual(first[0]["end_date"], filing.period_end)
            self.assertEqual(second, first)
            parser.assert_called_once()

    def test_provider_failure_classification_uses_typed_code_not_message_language(self) -> None:
        code, retryable, timed_out = _classify_research_exception(
            ProviderError(
                "模型网关调用超过期限。",
                retryable=True,
                code="MODEL_TIMEOUT",
            )
        )
        self.assertEqual(code, "MODEL_TIMEOUT")
        self.assertTrue(retryable)
        self.assertTrue(timed_out)

        code, retryable, timed_out = _classify_research_exception(
            ProviderError(
                "upstream unavailable",
                retryable=True,
                code="MODEL_HTTP_503",
            )
        )
        self.assertEqual(code, "MODEL_HTTP_503")
        self.assertTrue(retryable)
        self.assertFalse(timed_out)

    def test_model_stage_resume_is_offered_only_when_no_base_output_would_repeat(self) -> None:
        failed = [{
            "artifact_type": "stage-outcome",
            "content": {
                "stage": "base-agents",
                "completed": 0,
                "retryable": True,
            },
        }]
        self.assertTrue(_model_stages_retryable(failed, {}))
        self.assertFalse(_model_stages_retryable([
            *failed,
            {"artifact_type": "agent-analysis", "content": {"claims": []}},
        ], {}))

    def test_synthesis_retryability_uses_source_capabilities_not_an_all_stage_gate(self) -> None:
        artifacts = [{"artifact_type": "deterministic-financial-summary", "content": {
            "company": {"cik": "issuer", "ticker": "ISS"},
            "evidence": [{"evidence_id": "filing:one", "kind": "filing_text", "company_cik": "issuer",
                "document_id": "doc:one", "source_url": "https://issuer.example/report.pdf",
                "excerpt": "The company manufactures industrial refrigeration equipment."}],
        }}]
        self.assertTrue(_report_retryable(artifacts))
        self.assertFalse(_report_retryable([]))
        self.assertFalse(_report_retryable([{"artifact_type": "deterministic-financial-summary", "content": {}}]))
        self.assertTrue(_report_retryable([
            *artifacts,
            {"artifact_type": "research-report", "content": {"retryable": False}},
        ]))

    def test_application_components_are_unique_views_of_injected_dependencies(self) -> None:
        market = _FakeMarketData()
        engine = object()
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), market_data=market, financial_ingestion_engine=engine)
            self.assertIs(service._disclosure_service.market_data, market)
            self.assertIs(service._financial_pipeline.engine, engine)
            self.assertIs(service._financial_source_service.ingestion, engine)
            self.assertIs(service._financial_ingestion, service._financial_source_service.ingestion)
            self.assertIs(service._financial_recognition, service._financial_source_service.recognition)
            self.assertIs(service._financial_recovery, service._financial_source_service.recovery)
            self.assertIs(service._research_orchestrator.storage, service.storage)

    def test_research_orchestrator_transparently_forwards_factory_and_workflow_options(self) -> None:
        from openthesis.providers import ModelConfig
        from unittest.mock import patch

        config = ModelConfig(configured_model_id="fixture.model", configuration_version=7, role="comparison")
        provider = object()
        factory_calls: list[object] = []

        def factory(received):
            factory_calls.append(received)
            return provider

        orchestrator = ResearchOrchestrator(object(), factory)
        with patch("openthesis.application_services.ResearchWorkflow", return_value="workflow") as workflow_ctor:
            result = orchestrator.create(
                "pack", config, report_language="en", ui_language="zh-Hant",
                parallel_agents=True,
            )
        self.assertEqual(result, "workflow")
        self.assertEqual(factory_calls, [config])
        self.assertEqual(workflow_ctor.call_args.args[:4], (orchestrator.storage, "pack", provider, config))
        self.assertEqual(workflow_ctor.call_args.kwargs["report_language"], "en")
        self.assertEqual(workflow_ctor.call_args.kwargs["ui_language"], "zh-Hant")

    def test_report_service_renders_each_language_from_same_canonical_artifacts(self) -> None:
        seen: list[tuple[str, tuple[dict, ...], str]] = []
        artifacts = [{
            "artifact_id": "canonical-1",
            "artifact_type": "research-report",
            "content": {"facts": [{"concept": "revenue", "value": "100"}], "metadata": {"source": "canonical"}},
        }]
        canonical_artifacts = deepcopy(artifacts)

        def markdown(run_id, items, **kwargs):
            seen.append(("markdown", deepcopy(items), kwargs["language"]))
            items[0]["content"]["facts"][0]["value"] = "mutated by markdown"
            return run_id + ":md"

        def html(run_id, items, **kwargs):
            seen.append(("html", deepcopy(items), kwargs["language"]))
            items[0]["content"]["metadata"]["source"] = "mutated by html"
            return run_id + ":html"

        renderer = ReportService(markdown, html)
        for language in ("zh-CN", "zh-Hant", "en"):
            result = renderer.render("run-1", artifacts, language=language, company_name="Fixture")
            self.assertEqual(result, ("run-1:md", "run-1:html"))
        self.assertEqual(
            seen,
            [(format_name, tuple(canonical_artifacts), language)
             for language in ("zh-CN", "zh-Hant", "en")
             for format_name in ("markdown", "html")],
        )
        self.assertEqual([entry[2] for entry in seen], ["zh-CN", "zh-CN", "zh-Hant", "zh-Hant", "en", "en"])
        self.assertEqual(artifacts, canonical_artifacts)

    def test_financial_diagnostics_is_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = build_company("09988.HK", "BABA", reporting_currency="CNY")
            service.storage.save_company(company)
            service.storage.save_run(ResearchRun(
                run_id="diagnostics-redacted", company=company, workflow_id="test",
                research_pack_id="builtin", research_pack_version="1", provider_id="provider",
                model_id="model", data_as_of=utc_now_iso(), status=RunStatus.FAILED,
                model_configuration={"api_key": "do-not-export", "endpoint": "https://secret.invalid"},
                data_snapshot={"local_path": "C:\\Users\\admin\\secret.pdf"},
            ))
            diagnostics = service.financial_diagnostics("diagnostics-redacted")
            encoded = json.dumps(diagnostics, ensure_ascii=False)
            self.assertEqual(diagnostics["schema"], "openthesis.financial-diagnostics.v1")
            self.assertNotIn("do-not-export", encoded)
            self.assertNotIn("secret.pdf", encoded)
            self.assertNotIn("api_key", encoded)

    def test_filing_cache_reuse_requires_source_revision_and_period_identity(self) -> None:
        base = FilingDocument(
            "doc", "cik", "acc", "10-K", "FY", "2025-12-31", "2026-02-01",
            "annual.htm", "https://www.sec.gov/Archives/acc/annual.htm",
            revision="original",
        )
        self.assertTrue(_filing_identity_matches(base, FilingDocument(**base.to_dict())))
        for field, value in (
            ("source_url", "https://www.sec.gov/Archives/acc/revised.htm"),
            ("revision", "amended"),
            ("period_end", "2024-12-31"),
        ):
            changed = FilingDocument(**{**base.to_dict(), field: value})
            self.assertFalse(_filing_identity_matches(changed, base), field)
    def test_unsafe_financial_write_has_localized_fail_closed_guidance(self) -> None:
        for language, marker in (("zh-CN", "安全软件"), ("zh-Hant", "安全軟體"), ("en", "Security software")):
            message = _research_data_message("FILING_CONTENT_UNSAFE", language)
            self.assertIn(marker, message)
            self.assertNotIn("FILING_CONTENT_UNSAFE", message)

    def test_scanned_filing_diagnostics_have_actionable_localized_messages(self) -> None:
        self.assertEqual(
            _scanner_diagnostic_code(("filing:SCANNED_IMAGE_FILING_DETECTED",)),
            "SCANNED_IMAGE_FILING_DETECTED",
        )
        self.assertEqual(
            _scanner_diagnostic_code(("filing:SCANNED_IMAGE_LAYOUT_UNRESOLVED",)),
            "SCANNED_IMAGE_LAYOUT_UNRESOLVED",
        )
        for language in ("zh-CN", "zh-Hant", "en"):
            detected_message = _research_data_message(
                "SCANNED_IMAGE_FILING_DETECTED", language
            )
            unresolved_message = _research_data_message(
                "SCANNED_IMAGE_LAYOUT_UNRESOLVED", language
            )
            self.assertTrue(detected_message and unresolved_message)
            self.assertNotEqual(detected_message, unresolved_message)
            self.assertNotIn("SCANNED_IMAGE_", detected_message)
            self.assertNotIn("SCANNED_IMAGE_", unresolved_message)
            if language == "en":
                self.assertIn("scanned", detected_message.lower())
                self.assertIn("could not be located safely", unresolved_message.lower())

    def test_durable_official_source_bounds_transient_discovery_without_provider(self) -> None:
        calls = 0
        provider_calls: list[str] = []

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5):
                nonlocal calls
                calls += 1
                raise MarketDataError("temporary official source failure")

        with tempfile.TemporaryDirectory() as directory:
            company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter(), [company]),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            started = service.start_research({
                "mode": "company", "company": company.to_dict(),
                "download_filings": True,
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
            })
            deadline = time.monotonic() + 10
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, f"status={status}")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            self.assertIsNotNone(status["run_id"])
            self.assertIsNotNone(service.storage.get_run(status["run_id"]))
            self.assertIn("continuity", service.get_report(status["run_id"]))
            self.assertEqual(status["error_code"], "FILING_FETCH_FAILED")
            self.assertEqual(calls, 3, "one initial discovery plus at most two transient retries")
            self.assertEqual(provider_calls, [])

    def test_financial_retry_job_keeps_operation_result_when_report_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.retry_financials = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                _FinancialReportRefreshError({
                    "mode": "retry", "status": "partial", "accepted": ["fact:revenue"],
                    "error": "FILING_REPORT_REFRESH_FAILED",
                })
            )
            job = _ResearchJob("refresh-failed")
            service._run_financial_retry_job(job, "run-refresh-failed", False)
            snapshot = job.snapshot()
            self.assertEqual(snapshot["state"], "failed")
            self.assertEqual(snapshot["stage"], "report-refresh")
            self.assertEqual(snapshot["error_code"], "FILING_REPORT_REFRESH_FAILED")
            self.assertEqual(snapshot["operation_result"]["status"], "partial")
            self.assertEqual(snapshot["operation_result"]["accepted"], ["fact:revenue"])

    def test_job_records_per_stage_timings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            job = _ResearchJob("timed")
            service._update_job(job, state="running", stage="filing-download")
            time.sleep(0.01)
            service._update_job(job, stage="filing-parse")
            snapshot = job.snapshot()

            self.assertGreaterEqual(snapshot["stage_timings"]["filing-download"], 0.009)
            self.assertEqual(snapshot["stage"], "filing-parse")
            self.assertIn("stage_elapsed_seconds", snapshot)

    def test_download_helper_enforces_global_and_per_host_limits_with_deterministic_results(self) -> None:
        started: list[str] = []
        active = 0
        active_by_host: dict[str, int] = {}
        maximum = [0, 0]
        lock = threading.Lock()

        class Adapter:
            def download_filing(self, filing: FilingDocument, _target: Path) -> FilingDocument:
                nonlocal active
                host = filing.source_url.split("//", 1)[1].split("/", 1)[0]
                with lock:
                    started.append(filing.document_id)
                    active += 1
                    active_by_host[host] = active_by_host.get(host, 0) + 1
                    maximum[0] = max(maximum[0], active)
                    maximum[1] = max(maximum[1], active_by_host[host])
                time.sleep(0.01)
                with lock:
                    active -= 1
                    active_by_host[host] -= 1
                return filing

        filings = [_download_filing(f"f{index}", "a.example.test" if index < 4 else "b.example.test") for index in range(6)]
        progress: list[tuple[int, int]] = []
        downloaded, failures = _bounded_download_filings(
            Adapter(), filings, Path("."), progress=lambda done, total: progress.append((done, total))
        )

        self.assertEqual([item.document_id for item in downloaded], [item.document_id for item in filings])
        self.assertEqual(failures, [])
        self.assertLessEqual(maximum[0], 3)
        self.assertLessEqual(maximum[1], 2)
        self.assertEqual(progress[-1], (len(filings), len(filings)))

    def test_download_helper_deduplicates_urls_retries_once_and_keeps_partial_success(self) -> None:
        calls: dict[str, int] = {}

        class Adapter:
            def download_filing(self, filing: FilingDocument, _target: Path) -> FilingDocument:
                calls[filing.source_url] = calls.get(filing.source_url, 0) + 1
                if filing.document_id == "retry" and calls[filing.source_url] == 1:
                    raise MarketDataError("temporary", code="FILING_FETCH_FAILED")
                if filing.document_id == "bad":
                    raise OSError("permanent")
                return filing

        duplicate = _download_filing("duplicate", "a.example.test", url="https://a.example.test/shared.pdf")
        filings = [
            duplicate,
            _download_filing("retry", "a.example.test"),
            _download_filing("bad", "b.example.test"),
            _download_filing("other", "b.example.test"),
        ]
        downloaded, failures = _bounded_download_filings(Adapter(), filings, Path("."))

        self.assertEqual([item.document_id for item in downloaded], ["duplicate", "retry", "other"])
        self.assertEqual([item.document_id for item, _ in failures], ["bad"])
        self.assertEqual(calls["https://a.example.test/shared.pdf"], 1)
        self.assertEqual(calls["https://a.example.test/retry.pdf"], 2)
        self.assertEqual(calls["https://b.example.test/bad.pdf"], 1)

    def test_download_helper_cancel_prevents_queued_work(self) -> None:
        cancel = threading.Event()
        cancel.set()
        calls: list[str] = []

        class Adapter:
            def download_filing(self, filing: FilingDocument, _target: Path) -> FilingDocument:
                calls.append(filing.document_id)
                return filing

        downloaded, failures = _bounded_download_filings(
            Adapter(), [_download_filing("one", "a.example.test"), _download_filing("two", "b.example.test")], Path("."), cancel_event=cancel
        )
        self.assertEqual(downloaded, [])
        self.assertEqual(failures, [])
        self.assertEqual(calls, [])

    def test_research_history_years_is_bounded_and_defaults_to_five(self) -> None:
        self.assertEqual(_research_history_years({}), 5)
        self.assertEqual(_research_history_years({"evidence_policy": {"annual_history_years": 2}}), 2)
        self.assertEqual(_research_history_years({"evidence_policy": {"annual_history_years": 100}}), 10)
        self.assertEqual(_research_history_years({"evidence_policy": {"annual_history_years": 1}}), 2)

    def test_research_history_uses_pack_policy_and_request_override(self) -> None:
        class Pack:
            workflow = {"settings": {"evidence_policy": {"annual_history_years": 7}}}

        self.assertEqual(_research_history_years({}, Pack()), 7)
        self.assertEqual(
            _research_history_years(
                {"evidence_policy": {"annual_history_years": 3}}, Pack()
            ),
            3,
        )
    def test_job_leaves_download_stage_before_financial_ingestion_blocks(self) -> None:
        company = build_company("002594.SZ", "BYD")
        filing = FilingDocument(
            "q1-26", company.security_id, "q1-26", "QUARTERLY_REPORT", "Q1",
            "2026-03-31", "2026-04-28", "2026 Q1.pdf", "https://example.test/q1-26.pdf",
        )
        entered = threading.Event()
        release = threading.Event()
        download_entered = threading.Event()
        download_release = threading.Event()

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [filing][:limit]
            def download_filing(self, item, _target):
                download_entered.set()
                download_release.wait(timeout=2)
                return _download_offline_filing_fixture(item, _target)

        class BlockingEngine:
            def ingest(self, _company, _filings, **_kwargs):
                entered.set()
                release.wait(timeout=2)
                validation = FinancialValidation(
                    ValidationStatus.REJECTED, ("no_financial_facts",), frozenset(), (), ()
                )
                manifest = FilingManifest(
                    filing.document_id, filing.accession_number, filing.source_url,
                    filing.primary_document, filing.form_type, filing.fiscal_period,
                    filing.period_end, filing.revision, filing.supersedes_document_id,
                    filing.content_hash,
                )
                return FinancialDataset((), (), (manifest,), validation, (), ())

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(Adapter(), [company]),
                financial_ingestion_engine=BlockingEngine(),
            )
            started = service.start_research({
                "mode": "company",
                "company": company.to_dict(),
                "download_filings": True,
                "model": {},
            })
            self.assertTrue(download_entered.wait(timeout=2), "download did not start")
            downloading = service.get_research_status(started["job_id"])
            self.assertEqual(downloading["stage"], "filing-download")
            self.assertEqual(downloading["stage_current"], 0)
            self.assertEqual(downloading["stage_total"], 1)
            download_release.set()
            self.assertTrue(entered.wait(timeout=2), "ingestion did not start")
            status = service.get_research_status(started["job_id"])
            self.assertEqual(status["stage"], "filing-parse")
            self.assertEqual(status["stage_current"], 0)
            self.assertEqual(status["stage_total"], 1)
            self.assertGreaterEqual(status["percent"], 18)
            release.set()
            deadline = time.monotonic() + 3
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "blocked ingestion did not settle")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

    def test_job_progress_is_monotonic_and_stage_counts_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            job = _ResearchJob("progress-job", percent=40)

            service._update_job(
                job,
                stage="filing-parse",
                percent=18,
                stage_current=8,
                stage_total=6,
            )

            self.assertEqual(job.percent, 40)
            self.assertEqual(job.stage_current, 6)
            self.assertEqual(job.stage_total, 6)
            service._ingestion_progress(
                job,
                "filing-status",
                1,
                6,
                {
                    "filing_id": "annual-2025",
                    "label": "2025 Annual Report",
                    "status": "indexing",
                    "elapsed_seconds": 1.25,
                },
            )
            detailed = job.snapshot()
            self.assertEqual(detailed["stage"], "filing-parse")
            self.assertEqual(
                detailed["filing_states"]["annual-2025"]["status"], "indexing"
            )
            self.assertIn("engine_active_seconds", detailed)
            self.assertIn("external_wait_seconds", detailed)
            service._update_job(job, percent=100)
            self.assertEqual(job.percent, 100)

    def test_vision_requires_consent_without_accepting_session_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory_calls = []
            service = AppService(
                Path(directory),
                vision_adapter_factory=lambda config: factory_calls.append(config) or object(),
            )
            request = {
                "mode": "demo",
                "model": {},
                "vision_fallback": {
                    "enabled": True,
                    "consent": False,
                    "model": {
                        "configured_model_id": "test.vision",
                        "configuration_version": 2,
                        "role": "vision",
                    },
                },
            }
            self.assertEqual(_request_secrets(request), ())
            job = service.start_research(request)
            deadline = time.time() + 3
            status = service.get_research_status(job["job_id"])
            while status["state"] in {"queued", "running"} and time.time() < deadline:
                time.sleep(0.01)
                status = service.get_research_status(job["job_id"])
            self.assertEqual(status["error_code"], "VISION_CONSENT_REQUIRED")
            self.assertEqual(factory_calls, [])

    def test_mineru_flash_request_has_no_secret_or_model_and_forces_page_approval(self) -> None:
        config = _vision_config_from_request({
            "enabled": True,
            "consent": True,
            "provider": "mineru_flash",
            "require_page_approval": True,
        })
        self.assertIsNotNone(config)
        self.assertEqual(config.provider, "mineru_flash")
        self.assertEqual(config.configured_model_id, "")
        self.assertTrue(config.require_page_approval)
        self.assertFalse(hasattr(config, "token"))
        self.assertFalse(hasattr(config, "api_key"))

        for secret_name in ("token", "api_key", "endpoint", "model_id"):
            with self.subTest(secret_name=secret_name):
                with self.assertRaises(ValueError):
                    _vision_config_from_request({
                        "enabled": True,
                        "consent": True,
                        "provider": "mineru_flash",
                        "require_page_approval": True,
                        secret_name: "must-not-enter-python",
                    })

    def test_vision_approval_modes_are_explicit_and_batch_is_run_scoped(self) -> None:
        for mode in ("review_each_plan", "approve_current_research"):
            config = _vision_config_from_request({
                "enabled": True,
                "consent": True,
                "provider": "mineru_flash",
                "require_page_approval": True,
                "approval_mode": mode,
            })
            self.assertEqual(config.approval_mode, mode)

        batch = _vision_config_from_request({
            "enabled": True,
            "consent": True,
            "provider": "mineru_flash",
            "require_page_approval": True,
            "approval_mode": "approve_current_research",
        })
        summary = VisionUploadPlan(
            provider="mineru_flash", filing_hash="selected-hash", source_document="report.pdf",
            pages=(VisionPageRequest(4, b"a" * 512), VisionPageRequest(2, b"b" * 512)),
        ).safe_summary()
        self.assertTrue(_vision_batch_upload_allowed(
            summary, batch, {"selected-hash"}, job_active=True, cancelled=False,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            summary, batch, {"other-hash"}, job_active=True, cancelled=False,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            summary, batch, {"selected-hash"}, job_active=False, cancelled=False,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            summary, batch, {"selected-hash"}, job_active=True, cancelled=True,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            {**summary, "provider": "configured_model"}, batch, {"selected-hash"},
            job_active=True, cancelled=False,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            {**summary, "total_bytes": batch.max_bytes + 1}, batch,
            {"selected-hash"}, job_active=True, cancelled=False,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            {**summary, "pages": (4, 2, 4), "document_hashes": ("hash-4", "hash-2", "hash-4")},
            batch, {"selected-hash"}, job_active=True, cancelled=False,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            {**summary, "document_hashes": ("hash-4",)},
            batch, {"selected-hash"}, job_active=True, cancelled=False,
        ))

    def test_vision_batch_approval_ledger_is_idempotent_and_run_bounded(self) -> None:
        batch = VisionFallbackConfig(
            enabled=True, consent=True, provider="mineru_flash",
            require_page_approval=True, approve_upload=lambda _: True,
            max_pages=2, max_bytes=10 * 1024 * 1024,
            approval_mode="approve_current_research",
        )
        first = VisionUploadPlan(
            provider="mineru_flash", filing_hash="selected-hash", source_document="report.pdf",
            pages=(VisionPageRequest(4, b"a" * (6 * 1024 * 1024)),),
        ).safe_summary()
        second = VisionUploadPlan(
            provider="mineru_flash", filing_hash="selected-hash", source_document="report.pdf",
            pages=(VisionPageRequest(5, b"b" * (6 * 1024 * 1024)),),
        ).safe_summary()
        ledger: dict[str, tuple[str, int, int]] = {}
        self.assertTrue(_vision_batch_upload_allowed(
            first, batch, {"selected-hash"}, job_active=True, cancelled=False,
            approved_plans=ledger,
        ))
        self.assertEqual(len(ledger), 1)
        self.assertTrue(_vision_batch_upload_allowed(
            first, batch, {"selected-hash"}, job_active=True, cancelled=False,
            approved_plans=ledger,
        ))
        self.assertEqual(len(ledger), 1)
        self.assertFalse(_vision_batch_upload_allowed(
            {**first, "total_bytes": first["total_bytes"] + 1}, batch,
            {"selected-hash"}, job_active=True, cancelled=False, approved_plans=ledger,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            second, batch, {"selected-hash"}, job_active=True, cancelled=False,
            approved_plans=ledger,
        ))
        self.assertEqual(len(ledger), 1)
        self.assertFalse(_vision_batch_upload_allowed(
            {**first, "plan_id": "plan-c"}, batch, {"selected-hash"},
            job_active=True, cancelled=True, approved_plans=ledger,
        ))
        self.assertEqual(len(ledger), 1)

    def test_vision_batch_approval_ledger_enforces_cumulative_page_cap(self) -> None:
        batch = VisionFallbackConfig(
            enabled=True, consent=True, provider="mineru_flash",
            require_page_approval=True, approve_upload=lambda _: True,
            max_pages=1, max_bytes=10 * 1024 * 1024,
            approval_mode="approve_current_research",
        )
        first = VisionUploadPlan(
            provider="mineru_flash", filing_hash="selected-hash", source_document="report.pdf",
            pages=(VisionPageRequest(4, b"a"),),
        ).safe_summary()
        second = VisionUploadPlan(
            provider="mineru_flash", filing_hash="selected-hash", source_document="report.pdf",
            pages=(VisionPageRequest(5, b"b"),),
        ).safe_summary()
        ledger: dict[str, tuple[str, int, int]] = {}
        self.assertTrue(_vision_batch_upload_allowed(
            first, batch, {"selected-hash"}, job_active=True, cancelled=False,
            approved_plans=ledger,
        ))
        self.assertFalse(_vision_batch_upload_allowed(
            second, batch, {"selected-hash"}, job_active=True, cancelled=False,
            approved_plans=ledger,
        ))
        self.assertEqual(len(ledger), 1)

    def test_vision_journal_persists_only_safe_task_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = build_company("688981.SH", "SMIC")
            service.storage.save_company(company)
            row = service.storage.save_vision_task(
                "task-key", company_cik=company.cik, document_id="filing-1",
                provider="mineru_flash", page_hashes=["page-hash"], page_numbers=[7],
                status="uploaded", remote_task_id="remote-task",
            )
            self.assertEqual(row["page_hashes"], ["page-hash"])
            self.assertEqual(row["page_numbers"], [7])
            self.assertEqual(service.storage.get_vision_task("task-key")["status"], "uploaded")
            service.storage.save_vision_task(
                "task-key", company_cik=company.cik, document_id="filing-1",
                provider="mineru_flash", page_hashes=["page-hash"], page_numbers=[7],
                status="running",
            )
            self.assertEqual(
                service.storage.get_vision_task("task-key")["remote_task_id"],
                "remote-task",
            )
            service.storage.save_vision_result("result-key", {"facts": [{"value": 1}]})
            self.assertEqual(
                service.storage.get_vision_result("result-key"),
                {"facts": [{"value": 1}]},
            )
            self.assertNotIn("pdf_bytes", row)
            self.assertNotIn("authorization", row)
            self.assertNotIn("signed_url", row)
        with self.assertRaises(ValueError):
            _vision_config_from_request({"enabled": True, "consent": True, "provider": "mineru_flash", "require_page_approval": False})

            self.assertNotIn("api_key", json.dumps(request))
    def test_vision_decision_is_sidecar_safe_and_snapshot_hides_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            job = _ResearchJob("vision-job")
            job.stage = "vision-approval"
            job.vision_approval_pending = True
            job.vision_upload_preview = {"provider": "mineru_flash", "pages": (3,), "total_bytes": 10, "source_document": "annual.pdf", "filing_hash": "abc"}
            with service._jobs_lock:
                service._jobs[job.job_id] = job
            snapshot = service.vision_decision(job.job_id, True)
            self.assertTrue(snapshot["vision_approval"])
            self.assertNotIn("local_path", snapshot["vision_upload_preview"])
            self.assertNotIn("source_url", snapshot["vision_upload_preview"])
            self.assertNotIn("api_key", snapshot["vision_upload_preview"])
            with self.assertRaises(ValueError):
                service.vision_decision(job.job_id, False)
    def test_latest_annual_fy_anchor_survives_interim_research_target(self) -> None:
        facts = tuple(SimpleNamespace(concept=concept) for concept in (
            "revenue", "net_income", "operating_cash_flow",
            "assets", "liabilities", "equity",
        ))
        annual = FactGroupValidation(
            ("fy", "2025-12-31", "FY", "consolidated", "CNY"),
            ValidationStatus.VERIFIED.value, accepted=facts,
        )
        interim = FactGroupValidation(
            ("q1", "2026-03-31", "Q1", "consolidated", "CNY"),
            ValidationStatus.VERIFIED.value, accepted=facts,
        )
        selected = _latest_annual_validations((annual, interim), "2025-12-31")
        self.assertEqual(selected, [annual])

    def test_latest_annual_rejected_does_not_fall_back_to_old_verified_group(self) -> None:
        company = build_company("300750.SZ", "CATL")
        old = FilingDocument(
            document_id="old", company_cik=company.security_id, accession_number="old",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2024-12-31",
            filed_at="2025-03-01", primary_document="old.pdf", source_url="https://example.test/old",
        )
        latest = FilingDocument(
            document_id="latest", company_cik=company.security_id, accession_number="latest",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2025-12-31",
            filed_at="2026-03-01", primary_document="latest.pdf", source_url="https://example.test/latest",
        )
        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [old, latest][:limit]
            def download_filing(self, item, target):
                return _download_bound_financial_fixture(item, target)[0]
        rejected = FinancialValidation(
            ValidationStatus.REJECTED, ("core_coverage_insufficient",), frozenset(), (), ()
        )
        verified = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (), ())
        class FakeEngine:
            def ingest(self, _company, _filings):
                manifests = tuple(
                    FilingManifest(f.document_id, f.accession_number, f.source_url, f.primary_document,
                                   f.form_type, f.fiscal_period, f.period_end, f.revision,
                                   f.supersedes_document_id, f.content_hash)
                    for f in _filings
                )
                return FinancialDataset(
                    (), (), manifests, FinancialValidation(ValidationStatus.READY_WITH_WARNINGS, (), frozenset(), (), ()), (),
                    (FinancialGroupValidation(("old", "2024-12-31", "FY", "consolidated", "CNY"), verified),
                     FinancialGroupValidation(("latest", "2025-12-31", "FY", "consolidated", "CNY"), rejected)),
                )
        provider_calls = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter(), [company]),
                financial_ingestion_engine=FakeEngine(),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            started = service.start_research({
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
            })
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])
            self.assertEqual(status["error_code"], "FILING_DATA_QUALITY_FAILED")
            self.assertEqual(provider_calls, [])

    def test_latest_verified_partial_financial_data_continues_to_provider(self) -> None:
        company = build_company("300750.SZ", "CATL")
        filing = FilingDocument(
            "latest", company.security_id, "latest", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "latest.pdf", "https://example.test/latest",
        )
        source_data = {}
        rows = (("revenue-only", "revenue", "Revenue", "100", "CNY", "consolidated", True),)

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [filing][:limit]
            def download_filing(self, item, target):
                downloaded, facts, evidence = _download_bound_financial_fixture(
                    item, target, rows,
                    disclosure=("Our business serves domestic vehicle manufacturers through a nationwide sales network "
                                "and develops new battery products."),
                )
                source_data[downloaded.document_id] = (facts, evidence)
                return downloaded

        class Engine:
            def ingest(self, _company, _filings):
                actual_filing = _filings[0]
                facts, evidence = source_data[actual_filing.document_id]
                manifest = FilingManifest(
                    actual_filing.document_id, actual_filing.accession_number, actual_filing.source_url,
                    actual_filing.primary_document, actual_filing.form_type, actual_filing.fiscal_period,
                    actual_filing.period_end, actual_filing.revision, actual_filing.supersedes_document_id,
                    actual_filing.content_hash,
                )
                validation = FinancialValidation(
                    ValidationStatus.VERIFIED, (), frozenset({"revenue"}), facts, ()
                )
                group = FinancialGroupValidation(
                    (actual_filing.accession_number, actual_filing.period_end, "FY", "consolidated", "CNY"), validation
                )
                return FinancialDataset(facts, evidence, (manifest,), validation, (), (group,))

        provider_calls: list[tuple[str, str, int]] = []
        generation_calls: list[str] = []
        provider_inputs: list[str] = []
        stage_calls: list[tuple[str, str]] = []

        class Provider:
            def __init__(self, role: str):
                self.role = role

            def generate(self, *args, **kwargs):
                generation_calls.append(self.role)
                payload = json.dumps({"args": args, "kwargs": kwargs}, ensure_ascii=False, default=str)
                provider_inputs.append(payload)
                stage_calls.append((self.role, payload))
                return {"claims": []}

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter(), [company]),
                financial_ingestion_engine=Engine(),
                provider_factory=lambda config: (
                    provider_calls.append((config.public_id, config.role, config.configuration_version)),
                    Provider(config.role),
                )[1],
            )
            started, status = self._start_research_and_wait_for_worker(service, {
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
            })
            self.assertEqual(status["state"], "completed")
            self.assertTrue(status["run_id"])
            self.assertEqual(
                provider_calls,
                [("gateway:test.primary@1", "primary", 1)],
            )
            # Preparation freezes once; each configured model creates one
            # workflow shared by the ready source and financial stages.
            self.assertEqual(generation_calls.count("primary"), len(generation_calls))
            self.assertTrue(generation_calls)
            self.assertTrue(any("Our business serves domestic vehicle manufacturers" in item
                                for item in provider_inputs))
            self.assertTrue(any("revenue-only" in item for item in provider_inputs))
            report = service.get_report(status["run_id"])
            self.assertEqual(report["financial_status"]["state"], "warning")
            admitted = service.storage.get_facts(company.security_id)
            self.assertEqual([item["concept"] for item in admitted], ["revenue"])

    def test_parent_and_wrong_currency_groups_never_enter_research_facts(self) -> None:
        company = build_company("300750.SZ", "CATL")
        filing = FilingDocument(
            "latest", company.security_id, "latest", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "latest.pdf", "https://example.test/latest",
        )
        rows = (
            ("core-revenue", "revenue", "Revenue", "100", "CNY", "consolidated", True),
            ("core-net_income", "net_income", "Net income", "20", "CNY", "consolidated", True),
            ("core-operating_cash_flow", "operating_cash_flow", "Operating cash flow", "25", "CNY", "consolidated", True),
            ("core-assets", "assets", "Total assets", "200", "CNY", "consolidated", True),
            ("core-liabilities", "liabilities", "Total liabilities", "80", "CNY", "consolidated", True),
            ("core-equity", "equity", "Equity", "120", "CNY", "consolidated", True),
            ("parent-revenue", "revenue", "Parent-only revenue", "90", "CNY", "parent", True),
            ("foreign-revenue", "revenue", "Foreign revenue", "80", "USD", "consolidated", True),
        )
        source_data = {}
        core: tuple[FinancialFact, ...] = ()
        all_facts: tuple[FinancialFact, ...] = ()
        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [filing][:limit]
            def download_filing(self, item, target):
                nonlocal core, all_facts
                downloaded, facts, evidence = _download_bound_financial_fixture(item, target, rows)
                source_data[downloaded.document_id] = (facts, evidence)
                all_facts = facts
                core = tuple(fact for fact in facts if fact.fact_id.startswith("core-"))
                return downloaded
        class Engine:
            def ingest(self, _company, _filings):
                actual_filing = _filings[0]
                facts, evidence = source_data[actual_filing.document_id]
                manifest = FilingManifest(
                    actual_filing.document_id, actual_filing.accession_number, actual_filing.source_url,
                    actual_filing.primary_document, actual_filing.form_type, actual_filing.fiscal_period,
                    actual_filing.period_end, actual_filing.revision, actual_filing.supersedes_document_id,
                    actual_filing.content_hash,
                )
                grouped = {}
                for item in facts:
                    grouped.setdefault((item.consolidated_scope, item.currency), []).append(item)
                groups = tuple(
                    FinancialGroupValidation(
                        (actual_filing.accession_number, actual_filing.period_end, "FY", scope, currency),
                        FinancialValidation(ValidationStatus.VERIFIED, (),
                                            frozenset(item.concept for item in items), tuple(items), ()),
                    )
                    for (scope, currency), items in grouped.items()
                )
                aggregate = FinancialValidation(
                    ValidationStatus.VERIFIED, (), frozenset(item.concept for item in facts), facts, ()
                )
                return FinancialDataset(facts, evidence, (manifest,), aggregate, (), groups)
        class Provider:
            def generate(self, *_args, **_kwargs): return {"claims": []}
        with tempfile.TemporaryDirectory() as directory, \
             patch("openthesis.financial_sources.disclosures.build_filing_evidence", return_value=[]), \
             patch("openthesis.financial_sources.disclosures.build_disclosure_index", return_value=[]):
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter(), [company]),
                financial_ingestion_engine=Engine(), provider_factory=lambda _config: Provider(),
            )
            started, status = self._start_research_and_wait_for_worker(service, {
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
            })
            self.assertEqual(status["state"], "completed")
            saved = service.storage.get_facts(company.security_id)
            self.assertEqual({item["fact_id"] for item in saved}, {item.fact_id for item in core})
            self.assertEqual(
                {item["fact_id"] for item in service.storage.get_facts_audit(company.security_id)},
                {item.fact_id for item in all_facts},
            )

    def test_latest_annual_verified_allows_old_rejected_but_only_accepted_facts_continue(self) -> None:
        company = build_company("300750.SZ", "CATL")
        old = FilingDocument(
            document_id="old", company_cik=company.security_id, accession_number="old",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2024-12-31",
            filed_at="2025-03-01", primary_document="old.pdf", source_url="https://example.test/old",
        )
        latest = FilingDocument(
            document_id="latest", company_cik=company.security_id, accession_number="latest",
            form_type="ANNUAL_REPORT", fiscal_period="FY", period_end="2025-12-31",
            filed_at="2026-03-01", primary_document="latest.pdf", source_url="https://example.test/latest",
        )
        rows_by_accession = {
            "latest": (
                ("accepted-revenue", "revenue", "Revenue", "100", "CNY", "consolidated", True),
                ("accepted-net_income", "net_income", "Net income", "20", "CNY", "consolidated", True),
                ("accepted-operating_cash_flow", "operating_cash_flow", "Operating cash flow", "25", "CNY", "consolidated", True),
                ("accepted-assets", "assets", "Total assets", "200", "CNY", "consolidated", True),
                ("accepted-liabilities", "liabilities", "Total liabilities", "80", "CNY", "consolidated", True),
                ("accepted-equity", "equity", "Equity", "120", "CNY", "consolidated", True),
            ),
            "old": (("rejected-old", "revenue", "Revenue", "90", "CNY", "consolidated", False),),
        }
        source_data = {}
        latest_core: tuple[FinancialFact, ...] = ()

        class FakeEngine:
            def ingest(self, _company, _filings):
                actual_filing = _filings[0]
                facts, evidence = source_data[actual_filing.document_id]
                manifest = FilingManifest(
                    actual_filing.document_id, actual_filing.accession_number, actual_filing.source_url,
                    actual_filing.primary_document, actual_filing.form_type, actual_filing.fiscal_period,
                    actual_filing.period_end, actual_filing.revision, actual_filing.supersedes_document_id,
                    actual_filing.content_hash,
                )
                if actual_filing.accession_number == "latest":
                    nonlocal latest_core
                    latest_core = facts
                    validation = FinancialValidation(
                        ValidationStatus.VERIFIED, (), frozenset(item.concept for item in facts), facts, ()
                    )
                    return FinancialDataset(
                        facts, evidence, (manifest,), validation, (),
                        (FinancialGroupValidation(
                            (actual_filing.accession_number, actual_filing.period_end,
                             "FY", "consolidated", "CNY"), validation,
                        ),),
                    )
                old_validation = FinancialValidation(
                    ValidationStatus.REJECTED, ("bad_old",), frozenset(), (), facts
                )
                return FinancialDataset(
                    (), evidence, (manifest,), old_validation, ("bad_old",),
                    (FinancialGroupValidation(
                        (actual_filing.accession_number, actual_filing.period_end,
                         "FY", "consolidated", "CNY"), old_validation,
                    ),),
                )

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5): return [old, latest][:limit]
            def download_filing(self, item, target):
                downloaded, facts, evidence = _download_bound_financial_fixture(
                    item, target, rows_by_accession[item.accession_number]
                )
                source_data[downloaded.document_id] = (facts, evidence)
                return downloaded
        class Provider:
            def generate(self, *_args, **_kwargs): return {"claims": []}
        with tempfile.TemporaryDirectory() as directory, \
             patch("openthesis.financial_sources.disclosures.build_filing_evidence", return_value=[]), \
             patch("openthesis.financial_sources.disclosures.build_disclosure_index", return_value=[]):
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter(), [company]),
                financial_ingestion_engine=FakeEngine(), provider_factory=lambda _config: Provider(),
            )
            started, status = self._start_research_and_wait_for_worker(service, {
                "mode": "company", "company": company.to_dict(), "download_filings": True,
                "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
            })
            self.assertEqual(status["state"], "completed")
            saved = service.storage.get_facts(company.security_id)
            audit = service.storage.get_facts_audit(company.security_id)
            self.assertEqual(
                {item["fact_id"] for item in saved},
                {f"accepted-{concept}" for concept in ("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity")},
            )
            self.assertEqual(
                {item["fact_id"] for item in audit},
                {f"accepted-{concept}" for concept in ("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity")} | {"rejected-old"},
            )
    def test_delete_research_run_removes_only_the_requested_finished_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(build_company("688981.SH", "SMIC"))
            company = build_company("688981.SH", "SMIC")
            service.storage.save_run(
                ResearchRun(
                    run_id="run-delete-service",
                    company=company,
                    workflow_id="test",
                    research_pack_id="test",
                    research_pack_version="1",
                    provider_id="none",
                    model_id="",
                    data_as_of="2026-08-09T00:00:00+00:00",
                    status=RunStatus.FAILED,
                    completed_at=utc_now_iso(),
                )
            )

            self.assertEqual(
                service.delete_research_run("run-delete-service"),
                {"run_id": "run-delete-service", "deleted": True},
            )
            self.assertEqual(service.list_research_runs(), [])

    def test_verified_no_filings_stops_before_model_provider_creation(self) -> None:
        class NoFilingsAdapter:
            def list_financial_filings(self, _company: Company, *, limit: int = 5):
                return []

        provider_calls: list[str] = []

        def provider_factory(config):
            provider_calls.append(config.public_id)
            raise AssertionError("model provider must not be created without a financial report")

        with tempfile.TemporaryDirectory() as directory:
            company = build_company("688825.SH", "长鑫科技")
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(NoFilingsAdapter(), [company]),
                provider_factory=provider_factory,
            )
            started = service.start_research(
                {
                    "mode": "company",
                    "company": company.to_dict(),
                    "download_filings": True,
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
                }
            )

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "no-filings research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            self.assertIsNotNone(status["run_id"])
            self.assertEqual(status["error_code"], "NO_FILINGS_AVAILABLE")
            self.assertEqual(status["market"], "CN_A")
            self.assertEqual(status["disclosure_url"], "https://www.cninfo.com.cn/new/index")
            self.assertNotIn("不代表", status["message"])
            self.assertEqual(provider_calls, [])

    def test_rejected_filing_quality_stops_before_provider_and_storage_write(self) -> None:
        company = build_company("832982.BJ", "Jinbo Bio")
        filing = FilingDocument(
            document_id="official:jinbo-bad",
            company_cik=company.security_id,
            accession_number="jinbo-bad",
            form_type="ANNUAL_REPORT",
            fiscal_period="FY",
            period_end="2023-12-31",
            filed_at="2024-04-25T00:00:00+00:00",
            primary_document="2023 Annual Report",
            source_url="https://example.invalid/jinbo.pdf",
        )

        requested_limits: list[int] = []

        provider_calls: list[str] = []

        def provider_factory(config):
            provider_calls.append(config.public_id)
            raise AssertionError("provider must not be created without admitted facts or disclosures")

        source_data = {}

        class BadEngine:
            def ingest(self, _company, _filings):
                actual_filing = _filings[0]
                facts, evidence = source_data[actual_filing.document_id]
                manifest = FilingManifest(
                    actual_filing.document_id, actual_filing.accession_number, actual_filing.source_url,
                    actual_filing.primary_document, actual_filing.form_type, actual_filing.fiscal_period,
                    actual_filing.period_end, actual_filing.revision, actual_filing.supersedes_document_id,
                    actual_filing.content_hash,
                )
                validation = FinancialValidation(
                    ValidationStatus.REJECTED, ("income_statement_core_missing",),
                    frozenset({"revenue"}), (), facts,
                )
                group = FinancialGroupValidation(
                    (actual_filing.accession_number, actual_filing.period_end, "FY", "consolidated", "CNY"),
                    validation,
                )
                return FinancialDataset(
                    (), evidence, (manifest,), validation, ("income_statement_core_missing",), (group,)
                )

        class BadFilingAdapter:
            def list_financial_filings(self, _company: Company, *, limit: int = 5):
                requested_limits.append(limit)
                return [filing][:limit]

            def download_filing(self, item: FilingDocument, target: Path) -> FilingDocument:
                downloaded, facts, evidence = _download_bound_financial_fixture(
                    item, target,
                    (("bad:revenue", "revenue", "Revenue", "100", "CNY", "consolidated", False),),
                )
                source_data[downloaded.document_id] = (facts, evidence)
                return downloaded

        with tempfile.TemporaryDirectory() as directory, \
             patch("openthesis.financial_sources.disclosures.build_filing_evidence", return_value=[]), \
             patch("openthesis.financial_sources.disclosures.build_disclosure_index", return_value=[]):
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(BadFilingAdapter(), [company]),
                financial_ingestion_engine=BadEngine(),
                provider_factory=provider_factory,
            )
            started = service.start_research(
                {
                    "mode": "company",
                    "company": company.to_dict(),
                    "download_filings": True,
                    "evidence_policy": {"annual_history_years": 2},
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
                }
            )
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "quality-gate research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])
            self.assertEqual(status["state"], "completed")
            self.assertIsNotNone(status["run_id"])
            self.assertEqual(status["error_code"], "FILING_DATA_QUALITY_FAILED", status)
            self.assertEqual(provider_calls, [])
            self.assertEqual(requested_limits, [5])
            self.assertEqual(service.storage.get_facts(company.security_id), [])

    def test_unverified_official_response_is_distinct_and_skips_model(self) -> None:
        class UnverifiedAdapter:
            def list_financial_filings(self, _company: Company, *, limit: int = 5):
                raise MarketDataError(
                    "ambiguous response",
                    code="FILING_STATUS_UNVERIFIED",
                )

        provider_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            company = build_company("688825.SH", "长鑫科技")
            service = AppService(
                Path(directory),
                market_data=_ResearchMarketData(UnverifiedAdapter(), [company]),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            started = service.start_research(
                {
                    "mode": "company",
                    "company": company.to_dict(),
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
                }
            )

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "unverified research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            self.assertIsNotNone(status["run_id"])
            self.assertEqual(status["error_code"], "FILING_STATUS_UNVERIFIED")
            self.assertNotIn("ambiguous response", status["message"])
            self.assertEqual(provider_calls, [])

    def test_bootstrap_exposes_stable_contract_and_safe_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), app_version="1.0.0-alpha.1")

            result = service.bootstrap()

            self.assertEqual(result["contract_version"], "2.0")
            self.assertEqual(result["app_version"], "1.0.0-alpha.1")
            self.assertIn(result["preferences"]["ui_language"], {"zh-CN", "zh-Hant", "en"})
            self.assertEqual(result["preferences"]["ui_language_mode"], "system")
            self.assertEqual(result["preferences"]["report_language"], result["preferences"]["ui_language"])
            self.assertEqual(result["preferences"]["parallel_agents"], "true")
            self.assertEqual(result["recent_runs"], [])
            self.assertEqual(
                {item["market"] for item in result["market_catalog"]},
                {"US", "CN_A", "HK"},
            )
            self.assertTrue(result["common_companies"])
            self.assertTrue(all("research_readiness" in item for item in result["common_companies"]))
            self.assertTrue(all(not item["research_readiness"]["recommended"] for item in result["common_companies"]))

    def test_preferences_persist_only_allowlisted_non_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            service = AppService(data_dir)

            saved = service.update_preferences(
                {"ui_language": "en", "report_language": "zh-CN"}
            )

            self.assertEqual(saved["ui_language"], "en")
            self.assertEqual(AppService(data_dir).bootstrap()["preferences"], saved)
            with self.assertRaises(PreferenceValidationError):
                service.update_preferences({"api_key": "never-store-this"})
            self.assertEqual(service.storage.get_setting("api_key", ""), "")

    def test_visual_fallback_policy_is_atomic_versioned_and_secret_free(self) -> None:
        policy = {
            "schema_version": 1,
            "policy_version": 1,
            "scope_version": 1,
            "enabled": True,
            "provider": "mineru_flash",
            "approval_mode": "approve_current_research",
            "standing_authorization": True,
            "authorization_scope": "financial_failed_pages",
            "provider_terms_version": 1,
            "authorized_at": "2026-09-13T12:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            saved = service.update_preferences(
                {"vision_fallback_policy": json.dumps(policy)}
            )
            self.assertEqual(
                json.loads(saved["vision_fallback_policy"]),
                policy,
            )
            request = _vision_request_from_preferences(saved)
            self.assertTrue(request["enabled"])
            self.assertTrue(request["consent"])
            self.assertEqual(request["provider"], "mineru_flash")

            with self.assertRaises(PreferenceValidationError):
                service.update_preferences(
                    {"vision_fallback_policy": json.dumps({**policy, "api_key": "secret"})}
                )
            with self.assertRaises(PreferenceValidationError):
                service.update_preferences(
                    {"vision_fallback_policy": json.dumps({
                        **policy,
                        "provider": "configured_model",
                        "model": {
                            "configured_model_id": "vision.ready",
                            "configuration_version": 1,
                            "token": "secret",
                        },
                    })}
                )
            with self.assertRaises(PreferenceValidationError):
                service.update_preferences(
                    {"vision_fallback_policy": json.dumps({**policy, "policy_version": 2})}
                )

    def test_unknown_report_has_a_stable_not_found_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(KeyError, "research run not found"):
                service.get_report("missing")

    def test_persisted_report_read_is_not_blocked_by_optional_recovery_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000042", ticker="HIST", name="History Company")
            service.storage.save_company(company)
            run = ResearchRun(
                run_id="persisted-report-without-recovery",
                company=company,
                workflow_id="test",
                research_pack_id="builtin",
                research_pack_version="1",
                provider_id="unused",
                model_id="unused",
                data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
            )
            service.storage.save_run(run)
            service.storage.save_artifact(ResearchArtifact(
                artifact_id="persisted-report",
                run_id=run.run_id,
                artifact_type="research-report",
                title="Persisted report",
                content={"report": {"executive_summary": "Saved research stays readable."}},
            ))

            report = service.get_report(run.run_id, language="en")

            self.assertEqual(report["run_id"], run.run_id)
            self.assertEqual(report["report_contract_version"], "1")
            self.assertEqual(report["report_read_state"], "partial")
            self.assertTrue(report["is_substantive"])
            self.assertEqual(
                [item["section_id"] for item in report["visible_sections"] if item["is_substantive"]],
                ["executive_summary"],
            )
            self.assertIn("Saved research stays readable.", report["markdown"])
            self.assertIn("Saved research stays readable.", report["html"])
            self.assertFalse(report["recovery_plan"]["available"])

    def test_optional_recovery_and_financial_diagnostics_do_not_hide_saved_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000043", ticker="SAFE", name="Saved Company")
            run = ResearchRun(
                run_id="persisted-report-optional-metadata-failure", company=company,
                workflow_id="test", research_pack_id="builtin", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.PARTIAL,
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_artifact(ResearchArtifact(
                artifact_id="saved-safe-summary", run_id=run.run_id,
                artifact_type="research-report", title="Saved report",
                content={"report": {"executive_summary": "Previously verified text stays available."}},
            ))

            with patch("openthesis.service._financial_status", side_effect=RuntimeError("unavailable")), patch.object(
                service._recovery_planner, "describe", side_effect=RuntimeError("unavailable")
            ):
                report = service.get_report(run.run_id, language="en")

            self.assertIn("Previously verified text stays available.", report["markdown"])
            self.assertEqual(report["financial_status"]["state"], "unavailable")
            self.assertFalse(report["recovery_plan"]["available"])
            self.assertTrue({item["code"] for item in report["report_read_diagnostics"]} >= {
                "FINANCIAL_STATUS_UNAVAILABLE", "RECOVERY_METADATA_UNAVAILABLE",
            })

    def test_unavailable_continuity_does_not_invalidate_financial_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000044", ticker="METALESS", name="Saved Company")
            run = ResearchRun(
                run_id="saved-report-unavailable-continuity", company=company,
                workflow_id="test", research_pack_id="builtin", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.PARTIAL,
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_artifact(ResearchArtifact(
                artifact_id="saved-summary-without-continuity", run_id=run.run_id,
                artifact_type="research-report", title="Saved report",
                content={"report": {"executive_summary": "Saved evidence remains readable."}},
            ))
            with patch.object(service._continuity, "snapshot", side_effect=RuntimeError("unavailable")), patch.object(
                service.storage, "continuity_snapshot", side_effect=RuntimeError("unavailable")
            ) as reread:
                report = service.get_report(run.run_id, language="en")

            self.assertIn("Saved evidence remains readable.", report["markdown"])
            # No filings is legitimately an unavailable source. That is not
            # the diagnostic fallback caused by optional metadata failure.
            self.assertGreater(report["financial_status"]["history_years"], 0)
            self.assertEqual(report["financial_status"]["next_action"], "retry_discovery")
            self.assertIsNone(report["financial_status"]["recovery_outcome"])
            codes = {item["code"] for item in report["report_read_diagnostics"]}
            self.assertIn("RECOVERY_METADATA_UNAVAILABLE", codes)
            self.assertNotIn("FINANCIAL_STATUS_UNAVAILABLE", codes)
            reread.assert_not_called()

    def test_refresh_financial_report_is_deterministic_and_does_not_download_or_create_provider(self) -> None:
        provider_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            company = Company(cik="0000000003", ticker="REF", name="Refresh Corp")
            run = ResearchRun(
                run_id="refresh-only", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
            )
            service = AppService(
                Path(directory),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            with patch.object(
                service, "_rebuild_financial_artifacts", return_value=("artifact-1",)
            ) as rebuild, patch.object(
                service, "get_report", return_value={"run_id": run.run_id}
            ) as get_report:
                result = service.refresh_financial_report(run.run_id, language="en")
            rebuild.assert_called_once()
            get_report.assert_called_once_with(run.run_id, language="en")
            self.assertEqual(result["financial_report_refresh"]["status"], "succeeded")
            self.assertEqual(provider_calls, [])

    def test_report_technical_details_are_explicitly_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000001", ticker="ACME", name="Acme Corp")
            run = ResearchRun(
                run_id="technical-report",
                company=company,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="test",
                model_id="test",
                data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_artifact(
                ResearchArtifact(
                    artifact_id="growth-1",
                    run_id=run.run_id,
                    artifact_type="growth-opportunities",
                    title="Growth opportunities",
                    content={
                        "opportunities": [
                            {
                                "title": "New platform",
                                "mechanism": "Expand the addressable market.",
                                "evidence_grade": "C",
                                "supporting_evidence_ids": ["fact:technical-only"],
                            }
                        ]
                    },
                    model_id="test:model",
                    agent_id="growth-opportunity-analyst",
                )
            )

            from openthesis.report_document import assemble_report_document

            with patch(
                "openthesis.report_document.assemble_report_document",
                wraps=assemble_report_document,
            ) as assemble:
                default_report = service.get_report(run.run_id, language="en")
            self.assertEqual(assemble.call_count, 1)
            technical_report = service.get_report(
                run.run_id, language="en", include_technical=True
            )

            self.assertNotIn("fact:technical-only", default_report["markdown"])
            self.assertIn("fact:technical-only", technical_report["markdown"])
            self.assertNotIn("fact:technical-only", default_report["html"])
            self.assertIn("fact:technical-only", technical_report["html"])

    def test_report_marks_empty_growth_model_output_as_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000002", ticker="GROW", name="Growth Corp")
            run = ResearchRun(
                run_id="growth-retryable",
                company=company,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="test",
                model_id="test",
                data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_artifact(
                ResearchArtifact(
                    artifact_id="growth-empty",
                    run_id=run.run_id,
                    artifact_type="growth-opportunities",
                    title="Growth opportunities",
                    content={
                        "opportunities": [],
                        "structured_output_valid": False,
                        "_response_error": "empty_content",
                        "_validation": {"passed": False, "issues": ["empty"]},
                    },
                    model_id="test:model",
                    agent_id="growth-opportunity-analyst",
                )
            )

            report = service.get_report(run.run_id, language="en")

            self.assertTrue(report["retryable_growth"])
            self.assertIn("growth-opportunity model returned no usable content", report["markdown"])

    def test_report_exposes_missing_financial_periods_and_zero_token_retry_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = build_company("09988.HK", "Alibaba", reporting_currency="CNY")
            run = ResearchRun(
                run_id="financial-status",
                company=company,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="test",
                model_id="test",
                data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
                research_configuration={"annual_history_years": 2},
            )
            service.storage.save_company(company)
            service.storage.save_run(run)
            service.storage.save_filings([
                FilingDocument(
                    "doc-2026", company.security_id, "acc-2026", "ANNUAL_REPORT", "FY",
                    "2026-03-31", "2026-06-01", "2026.pdf", "https://example.test/2026.pdf",
                ),
                FilingDocument(
                    "doc-2024", company.security_id, "acc-2024", "ANNUAL_REPORT", "FY",
                    "2024-03-31", "2024-06-01", "2024.pdf", "https://example.test/2024.pdf",
                ),
            ])

            status = service.get_report(run.run_id, language="en")["financial_status"]

            self.assertEqual(status["state"], "incomplete")
            self.assertEqual(status["missing_periods"], ["2025"])
            self.assertTrue(status["retryable"])
            self.assertEqual(status["next_action"], "retry_missing_periods")
            self.assertEqual(status["model_calls"], 0)
            self.assertEqual(status["token_delta"], 0)

    def test_retry_financials_refreshes_only_unhealthy_nodes_without_provider(self) -> None:
        company = _retry_inline_company()
        provider_calls: list[str] = []
        accession_unhealthy = f"{company.cik}-26-000001"
        accession_healthy = f"{company.cik}-25-000001"
        complete_2025 = _retry_inline_annual_values(2025)
        complete_2024 = _retry_inline_annual_values(2024)
        # Start with a real, parser-backed but incomplete original for 2025.
        # The 2024 original is already healthy and must remain a local cache hit.
        client = _OfflineInlineSecClient(company, (
            (accession_unhealthy, 2025, {2025: {"revenue": complete_2025["revenue"]}}),
            (accession_healthy, 2024, {2024: complete_2024}),
        ))

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), sec_client_factory=lambda *_args: client,
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            seed_trace, seed_errors = _seed_retry_inline_cohort(
                service, company, annual_history_years=2,
            )
            self.assertTrue(
                any("FILING_DATA_QUALITY_FAILED" in item for item in seed_errors),
                f"seed_trace={seed_trace}; groups={service.storage.get_validation_groups(company.cik)}",
            )
            self.assertEqual(seed_trace["targets"], {accession_unhealthy, accession_healthy})
            self.assertEqual(set(client.downloaded_accessions), {accession_unhealthy, accession_healthy})
            initial_groups = {
                item["accession_number"]: item["status"]
                for item in service.storage.get_validation_groups(company.cik)
            }
            self.assertEqual(initial_groups[accession_healthy], "VERIFIED")
            self.assertEqual(initial_groups[accession_unhealthy], "READY_WITH_WARNINGS")
            initial_good = {
                item["fact_id"]: item["value"]
                for item in service.storage.get_facts(company.cik)
                if item["accession_number"] == accession_healthy
            }
            self.assertEqual(len(initial_good), len(_RETRY_INLINE_FACTS))
            initial_bad_audit = {
                item["fact_id"]
                for item in service.storage.get_facts_audit(company.cik)
                if item["accession_number"] == accession_unhealthy
            }
            self.assertTrue(initial_bad_audit)
            initial_snapshot = service.storage.get_financial_research_snapshot(company.cik)
            healthy_generation = initial_snapshot["generations"][accession_healthy]

            # Simulate the unhealthy node's local original having been lost;
            # recovery must reacquire this accession while leaving the healthy
            # sibling's verified local original untouched.
            unhealthy_filing = next(
                item for item in service.storage.get_filings(company.cik)
                if item.accession_number == accession_unhealthy
            )
            unhealthy_original = Path(unhealthy_filing.local_path)
            self.assertTrue(unhealthy_original.is_file())
            unhealthy_original.unlink()
            self.assertFalse(unhealthy_original.exists())

            service.storage.save_run(ResearchRun(
                run_id="financial-retry", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.PARTIAL,
                research_configuration={"annual_history_years": 2},
            ))

            # The same official filing identity now supplies the complete
            # original; repair should reacquire/recompile only this unhealthy
            # accession and preserve its earlier audited generation.
            client.values_by_accession[accession_unhealthy] = {2025: complete_2025}

            result = service.retry_financials("financial-retry")
            self.assertEqual(result["run_id"], "financial-retry")
            self.assertEqual(result["financial_retry"]["targets"], [accession_unhealthy])
            self.assertEqual(client.downloaded_accessions.count(accession_unhealthy), 2)
            self.assertEqual(client.downloaded_accessions.count(accession_healthy), 1)
            self.assertEqual(provider_calls, [])
            _assert_retry_inline_cohort(
                self, service, company, {accession_unhealthy, accession_healthy},
            )
            self.assertEqual(
                {
                    item["fact_id"]: item["value"]
                    for item in service.storage.get_facts(company.cik)
                    if item["accession_number"] == accession_healthy
                },
                initial_good,
            )
            repaired_snapshot = service.storage.get_financial_research_snapshot(company.cik)
            self.assertEqual(repaired_snapshot["generations"][accession_healthy], healthy_generation)
            final_audit_ids = {
                item["fact_id"]
                for item in service.storage.get_facts_audit(company.cik)
                if item["accession_number"] == accession_unhealthy
            }
            self.assertTrue(initial_bad_audit <= final_audit_ids)

            second = service.retry_financials("financial-retry")
            self.assertEqual(second["financial_retry"]["targets"], [])
            self.assertEqual(second["financial_retry"]["status"], "succeeded")
            self.assertEqual(client.downloaded_accessions.count(accession_unhealthy), 2)
            self.assertEqual(client.downloaded_accessions.count(accession_healthy), 1)
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                service.rebuild_financials("financial-retry")
            service.rebuild_financials("financial-retry", confirmed=True)
            self.assertEqual(client.downloaded_accessions.count(accession_unhealthy), 2)
            self.assertEqual(client.downloaded_accessions.count(accession_healthy), 1)
            self.assertEqual(provider_calls, [])

    def test_retry_financials_keeps_success_when_another_node_fails(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        download_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            good = FilingDocument("good", company.security_id, "good-2025", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-01", "good.pdf", "https://example.test/good.pdf")
            bad = FilingDocument("bad", company.security_id, "bad-2024", "ANNUAL_REPORT", "FY", "2024-12-31", "2025-03-01", "bad.pdf", "https://example.test/bad.pdf")
            class Adapter:
                def list_financial_filings(self, _company, *, limit=5): return [good, bad][:limit]
                def download_filing(self, filing, target):
                    download_calls.append(filing.accession_number)
                    target.mkdir(parents=True, exist_ok=True)
                    path = target / f"{filing.accession_number}.pdf"
                    content = b"official"
                    path.write_bytes(content)
                    filing.local_path, filing.content_hash = str(path), hashlib.sha256(content).hexdigest()
                    return filing
            class Engine:
                def ingest(self, _company, filings, **_kwargs):
                    filing = filings[0]
                    if filing.accession_number == bad.accession_number: raise OSError("bad filing")
                    fact = FinancialFact(f"{filing.accession_number}:revenue", company.security_id, "revenue", "Revenue", 10.0, "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", filing.period_end, filing.filed_at, filing.accession_number, filing.source_url, statement="income_statement", currency="CNY", raw_text="Revenue 10", validation_status="VERIFIED")
                    validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue"}), (fact,), ())
                    group = FinancialGroupValidation((filing.accession_number, filing.period_end, "FY", "consolidated", "CNY"), validation)
                    manifest = FilingManifest(filing.document_id, filing.accession_number, filing.source_url, filing.primary_document, filing.form_type, filing.fiscal_period, filing.period_end, filing.revision, filing.supersedes_document_id, filing.content_hash)
                    return FinancialDataset((fact,), (), (manifest,), validation, (), (group,))
            service = AppService(Path(directory), market_data=_ResearchMarketData(Adapter()), financial_ingestion_engine=Engine())
            service.storage.save_company(company)
            service.storage.save_run(ResearchRun(run_id="financial-retry-partial", company=company, workflow_id="test", research_pack_id="test", research_pack_version="1", provider_id="unused", model_id="unused", data_as_of=utc_now_iso(), status=RunStatus.PARTIAL))
            service.storage.save_filings([good, bad])
            result = service.retry_financials("financial-retry-partial")
            self.assertEqual(result["run_id"], "financial-retry-partial")
            self.assertEqual(result["financial_retry"]["status"], "partial")
            self.assertNotEqual(result["financial_retry"]["status"], "succeeded")
            self.assertTrue(result["financial_retry"]["accepted"])
            self.assertTrue(result["financial_retry"]["error"])
            self.assertEqual(set(download_calls), {good.accession_number, bad.accession_number})
            self.assertTrue(any(item["accession_number"] == good.accession_number for item in service.storage.get_facts_audit(company.security_id)))
            self.assertEqual(service.storage.get_financial_retry_state(company.security_id)["last_stage"], "filing-parse")

    def test_market_retry_batches_production_targets_and_persists_each_slice(self) -> None:
        company = _retry_inline_company()
        accession_2025 = "0000030075-26-000001"
        accession_2024 = "0000030075-25-000001"
        complete_2025 = {
            "revenue": "1250000", "net_income": "240000",
            "operating_cash_flow": "310000", "assets": "3000000",
            "liabilities": "1700000", "equity": "1300000",
        }
        complete_2024 = {
            "revenue": "1100000", "net_income": "210000",
            "operating_cash_flow": "275000", "assets": "2700000",
            "liabilities": "1500000", "equity": "1200000",
        }
        client = _OfflineInlineSecClient(company, (
            (accession_2025, 2025, {2025: complete_2025}),
            (accession_2024, 2024, {2024: complete_2024}),
        ))
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), sec_client_factory=lambda *_args: client,
            )
            service.update_preferences({
                "sec_contact_profile": "personal",
                "sec_contact_email": "offline@example.test",
            })
            service.storage.save_company(company)
            trace = {"targets": set(), "downloaded": set(), "processed": set(), "warnings": set()}

            errors = service._retry_market_financials(
                company, {"research_configuration": {"annual_history_years": 2}}, trace=trace,
            )

            self.assertEqual(errors, [], f"trace={trace}; list_limits={client.list_limits}")
            expected_accessions = {accession_2025, accession_2024}
            self.assertEqual(trace["targets"], expected_accessions)
            self.assertEqual(trace["downloaded"], expected_accessions)
            self.assertEqual(trace["processed"], expected_accessions)
            self.assertEqual(set(client.downloaded_accessions), expected_accessions)
            self.assertEqual(client.list_limits, [5])
            self.assertEqual(client.companyfacts_calls, 1)
            persisted = service.storage.get_facts_audit(company.cik)
            self.assertEqual(
                {item["accession_number"] for item in persisted},
                expected_accessions,
            )
            self.assertEqual(
                {item["accession_number"] for item in service.storage.get_validation_groups(company.cik)},
                expected_accessions,
            )
            self.assertEqual(len(persisted), 12)
            self.assertTrue(all(item["normalization_lineage"] for item in persisted))
            downloaded = {item.accession_number: item for item in service.storage.get_filings(company.cik)}
            evidence = {
                item["evidence_id"]: item
                for item in service.storage.get_financial_evidence()
            }
            for fact in persisted:
                unit_id = fact["normalization_lineage"]["unit_evidence_ids"][0]
                self.assertEqual(
                    evidence[unit_id]["content_hash"],
                    downloaded[fact["accession_number"]].content_hash,
                )

    def test_market_retry_batch_keeps_incomplete_group_partial(self) -> None:
        company = _retry_inline_company()
        accession_good = "0000030075-26-000001"
        accession_partial = "0000030075-25-000001"
        complete_2025 = {
            "revenue": "1250000", "net_income": "240000",
            "operating_cash_flow": "310000", "assets": "3000000",
            "liabilities": "1700000", "equity": "1300000",
        }
        client = _OfflineInlineSecClient(company, (
            (accession_good, 2025, {2025: complete_2025}),
            (accession_partial, 2024, {2024: {"revenue": "1100000"}}),
        ))
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), sec_client_factory=lambda *_args: client,
            )
            service.update_preferences({
                "sec_contact_profile": "personal",
                "sec_contact_email": "offline@example.test",
            })
            service.storage.save_company(company)
            trace = {"targets": set(), "downloaded": set(), "processed": set(), "warnings": set()}

            errors = service._retry_market_financials(
                company,
                {"research_configuration": {"annual_history_years": 2}},
                trace=trace,
            )

            expected_accessions = {accession_good, accession_partial}
            self.assertTrue(any("FILING_DATA_QUALITY_FAILED" in item for item in errors))
            self.assertEqual(trace["targets"], expected_accessions)
            self.assertEqual(set(client.downloaded_accessions), expected_accessions)
            self.assertEqual(trace["processed"], expected_accessions)
            self.assertEqual(
                {item["accession_number"] for item in service.storage.get_facts_audit(company.cik)},
                expected_accessions,
            )
            active = service.storage.get_facts(company.cik)
            self.assertIn(accession_good, {item["accession_number"] for item in active})
            self.assertTrue(all(item["normalization_lineage"] for item in active))
            statuses = {
                item["accession_number"]: item["status"]
                for item in service.storage.get_validation_groups(company.cik)
            }
            self.assertEqual(statuses[accession_good], "VERIFIED")
            self.assertEqual(statuses[accession_partial], "READY_WITH_WARNINGS")

    def test_verified_target_survives_incomplete_comparator_group(self) -> None:
        company = _retry_inline_company()
        accession = "0000030075-26-000001"
        complete_2025 = {
            "revenue": "1250000", "net_income": "240000",
            "operating_cash_flow": "310000", "assets": "3000000",
            "liabilities": "1700000", "equity": "1300000",
        }
        client = _OfflineInlineSecClient(company, (
            (accession, 2025, {
                2025: complete_2025,
                2024: {"revenue": "1100000"},
            }),
        ))
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), sec_client_factory=lambda *_args: client,
            )
            service.update_preferences({
                "sec_contact_profile": "personal",
                "sec_contact_email": "offline@example.test",
            })
            service.storage.save_company(company)

            trace = {"targets": set(), "downloaded": set(), "processed": set(), "warnings": set()}
            errors = service._retry_market_financials(
                company,
                {"research_configuration": {"annual_history_years": 1}},
                trace=trace,
            )
            self.assertTrue(
                any("FILING_DATA_QUALITY_FAILED" in item for item in errors),
                f"trace={trace}; groups={service.storage.get_validation_groups(company.cik)}",
            )
            active = service.storage.get_facts(company.cik)
            self.assertEqual({item["fiscal_year"] for item in active}, {2025})
            self.assertEqual(
                {item["concept"] for item in active},
                {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity"},
            )
            self.assertTrue(all(item["normalization_lineage"] for item in active))
            generation = service.storage.get_financial_research_snapshot(company.cik)["generations"][accession]
            self.assertTrue(generation)
            self.assertTrue(all(item["generation_id"] == generation for item in active))
            target_status = next(
                item["status"]
                for item in service.storage.get_validation_groups(company.cik)
                if item["accession_number"] == accession and item["period_end"] == "2025-12-31"
            )
            self.assertEqual(target_status, "VERIFIED")
            audit = service.storage.get_facts_audit(company.cik)
            self.assertEqual({item["fiscal_year"] for item in audit}, {2024, 2025})
            self.assertEqual(set(client.downloaded_accessions), {accession})

    def test_us_retry_is_local_and_idempotent_when_annual_window_is_complete(self) -> None:
        company = Company(
            cik="0000000002", ticker="CACHE", name="Cached Corp", exchange="NASDAQ",
            market="US", security_id="US:NASDAQ:CACHE", listing_currency="USD",
            reporting_currency="USD", accounting_standard="US_GAAP",
        )
        specs = _retry_inline_report_specs(company, (2025, 2024, 2023))
        expected_accessions = {item[0] for item in specs}
        offline_client = _OfflineInlineSecClient(company, specs)
        client_factories = {"count": 0}

        def client_factory(*_args):
            client_factories["count"] += 1
            return offline_client

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), sec_client_factory=client_factory)
            seed_trace, seed_errors = _seed_retry_inline_cohort(
                service, company, annual_history_years=2,
            )
            self.assertEqual(seed_errors, [], f"seed_trace={seed_trace}")
            self.assertEqual(seed_trace["downloaded"], expected_accessions)
            self.assertEqual(offline_client.companyfacts_calls, 1)
            self.assertEqual(set(offline_client.downloaded_accessions), expected_accessions)
            _assert_retry_inline_cohort(self, service, company, expected_accessions)

            service.storage.save_run(ResearchRun(
                run_id="us-cache-status", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.COMPLETED,
                research_configuration={"annual_history_years": 2},
                data_snapshot={
                    "financial_fact_ids_sha256": _canonical_snapshot_digest(
                        ["previous-generation:revenue"]
                    ),
                },
            ))

            # The verified originals and canonical cohort are present already.
            # Any client construction/list/facts/download here would be external work.
            client_factories["count"] = 0
            offline_client.list_limits.clear()
            offline_client.companyfacts_calls = 0
            offline_client.downloaded_accessions.clear()

            payload = {"research_configuration": {"annual_history_years": 2}}
            self.assertEqual(service._retry_us_financials(company, payload), [])
            self.assertEqual(service._retry_us_financials(company, payload), [])
            self.assertEqual(client_factories["count"], 0)
            self.assertEqual(offline_client.list_limits, [])
            self.assertEqual(offline_client.companyfacts_calls, 0)
            self.assertEqual(offline_client.downloaded_accessions, [])
            _assert_retry_inline_cohort(self, service, company, expected_accessions)
            status = service.get_report("us-cache-status", language="en")["financial_status"]
            self.assertEqual(status["state"], "complete")
            self.assertEqual(status["available_periods"], ["2025", "2024", "2023"])
            self.assertTrue(status["snapshot_stale"])

    def test_us_retry_uses_canonical_local_facts_when_company_facts_is_unavailable(self) -> None:
        """A transient SEC facts outage must not discard a trusted local group."""
        company = Company(
            cik="0000000003", ticker="LOCAL", name="Local SEC Corp", exchange="NASDAQ",
            market="US", security_id="US:NASDAQ:LOCAL", listing_currency="USD",
            reporting_currency="USD", accounting_standard="US_GAAP",
        )
        specs = _retry_inline_report_specs(company, (2025, 2024))
        expected_accessions = {item[0] for item in specs}
        offline_client = _OfflineInlineSecClient(company, specs)
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), sec_client_factory=lambda *_args: offline_client,
            )
            seed_trace, seed_errors = _seed_retry_inline_cohort(
                service, company, annual_history_years=1,
            )
            self.assertEqual(seed_errors, [], f"seed_trace={seed_trace}")
            self.assertEqual(seed_trace["downloaded"], expected_accessions)
            self.assertEqual(offline_client.companyfacts_calls, 1)
            self.assertEqual(set(offline_client.downloaded_accessions), expected_accessions)
            _assert_retry_inline_cohort(self, service, company, expected_accessions)

            # Period verification must not rewrite the source's filing revision.
            stored_by_accession = {
                item.accession_number: item
                for item in service.storage.get_filings(company.cik)
            }
            discovered_by_accession = {
                item.accession_number: item for item in offline_client.filings
            }
            self.assertEqual(set(stored_by_accession), expected_accessions)
            for accession in expected_accessions:
                discovered = discovered_by_accession[accession]
                stored = stored_by_accession[accession]
                for field in (
                    "company_cik", "accession_number", "document_id", "source_url",
                    "form_type", "fiscal_period", "period_end",
                ):
                    self.assertEqual(
                        getattr(discovered, field), getattr(stored, field),
                        f"{accession} cache identity field {field}",
                    )
                self.assertEqual(discovered.revision, "original", accession)
                self.assertEqual(stored.revision, "original", accession)
                self.assertTrue(Path(stored.local_path).is_file(), accession)
                self.assertEqual(
                    hashlib.sha256(Path(stored.local_path).read_bytes()).hexdigest(),
                    stored.content_hash,
                    f"{accession} cached source SHA-256",
                )
            facts_before = {
                item["fact_id"]: item["value"]
                for item in service.storage.get_facts(company.cik)
            }
            offline_client.list_limits.clear()
            offline_client.companyfacts_calls = 0
            offline_client.downloaded_accessions.clear()

            def unavailable_company_facts(_company):
                offline_client.companyfacts_calls += 1
                raise RuntimeError("SEC facts temporarily unavailable")

            offline_client.get_company_facts = unavailable_company_facts
            trace = {
                "targets": set(), "downloaded": set(), "processed": set(),
                "warnings": set(), "diagnostics": set(), "freshness": set(),
                "terminal_state": set(), "next_action": set(),
            }
            errors = service._retry_us_financials(
                company,
                {
                    "research_configuration": {"annual_history_years": 1},
                    "_financial_recovery_targets": sorted(expected_accessions),
                },
                trace=trace,
            )

            self.assertEqual(errors, [], f"trace={trace}")
            self.assertTrue(expected_accessions.issubset(trace["processed"]))
            self.assertEqual(trace["downloaded"], set(), f"trace={trace}")
            self.assertGreaterEqual(offline_client.companyfacts_calls, 1)
            self.assertLessEqual(offline_client.companyfacts_calls, 3)
            self.assertEqual(offline_client.downloaded_accessions, [])
            self.assertEqual(
                {item["fact_id"]: item["value"] for item in service.storage.get_facts(company.cik)},
                facts_before,
            )
            _assert_retry_inline_cohort(self, service, company, expected_accessions)

    def test_financial_status_merges_verified_total_equity_facts(self) -> None:
        company = build_company("300005.SZ", "Status Alias Company")
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(company)
            for year in (2025, 2024, 2023):
                accession = f"status-{year}"
                filing = FilingDocument(
                    f"status-doc-{year}", company.security_id, accession,
                    "ANNUAL_REPORT", "FY", f"{year}-12-31", f"{year + 1}-03-01",
                    f"status-{year}.pdf", f"https://example.test/{accession}",
                )
                service.storage.save_filings([filing])
                fact = FinancialFact(
                    f"status-equity-{year}", company.security_id, "total_equity", "Total equity",
                    100.0, "CNY", year, "FY", "ANNUAL_REPORT", f"{year}-01-01",
                    f"{year}-12-31", f"{year + 1}-03-01", accession, filing.source_url,
                    currency="CNY", statement="balance_sheet", validation_status="VERIFIED",
                )
                validation = FinancialValidation(
                    ValidationStatus.VERIFIED, (), frozenset({
                        "revenue", "net_income", "operating_cash_flow", "assets", "liabilities",
                    }), (), (),
                )
                service.storage.replace_financial_ingestion(
                    company.security_id, [accession], [fact], [],
                    [FinancialGroupValidation((accession, filing.period_end, "FY", "consolidated", "CNY"), validation)],
                )

            status = _financial_status(
                service.storage,
                company.to_dict(),
                {"company": company.to_dict(), "research_configuration": {"annual_history_years": 2}},
            )
            self.assertEqual(status["state"], "complete")
            self.assertEqual(status["unverified_periods"], [])

    def test_financial_status_does_not_invent_an_annual_gap_from_newer_interim_reports(self) -> None:
        company = build_company("002594.SZ", "BYD", reporting_currency="CNY")
        required = frozenset({
            "revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity",
        })
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(company)
            filings: list[FilingDocument] = []
            groups: list[FinancialGroupValidation] = []
            for year in range(2020, 2026):
                accession = f"annual-{year}"
                filing = FilingDocument(
                    f"doc-{accession}", company.security_id, accession,
                    "ANNUAL_REPORT", "FY", f"{year}-12-31", f"{year + 1}-03-01",
                    f"{year}-annual.pdf", f"https://example.test/{accession}",
                )
                filings.append(filing)
                groups.append(FinancialGroupValidation(
                    (accession, filing.period_end, "FY", "consolidated", "CNY"),
                    FinancialValidation(ValidationStatus.VERIFIED, (), required, (), ()),
                ))
            for period, end in (("Q1", "2026-03-31"), ("H1", "2026-06-30")):
                accession = f"interim-{period.lower()}-2026"
                filing = FilingDocument(
                    f"doc-{accession}", company.security_id, accession,
                    "QUARTERLY_REPORT" if period == "Q1" else "INTERIM_REPORT",
                    period, end, "2026-08-01", f"2026-{period}.pdf",
                    f"https://example.test/{accession}",
                )
                filings.append(filing)
                groups.append(FinancialGroupValidation(
                    (accession, end, period, "consolidated", "CNY"),
                    FinancialValidation(ValidationStatus.VERIFIED, (), required, (), ()),
                ))
            service.storage.save_filings(filings)
            service.storage.replace_financial_ingestion(
                company.security_id,
                [item.accession_number for item in filings],
                [], [], groups,
            )

            status = _financial_status(
                service.storage,
                company.to_dict(),
                {"research_configuration": {"annual_history_years": 5}},
            )

            self.assertEqual(status["state"], "complete")
            self.assertEqual(status["expected_periods"], ["2025", "2024", "2023", "2022", "2021", "2020"])
            self.assertNotIn("2026", status["available_periods"])
            self.assertEqual(status["missing_periods"], [])

    def test_interim_failure_does_not_poison_verified_annual_status(self) -> None:
        company = build_company("002594.SZ", "BYD", reporting_currency="CNY")
        required = frozenset({
            "revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity",
        })
        annual = FilingDocument(
            "annual-2025", company.security_id, "annual-2025", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "2025.pdf", "https://example.test/2025.pdf",
        )
        prior_annual = FilingDocument(
            "annual-2024", company.security_id, "annual-2024", "ANNUAL_REPORT", "FY",
            "2024-12-31", "2025-03-01", "2024.pdf", "https://example.test/2024.pdf",
        )
        older_annual = FilingDocument(
            "annual-2023", company.security_id, "annual-2023", "ANNUAL_REPORT", "FY",
            "2023-12-31", "2024-03-01", "2023.pdf", "https://example.test/2023.pdf",
        )
        interim = FilingDocument(
            "q1-2025", company.security_id, "q1-2025", "QUARTERLY_REPORT", "Q1",
            "2025-03-31", "2025-04-30", "2025-q1.pdf", "https://example.test/2025-q1.pdf",
        )
        annual_group = FinancialGroupValidation(
            (annual.accession_number, annual.period_end, "FY", "consolidated", "CNY"),
            FinancialValidation(ValidationStatus.VERIFIED, (), required, (), ()),
        )
        prior_annual_group = FinancialGroupValidation(
            (prior_annual.accession_number, prior_annual.period_end, "FY", "consolidated", "CNY"),
            FinancialValidation(ValidationStatus.VERIFIED, (), required, (), ()),
        )
        older_annual_group = FinancialGroupValidation(
            (older_annual.accession_number, older_annual.period_end, "FY", "consolidated", "CNY"),
            FinancialValidation(ValidationStatus.VERIFIED, (), required, (), ()),
        )
        interim_group = FinancialGroupValidation(
            (interim.accession_number, interim.period_end, "Q1", "consolidated", "CNY"),
            FinancialValidation(ValidationStatus.REJECTED, ("quarterly_statement_incomplete",), frozenset(), (), ()),
        )
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(company)
            service.storage.save_filings([annual, prior_annual, older_annual, interim])
            service.storage.replace_financial_ingestion(
                company.security_id, [annual.accession_number, prior_annual.accession_number, older_annual.accession_number, interim.accession_number],
                [], [], [annual_group, prior_annual_group, older_annual_group, interim_group],
            )

            status = _financial_status(
                service.storage, company.to_dict(),
                {"research_configuration": {"annual_history_years": 2}},
            )

        self.assertEqual(status["state"], "complete", status)
        annual_node = next(item for item in status["nodes"] if item["period"] == "2025")
        self.assertEqual(annual_node["state"], "verified")
        q1_node = next(item for item in status["interim_nodes"] if item["period"] == "2025-03-31")
        self.assertEqual(q1_node["state"], "rejected")

    def test_demo_research_job_reports_progress_and_produces_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            started = service.start_research({"mode": "demo"})
            self.assertEqual(started["state"], "queued")
            self.assertNotIn("api_key", str(started))

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "demo research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["percent"], 100)
            self.assertTrue(status["run_id"])
            report = service.get_report(status["run_id"])
            self.assertIn("OpenThesis", report["markdown"])

    def test_research_job_exposes_agent_progress_and_cancel_acknowledgement(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider:
            def test_connection(self) -> str:
                return "connected"

            def generate(self, _system_prompt: str, _user_prompt: str, *, json_mode: bool = True):
                entered.set()
                release.wait(timeout=2)
                return {"claims": []}

        def provider_factory(_config):
            return BlockingProvider()

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), provider_factory=provider_factory)
            started = service.start_research(
                {
                    "mode": "demo",
                    "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
                }
            )
            self.assertIn("agent_states", started)
            self.assertIn("total_agents", started)
            deadline = time.monotonic() + 3
            while not entered.is_set():
                self.assertLess(time.monotonic(), deadline, "provider did not start")
                time.sleep(0.01)
            cancelling = service.cancel_research(started["job_id"])
            self.assertEqual(cancelling["state"], "cancelling")
            self.assertTrue(cancelling["cancel_requested"])
            self.assertNotEqual(cancelling["message"], started["message"])
            release.set()
            deadline = time.monotonic() + 3
            status = service.get_research_status(started["job_id"])
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "cancelled job did not settle")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

    def test_python_service_has_no_model_connection_or_discovery_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            self.assertFalse(hasattr(service, "test_model_connection"))
            self.assertFalse(hasattr(service, "discover_models_for_session"))
            self.assertFalse(hasattr(service, "model_catalog"))
    def test_unknown_research_job_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(KeyError, "research job not found"):
                service.get_research_status("missing")

    def test_bootstrap_exposes_common_companies_and_ot_packs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            result = service.bootstrap()
            self.assertIn("company.search", result["capabilities"])
            self.assertIn("ot.compile", result["capabilities"])
            self.assertNotIn("models.discover", result["capabilities"])
            self.assertNotIn("model_catalog", result)
            self.assertTrue(result["common_companies"])
            self.assertTrue(all("research_readiness" in item for item in result["common_companies"]))
            self.assertTrue(result["research_packs"])
            self.assertTrue(all(item["content_hash"] for item in result["research_packs"]))
    def test_company_search_uses_saved_sec_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), sec_client_factory=_FakeSecClient)
            service.update_preferences(
                {"sec_contact_profile": "personal", "sec_contact_email": "me@example.com"}
            )

            matches = service.search_companies("acme", request_id="search-us-acme")

            self.assertEqual(matches["contract"], "openthesis.company-search.v2")
            self.assertEqual(matches["state"], "ready")
            self.assertEqual(matches["request_id"], "search-us-acme")
            self.assertEqual(matches["query"], "acme")
            self.assertEqual(matches["market"], "US")
            self.assertEqual(matches["companies"][0]["ticker"], "ACME")
            self.assertEqual(matches["companies"][0]["identity_state"], "official_verified")

    def test_us_company_search_returns_stable_missing_sec_email_error(self) -> None:
        from openthesis.service import ServiceConfigurationError

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), sec_client_factory=_FakeSecClient)
            with self.assertRaises(ServiceConfigurationError) as raised:
                service.search_companies("AAPL", market="US")
            self.assertEqual(raised.exception.code, "SEC_EMAIL_CONFIG_REQUIRED")

    def test_first_thesis_save_upserts_explicit_company_identity(self) -> None:
        from openthesis.service import ServiceConfigurationError

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            with self.assertRaises(ServiceConfigurationError) as raised:
                service.save_thesis_version("US:NYSE:BRK.B", {"thesis": "value"})
            self.assertEqual(raised.exception.code, "COMPANY_IDENTITY_REQUIRED")

            company = Company(
                cik="US:NYSE:BRK.B", ticker="BRK.B", name="Berkshire Hathaway",
                exchange="NYSE", market="US", security_id="US:NYSE:BRK.B",
            )
            saved = service.save_thesis_version(
                company.cik,
                {"thesis": "value"},
                company=company.to_dict(),
            )
            self.assertEqual(saved["company_cik"], company.cik)
            self.assertTrue(service.storage.company_exists(company.cik))

    def test_a_share_search_does_not_require_sec_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            market_data = _FakeMarketData()
            service = AppService(Path(directory), market_data=market_data)

            matches = service.search_companies("832982", market="CN_A", request_id="search-cn-bj")

            self.assertEqual(matches["contract"], "openthesis.company-search.v2")
            self.assertEqual(matches["state"], "ready")
            self.assertEqual(matches["request_id"], "search-cn-bj")
            self.assertEqual(matches["query"], "832982")
            self.assertEqual(matches["market"], "CN_A")
            self.assertEqual(matches["companies"][0]["ticker"], "832982.BJ")
            self.assertEqual(matches["companies"][0]["listing_currency"], "CNY")
            self.assertEqual(matches["companies"][0]["identity_state"], "official_verified")
            self.assertEqual(market_data.calls, [("832982", "CN_A", 15)])

    def test_manual_market_snapshot_is_explicit_and_currency_aware(self) -> None:
        company = Company(cik="HK:00700", ticker="00700.HK", name="Tencent", exchange="HKEX", market="HK", listing_currency="HKD", reporting_currency="CNY")

        snapshot = _market_snapshot(
            {
                "price": 555.5,
                "market_cap_billions": 5_200,
                "currency": "HKD",
                "as_of": "2026-08-09",
            },
            company,
        )

        self.assertEqual(snapshot["source"], "manual")
        self.assertEqual(snapshot["market_cap"], 5_200_000_000_000)
        self.assertEqual(snapshot["currency"], "HKD")
        with self.assertRaisesRegex(ValueError, "as-of date"):
            _market_snapshot({"price": 1}, company)

    def test_completed_run_does_not_inherit_old_financial_retry_state(self) -> None:
        class StorageStub:
            def get_filings(self, _key): return []
            def get_facts(self, _key): return []
            def get_financial_research_snapshot(self, _key): return {"facts": [], "storage_issues": []}
            def get_validation_groups(self, _key): return []
            def get_financial_retry_state(self, _key):
                return {"attempt_count": 4, "last_stage": "filing-validation", "last_error": "FILING_FETCH_FAILED", "updated_at": "2026-09-07"}
            def get_financial_recovery_cases(self, _run_id): return []
        payload = {"cik": "0001", "ticker": "002594.SZ", "name": "Fixture", "market": "CN_A", "listing_currency": "CNY", "reporting_currency": "CNY"}
        status = _financial_status(StorageStub(), payload, {"status": "completed"}, run_id="new-run")
        self.assertEqual(status["attempt_count"], 0)
        self.assertEqual(status["last_stage"], "")
        self.assertNotIn("FILING_FETCH_FAILED", json.dumps(status))

    def test_default_snapshot_service_uses_fx_and_secure_configured_adapter_seam(self) -> None:
        class Configured:
            name = "configured"
            def quote(self, company, mapping, *, policy):
                today = datetime.now(timezone.utc).date().isoformat()
                return QuoteSnapshot("configured", mapping.provider_symbol, mapping.exchange, 300, 100, "HKD", today, f"{today}T00:00:00+00:00")
        class Noop:
            def __init__(self, *args, **kwargs): self.name = "disabled"
            def quote(self, company, mapping, *, policy): raise ValueError("disabled")
        class Fx:
            def rate(self, source, target, as_of):
                return type("FxResult", (), {"rate": 0.13, "as_of": as_of, "source": "fixture-ecb"})()
        company = Company(cik="HK:00700", ticker="00700.HK", name="Tencent", exchange="HKEX", market="HK", listing_currency="HKD", reporting_currency="CNY")
        with patch("openthesis.service.FxRouter", return_value=Fx()), patch(
            "openthesis.service.EastmoneyPublicQuoteAdapter", Noop
        ), patch("openthesis.service.YahooChartQuoteAdapter", Noop):
            with tempfile.TemporaryDirectory() as directory:
                service = AppService(Path(directory), market_quote_adapter_factory=lambda storage: Configured())
                result = service.preview_market_snapshot({"company": company.to_dict()})
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["quote_currency"], "HKD")
        self.assertEqual(result["valuation_currency"], "CNY")
        self.assertEqual(result["equity_market_value"], 13.0)
        self.assertNotIn("api_key", json.dumps(result))

    def test_model_discovery_and_credentials_are_owned_by_rust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            self.assertFalse(hasattr(service, "discover_models_for_session"))
            self.assertNotIn("models.discover", service.hello()["capabilities"])
            self.assertNotIn("models.catalog", service.hello()["capabilities"])
    def test_real_company_research_uses_sec_data_and_produces_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), sec_client_factory=_FakeSecClient)
            service.update_preferences(
                {"sec_contact_profile": "personal", "sec_contact_email": "me@example.com"}
            )
            started = service.start_research(
                {
                    "mode": "company",
                    "company": {
                        "cik": "0000000001",
                        "ticker": "ACME",
                        "name": "Acme Corp",
                        "exchange": "",
                    },
                    "download_filings": False,
                    "model": {},
                }
            )

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "company research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            report = service.get_report(status["run_id"])
            self.assertEqual(report["ticker"], "ACME")
            self.assertIn("OpenThesis", report["markdown"])

    def test_thesis_edits_append_versions_through_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company(cik="0000000001", ticker="ACME", name="Acme Corp")
            service.storage.save_company(company)
            service.storage.save_thesis_version(
                company.cik,
                {"thesis": "initial"},
                created_by="test",
                created_at=utc_now_iso(),
            )

            saved = service.save_thesis_version(
                "0000000001", {"thesis": "revised"}
            )

            self.assertEqual(saved["version"], 2)
            self.assertEqual(service.list_theses()[0]["version"], 2)
            loaded = service.get_thesis(saved["thesis_version_id"])
            self.assertEqual(loaded["content"]["thesis"], "revised")

    def test_bootstrap_recovers_research_left_running_by_previous_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = AppService(data_dir)
            run = ResearchRun(
                run_id="interrupted-run",
                company=Company(cik="0000000001", ticker="ACME", name="Acme Corp"),
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="none",
                model_id="",
                data_as_of=utc_now_iso(),
                status=RunStatus.RUNNING,
            )
            first.storage.save_company(run.company)
            first.storage.save_run(run)

            recovered = AppService(data_dir)

            self.assertEqual(recovered.bootstrap()["interrupted_runs"], 1)
            self.assertEqual(
                recovered.storage.get_run(run.run_id)["status"],
                RunStatus.PARTIAL.value,
            )

    def test_installs_declarative_ot_from_protocol_payload(self) -> None:
        draft = minimal_studio_draft()
        draft["package"]["id"] = "service.pack"
        draft["package"]["name"] = "Service Pack"
        raw, compiled = compile_studio_draft(draft)
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            installed = service.install_research_pack(
                "service.ot", base64.b64encode(raw).decode("ascii")
            )
            self.assertEqual(installed["pack_id"], "service.pack")
            self.assertEqual(installed["content_hash"], compiled.content_identity)
            self.assertTrue(any(pack["pack_id"] == "service.pack" for pack in service.research_packs()))
    def test_legacy_raw_model_credentials_are_rejected_before_provider_creation(self) -> None:
        created = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), provider_factory=lambda config: created.append(config)
            )
            with self.assertRaisesRegex(ValueError, "legacy model configuration"):
                service.start_research({
                    "mode": "demo",
                    "model": {
                        "provider": "openai",
                        "model": "gpt-test",
                        "base_url": "https://api.example.test/v1",
                        "api_key": "must-not-enter-python",
                    },
                })
            self.assertEqual(created, [])
            self.assertNotIn("must-not-enter-python", str(service.__dict__))
    def test_model_comparison_requires_configured_unique_comparison_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            with self.assertRaisesRegex(ValueError, "comparison_models must contain between one and four models"):
                service.start_research({
                    "mode": "demo",
                    "model": {
                        "configured_model_id": "test.primary",
                        "configuration_version": 1,
                        "role": "primary",
                    },
                    "compare_enabled": True,
                    "comparison_models": [],
                })
    def test_sec_latest_invalid_group_falls_back_to_prior_verified_year(self) -> None:
        company = build_company("AAPL", "Apple")

        def make_fact(accession: str, end: str, concept: str, value: float) -> FinancialFact:
            statement = (
                "cash_flow" if concept == "operating_cash_flow"
                else "balance_sheet" if concept in {"assets", "liabilities", "equity", "total_equity"}
                else "income_statement"
            )
            year = end[:4]
            return FinancialFact(
                f"{accession}:{concept}", company.cik, concept, concept, value, "USD", int(year),
                "FY", "10-K", f"{year}-01-01", end, f"{int(year) + 1}-02-01", accession,
                f"https://www.sec.gov/Archives/{accession}", statement=statement,
                scope="consolidated", consolidated_scope="consolidated", currency="USD",
                raw_text=f"{concept} {value}", validation_status="VERIFIED",
            )

        concepts = (
            "revenue", "net_income", "operating_cash_flow", "assets", "liabilities",
            "equity", "total_equity",
        )
        old_values = (100.0, 20.0, 25.0, 200.0, 80.0, 120.0, 120.0)
        latest_values = (110.0, 22.0, 25.0, 200.0, 80.0, 120.0, 10.0)
        facts = [
            *(make_fact("old-10k", "2024-12-31", concept, value) for concept, value in zip(concepts, old_values)),
            *(make_fact("latest-10k", "2025-12-31", concept, value) for concept, value in zip(concepts, latest_values)),
        ]
        result = _latest_sec_verified_group(
            facts, FinancialIngestionEngine(), expected_period_end="2025-12-31"
        )
        self.assertIsNotNone(result)
        self.assertEqual({fact.end_date for fact in result or ()}, {"2024-12-31"})

    def test_sec_latest_shares_only_group_does_not_pollute_prior_reporting_currency(self) -> None:
        company = Company(
            "0001046179", "TSM", "TSMC", market="US",
            listing_currency="USD", reporting_currency="TWD",
        )
        statements = {
            "revenue": "income_statement", "net_income": "income_statement",
            "operating_cash_flow": "cash_flow", "assets": "balance_sheet",
            "liabilities": "balance_sheet", "equity": "balance_sheet",
        }
        values = {
            "revenue": 100.0, "net_income": 20.0, "operating_cash_flow": 30.0,
            "assets": 200.0, "liabilities": 80.0, "equity": 120.0,
        }
        facts = [FinancialFact(
            f"old:{concept}", company.cik, concept, concept, value, "TWD", 2024,
            "FY", "20-F", "2024-01-01" if statement != "balance_sheet" else None,
            "2024-12-31", "2025-03-01", "old-20f",
            "https://www.sec.gov/Archives/old", statement=statement,
            scope="consolidated", consolidated_scope="consolidated", currency="TWD",
            raw_text=f"{concept} {value}", validation_status="VERIFIED",
        ) for concept, value in values.items() for statement in (statements[concept],)]
        facts.append(FinancialFact(
            "new:shares", company.cik, "shares_outstanding",
            "EntityCommonStockSharesOutstanding", 10.0, "SHARES", 2025,
            "FY", "20-F", None, "2025-12-31", "2026-03-01", "new-20f",
            "https://www.sec.gov/Archives/new", statement="summary",
            scope="consolidated", consolidated_scope="consolidated", currency="SHARES",
            raw_text="shares 10", validation_status="VERIFIED",
        ))

        result = _latest_sec_verified_group(
            facts, FinancialIngestionEngine(), company=company,
            expected_period_end="2025-12-31",
        )

        self.assertIsNotNone(result)
        self.assertEqual({fact.end_date for fact in result or ()}, {"2024-12-31"})
        self.assertEqual({fact.currency for fact in result or ()}, {"TWD"})

    def test_sec_latest_wrong_scope_or_currency_is_not_researched(self) -> None:
        company = build_company("AAPL", "Apple")
        values = {
            "revenue": 100.0,
            "net_income": 20.0,
            "operating_cash_flow": 25.0,
            "assets": 200.0,
            "liabilities": 100.0,
            "equity": 100.0,
        }
        statements = {
            "revenue": "income_statement", "net_income": "income_statement",
            "operating_cash_flow": "cash_flow", "assets": "balance_sheet",
            "liabilities": "balance_sheet", "equity": "balance_sheet",
        }
        facts = [FinancialFact(
            f"wrong:{concept}", company.cik, concept, concept, value, "EUR", 2025,
            "FY", "10-K", "2025-01-01" if concept not in {"assets", "liabilities", "equity"} else None,
            "2025-12-31", "2026-02-01", "wrong", "https://www.sec.gov/Archives/wrong",
            scope="parent", statement=statements[concept], consolidated_scope="parent",
            currency="EUR", source_document="wrong.htm", raw_text=f"{concept} {value}",
            parser_version="sec-fixture",
        ) for concept, value in values.items()]
        self.assertIsNone(
            _latest_sec_verified_group(
                facts, FinancialIngestionEngine(), company=company,
                expected_period_end="2025-12-31",
            )
        )

    def test_duplicate_recovery_requests_share_one_provider_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            plan = RecoveryPlan("model-stages", ("financial-analyst",), "retry", ())
            service._recovery_planner = SimpleNamespace(
                plan=lambda *_args, **_kwargs: plan,
            )
            service._continuity = SimpleNamespace(
                snapshot=lambda _run_id: {},
                checkpoint=lambda *_args, **_kwargs: None,
            )
            service.get_report = lambda run_id: {"run_id": run_id, "shared": True}
            entered = threading.Event()
            release = threading.Event()
            calls = 0
            results: list[dict[str, object]] = []

            def operation(received_plan: RecoveryPlan, authorization_id: str) -> dict[str, object]:
                nonlocal calls
                self.assertIs(received_plan, plan)
                self.assertEqual(authorization_id, "")
                calls += 1
                entered.set()
                self.assertTrue(release.wait(2))
                return {"run_id": "run", "owner": True}

            def execute() -> None:
                results.append(service._execute_recovery_plan(
                    "run", target="model-stages", operation=operation,
                ))

            first = threading.Thread(target=execute)
            second = threading.Thread(target=execute)
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            time.sleep(0.05)
            self.assertEqual(calls, 1)
            release.set()
            first.join(2)
            second.join(2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(calls, 1)
            self.assertEqual(len(results), 2)
            self.assertTrue(any(result.get("shared") for result in results))


class FinancialRetryContractTests(unittest.TestCase):
    def test_legacy_snapshot_without_derived_contract_is_stale(self) -> None:
        company = build_company("legacy-snapshot", "LEGACY", reporting_currency="CNY")
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(company)
            status = _financial_status(
                service.storage,
                company.to_dict(),
                {
                    "status": "completed",
                    "data_snapshot": {"financial_fact_ids_sha256": "old-digest"},
                },
            )
            self.assertTrue(status["snapshot_stale"])

    def test_canonical_retry_snapshot_requires_matching_saved_hash(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        filing = FilingDocument(
            "complete-retry", company.security_id, "complete-retry", "ANNUAL_REPORT", "FY",
            "2025-12-31", "2026-03-01", "2025.pdf", "https://example.test/2025.pdf",
        )
        values = {
            "revenue": (100.0, "income_statement"),
            "net_income": (20.0, "income_statement"),
            "operating_cash_flow": (25.0, "cash_flow"),
            "assets": (200.0, "balance_sheet"),
            "liabilities": (80.0, "balance_sheet"),
            "equity": (120.0, "balance_sheet"),
        }
        facts = [FinancialFact(
            f"complete-retry:{concept}", company.security_id, concept, concept, value, "CNY", 2025,
            "FY", "ANNUAL_REPORT", "2025-01-01" if statement != "balance_sheet" else None,
            "2025-12-31", "2026-03-01", "complete-retry", filing.source_url,
            statement=statement, scope="consolidated", consolidated_scope="consolidated",
            currency="CNY", source_document="2025.pdf", source_page=1,
            raw_text=f"{concept} {value}", parser_version="fixture-parser",
            validation_status="VERIFIED",
        ) for concept, (value, statement) in values.items()]
        digest = _canonical_snapshot_digest(sorted(item.fact_id for item in facts))
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            service.storage.save_company(company)
            service.storage.save_filings([filing])
            service.storage.save_facts(facts)
            for snapshot, expected_error in (
                ({}, "FINANCIAL_SNAPSHOT_STALE"),
                ({"financial_fact_ids_sha256": "wrong"}, "FINANCIAL_SNAPSHOT_STALE"),
            ):
                with self.assertRaisesRegex(ValueError, expected_error):
                    _canonical_retry_snapshot(service.storage, company, {"data_snapshot": snapshot})
            retry_facts = _canonical_retry_snapshot(
                service.storage, company,
                {"data_snapshot": {"financial_fact_ids_sha256": digest}},
            )
            self.assertEqual(
                {item.concept for item in retry_facts}, set(values)
            )

    def test_model_retry_rejects_incomplete_canonical_snapshot_before_provider(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        provider_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            filing = FilingDocument(
                "retry-incomplete", company.security_id, "retry-incomplete", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "2025.pdf", "https://example.test/2025.pdf",
            )
            fact = FinancialFact(
                "retry-incomplete:revenue", company.security_id, "revenue", "Revenue", 10.0,
                "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31",
                "2026-03-01", "retry-incomplete", filing.source_url,
                statement="income_statement", scope="consolidated", currency="CNY",
                raw_text="Revenue 10", validation_status="VERIFIED",
            )
            service.storage.save_company(company)
            service.storage.save_filings([filing])
            service.storage.save_facts([fact])
            service.storage.save_run(ResearchRun(
                run_id="retry-incomplete-run", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1", provider_id="unused",
                model_id="unused", data_as_of=utc_now_iso(), status=RunStatus.PARTIAL,
            ))
            model = {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"}
            for method in (service.retry_research_synthesis, service.retry_research_growth):
                with self.assertRaisesRegex(ValueError, "MODEL_STAGE_RESUME_NOT_AVAILABLE"):
                    method("retry-incomplete-run", model)
            self.assertEqual(provider_calls, [])

    def test_profile_aware_sec_cache_latest_and_status_paths(self) -> None:
        """Financial service gates follow the declared industry profile.

        The specialized profiles require four balance/income concepts and do
        not inherit the industrial revenue/operating-cash-flow gate.  Exercise
        the cache, latest-group, and user-facing status projections together so
        a service-side shortcut cannot silently reintroduce the old rule.
        """
        concepts = {
            "net_income": "income_statement",
            "assets": "balance_sheet",
            "liabilities": "balance_sheet",
            "total_equity": "balance_sheet",
        }
        for industry in ("banking", "insurance", "securities brokerage"):
            with self.subTest(industry=industry), tempfile.TemporaryDirectory() as directory:
                company = Company(
                    f"US:profile-{industry}", "PROFILE", "Profile issuer",
                    market="US", listing_currency="USD", reporting_currency="USD",
                    industry=industry,
                )
                path = Path(directory) / "annual.htm"
                path.write_text("official filing", encoding="utf-8")
                filing = FilingDocument(
                    f"doc-{industry}", company.cik, f"acc-{industry}", "10-K", "FY",
                    "2025-12-31", "2026-02-01", "annual.htm",
                    "https://www.sec.gov/Archives/profile/annual.htm",
                    local_path=str(path), content_hash="profile-hash",
                )
                facts = []
                for concept, statement in concepts.items():
                    facts.append(FinancialFact(
                        f"{filing.accession_number}:{concept}", company.cik, concept,
                        concept,
                        100.0 if concept == "net_income" else 2_000.0 if concept == "assets" else 1_000.0,
                        "USD", 2025, "FY", "10-K", "2025-01-01" if concept == "net_income" else None,
                        "2025-12-31", filing.filed_at, filing.accession_number,
                        filing.source_url, scope="consolidated", statement=statement,
                        consolidated_scope="consolidated", currency="USD",
                        source_document=filing.primary_document,
                        raw_text=f"{concept} 1000", parser_version="profile-fixture",
                    ))
                self.assertTrue(_cached_us_annual_window_is_complete(
                    company, [filing], [item.to_dict() for item in facts], required_count=1,
                ))
                latest = _latest_sec_verified_group(
                    facts, FinancialIngestionEngine(), company=company,
                    expected_period_end="2025-12-31",
                )
                self.assertIsNotNone(latest)

                groups = [
                    {
                        "period_end": period,
                        "status": "VERIFIED",
                        "consolidated_scope": "consolidated",
                        "currency": "USD",
                        "covered_concepts": list(concepts),
                    }
                    for period in ("2025-12-31", "2024-12-31", "2023-12-31")
                ]
                class StorageStub:
                    def get_filings(self, _key):
                        return [
                            FilingDocument(f"{industry}-{year}", company.cik, f"{industry}-{year}", "10-K", "FY", f"{year}-12-31", "", "annual.htm", filing.source_url)
                            for year in (2025, 2024, 2023)
                        ]
                    def get_facts(self, _key):
                        return []
                    def get_financial_research_snapshot(self, _key):
                        return {"facts": [], "storage_issues": []}
                    def get_validation_groups(self, _key):
                        return groups
                    def get_financial_retry_state(self, _key):
                        return {"attempt_count": 0, "last_stage": "", "last_error": "", "updated_at": ""}
                status = _financial_status(
                    StorageStub(),
                    {**company.to_dict(), "company_type": industry},
                    {"research_configuration": {"annual_history_years": 2}},
                )
                self.assertEqual(status["state"], "complete")

    def test_financial_retry_job_keeps_status_and_cancel_rpc_responsive(self) -> None:
        company = build_company("300750.SZ", "CATL", reporting_currency="CNY")
        entered = threading.Event()
        release = threading.Event()
        provider_calls: list[str] = []

        class Adapter:
            def list_financial_filings(self, _company, *, limit=5):
                return [FilingDocument(
                    "slow-doc", company.security_id, "slow-2025", "ANNUAL_REPORT", "FY",
                    "2025-12-31", "2026-03-01", "slow.pdf", "https://example.test/slow.pdf",
                )][:limit]

            def download_filing(self, filing, target):
                entered.set()
                release.wait()
                target.mkdir(parents=True, exist_ok=True)
                path = target / "slow.pdf"
                path.write_bytes(_OFFLINE_FILING_FIXTURE_BYTES)
                return replace(
                    filing,
                    local_path=str(path),
                    content_hash=hashlib.sha256(_OFFLINE_FILING_FIXTURE_BYTES).hexdigest(),
                )

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory), market_data=_ResearchMarketData(Adapter()),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            service.storage.save_company(company)
            service.storage.save_run(ResearchRun(
                run_id="financial-job", company=company, workflow_id="test",
                research_pack_id="test", research_pack_version="1",
                provider_id="unused", model_id="unused", data_as_of=utc_now_iso(),
                status=RunStatus.PARTIAL,
            ))

            started = None
            try:
                started_at = time.monotonic()
                started = service.start_financial_retry("financial-job")
                # Queueing, status inspection, and cancellation must remain
                # responsive while the official-source adapter is blocked.
                self.assertLess(time.monotonic() - started_at, 0.5)
                if not entered.wait(3):
                    public_status = service.get_research_status(started["job_id"])
                    self.fail(f"download did not start; public job status={public_status}")

                status_started_at = time.monotonic()
                live = service.get_research_status(started["job_id"])
                self.assertLess(time.monotonic() - status_started_at, 0.5)
                self.assertIn(live["state"], {"queued", "running"})

                cancel_started_at = time.monotonic()
                cancelling = service.cancel_research(started["job_id"])
                self.assertLess(time.monotonic() - cancel_started_at, 0.5)
                self.assertEqual(cancelling["state"], "cancelling")

                release.set()
                deadline = time.monotonic() + 5
                terminal = cancelling
                while terminal["state"] not in {"cancelled", "failed", "completed"}:
                    self.assertLess(time.monotonic(), deadline, f"public job status={terminal}")
                    time.sleep(0.01)
                    terminal = service.get_research_status(started["job_id"])
                self.assertEqual(terminal["state"], "cancelled")
                self.assertEqual(provider_calls, [])
            finally:
                # Never clean the SQLite directory while its background job
                # may still be using it, including when an assertion fails.
                release.set()
                if started is not None:
                    cleanup_deadline = time.monotonic() + 10
                    cleanup_status = service.get_research_status(started["job_id"])
                    while cleanup_status["state"] not in {"cancelled", "failed", "completed"}:
                        if time.monotonic() >= cleanup_deadline:
                            break
                        time.sleep(0.02)
                        cleanup_status = service.get_research_status(started["job_id"])

    def test_financial_retry_result_is_explicit_and_serializable(self) -> None:
        result = FinancialRetryResult(
            mode="retry",
            targets=("fy-2025",),
            downloaded=("fy-2025",),
            accepted=("fy-2025:revenue",),
            rejected=("fy-2024:assets",),
            status="partial",
            error="FILING_DATA_QUALITY_FAILED",
            updated_artifacts=("deterministic-financial-summary", "research-report"),
        ).to_dict()
        self.assertEqual(result["mode"], "retry")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["updated_artifacts"], [
            "deterministic-financial-summary", "research-report"
        ])
        self.assertNotEqual(result["status"], "succeeded")


def _download_filing(document_id: str, host: str, *, url: str | None = None) -> FilingDocument:
    return FilingDocument(
        document_id=document_id,
        company_cik="fixture",
        accession_number=document_id,
        form_type="ANNUAL_REPORT",
        fiscal_period="FY",
        period_end="2025-12-31",
        filed_at="2026-03-01T00:00:00+00:00",
        primary_document=document_id,
        source_url=url or f"https://{host}/{document_id}.pdf",
    )


if __name__ == "__main__":
    unittest.main()
