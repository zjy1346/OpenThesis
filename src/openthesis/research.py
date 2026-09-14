from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .domain import (
    Company,
    ResearchArtifact,
    ResearchRun,
    RunStatus,
    utc_now_iso,
)
from .financials import (
    NormalizedMoney,
    calculate_interim_metrics,
    calculate_metrics,
    deterministic_summary,
    reverse_dcf_analysis,
)
from .financial_ingestion import FinancialProfile
from .growth import normalize_growth_output
from .i18n import EN, OUTPUT_LANGUAGE_INSTRUCTIONS, UI_HANT, ZH_HANT, normalize_language, translate
from .packs import ResearchPack
from .providers import ModelConfig, ModelProvider, ProviderError
from .storage import Storage
from .research_materials import route_materials, insufficient_material_result


ProgressCallback = Callable[[str, int], None]
CancelCheck = Callable[[], bool]
AgentProgressCallback = Callable[[str, str], None]


def _valuation_money(value: object, *, currency: str, source_id: str = "", as_of: str = "") -> NormalizedMoney | None:
    """Build the typed valuation boundary from an already-normalized snapshot."""
    if isinstance(value, NormalizedMoney):
        return value
    if isinstance(value, dict):
        try:
            amount = float(value["value"])
            unit_scale = float(value.get("unit_scale", 1.0))
            provenance = str(value.get("unit_provenance", value.get("provenance", "normalized")))
            item_currency = str(value.get("currency") or currency).upper()
            if provenance == "normalized" or unit_scale == 1.0:
                return NormalizedMoney.from_normalized(
                    amount, item_currency,
                    source_id=str(value.get("source_id", source_id)),
                    as_of=str(value.get("as_of", as_of)),
                )
            return NormalizedMoney.from_raw(amount, item_currency, unit_scale, provenance)
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return NormalizedMoney.from_normalized(value, currency, source_id=source_id, as_of=as_of)
    return None


def _valuation_money_is_positive(value: object, *, currency: str) -> bool:
    item = _valuation_money(value, currency=currency)
    return item is not None and item.normalized_value > 0


class ResearchCancelled(RuntimeError):
    def __init__(self, message: str = "研究已由用户取消", run_id: str = ""):
        super().__init__(message)
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class ProviderContextCapability:
    """Provider input budget used to choose a lossless context shape."""

    max_input_tokens: int = 128_000
    reserved_output_tokens: int = 4_096
    reserved_repair_tokens: int = 2_048
    explicit_max_input_bytes: int | None = None

    @property
    def max_input_bytes(self) -> int:
        if self.explicit_max_input_bytes is not None:
            return max(1, int(self.explicit_max_input_bytes))
        # UTF-8 can use up to four bytes per token; this is only a shape
        # threshold, never a permission to truncate research material.
        available = max(
            1,
            self.max_input_tokens
            - self.reserved_output_tokens
            - self.reserved_repair_tokens,
        )
        # Keep the reservation honest even for tiny test/local providers.  A
        # fixed 16 KiB floor can consume the entire window and silently
        # discard the output/repair reservation that this value represents.
        return max(1_024, available * 4)


class SynthesisContextLimitError(RuntimeError):
    """The complete context cannot fit without deleting research material."""

    def __init__(
        self,
        sections: list[dict[str, Any]],
        limit_bytes: int,
        *,
        required_bytes: int | None = None,
        counting_mode: str = "conservative_utf8_byte_upper_bound",
    ):
        super().__init__("synthesis context exceeds provider input budget")
        self.sections = sections
        self.limit_bytes = limit_bytes
        self.required_bytes = required_bytes if required_bytes is not None else limit_bytes + 1
        self.available_bytes = limit_bytes
        self.counting_mode = counting_mode


def provider_context_capability(provider: Any) -> ProviderContextCapability:
    """Read an optional provider capability without changing old providers."""
    for name in ("context_window_tokens", "max_input_tokens"):
        value = getattr(provider, name, None)
        if isinstance(value, int) and value > 0:
            output = getattr(provider, "reserved_output_tokens", 4_096)
            repair = getattr(provider, "reserved_repair_tokens", 2_048)
            explicit = getattr(provider, "max_input_bytes", None)
            return ProviderContextCapability(
                value,
                output if isinstance(output, int) and output >= 0 else 4_096,
                repair if isinstance(repair, int) and repair >= 0 else 2_048,
                explicit if isinstance(explicit, int) and explicit > 0 else None,
            )
    return ProviderContextCapability()


CORE_SYSTEM_PROMPT = """\
You are a careful long-term company research analyst inside OpenThesis.
Use only the evidence supplied in the task. Never invent financial values,
citations, customers, products, events, or management statements.
Keep facts, calculations, inferences, assumptions, forecasts, risks, and
unknowns separate. If evidence is insufficient, say so explicitly.
Return one valid JSON object and no markdown wrapper. This is research
assistance, not personalized investment advice and never a trade instruction.
Read company.market, exchange, reporting_currency, accounting_standard, and
industry_support before analysis. For CN_A listings, explicitly examine
controlling-shareholder and related-party exposure, pledges, subsidies,
regulatory/delisting risks, audit opinions, and CAS reporting scope; never treat
cumulative quarterly income or cash flow as a standalone quarter. For HK
listings, explicitly examine listing structure, controlling shareholders,
connected transactions, VIE exposure where evidenced, HKEX compliance, reporting
standard, and currency differences. For financial_beta issuers, do not apply an
ordinary-company free-cash-flow valuation framework.
Always distinguish fiscal_period values: FY is a full year, while Q1, H1, and Q3
are cumulative interim periods. Cover the latest supplied interim period, compare
it only with the same prior-year period, and never combine it with FY totals.
"""


def _agent_prompt_bundle(
    agent_id: str,
    role_prompt: str,
    report_language: str,
    context_json: str,
    prior_artifacts: dict[str, Any],
) -> tuple[str, str]:
    """Build the exact provider prompts used for budget accounting."""
    language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS[report_language]
    user_prompt = json.dumps(
        {
            "agent": agent_id,
            "task_instructions": role_prompt,
            "output_language": report_language,
            "output_language_instruction": language_instruction,
            "research_context": route_materials(json.loads(context_json), agent_id),
            "prior_artifacts": prior_artifacts,
            "required_claim_shape": {
                "text": "string",
                "kind": "fact|calculation|inference|assumption|forecast|risk|unknown",
                "confidence": "0..1 or null",
                "evidence_ids": ["fact:<id>"],
            },
        },
        ensure_ascii=False,
    )
    system_prompt = CORE_SYSTEM_PROMPT + "\n" + language_instruction
    if agent_id == "research-synthesizer-repair":
        system_prompt += "\n\n" + role_prompt
    return system_prompt, user_prompt


def _agent_input_size(
    agent_id: str,
    role_prompt: str,
    report_language: str,
    context_json: str,
    prior_artifacts: dict[str, Any],
) -> int:
    system_prompt, user_prompt = _agent_prompt_bundle(
        agent_id, role_prompt, report_language, context_json, prior_artifacts
    )
    return len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))


