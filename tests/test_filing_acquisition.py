"""Official byte acquisition contracts; no network or model providers."""
from dataclasses import replace
import hashlib
import threading
from pathlib import Path

from openthesis.domain import FilingDocument
from openthesis.financial_sources.acquisition import OfficialFilingAcquisition
from openthesis.market_data import MarketDataError


def filing(accession="2025", **overrides):
    return replace(FilingDocument(
        document_id=f"doc:{accession}", company_cik="issuer:1",
        accession_number=accession, form_type="ANNUAL_REPORT", fiscal_period="FY",
        period_end="2025-12-31", filed_at="2026-03-01", primary_document=f"{accession}.pdf",
        source_url=f"https://official.example/{accession}.pdf",
    ), **overrides)


def write_source(item, directory: Path, data=b"original official bytes"):
    path = directory / item.primary_document
    path.write_bytes(data)
    return replace(item, local_path=str(path), content_hash=hashlib.sha256(data).hexdigest())


class Adapter:
    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform

    def download_filing(self, item, directory):
        self.calls.append(item.accession_number)
        downloaded = write_source(item, directory)
        return self.transform(downloaded) if self.transform else downloaded


def test_verified_cache_is_reused_without_mutating_discovery_or_reaching_network(tmp_path):
    requested = filing()
    stored = write_source(requested, tmp_path)
    adapter = Adapter()
    progress = []
    result = OfficialFilingAcquisition().acquire(
        adapter, [requested], tmp_path, stored_filings=[stored],
        progress=lambda completed, total: progress.append((completed, total)),
    )
    assert adapter.calls == []
    assert result.filings == result.cached == (replace(stored, period_metadata_status="source_confirmed"),)
    assert result.downloaded == result.errors == ()
    assert progress == [(1, 1)]
    assert requested.local_path == requested.content_hash == ""
    assert result.filings[0] is not requested and result.filings[0] is not stored


def test_cached_bytes_with_changed_hash_are_downloaded_again(tmp_path):
    requested = filing()
    stored = write_source(requested, tmp_path)
    Path(stored.local_path).write_bytes(b"changed cached bytes")
    adapter = Adapter()
    result = OfficialFilingAcquisition().acquire(adapter, [requested], tmp_path, stored_filings=[stored])
    assert adapter.calls == [requested.accession_number]
    assert result.cached == () and result.errors == ()
    assert result.filings == result.downloaded
    assert Path(result.filings[0].local_path).read_bytes() == b"original official bytes"


def test_same_accession_does_not_reuse_other_issuer_document_revision_or_url(tmp_path):
    requested = filing()
    for field, changed in (
        ("company_cik", "issuer:2"), ("document_id", "another-document"),
        ("revision", "corrected"),
        ("source_url", "https://official.example/other.pdf"),
    ):
        stored = write_source(replace(requested, **{field: changed}), tmp_path)
        adapter = Adapter()
        result = OfficialFilingAcquisition().acquire(adapter, [requested], tmp_path, stored_filings=[stored])
        assert adapter.calls == [requested.accession_number], field
        assert result.cached == () and result.errors == (), field
        assert result.filings[0].company_cik == requested.company_cik


def test_period_metadata_conflict_retains_bytes_without_download_or_financial_permission(tmp_path):
    requested = filing()
    stored = write_source(replace(requested, period_end="2024-12-31"), tmp_path)
    adapter = Adapter()
    result = OfficialFilingAcquisition().acquire(adapter, [requested], tmp_path, stored_filings=[stored])
    assert adapter.calls == []
    assert result.downloaded == ()
    assert result.filings[0].local_path == stored.local_path
    assert result.filings[0].content_hash == stored.content_hash
    assert result.filings[0].period_end == stored.period_end
    assert result.filings[0].period_metadata_status == "conflict"
    assert [error.code for _, error in result.errors] == ["FILING_PERIOD_METADATA_CONFLICT"]
    assert stored.period_metadata_status == requested.period_metadata_status == ""


def test_adapter_cannot_change_expected_identity_by_mutating_request_in_place(tmp_path):
    requested = filing()

    class MutatingAdapter:
        def download_filing(self, item, directory):
            item.company_cik = "issuer:attacker"
            return write_source(item, directory)

    result = OfficialFilingAcquisition().acquire(MutatingAdapter(), [requested], tmp_path)
    assert result.filings == result.downloaded == ()
    assert [error.code for _, error in result.errors] == ["FILING_SOURCE_IDENTITY_MISMATCH"]
    assert requested.company_cik == "issuer:1" and requested.local_path == ""


def test_success_metadata_without_matching_actual_bytes_cannot_publish(tmp_path):
    for transform, code in (
        (lambda item: replace(item, content_hash="0" * 64), "FILING_CONTENT_HASH_MISMATCH"),
        (lambda item: replace(item, local_path=str(tmp_path / "absent.pdf")), "FILING_LOCAL_FILE_MISSING"),
    ):
        result = OfficialFilingAcquisition().acquire(Adapter(transform), [filing()], tmp_path)
        assert result.filings == result.downloaded == ()
        assert [error.code for _, error in result.errors] == [code]


def test_partial_download_failure_preserves_good_files_and_input_order(tmp_path):
    requested = [filing(str(index)) for index in range(3)]
    stored = write_source(requested[1], tmp_path)

    class PartialAdapter(Adapter):
        def download_filing(self, item, directory):
            if item.accession_number == "2":
                raise MarketDataError("not authorized", code="FILING_ACCESS_DENIED")
            return super().download_filing(item, directory)

    result = OfficialFilingAcquisition().acquire(
        PartialAdapter(), requested, tmp_path, stored_filings=[stored],
    )
    assert [item.accession_number for item in result.filings] == ["0", "1"]
    assert [item.accession_number for item in result.cached] == ["1"]
    assert [item.accession_number for item in result.downloaded] == ["0"]
    assert [(item.accession_number, error.code) for item, error in result.errors] == [
        ("2", "FILING_ACCESS_DENIED"),
    ]


def test_preexisting_cancel_does_not_download_or_publish_cache(tmp_path):
    requested = filing()
    adapter = Adapter()
    result = OfficialFilingAcquisition().acquire(
        adapter, [requested], tmp_path, stored_filings=[write_source(requested, tmp_path)],
        cancel_check=lambda: True,
    )
    assert result.cancelled and result.filings == result.downloaded == ()
    assert adapter.calls == []


def test_response_arriving_after_cancel_is_not_published(tmp_path):
    cancelled = threading.Event()

    class LateAdapter(Adapter):
        def download_filing(self, item, directory):
            downloaded = super().download_filing(item, directory)
            cancelled.set()
            return downloaded

    result = OfficialFilingAcquisition().acquire(
        LateAdapter(), [filing()], tmp_path, cancel_check=cancelled.is_set,
    )
    assert result.cancelled and result.filings == result.downloaded == ()


def test_unresolvable_source_does_not_silently_disappear_from_cohort(tmp_path):
    requested = filing(source_url="")
    adapter = Adapter()
    result = OfficialFilingAcquisition().acquire(adapter, [requested], tmp_path)
    assert adapter.calls == []
    assert result.filings == ()
    assert [(item.accession_number, error.code) for item, error in result.errors] == [
        (requested.accession_number, "FILING_DOWNLOAD_INCOMPLETE"),
    ]
