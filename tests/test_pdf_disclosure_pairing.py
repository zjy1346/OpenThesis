from __future__ import annotations

from openthesis.domain import EvidenceRef, FinancialFact
from openthesis.financial_ingestion import (
    _pair_pdf_facts_evidence,
    _pdf_evidence_id_for_fact,
)


def _fact(fact_id: str, *, source_page: int | None = 3) -> FinancialFact:
    return FinancialFact(
        fact_id=fact_id,
        company_cik="CN_A:SZSE:000811.SZ",
        concept="revenue",
        reported_concept="营业收入",
        value=7051834600.0,
        unit="CNY",
        fiscal_year=2025,
        fiscal_period="FY",
        form_type="annual",
        start_date="2025-01-01",
        end_date="2025-12-31",
        filed_at="2026-03-31",
        accession_number="cninfo:1225092828",
        source_url="https://example.invalid/annual-report.pdf",
        source_page=source_page,
        source_document="annual-report.pdf",
        raw_text="营业收入：705,183.46万元",
    )


def _ref(evidence_id: str, *, page: int = 3, source_url: str | None = None) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        document_id="cninfo:1225092828",
        source_url=source_url or "https://example.invalid/annual-report.pdf",
        title="original financial disclosure",
        locator=f"page:{page}",
        excerpt="营业收入：705,183.46万元",
        published_at="2026-03-31",
        content_hash="a" * 64,
    )


def test_disclosure_and_note_candidates_pair_to_their_exact_original_page_ref() -> None:
    for prefix in ("disclosure", "note"):
        fact = _fact(f"{prefix}:abc123")
        ref = _ref(f"fact:{fact.fact_id}")

        paired, diagnostics = _pair_pdf_facts_evidence((fact,), (ref,))

        assert paired == ((fact, ref),)
        assert diagnostics == ()
        assert _pdf_evidence_id_for_fact(fact.fact_id) == ref.evidence_id


def test_ingest_candidates_keep_the_existing_canonical_relation() -> None:
    fact = _fact("ingest:abc123")
    ref = _ref("fact:abc123")

    paired, diagnostics = _pair_pdf_facts_evidence((fact,), (ref,))

    assert paired == ((fact, ref),)
    assert diagnostics == ()


def test_unknown_prefix_is_not_inferred_from_fact_metadata() -> None:
    fact = _fact("unrecognized:abc123")
    plausible_but_unrelated = _ref("fact:abc123")

    paired, diagnostics = _pair_pdf_facts_evidence((fact,), (plausible_but_unrelated,))

    assert paired == ()
    assert diagnostics == ("pdf_evidence_missing:unrecognized:abc123",)
    assert _pdf_evidence_id_for_fact(fact.fact_id) is None


def test_missing_and_nonunique_refs_are_rejected() -> None:
    fact = _fact("disclosure:abc123")
    one_ref = _ref("fact:disclosure:abc123")

    missing, missing_diagnostics = _pair_pdf_facts_evidence((fact,), ())
    conflicting, conflicting_diagnostics = _pair_pdf_facts_evidence(
        (fact,), (one_ref, _ref(one_ref.evidence_id, page=4))
    )

    assert missing == ()
    assert missing_diagnostics == ("pdf_evidence_missing:disclosure:abc123",)
    assert conflicting == ()
    assert conflicting_diagnostics == ("pdf_evidence_ambiguous:disclosure:abc123",)


def test_ref_must_locate_the_fact_on_its_original_page_and_source() -> None:
    fact = _fact("note:abc123", source_page=3)
    wrong_page = _ref("fact:note:abc123", page=4)
    wrong_source = _ref(
        "fact:note:abc123", source_url="https://example.invalid/another-report.pdf"
    )
    non_page_locator = _ref("fact:note:abc123")
    non_page_locator.locator = "metadata"

    for invalid_ref in (wrong_page, wrong_source, non_page_locator):
        paired, diagnostics = _pair_pdf_facts_evidence((fact,), (invalid_ref,))
        assert paired == ()
        assert diagnostics == ("pdf_evidence_location_mismatch:note:abc123",)