@dataclass(slots=True)
class ResearchContext:
    company: Company
    facts: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    interim_metrics: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    valuation: dict[str, Any] | None = None
    market_snapshot: dict[str, Any] | None = None

    def compact_json(self) -> str:
        return json.dumps(
            {
                "company": self.company.to_dict(),
                "metrics": self.metrics,
                "latest_interim_metrics": self.interim_metrics,
                "evidence": self.evidence,
                "valuation": self.valuation,
                "market_snapshot": self.market_snapshot,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def build_fact_evidence(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for fact in facts:
        evidence.append(
            {
                "evidence_id": f"fact:{fact['fact_id']}",
                "kind": "financial_fact",
                "company_cik": fact.get("company_cik", ""),
                "entity": fact.get("entity", ""),
                "market": fact.get("market", ""),
                "concept": fact["concept"],
                "value": fact["value"],
                "unit": fact["unit"],
                "unit_scale": fact.get("unit_scale", 1.0),
                "unit_provenance": fact.get("unit_provenance", "unknown"),
                "scope": fact.get("scope", ""),
                "consolidated_scope": fact.get("consolidated_scope", ""),
                "fiscal_year": fact["fiscal_year"],
                "fiscal_period": fact.get("fiscal_period", ""),
                "form_type": fact.get("form_type", ""),
                "start_date": fact.get("start_date", ""),
                "end_date": fact.get("end_date", ""),
                "filed_at": fact["filed_at"],
                "source_url": fact["source_url"],
                "raw_text": fact.get("raw_text", ""),
                "accession_number": fact.get("accession_number", ""),
                "source_document": fact.get("source_document", ""),
            }
        )
    return evidence


def verify_agent_output(
    output: dict[str, Any],
    available_evidence: set[str],
    language: str = "zh-CN",
    evidence_records: dict[str, dict[str, Any]] | None = None,
    *,
    semantic_reviewer: Callable[[dict[str, Any], list[dict[str, Any]]], Any] | None = None,
) -> dict[str, Any]:
    english = normalize_language(language) == EN
    issues: list[str] = []
    material_gate = output.get('_material_adequacy')
    if isinstance(material_gate, dict) and material_gate.get('status') == 'insufficient':
        issues.append('Required official material is missing' if english else '缺少必要的官方披露材料')
    claims = output.get("claims", [])
    if claims is not None and not isinstance(claims, list):
        issues.append("claims must be an array" if english else "claims 必须是数组")
        claims = []
    verified_count = 0
    unsupported_count = 0
    claim_verifications: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(claims or []):
        if not isinstance(claim, dict):
            issues.append(
                "A claim is not a JSON object" if english else "存在非对象 claim"
            )
            claim_verifications.append({"index": claim_index, "state": "insufficient_evidence"})
            continue
        evidence_ids = claim.get("evidence_ids", [])
        if not isinstance(evidence_ids, list):
            issues.append(
                "claim.evidence_ids must be an array"
                if english
                else "claim.evidence_ids 必须是数组"
            )
            claim_verifications.append({"index": claim_index, "state": "insufficient_evidence"})
            continue
        missing = [item for item in evidence_ids if item not in available_evidence]
        if missing:
            prefix = "Unknown evidence reference: " if english else "引用不存在："
            issues.append(prefix + ", ".join(map(str, missing)))
        asserted = {
            key: claim.get(key)
            for key in (
                "company_cik", "entity", "market", "concept", "value", "unit", "currency",
                "unit_scale", "unit_provenance", "scope", "consolidated_scope",
                "start_date", "end_date", "filed_at", "fiscal_year",
                "fiscal_period", "form_type", "accession_number", "source_document",
            )
            if claim.get(key) not in (None, "")
        }
        cited_records: list[dict[str, Any]] = []
        financial_records = []
        matching_records: list[dict[str, Any]] = []
        related_records: list[dict[str, Any]] = []
        if evidence_records and not missing:
            cited_records = [
                evidence_records[item]
                for item in evidence_ids
                if item in evidence_records and isinstance(evidence_records[item], dict)
            ]
            financial_records = [
                record for record in cited_records
                if record.get("kind") == "financial_fact"
            ]
            matching_records = [
                record for record in financial_records
                if _claim_matches_financial_evidence(asserted, record)
            ]
            related_records = [
                record for record in financial_records
                if _same_financial_target(asserted, record)
            ]
            if financial_records and asserted and (
                not matching_records
                or any(record not in matching_records for record in related_records)
            ):
                issues.append(
                    "Claim period/value does not match its cited financial evidence"
                    if english
                    else "结论中的期间或数值与引用的财务证据不一致"
                )
        state = "insufficient_evidence"
        if missing or not evidence_ids:
            state = "insufficient_evidence"
        elif claim.get("kind") in {"fact", "calculation"} and not _claim_has_deterministic_fields(claim):
            # A factual/calculation channel without deterministic fields is
            # not allowed to become text-supported merely because its prose
            # resembles a cited excerpt.
            text_state = _claim_text_support_state(claim, cited_records)
            if text_state == "contradicted":
                state = "contradicted"
                issues.append(
                    "Claim text contradicts its cited evidence"
                    if english else "结论文本与引用的证据方向相反"
                )
            else:
                issues.append(
                    "Factual claims require deterministic identity and value fields"
                    if english else "事实性结论必须包含可确定核验的身份与数值字段"
                )
                state = "insufficient_evidence"
        elif financial_records and asserted and matching_records and not any(
            record not in matching_records for record in related_records
        ):
            state = "numeric_verified"
        elif financial_records and asserted:
            state = "contradicted"
        else:
            text_state = _claim_text_support_state(claim, cited_records)
            if text_state == "contradicted":
                state = "contradicted"
                issues.append(
                    "Claim text contradicts its cited evidence"
                    if english
                    else "结论文本与引用的证据方向相反"
                )
            elif (
                text_state == "insufficient_evidence"
                and semantic_reviewer is not None
                and claim.get("kind") == "inference"
            ):
                try:
                    review = semantic_reviewer(claim, cited_records)
                except Exception:
                    review = None
                if str(review).casefold() in {"entailed", "supported", "text_supported"}:
                    text_state = "text_supported"
            if text_state == "contradicted":
                state = "contradicted"
            elif text_state == "text_supported":
                state = (
                    "reference_only"
                    if claim.get("kind") in {
                        "assumption", "forecast", "opinion", "scenario", "risk"
                    }
                    else "text_supported"
                )
            else:
                state = (
                    "insufficient_evidence"
                    if claim.get("kind") in {"fact", "calculation"}
                    and not _claim_has_deterministic_fields(claim)
                    else "reference_only"
                )
                # Explicitly labeled inference/assumption material may remain
                # in the audit result as reference_only.  It is not counted
                # as verified and is removed from downstream stage material;
                # factual claims and asserted values remain fail-closed.
                if claim.get("kind") in {"fact", "calculation"} or asserted:
                    issues.append(
                        "Citation exists, but the claim is not numerically or textually supported"
                        if english
                        else "虽然存在引用，但该结论没有数值或文本证据支持"
                    )
        if state in {"numeric_verified", "text_supported"}:
            verified_count += 1
        elif claim.get("kind") in {"fact", "calculation"} or asserted:
            unsupported_count += 1
        claim_verifications.append({
            "index": claim_index,
            "state": state,
            "evidence_ids": list(evidence_ids),
        })
    structured_output_valid = bool(output.get("structured_output_valid", True))
    if not structured_output_valid:
        issues.append(
            "Model output was not valid structured JSON"
            if english
            else "模型输出不是有效的结构化 JSON"
        )
    return {
        "structured_output_valid": structured_output_valid,
        "claim_count": len(claims or []),
        "verified_claim_count": verified_count,
        "unsupported_fact_count": unsupported_count,
        "claim_verifications": claim_verifications,
        "issues": issues,
        "passed": structured_output_valid and not issues and unsupported_count == 0,
    }


def _claim_text_support_state(
    claim: dict[str, Any], records: list[dict[str, Any]]
) -> str:
    """Classify prose claims conservatively from cited source text.

    A valid citation is only a reference.  Text support requires source text
    plus either matching direction and vocabulary; opposite direction is an
    explicit contradiction.  This intentionally does not attempt open-ended
    semantic inference.
    """
    claim_text = str(
        claim.get("text") or claim.get("conclusion") or claim.get("argument") or ""
    ).strip()
    if not claim_text or not records:
        return "insufficient_evidence"
    claim_direction = _claim_direction(claim_text)
    for record in records:
        source_text = str(
            record.get("raw_text") or record.get("text") or record.get("excerpt") or ""
        ).strip()
        if not source_text:
            continue
        source_direction = _claim_direction(source_text)
        if claim_direction and source_direction and claim_direction != source_direction:
            return "contradicted"
        claim_words = set(re.findall(r"[A-Za-z]{4,}|[\u4e00-\u9fff]{2,}", claim_text.casefold()))
        source_words = set(re.findall(r"[A-Za-z]{4,}|[\u4e00-\u9fff]{2,}", source_text.casefold()))
        concept = str(record.get("concept", "")).replace("_", " ").casefold()
        if claim_words & source_words or concept and concept in claim_text.casefold():
            return "text_supported"
    return "insufficient_evidence"


def _claim_direction(text: str) -> str | None:
    lowered = text.casefold()
    positive = re.search(
        r"\b(?:grow|grew|growth|increase|increased|rise|rose|up|higher|improv)\w*\b|增长|增加|上升|提升|改善",
        lowered,
    )
    negative = re.search(
        r"\b(?:declin|decreas|decreased|fall|fell|down|lower|wors)\w*\b|下降|减少|下滑|降低|恶化",
        lowered,
    )
    if positive and not negative:
        return "positive"
    if negative and not positive:
        return "negative"
    return None


def _claim_matches_financial_evidence(
    asserted: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    for key, expected in asserted.items():
        actual = evidence.get(key)
        if key == "currency" and actual in (None, ""):
            actual = evidence.get("unit")
        if key == "value":
            try:
                left = Decimal(str(expected))
                right = Decimal(str(actual))
            except (InvalidOperation, TypeError, ValueError):
                return False
            tolerance = max(Decimal("0.000001"), abs(right) * Decimal("1e-9"))
            if abs(left - right) > tolerance:
                return False
        elif key == "unit_scale":
            try:
                if Decimal(str(expected)) != Decimal(str(actual)):
                    return False
            except (InvalidOperation, TypeError, ValueError):
                return False
        elif str(expected).strip().casefold() != str(actual).strip().casefold():
            return False
    return True


def _same_financial_target(asserted: dict[str, Any], evidence: dict[str, Any]) -> bool:
    """Identify a cited financial fact target without treating its value as identity."""
    if "concept" in asserted and str(asserted["concept"]).casefold() != str(
        evidence.get("concept", "")
    ).casefold():
        return False
    for key in ("company_cik", "entity"):
        expected = asserted.get(key)
        actual = evidence.get(key)
        if expected not in (None, "") and actual not in (None, ""):
            if str(expected).strip().casefold() != str(actual).strip().casefold():
                return False
    for key in ("fiscal_year", "fiscal_period", "end_date"):
        expected = asserted.get(key)
        actual = evidence.get(key)
        if expected not in (None, "") and actual not in (None, ""):
            if str(expected).strip().casefold() != str(actual).strip().casefold():
                return False
    return True


def _claim_has_deterministic_fields(claim: dict[str, Any]) -> bool:
    kind = str(claim.get("kind", "")).casefold()
    has_period = bool(
        claim.get("end_date")
        or (claim.get("fiscal_year") not in (None, "") and claim.get("fiscal_period"))
    )
    has_value_unit = claim.get("value") not in (None, "") and bool(
        claim.get("unit") or claim.get("currency")
    )
    if kind == "fact":
        return bool(claim.get("concept")) and has_value_unit and has_period
    if kind == "calculation":
        return (
            has_value_unit
            and has_period
            and bool(claim.get("formula") or claim.get("input_claim_ids") or claim.get("inputs"))
        )
    return True


_REQUIRED_SYNTHESIS_SECTIONS = frozenset(
    {
        "executive_summary",
        "business_model",
        "financial_quality",
        "balance_sheet",
        "competitive_position",
        "growth_opportunities",
        "counterarguments",
        "scenarios",
        "thesis",
        "invalidation_conditions",
        "leading_indicators",
        "unresolved_questions",
        "claims",
    }
)

def validate_research_synthesis(
    output: dict[str, Any],
    available_evidence: set[str],
    language: str = "zh-CN",
    evidence_records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    verification = verify_agent_output(
        output, available_evidence, language, evidence_records
    )
    english = normalize_language(language) == EN
    missing = sorted(
        key
        for key in _REQUIRED_SYNTHESIS_SECTIONS
        if key not in output or output.get(key) in (None, "", [])
    )
    if missing:
        prefix = "Missing required report sections: " if english else "缺少必要报告章节："
        verification["issues"].append(prefix + ", ".join(missing))
    if verification["claim_count"] == 0:
        verification["issues"].append(
            "The final report contains no verifiable claims"
            if english
            else "最终报告没有可验证的主要结论"
        )
    verification["passed"] = not verification["issues"] and verification["unsupported_fact_count"] == 0
    return verification


def _presentation_stage_value(value: Any, *, remove_claims: bool = True) -> Any:
    if isinstance(value, dict):
        return {
            key: _presentation_stage_value(item, remove_claims=remove_claims)
            for key, item in value.items()
            if not str(key).startswith("_")
            and key != "structured_output_valid"
            and (not remove_claims or key != "claims")
        }
    if isinstance(value, list):
        return [
            _presentation_stage_value(item, remove_claims=remove_claims)
            for item in value
        ]
    return value


def _collect_stage_claims(*values: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            claims = value.get("claims")
            if isinstance(claims, list):
                found.extend(item for item in claims if isinstance(item, dict))
            for key, item in value.items():
                if key != "claims" and not str(key).startswith("_"):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in found:
        text = str(
            claim.get("text")
            or claim.get("conclusion")
            or claim.get("argument")
            or ""
        ).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(_presentation_stage_value(claim, remove_claims=False))
    return unique


def _typed_trusted_channels(
    claims: list[dict[str, Any]], states: dict[int, str],
) -> dict[str, list[dict[str, Any]]]:
    channels: dict[str, list[dict[str, Any]]] = {
        "verified_facts": [],
        "verified_calculations": [],
        "labelled_inferences": [],
        "assumptions_scenarios": [],
        "unresolved": [],
    }
    for index, claim in enumerate(claims):
        state = states.get(index, "insufficient_evidence")
        kind = str(claim.get("kind", "unknown")).casefold()
        if state in {"numeric_verified", "text_supported"}:
            channel = (
                "verified_facts" if kind == "fact" else
                "verified_calculations" if kind == "calculation" else
                "labelled_inferences"
            )
        elif kind in {"assumption", "forecast", "risk"}:
            channel = "assumptions_scenarios"
        else:
            channel = "unresolved"
        channels[channel].append(dict(claim))
    return channels


def _trusted_stage_result(
    result: dict[str, Any], verification: dict[str, Any]
) -> dict[str, Any]:
    """Return only material that passed the claim verification gate.

    Raw provider output is persisted separately for audit.  When a stage is
    partial, narrative fields are intentionally withheld; otherwise an
    unverified paragraph could influence later agents despite a rejected
    citation.
    """
    states = {
        int(item.get("index", -1)): item.get("state")
        for item in verification.get("claim_verifications", [])
        if isinstance(item, dict)
    }
    trusted_claims = [
        claim
        for index, claim in enumerate(result.get("claims", []))
        if isinstance(claim, dict)
        and states.get(index) in {"numeric_verified", "text_supported"}
    ] if isinstance(result.get("claims"), list) else []
    channels = _typed_trusted_channels(
        result.get("claims", []) if isinstance(result.get("claims"), list) else [],
        states,
    )
    if not isinstance(result.get("claims"), list):
        kind = str(result.get("kind", "")).casefold()
        if kind in {"unknown", "opinion", "assumption", "unresolved"}:
            retained = dict(result)
            retained["_verification_state"] = "completed_partial"
            retained["_verified_claim_count"] = 0
            retained["trusted_channels"] = channels
            return retained
        return {
            "_verification_state": "failed_verification",
            "_verified_claim_count": 0,
            "trusted_channels": channels,
        }
    # An empty claims array is not proof that an arbitrary narrative is safe:
    # only explicitly labelled non-factual material is allowed through that
    # channel.  Keep this before the all([]) check below, which would
    # otherwise incorrectly promote a no-claims analysis to verified.
    if isinstance(result.get("claims"), list) and not result.get("claims"):
        kind = str(result.get("kind", "")).casefold()
        if kind in {"unknown", "opinion", "assumption", "unresolved"}:
            retained = dict(result)
            retained["_verification_state"] = "completed_partial"
            retained["_verified_claim_count"] = 0
            retained["trusted_channels"] = channels
            return retained
        return {
            "claims": [],
            "_verification_state": "failed_verification",
            "_verified_claim_count": 0,
            "trusted_channels": channels,
        }
    if verification.get("passed") and isinstance(result.get("claims"), list):
        # Preserve explicitly labelled inference/opinion sections while
        # removing their unverified claims from downstream inputs.
        if all(
            state in {"numeric_verified", "text_supported"}
            for state in states.values()
        ):
            retained = dict(result)
            retained["trusted_channels"] = channels
            return retained
        retained = dict(result)
        retained["claims"] = trusted_claims
        retained["_verification_state"] = "completed_partial"
        retained["_verified_claim_count"] = len(trusted_claims)
        retained["trusted_channels"] = channels
        return retained
    if isinstance(result.get("claims"), list) and not states:
        return {
            "claims": [],
            "_verification_state": "failed_verification",
            "_verified_claim_count": 0,
            "trusted_channels": channels,
        }
    return {
        "claims": trusted_claims,
        "_verification_state": "completed_partial" if trusted_claims else "failed_verification",
        "_verified_claim_count": len(trusted_claims),
        "trusted_channels": channels,
    }


def _gate_stage_output(
    result: dict[str, Any],
    available: set[str],
    evidence_records: dict[str, dict[str, Any]],
    language: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify a later stage and return a safe downstream projection."""
    verification = verify_agent_output(result, available, language, evidence_records)
    if isinstance(result.get("scenarios"), list):
        # Forecast agents have a deliberately narrower contract than factual
        # analysts: scenarios are assumptions to be reviewed, never verified
        # facts.  Keep structurally valid scenarios in a partial channel and
        # validate any attached references/certainty bounds.
        safe_scenarios: list[Any] = []
        scenario_issues: list[str] = []
        for scenario in result["scenarios"]:
            if isinstance(scenario, str):
                if scenario.strip():
                    safe_scenarios.append(scenario)
                continue
            if not isinstance(scenario, dict):
                scenario_issues.append("Scenario must be an object or non-empty text")
                continue
            ids = [
                str(item).strip()
                for key in (
                    "evidence_ids",
                    "supporting_evidence_ids",
                    "contradicting_evidence_ids",
                )
                for item in scenario.get(key, [])
                if isinstance(scenario.get(key), list) and str(item).strip()
            ]
            if any(item not in available for item in ids):
                scenario_issues.append("Scenario contains unverified evidence")
                continue
            probability = scenario.get("probability_range")
            if probability is not None:
                valid_probability = (
                    isinstance(probability, list)
                    and len(probability) == 2
                    and all(isinstance(item, (int, float)) and 0 <= item <= 1 for item in probability)
                )
                if not valid_probability:
                    scenario_issues.append("Scenario probability range is invalid")
                    continue
            safe_scenarios.append(dict(scenario))
        verification["issues"].extend(scenario_issues)
        verification["passed"] = False
        trusted = {"scenarios": safe_scenarios}
        trusted["trusted_channels"] = {
            "verified_facts": [], "verified_calculations": [],
            "labelled_inferences": [], "assumptions_scenarios": safe_scenarios,
            "unresolved": [],
        }
        trusted["_verification_state"] = (
            "completed_partial" if safe_scenarios else "failed_verification"
        )
        trusted["_verified_claim_count"] = 0
        return trusted, verification
    if isinstance(result.get("opportunities"), list):
        safe_opportunities: list[dict[str, Any]] = []
        verified_opportunities = 0
        partial_opportunity = False
        for opportunity in result["opportunities"]:
            if not isinstance(opportunity, dict):
                continue
            supporting_ids = [
                str(item).strip()
                for item in opportunity.get("supporting_evidence_ids", [])
                if str(item).strip()
            ] if isinstance(opportunity.get("supporting_evidence_ids"), list) else []
            ids = [
                str(item).strip()
                for key in ("supporting_evidence_ids", "contradicting_evidence_ids")
                for item in opportunity.get(key, [])
                if isinstance(opportunity.get(key), list) and str(item).strip()
            ]
            valid_supporting = [item for item in supporting_ids if item in available]
            if not valid_supporting:
                verification["issues"].append(
                    "Growth opportunity requires verified supporting evidence"
                )
                continue
            if not all(item in available for item in ids):
                verification["issues"].append("Growth opportunity contains unverified evidence")
                continue
            # If the provider supplied an explicit claim/assertion, run it
            # through the same text/numeric gate as every other stage.  A
            # generic mechanism without a claim is retained only when it has
            # at least one concrete evidence record (not a reference-only
            # pointer).
            claim_text = str(
                opportunity.get("claim")
                or opportunity.get("assertion")
                or opportunity.get("text")
                or ""
            ).strip()
            opportunity_verified = True
            support_records = [
                evidence_records[item]
                for item in valid_supporting
                if isinstance(evidence_records.get(item), dict)
            ]
            if not support_records or all(
                str(record.get("kind", "")).casefold()
                in {"reference", "reference_only"}
                for record in support_records
            ):
                verification["issues"].append(
                    "Growth opportunity evidence is reference-only"
                )
                continue
            if claim_text:
                claim_check = verify_agent_output(
                    {
                        "claims": [
                            {
                                "kind": "inference",
                                "text": claim_text,
                                "evidence_ids": valid_supporting,
                            }
                        ]
                    },
                    available,
                    language,
                    evidence_records,
                )
                claim_state = (
                    claim_check.get("claim_verifications", [{}])[0].get("state")
                    if claim_check.get("claim_verifications")
                    else "insufficient_evidence"
                )
                if claim_state == "reference_only":
                    # Retain this explicitly qualified opportunity for human
                    # review and evidence-count display, but never promote it
                    # to a verified opportunity.
                    partial_opportunity = True
                    opportunity_verified = False
                elif claim_state not in {"numeric_verified", "text_supported"}:
                    verification["issues"].append(
                        "Growth opportunity claim failed evidence verification"
                    )
                    continue
            safe_opportunities.append(opportunity)
            if opportunity_verified:
                verified_opportunities += 1
        if partial_opportunity:
            verification["issues"].append(
                "Growth opportunity claim remains reference-only"
            )
        verification["passed"] = not verification["issues"]
        trusted = dict(result)
        trusted["opportunities"] = safe_opportunities
        lineage = dict(result.get("_lineage") or {})
        raw_count = int(lineage.get("raw_candidate_count", len(result["opportunities"])))
        lineage.update({
            "raw_candidate_count": raw_count,
            "normalized_count": int(lineage.get("normalized_count", len(result["opportunities"]))),
            "verified_count": verified_opportunities,
            "retained_count": len(safe_opportunities),
            "rejected_count": max(0, raw_count - len(safe_opportunities)),
            "rejected_reasons": list(dict.fromkeys(
                list(lineage.get("rejected_reasons", [])) + list(verification["issues"])
            )),
            "cap_applied": bool(lineage.get("cap_applied", False) or raw_count > 5),
            "cap_limit": int(lineage.get("cap_limit", 5)),
        })
        trusted["_lineage"] = lineage
        trusted["trusted_channels"] = {
            "verified_facts": [], "verified_calculations": [],
            "labelled_inferences": safe_opportunities if not partial_opportunity else [],
            "assumptions_scenarios": safe_opportunities if partial_opportunity else [],
            "unresolved": [],
        }
        trusted["_verification_state"] = (
            "completed_verified"
            if verification["passed"]
            else "completed_partial"
            if safe_opportunities
            else "failed_verification"
        )
        trusted["_verified_claim_count"] = len(safe_opportunities)
        return trusted, verification
    return _trusted_stage_result(result, verification), verification


def _collect_strings(value: Any, *keys: str) -> list[str]:
    found: list[str] = []
    internal_id = re.compile(
        r"(?i)(?:fact|evidence|filing|artifact|run):[A-Za-z0-9_.:/-]+|"
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b"
    )

    def visit(item: Any) -> None:
        if isinstance(item, str):
            text = internal_id.sub("", item).strip()
            if text and text not in found:
                found.append(text)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for key in keys:
                if key in item:
                    visit(item[key])
            for child in item.values():
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(value)
    return found


def _synthesis_prior_artifacts(
    dossier: Any,
    growth: Any,
    skeptic: Any,
    forecast: Any,
) -> dict[str, Any]:
    """Build a deterministic, lossless, section-preserving synthesis input.

    Full stage artifacts remain persisted separately.  The synthesis view
    removes only private transport fields; it never clips prose, arrays,
    claims, evidence, or sections based on a byte limit.
    """
    base_analyses = dossier.get("analyses", dossier) if isinstance(dossier, dict) else dossier
    values = {
        "base_analyses": base_analyses,
        "growth_opportunities": growth,
        "counter_analysis": skeptic,
        "forecast": forecast,
    }
    evidence_ids = _collect_evidence_ids(values)
    values["source_evidence_ids"] = evidence_ids
    return _lossless_synthesis_value(values)


def _synthesis_lineage(
    synthesis: Any,
    verification: dict[str, Any],
    report_payload: Any,
    dossier: Any,
    growth: Any,
    skeptic: Any,
    forecast: Any,
) -> dict[str, Any]:
    """Record input/output counts without changing the report contract."""
    prior = _synthesis_prior_artifacts(dossier, growth, skeptic, forecast)
    provider_claims = synthesis.get("claims", []) if isinstance(synthesis, dict) else []
    rendered_claims = report_payload.get("claims", []) if isinstance(report_payload, dict) else []
    return {
        "eligible_verified_input_count": len(prior.get("source_evidence_ids", [])),
        "provider_output_count": len(provider_claims) if isinstance(provider_claims, list) else 0,
        "post_validation_count": int(verification.get("verified_claim_count", 0) or 0),
        "rendered_count": len(rendered_claims) if isinstance(rendered_claims, list) else 0,
    }


def _skeptical_prior_artifacts(
    context: ResearchContext, dossier: Any, growth: Any
) -> dict[str, Any]:
    """Give the skeptical stage explicit canonical evidence and claim graph."""
    claim_graph = _collect_stage_claims(dossier, growth)
    channels = dossier.get("trusted_channels") if isinstance(dossier, dict) else None
    if isinstance(channels, dict):
        claim_graph.extend(
            dict(item)
            for channel in channels.values()
            if isinstance(channel, list)
            for item in channel
            if isinstance(item, dict)
        )
    seen: set[str] = set()
    unique_graph: list[dict[str, Any]] = []
    for item in claim_graph:
        marker = str(item.get("text") or item.get("conclusion") or item.get("argument") or item)
        if marker in seen:
            continue
        seen.add(marker)
        unique_graph.append(item)
    return {
        "research_dossier": dossier,
        "growth_opportunities": growth,
        "canonical_evidence": _lossless_synthesis_value(context.evidence),
        "thesis_claim_graph": unique_graph,
    }


def _sectioned_synthesis_context(
    projected: dict[str, Any], capability: ProviderContextCapability
) -> dict[str, Any]:
    """Return a complete context only when it fits one provider call.

    Oversized inputs must use the multi-call seam below; this function never
    wraps an oversized payload and sends it as if it fit.
    """
    encoded = json.dumps(
        projected, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) <= capability.max_input_bytes:
        return projected
    raise SynthesisContextLimitError(
        [
            {
                "name": str(name),
                "bytes": len(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    .encode("utf-8")
                ),
            }
            for name, value in projected.items()
            if name != "source_evidence_ids"
        ],
        capability.max_input_bytes,
    )


def _partition_synthesis_context(
    projected: dict[str, Any],
    capability: ProviderContextCapability,
    *,
    input_overhead_bytes: int = 0,
) -> list[dict[str, Any]]:
    """Make individually bounded, lossless named JSON-boundary payloads.

    A top-level section is only a presentation label.  Large nested analysis
    objects (notably ``base_analyses.claims``) are recursively partitioned by
    dict/list boundaries, so one large section cannot force an unsafe
    all-or-nothing request.  Atomic strings are never clipped.
    """
    limit = capability.max_input_bytes - max(0, input_overhead_bytes)
    if limit < 1:
        raise SynthesisContextLimitError(
            [{"name": "agent-envelope", "bytes": input_overhead_bytes}],
            capability.max_input_bytes,
        )
    if _synthesis_json_bytes(projected) <= limit:
        return [{"name": "complete", "content": projected}]

    parts: list[dict[str, Any]] = []
    for name, value in projected.items():
        if name == "source_evidence_ids":
            continue
        # Reserve space for the stable envelope and evidence references.  The
        # exact wrapped size is checked below as a final guard.
        envelope_bytes = _synthesis_json_bytes(
            {"name": str(name), "content": None, "source_evidence_ids": []}
        )
        # ``input_overhead_bytes`` accounts for the exact fixed agent
        # envelope; the remaining budget is available to this section's JSON.
        content_budget = limit - envelope_bytes
        if content_budget < 1:
            raise SynthesisContextLimitError(
                [{"name": str(name), "bytes": _synthesis_json_bytes(value)}],
                limit,
            )
        for chunk in _recursive_synthesis_chunks(value, content_budget):
            part = {
                "name": str(name),
                "content": chunk,
                # Only references present in this chunk travel with it.  The
                # complete union remains available to the final merge.
                "source_evidence_ids": _collect_evidence_ids(chunk),
            }
            size = _synthesis_json_bytes(part)
            if size > limit:
                raise SynthesisContextLimitError(
                    [{"name": str(name), "bytes": size}], limit
                )
            parts.append(part)
    return parts


def _synthesis_json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .encode("utf-8")
    )


def _recursive_synthesis_chunks(value: Any, budget: int) -> list[Any]:
    """Split a JSON value without splitting an atomic value."""
    if _synthesis_json_bytes(value) <= budget:
        return [value]
    if isinstance(value, dict):
        chunks: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for key, child in value.items():
            for child_chunk in _recursive_synthesis_chunks(child, budget):
                # Multiple chunks for one key are alternatives (for example
                # slices of a claims list), not values that may overwrite one
                # another in the same dict.
                if str(key) in current:
                    chunks.append(current)
                    current = {}
                candidate = dict(current)
                candidate[str(key)] = child_chunk
                if current and _synthesis_json_bytes(candidate) > budget:
                    chunks.append(current)
                    current = {str(key): child_chunk}
                elif not current and _synthesis_json_bytes(candidate) > budget:
                    raise SynthesisContextLimitError(
                        [{"name": str(key), "bytes": _synthesis_json_bytes(candidate)}],
                        budget,
                    )
                else:
                    current = candidate
        if current:
            chunks.append(current)
        return chunks or [{}]
    if isinstance(value, list):
        chunks: list[list[Any]] = []
        current: list[Any] = []
        for child in value:
            for child_chunk in _recursive_synthesis_chunks(child, budget):
                candidate = [*current, child_chunk]
                if current and _synthesis_json_bytes(candidate) > budget:
                    chunks.append(current)
                    current = [child_chunk]
                elif not current and _synthesis_json_bytes(candidate) > budget:
                    raise SynthesisContextLimitError(
                        [{"name": "list-item", "bytes": _synthesis_json_bytes(candidate)}],
                        budget,
                    )
                else:
                    current = candidate
        if current:
            chunks.append(current)
        return chunks or [[]]
    # A scalar larger than the input budget is indivisible by contract.
    raise SynthesisContextLimitError(
        [{"name": "atomic-value", "bytes": _synthesis_json_bytes(value)}],
        budget,
    )


def _collect_evidence_ids(value: Any) -> list[str]:
    found: list[str] = []
    seen_nodes: set[int] = set()
    pattern = re.compile(r"(?i)(?:fact|evidence|filing|artifact|run):[A-Za-z0-9_.:/-]+")
    def visit(item: Any) -> None:
        if isinstance(item, str):
            for match in pattern.findall(item):
                if match not in found:
                    found.append(match)
        elif isinstance(item, (dict, list)):
            marker = id(item)
            if marker in seen_nodes:
                return
            seen_nodes.add(marker)
        if isinstance(item, dict):
            for child in item.values(): visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
    visit(value)
    return found


def _lossless_synthesis_value(value: Any, _seen: set[int] | None = None) -> Any:
    """Copy JSON-like synthesis data without lossy size-based truncation."""
    seen = _seen if _seen is not None else set()
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if id(value) in seen:
            return "[cyclic synthesis value]"
        seen.add(id(value))
        return [_lossless_synthesis_value(item, seen) for item in value]
    if isinstance(value, tuple):
        return [_lossless_synthesis_value(item, seen) for item in value]
    if isinstance(value, dict):
        if id(value) in seen:
            return "[cyclic synthesis value]"
        seen.add(id(value))
        return {
            str(key): _lossless_synthesis_value(item, seen)
            for key, item in value.items()
            if not str(key).startswith("_") and key not in {"raw_response", "prompt"}
        }
    return value


def _synthesis_repair_input(
    synthesis: Any,
    verification: dict[str, Any],
    dossier: Any,
    growth: Any,
    skeptic: Any,
    forecast: Any,
) -> dict[str, Any]:
    missing = [
        key for key in sorted(_REQUIRED_SYNTHESIS_SECTIONS)
        if not isinstance(synthesis, dict) or key not in synthesis or synthesis.get(key) in (None, "", [])
    ]
    issues = " ".join(str(item) for item in verification.get("issues", []))
    if "claim" in issues.casefold() or verification.get("unsupported_fact_count", 0):
        missing.append("claims")
    fields = list(dict.fromkeys(missing or ["report_sections"]))
    stage_by_field = {
        "growth_opportunities": growth,
        "counterarguments": skeptic,
        "scenarios": forecast,
        "claims": {"dossier": dossier, "growth": growth, "counter_analysis": skeptic, "forecast": forecast},
    }
    context = {field: _lossless_synthesis_value(stage_by_field[field])
               for field in fields if field in stage_by_field}
    if not context:
        context = {"base_analyses": _lossless_synthesis_value(dossier)}
    result = {
        "repair_sections": fields,
        "section_context": context,
        "repair_schema": {"type": "object", "required": ["section_patches"], "properties": {"section_patches": {"type": "object", "only": fields}}},
        "repair_instruction": "Return only {section_patches: {...}} for the listed sections. Preserve evidence IDs and do not invent evidence.",
    }
    return result


def _section_patch_from_output(output: Any, fields: list[str]) -> dict[str, Any]:
    """Accept only requested patch fields from a repair response."""
    if not isinstance(output, dict):
        return {}
    source = output.get("section_patches") if isinstance(output.get("section_patches"), dict) else output
    patches = {
        key: source[key]
        for key in fields
        if isinstance(source, dict) and key in source and not str(key).startswith("_")
    }
    return _lossless_synthesis_value(patches)


def _response_diagnostics(payload: Any) -> dict[str, Any]:
    """Persist bounded provider diagnostics without response text or secrets."""

    if not isinstance(payload, dict):
        return {"finish_reason": None, "content_length": 0, "parse_error_class": "invalid_payload"}
    meta = payload.get("_response_meta")
    meta = meta if isinstance(meta, dict) else {}
    error = payload.get("_response_error")
    known_errors = {
        "empty_content",
        "invalid_json",
        "invalid_shape",
        "provider_error",
        "context_budget_exceeded",
    }
    parse_error_class = error if isinstance(error, str) and error in known_errors else (
        "unknown_parse_error"
        if error or payload.get("structured_output_valid") is False
        else None
    )
    try:
        content_length = int(meta.get("content_length") or 0)
    except (TypeError, ValueError):
        content_length = 0
    diagnostics = {
        "finish_reason": meta.get("finish_reason"),
        "content_length": content_length,
        "parse_error_class": parse_error_class,
    }
    if parse_error_class == "provider_error":
        diagnostics["provider_error_code"] = str(payload.get("_provider_error_code") or "MODEL_ERROR")
        diagnostics["provider_retryable"] = bool(payload.get("_provider_retryable"))
    return diagnostics


class ResearchWorkflow:
    def __init__(
        self,
        storage: Storage,
        research_pack: ResearchPack,
        provider: ModelProvider | None,
        model_config: ModelConfig,
        cancel_check: CancelCheck | None = None,
        report_language: str = "zh-CN",
        ui_language: str = "zh-CN",
        parallel_agents: bool = True,
        agent_progress: AgentProgressCallback | None = None,
    ):
        self.storage = storage
        self.pack = research_pack
        self.provider = provider
        self.model_config = model_config
        self.cancel_check = cancel_check or (lambda: False)
        self.report_language = normalize_language(report_language)
        self.ui_language = normalize_language(ui_language)
        self.parallel_agents = parallel_agents
        self.agent_progress = agent_progress or (lambda _agent_id, _state: None)

    def _report_text(
        self, chinese: str, english: str, traditional: str | None = None
    ) -> str:
        if self.report_language == EN:
            return english
        if self.report_language == ZH_HANT:
            prefix = chinese[: len(chinese) - len(chinese.lstrip("# >-"))]
            return prefix + (
                traditional
                if traditional is not None
                else UI_HANT.get(chinese[len(prefix):], chinese[len(prefix):])
            )
        return chinese

    def _progress_text(self, chinese: str, **params: object) -> str:
        return translate(chinese, self.ui_language, **params)

    def _run_synthesis_with_budget(
        self,
        context: ResearchContext,
        dossier: dict[str, Any],
        growth: dict[str, Any],
        skeptic: dict[str, Any],
        forecast: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit the complete canonical synthesis context exactly once.

        Local byte/token estimates are diagnostics only.  Provider tokenizers,
        server-side prompt framing and context limits differ, so a conservative
        local estimate must never reject a request the selected provider can
        accept.  Capacity is classified from the provider's explicit response
        or an incomplete length-truncated response after this one attempt.
        """
        projected = _synthesis_prior_artifacts(dossier, growth, skeptic, forecast)
        context_json = context.compact_json()
        result = self._run_agent(
            "research-synthesizer",
            "prompts/research-synthesizer.md",
            context_json,
            projected,
            enforce_local_budget=False,
        )
        meta = result.get("_response_meta") if isinstance(result, dict) else None
        finish_reason = str(meta.get("finish_reason") or "").casefold() if isinstance(meta, dict) else ""
        if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
            missing_sections = {
                key for key in _REQUIRED_SYNTHESIS_SECTIONS
                if result.get(key) in (None, "", [])
            }
            if missing_sections:
                raise SynthesisContextLimitError(
                    [{"name": "complete", "bytes": len(context_json.encode("utf-8"))}],
                    0,
                    required_bytes=len(context_json.encode("utf-8")),
                    counting_mode="provider_finish_reason",
                )
        return result

    def _bounded_synthesis_merge(
        self,
        context: ResearchContext,
        entries: list[dict[str, Any]],
        source_evidence_ids: list[str],
        capability: ProviderContextCapability,
    ) -> dict[str, Any]:
        """Recursively merge section results without an oversized call.

        Providers may return a small result for each section while the list of
        results itself still exceeds the input window.  In that case merge
        bounded groups, then merge those merge-results again until the final
        envelope fits.  No group is silently dropped or clipped.
        """
        current = list(entries)
        while True:
            payload = {
                "context_format": "lossless-merge-v1",
                "sections": current,
                "source_evidence_ids": source_evidence_ids,
            }
            if _synthesis_json_bytes(payload) <= capability.max_input_bytes:
                return payload
            if len(current) <= 1:
                raise SynthesisContextLimitError(
                    [{"name": "merge", "bytes": _synthesis_json_bytes(payload)}],
                    capability.max_input_bytes,
                )

            batches: list[list[dict[str, Any]]] = []
            batch: list[dict[str, Any]] = []
            for entry in current:
                candidate = [*batch, entry]
                candidate_payload = {
                    "context_format": "lossless-merge-v1",
                    "sections": candidate,
                    "source_evidence_ids": source_evidence_ids,
                }
                if batch and _synthesis_json_bytes(candidate_payload) > capability.max_input_bytes:
                    batches.append(batch)
                    batch = [entry]
                elif not batch and _synthesis_json_bytes(candidate_payload) > capability.max_input_bytes:
                    raise SynthesisContextLimitError(
                        [{"name": str(entry.get("name", "merge-item")), "bytes": _synthesis_json_bytes(candidate_payload)}],
                        capability.max_input_bytes,
                    )
                else:
                    batch = candidate
            if batch:
                batches.append(batch)
            merged: list[dict[str, Any]] = []
            for index, group in enumerate(batches):
                group_payload = {
                    "context_format": "lossless-merge-v1",
                    "sections": group,
                    "source_evidence_ids": source_evidence_ids,
                }
                merged_output = self._run_agent(
                    "research-synthesizer-merge",
                    "prompts/research-synthesizer.md",
                    context.compact_json(),
                    group_payload,
                )
                merged.append({"name": f"merged-{index + 1}", "output": merged_output})
            if (
                len(merged) == len(current)
                and _synthesis_json_bytes(
                    {
                        "context_format": "lossless-merge-v1",
                        "sections": merged,
                        "source_evidence_ids": source_evidence_ids,
                    }
                )
                >= _synthesis_json_bytes(payload)
            ):
                raise SynthesisContextLimitError(
                    [{"name": "merge", "bytes": _synthesis_json_bytes(payload)}],
                    capability.max_input_bytes,
                )
            current = merged

    def _build_staged_fallback(
        self,
        stage_results: dict[str, dict[str, Any]],
        growth: dict[str, Any],
        skeptic: dict[str, Any],
        forecast: dict[str, Any],
        financial_metrics: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        business_raw = stage_results.get("business-analyst", {})
        financial_raw = stage_results.get("financial-analyst", {})
        accounting_raw = stage_results.get("accounting-risk-analyst", {})
        business = _presentation_stage_value(business_raw)
        claims = _collect_stage_claims(
            *stage_results.values(), growth, skeptic, forecast
        )
        possible_moats = (
            business.get("possible_moats", []) if isinstance(business, dict) else []
        )
        unknowns = _collect_strings(
            [business_raw, skeptic], "unknowns", "missing_evidence", "unresolved_questions"
        )
        invalidation = _collect_strings(
            skeptic, "unsupported_assumptions", "strongest_counterarguments"
        )
        leading_indicators = _collect_strings(growth, "leading_indicators")
        metric_rows = [
            metric
            for metric in (financial_metrics or [])
            if isinstance(metric, dict) and metric.get("year") is not None
        ]
        latest_metric = max(
            metric_rows,
            key=lambda metric: int(metric.get("year", 0)),
            default=None,
        )
        metric_values: dict[str, Any] = {}
        if latest_metric is not None:
            metric_values["year"] = latest_metric.get("year")
            for key in ("assets", "liabilities", "equity", "total_equity"):
                value = latest_metric.get(key)
                if value is not None:
                    metric_values[key] = value
        if len(metric_values) > 1:
            balance_sheet: Any = {
                "summary": self._report_text(
                    "以下资产负债表摘要仅使用通过校验的确定性财务数据。",
                    "This balance-sheet summary uses only deterministic financial data that passed validation.",
                    "以下資產負債表摘要僅使用通過驗證的確定性財務數據。",
                ),
                **metric_values,
            }
        else:
            balance_sheet = self._report_text(
                "缺少已验证的资产负债表数据，暂不能判断资产、负债与权益结构。",
                "Verified balance-sheet data is unavailable, so the asset, liability, and equity structure cannot be assessed.",
                "缺少已驗證的資產負債表數據，暫不能判斷資產、負債與權益結構。",
            )
        return {
            "executive_summary": self._report_text(
                "最终综合未完整生成。以下内容由已完成的研究阶段确定性整理。",
                "Final synthesis was incomplete. The content below deterministically preserves completed research stages.",
            ),
            "business_model": business,
            "financial_quality": {
                "financial_analysis": _presentation_stage_value(financial_raw),
                "accounting_risk": _presentation_stage_value(accounting_raw),
            },
            "balance_sheet": balance_sheet,
            "competitive_position": possible_moats
            or self._report_text(
                "竞争地位尚待最终综合；请结合商业模式与信息缺口复核。",
                "Competitive position awaits final synthesis; review the business model and information gaps.",
            ),
            "growth_opportunities": growth.get("opportunities", growth),
            "counterarguments": skeptic.get("strongest_counterarguments", skeptic),
            "scenarios": forecast.get("scenarios", forecast),
            "thesis": self._report_text(
                "尚未形成最终长期结论；可重试最终综合，阶段性结论已保留。",
                "No final long-term thesis was formed; retry synthesis while preserving stage conclusions.",
            ),
            "invalidation_conditions": invalidation
            or self._report_text("尚待最终综合。", "Awaiting final synthesis."),
            "leading_indicators": leading_indicators
            or self._report_text("尚待最终综合。", "Awaiting final synthesis."),
            "unresolved_questions": unknowns
            or self._report_text(
                "最终综合输出需要重新生成。",
                "The final synthesis output needs to be regenerated.",
            ),
            "claims": claims,
        }

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise ResearchCancelled()

    def _run_declarative_ot_workflow(
        self,
        run: ResearchRun,
        context: ResearchContext,
        evidence: list[dict[str, Any]],
        notify: ProgressCallback,
    ) -> ResearchRun:
        """Execute a non-official .ot dependency graph without hidden fixed agents."""
        raw_steps = self.pack.workflow.get("steps", [])
        if not isinstance(raw_steps, list) or not raw_steps:
            raise RuntimeError("OT workflow has no executable steps")
        steps: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                raise RuntimeError("OT workflow step is invalid")
            step_id = str(raw_step.get("id", "")).strip()
            prompt_path = str(raw_step.get("prompt", "")).strip()
            role = str(raw_step.get("role", "")).strip()
            dependencies = raw_step.get("depends_on", [])
            if (
                not step_id
                or step_id in steps
                or not prompt_path
                or not role
                or not isinstance(dependencies, list)
                or any(not isinstance(item, str) for item in dependencies)
            ):
                raise RuntimeError("OT workflow step contract is invalid")
            steps[step_id] = {
                **raw_step,
                "id": step_id,
                "prompt": prompt_path,
                "role": role,
                "depends_on": list(dependencies),
            }
            order.append(step_id)
        if len(steps) > 64:
            raise RuntimeError("OT workflow exceeds the executable step limit")

        run.workflow_id = f"ot:{self.pack.pack_id}"
        run.research_configuration["ot_workflow"] = {
            "schema": self.pack.workflow.get("schema", ""),
            "step_ids": order,
            "roles": [steps[step_id]["role"] for step_id in order],
            "settings": self.pack.workflow.get("settings", {}),
        }
        self.storage.save_run(run)

        available = {item["evidence_id"] for item in evidence}
        evidence_records = {item["evidence_id"]: item for item in evidence}
        remaining = set(order)
        results: dict[str, dict[str, Any]] = {}
        verifications: dict[str, dict[str, Any]] = {}
        total = len(order)
        completed = 0

        def execute_step(step_id: str) -> tuple[str, dict[str, Any]]:
            step = steps[step_id]
            self._check_cancelled()
            self._set_agent_state(step_id, "running")
            prior = {
                dependency: results[dependency]
                for dependency in step["depends_on"]
            }
            output = self._run_agent(
                step_id,
                step["prompt"],
                context.compact_json(),
                {
                    "workflow_role": step["role"],
                    "workflow_settings": self.pack.workflow.get("settings", {}),
                    "dependency_outputs": prior,
                },
            )
            return step_id, output

        while remaining:
            ready = [
                step_id
                for step_id in order
                if step_id in remaining
                and all(dependency in results for dependency in steps[step_id]["depends_on"])
            ]
            if not ready:
                raise RuntimeError("OT workflow dependencies cannot be resolved")
            batch = ready[:2] if self.parallel_agents else ready[:1]
            notify(
                self._progress_text(
                    "正在执行 .ot 工作流：{completed}/{total}",
                    completed=completed,
                    total=total,
                ),
                20 + round(completed * 68 / total),
            )
            for step_id in batch:
                self._set_agent_state(step_id, "queued")
            if len(batch) > 1:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {executor.submit(execute_step, step_id): step_id for step_id in batch}
                    completed_batch = []
                    for future in futures:
                        try:
                            completed_batch.append(future.result())
                        except ResearchCancelled:
                            self._set_agent_state(futures[future], "cancelled")
                            raise
                        except Exception:
                            self._set_agent_state(futures[future], "failed")
                            raise
            else:
                completed_batch = [execute_step(batch[0])]

            for step_id, output in completed_batch:
                verification = verify_agent_output(
                    output, available, self.report_language, evidence_records
                )
                trusted_output = _trusted_stage_result(output, verification)
                results[step_id] = trusted_output
                verifications[step_id] = verification
                remaining.remove(step_id)
                completed += 1
                self._save(
                    run,
                    "ot-agent-analysis",
                    str(steps[step_id]["role"]),
                    {
                        "step_id": step_id,
                        "role": steps[step_id]["role"],
                        "depends_on": steps[step_id]["depends_on"],
                        "output_schema": steps[step_id].get("output_schema", ""),
                        "result": output,
                        "verification": verification,
                    },
                    agent_id=step_id,
                )
                self._set_agent_state(
                    step_id,
                    "completed_verified"
                    if verification["passed"] and "_verification_state" not in trusted_output
                    else "completed_partial"
                    if trusted_output.get("_verification_state") == "completed_partial"
                    else "failed_verification",
                )

        sink_ids = [
            step_id
            for step_id in order
            if not any(step_id in steps[other]["depends_on"] for other in order)
        ]
        preferred = next(
            (
                step_id
                for step_id in reversed(order)
                if "synth" in str(steps[step_id]["role"]).lower()
            ),
            sink_ids[-1] if sink_ids else order[-1],
        )
        final_output = results[preferred]
        claims = _collect_stage_claims(*results.values())
        aggregate = verify_agent_output(
            {"claims": claims}, available, self.report_language, evidence_records
        )
        issues = list(aggregate["issues"])
        for step_id in order:
            for issue in verifications[step_id]["issues"]:
                labelled = f"{step_id}: {issue}"
                if labelled not in issues:
                    issues.append(labelled)
        aggregate["issues"] = issues
        aggregate["passed"] = not issues and all(
            item["passed"] for item in verifications.values()
        )

        if _REQUIRED_SYNTHESIS_SECTIONS.issubset(final_output):
            report_payload = final_output
            synthesis_verification = validate_research_synthesis(
                final_output, available, self.report_language, evidence_records
            )
            synthesis_verification["issues"] = list(dict.fromkeys(
                [*aggregate["issues"], *synthesis_verification["issues"]]
            ))
            synthesis_verification["passed"] = (
                aggregate["passed"] and not synthesis_verification["issues"]
            )
            verification = synthesis_verification
        else:
            visible_results = {
                step_id: {
                    "role": steps[step_id]["role"],
                    "output": _presentation_stage_value(results[step_id], remove_claims=False),
                }
                for step_id in order
            }
            report_payload = {
                "narrative": "\n\n".join(
                    f"{step_id} · {item['role']}\n"
                    + json.dumps(item["output"], ensure_ascii=False, indent=2)
                    for step_id, item in visible_results.items()
                ),
                "workflow_results": visible_results,
                "claims": claims,
            }
            verification = aggregate

        self._save(
            run,
            "research-report",
            self._report_text(".ot 工作流研究报告", ".ot Workflow Research Report"),
            {
                "mode": "ot-workflow",
                "report": report_payload,
                "verification": verification,
                "retryable": False,
                "workflow": {
                    "pack_id": self.pack.pack_id,
                    "pack_version": self.pack.version,
                    "content_identity": self.pack.content_hash,
                    "selected_output_step": preferred,
                    "steps": order,
                },
            },
            agent_id=preferred,
        )
        if report_payload.get("thesis"):
            thesis_version = self.storage.save_thesis_version(
                run.company.cik,
                {
                    "thesis": report_payload.get("thesis"),
                    "claims": report_payload.get("claims", claims),
                    "invalidation_conditions": report_payload.get("invalidation_conditions", []),
                    "leading_indicators": report_payload.get("leading_indicators", []),
                    "unresolved_questions": report_payload.get("unresolved_questions", []),
                },
                run_id=run.run_id,
                created_by=self.model_config.public_id,
                created_at=utc_now_iso(),
            )
            self._save(
                run,
                "thesis-snapshot",
                self._report_text(
                    f"投资逻辑 v{thesis_version['version']}",
                    f"Investment Thesis v{thesis_version['version']}",
                ),
                thesis_version,
                agent_id="thesis-versioning",
            )
        run.status = RunStatus.COMPLETED if verification["passed"] else RunStatus.PARTIAL
        run.completed_at = utc_now_iso()
        self.storage.save_run(run)
        notify(self._progress_text(".ot 工作流研究完成"), 100)
        return run
    def run(
        self,
        company: Company,
        facts: list[dict[str, Any]] | FinancialProfile,
        filing_evidence: list[dict[str, Any]] | None = None,
        valuation_inputs: dict[str, float] | None = None,
        market_snapshot: dict[str, Any] | None = None,
        progress: ProgressCallback | None = None,
        reproducibility: dict[str, Any] | None = None,
    ) -> ResearchRun:
        notify = progress or (lambda _message, _percent: None)
        reproducibility = reproducibility or {}
        profile = facts if isinstance(facts, FinancialProfile) else None
        if profile is not None:
            facts = list(profile.fact_dicts)
        run = ResearchRun(
            run_id=uuid.uuid4().hex,
            company=company,
            workflow_id="complete-fundamental-research",
            research_pack_id=self.pack.pack_id,
            research_pack_version=self.pack.version,
            provider_id=self.model_config.provider,
            model_id=self.model_config.model,
            data_as_of=utc_now_iso(),
            status=RunStatus.RUNNING,
            report_language=self.report_language,
            market_snapshot=market_snapshot,
            model_configuration={
                "configured_model_id": self.model_config.configured_model_id,
                "configuration_version": self.model_config.configuration_version,
                "role": self.model_config.role,
            },
            research_configuration={
                **dict(reproducibility.get("research_configuration", {})),
                "report_language": self.report_language,
                "parallel_agents": self.parallel_agents,
                "research_pack_id": self.pack.pack_id,
                "research_pack_version": self.pack.version,
                "research_pack_content_identity": self.pack.content_hash,
            },
            data_snapshot=dict(reproducibility.get("data_snapshot", {})),
        )
        self.storage.save_run(run)
        try:
            self._check_cancelled()
            metrics = calculate_metrics(facts)
            interim_metrics = calculate_interim_metrics(facts)
            evidence = build_fact_evidence(facts)
            evidence.extend(filing_evidence or [])
            valuation = None
            snapshot_currency = str((market_snapshot or {}).get("valuation_currency") or (market_snapshot or {}).get("currency", ""))
            snapshot_status = str((market_snapshot or {}).get("status", "VERIFIED")).upper()
            snapshot_value = (market_snapshot or {}).get("equity_market_value")
            requested_value = (
                snapshot_value
                if snapshot_value is not None
                else (valuation_inputs or {}).get("equity_market_value", (valuation_inputs or {}).get("market_cap"))
            )
            valuation_money = _valuation_money(
                requested_value,
                currency=snapshot_currency or company.reporting_currency,
                source_id=str((market_snapshot or {}).get("source_id") or (market_snapshot or {}).get("source", "")),
                as_of=str((market_snapshot or {}).get("as_of", "")),
            )
            if company.industry_support == "financial_beta" and valuation_inputs:
                valuation = {
                    "status": "not_applicable",
                    "reason": self._report_text(
                        "金融机构 Beta 暂不使用标准自由现金流反向 DCF。",
                        "Financials Beta does not apply the standard free-cash-flow reverse DCF.",
                    ),
                    "currency": company.reporting_currency,
                }
            elif valuation_inputs and snapshot_status in {"CONFLICT", "STALE", "UNAVAILABLE"}:
                valuation = {
                    "status": "market_snapshot_unavailable",
                    "reason": self._report_text(
                        "行情快照存在冲突、陈旧或不可用状态；未执行权益反向 DCF。",
                        "Reverse DCF was skipped because the market snapshot is conflicted, stale, or unavailable.",
                    ),
                    "error_code": str((market_snapshot or {}).get("error_code") or "MARKET_SNAPSHOT_UNAVAILABLE"),
                    "currency": snapshot_currency or company.reporting_currency,
                }
            elif (
                valuation_inputs
                and snapshot_currency
                and snapshot_currency != company.reporting_currency
            ):
                valuation = {
                    "status": "currency_mismatch",
                    "reason": self._report_text(
                        "手动市值币种与财报币种不同；未提供汇率，因此不执行反向 DCF。",
                        "The manual market-cap currency differs from the reporting currency; reverse DCF was skipped because no FX rate was supplied.",
                    ),
                    "currency": snapshot_currency,
                    "reporting_currency": company.reporting_currency,
                }
            elif valuation_inputs and _valuation_money_is_positive(
                requested_value, currency=snapshot_currency or company.reporting_currency
            ):
                valuation = reverse_dcf_analysis(
                    metrics,
                    valuation_money,
                    valuation_inputs.get("discount_rate", 0.10),
                    valuation_inputs.get("terminal_growth", 0.03),
                    int(valuation_inputs.get("horizon_years", 5)),
                    market_as_of=str((market_snapshot or {}).get("as_of", "")),
                    currency=company.reporting_currency,
                    require_typed=True,
                )
                valuation["currency"] = company.reporting_currency
                valuation["market_snapshot"] = dict(market_snapshot or {})
            context = ResearchContext(
                company,
                facts,
                metrics,
                interim_metrics,
                evidence,
                valuation,
                market_snapshot,
            )
            self._check_cancelled()
            notify(self._progress_text("已完成确定性财务计算"), 15)
            summary = deterministic_summary(
                company.name,
                metrics,
                self.report_language,
                company.reporting_currency,
            )
            self._save(
                run,
                "deterministic-financial-summary",
                self._report_text(
                    "确定性财务概览", "Deterministic Financial Overview"
                ),
                {
                    "markdown": summary,
                    "metrics": metrics,
                    "interim_metrics": interim_metrics,
                    "evidence": evidence,
                    "currency": company.reporting_currency,
                    "accounting_standard": company.accounting_standard,
                    "industry_support": company.industry_support,
                    "market_snapshot": market_snapshot,
                    "financial_quality": {
                        "status": profile.status.value,
                        "rejected_periods": list(profile.rejected_periods),
                        "period_continuity": list(profile.period_continuity),
                        "period_coverage": {
                            **dict(profile.period_coverage),
                            "research_as_of": run.data_as_of,
                        },
                    } if profile is not None else None,
                },
                agent_id="calculation-engine",
            )
            if valuation is not None:
                self._save(
                    run,
                    "deterministic-valuation",
                    self._report_text(
                        "反向 DCF 隐含预期", "Reverse DCF Implied Expectations"
                    ),
                    valuation,
                    agent_id="valuation-engine",
                )
            if self.provider is None:
                self._save(
                    run,
                    "research-report",
                    self._report_text("基础研究报告", "Basic Research Report"),
                    {
                        "mode": "deterministic-only",
                        "summary": summary,
                        "notice": self._report_text(
                            "未配置模型，因此没有生成定性研究、增长机会和长期情景。",
                            "No model was configured, so qualitative research, "
                            "growth opportunities, and long-term scenarios were "
                            "not generated.",
                        ),
                    },
                    agent_id="deterministic-fallback",
                )
                run.status = RunStatus.PARTIAL
                run.completed_at = utc_now_iso()
                self.storage.save_run(run)
                notify(
                    self._progress_text(
                        "基础财务分析完成；配置模型后可运行完整研究"
                    ),
                    100,
                )
                return run

            if self.pack.pack_id != "official.long-term-fundamentals":
                return self._run_declarative_ot_workflow(run, context, evidence, notify)

            available = {item["evidence_id"] for item in evidence}
            evidence_records = {item["evidence_id"]: item for item in evidence}
            stage_one = {
                "financial-analyst": "prompts/financial-analyst.md",
                "business-analyst": "prompts/business-analyst.md",
                "accounting-risk-analyst": "prompts/accounting-risk-analyst.md",
            }
            stage_results: dict[str, dict[str, Any]] = {}
            stage_verifications: dict[str, dict[str, Any]] = {}
            notify(
                self._progress_text(
                    "正在并行运行财务、商业与会计风险 Agent（0/3）"
                ),
                25,
            )
            notify(
                (
                    "Running base agents in parallel (0/3)"
                    if self.ui_language == EN and self.parallel_agents
                    else "Running base agents sequentially (0/3)"
                    if self.ui_language == EN
                    else "正在並行執行基礎 Agent（0/3）"
                    if self.ui_language == ZH_HANT and self.parallel_agents
                    else "正在依序執行基礎 Agent（0/3）"
                    if self.ui_language == ZH_HANT
                    else "正在并行运行基础 Agent（0/3）"
                    if self.parallel_agents
                    else "正在按顺序运行基础 Agent（0/3）"
                ),
                25,
            )
            for agent_id in stage_one:
                self._set_agent_state(agent_id, "queued")

            completed_agents = 0

            def record_stage_result(agent_id: str, result: dict[str, Any]) -> None:
                nonlocal completed_agents
                self._check_cancelled()
                verification = verify_agent_output(
                    result, available, self.report_language, evidence_records
                )
                stage_verifications[agent_id] = verification
                self._save(
                    run,
                    "agent-analysis",
                    agent_id,
                    {"result": result, "verification": verification},
                    agent_id=agent_id,
                )
                trusted_result = _trusted_stage_result(result, verification)
                stage_results[agent_id] = trusted_result
                # The trusted projection, rather than the provider's empty
                # or absent claims list, owns the lifecycle state.  A
                # no-claims arbitrary narrative is therefore failed even
                # when the structural verifier has no citation to contradict.
                stage_state = trusted_result.get("_verification_state")
                if stage_state not in {
                    "completed_verified",
                    "completed_partial",
                    "failed_verification",
                }:
                    stage_state = (
                        "completed_verified"
                        if verification["passed"]
                        else "completed_partial"
                        if trusted_result.get("_verified_claim_count", 0)
                        else "failed_verification"
                    )
                self._set_agent_state(agent_id, stage_state)
                completed_agents += 1
                notify(
                    self._progress_text(
                        "基础分析 Agent 已完成 {completed}/3：{agent_id}",
                        completed=completed_agents,
                        agent_id=agent_id,
                    ),
                    25 + completed_agents * 7,
                )

            if self.parallel_agents:
                executor = ThreadPoolExecutor(max_workers=2)
                futures: dict[Future[dict[str, Any]], str] = {}
                failures: dict[str, BaseException] = {}
                try:
                    queued = iter(stage_one.items())

                    def submit_next() -> Future[dict[str, Any]] | None:
                        try:
                            agent_id, prompt_path = next(queued)
                        except StopIteration:
                            return None
                        self._set_agent_state(agent_id, "running")
                        future = executor.submit(
                            self._run_agent,
                            agent_id,
                            prompt_path,
                            context.compact_json(),
                            {},
                        )
                        futures[future] = agent_id
                        return future

                    pending = {
                        future
                        for future in (submit_next(), submit_next())
                        if future is not None
                    }
                    while pending:
                        self._check_cancelled()
                        done, pending = wait(
                            pending, timeout=0.05, return_when=FIRST_COMPLETED
                        )
                        for future in done:
                            agent_id = futures[future]
                            try:
                                result = future.result()
                            except ResearchCancelled:
                                self._set_agent_state(agent_id, "cancelled")
                                raise
                            except Exception as exc:
                                self._set_agent_state(agent_id, "failed")
                                failures[agent_id] = exc
                            else:
                                record_stage_result(agent_id, result)
                            next_future = submit_next()
                            if next_future is not None:
                                pending.add(next_future)
                finally:
                    executor.shutdown(
                        wait=not self.cancel_check(),
                        cancel_futures=True,
                    )

                for agent_id, error in failures.items():
                    if not (
                        isinstance(error, ProviderError) and error.retryable
                    ):
                        raise error
                    self._check_cancelled()
                    self._set_agent_state(agent_id, "retrying")
                    notify(
                        self._progress_text(
                            "{agent_id} 暂时失败，正在单独重试",
                            agent_id=agent_id,
                        ),
                        25 + completed_agents * 7,
                    )
                    try:
                        result = self._run_agent(
                            agent_id,
                            stage_one[agent_id],
                            context.compact_json(),
                            {},
                        )
                    except ResearchCancelled:
                        self._set_agent_state(agent_id, "cancelled")
                        raise
                    except Exception:
                        self._set_agent_state(agent_id, "failed")
                        raise
                    record_stage_result(agent_id, result)
            else:
                for agent_id, prompt_path in stage_one.items():
                    self._check_cancelled()
                    self._set_agent_state(agent_id, "running")
                    try:
                        result = self._run_agent(
                            agent_id, prompt_path, context.compact_json(), {}
                        )
                    except ResearchCancelled:
                        self._set_agent_state(agent_id, "cancelled")
                        raise
                    except Exception:
                        self._set_agent_state(agent_id, "failed")
                        raise
                    record_stage_result(agent_id, result)

            dossier = {
                "company": company.to_dict(),
                "metrics": metrics[:5],
                "analyses": stage_results,
                "verified_analyses": stage_results,
                "unverified_supplement": [
                    {
                        "agent_id": agent_id,
                        "state": value.get("_verification_state", "completed_verified"),
                        "claim_verifications": stage_verifications.get(agent_id, {}).get("claim_verifications", []),
                        "issues": stage_verifications.get(agent_id, {}).get("issues", []),
                    }
                    for agent_id, value in stage_results.items()
                    if isinstance(value, dict)
                    and value.get("_verification_state") not in (None, "completed_verified")
                ],
            }
            self._save(
                run,
                "verified-research-dossier",
                self._report_text(
                    "经过验证的研究档案", "Verified Research Dossier"
                ),
                dossier,
                agent_id="evidence-verifier",
            )
            notify(self._progress_text("基础研究档案完成"), 50)

            notify(self._progress_text("正在研究公司与行业增长机会"), 52)
            growth_raw = self._run_agent(
                "growth-opportunity-analyst",
                "prompts/growth-opportunity-analyst.md",
                context.compact_json(),
                {"research_dossier": dossier},
            )
            growth_validation = normalize_growth_output(
                growth_raw,
                available,
                self.report_language,
            )
            growth, growth_gate = _gate_stage_output(
                growth_validation.output,
                available,
                evidence_records,
                self.report_language,
            )
            if growth_raw.get('_response_error') != 'insufficient_material' and (
                not growth_validation.passed
                or not growth_gate["passed"]
                or not growth.get("opportunities")
            ):
                notify(self._progress_text("增长机会输出不完整，正在进行一次修复"), 58)
                growth_raw = self._run_agent(
                    "growth-opportunity-analyst",
                    "prompts/growth-opportunity-analyst.md",
                    context.compact_json(),
                    {
                        "research_dossier": dossier,
                        "repair_instruction": (
                            "Return one complete growth-opportunity JSON object with a non-empty "
                            "opportunities array. Repair only this stage and do not invent evidence."
                        ),
                    },
                )
                growth_validation = normalize_growth_output(
                    growth_raw,
                    available,
                    self.report_language,
                )
                growth, growth_gate = _gate_stage_output(
                    growth_validation.output,
                    available,
                    evidence_records,
                    self.report_language,
                )
            self._save(
                run,
                "growth-opportunities",
                self._report_text("增长机会", "Growth Opportunities"),
                {**growth, "_audit": {"result": growth_raw, "verification": growth_gate}},
                agent_id="growth-opportunity-analyst",
            )
            notify(self._progress_text("增长机会研究完成"), 65)

            notify(self._progress_text("正在进行反方审查与压力测试"), 67)
            skeptic_raw = self._run_agent(
                "skeptical-analyst",
                "prompts/skeptical-analyst.md",
                context.compact_json(),
                _skeptical_prior_artifacts(context, dossier, growth),
            )
            skeptic, skeptic_gate = _gate_stage_output(
                skeptic_raw, available, evidence_records, self.report_language
            )
            self._save(
                run,
                "counter-analysis",
                self._report_text("反方审查", "Counter-analysis"),
                {**skeptic, "_audit": {"result": skeptic_raw, "verification": skeptic_gate}},
                agent_id="skeptical-analyst",
            )
            notify(self._progress_text("反方审查完成"), 75)

            notify(self._progress_text("正在生成长期经营情景"), 77)
            forecast_raw = self._run_agent(
                "forecast-analyst",
                "prompts/forecast-analyst.md",
                context.compact_json(),
                {
                    "research_dossier": dossier,
                    "growth_opportunities": growth,
                    "counter_analysis": skeptic,
                },
            )
            forecast, forecast_gate = _gate_stage_output(
                forecast_raw, available, evidence_records, self.report_language
            )
            self._save(
                run,
                "forecast-scenarios",
                self._report_text("长期经营情景", "Long-term Operating Scenarios"),
                {**forecast, "_audit": {"result": forecast_raw, "verification": forecast_gate}},
                agent_id="forecast-analyst",
            )
            notify(self._progress_text("长期情景完成"), 88)

            notify(self._progress_text("正在合成最终长期研究报告"), 90)
            try:
                synthesis = self._run_synthesis_with_budget(
                    context, dossier, growth, skeptic, forecast
                )
            except SynthesisContextLimitError as exc:
                synthesis = {
                    "claims": [],
                    "_response_error": "context_budget_exceeded",
                    "_context_sections": exc.sections,
                    "_context_required_bytes": exc.required_bytes,
                    "_context_available_bytes": exc.available_bytes,
                    "_context_counting_mode": exc.counting_mode,
                }
            except ProviderError as exc:
                # A final-only provider failure is recoverable from the five
                # persisted prerequisite stages. Never issue an implicit
                # repair/model retry here because that would consume tokens
                # without a user action.
                synthesis = {
                    "claims": [],
                    "_response_error": (
                        "context_budget_exceeded"
                        if exc.code == "MODEL_CONTEXT_CAPACITY"
                        else "provider_error"
                    ),
                    "_provider_error_code": exc.code,
                    "_provider_retryable": exc.retryable,
                    "_context_counting_mode": "provider_error_code",
                }
            context_budget_exceeded = (
                synthesis.get("_response_error") == "context_budget_exceeded"
            )
            verification = validate_research_synthesis(
                synthesis, available, self.report_language, evidence_records
            )
            report_payload: dict[str, Any] = synthesis
            report_mode = "synthesized"
            if synthesis.get('_response_error') == 'insufficient_material':
                report_mode = 'staged-fallback'
                report_payload = self._build_staged_fallback(
                    stage_results, growth, skeptic, forecast, context.metrics
                )
                report_payload['unresolved_questions'] = list(synthesis.get('unresolved_questions', []))
                report_payload['_material_adequacy'] = synthesis.get('_material_adequacy', {})
            if context_budget_exceeded:
                report_mode = "synthesis-incomplete"
                report_payload = {
                    "research_complete": False,
                    "claims": [],
                    "cross_section_synthesis_status": "not_completed_context_capacity",
                    "context_budget": {
                        "required_bytes": synthesis.get("_context_required_bytes"),
                        "available_bytes": synthesis.get("_context_available_bytes"),
                        "counting_mode": synthesis.get("_context_counting_mode"),
                    },
                }
            if synthesis.get("_response_error") == "provider_error":
                report_mode = "synthesis-incomplete"
                report_payload = {
                    "research_complete": False,
                    "claims": [],
                    "cross_section_synthesis_status": "not_completed_provider_error",
                    "provider_error_code": synthesis.get("_provider_error_code"),
                    "provider_retryable": synthesis.get("_provider_retryable"),
                }
            if not verification["passed"] and synthesis.get("_response_error") not in {"context_budget_exceeded", "insufficient_material", "provider_error"}:
                # Do not spend a hidden second model call repairing malformed
                # synthesis.  Preserve every prerequisite artifact and expose
                # an explicit retryable state for a user-selected next attempt.
                report_mode = "synthesis-incomplete"
                report_payload = {
                    "research_complete": False,
                    "claims": [],
                    "cross_section_synthesis_status": "not_completed_invalid_output",
                }
            diagnostics = _response_diagnostics(synthesis)
            if synthesis.get("_response_error") == "context_budget_exceeded":
                diagnostics.update(
                    {
                        "required_bytes": synthesis.get("_context_required_bytes"),
                        "available_bytes": synthesis.get("_context_available_bytes"),
                        "counting_mode": synthesis.get("_context_counting_mode"),
                    }
                )
            synthesis_lineage = _synthesis_lineage(
                synthesis, verification, report_payload, dossier, growth, skeptic, forecast
            )
            self._save(
                run,
                "research-report",
                self._report_text(
                    "完整长期研究报告", "Complete Long-term Research Report"
                ),
                {
                    "mode": report_mode,
                    "report": report_payload,
                    "verification": verification,
                    "retryable": not verification["passed"],
                    "diagnostics": diagnostics,
                    "lineage": synthesis_lineage,
                },
                agent_id="research-synthesizer",
            )
            if not verification["passed"]:
                run.status = RunStatus.PARTIAL
                run.completed_at = utc_now_iso()
                self.storage.save_run(run)
                notify(
                    self._progress_text(
                        "综合报告生成不完整；已保留阶段研究结果，可稍后重试综合"
                    ),
                    100,
                )
                return run
            thesis_content = {
                "thesis": synthesis.get("thesis"),
                "claims": synthesis.get("claims", []),
                "invalidation_conditions": synthesis.get(
                    "invalidation_conditions", []
                ),
                "leading_indicators": synthesis.get("leading_indicators", []),
                "unresolved_questions": synthesis.get("unresolved_questions", []),
                "growth_opportunities": growth.get("opportunities", growth),
                "scenarios": forecast.get("scenarios", forecast),
            }
            thesis_version = self.storage.save_thesis_version(
                company.cik,
                thesis_content,
                run_id=run.run_id,
                created_by=self.model_config.public_id,
                created_at=utc_now_iso(),
            )
            self._save(
                run,
                "thesis-snapshot",
                self._report_text(
                    f"投资逻辑 v{thesis_version['version']}",
                    f"Investment Thesis v{thesis_version['version']}",
                ),
                thesis_version,
                agent_id="thesis-versioning",
            )
            run.status = RunStatus.COMPLETED if verification["passed"] else RunStatus.PARTIAL
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            notify(self._progress_text("研究完成"), 100)
            return run
        except ResearchCancelled as exc:
            exc.run_id = run.run_id
            run.errors.append(str(exc))
            run.status = RunStatus.CANCELLED
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            raise
        except Exception as exc:
            run.errors.append(str(exc))
            run.status = RunStatus.FAILED
            run.completed_at = utc_now_iso()
            self.storage.save_run(run)
            raise

    def retry_synthesis(
        self,
        run: ResearchRun,
        artifacts: list[dict[str, Any]],
        facts: list[dict[str, Any]],
    ) -> ResearchRun:
        """Regenerate only the final synthesis from persisted stage artifacts."""

        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        deterministic = _latest_artifact(artifacts, "deterministic-financial-summary")
        dossier_artifact = _latest_artifact(artifacts, "verified-research-dossier")
        growth_artifact = _latest_artifact(artifacts, "growth-opportunities")
        skeptic_artifact = _latest_artifact(artifacts, "counter-analysis")
        forecast_artifact = _latest_artifact(artifacts, "forecast-scenarios")
        required = (deterministic, dossier_artifact, growth_artifact, skeptic_artifact, forecast_artifact)
        if any(item is None for item in required):
            raise RuntimeError("saved research stages are incomplete")

        deterministic_content = deterministic["content"]
        evidence = deterministic_content.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        context = ResearchContext(
            run.company,
            facts,
            deterministic_content.get("metrics", []),
            deterministic_content.get("interim_metrics", []),
            evidence,
            (_latest_artifact(artifacts, "deterministic-valuation") or {}).get("content"),
            deterministic_content.get("market_snapshot"),
        )
        dossier = dossier_artifact["content"]
        growth = growth_artifact["content"]
        skeptic = skeptic_artifact["content"]
        forecast = forecast_artifact["content"]
        try:
            synthesis = self._run_synthesis_with_budget(
                context, dossier, growth, skeptic, forecast
            )
        except SynthesisContextLimitError as exc:
            synthesis = {
                "claims": [],
                "_response_error": "context_budget_exceeded",
                "_context_sections": exc.sections,
                "_context_required_bytes": exc.required_bytes,
                "_context_available_bytes": exc.available_bytes,
                "_context_counting_mode": exc.counting_mode,
            }
        except ProviderError as exc:
            synthesis = {
                "claims": [],
                "_response_error": (
                    "context_budget_exceeded"
                    if exc.code == "MODEL_CONTEXT_CAPACITY"
                    else "provider_error"
                ),
                "_provider_error_code": exc.code,
                "_provider_retryable": exc.retryable,
                "_context_counting_mode": "provider_error_code",
            }
        available = {
            str(item.get("evidence_id"))
            for item in evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
        evidence_records = {
            str(item.get("evidence_id")): item
            for item in evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
        verification = validate_research_synthesis(
            synthesis, available, self.report_language, evidence_records
        )
        if verification["passed"]:
            report_payload = synthesis
            mode = "synthesized"
        else:
            capacity = synthesis.get("_response_error") == "context_budget_exceeded"
            report_payload = {
                "research_complete": False,
                "claims": [],
                "cross_section_synthesis_status": (
                    "not_completed_context_capacity"
                    if capacity
                    else "not_completed_provider_error"
                    if synthesis.get("_response_error") == "provider_error"
                    else "not_completed_invalid_output"
                ),
            }
            mode = "synthesis-incomplete"
        synthesis_lineage = _synthesis_lineage(
            synthesis, verification, report_payload, dossier, growth, skeptic, forecast
        )
        self._save(
            run,
            "research-report",
            self._report_text("完整长期研究报告", "Complete Long-term Research Report"),
            {
                "mode": mode,
                "report": report_payload,
                "verification": verification,
                "retryable": not verification["passed"],
                "diagnostics": _response_diagnostics(synthesis),
                "lineage": synthesis_lineage,
            },
            agent_id="research-synthesizer",
        )
        if verification["passed"]:
            thesis_content = {
                "thesis": synthesis.get("thesis"),
                "claims": synthesis.get("claims", []),
                "invalidation_conditions": synthesis.get("invalidation_conditions", []),
                "leading_indicators": synthesis.get("leading_indicators", []),
                "unresolved_questions": synthesis.get("unresolved_questions", []),
                "growth_opportunities": growth.get("opportunities", growth),
                "scenarios": forecast.get("scenarios", forecast),
            }
            thesis_version = self.storage.save_thesis_version(
                run.company.cik,
                thesis_content,
                run_id=run.run_id,
                created_by=self.model_config.public_id,
                created_at=utc_now_iso(),
            )
            self._save(
                run,
                "thesis-snapshot",
                self._report_text(
                    f"投资逻辑 v{thesis_version['version']}",
                    f"Investment Thesis v{thesis_version['version']}",
                ),
                thesis_version,
                agent_id="thesis-versioning",
            )
        run.status = RunStatus.COMPLETED if verification["passed"] else RunStatus.PARTIAL
        run.completed_at = utc_now_iso()
        self.storage.save_run(run)
        return run

    def retry_growth(
        self,
        run: ResearchRun,
        artifacts: list[dict[str, Any]],
        facts: list[dict[str, Any]],
    ) -> ResearchRun:
        """Regenerate only growth opportunities, then refresh final synthesis."""

        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        deterministic = _latest_artifact(artifacts, "deterministic-financial-summary")
        dossier_artifact = _latest_artifact(artifacts, "verified-research-dossier")
        if deterministic is None or dossier_artifact is None:
            raise RuntimeError("saved research stages are incomplete")

        deterministic_content = deterministic["content"]
        evidence = deterministic_content.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        available = {
            str(item.get("evidence_id"))
            for item in evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
        context = ResearchContext(
            run.company,
            facts,
            deterministic_content.get("metrics", []),
            deterministic_content.get("interim_metrics", []),
            evidence,
            (_latest_artifact(artifacts, "deterministic-valuation") or {}).get("content"),
            deterministic_content.get("market_snapshot"),
        )
        growth_raw = self._run_agent(
            "growth-opportunity-analyst",
            "prompts/growth-opportunity-analyst.md",
            context.compact_json(),
            {
                "research_dossier": dossier_artifact["content"],
                "retry_instruction": (
                    "The previous growth stage returned no usable opportunities. Regenerate only "
                    "this stage using the persisted evidence; do not rerun or assume other stages."
                ),
            },
        )
        validation = normalize_growth_output(
            growth_raw,
            available,
            self.report_language,
        )
        self._save(
            run,
            "growth-opportunities",
            self._report_text("增长机会", "Growth Opportunities"),
            validation.output,
            agent_id="growth-opportunity-analyst",
        )
        if not validation.passed or not validation.output.get("opportunities"):
            raise RuntimeError("growth-opportunity model returned no usable content")
        return self.retry_synthesis(
            run,
            self.storage.get_artifacts(run.run_id),
            facts,
        )

    def _set_agent_state(self, agent_id: str, state: str) -> None:
        self.agent_progress(agent_id, state)

    def _generate_with_cancellation(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        """Run a provider call without allowing a cancelled UI job to hang.

        Provider SDKs generally expose a blocking request. Running that call in
        a daemon worker lets the workflow acknowledge cancellation immediately;
        the late result is deliberately discarded by the cancelled workflow.
        """
        result: dict[str, Any] | None = None
        error: BaseException | None = None

        def invoke() -> None:
            nonlocal result, error
            try:
                result = self.provider.generate(  # type: ignore[union-attr]
                    system_prompt,
                    user_prompt,
                    json_mode=json_mode,
                )
            except Exception as exc:
                error = exc

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.05)
            if self.cancel_check():
                raise ResearchCancelled()
        if error is not None:
            raise error
        if result is None:
            raise RuntimeError("model provider returned no result")
        return result

    def _run_agent(
        self,
        agent_id: str,
        prompt_path: str,
        context_json: str,
        prior_artifacts: dict[str, Any],
        *,
        enforce_local_budget: bool = True,
    ) -> dict[str, Any]:
        self._check_cancelled()
        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        gate = route_materials(json.loads(context_json), agent_id)["material_adequacy"]
        if gate["status"] == "insufficient":
            return insufficient_material_result(gate, self.report_language)
        role_prompt = self.pack.prompt(prompt_path)
        system_prompt, user_prompt = _agent_prompt_bundle(
            agent_id,
            role_prompt,
            self.report_language,
            context_json,
            prior_artifacts,
        )
        input_size = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
        capability = provider_context_capability(self.provider)
        if enforce_local_budget and input_size > capability.max_input_bytes:
            raise SynthesisContextLimitError(
                [{"name": agent_id, "bytes": input_size}],
                capability.max_input_bytes,
            )
        try:
            result = self._generate_with_cancellation(
                system_prompt,
                user_prompt,
                json_mode=True,
            )
        except Exception:
            self._check_cancelled()
            raise
        self._check_cancelled()
        result["_material_adequacy"] = gate
        return result

    def _save(
        self,
        run: ResearchRun,
        artifact_type: str,
        title: str,
        content: dict[str, Any],
        *,
        agent_id: str,
    ) -> ResearchArtifact:
        raw = json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        identity = agent_id.encode() + b"|" + raw
        suffix = hashlib.sha256(identity).hexdigest()[:10]
        artifact = ResearchArtifact(
            artifact_id=f"{run.run_id}:{artifact_type}:{suffix}",
            run_id=run.run_id,
            artifact_type=artifact_type,
            title=title,
            content=content,
            model_id=self.model_config.public_id,
            agent_id=agent_id,
        )
        self.storage.save_artifact(artifact)
        return artifact


def _latest_artifact(
    artifacts: list[dict[str, Any]], artifact_type: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in reversed(artifacts)
            if item.get("artifact_type") == artifact_type
        ),
        None,
    )
