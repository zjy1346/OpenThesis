import time
from concurrent.futures import CancelledError

import pytest

from openthesis.domain import FilingDocument
from openthesis.financial_sources.disclosures import cached_filing_evidence, qualitative_cache_key
from openthesis.financial_sources.pdf_document_units import PdfDocumentContext, read_document_pages


class Store:
    def __init__(self):
        self.values = {}
    def get_qualitative_evidence_cache(self, key):
        return self.values.get(key)
    def save_qualitative_evidence_cache(self, key, value):
        self.values[key] = value


def filing(path):
    return FilingDocument("doc", "issuer", "acc", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-01",
        "report.pdf", "https://example.test/report.pdf", local_path=str(path))


def test_expired_hash_budget_never_opens_file(tmp_path, monkeypatch):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"not parsed")
    monkeypatch.setattr("pathlib.Path.open", lambda *_a, **_kw: pytest.fail("expired budget opened PDF"))
    with pytest.raises(TimeoutError):
        qualitative_cache_key(filing(path), deadline=time.monotonic() - 1)


def test_cancelled_index_is_not_cached_or_material_gap(tmp_path):
    store = Store()
    with pytest.raises(CancelledError):
        cached_filing_evidence(store, [filing(tmp_path / "report.pdf")], cancel_check=lambda: True)
    assert not store.values


def test_budget_exhaustion_is_resumable_not_empty_source_success(tmp_path):
    store = Store()
    result = cached_filing_evidence(store, [filing(tmp_path / "report.pdf")], deadline=time.monotonic() - 1)
    assert any(item.get("reason") == "disclosure_index_budget_exhausted_resumable" for item in result)
    assert not store.values


def test_native_failure_does_not_launch_unbounded_second_parser(monkeypatch):
    monkeypatch.setattr("openthesis.financial_sources.pdf_document_units.read_document_context",
                        lambda *_a, **_kw: PdfDocumentContext())
    with pytest.raises(ValueError, match="DISCLOSURE_NATIVE_TEXT_UNAVAILABLE"):
        read_document_pages("original.pdf")

