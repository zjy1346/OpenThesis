"""Pure binding of a visual JSON observation to document and page identities.

This verifies the captured model observation, not image-model infallibility.
Numeric corroboration and the financial invariants remain downstream.
"""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
import re

from .financial_cloud_proof import canonical_cloud_source, cloud_period_start
from .financial_contracts import source_numeric_token
from .financial_unit_declarations import literal_money_unit
from .text_normalization import canonical_search_text


GATEWAY_FACT_VERSION = "vision-json-v4"


def gateway_source_id(document_id, payload):
    return "vision-observation:" + sha256((document_id + "|" + canonical_cloud_source(payload)).encode()).hexdigest()


def gateway_source_issue(fact, row_ref, evidence_by_id):
    if fact.parser_version != GATEWAY_FACT_VERSION:
        return "stale_gateway_observation_contract" if fact.parser_version == "vision-json-v3" else None
    ids = tuple(key for key in fact.classification_evidence_ids if key.startswith("vision-observation:"))
    if len(ids) != 1 or row_ref is None:
        return "gateway_source_proof_missing"
    proof = evidence_by_id.get(ids[0])
    if (proof is None or proof.document_id != row_ref.document_id
            or proof.source_url != row_ref.source_url or proof.content_hash != row_ref.content_hash
            or proof.locator != row_ref.locator or not proof.content_hash):
        return "gateway_source_binding_mismatch"
    try:
        payload = json.loads(proof.excerpt)
        observed = payload["observation"]
        lineage = fact.normalization_lineage
        page = payload["original_page"]
        if (payload["schema"] != GATEWAY_FACT_VERSION
                or payload["document_hash"] != row_ref.content_hash
                or not re.fullmatch(r"[0-9a-f]{64}", payload["page_hash"])
                or isinstance(page, bool) or not isinstance(page, int) or page < 1
                or page != fact.source_page or row_ref.locator != f"page:{page}"
                or gateway_source_id(row_ref.document_id, payload) != proof.evidence_id
                or lineage is None or lineage.inputs
                or observed["concept"] != fact.concept or observed["statement"] != fact.statement
                or observed["scope"] != fact.consolidated_scope or observed["scope"] != "consolidated"
                or observed["currency"] != fact.currency
                or observed["period_end"] != fact.end_date
                or observed["raw_value"] != lineage.raw_value
                or Decimal(observed["scale"]) != Decimal(lineage.scale)
                or observed["raw_text"] != fact.raw_text or row_ref.excerpt != fact.raw_text
                or cloud_period_start(fact.fiscal_period, fact.end_date, fact.statement) != fact.start_date):
            return "gateway_observation_identity_mismatch"
        unit_refs = tuple(evidence_by_id.get(key) for key in lineage.unit_evidence_ids)
        if (len(unit_refs) != 1 or unit_refs[0] is None
                or unit_refs[0].excerpt != observed["unit_text"]
                or not literal_money_unit(observed["unit_text"]).proves(fact.currency, lineage.scale)):
            return "gateway_observation_unit_mismatch"
        row_text = canonical_search_text(fact.raw_text)
        if any(not canonical_search_text(observed[key])
               or canonical_search_text(observed[key]) not in row_text
               for key in ("unit_text", "scope_text", "period_text")):
            return "gateway_observation_source_phrases_missing"
        digits = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
        numbers = re.findall(r"(?<![\d.])(?:\(\s*" + digits + r"\s*\)|[+-]?" + digits + r")(?![\d.])",
                             source_numeric_token(fact.raw_text))
        def amount(token):
            value = source_numeric_token(token).replace(",", "")
            if value.startswith("(") and value.endswith(")"):
                value = "-" + value[1:-1].strip()
            return Decimal(value)
        if amount(observed["raw_value"]) not in {amount(token) for token in numbers}:
            return "gateway_observation_raw_value_missing"
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return "gateway_source_proof_invalid"
    return None
