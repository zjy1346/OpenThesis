"""Official collection cannot spend the entire cohort budget on a serial prepass."""
from dataclasses import replace
import os

from openthesis.disclosure_index import cached_page_texts, remember_page_texts
from openthesis.financial_ingestion import FinancialIngestionEngine
from openthesis.financial_ingestion import _manifest_for, _parse_local_pdfs_bounded
from openthesis.financial_sources.pdf import FinancialPdfSource
from openthesis.financial_sources.pdf_document_units import PdfDocumentContext
from test_statement_period_binding import filing
from openthesis.domain import Company


def test_unresolved_periods_are_not_a_serial_barrier_before_document_workers(tmp_path, monkeypatch):
    calls = []
    documents = []
    for index in range(3):
        path = tmp_path / f"source-{index}.pdf"
        path.write_bytes(b"offline-worker-fixture")
        documents.append(replace(filing(), document_id=f"doc-{index}", local_path=str(path)))

    def context(*_args, **_kwargs):
        calls.append("context-before-workers")
        return PdfDocumentContext()

    def parse(_engine, _company, filings, _manifests, **_kwargs):
        assert calls == [], "do not serially pre-read all books before scheduling even one document"
        calls.append("workers")
        return {item.document_id: ([], [], "DOCUMENT_CONTEXT_TIMEOUT") for item in filings}

    monkeypatch.setattr("openthesis.financial_sources.pdf_document_units.read_document_context", context)
    monkeypatch.setattr("openthesis.financial_ingestion._parse_local_pdfs_bounded", parse)
    result = FinancialPdfSource(FinancialIngestionEngine()).collect_candidate_batches(
        Company(documents[0].company_cik, "09988.HK", "Fixture", market="HK"), documents)
    assert calls == ["workers"]
    assert len(result.source_failures) == 3
    assert all(item.code == "DOCUMENT_CONTEXT_TIMEOUT" for item in result.source_failures)


def test_text_reuse_is_bound_to_bytes_even_when_size_and_mtime_are_preserved(tmp_path):
    path = tmp_path / "same-path.pdf"
    path.write_bytes(b"original")
    original_stat = path.stat()
    remember_page_texts(path, ("original page",))
    assert cached_page_texts(path) == ("original page",)
    path.write_bytes(b"replaced")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert path.stat().st_size == original_stat.st_size
    assert cached_page_texts(path) is None


def test_identical_bytes_cannot_share_candidates_across_distinct_disclosure_identities(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"shared-original-bytes")
    first = replace(filing(), document_id="original-a", accession_number="a", local_path=str(path), content_hash="a" * 64)
    second = replace(first, document_id="original-b", accession_number="b", revision="corrected")
    calls = []

    def parse(_path, _company, source, _manifest):
        calls.append((source.document_id, source.revision))
        return [], []

    _parse_local_pdfs_bounded(FinancialIngestionEngine(),
        Company(first.company_cik, "09988.HK", "Fixture", market="HK"), (first, second),
        {item.document_id: _manifest_for(item) for item in (first, second)}, parse=parse)
    assert sorted(calls) == [("original-a", "original"), ("original-b", "corrected")]
