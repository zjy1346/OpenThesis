"""Pure identity and reproducibility metadata for captured research material."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain import Company, utc_now_iso
from ..market_snapshot import ReverseDcfPolicy
from ..storage import DERIVED_PIPELINE_CONTRACT


def canonical_snapshot_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def financial_semantics_digest(facts: Any) -> str:
    """Bind source/value identity, excluding only admission display statuses.

    Upgrading a cohort from PARTIAL to individually VERIFIED must not rewrite
    the historical meaning. Values, units, dates, scope and original proof are
    all retained; fact-ID equality alone cannot excuse a changed amount.
    """
    statuses = {"validation_status", "extraction_status", "provenance_status", "derived_version"}
    rows = [{key: value for key, value in item.items() if key not in statuses} for item in facts]
    rows.sort(key=lambda item: (str(item.get("fact_id", "")), canonical_snapshot_digest(item)))
    return canonical_snapshot_digest(rows)


def build_research_snapshot(
    company: Company, facts: list[dict[str, Any]], filing_evidence: list[dict[str, Any]],
    pack: Any, report_language: str, parallel_agents: bool, annual_history_years: int,
    valuation_inputs: dict[str, Any] | None, market_snapshot: dict[str, Any] | None,
    valuation_policy: ReverseDcfPolicy | None = None, vision_policy: dict[str, Any] | None = None,
    policy_source: str = "policy_default",
) -> dict[str, Any]:
    policy = valuation_policy or ReverseDcfPolicy()
    return {
        "data_snapshot": {
            "captured_at": utc_now_iso(),
            "company_identity": canonical_snapshot_digest(company.to_dict()),
            "financial_facts_sha256": canonical_snapshot_digest(facts),
            "financial_semantics_sha256": financial_semantics_digest(facts),
            "financial_fact_count": len(facts),
            "financial_fact_ids_sha256": canonical_snapshot_digest(sorted(
                str(item.get("fact_id", "")) for item in facts if item.get("fact_id"))),
            "filing_evidence_sha256": canonical_snapshot_digest(filing_evidence),
            "filing_evidence_count": len(filing_evidence),
            "derived_pipeline_contract": dict(DERIVED_PIPELINE_CONTRACT),
        },
        "research_configuration": {
            "report_language": report_language, "parallel_agents": bool(parallel_agents),
            "valuation_inputs": valuation_inputs, "market_snapshot": market_snapshot,
            "valuation_policy": {
                "version": policy.version, "horizon_years": policy.horizon_years,
                "discount_rate": policy.discount_rate, "terminal_growth": policy.terminal_growth,
                "source": policy_source,
            },
            "research_pack_id": pack.pack_id, "research_pack_version": pack.version,
            "research_pack_content_identity": pack.content_hash,
            "annual_history_years": int(annual_history_years),
            "vision_policy": dict(vision_policy or {}),
        },
    }


def scanner_diagnostic_code(*diagnostic_sources: Any) -> str | None:
    for source in diagnostic_sources:
        if isinstance(source, str):
            values = (source,)
        elif isinstance(source, dict):
            values = tuple(source.values())
        else:
            try:
                values = tuple(source or ())
            except TypeError:
                values = ()
        for value in values:
            for code in ("SCANNED_IMAGE_FILING_DETECTED", "SCANNED_IMAGE_LAYOUT_UNRESOLVED"):
                if code in str(value):
                    return code
    return None
