"""Public source wrapper boundaries use the actual journal and byte checks."""
from dataclasses import replace

import pytest

from openthesis.domain import ResearchRun
from openthesis.financial_sources.recovery import DurableOfficialSource
from openthesis.resilience.contracts import RecoveryLimits
from openthesis.resilience.strategies import RecoveryError
from test_official_financial_preparation import setup_preparer
from openthesis.financial_sources.deferred import DeferredOfficialSource
from openthesis.onboarding import build_sec_user_agent
from openthesis.market_data import MarketDataError


def source_setup(tmp_path, **kwargs):
    company, filing, adapter, _preparer, storage = setup_preparer(tmp_path)
    storage.save_run(ResearchRun("run", company, "wf", "pack", "1", "", "", "2026-10-06"))
    return company, filing, adapter, storage, DurableOfficialSource(adapter, storage, "run", company,
        sleeper=lambda _seconds: None, **kwargs)


def test_more_than_eight_required_downloads_are_not_cut_off_by_the_repair_budget(tmp_path):
    company, filing, adapter, storage, owner = source_setup(tmp_path)
    attempts = []
    def download(request, _target):
        attempts.append(request.accession_number)
        return replace(request, local_path=filing.local_path, content_hash=filing.content_hash)
    adapter.download_filing = download
    for index in range(10):
        request = replace(filing, document_id=f"document-{index}", accession_number=f"accession-{index}",
            source_url=f"https://www.sec.gov/Archives/annual-{index}.htm", local_path="", content_hash="")
        assert owner.download_filing(request, tmp_path).content_hash == filing.content_hash
    assert len(attempts) == 10
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_incidents WHERE target LIKE '%:initial'").fetchone()[0] == 10
        assert db.execute("SELECT COUNT(*) FROM recovery_incidents WHERE target LIKE 'financial-evidence:%'").fetchone()[0] == 0


def test_same_captured_discovery_reuses_verified_receipt_after_wrapper_restart(tmp_path):
    company, _filing, adapter, storage, owner = source_setup(tmp_path)
    first = owner.list_financial_filings(company, limit=13)
    second = DurableOfficialSource(adapter, storage, "run", company).list_financial_filings(company, limit=13)
    assert first == second
    assert adapter.list_limits == [13]


def test_repair_budget_shared_across_distinct_source_operations_and_restart(tmp_path):
    company, filing, adapter, storage, owner = source_setup(tmp_path,
        limits=RecoveryLimits(automatic_actions=1, calls=32, pages=20))
    seen = []
    def broken(company, limit):
        seen.append(limit)
        raise OSError("offline network outage")
    adapter.list_annual_filings = broken
    with pytest.raises(RecoveryError):
        owner.list_financial_filings(company, limit=4)
    restarted = DurableOfficialSource(adapter, storage, "run", company,
        sleeper=lambda _seconds: None, limits=RecoveryLimits(automatic_actions=100, calls=999, pages=20))
    with pytest.raises(RecoveryError, match="RECOVERY_INCIDENT_BUDGET_EXHAUSTED"):
        restarted.list_financial_filings(company, limit=5)
    # Every distinct normal operation may try once. Only one extra repair was
    # approved; neither the new target nor larger requested limits reset it.
    assert seen == [4, 4, 5]
    with storage.connect() as db:
        repair = db.execute("SELECT action_count FROM recovery_incidents WHERE target LIKE 'financial-evidence:%'").fetchone()
    assert repair["action_count"] == 1


def test_byte_mutation_blocks_previously_committed_download_receipt(tmp_path):
    company, filing, adapter, storage, owner = source_setup(tmp_path)
    adapter.download_filing = lambda *_args: replace(filing)
    request = replace(filing, local_path="", content_hash="")
    assert owner.download_filing(request, tmp_path).content_hash == filing.content_hash
    # This is a test-owned copy, not a frozen public corpus original.
    (tmp_path / "original.htm").write_bytes(b"changed test-owned bytes")
    with pytest.raises((ValueError, RecoveryError), match="HASH|INTEGRITY|REJECTED"):
        DurableOfficialSource(adapter, storage, "run", company).download_filing(request, tmp_path)
    assert adapter.download_calls == 0


