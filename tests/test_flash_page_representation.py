from __future__ import annotations

from concurrent.futures import CancelledError
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from openthesis.domain import Company, FilingDocument
from openthesis.financial_contracts import CandidateBatch
from openthesis.financial_ingestion import _vision_failed_pages
from openthesis.financial_sources import VisionCandidateExtractor
from openthesis.vision_financials import (
    VisionAdapterError,
    VisionExtractionResult,
    VisionFallbackConfig,
    _render_pdfium_page,
    default_pdf_to_image_pdf,
)


def _company() -> Company:
    return Company("000000001", "EX", "Example Corp", reporting_currency="USD")


def _write_source_pdf(
    path, page_texts=("Consolidated Income Statement",), *, width=80, height=60,
) -> FilingDocument:
    writer = PdfWriter()
    font_ref = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for page_text in page_texts:
        page = writer.add_blank_page(width=width, height=height)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
        })
        content = DecodedStreamObject()
        lines = page_text.splitlines() or [page_text]
        if len(lines) == 1:
            safe_text = lines[0].replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            operations = [f"BT /F1 9 Tf 4 35 Td ({safe_text}) Tj ET"]
        else:
            operations = []
            for line_index, line in enumerate(lines):
                safe_text = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                y = max(1, height - 15 - line_index * 12)
                operations.append(f"BT /F1 9 Tf 4 {y} Td ({safe_text}) Tj ET")
        content.set_data("\n".join(operations).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)
    raw = path.read_bytes()
    filing = FilingDocument(
        "pdf-page-source", "000000001", "source-accession", "ANNUAL_REPORT",
        "FY", "2025-12-31", "2026-03-01", "example-annual-report.pdf",
        "https://example.invalid/report.pdf", local_path=str(path),
        content_hash=sha256(raw).hexdigest(),
    )
    return filing


def _config(provider: str) -> VisionFallbackConfig:
    return VisionFallbackConfig(
        enabled=True,
        consent=True,
        provider=provider,
        configured_model_id="configured-test-model" if provider == "configured_model" else "",
        max_pages=2,
        max_bytes=2_000_000,
        require_page_approval=True,
        approve_upload=lambda _summary: True,
    )


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    def extract(self, subject, filing, pages, config, *, cancel_check=None):
        self.calls.append((subject, filing, pages, config))
        return VisionExtractionResult(diagnostics=("VISION_NO_CANDIDATES",),
                                      error_code="VISION_NO_CANDIDATES")


def _image_xobject(pdf: bytes):
    reader = PdfReader(BytesIO(pdf))
    page = reader.pages[0]
    xobjects = page["/Resources"].get_object()["/XObject"].get_object()
    return reader, page, xobjects["/Im0"].get_object()


def test_default_flash_representation_renders_and_wraps_one_real_page_deterministically(tmp_path):
    path = tmp_path / "one-page.pdf"
    _write_source_pdf(path)
    source_pdf = path.read_bytes()

    first = default_pdf_to_image_pdf(source_pdf)
    second = default_pdf_to_image_pdf(source_pdf)

    reader, page, image = _image_xobject(first)
    assert len(reader.pages) == 1
    assert page.extract_text() == ""
    assert (image["/Width"], image["/Height"]) == (120, 90)
    assert image["/ColorSpace"] == "/DeviceRGB"
    assert image["/BitsPerComponent"] == 8
    assert image["/Filter"] == "/FlateDecode"
    assert len(image.get_data()) == 120 * 90 * 3
    assert b"/Im0 Do" in page.get_contents().get_data()
    assert first == second


def test_failed_page_selection_preserves_source_page_and_only_flash_uses_image_wrapper(tmp_path):
    path = tmp_path / "source.pdf"
    unit_sentence = (
        "These financial statements are expressed in thousands of Hong Kong dollars, "
        "except where indicated."
    )
    filing = _write_source_pdf(
        path,
        (
            "Contents Page\n"
            "Consolidated Income Statement 7\n"
            "Consolidated Balance Sheet 8\n"
            "Consolidated Statement of Cash Flows 9\n"
            "Auditor Report 12",
            "Consolidated Income Statement\nRevenue line",
            "Consolidated Balance Sheet",
            "Consolidated Statement of Cash Flows",
            unit_sentence,
            "Appendix",
            "Auditor Report\n12",
        ),
        width=80,
        height=120,
    )
    subject = _company()
    validation_issues = {filing.document_id: ("income_statement_core_missing",)}

    flash_adapter = RecordingAdapter()
    flash_batch = VisionCandidateExtractor(
        object(), flash_adapter, _config("mineru_flash"),
        validation_issues_by_document=validation_issues,
    ).extract(subject, filing)
    assert isinstance(flash_batch, CandidateBatch)
    flash_pages = flash_adapter.calls[0][2]
    assert [page.original_page for page in flash_pages] == [2]
    assert [item.original_page for item in flash_pages[0].unit_contexts] == [5]
    for page_request in flash_pages:
        flash_page = PdfReader(BytesIO(page_request.pdf_bytes)).pages[0]
        assert flash_page.extract_text() == ""
        flash_resources = flash_page["/Resources"].get_object()
        assert "/Im0" in flash_resources["/XObject"].get_object()

    configured_adapter = RecordingAdapter()
    configured_batch = VisionCandidateExtractor(
        object(), configured_adapter, _config("configured_model"),
        validation_issues_by_document=validation_issues,
    ).extract(subject, filing)
    assert isinstance(configured_batch, CandidateBatch)
    native_pages = configured_adapter.calls[0][2]
    assert [page.original_page for page in native_pages] == [2]
    assert [item.original_page for item in native_pages[0].unit_contexts] == [5]
    native_text = PdfReader(
        BytesIO(native_pages[0].pdf_bytes),
    ).pages[0].extract_text()
    assert "Consolidated Income Statement" in native_text
    assert "Revenue line" in native_text
    assert unit_sentence not in native_text


