"""Offline production collaborators, not a fake admission/publication shortcut."""
from concurrent.futures import CancelledError
from dataclasses import replace
import hashlib
from threading import Event

import pytest

from openthesis.application.financials import OfficialFinancialPreparer, OfficialFinancialRequest, OfficialFinancialUnavailable
from openthesis.financial_ingestion import FinancialIngestionEngine
from openthesis.financial_pipeline import FinancialPipeline
from openthesis.financial_recovery import FinancialRecoveryController
from openthesis.financial_sources.acquisition import OfficialFilingAcquisition
from openthesis.sec_client import SecClient, SecFinancialSourceAdapter
from openthesis.storage import Storage
from openthesis.domain import ResearchRun
from openthesis.market_data import MarketDataError
from test_inline_xbrl_source import source


INSTANT = '''<x:context id="i"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">0001045810</x:identifier></x:entity>
<x:period><x:instant>2026-01-25</x:instant></x:period></x:context>'''
VALUES = (
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "c", "1000"),
    ("NetIncomeLoss", "c", "100"),
    ("NetCashProvidedByUsedInOperatingActivities", "c", "110"),
    ("Assets", "i", "1000"),
    ("Liabilities", "i", "600"),
    ("StockholdersEquity", "i", "400"),
)


class OfflineOfficialSource:
    def __init__(self, company, filing):
        self.company = company
        self.filing = filing
        self.facts_calls = 0
        self.download_calls = 0
        self.list_limits = []
        self.identity_search_calls = 0

    def search_companies(self, query, limit=15):
        self.identity_search_calls += 1
        normalized = query.strip().casefold()
        if limit < 1 or normalized not in {
            self.company.ticker.casefold(), self.company.name.casefold(),
        }:
            return []
        return [self.company]

    def list_annual_filings(self, _company, limit):
        self.list_limits.append(limit)
        return [replace(self.filing)]

    def get_company_facts(self, _company):
        self.facts_calls += 1
        raise OSError("a structured endpoint is unavailable, original remains available")

    def download_filing(self, _filing, _directory):
        self.download_calls += 1
        raise OSError("offline source cannot download")


def setup_preparer(tmp_path):
    numbers = "".join(f'<ix:nonFraction name="g:{tag}" contextRef="{context}" unitRef="u">{value}</ix:nonFraction>'
                      for tag, context, value in VALUES)
    company, filing, raw = source(numbers=numbers, extra_context=INSTANT)
    original = tmp_path / "original.htm"
    original.write_bytes(raw)
    filing = replace(filing, local_path=str(original))
    storage = Storage(tmp_path / "data")
    storage.save_company(company)
    adapter = OfflineOfficialSource(company, filing)
    preparer = OfficialFinancialPreparer(storage, FinancialRecoveryController(storage),
        OfficialFilingAcquisition(), FinancialPipeline(FinancialIngestionEngine()))
    return company, filing, adapter, preparer, storage


@pytest.mark.parametrize("code,action", [
    ("FILING_ACCESS_DENIED", "configure_official_source"),
    ("FILING_FORMAT_UNSUPPORTED", "provide_compatible_official_source"),
    ("FILING_RULE_UNSUPPORTED", "provide_compatible_official_source"),
    ("FILING_STATUS_UNVERIFIED", "inspect_official_disclosure_status"),
])
def test_durable_source_diagnostic_preserves_its_action_through_the_use_case(tmp_path, code, action):
    company, _filing, adapter, preparer, storage = setup_preparer(tmp_path)
    storage.save_run(ResearchRun("source-action", company, "wf", "pack", "1", "unused", "unused", "2026-10-06"))
    calls = []
    def unavailable(*args, **kwargs):
        calls.append("discovery")
        raise MarketDataError("private provider detail must not be shown", code=code)
    adapter.list_annual_filings = unavailable
    with pytest.raises(OfficialFinancialUnavailable) as raised:
        preparer.prepare(OfficialFinancialRequest(company, adapter, run_id="source-action", annual_history_years=1))
    assert calls == ["discovery"]
    assert raised.value.code == code
    assert raised.value.outcome.state.value == "needs_action"
    assert raised.value.outcome.next_actions == (action,)
    assert "private provider detail" not in str(raised.value.outcome.to_dict())
    assert storage.get_facts(company.cik) == []


