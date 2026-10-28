"""The public composition uses verified originals, never fake DEMO caches."""
from openthesis.service import AppService
from test_official_financial_preparation import setup_preparer, prior_original
from openthesis.application.financials import OfficialFinancialRequest
from dataclasses import replace
from openthesis.sec_client import SecClientError


def test_unconfigured_sec_does_not_prevent_verified_cache_repair(tmp_path):
    company, filing, adapter, preparer, storage = setup_preparer(tmp_path)
    prior = prior_original(tmp_path, filing)
    adapter.list_annual_filings = lambda *_args, **_kwargs: [replace(filing), replace(prior)]
    first = preparer.prepare(OfficialFinancialRequest(company, adapter, annual_history_years=2))
    before = storage.get_financial_research_snapshot(company.cik)
    calls = []
    def no_network_client(*args):
        calls.append(args)
        raise AssertionError("no contact configuration may reach a SEC client")
    service = AppService(storage.data_dir, sec_client_factory=no_network_client)
    assert not service.preferences()["sec_contact_email"]
    trace = {}
    errors = service._retry_us_financials(company,
        {"research_configuration": {"annual_history_years": 2}}, trace=trace)
    assert errors == [] and calls == []
    assert trace["processed"] == {item.accession_number for item in first.filings}
    assert trace["freshness"] == {"freshness_unverified"}
    assert service.storage.get_financial_research_snapshot(company.cik) == before


def test_transient_discovery_without_cache_has_an_actionable_waiting_outcome(tmp_path):
    company, _filing, adapter, _preparer, storage = setup_preparer(tmp_path)
    def unavailable(*_args, **_kwargs):
        raise SecClientError("request timed out")
    adapter.list_annual_filings = unavailable
    service = AppService(storage.data_dir, sec_client_factory=lambda *_args: adapter)
    service.storage.set_setting("sec_contact_email", "offline-test@example.test")
    trace = {}
    errors = service._retry_us_financials(company,
        {"research_configuration": {"annual_history_years": 1}}, trace=trace)
    assert errors == ["FILING_FETCH_FAILED"]
    assert trace["terminal_state"] == {"waiting_external"}
    assert trace["next_action"] == {"retry_discovery"}
    assert service.storage.get_facts(company.cik) == []


def test_transient_download_failure_is_not_deterministic_input_exhaustion(tmp_path):
    company, filing, adapter, _preparer, storage = setup_preparer(tmp_path)
    adapter.filing = replace(filing, local_path="", content_hash="")
    def unavailable(*_args, **_kwargs):
        raise TimeoutError("remote source timed out")
    adapter.download_filing = unavailable
    service = AppService(storage.data_dir, sec_client_factory=lambda *_args: adapter)
    service.storage.set_setting("sec_contact_email", "offline-test@example.test")
    trace = {}
    errors = service._retry_us_financials(company,
        {"research_configuration": {"annual_history_years": 1}}, trace=trace)
    assert errors
    assert trace["terminal_state"] == {"waiting_external"}
    assert trace["next_action"] == {"retry_download"}
    assert trace["processed"] == set()
    assert service.storage.get_facts(company.cik) == []
