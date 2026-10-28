from dataclasses import replace
import json

from openthesis.financial_sources.document_units import discover_document_units
from openthesis.financial_sources.note_disclosures import (
    extract_note_candidates, note_row_disclosures, _ref_id,
)
from test_explicit_financial_disclosures import candidate
from test_numeric_continuations_304 import _rows
from test_scoped_note_disclosures import PAGES, check


TEXT = ("冰轮环境2024年年度报告全文\n58、现金流量表补充资料\n"
        "项目 本期金额 上期金额\n净利润 667,448,757.2 674,392,657.6\n3 6\n"
        "加：资产减值准备 13,472,421.38 33,659,190.10")


def test_wrapped_last_digits_never_become_an_apparently_complete_shorter_amount():
    assert not note_row_disclosures(TEXT, 2024)


def wrapped():
    company, filing, _, _ = candidate()
    text = TEXT.replace("2024", "2025")
    pages = [*PAGES[:2], text, PAGES[3]]
    return extract_note_candidates(company, filing, 3, text, (0., 0., 600., 800.),
        discover_document_units(pages).note_for_page(3), positioned_rows=_rows(label="净利润"))


def test_original_positioned_fragments_recover_exact_amount_with_replayable_proof():
    facts, refs = wrapped()
    assert len(facts) == 1
    fact = facts[0]
    assert fact.concept == "net_income" and fact.value == 667448757.23
    assert "667,448,757.2" in fact.raw_text and "\n3 6" in fact.raw_text
    assert check(fact, refs).validation.accepted


def test_missing_layout_proof_cannot_admit_wrapped_prefix_or_completed_value():
    facts, refs = wrapped()
    fact = facts[0]
    assert not check(fact, [ref for ref in refs if not ref.evidence_id.startswith("note-layout:")]).validation.accepted


def test_resigned_layout_tokens_cannot_disagree_with_original_context_quote():
    facts, refs = wrapped()
    fact = facts[0]
    layout = next(ref for ref in refs if ref.evidence_id.startswith("note-layout:"))
    payload = json.loads(layout.excerpt)
    payload["rows"][1]["cells"][0]["text"] = "9"
    payload["rows"][1]["text"] = "9 6"
    excerpt = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    changed = replace(layout, excerpt=excerpt, evidence_id=_ref_id("layout", layout.document_id, 3, excerpt))
    altered = replace(fact, classification_evidence_ids=tuple(
        changed.evidence_id if key == layout.evidence_id else key for key in fact.classification_evidence_ids))
    assert not check(altered, [changed if ref == layout else ref for ref in refs]).validation.accepted