def prior_original(tmp_path, filing):
    raw = (tmp_path / "original.htm").read_bytes()
    prior_raw = raw.replace(b"2025-01-27", b"2024-01-27").replace(b"2026-01-25", b"2025-01-25")
    prior_raw = prior_raw.replace(b">2026<", b">2025<")
    prior_path = tmp_path / "prior.htm"
    prior_path.write_bytes(prior_raw)
    return replace(filing, document_id="prior-source", accession_number="0001045810-25-000021",
        period_end="2025-01-25", filed_at="2025-02-25", primary_document="prior.htm",
        source_url="https://www.sec.gov/Archives/prior.htm", local_path=str(prior_path),
        content_hash=hashlib.sha256(prior_raw).hexdigest())


def test_original_runs_through_compiler_and_storage_despite_companyfacts_failure(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    request = OfficialFinancialRequest(company, adapter,
        candidate_sources=(SecFinancialSourceAdapter(adapter),), annual_history_years=1)
    result = preparer.prepare(request)
    assert result.publication["state"] == "promoted"
    assert result.view.model_ready
    assert {fact.concept: fact.value for fact in result.view.facts} == {
        "revenue": 1000, "parent_net_income": 100, "operating_cash_flow": 110,
        "assets": 1000, "liabilities": 600, "equity": 400,
    }
    from openthesis.financial_xbrl_semantics import INLINE_PARSER_VERSION
    assert all(fact.parser_version == INLINE_PARSER_VERSION for fact in result.view.facts)
    assert len(storage.get_financial_research_snapshot(company.cik)["facts"]) == 6
    assert adapter.facts_calls == 1 and adapter.download_calls == 0
    assert result.downloaded_accessions == ()
    assert any("structured" in diagnostic for diagnostic in result.view.diagnostics)
    assert result.filings[0].content_hash == filing.content_hash


def test_empty_official_index_reuses_only_hash_verified_originals_and_revalidates(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    first = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))
    before = storage.get_financial_research_snapshot(company.cik)
    adapter.list_annual_filings = lambda *_args, **_kwargs: []
    discovery = preparer.discovery.discover(adapter, company, annual_limit=1)
    assert discovery.error_code == "NO_FILINGS_AVAILABLE"
    assert discovery.freshness == "freshness_unverified"
    assert discovery.filings[0].content_hash == filing.content_hash
    second = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1, repair=True))
    assert second.discovery.freshness == "freshness_unverified"
    assert "verified_local_cohort_reused_without_remote_refresh" in second.discovery.diagnostics
    assert second.publication is None
    assert second.view.model_ready
    assert second.view.facts == first.view.facts
    assert storage.get_financial_research_snapshot(company.cik) == before
    assert adapter.download_calls == 0


def test_hashless_local_file_never_counts_as_verified_discovery_cache(tmp_path):
    company, filing, _adapter, _preparer, storage = setup_preparer(tmp_path)
    hashless = replace(filing, content_hash="")
    storage.save_filings([hashless])
    assert FinancialRecoveryController._cache_valid(hashless) is False


def test_cancellation_at_publication_boundary_cannot_advance_financial_heads(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    cancelled = Event()
    before = storage.get_financial_research_snapshot(company.cik)
    publish = storage.replace_financial_ingestion
    def arriving_cancel(*args, **kwargs):
        cancelled.set()
        return publish(*args, **kwargs)
    storage.replace_financial_ingestion = arriving_cancel
    with pytest.raises(CancelledError):
        preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1),
                         cancel_check=cancelled.is_set)
    assert storage.get_financial_research_snapshot(company.cik) == before


