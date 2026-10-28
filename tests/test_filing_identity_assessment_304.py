"""3.0.4 filing identity and local-cache safety contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from openthesis.domain import FilingDocument
from openthesis.financial_sources import downloads


_PDF_BYTES = b"official filing bytes\n"
_PDF_HASH = hashlib.sha256(_PDF_BYTES).hexdigest()


def _filing(tmp_path: Path, **changes: object) -> FilingDocument:
    values: dict[str, object] = {
        "document_id": "doc-2025",
        "company_cik": "C0001234567",
        "accession_number": "000123-25-000001",
        "form_type": "10-K",
        "fiscal_period": "FY",
        "period_end": "2025-12-31",
        "filed_at": "2026-03-01",
        "primary_document": "2025 Annual Report",
        "source_url": "https://EXAMPLE.com:443/reports/Annual.PDF?edition=Final",
        "local_path": str(tmp_path / "filing.pdf"),
        "content_hash": _PDF_HASH,
        "revision": "original",
    }
    values.update(changes)
    return FilingDocument(**values)


def test_frozen_xiaomi_provisional_discovery_keeps_verified_original_identity(tmp_path: Path):
    fixture_path = Path("tmp/qa-3.0.3-independent-20261014/xiaomi-cache-identity-repro.json")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    common = {"filed_at": "2026-04-28", "primary_document": "2025 ANNUAL REPORT"}
    discovered = FilingDocument(**common, **fixture["discovered_identity"])
    stored = FilingDocument(
        **common,
        **fixture["cached_identity"],
        local_path=str(tmp_path / "xiaomi.pdf"),
        content_hash=fixture["cache_sha256"],
    )

    assessment = downloads.assess_filing_identity(discovered, stored)

    assert assessment.same_original is True
    assert assessment.metadata_compatible is True
    assert assessment.actual_revision == "original"
    assert assessment.resolved_period_end == "2025-12-31"
    assert assessment.period_state == "source_confirmed"


def test_same_original_allows_only_url_host_scheme_default_port_normalization(tmp_path: Path):
    discovered = _filing(tmp_path)
    stored = replace(
        discovered,
        source_url="https://example.com/reports/Annual.PDF?edition=Final",
    )

    assessment = downloads.assess_filing_identity(discovered, stored)

    assert assessment.same_original is True
    assert assessment.metadata_compatible is True
    assert assessment.period_state == "source_confirmed"
    assert assessment.resolved_period_end == "2025-12-31"
    assert assessment.actual_revision == "original"
    assert assessment.diagnostic_code == ""
    assert downloads.filing_identity_matches(discovered, stored) is True


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("company_cik", "C0009999999", "FILING_IDENTITY_MISMATCH"),
        ("document_id", "doc-other", "FILING_IDENTITY_MISMATCH"),
        ("accession_number", "000123-25-000002", "FILING_IDENTITY_MISMATCH"),
        ("revision", "corrected", "FILING_IDENTITY_MISMATCH"),
        ("content_hash", "a" * 64, "FILING_CONTENT_HASH_MISMATCH"),
    ],
)
def test_different_identity_or_bytes_cannot_reuse_cache(
    tmp_path: Path, field: str, value: str, diagnostic: str
):
    discovered = _filing(tmp_path)
    stored = replace(discovered, **{field: value})

    assessment = downloads.assess_filing_identity(discovered, stored)

    assert assessment.same_original is False
    assert downloads.filing_identity_matches(discovered, stored) is False
    assert assessment.diagnostic_code == diagnostic


@pytest.mark.parametrize(
    "stored_url",
    [
        "https://example.com/reports/annual.pdf?edition=Final",
        "https://example.com/reports/Annual.PDF/?edition=Final",
        "https://example.com/reports/Annual.PDF?Edition=Final",
    ],
)
def test_path_query_and_trailing_slash_are_not_case_or_semantically_normalized(
    tmp_path: Path, stored_url: str
):
    assessment = downloads.assess_filing_identity(
        _filing(tmp_path), _filing(tmp_path, source_url=stored_url)
    )
    assert assessment.same_original is False
    assert assessment.diagnostic_code == "FILING_IDENTITY_MISMATCH"


def test_superseded_document_identifier_retains_source_case(tmp_path: Path):
    discovered = _filing(tmp_path, supersedes_document_id="Report-A")
    stored = replace(discovered, supersedes_document_id="report-a")
    assert not downloads.assess_filing_identity(discovered, stored).same_original


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/report.pdf",
        "https://user:password@example.com/report.pdf",
        "https:///report.pdf",
    ],
)
def test_non_official_or_userinfo_url_is_rejected(tmp_path: Path, url: str):
    assessment = downloads.assess_filing_identity(
        _filing(tmp_path, source_url=url), _filing(tmp_path)
    )
    assert assessment.same_original is False
    assert assessment.diagnostic_code == "FILING_SOURCE_URL_INVALID"


def test_period_metadata_conflict_does_not_mix_bytes_with_metadata(tmp_path: Path):
    discovered = _filing(tmp_path, period_end="2025-12-31")
    stored = _filing(tmp_path, period_end="2024-12-31")

    assessment = downloads.assess_filing_identity(discovered, stored)

    assert assessment.same_original is True
    assert assessment.metadata_compatible is False
    assert assessment.period_state == "conflict"
    assert assessment.resolved_period_end == ""
    assert assessment.diagnostic_code == "FILING_PERIOD_METADATA_CONFLICT"
    assert downloads.filing_identity_matches(discovered, stored) is False


@pytest.mark.parametrize(
    ("discovered_end", "stored_end", "expected_end", "expected_state"),
    [
        ("", "2025-12-31", "2025-12-31", "source_confirmed"),
        ("2025-12-31", "", "2025-12-31", "discovered"),
        ("", "", "", "unresolved"),
    ],
)
def test_period_end_can_be_filled_only_from_an_explicit_source(
    tmp_path: Path,
    discovered_end: str,
    stored_end: str,
    expected_end: str,
    expected_state: str,
):
    assessment = downloads.assess_filing_identity(
        _filing(tmp_path, period_end=discovered_end),
        _filing(tmp_path, period_end=stored_end),
    )

    assert assessment.same_original is True
    assert assessment.metadata_compatible is True
    assert assessment.period_state == expected_state
    assert assessment.resolved_period_end == expected_end


@pytest.mark.parametrize("legacy_revision", ["period_end_provisional", "period_end_verified"])
def test_legacy_period_revision_is_metadata_state_not_a_new_bytes_revision(
    tmp_path: Path, legacy_revision: str
):
    discovered = _filing(tmp_path, period_end="2025-12-31")
    stored = _filing(tmp_path, revision=legacy_revision, period_end="2025-12-31")

    assessment = downloads.assess_filing_identity(discovered, stored)

    assert assessment.same_original is True
    assert assessment.metadata_compatible is True
    assert assessment.actual_revision == "original"
    assert assessment.resolved_period_end == "2025-12-31"
    assert assessment.period_state == (
        "source_confirmed" if legacy_revision == "period_end_verified" else "discovered"
    )


def test_local_cache_diagnostic_reports_missing_hash_and_hash_mismatch(tmp_path: Path):
    path = tmp_path / "filing.pdf"
    path.write_bytes(_PDF_BYTES)

    assert downloads.local_cache_diagnostic(_filing(tmp_path, content_hash="")) == (
        "FILING_CONTENT_HASH_MISSING"
    )
    assert downloads.local_cache_diagnostic(
        _filing(tmp_path, content_hash="b" * 64)
    ) == "FILING_CONTENT_HASH_MISMATCH"


def test_local_cache_diagnostic_reports_missing_file_and_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    missing = _filing(tmp_path, local_path=str(tmp_path / "missing.pdf"))
    assert downloads.local_cache_diagnostic(missing) == "FILING_LOCAL_FILE_MISSING"

    path = tmp_path / "filing.pdf"
    path.write_bytes(_PDF_BYTES)
    original_open = Path.open

    def denied_open(self: Path, *args: object, **kwargs: object):
        if self == path:
            raise OSError("offline test I/O")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    assert downloads.local_cache_diagnostic(_filing(tmp_path)) == (
        "OFFICIAL_LOCAL_IO_UNAVAILABLE"
    )


def test_filing_cache_valid_is_a_single_diagnostic_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[FilingDocument] = []

    def diagnostic(filing: FilingDocument) -> str:
        calls.append(filing)
        return "FILING_CONTENT_HASH_MISSING"

    monkeypatch.setattr(downloads, "local_cache_diagnostic", diagnostic)
    filing = _filing(tmp_path)
    assert downloads.filing_cache_valid(filing) is False
    assert calls == [filing]