def test_missing_sec_contact_is_needs_action_without_transient_retry(tmp_path):
    company, filing, _adapter, storage, _owner = source_setup(tmp_path)
    calls = []
    def factory():
        calls.append("configuration")
        build_sec_user_agent("personal", "")
        raise AssertionError("must not construct a network client")
    owner = DurableOfficialSource(DeferredOfficialSource(factory), storage, "run", company)
    with pytest.raises(RecoveryError) as error:
        owner.list_annual_filings(company)
    assert error.value.code == "SEC_CONTACT_CONFIGURATION_REQUIRED"
    assert error.value.needs_action == "configure_sec_contact"
    assert calls == ["configuration"]


def test_deterministic_source_failure_never_repeats_after_wrapper_restart(tmp_path):
    company, filing, adapter, storage, owner = source_setup(tmp_path)
    calls = []
    def listing(_company, limit):
        calls.append(limit)
        return [replace(filing, company_cik="unrelated-issuer")]
    adapter.list_annual_filings = listing
    for _attempt in range(2):
        with pytest.raises(RecoveryError):
            owner.list_annual_filings(company)
        owner = DurableOfficialSource(adapter, storage, "run", company)
    assert calls == [5]
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0


@pytest.mark.parametrize("code, expected_calls", [
    ("FILING_FETCH_FAILED", 3), ("FILING_STATUS_UNVERIFIED", 1),
    ("FILING_CONTENT_UNSAFE", 1), ("FILING_RULE_UNSUPPORTED", 1),
])
def test_typed_official_failures_keep_their_root_cause_and_bounded_retry(code, expected_calls, tmp_path):
    company, _filing, adapter, storage, owner = source_setup(tmp_path)
    calls = []
    def listing(_company, limit):
        calls.append(limit)
        raise MarketDataError("provider body must not enter public diagnostics", code=code)
    adapter.list_annual_filings = listing
    with pytest.raises(RecoveryError) as error:
        owner.list_annual_filings(company)
    assert error.value.code == code
    assert calls == [5] * expected_calls
    assert "provider body" not in str(error.value)
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0


def test_unknown_typed_provider_code_is_not_mislabeled_as_transient(tmp_path):
    company, _filing, adapter, _storage, owner = source_setup(tmp_path)
    calls = []
    def listing(_company, limit):
        calls.append(limit)
        raise MarketDataError("unexpected provider error", code="not-a-registered-code")
    adapter.list_annual_filings = listing
    with pytest.raises(RecoveryError) as error:
        owner.list_annual_filings(company)
    assert error.value.code.startswith("RECOVERY_INTERNAL_")
    assert calls == [5]


@pytest.mark.parametrize("code, action", [
    ("FILING_ACCESS_DENIED", "configure_official_source"),
    ("FILING_FORMAT_UNSUPPORTED", "provide_compatible_official_source"),
    ("FILING_STATUS_UNVERIFIED", "inspect_official_disclosure_status"),
])
def test_repeat_of_deterministic_failure_preserves_the_cause_and_action(tmp_path, code, action):
    company, _filing, adapter, storage, owner = source_setup(tmp_path)
    calls = []
    def unavailable(_company, limit):
        calls.append(limit)
        raise MarketDataError("private provider response", code=code)
    adapter.list_annual_filings = unavailable
    for _attempt in range(2):
        with pytest.raises(RecoveryError) as raised:
            owner.list_annual_filings(company, limit=6)
        assert raised.value.code == code
        assert raised.value.needs_action == action
        owner = DurableOfficialSource(adapter, storage, "run", company)
    assert calls == [6]