def test_real_preparation_records_and_recovers_transient_discovery_without_reducing_window(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    storage.save_run(ResearchRun("source-recovery", company, "wf", "pack", "1", "", "", "2026-10-05"))
    attempts = []
    def listing(_company, limit):
        attempts.append(limit)
        if len(attempts) <= 2:
            raise OSError("offline simulated transport outage")
        return [replace(filing)]
    adapter.list_annual_filings = listing
    result = preparer.prepare(OfficialFinancialRequest(company, adapter,
        annual_history_years=1, run_id="source-recovery"))
    assert result.view.model_ready
    assert attempts == [4, 4, 4]
    assert result.recovery_outcome.state == "committed"
    with storage.connect() as db:
        actions = db.execute("SELECT * FROM recovery_actions ORDER BY updated_at").fetchall()
    assert sum(row["attempt"] for row in actions) == 3
    assert sum(row["retry_count"] for row in actions) == 1


def test_force_rebuild_reuses_proven_bytes_but_revalidates_the_same_full_cohort(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    first = preparer.prepare(OfficialFinancialRequest(company, adapter,
        candidate_sources=(SecFinancialSourceAdapter(adapter),), annual_history_years=1))
    # Discovery has no local path; only a matching stored, hashed original can
    # make the second acquisition avoid download. Force means recompile, not delete.
    adapter.filing = replace(filing, local_path="", content_hash="")
    second = preparer.prepare(OfficialFinancialRequest(company, adapter,
        candidate_sources=(SecFinancialSourceAdapter(adapter),), annual_history_years=1, force=True))
    assert second.publication["state"] == "promoted"
    assert [(fact.concept, fact.value) for fact in second.view.facts] == [
        (fact.concept, fact.value) for fact in first.view.facts]
    assert second.filings[0].content_hash == filing.content_hash
    assert adapter.download_calls == 0 and adapter.facts_calls == 2
    assert adapter.list_limits == [4, 4]
    assert len(storage.get_facts_audit(company.cik)) >= 6


def test_invalid_rebuild_retains_trusted_active_values_without_claiming_repair(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    first = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))
    before = storage.get_financial_research_snapshot(company.cik)["facts"]
    original = tmp_path / "broken.htm"
    raw = b"<html>not an XBRL statement</html>"
    original.write_bytes(raw)
    adapter.filing = replace(filing, local_path=str(original), content_hash=hashlib.sha256(raw).hexdigest())
    second = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1, force=True))
    assert second.publication["state"] != "promoted"
    assert second.view.publishable_accessions == ()
    assert storage.get_financial_research_snapshot(company.cik)["facts"] == before
    # These old inline values cannot be rebound to a different source hash.
    assert not second.view.facts
    assert any("source_document_identity_mismatch" in issue
               for group in second.view.canonical_dataset.validations for issue in group.issues)
    assert first.view.facts