def test_source_hash_mismatch_stops_before_converter_or_adapter(tmp_path):
    path = tmp_path / "tampered-source.pdf"
    filing = _write_source_pdf(path)
    filing.content_hash = "0" * 64
    adapter = RecordingAdapter()

    with patch("openthesis.financial_ingestion._toc_statement_page_map",
               return_value={"income_statement": (1,)}), \
            patch("openthesis.vision_financials.default_pdf_to_image_pdf") as convert:
        batch = VisionCandidateExtractor(object(), adapter, _config("mineru_flash")).extract(
            _company(), filing,
        )

    assert batch.diagnostics == ("VISION_SOURCE_HASH_MISMATCH",)
    convert.assert_not_called()
    assert adapter.calls == []


def test_failed_page_selection_reuses_the_single_hash_verified_source_bytes(tmp_path):
    source_path = tmp_path / "frozen-source.pdf"
    unit_sentence = (
        "These financial statements are expressed in thousands of Hong Kong dollars, "
        "except where indicated."
    )
    filing = _write_source_pdf(
        source_path,
        ("Consolidated Income Statement", unit_sentence, "Original appendix."),
    )
    replacement_path = tmp_path / "replacement-source.pdf"
    _write_source_pdf(
        replacement_path,
        (
            "Consolidated Balance Sheet",
            "These financial statements are presented in US dollars. "
            "All amounts are rounded to the nearest million unless otherwise stated.",
            "Replacement appendix.",
        ),
    )
    replacement_bytes = replacement_path.read_bytes()
    real_read_bytes = Path.read_bytes
    target_reads = 0

    def read_bytes_spy(path):
        nonlocal target_reads
        path = Path(path)
        if path.resolve() == source_path.resolve():
            target_reads += 1
            data = real_read_bytes(path)
            if target_reads == 1:
                # A later path reopen would now see a different PDF than the one
                # whose digest was accepted. All processing must use the first
                # immutable byte snapshot instead.
                path.write_bytes(replacement_bytes)
            return data
        return real_read_bytes(path)

    with patch.object(Path, "read_bytes", new=read_bytes_spy), \
            patch("openthesis.financial_ingestion._toc_statement_page_map",
                  return_value={"income_statement": (1,)}):
        pages = _vision_failed_pages(filing, _config("configured_model"))

    assert target_reads == 1
    assert len(pages) == 1
    request = pages[0]
    assert request.original_page == 1
    exported_page = PdfReader(BytesIO(request.pdf_bytes)).pages[0]
    assert "Consolidated Income Statement" in exported_page.extract_text()
    assert "Consolidated Balance Sheet" not in exported_page.extract_text()
    assert len(request.unit_contexts) == 1
    assert request.unit_contexts[0].original_page == 2
    assert unit_sentence in request.unit_contexts[0].excerpt
    assert request.unit_contexts[0].document_hash == filing.content_hash


def test_size_limit_remains_candidate_diagnostic_in_real_page_worker(tmp_path):
    path = tmp_path / "VISION_IMAGE_SIZE_LIMIT.pdf"
    filing = _write_source_pdf(path, width=3_000, height=3_000)
    adapter = RecordingAdapter()

    batch = VisionCandidateExtractor(object(), adapter, _config("mineru_flash")).extract(
        _company(), filing,
    )

    assert batch.diagnostics == ("VISION_IMAGE_SIZE_LIMIT",)
    assert "vision_no_failed_pages" not in batch.diagnostics
    assert adapter.calls == []


def test_invalid_pdf_render_failure_is_mapped_by_real_pdfium_worker():
    with pytest.raises(VisionAdapterError) as raised:
        default_pdf_to_image_pdf(b"not a PDF document")

    assert raised.value.code == "VISION_IMAGE_RENDER_FAILED"


def test_cancellation_during_page_preparation_raises_cancelled_error(tmp_path):
    path = tmp_path / "cancelled.pdf"
    filing = _write_source_pdf(path)
    adapter = RecordingAdapter()
    checks = iter((False, True))

    with patch("openthesis.financial_ingestion._toc_statement_page_map",
               return_value={"income_statement": (1,)}), \
            patch("openthesis.vision_financials.default_pdf_to_image_pdf") as convert:
        extractor = VisionCandidateExtractor(
            object(), adapter, _config("mineru_flash"), cancel_check=lambda: next(checks),
        )
        with pytest.raises(CancelledError):
            extractor.extract(_company(), filing)

    convert.assert_not_called()
    assert adapter.calls == []


def test_excessive_page_surface_is_rejected_before_render_allocation(monkeypatch):
    class Page:
        render_called = False

        def get_width(self):
            return 3000.0

        def get_height(self):
            return 40.0

        def render(self, **_kwargs):
            self.render_called = True
            raise AssertionError("oversized page must be rejected before bitmap allocation")

    page = Page()

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return page

        def close(self):
            return None

    fake_pdfium = SimpleNamespace(PdfDocument=lambda _pdf: Document())
    monkeypatch.setitem(sys.modules, "pypdfium2", fake_pdfium)

    with pytest.raises(VisionAdapterError) as raised:
        _render_pdfium_page(b"synthetic native input")

    assert raised.value.code == "VISION_IMAGE_SIZE_LIMIT"
    assert page.render_called is False
