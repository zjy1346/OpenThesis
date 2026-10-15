"""Hash-bound qualitative disclosure collection, independent of RPC transport."""
from __future__ import annotations

import hashlib
import time
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any, Sequence

from ..domain import EvidenceRef, FilingDocument
from ..filing_parser import (QUALITATIVE_PARSER_VERSION, TOPIC_PATTERNS, build_filing_evidence,
    build_disclosure_index, _topic_evidence_from_text, _looks_like_table_of_contents)
from ..research_materials import usable_disclosure


def _check_budget(deadline, cancel_check):
    if cancel_check is not None and cancel_check():
        raise CancelledError()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("DISCLOSURE_INDEX_BUDGET_EXHAUSTED")


def qualitative_cache_key(filing: FilingDocument, *, deadline=None, cancel_check=None) -> str:
    _check_budget(deadline, cancel_check)
    content_hash = str(filing.content_hash or "").strip().casefold()
    path = Path(str(filing.local_path or ""))
    if not content_hash and path.is_file():
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                _check_budget(deadline, cancel_check)
                hasher.update(chunk)
        content_hash = hasher.hexdigest()
    scope = hashlib.sha256(f"{filing.company_cik}|{filing.document_id}|{filing.source_url}".encode()).hexdigest()
    return f"{QUALITATIVE_PARSER_VERSION}:{content_hash}:{scope}" if content_hash else ""


def cached_filing_evidence(
    storage: Any, filings: Sequence[FilingDocument], *, maximum_filings: int = 2,
    retrieval_budget: int = 10, requested_topics: Sequence[str] | None = None,
    cache_only: bool = False,
    deadline: float | None = None, cancel_check=None,
) -> list[dict[str, Any]]:
    """First-window retrieval followed by bounded, local gap expansion.

    Every inspected document gets a complete, hash-bound page index. Topic
    misses trigger older official documents and uncategorized body retrieval;
    a search miss is never represented as proof of non-disclosure.
    """
    ordered = sorted(filings, key=lambda item: item.filed_at, reverse=True)
    annual = next((item for item in ordered if item.fiscal_period == "FY"), None)
    selected = ([annual] if annual else []) + [item for item in ordered if item is not annual]
    collected = []
    wanted = set(requested_topics if requested_topics is not None else TOPIC_PATTERNS)
    first_window = max(1, int(maximum_filings))
    budget = max(first_window, min(30, int(retrieval_budget)))
    indexed = []
    # One caller-owned absolute budget covers hash, cache and native indexing;
    # each document must not refresh it. No model-token budget is involved.
    deadline = time.monotonic() + 300.0 if deadline is None else deadline

    def normalize(source, filing):
        item = dict(source)
        if item.get("kind") != "material_gap":
            item.update({"kind": "filing_text", "company_cik": filing.company_cik,
                "document_sha256": filing.content_hash,
                "fiscal_year": int(filing.period_end[:4]) if str(filing.period_end)[:4].isdigit() else None,
                "fiscal_period": filing.fiscal_period, "end_date": filing.period_end,
                "form_type": filing.form_type, "accession_number": filing.accession_number})
        return item

    def missing_topics():
        return wanted - {str(item.get("topic") or str(item.get("title", "")).split(" · ")[-1])
                         for item in collected if usable_disclosure(item)}

    for position, filing in enumerate(selected[:budget]):
        if position >= first_window and not missing_topics():
            break
        try:
            key = qualitative_cache_key(filing, deadline=deadline, cancel_check=cancel_check)
        except TimeoutError:
            collected.append({"evidence_id": f"material-gap:budget:{filing.document_id}",
                "kind": "material_gap", "document_id": filing.document_id,
                "reason": "disclosure_index_budget_exhausted_resumable",
                "next_action": "resume_official_disclosure_index"})
            break
        cached = storage.get_qualitative_evidence_cache(key) if key else None
        cache_missing = cached is None
        if cached is None:
            try:
                cached = [] if cache_only else [dict(item) for item in build_filing_evidence(
                    (filing,), maximum_filings=1, deadline=deadline, cancel_check=cancel_check)]
            except TimeoutError:
                collected.append({"evidence_id": f"material-gap:budget:{filing.document_id}",
                    "kind": "material_gap", "document_id": filing.document_id,
                    "reason": "disclosure_index_budget_exhausted_resumable",
                    "next_action": "resume_official_disclosure_index"})
                break
        normalized = [normalize(source, filing) for source in cached]
        if key and cache_missing and not cache_only:
            _check_budget(deadline, cancel_check)
            storage.save_qualitative_evidence_cache(key, normalized)
        collected.extend(normalized if position < first_window else [item for item in normalized
            if str(item.get("topic") or str(item.get("title", "")).split(" · ")[-1]) in missing_topics()
            or item.get("kind") == "material_gap"])
        try:
            index = storage.get_qualitative_evidence_cache(key + ":pages") if key else None
            if index is None:
                if cache_only:
                    raise ValueError("DISCLOSURE_INDEX_NOT_CACHED")
                index = build_disclosure_index(filing, deadline=deadline, cancel_check=cancel_check)
                if key:
                    _check_budget(deadline, cancel_check)
                    storage.save_qualitative_evidence_cache(key + ":pages", index)
            if not isinstance(index, list) or any(not isinstance(page, dict) or
                page.get("document_id") != filing.document_id or
                page.get("document_sha256") != filing.content_hash or
                page.get("source_sha256") != hashlib.sha256(str(page.get("raw_text") or "").encode("utf-8")).hexdigest()
                for page in index):
                raise ValueError("DISCLOSURE_INDEX_INTEGRITY_INVALID")
            indexed.append((filing, index))
            for page in index:
                _check_budget(deadline, cancel_check)
                text = str(page.get("raw_text") or "")
                if not text.strip() or _looks_like_table_of_contents(text):
                    continue
                refs = _topic_evidence_from_text(filing, text, page.get("page"), 3, 1700)
                collected.extend(normalize(ref.to_dict(), filing) for ref in refs
                                 if ref.title.split(" · ")[-1] in missing_topics())
        except TimeoutError:
            collected.append({"evidence_id": f"material-gap:budget:{filing.document_id}", "kind": "material_gap",
                "document_id": filing.document_id, "reason": "disclosure_index_budget_exhausted_resumable",
                "next_action": "resume_official_disclosure_index"})
            break
        except (ValueError, TypeError, OSError):
            collected.append({"evidence_id": f"material-gap:index:{filing.document_id}", "kind": "material_gap",
                "document_id": filing.document_id, "reason": "disclosure_index_unavailable"})
    # No classification hit does not mean no company disclosure. Supply
    # complete unclassified source bodies for the semantic question lane.
    if missing_topics() and not any(usable_disclosure(item) for item in collected):
        for filing, index in indexed:
            for page in index:
                text = str(page.get("raw_text") or "").strip()
                identity = hashlib.sha256(f"{filing.document_id}|{page.get('page')}|{text}".encode()).hexdigest()[:20]
                item = normalize({"evidence_id": f"filing:{identity}", "document_id": filing.document_id,
                    "source_url": filing.source_url, "title": f"{filing.form_type} {filing.period_end} · unclassified",
                    "excerpt": text, "locator": page.get("locator"), "content_hash": filing.content_hash,
                    "retrieval_state": "unclassified_source_body"}, filing)
                if usable_disclosure(item) and not _looks_like_table_of_contents(text):
                    collected.append(item)
    gaps = sorted(missing_topics())
    if gaps:
        collected.append({"evidence_id": "material-gap:retrieval:" + hashlib.sha256(
            "|".join(item.document_id for item, _ in indexed).encode()).hexdigest()[:20],
            "kind": "material_gap", "reason": "retrieval_budget_exhausted" if len(selected) > budget else "retrieval_not_matched",
            "missing_topics": gaps, "inspected_documents": [item.document_id for item, _ in indexed],
            "next_action": "expand_official_disclosure_retrieval"})
    return list({str(item.get("evidence_id")): item for item in collected}.values())


