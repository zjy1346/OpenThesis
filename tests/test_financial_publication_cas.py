"""Concurrent writers use one transactional financial publication seam."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from dataclasses import replace

import pytest

from openthesis.storage import Storage
from test_financial_lineage_storage import _fixture, _prepare


def publish(storage, company, filing, fact, evidence, generation, expected):
    view = _prepare(company, filing, (fact,), evidence)
    return storage.replace_financial_ingestion(company.security_id, [filing.accession_number],
        list(view.facts), evidence=list(evidence), generation_id=generation,
        source_hashes={filing.accession_number: filing.content_hash},
        complete_accessions={filing.accession_number},
        expected_generations={filing.accession_number: expected})


def test_stale_financial_candidate_is_journaled_but_cannot_replace_active_generation(tmp_path):
    company, filing, fact, refs = _fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    first = publish(storage, company, filing, fact, refs, "winner", "")
    before = storage.get_financial_research_snapshot(company.security_id)
    second = publish(storage, company, filing, fact, refs, "stale", "")
    assert first["state"] == "promoted"
    assert second["state"] == "stale_generation"
    assert second["accessions"] == {filing.accession_number: "stale_generation"}
    assert storage.get_financial_research_snapshot(company.security_id) == before
    with storage.connect() as db:
        assert db.execute("SELECT state FROM financial_ingestion_candidates WHERE generation_id='stale'").fetchone()[0] == "stale_generation"
    third = publish(storage, company, filing, fact, refs, "fresh", "winner")
    assert third["state"] == "promoted"
    assert storage.get_financial_research_snapshot(company.security_id)["generations"][filing.accession_number] == "fresh"


def test_two_publications_capturing_same_generation_have_exactly_one_winner(tmp_path):
    company, filing, fact, refs = _fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    barrier = Barrier(2)
    def submit(generation):
        expected = storage.get_financial_research_snapshot(company.security_id)["generations"].get(filing.accession_number, "")
        barrier.wait(timeout=5)
        return publish(storage, company, filing, fact, refs, generation, expected)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ("first", "second")))
    assert sorted(item["state"] for item in results) == ["promoted", "stale_generation"]
    current = storage.get_financial_research_snapshot(company.security_id)
    winner = next(item["generation_id"] for item in results if item["state"] == "promoted")
    assert current["generations"] == {filing.accession_number: winner}
    assert {item["generation_id"] for item in current["facts"]} == {winner}


def test_reusing_generation_id_cannot_rewrite_immutable_candidate_or_active_facts(tmp_path):
    company, filing, fact, refs = _fixture()
    storage = Storage(tmp_path)
    storage.save_company(company)
    storage.save_filings([filing])
    publish(storage, company, filing, fact, refs, "immutable", "")
    before = storage.get_financial_research_snapshot(company.security_id)
    with storage.connect() as db:
        candidate = dict(db.execute("SELECT * FROM financial_ingestion_candidates WHERE generation_id='immutable'").fetchone())
    # Supply a fresh CAS head: CAS alone must not permit rewriting the journal.
    changed = replace(fact, raw_text="Different candidate under an existing identity")
    with pytest.raises(ValueError, match="FINANCIAL_GENERATION_ALREADY_EXISTS"):
        publish(storage, company, filing, changed, refs, "immutable", "immutable")
    assert storage.get_financial_research_snapshot(company.security_id) == before
    with storage.connect() as db:
        assert dict(db.execute("SELECT * FROM financial_ingestion_candidates WHERE generation_id='immutable'").fetchone()) == candidate
