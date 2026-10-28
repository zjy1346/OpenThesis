from concurrent.futures import wait as real_wait
from hashlib import sha256
from unittest.mock import patch

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FilingDocument, FinancialFact, EvidenceRef
import openthesis.financial_ingestion as ingestion


def test_completed_checkpoint_result_is_harvested_before_batch_deadline(tmp_path):
    source = tmp_path / "original.pdf"
    source.write_bytes(b"original byte identity for scheduler-only seam")
    filing = FilingDocument("doc:completed", DEMO_COMPANY.cik, "annual-2025", "10-K", "FY",
                            "2025-12-31", "2026-02-01", "annual.pdf", "https://example.org/annual.pdf",
                            local_path=str(source), content_hash=sha256(source.read_bytes()).hexdigest())
    manifest = ingestion._manifest_for(filing)
    engine = ingestion.FinancialIngestionEngine(checkpoint_dir=tmp_path / "checkpoints")
    # The native/parser seam is not under test: the real scheduler receives a
    # completed durable result exactly as its deadline expires.
    facts = [FinancialFact(**demo_facts()[0])]
    refs = [EvidenceRef("fact:completed", filing.document_id, filing.source_url,
                        "Official financial row", "page:1", "Original amount", filing.filed_at)]
    engine.parse_local_pdf_resumable = lambda *args, **kwargs: (facts, refs, ())
    expired = False
    real_clock = ingestion.time.monotonic
    def clock():
        return real_clock() + (1000 if expired else 0)
    def finished_at_deadline(*args, **kwargs):
        nonlocal expired
        result = real_wait(*args, **kwargs)
        if result[0]:
            expired = True
        return result
    with patch.object(ingestion.time, "monotonic", clock), patch.object(ingestion, "wait", finished_at_deadline):
        result = ingestion._parse_local_pdfs_bounded(engine, DEMO_COMPANY, [filing],
            {filing.document_id: manifest})
    assert result[filing.document_id] == (facts, refs, None)
