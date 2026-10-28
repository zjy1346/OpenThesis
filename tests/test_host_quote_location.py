"""Host quote positioning: model offsets/hash are hints, not publication gates."""
import hashlib

import pytest

from openthesis.research_assertions import AssertionSession, validate_response


def query(text="公司从事工业制冷设备研发、生产与销售。"):
    session = AssertionSession()
    session.classify({"kind": "inference", "text": "主营业务包括制冷设备制造及销售。"}, [{
        "evidence_id": "filing:business", "raw_text": text, "company_cik": "CN_TEST",
        "document_id": "doc1", "document_sha256": "a" * 64, "page": 7,
    }])
    return next(iter(session.queries.values()))


@pytest.mark.parametrize("legacy", [{}, {"start": 1, "end": 5},
                                    {"source_sha256": "provider-cannot-compute-this"}])
def test_host_positions_unique_quote_without_provider_offsets_or_hash(legacy):
    item = query()
    original = item["sources"][0]["raw_text"]
    result = validate_response({"structured_output_valid": True, "results": [{
        "assertion_id": item["assertion_id"], "state": "supported",
        "source_spans": [{"evidence_id": "filing:business", "quote": original, **legacy}],
    }]}, [item], "offline-reviewer")
    span = result[item["assertion_id"]]["source_spans"][0]
    assert span["quote"] == original
    assert span["start"] == 0 and span["end"] == len(original)
    assert span["source_sha256"] == hashlib.sha256(original.encode()).hexdigest()


def test_quote_whitespace_punctuation_has_reversible_original_offsets():
    item = query("公司从事工业制冷设备研发，\n  生产与销售。")
    result = validate_response({"structured_output_valid": True, "results": [{
        "assertion_id": item["assertion_id"], "state": "supported",
        "source_spans": [{"evidence_id": "filing:business",
                          "quote": "公司从事工业制冷设备研发,生产与销售。"}],
    }]}, [item], "offline-reviewer")
    span = result[item["assertion_id"]]["source_spans"][0]
    assert span["quote"] == item["sources"][0]["raw_text"]
    assert span["end"] == len(span["quote"])


@pytest.mark.parametrize("quote", ["公司从事消费保险销售。", "利润100万元。", "The company operates no where else."])
def test_changed_words_numbers_and_negation_are_not_locatable(quote):
    item = query("公司从事工业制冷设备销售。利润10万元。The company operates nowhere else.")
    assert validate_response({"structured_output_valid": True, "results": [{
        "assertion_id": item["assertion_id"], "state": "supported",
        "source_spans": [{"evidence_id": "filing:business", "quote": quote}],
    }]}, [item], "offline-reviewer") == {}


def test_repeated_quote_needs_unambiguous_block_or_valid_original_locator():
    item = query("公司生产设备。\n公司生产设备。")
    answer = {"assertion_id": item["assertion_id"], "state": "supported",
              "source_spans": [{"evidence_id": "filing:business", "quote": "公司生产设备。"}]}
    assert validate_response({"structured_output_valid": True, "results": [answer]}, [item], "fixture") == {}
    answer["source_spans"][0].update(start=8, end=15)
    accepted = validate_response({"structured_output_valid": True, "results": [answer]}, [item], "fixture")
    assert accepted[item["assertion_id"]]["source_spans"][0]["start"] == 8
