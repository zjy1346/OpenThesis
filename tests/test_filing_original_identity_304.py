"""Byte identity must survive source-proven period metadata enrichment."""
from dataclasses import replace
import json
from pathlib import Path

from openthesis.domain import FilingDocument
from openthesis.financial_sources.acquisition import OfficialFilingAcquisition
from openthesis.financial_sources.downloads import filing_cache_valid, filing_identity_matches


def xiaomi_original():
    fixture = json.loads(Path("tmp/qa-3.0.3-independent-20261014/xiaomi-cache-identity-repro.json").read_text(encoding="utf-8-sig"))
    common = {"filed_at": "2026-04-28", "primary_document": "2025 ANNUAL REPORT"}
    discovered = FilingDocument(**common, **fixture["discovered_identity"])
    cached = FilingDocument(**common, **fixture["cached_identity"],
        local_path=fixture["cache_path"], content_hash=fixture["cache_sha256"])
    return discovered, cached


def test_verified_xiaomi_original_is_not_lost_when_discovery_has_provisional_period():
    discovered, cached = xiaomi_original()
    original_state = cached.to_dict()
    assert filing_cache_valid(cached), "QA's frozen official PDF must still match its SHA-256"
    reused = OfficialFilingAcquisition.verified_cache(discovered, cached)
    assert reused is not None, "metadata incompleteness cannot discard verified official bytes"
    assert reused.local_path == cached.local_path and reused.content_hash == cached.content_hash
    assert reused.period_end == "2025-12-31"
    assert cached.to_dict() == original_state
    assert discovered.local_path == "" and discovered.period_end == ""


def test_source_url_path_and_query_case_are_not_byte_identity_equivalences():
    _, cached = xiaomi_original()
    for url in (cached.source_url.replace(".pdf", ".PDF"), cached.source_url + "/"):
        assert not filing_identity_matches(replace(cached, source_url=url), cached)
    assert not filing_identity_matches(replace(cached, source_url=cached.source_url + "?edition=A"),
        replace(cached, source_url=cached.source_url + "?edition=a"))
    assert filing_identity_matches(replace(cached, source_url=cached.source_url.replace("www1.hkexnews.hk", "WWW1.HKEXNEWS.HK")), cached)


def test_real_revision_and_content_change_never_reuse_the_prior_file():
    discovered, cached = xiaomi_original()
    assert OfficialFilingAcquisition.verified_cache(replace(discovered, revision="corrected"), cached) is None
    assert OfficialFilingAcquisition.verified_cache(replace(discovered, content_hash="b" * 64), cached) is None
    assert OfficialFilingAcquisition.verified_cache(replace(discovered, company_cik="different-issuer"), cached) is None
