"""Regression coverage for safe diagnostics from the spawned vision worker."""

from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from openthesis.domain import Company, FilingDocument
from openthesis.financial_sources import VisionCandidateExtractor
from openthesis.vision_financials import VisionFallbackConfig


def _filing(tmp_path: Path) -> FilingDocument:
    path = tmp_path / "tampered-source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    original_hash = sha256(path.read_bytes()).hexdigest()
    return FilingDocument(
        "pdf-page-source",
        "000000001",
        "source-accession",
        "ANNUAL_REPORT",
        "FY",
        "2025-12-31",
        "2026-03-01",
        "example-annual-report.pdf",
        "https://example.invalid/report.pdf",
        local_path=str(path),
        content_hash=original_hash,
    )


def _config() -> VisionFallbackConfig:
    return VisionFallbackConfig(
        enabled=True,
        consent=True,
        provider="configured_model",
        configured_model_id="configured-test-model",
        timeout_seconds=20,
        max_pages=2,
        max_bytes=2_000_000,
        require_page_approval=True,
        approve_upload=lambda _summary: True,
    )


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    def extract(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("adapter must not run when source identity fails")


def test_spawned_hash_mismatch_remains_specific_and_stops_before_adapter(tmp_path):
    filing = _filing(tmp_path)
    source_path = Path(filing.local_path)
    source_path.write_bytes(source_path.read_bytes() + b"\n% altered after identity check\n")
    adapter = RecordingAdapter()

    batch = VisionCandidateExtractor(object(), adapter, _config()).extract(
        Company("000000001", "EX", "Example Corp", reporting_currency="USD"), filing,
    )

    assert batch.diagnostics == ("VISION_SOURCE_HASH_MISMATCH",)
    assert adapter.calls == []


def test_unknown_worker_diagnostic_is_redacted_to_generic_code(tmp_path):
    filing = _filing(tmp_path)
    adapter = RecordingAdapter()

    with patch(
        "openthesis.financial_ingestion._run_pdfium_isolated",
        return_value=(None, "C:/Users/private/financial-report.pdf: access denied"),
    ):
        batch = VisionCandidateExtractor(object(), adapter, _config()).extract(
            Company("000000001", "EX", "Example Corp", reporting_currency="USD"), filing,
        )

    assert batch.diagnostics == ("VISION_PAGE_PREPARATION_UNAVAILABLE",)
    assert "private" not in " ".join(batch.diagnostics)
    assert adapter.calls == []