def test_repaired_parser_case_moves_to_validation_when_current_original_fails_financial_checks(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    run_id = "parser-then-validation"
    storage.save_run(ResearchRun(run_id, company, "wf", "pack", "1", "", "", "2026-10-05"))
    storage.save_financial_recovery_case(run_id=run_id, company_cik=company.cik,
        accession_number=filing.accession_number, document_hash=filing.content_hash,
        stage="filing-parse", error_code="FILING_LOCAL_PARSE_FAILED",
        next_action="retry_saved_candidate_validation_or_enable_authorized_cloud")

    original = (tmp_path / "original.htm").read_bytes()
    duplicate_facts = "".join(
        f'<ix:nonFraction name="g:{tag}" contextRef="{context}" unitRef="u">{int(value) * 2}</ix:nonFraction>'
        for tag, context, value in VALUES
    ).encode()
    raw = original.replace(b"</html>", duplicate_facts + b"</html>")
    unbalanced = tmp_path / "unbalanced.htm"
    unbalanced.write_bytes(raw)
    adapter.filing = replace(filing, local_path=str(unbalanced), content_hash=hashlib.sha256(raw).hexdigest())

    result = preparer.prepare(OfficialFinancialRequest(company, adapter,
        annual_history_years=1, run_id=run_id))

    assert result.errors == ("FILING_DATA_QUALITY_FAILED",)
    assert result.publication["accessions"][filing.accession_number] != "promoted"
    case = storage.get_financial_recovery_cases(run_id)[0]
    assert case["status"] == "open"
    assert case["stage"] == "filing-validation", result.recovery_outcome
    assert case["error_code"] == "FILING_DATA_QUALITY_FAILED"
    assert case["next_action"] == "repair_required_official_evidence"


def test_publishable_accession_does_not_get_validation_recovery_case_during_preparation(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    run_id = "parser-case-publishable"
    storage.save_run(ResearchRun(run_id, company, "wf", "pack", "1", "", "", "2026-10-05"))
    storage.save_financial_recovery_case(run_id=run_id, company_cik=company.cik,
        accession_number=filing.accession_number, document_hash=filing.content_hash,
        stage="filing-parse", error_code="FILING_LOCAL_PARSE_FAILED",
        next_action="retry_saved_candidate_validation_or_enable_authorized_cloud")

    result = preparer.prepare(OfficialFinancialRequest(company, adapter,
        annual_history_years=1, run_id=run_id))

    assert result.publication["accessions"][filing.accession_number] == "promoted"
    case = storage.get_financial_recovery_cases(run_id)[0]
    assert case["status"] == "open"
    assert case["stage"] == "filing-parse"
    assert case["error_code"] == "FILING_LOCAL_PARSE_FAILED"


def test_current_source_failure_is_not_reclassified_as_financial_validation(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    run_id = "parser-case-source-failed"
    storage.save_run(ResearchRun(run_id, company, "wf", "pack", "1", "", "", "2026-10-05"))
    storage.save_financial_recovery_case(run_id=run_id, company_cik=company.cik,
        accession_number=filing.accession_number, document_hash=filing.content_hash,
        stage="filing-parse", error_code="PDFIUM_WORKER_TIMEOUT",
        next_action="retry_saved_candidate_validation_or_enable_authorized_cloud")

    adapter.filing = replace(filing, local_path="", content_hash="")
    result = preparer.prepare(OfficialFinancialRequest(company, adapter,
        annual_history_years=1, run_id=run_id))

    assert any(error.endswith(":FILING_DOWNLOAD_FAILED") for error in result.errors), result.errors
    case = storage.get_financial_recovery_cases(run_id)[0]
    assert case["status"] == "open"
    assert case["stage"] == "filing-download"
    assert case["error_code"] == "FILING_DOWNLOAD_FAILED"


def test_cancelled_preparation_does_not_publish_or_start_remote_acquisition(tmp_path):
    company, _filing, adapter, preparer, storage = setup_preparer(tmp_path)
    with pytest.raises(CancelledError):
        preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1),
                         cancel_check=lambda: True)
    assert not adapter.list_limits and not adapter.download_calls
    assert not storage.get_financial_research_snapshot(company.cik)["facts"]


def test_verified_original_currency_corrects_metadata_without_converting_values(tmp_path):
    company, _filing, adapter, preparer, _storage = setup_preparer(tmp_path)
    company.reporting_currency = "CNY"
    request = OfficialFinancialRequest(company, adapter, annual_history_years=1)
    result = preparer.prepare(request)
    assert company.reporting_currency == "CNY" and request.company.reporting_currency == "CNY"
    assert result.company.reporting_currency == result.view.reporting_currency == "USD"
    assert {fact.currency for fact in result.view.facts} == {"USD"}
    assert next(fact.value for fact in result.view.facts if fact.concept == "revenue") == 1000
    assert result.view.model_ready and not result.currency_lane_ambiguous


def test_healthy_repair_revalidates_without_source_requests_or_new_generation(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    first = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1))
    before = storage.get_financial_research_snapshot(company.cik)
    adapter.filing = replace(filing, local_path="", content_hash="")
    second = preparer.prepare(OfficialFinancialRequest(company, adapter,
        candidate_sources=(SecFinancialSourceAdapter(adapter),), annual_history_years=1, repair=True))
    assert second.publication is None and second.target_accessions == ()
    assert second.reused_accessions == (filing.accession_number,)
    assert adapter.facts_calls == adapter.download_calls == 0
    assert storage.get_financial_research_snapshot(company.cik) == before
    assert [(fact.concept, fact.value) for fact in second.view.facts] == [
        (fact.concept, fact.value) for fact in first.view.facts]