def saved_filing_evidence(
    artifacts: Sequence[dict[str, Any]], filings: Sequence[FilingDocument], *, company_cik: str,
) -> list[dict[str, Any]]:
    """Reuse host-captured inputs, not provider claims or an old success flag.

    A removed local PDF need not erase its already captured text. Conversely,
    a changed document hash, issuer or URL invalidates that particular input.
    Missing current filing identity cannot be repaired by guessing from a model.
    """
    current = {filing.document_id: filing for filing in filings}
    selected: dict[str, dict[str, Any]] = {}
    for artifact in reversed(artifacts):
        if artifact.get("artifact_type") != "deterministic-financial-summary":
            continue
        content = artifact.get("content")
        subject = content.get("company") if isinstance(content, dict) else None
        if not isinstance(subject, dict) or subject.get("cik") != company_cik:
            continue
        evidence = content.get("evidence")
        if not isinstance(evidence, list):
            continue
        for source in evidence:
            if not isinstance(source, dict) or source.get("kind") != "filing_text":
                continue
            filing = current.get(str(source.get("document_id") or ""))
            evidence_id = str(source.get("evidence_id") or "")
            document_hash = source.get("document_sha256") or source.get("content_hash")
            if (filing is None or not evidence_id or not filing.content_hash or
                document_hash != filing.content_hash or source.get("source_url") != filing.source_url or
                source.get("company_cik") != filing.company_cik or
                source.get("end_date") != filing.period_end or
                source.get("fiscal_period") != filing.fiscal_period or not usable_disclosure(source)):
                continue
            item = dict(source)
            item["document_sha256"] = filing.content_hash
            selected.setdefault(evidence_id, item)
    return list(selected.values())


def financial_evidence_record(item: EvidenceRef) -> dict[str, Any]:
    """Project references without upgrading an unverified visual observation."""
    record = item.to_dict()
    if str(record.get("evidence_id", "")).startswith("observation:"):
        record.update({"kind": "unverified_visual_observation",
                       "verification_status": "unverified", "usable_for_calculation": False})
    return record
