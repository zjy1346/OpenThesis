"""Period evidence cannot overwrite the edition of captured source bytes."""
from dataclasses import replace
import hashlib

import pytest

from openthesis.financial_ingestion import (
    FinancialIngestionEngine, _manifest_for, _manifest_from_observed_pages,
)
from openthesis.financial_sources.acquisition import OfficialFilingAcquisition
from openthesis.financial_sources.pdf import FinancialPdfSource
from tests.test_inline_xbrl_source import source
from tests.test_statement_period_binding import filing


@pytest.mark.parametrize("edition", ["original", "restated", "corrected"])
def test_observed_statement_date_preserves_the_actual_source_edition(edition):
    original = replace(filing(), revision=edition)
    provisional = _manifest_for(original)
    assert provisional.period_end == ""
    assert provisional.original_revision == edition
    resolved = _manifest_from_observed_pages(provisional, original, [
        "Consolidated Income Statements\nYear ended March 31, 2026\nRevenue 12",
    ])
    assert resolved.period_end == "2026-03-31"
    assert resolved.revision == edition


@pytest.mark.parametrize("edition", ["original", "restated"])
def test_inline_collection_preserves_edition_and_reuses_the_exact_original(tmp_path, edition):
    company, original, raw = source()
    raw = raw.replace(b"2025-01-27", b"2025-01-01").replace(b"2026-01-25", b"2025-12-31")
    raw = raw.replace(b">2026<", b">2025<")
    path = tmp_path / "original.htm"
    path.write_bytes(raw)
    original = replace(original, revision=edition, period_end="2025-12-31",
                       local_path=str(path), content_hash=hashlib.sha256(raw).hexdigest())
    collected = replace(original)
    result = FinancialPdfSource(FinancialIngestionEngine()).collect_candidate_batches(company, [collected])
    assert collected.period_end == "2025-12-31"
    assert collected.revision == edition
    assert result.manifests[0].revision == edition
    candidates = [candidate for batch in result.batches_by_document[collected.document_id] for candidate in batch.candidates]
    assert candidates and all(candidate.fact.revision == edition for candidate in candidates)
    discovered = replace(original, local_path="", content_hash="")
    reused = OfficialFilingAcquisition.verified_cache(discovered, collected)
    assert reused is not None and reused.content_hash == original.content_hash
    assert OfficialFilingAcquisition.verified_cache(replace(discovered, revision="a-new-edition"), collected) is None
    assert path.read_bytes() == raw


def test_nonprovisional_metadata_does_not_replace_a_corrected_edition():
    original = replace(filing(), period_end="2026-03-31", revision="corrected")
    assert _manifest_for(original).revision == "corrected"


@pytest.mark.parametrize("field,value", [
    ("company_cik", "other-issuer"), ("document_id", "other-document"),
    ("accession_number", "other-accession"), ("source_url", "https://www.sec.gov/Archives/other.htm"),
    ("primary_document", "other.htm"), ("filed_at", "2026-03-15"),
    ("revision", "corrected"), ("supersedes_document_id", "earlier-source"),
])
def test_parse_cache_is_bound_to_source_identity_not_just_identical_bytes(field, value):
    company, original, _raw = source()
    changed = replace(original, **{field: value})
    engine = FinancialIngestionEngine()
    key = engine._parse_cache_key("unused", company, original, original.content_hash)
    assert engine._parse_cache_key("unused", company, changed, original.content_hash) != key


def test_moving_identical_source_bytes_does_not_invalidate_parse_identity():
    company, original, _raw = source()
    engine = FinancialIngestionEngine()
    assert engine._parse_cache_key("first.htm", company, original, original.content_hash) == engine._parse_cache_key(
        "moved.htm", company, replace(original, local_path="moved.htm"), original.content_hash,
    )
