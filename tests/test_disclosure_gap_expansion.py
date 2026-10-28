import hashlib

from openthesis.domain import FilingDocument
from openthesis.financial_sources.disclosures import cached_filing_evidence, qualitative_cache_key
from openthesis.research_coverage import OBLIGATIONS, obligation_production_plan
from openthesis.research_materials import usable_disclosure


class Cache:
    def __init__(self): self.values = {}
    def get_qualitative_evidence_cache(self, key): return self.values.get(key)
    def save_qualitative_evidence_cache(self, key, value): self.values[key] = value


def filing(number):
    return FilingDocument(f"doc:{number}", "CN_TEST", f"acc:{number}", "ANNUAL_REPORT", "FY",
        f"{2026 - number}-12-31", f"{2027 - number}-03-01", "report.pdf",
        f"https://example.invalid/{number}.pdf", content_hash="a" * 64)


def test_same_bytes_do_not_share_another_issuer_or_document_cache():
    assert qualitative_cache_key(filing(1)) != qualitative_cache_key(filing(2))


def test_missing_topic_expands_past_first_two_and_keeps_short_complete_body(monkeypatch):
    docs = [filing(number) for number in range(1, 5)]
    inspected = []
    monkeypatch.setattr("openthesis.financial_sources.disclosures.build_filing_evidence", lambda *_args, **_kw: [])
    def index(document, **_budget):
        inspected.append(document.document_id)
        text = "主营业务：公司主要生产工业制冷设备。" if document.document_id == "doc:3" else "财务报表目录"
        return [{"document_id": document.document_id, "document_sha256": document.content_hash,
                 "page": 7, "raw_text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest()}]
    monkeypatch.setattr("openthesis.financial_sources.disclosures.build_disclosure_index", index)
    result = cached_filing_evidence(Cache(), docs, requested_topics=["business"])
    assert "doc:3" in inspected and "doc:4" not in inspected
    assert any(usable_disclosure(item) and "工业制冷设备" in item.get("excerpt", "") for item in result)


def test_index_corruption_is_a_local_gap_not_source_text(monkeypatch):
    document, cache = filing(1), Cache()
    key = qualitative_cache_key(document)
    cache.values[key] = []
    cache.values[key + ":pages"] = [{"document_id": document.document_id, "document_sha256": document.content_hash,
        "raw_text": "invented", "source_sha256": "0" * 64}]
    result = cached_filing_evidence(cache, [document], requested_topics=["business"])
    assert not any(usable_disclosure(item) for item in result)
    assert any(item.get("reason") == "disclosure_index_unavailable" for item in result)


def test_every_canonical_obligation_has_a_producer_and_gap_action():
    plan = obligation_production_plan()
    assert {item["obligation_id"] for item in plan} == {item.obligation_id for item in OBLIGATIONS}
    assert all(item["producer"] and item["gap_action"] and item["necessary_input"] for item in plan)
