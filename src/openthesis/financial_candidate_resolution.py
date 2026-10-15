"""Pure reconciliation of source-proven displayed rounding, never source repair."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
import json
import re
from typing import Sequence

from .domain import EvidenceRef, FilingDocument
from .financial_contracts import FactCandidate, FactIdentity, PDF_PARSER_VERSION
from .financial_xbrl_semantics import INLINE_PARSER_VERSION
from .financial_xbrl_presentation import PRESENTATION_VERSION
from .financial_sources.amount_disclosures import DISCLOSURE_VERSION
from .financial_sources.note_disclosures import NOTE_DISCLOSURE_VERSIONS


def resolve_issuer_presentation(
    options: Sequence[FactCandidate], source_evidence: Sequence[EvidenceRef],
    filing: FilingDocument | None,
) -> dict[str, FactCandidate]:
    """Fence uncontextualized taxonomy roles contradicted by the original.

    Every option must have passed independent source validation before this
    function is called. This resolves presentation, not amount conflicts: no
    value is generated or subtracted and the structured observation is kept
    in quarantine. An unproven native interpretation never overrides a row.
    """
    if filing is None or not filing.content_hash:
        return {}
    refs = {ref.evidence_id: ref for ref in source_evidence}
    authorities = []
    for option in options:
        fact = option.fact
        if (fact.parser_version != INLINE_PARSER_VERSION or fact.concept != "total_equity"
                or fact.consolidated_scope != "consolidated"
                or not re.fullmatch(r"\{https?://fasb\.org/us-gaap/20\d{2}(?:q[1-4]|-\d{2}-\d{2})?\}StockholdersEquity",
                                    fact.reported_concept)
                or fact.normalization_lineage is None):
            continue
        for proof_id in fact.normalization_lineage.unit_evidence_ids:
            ref = refs.get(proof_id)
            if (ref is None or ref.document_id != filing.document_id
                    or ref.content_hash.casefold() != filing.content_hash.casefold()):
                continue
            try:
                proof = json.loads(ref.excerpt)
                if (proof.get("schema") == INLINE_PARSER_VERSION
                        and proof.get("presentation", {}).get("schema") == PRESENTATION_VERSION):
                    authorities.append(option)
                    break
            except (ValueError, AttributeError, TypeError):
                continue

    def source_context(candidate: FactCandidate) -> tuple:
        fact = candidate.fact
        return (fact.company_cik, fact.accession_number, fact.start_date, fact.end_date,
                fact.fiscal_year, fact.fiscal_period, fact.consolidated_scope,
                fact.currency, fact.statement, fact.revision)

    superseded = {}
    for option in options:
        fact = option.fact
        if (fact.parser_version != "sec-companyfacts-v4" or fact.concept != "equity"
                or fact.reported_concept != "StockholdersEquity"):
            continue
        matches = [authority for authority in authorities
                   if source_context(authority) == source_context(option)]
        if matches:
            superseded[fact.fact_id] = min(matches, key=lambda item: item.fact.fact_id)
    return superseded


@dataclass(frozen=True, slots=True)
class DisplayRoundingResolution:
    selected: FactCandidate
    rounded_candidate_ids: tuple[str, ...]
    policy: str = "same-original-display-quantization-v1"


def resolve_display_rounding(
    options: Sequence[FactCandidate], filing: FilingDocument,
) -> DisplayRoundingResolution | None:
    """Resolve one precise original against coarser displays of the same fact.

    Callers must independently validate every option first. No arbitrary
    tolerance, magnitude heuristic, currency conversion or generated value is
    allowed. A coarser printed unit's half-quantum must enclose the selected
    original amount; disagreeing equally precise originals remain conflicts.
    Both originals must be positioned, hashed observations in the same filing.
    """
    if len(options) < 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", filing.content_hash):
        return None
    identity = FactIdentity.from_fact(options[0].fact)
    observations: list[tuple[FactCandidate, Decimal, Decimal]] = []
    source_scales = set()
    for option in options:
        fact = option.fact
        lineage = fact.normalization_lineage
        if (
            FactIdentity.from_fact(fact) != identity
            or fact.statement != options[0].fact.statement
            or fact.parser_version not in {PDF_PARSER_VERSION, DISCLOSURE_VERSION, *NOTE_DISCLOSURE_VERSIONS}
            or fact.concept == "reported_roe"
            or fact.source_page is None or fact.source_bbox is None
            or lineage is None or lineage.inputs
            or lineage.quantity_kind != "money"
            or lineage.value_transform not in {"identity", "absolute_value"}
            or (lineage.value_transform == "absolute_value" and fact.concept not in {
                "capital_expenditure", "share_repurchase", "shareholder_dividends_paid",
            })
            or (options[0].fact.normalization_lineage is not None
                and lineage.value_transform != options[0].fact.normalization_lineage.value_transform)
            or len(option.evidence) != 1
        ):
            return None
        ref = option.evidence[0]
        if (
            ref.document_id != filing.document_id
            or ref.source_url != filing.source_url
            or ref.content_hash.casefold() != filing.content_hash.casefold()
            or ref.locator != f"page:{fact.source_page}"
            or ref.bbox != fact.source_bbox or ref.excerpt != fact.raw_text
        ):
            return None
        raw = lineage.raw_value.strip().replace(",", "").replace("−", "-")
        if raw.startswith("(") and raw.endswith(")"):
            raw = "-" + raw[1:-1].strip()
        try:
            raw_value, scale = Decimal(raw), Decimal(lineage.scale)
            amount = Decimal(lineage.normalized_value)
            if not all(value.is_finite() for value in (raw_value, scale, amount)) or scale <= 0:
                return None
            with localcontext() as context:
                context.prec = max(50, len(raw) + len(lineage.scale) + len(lineage.normalized_value) + 8)
                quantum = Decimal(1).scaleb(raw_value.as_tuple().exponent) * scale
        except (ArithmeticError, ValueError):
            return None
        source_scales.add(scale)
        observations.append((option, amount, quantum))
    # Equal-unit differing source values are not a display-scale conflict.
    if len(source_scales) < 2:
        return None
    finest = min(quantum for _option, _amount, quantum in observations)
    precise = [(option, amount) for option, amount, quantum in observations if quantum == finest]
    if len({amount for _option, amount in precise}) != 1:
        return None
    selected, selected_amount = min(precise, key=lambda item: item[0].fact.fact_id)
    rounded_ids = []
    for option, amount, quantum in observations:
        with localcontext() as context:
            context.prec = max(50, len(str(amount)) + len(str(selected_amount)) + len(str(quantum)) + 8)
            if abs(amount - selected_amount) > quantum / 2:
                return None
        if quantum > finest:
            rounded_ids.append(option.fact.fact_id)
    if not rounded_ids:
        return None
    return DisplayRoundingResolution(selected, tuple(sorted(rounded_ids)))