def test_partial_repair_preserves_healthy_sibling_generation_and_annual_comparison(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    prior = prior_original(tmp_path, filing)
    adapter.list_annual_filings = lambda _company, limit: [replace(filing), replace(prior)]
    preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=2))
    before = storage.get_financial_research_snapshot(company.cik)
    with storage.connect() as db:
        db.execute("UPDATE financial_facts SET value=999 WHERE accession_number=? AND concept='revenue'",
                   (filing.accession_number,))
    result = preparer.prepare(OfficialFinancialRequest(company, adapter,
        candidate_sources=(SecFinancialSourceAdapter(adapter),), annual_history_years=2, repair=True))
    assert result.target_accessions == (filing.accession_number,)
    assert result.reused_accessions == (prior.accession_number,)
    assert result.publication["state"] == "promoted"
    assert adapter.facts_calls == 1 and adapter.download_calls == 0
    after = storage.get_financial_research_snapshot(company.cik)
    assert after["generations"][prior.accession_number] == before["generations"][prior.accession_number]
    assert after["generations"][filing.accession_number] != before["generations"][filing.accession_number]
    assert {fact.fiscal_year for fact in result.view.facts} == {2025, 2026}
    assert {fact.value for fact in result.view.facts if fact.concept == "revenue"} == {1000}
    assert result.view.capability_map["annual_trend"]


def test_competing_official_rebuild_cannot_publish_from_an_outdated_snapshot(tmp_path):
    company, _filing, adapter, preparer, storage = setup_preparer(tmp_path)
    request = OfficialFinancialRequest(company, adapter, annual_history_years=1, force=True)
    preparer.prepare(request)
    run_id = "stale-existing-parser-case"
    storage.save_run(ResearchRun(run_id, company, "wf", "pack", "1", "", "", "2026-10-05"))
    storage.save_financial_recovery_case(run_id=run_id, company_cik=company.cik,
        accession_number=_filing.accession_number, document_hash=_filing.content_hash,
        stage="filing-parse", error_code="FILING_LOCAL_PARSE_FAILED",
        next_action="retry_saved_candidate_validation_or_enable_authorized_cloud")
    original_publish = storage.replace_financial_ingestion
    competing = {}
    def publish_after_competitor(*args, **kwargs):
        storage.replace_financial_ingestion = original_publish
        preparer.prepare(request)
        competing.update(storage.get_financial_research_snapshot(company.cik))
        return original_publish(*args, **kwargs)
    storage.replace_financial_ingestion = publish_after_competitor
    with pytest.raises(OfficialFinancialUnavailable, match="FINANCIAL_GENERATION_CHANGED"):
        preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=1,
            force=True, run_id=run_id))
    assert storage.get_financial_research_snapshot(company.cik) == competing
    case = storage.get_financial_recovery_cases(run_id)[0]
    assert case["status"] == "open"
    assert case["stage"] == "filing-parse"
    assert case["error_code"] == "FILING_LOCAL_PARSE_FAILED"
    with storage.connect() as db:
        assert db.execute("SELECT count(*) FROM financial_ingestion_candidates WHERE state='stale_generation'").fetchone()[0] == 1


def test_failed_byte_download_still_validates_official_structured_rows(tmp_path):
    company, filing, adapter, preparer, _storage = setup_preparer(tmp_path)
    adapter.filing = replace(filing, local_path="", content_hash="")
    payload = {"facts": {"us-gaap": {tag: {"units": {"USD": [{
        "val": int(value), "fy": 2026, "fp": "FY", "form": "10-K",
        "accn": filing.accession_number, "filed": filing.filed_at[:10], "end": filing.period_end,
        **({"start": "2025-01-27"} if context == "c" else {}),
    }]}} for tag, context, value in VALUES}}}
    client = object.__new__(SecClient)
    client._get_json = lambda *_args: payload
    adapter.get_company_facts = client.get_company_facts
    result = preparer.prepare(OfficialFinancialRequest(company, adapter,
        candidate_sources=(SecFinancialSourceAdapter(adapter),), annual_history_years=1))
    assert adapter.download_calls == 1 and result.errors
    assert result.filings[0].content_hash == "" and result.filings[0].local_path == ""
    assert result.publication["state"] == "promoted" and result.view.model_ready, (
        result.publication, result.view.diagnostics, result.view.canonical_dataset.validations)
    assert len(result.view.facts) == 6
    assert all(fact.parser_version == "sec-companyfacts-v4" for fact in result.view.facts)
    assert all(not ref.content_hash for ref in result.view.evidence)


@pytest.mark.parametrize("window", [True, 0, 11, "5", 1.5])
def test_captured_request_rejects_invalid_annual_window(window):
    company, _filing, _raw = source()
    with pytest.raises(ValueError, match="INVALID_ANNUAL_HISTORY_WINDOW"):
        OfficialFinancialRequest(company, object(), annual_history_years=window)
