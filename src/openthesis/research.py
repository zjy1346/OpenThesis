from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from concurrent.futures import CancelledError, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
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
    read_metric_period,
    reverse_dcf_analysis,
)
from .financial_evidence import build_fact_evidence
from .financial_ingestion import FinancialProfile
from .growth import normalize_growth_output
from .i18n import EN, OUTPUT_LANGUAGE_INSTRUCTIONS, UI_HANT, ZH_HANT, normalize_language, translate
from .packs import ResearchPack
from .providers import ModelConfig, ModelProvider, ProviderError
from .model_scheduler import shared_model_scheduler
from .storage import Storage
from .storage_research_publication import ResearchPublicationConflict, ResearchPublicationSnapshot
from .research_continuity import StageAttempt, StageOutcome
from .research_materials import route_materials, prompt_materials, insufficient_material_result, canonical_request_materials
from .research_coverage import CONTRACT_VERSION as RESEARCH_COVERAGE_VERSION, ResearchCoverageContract, prompt_contract as coverage_prompt_contract, obligation_production_plan
from .research_execution.registry import SOURCE_ANALYSIS_STAGES, StageRegistry, standard_model_registry
from .research_execution.model import (
    ModelStageCancelled,
    ModelStageExecutor,
    ModelStageRequest,
    ProviderContextCapability,
    SynthesisContextLimitError,
    provider_context_capability,
    provider_declares_context_capability as _provider_declares_context_capability,
)
from .research_execution.executor import (
    StageExecutor,
    StageExecutionCancelled,
    StageResult,
    StageValidation,
)
from .research_execution.checkpoints import (
    candidate_checkpoint,
    completed_checkpoint,
    read_checkpoints,
)
from .research_execution.durable import DurableStageVerifier
from .research_execution.requests import ModelRequestJournal
from .research_execution.model_output import ModelOutputContract, OutputField
from .research_execution.production import (
    GraphBindings,
    _source_business_inputs,
    execute_official_model_graph,
    run_declarative_ot_workflow,
)
from .resilience.contracts import digest as recovery_digest
from .report_readiness import (
    REQUIRED_REPORT_SECTIONS,
    assess_report_readiness,
    financial_source_evidence_count,
    missing_research_stages,
)
from .valuation_claims import enforce_valuation_consistency
from .research_numeric_assertions import numeric_prose_supported
from .research_assertions import classify_assertion
from .research_calculations import formula_contract, prove_calculation
from .research_section_validation import SECTION_PROOF_VERSION, project_supported_sections, rewrite_claim_references
from .research_claim_dedup import deduplicate_claims
from .research_claim_repair import ClaimValidation, ClaimRepairContract, stable_claim_id, claim_presentation
from .financial_disclosure_bindings import cited_compiled_rows


ProgressCallback = Callable[[str, int], None]
CancelCheck = Callable[[], bool]
AgentProgressCallback = Callable[[str, str], None]


def _valuation_money(value: object, *, currency: str, source_id: str = "", as_of: str = "") -> NormalizedMoney | None:
    """Build the typed valuation boundary from an already-normalized snapshot."""
    if isinstance(value, NormalizedMoney):
        return value if (re.fullmatch(r"[A-Z]{3}", value.currency) and
            math.isfinite(value.normalized_value) and math.isfinite(value.unit_scale)
            and value.unit_scale > 0 and value.unit_provenance not in {"", "unknown"}) else None
    if isinstance(value, dict):
        try:
            amount = float(value["value"])
            unit_scale = float(value["unit_scale"])
            provenance = str(value.get("unit_provenance", value.get("provenance", "")))
            item_currency = str(value.get("currency") or "").upper()
            if (not re.fullmatch(r"[A-Z]{3}", item_currency) or not provenance or provenance == "unknown"
                    or not math.isfinite(amount) or not math.isfinite(unit_scale) or unit_scale <= 0):
                return None
            if provenance == "normalized" and unit_scale != 1.0:
                return None
            if provenance == "normalized":
                return NormalizedMoney.from_normalized(
                    amount, item_currency,
                    source_id=str(value.get("source_id", source_id)),
                    as_of=str(value.get("as_of", as_of)),
                )
            return NormalizedMoney.from_raw(amount, item_currency, unit_scale, provenance)
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and re.fullmatch(r"[A-Z]{3}", currency):
        return NormalizedMoney.from_normalized(value, currency, source_id=source_id, as_of=as_of)
    return None


def _valuation_money_is_positive(value: object, *, currency: str) -> bool:
    item = _valuation_money(value, currency=currency)
    return item is not None and item.normalized_value > 0


class ResearchCancelled(RuntimeError):
    def __init__(self, message: str = "研究已由用户取消", run_id: str = ""):
        super().__init__(message)
        self.run_id = run_id




CORE_SYSTEM_PROMPT = f"""\
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
Official report completeness is governed by {RESEARCH_COVERAGE_VERSION};
only substantive, source-linked analysis can satisfy a research obligation.
Dimensional financial facts are separately scoped disclosures, not group totals.
Preserve their exact expanded axes/members and scope in claims. An axis can
represent geography, product, share class or another disclosure; never assume
it is a business segment. Do not sum overlapping dimensions or mix their units.
"""


CLAIM_CONTRACT_VERSION = "openthesis.claim.v2"
RESEARCH_VERIFICATION_VERSION = "research-verification-v9-typed-claim-repair"
DECLARED_MODEL_ROLE_VERSION = "declared-model-role-v1"


def _claim_contract_shape() -> dict[str, Any]:
    """One prompt/parser/verifier contract for every research agent.

    Deterministic fields are optional in provider output because a uniquely
    cited canonical financial fact can hydrate them without another model
    call.  Calculations still have to provide their formula/inputs.
    """

    return {
        "schema": CLAIM_CONTRACT_VERSION,
        "verification_policy": RESEARCH_VERIFICATION_VERSION,
        "required": {
            "text": "string",
            "kind": "fact|calculation|inference|assumption|forecast|risk|unknown",
            "confidence": "0..1 or null",
            "evidence_ids": ["fact:<id>"],
        },
        "deterministic_fact_fields": {
            "concept": "canonical concept",
            "value": "number",
            "unit": "currency or ratio",
            "fiscal_year": "integer",
            "fiscal_period": "FY|Q1|H1|Q3",
            "end_date": "YYYY-MM-DD",
            "scope": "consolidated|parent|dimension:<source identity>",
        },
        "deterministic_calculation_fields": {
            "formula": "exact registered formula name, also used as concept",
            "inputs": "object mapping each registered input role to a cited canonical fact evidence_id",
            "formulas": formula_contract(),
            "policy": "No inline amounts, arbitrary expressions or formula inferred from prose. Copy the current input's issuer/scope/period. Ratio values are fractions, not percentage display values.",
        },
        "policy": (
            "For a factual numeric claim, copy deterministic fields from the cited "
            "financial_fact. If exactly one financial_fact is cited, OpenThesis may "
            "hydrate omitted identity fields from that record. Never invent them."
        ),
        "section_binding": (
            "Bind each factual analysis section to its own evidence_ids and claims, "
            "or give its top-level claims a section_id. A claim in another section "
            "does not verify this section. Clearly label scenarios as assumptions."
        ),
    }


def _agent_role(agent_id: str, role_id: str | None) -> str:
    """A validated declaration owns semantics; node names retain identity."""
    role = agent_id if role_id is None else role_id
    if not isinstance(role, str) or not role.strip():
        raise ValueError("MODEL_STAGE_ROLE_INVALID")
    return role


def _agent_prompt_bundle(
    agent_id: str,
    role_prompt: str,
    report_language: str,
    context_json: str,
    prior_artifacts: dict[str, Any],
    *, role_id: str | None = None, output_schema: str | None = None,
) -> tuple[str, str]:
    """Build the exact provider prompts used for budget accounting."""
    language_instruction = OUTPUT_LANGUAGE_INSTRUCTIONS[report_language]
    role = _agent_role(agent_id, role_id)
    claim_contract = _claim_contract_shape()
    def stable_record(value):
        if isinstance(value, dict):
            return {key: stable_record(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [stable_record(item) for item in value]
        return value

    from .research_execution.source_compiler import compiled_role_sources
    compiled_sources = compiled_role_sources(context_json, role, _collect_evidence_ids(prior_artifacts))
    # Keep the existing material sections and list ordering (which can encode
    # source/role priority). Canonicalize mapping keys only, without dropping
    # fields, changing independent agents or inventing a paid cache prewarm.
    from .research_execution.executor import dependency_findings
    materials, stage_findings, material_registry = canonical_request_materials(
        {}, dependency_findings(prior_artifacts), compiled_sources=compiled_sources)
    # Role-specific adequacy metadata varies between agents, while the rest of
    # this context is shared. Keep the shared factual material in the longest
    # possible common prefix by placing this metadata after it.
    for late_key in ("material_adequacy", "reading_packet"):
        if late_key in materials:
            late_value = materials.pop(late_key)
            materials[late_key] = late_value
    prompt_payload = {
        "claim_contract": stable_record(claim_contract),
        "output_language": report_language,
        "output_language_instruction": language_instruction,
        "material_registry": material_registry,
        "request_reference_contract": "material_ref resolves to complete text in material_registry in THIS request. finding_ref resolves to prior_artifacts.findings_registry in THIS request. request_path_ref resolves to the JSON path array in THIS request. Preserve each referring record's issuer, period, units and revision; identical text never merges those identities. All referenced material is included, not a summary or external lookup.",
        "research_context": materials,
        "agent": agent_id,
        "task_instructions": role_prompt,
        "prior_artifacts": stage_findings,
        "assigned_research_obligations": obligation_production_plan(role),
    }
    if role_id is not None:
        prompt_payload["agent_role"] = role
    if output_schema is not None:
        if not isinstance(output_schema, str) or not output_schema.strip():
            raise ValueError("MODEL_STAGE_OUTPUT_SCHEMA_INVALID")
        prompt_payload["output_schema"] = output_schema
    # Older/custom research packs may explicitly request this legacy key.
    # Keep that compatibility without sending the same schema twice to every
    # built-in model call; the byte budget must be spent on research material.
    if re.search(r"\brequired_claim_shape\b", role_prompt):
        prompt_payload["required_claim_shape"] = claim_contract
    if role.startswith("research-synthesizer"):
        prompt_payload["research_coverage_contract"] = coverage_prompt_contract(compact=True)
        from .research_execution.analysis_references import reference_registry
        prompt_payload["analysis_registry"] = reference_registry(prior_artifacts)
        prompt_payload["analysis_reference_contract"] = (
            "Prefer analysis_refs:[{analysis_id,section}] to retain prior findings verbatim. IDs resolve only to current "
            "prior_artifacts paths; the host copies their complete bodies and performs the ordinary source/numeric checks. "
            "Do not rewrite unchanged premises just to fill sections. New conclusions and opposing views still need their own sources.")
    delivery = prior_artifacts.get("sectional_delivery")
    if (role == "research-synthesizer" and output_schema == "research-report-section-v1"
            and isinstance(delivery, dict)):
        sections = delivery.get("sections", [delivery.get("section")])
        if (not isinstance(sections, list) or not sections or any(not isinstance(section, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", section) for section in sections)):
            raise ValueError("MODEL_SECTION_DELIVERY_TARGETS_INVALID")
        prompt_payload["sectional_delivery"] = deepcopy(delivery)
        prompt_payload["prior_artifacts"].pop("sectional_delivery", None)
        prompt_payload["task_instructions"] += (
            "\nOutput-length recovery overrides only delivery shape, not research scope. "
            f"Return only these missing section objects: {', '.join(sections)}, and their source-bound `claims` in JSON. "
            "Do not return a shortened whole report. Do not shorten evidence, omit opposing views, "
            "or infer from absent sources. All original sources and prior findings remain supplied. "
            "Use them to preserve cross-section dependencies and explicit uncertainties. "
            "The host will assemble these complete section objects without another lossy rewrite.")
        prompt_payload["assigned_research_obligations"] = [item for item in obligation_production_plan()
            if any(path.split(".")[0] in sections for path in item["report_paths"])]
        from .research_execution.repair_context import repair_context
        narrowed = repair_context(json.dumps(prompt_payload, ensure_ascii=False), tuple(sections))
        prompt_payload = {**narrowed, **{key: prompt_payload[key] for key in (
            "agent", "task_instructions", "assigned_research_obligations", "sectional_delivery", "output_schema")}}
    user_prompt = json.dumps(
        prompt_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    system_prompt = CORE_SYSTEM_PROMPT + "\n" + language_instruction
    if role == "research-synthesizer-repair":
        system_prompt += "\n\n" + role_prompt
    return system_prompt, user_prompt


def _agent_input_size(
    agent_id: str,
    role_prompt: str,
    report_language: str,
    context_json: str,
    prior_artifacts: dict[str, Any],
    *, role_id: str | None = None, output_schema: str | None = None,
) -> int:
    system_prompt, user_prompt = _agent_prompt_bundle(
        agent_id, role_prompt, report_language, context_json, prior_artifacts,
        role_id=role_id, output_schema=output_schema,
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
    financial_period_coverage: dict[str, Any] = field(default_factory=dict)
    repair_observations: list[dict[str, Any]] = field(default_factory=list)
    material_issues: list[dict[str, Any]] = field(default_factory=list)
    research_as_of: str = ""

    def compact_json(self) -> str:
        return json.dumps(
            {
                "company": self.company.to_dict(),
                "facts": self.facts,
                "metrics": self.metrics,
                "latest_interim_metrics": self.interim_metrics,
                "evidence": self.evidence,
                "valuation": self.valuation,
                "market_snapshot": self.market_snapshot,
                "research_as_of": self.research_as_of,
                "valuation_gaps": self.valuation_gaps(),
                "repair_observations": self.repair_observations,
                "issues": self.material_issues,
                **({"financial_period_coverage": self.financial_period_coverage}
                   if self.financial_period_coverage else {}),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def valuation_gaps(self) -> dict[str, Any]:
        """Host-owned missing inputs; None is not permission to guess CapEx."""
        latest = max((row for row in self.metrics if isinstance(row, dict)), key=lambda row: str(
            row.get("period_end") or row.get("end_date") or row.get("year") or row.get("fiscal_year") or ""), default={})
        status = str((self.valuation or {}).get("status") or "not_calculated")
        reasons = []
        market = self.market_snapshot or {}
        if not market.get("equity_market_value", market.get("market_cap")) and not (
                self.valuation or {}).get("equity_market_value"):
            reasons.append("market_equity_value_missing")
        if latest.get("free_cash_flow") is None:
            reasons.append(str(latest.get("free_cash_flow_gap") or "verified_cash_flow_base_missing"))
        elif not isinstance(latest.get("free_cash_flow"), (int, float)) or latest["free_cash_flow"] <= 0:
            reasons.append("positive_verified_cash_flow_base_unavailable")
        if status not in {"ok", "extreme_assumption", "not_calculated"}:
            reasons.append(status)
        return {"status": status, "reasons": list(dict.fromkeys(reasons)),
                "available_financial_inputs": [key for key in
                    ("operating_cash_flow", "capital_expenditure", "free_cash_flow")
                    if latest.get(key) is not None],
                "instruction": "Use these host gaps exactly; do not infer non-disclosure from an absent valuation. Existing inputs remain usable under their own contracts."}


@dataclass(frozen=True, slots=True)
class StageMaterialManifest:
    """Auditable identity and coverage for one model-stage input."""

    stage: str
    input_sha256: str
    unique_evidence_ids: tuple[str, ...]
    exact_duplicates_removed: int
    mode: str
    partition_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "openthesis.stage-material-manifest.v1",
            "stage": self.stage,
            "input_sha256": self.input_sha256,
            "unique_evidence_ids": list(self.unique_evidence_ids),
            "exact_duplicates_removed": self.exact_duplicates_removed,
            "mode": self.mode,
            "partition_count": self.partition_count,
        }


def _deduplicated_stage_context(
    context_json: str, stage: str, *, mode: str = "direct", partition_count: int = 1
) -> tuple[str, StageMaterialManifest]:
    """Remove exact repeated records while preserving every unique payload."""

    context = json.loads(context_json)
    evidence = context.get("evidence") if isinstance(context, dict) else None
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    removed = 0
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            encoded = json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            if digest in seen:
                removed += 1
                continue
            seen.add(digest)
            unique.append(item)
        context["evidence"] = unique
    compact = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    evidence_ids = tuple(
        dict.fromkeys(
            str(item.get("evidence_id"))
            for item in unique
            if item.get("evidence_id")
        )
    )
    return compact, StageMaterialManifest(
        stage=stage,
        input_sha256=hashlib.sha256(compact.encode("utf-8")).hexdigest(),
        unique_evidence_ids=evidence_ids,
        exact_duplicates_removed=removed,
        mode=mode,
        partition_count=partition_count,
    )


def _stage_input_digest(context_json: str, prior_artifacts: dict[str, Any], *,
                        role_id: str | None = None, output_schema: str | None = None) -> str:
    payload = {"context": json.loads(context_json), "prior_artifacts": prior_artifacts}
    if role_id is not None:
        payload["agent_role"] = role_id
    if output_schema is not None:
        payload["output_schema"] = output_schema
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    normalized_claims: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(claims or []):
        claim_path = f"/claims/{claim_index}"
        claim_identity = stable_claim_id(claim, claim_path)
        issue_code = ""
        missing_fields: tuple[str, ...] = ()
        if not isinstance(claim, dict):
            issues.append(
                "A claim is not a JSON object" if english else "存在非对象 claim"
            )
            # Shape errors are not evidence that readable source analysis is
            # false. Keep a string as an explicitly untyped pending assertion;
            # never invent a fact kind, value, unit or supporting reference.
            pending_claim = {"text": claim} if isinstance(claim, str) and claim.strip() else {}
            claim_verifications.append({"index": claim_index, "state": "insufficient_evidence",
                "path": claim_path, "claim_id": claim_identity, "issue_code": "CLAIM_OBJECT_REQUIRED",
                "missing_fields": [], "evidence_ids": [], "normalized_claim": pending_claim,
                "repairability": "unrepairable"})
            normalized_claims.append(pending_claim)
            continue
        evidence_ids = claim.get("evidence_ids", [])
        if (not isinstance(evidence_ids, list)
                or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)):
            issues.append(
                "claim.evidence_ids must be an array"
                if english
                else "claim.evidence_ids 必须是数组"
            )
            claim_verifications.append({"index": claim_index, "state": "insufficient_evidence",
                "path": claim_path, "claim_id": claim_identity, "issue_code": "CLAIM_EVIDENCE_IDS_INVALID",
                "missing_fields": [], "evidence_ids": [], "normalized_claim": deepcopy(claim),
                "repairability": "unrepairable"})
            normalized_claims.append(dict(claim))
            if claim.get("kind") in {"fact", "calculation"}:
                unsupported_count += 1
            continue
        missing = [item for item in evidence_ids if item not in available_evidence]
        if missing or not evidence_ids:
            issue_code = "CLAIM_REFERENCE_NOT_FOUND"
        if missing:
            prefix = "Unknown evidence reference: " if english else "引用不存在："
            issues.append(prefix + ", ".join(map(str, missing)))
        cited_records: list[dict[str, Any]] = []
        if evidence_records and not missing:
            cited_records = [
                evidence_records[item]
                for item in evidence_ids
                if item in evidence_records and isinstance(evidence_records[item], dict)
            ]
            compiled = [row for source in cited_records
                        for row in cited_compiled_rows(source, evidence_records)]
            cited_records = list({record.get("evidence_id"): record
                                  for record in (*cited_records, *compiled)}.values())
        claim = _hydrate_claim_from_unique_financial_evidence(claim, cited_records)
        normalized_claims.append(claim)
        asserted = {
            key: claim.get(key)
            for key in (
                "company_cik", "entity", "market", "concept", "value", "unit", "currency",
                "unit_scale", "unit_provenance", "scope", "consolidated_scope",
                "start_date", "end_date", "filed_at", "fiscal_year",
                "fiscal_period", "form_type", "accession_number", "source_document", "dimensions",
            )
            if claim.get(key) not in (None, "")
        }
        financial_records = []
        matching_records: list[dict[str, Any]] = []
        related_records: list[dict[str, Any]] = []
        if evidence_records and not missing:
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
            if claim.get("kind") != "calculation" and financial_records and asserted and (
                not matching_records
                or any(record not in matching_records for record in related_records)
            ):
                issues.append(
                    "Claim period/value does not match its cited financial evidence"
                    if english
                    else "结论中的期间或数值与引用的财务证据不一致"
                )
        state = "insufficient_evidence"
        calculation_record = None
        if missing or not evidence_ids:
            state = "insufficient_evidence"
        elif claim.get("kind") in {"fact", "calculation"} and not _claim_has_deterministic_fields(claim):
            # A fact mislabeled by the model may be retained as a qualitative
            # disclosure inference, but only when matching official filing
            # text directly supports it and no numeric assertion is present.
            text_state = _claim_text_support_state(
                claim,
                cited_records,
                filing_only=claim.get("kind") == "fact",
            )
            if text_state == "contradicted":
                state = "contradicted"
                issue_code = "CLAIM_DIRECTION_CONFLICT"
                issues.append(
                    "Claim text contradicts its cited evidence"
                    if english else "结论文本与引用的证据方向相反"
                )
            elif (
                claim.get("kind") == "fact"
                and text_state == "text_supported"
                and not _has_numeric_fact_assertion(claim)
                and _filing_context_matches_claim(claim, cited_records)
            ):
                claim = dict(claim)
                claim["kind"] = "inference"
                claim["claim_channel"] = "filing_disclosure_inference"
                claim["claim_contract"] = CLAIM_CONTRACT_VERSION
                normalized_claims[-1] = claim
                state = "text_supported"
            else:
                issues.append(
                    "Factual claims require deterministic identity and value fields"
                    if english else "事实性结论必须包含可确定核验的身份与数值字段"
                )
                state = "insufficient_evidence"
                issue_code = "CLAIM_DETERMINISTIC_FIELDS_MISSING"
                missing_fields = _missing_claim_fields(claim)
        elif claim.get("kind") == "calculation":
            proof = prove_calculation(claim, cited_records)
            issue_code = proof.issue_code
            if proof.record is None:
                state = "contradicted" if issue_code.endswith("CONFLICT") else "insufficient_evidence"
                issues.append("Calculation formula, inputs or result could not be verified" if english
                              else "计算的公式、输入或结果未通过确定性验证")
            else:
                state = "numeric_verified"
                calculation_record = proof.record
        elif financial_records and asserted and matching_records and not any(
            record not in matching_records for record in related_records
        ):
            text_state = _claim_text_support_state(claim, cited_records)
            if text_state == "contradicted":
                state = "contradicted"
                issue_code = "CLAIM_DIRECTION_CONFLICT"
                issues.append(
                    "Claim text contradicts its cited evidence"
                    if english else "结论文本与引用的证据方向相反"
                )
            else:
                state = "numeric_verified"
        elif financial_records and asserted:
            state = "contradicted"
            issue_code = _financial_claim_conflict_code(asserted, financial_records)
        else:
            text_state = _claim_text_support_state(claim, cited_records)
            if text_state == "contradicted":
                state = "contradicted"
                issue_code = "CLAIM_DIRECTION_CONFLICT"
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
                # A callback's success word is not source evidence. Legacy
                # integrations must return the same source-span protocol as
                # the configured semantic verifier, then scope checks rerun.
                from .research_assertions import AssertionSession, assertion_session, validate_response
                session = AssertionSession()
                with assertion_session(session):
                    _claim_text_support_state(claim, cited_records)
                accepted = validate_response(review, list(session.queries.values()), "explicit_semantic_reviewer")
                with assertion_session(AssertionSession(accepted)):
                    text_state = _claim_text_support_state(claim, cited_records)
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
        prose_records = cited_records
        if calculation_record is not None:
            prose_records = [calculation_record, *cited_records]
        elif state == "numeric_verified" and claim.get("concept"):
            # A second valid citation to net income must not supply a wrong
            # revenue number. Comparative periods of the same concept remain
            # available, but a different accounting target does not.
            prose_records = [record for record in financial_records
                if str(record.get("concept", "")).casefold() == str(claim["concept"]).casefold()
                and all(claim.get(key) in (None, "")
                        or str(claim[key]).casefold() == str(record.get(key, "")).casefold()
                        for key in ("company_cik", "entity", "scope"))]
        if state in {"numeric_verified", "text_supported"} and not numeric_prose_supported(
            str(claim.get("text") or claim.get("conclusion") or claim.get("argument") or ""),
            prose_records,
            fiscal_year=claim.get("fiscal_year") if state == "numeric_verified" else None,
        ):
            state = "contradicted"
            issue_code = "CLAIM_PROSE_NUMERIC_CONFLICT"
            issues.append(
                "Numbers in claim prose are not supported by the cited source"
                if english else "结论正文中的数值未得到所引源证据支持"
            )
        if state in {"numeric_verified", "text_supported"}:
            verified_count += 1
        elif claim.get("kind") in {"fact", "calculation"} or asserted:
            unsupported_count += 1
        if state == "contradicted":
            issue_code = issue_code or "CLAIM_NUMERIC_OR_DIRECTION_CONFLICT"
        elif state == "insufficient_evidence" and not issue_code:
            issue_code = "CLAIM_SUPPORT_INSUFFICIENT"
        if (state == "insufficient_evidence" and _has_numeric_fact_assertion(claim)
                and not financial_records and cited_records
                and all(record.get("kind") == "filing_text" for record in cited_records)):
            issue_code = "CLAIM_QUANTITATIVE_DISCLOSURE_UNCOMPILED"
        validation = ClaimValidation(claim_identity, claim_path,
            state, issue_code, missing_fields, tuple(evidence_ids), deepcopy(claim),
            "field_patch" if missing_fields and not missing and financial_records else "unrepairable")
        claim_verifications.append({
            **validation.to_dict(),
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
    for row in claim_verifications:
        row.update(claim_presentation(row["state"], row.get("normalized_claim")))
    return {
        "structured_output_valid": structured_output_valid,
        "claim_count": len(claims or []),
        "verified_claim_count": verified_count,
        "unsupported_fact_count": unsupported_count,
        "claim_verifications": claim_verifications,
        "normalized_claims": normalized_claims,
        "candidate_claims": deepcopy(claims or []),
        "displayable_claims": [deepcopy(row["normalized_claim"]) for row in claim_verifications
                               if row["display_eligible"]],
        "issues": issues,
        "passed": structured_output_valid and not issues and unsupported_count == 0,
    }


_HYDRATABLE_FACT_FIELDS = (
    "company_cik", "entity", "market", "concept", "value", "unit", "currency",
    "unit_scale", "unit_provenance", "scope", "consolidated_scope", "start_date",
    "end_date", "filed_at", "fiscal_year", "fiscal_period", "form_type",
    "accession_number", "source_document", "dimensions",
)


def _hydrate_claim_from_unique_financial_evidence(
    claim: dict[str, Any], records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fill omitted fact identity only from one unambiguous canonical record."""

    hydrated = dict(claim)
    if str(hydrated.get("kind", "")).casefold() != "fact":
        return hydrated
    identity = {key: claim[key] for key in _HYDRATABLE_FACT_FIELDS
                if key not in {"value", "unit_scale", "unit_provenance", "filed_at"}
                and claim.get(key) not in (None, "")}
    financial = [item for item in records if item.get("kind") == "financial_fact"
                 and _claim_matches_financial_evidence(identity, item)]
    if len(financial) != 1:
        return hydrated
    source = financial[0]
    for key in _HYDRATABLE_FACT_FIELDS:
        if hydrated.get(key) in (None, "") and source.get(key) not in (None, ""):
            hydrated[key] = source[key]
    hydrated["claim_contract"] = CLAIM_CONTRACT_VERSION
    hydrated["deterministic_fields_source"] = "unique_canonical_evidence"
    return hydrated


def _claim_text_support_state(
    claim: dict[str, Any], records: list[dict[str, Any]], *, filing_only: bool = False
) -> str:
    """Check source scope, then consume exact-quote or source-bound semantics.

    Word overlap is a retrieval heuristic, never a factual support proof.
    Outside a host verification session only full source quotations pass.
    """
    claim_text = str(
        claim.get("text") or claim.get("conclusion") or claim.get("argument") or ""
    ).strip()
    if not claim_text or not records:
        return "insufficient_evidence"
    eligible = []
    for record in records:
        if (
            str(record.get("kind", "")).casefold() == "unverified_visual_observation"
            or (str(record.get("evidence_id", "")).casefold().startswith("observation:")
                and record.get("kind") != "unadmitted_source_row")
            or (record.get("kind") == "unadmitted_source_row"
                and str(claim.get("kind", "")).casefold() in {"fact", "calculation"})
        ):
            # These transcriptions are preserved for human inspection only;
            # they cannot verify prose or numeric claims.
            continue
        if filing_only and not _is_filing_text_record(record):
            continue
        if _is_filing_text_record(record) and not _filing_context_matches_claim(claim, [record]):
            continue
        from .research_assertions import source_text as assertion_source_text
        source_text = assertion_source_text(record).strip()
        if not source_text:
            continue
        eligible.append(record)
        # Direction-word overlap cannot decide a compound assertion: revenue
        # can rise while margins fall in the same source paragraph. Numeric
        # identities stay deterministic; prose uses the source-bound verifier.
    return classify_assertion(claim, eligible)


def _is_filing_text_record(record: dict[str, Any]) -> bool:
    kind = str(record.get("kind", "")).casefold()
    evidence_id = str(record.get("evidence_id", "")).casefold()
    return kind in {"filing_text", "disclosure_text"} or evidence_id.startswith("filing:")


def _filing_context_matches_claim(
    claim: dict[str, Any], records: list[dict[str, Any]]
) -> bool:
    """Require disclosure citations to bind to the asserted issuer and period."""
    filing_records = [record for record in records if _is_filing_text_record(record)]
    if not filing_records:
        return False
    for record in filing_records:
        source_end = str(record.get("end_date") or "").strip()[:10]
        source_year = str(record.get("fiscal_year") or source_end[:4]).strip()
        source_period = str(record.get("fiscal_period") or "").strip().casefold()
        if not record.get("company_cik") or not source_end:
            return False
        for key in ("company_cik", "entity", "scope"):
            expected = claim.get(key)
            actual = record.get(key)
            if expected not in (None, "") and (
                actual in (None, "")
                or str(expected).strip().casefold() != str(actual).strip().casefold()
            ):
                return False
        end_date = claim.get("end_date")
        if end_date not in (None, "") and str(end_date).strip()[:10] != source_end:
            return False
        fiscal_year = claim.get("fiscal_year")
        if fiscal_year not in (None, "") and str(fiscal_year).strip() != source_year:
            return False
        fiscal_period = claim.get("fiscal_period")
        if fiscal_period not in (None, "") and str(fiscal_period).strip().casefold() != source_period:
            return False
    return True


def _has_numeric_fact_assertion(claim: dict[str, Any]) -> bool:
    if any(
        claim.get(key) not in (None, "")
        for key in ("value", "unit", "currency", "unit_scale", "formula", "input_claim_ids", "inputs")
    ):
        return True
    text = str(claim.get("text") or claim.get("conclusion") or claim.get("argument") or "")
    return bool(
        re.search(
            r"(?i)(?:[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|％|倍|元|万元|亿元|美元|人民币|港元|CNY|USD|HKD|million|billion|thousand|\bx\b))",
            text,
        )
    )


def _claim_targets_record(
    claim: dict[str, Any], claim_text: str, record: dict[str, Any]
) -> bool:
    """Bind prose direction to the same concept/period/scope before comparing.

    Direction words alone are never a contradiction.  Structured identity is
    authoritative; prose-only claims must mention a label carried by the cited
    record.  This prevents an expense decline from contradicting cited revenue
    growth merely because both sentences contain directional language.
    """

    if _is_filing_text_record(record):
        return _filing_context_matches_claim(claim, [record])
    asserted = {
        key: claim.get(key)
        for key in ("concept", "fiscal_year", "fiscal_period", "end_date", "scope")
        if claim.get(key) not in (None, "")
    }
    if asserted:
        return _same_financial_target(asserted, record)
    lowered = re.sub(r"\s+", "", claim_text.casefold())
    labels = {
        str(record.get("concept", "")).replace("_", " ").casefold(),
        str(record.get("reported_concept", "")).casefold(),
    }
    raw = str(record.get("raw_text", ""))
    claim_subject = _direction_subject(claim_text)
    source_subject = _direction_subject(raw)
    if claim_subject and source_subject and (
        claim_subject in source_subject or source_subject in claim_subject
    ):
        return True
    raw_label = re.split(r"[-+()（）\d,.，%％]", raw, maxsplit=1)[0].strip().casefold()
    if raw_label:
        labels.add(raw_label)
    for label in labels:
        compact = re.sub(r"\s+", "", label)
        if len(compact) >= 2 and compact in lowered:
            return True
        for token in re.findall(r"[a-z]{4,}|[\u4e00-\u9fff]{2,}", compact):
            if token in lowered:
                return True
    return False


def _direction_subject(text: str) -> str:
    """Extract the subject immediately preceding a directional predicate."""

    lowered = re.sub(r"\s+", " ", str(text).casefold()).strip()
    parts = re.split(
        r"\b(?:grow|grew|growth|increase|increased|rise|rose|up|higher|improv\w*|"
        r"declin\w*|decreas\w*|fall|fell|down|lower|wors\w*)\b|"
        r"增长|增加|上升|提升|改善|下降|减少|下滑|降低|恶化",
        lowered,
        maxsplit=1,
    )
    if len(parts) < 2:
        return ""
    subject = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", parts[0])
    return subject[-40:]


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
            if not left.is_finite() or not right.is_finite():
                return False
            # A structured value asserts the canonical source amount, not a
            # display-rounded estimate. Only prose has a written-precision
            # rounding contract; a percentage tolerance would accept more
            # wrong currency units merely because the company is large.
            if left != right:
                return False
        elif key == "unit_scale":
            try:
                if Decimal(str(expected)) != Decimal(str(actual)):
                    return False
            except (InvalidOperation, TypeError, ValueError):
                return False
        elif key == "dimensions":
            # Namespace URIs and member strings are case-sensitive. JSON
            # roundtrips may change tuples to lists, not their source identity.
            if json.dumps(expected, sort_keys=True, ensure_ascii=False) != json.dumps(actual, sort_keys=True, ensure_ascii=False):
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


def _missing_claim_fields(claim: dict[str, Any]) -> tuple[str, ...]:
    missing = [key for key in ("concept", "value") if claim.get(key) in (None, "")]
    if not (claim.get("unit") or claim.get("currency")):
        missing.append("unit")
    if not (claim.get("end_date") or (claim.get("fiscal_year") not in (None, "") and claim.get("fiscal_period"))):
        missing.append("end_date")
    if claim.get("kind") == "calculation":
        if not claim.get("formula"):
            missing.append("formula")
        if not claim.get("inputs"):
            missing.append("inputs")
    return tuple(missing)


def _financial_claim_conflict_code(asserted: dict, records: list[dict]) -> str:
    candidates = list(records)
    for fields, code in (
        (("concept",), "CLAIM_CONCEPT_CONFLICT"),
        (("company_cik", "entity", "market", "scope", "consolidated_scope", "dimensions"), "CLAIM_IDENTITY_CONFLICT"),
        (("end_date", "start_date", "fiscal_year", "fiscal_period"), "CLAIM_PERIOD_CONFLICT"),
        (("unit", "currency", "unit_scale", "unit_provenance"), "CLAIM_UNIT_CONFLICT"),
        (("value",), "CLAIM_VALUE_CONFLICT"),
    ):
        constraint = {key: asserted[key] for key in fields if key in asserted}
        if constraint:
            candidates = [record for record in candidates if _claim_matches_financial_evidence(constraint, record)]
            if not candidates:
                return code
    return "CLAIM_EVIDENCE_AMBIGUOUS"


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
            bool(claim.get("concept")) and has_value_unit
            and has_period
            and bool(claim.get("formula") and claim.get("inputs"))
        )
    return True


_REQUIRED_SYNTHESIS_SECTIONS = frozenset(REQUIRED_REPORT_SECTIONS)
_SYNTHESIS_OUTPUT_CONTRACT = ModelOutputContract("research-report-shape-v1", tuple(
    OutputField(name, ("array",) if name == "claims" else
                ("object", "array") if name in {"growth_opportunities", "scenarios"} else
                ("string", "object", "array"))
    for name in REQUIRED_REPORT_SECTIONS
))


def _agent_output_contract(role: str, output_schema: str | None) -> ModelOutputContract | None:
    # Known final-report roles may use the generic registered OT artifact URI.
    # Diagnostic/custom schemas are not silently replaced by the report schema.
    if role == "research-synthesizer" and output_schema in {
        None, "research-report-v1", "ot://openthesis/agent-artifact/1",
    }:
        return _SYNTHESIS_OUTPUT_CONTRACT
    return None


def validate_research_synthesis(
    output: dict[str, Any],
    available_evidence: set[str],
    language: str = "zh-CN",
    evidence_records: dict[str, dict[str, Any]] | None = None,
    *, coverage_context: ResearchContext | None = None,
) -> dict[str, Any]:
    claims = output.get("claims")
    verification = verify_agent_output(
        ({**output, "claims": _collect_stage_claims(output)}
         if claims is None or isinstance(claims, list) else output),
        available_evidence, language, evidence_records
    )
    if evidence_records is not None and not output.get("_response_error"):
        _supported, section_audit = _supported_synthesis_sections(
            output, available_evidence, evidence_records, language,
            valuation=coverage_context.valuation if coverage_context is not None else None,
        )
        verification["section_validation"] = section_audit
        verification["trusted_report"] = _supported
        if section_audit["quarantined_paths"]:
            verification["issues"].append(
                "Some report content lacks independent section evidence"
                if normalize_language(language) == EN else "部分报告内容缺少独立章节证据支持"
            )
            verification["passed"] = False
    verification["claim_integrity_passed"] = (
        verification.get("passed") is True
        and verification.get("verified_claim_count", 0) > 0
        and verification.get("unsupported_fact_count", 0) == 0
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
    if verification["verified_claim_count"] == 0:
        verification["issues"].append(
            "The final report contains no verifiable claims"
            if english
            else "最终报告没有可验证的主要结论"
        )
    response_meta = output.get("_response_meta", {})
    if (isinstance(output.get("_output_incomplete"), dict)
            or isinstance(response_meta, dict) and str(response_meta.get("finish_reason") or "").casefold()
               in {"length", "max_tokens", "max_output_tokens"}):
        verification["issues"].append("MODEL_OUTPUT_BUDGET_EXHAUSTED")
    verification["passed"] = not verification["issues"] and verification["unsupported_fact_count"] == 0
    coverage = (ResearchCoverageContract().assess(
        verification.get("trusted_report", {}), verification, evidence_records=evidence_records or {},
        metrics=coverage_context.metrics, valuation=coverage_context.valuation,
        company=coverage_context.company.to_dict(),
        financial_period_coverage=coverage_context.financial_period_coverage,
    ) if coverage_context is not None else None)
    readiness = assess_report_readiness(output, verification, coverage=coverage)
    verification["report_readiness"] = readiness.to_dict()
    if not readiness.complete:
        verification["issues"].append(
            "Report does not contain substantive, displayable analysis in every required section"
            if english
            else "报告的必要章节未全部包含实质、可展示的分析内容"
        )
        verification["passed"] = False
    return verification


def _supported_synthesis_sections(
    output: dict[str, Any], available: set[str], records: dict[str, dict[str, Any]], language: str,
    *, valuation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = sorted(_REQUIRED_SYNTHESIS_SECTIONS | {
        path.split(".")[0] for requirement in ResearchCoverageContract.obligations
        for path in requirement.paths} | {key for key in output
        if isinstance(key, str) and not key.startswith("_")
        and key not in {"structured_output_valid", "trusted_channels", "claims"}})
    deterministic, _ = enforce_valuation_consistency(
        {"valuation_and_sensitivity": {}}, valuation, language)
    computed_fields = {f"{section}.{key}": value
                       for section in ("implied_expectations", "valuation_and_sensitivity")
                       for key, value in deterministic.get(section, {}).items()
                       if key != "role"}
    return _supported_research_fields(output, fields, available, records, language,
                                      computed_fields=computed_fields)


def _supported_research_fields(
    output: dict[str, Any], fields: list[str], available: set[str],
    records: dict[str, dict[str, Any]], language: str,
    *, computed_fields: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # The host's map key is a source identity, not a model assertion. Older
    # typed snapshots legitimately omit the redundant ID in the value.
    # Conflicting explicit IDs remain unusable instead of being overwritten.
    records = {key: {**record, "evidence_id": key} for key, record in records.items()
               if isinstance(record, dict) and record.get("evidence_id") in (None, "", key)}
    def verify(claims):
        return verify_agent_output({"claims": claims}, available, language, records)

    numeric_gaps = {}
    def prose(text, refs):
        cited = [records[ref] for ref in refs if ref in records]
        from .research_numeric_assertions import disclosure_quantity_conflicted
        if disclosure_quantity_conflicted(text, cited):
            return "contradicted"
        conflict_candidates = cited or list(records.values())
        years = set(re.findall(r"(?:19|20)\d{2}", text))
        targeted = [record for record in conflict_candidates if record.get("kind") == "financial_fact"
                    and (str(record.get("fiscal_year")) in years or not years and bool(cited))
                    and _claim_targets_record({}, text, record)]
        if targeted and not numeric_prose_supported(text, targeted):
            # A missing source is not a proof of error. Only an unambiguous
            # same-concept, explicitly same-year financial target can establish
            # a numeric conflict here; other statements stay pending.
            targets = {(row.get("concept"), row.get("fiscal_year"), row.get("scope"),
                        row.get("currency") or row.get("unit"), row.get("company_cik"),
                        row.get("start_date"), row.get("end_date")) for row in targeted}
            if (len(targets) == 1 and (not years or years == {str(targeted[0].get("fiscal_year"))})
                    and _has_numeric_fact_assertion({"text": text})):
                return "contradicted"
        if not numeric_prose_supported(text, cited):
            from .research_section_validation import content_digest
            key = content_digest(text)
            numeric_gaps[key] = {"body_sha256": key, "evidence_ids": list(refs),
                                 "code": "CONTENT_NUMERIC_PREMISE_UNVERIFIED"}
            return False  # Conflict detection alone never grants source support.
        state = _claim_text_support_state({"kind": "inference", "text": text,
                                          "evidence_ids": refs}, cited)
        if state == "contradicted":
            return "contradicted"
        if state == "text_supported":
            return True
        # Mentioning a metric name is not proof of the surrounding analysis.
        # Structured facts are now available to the same semantic session;
        # numeric assertions additionally pass the deterministic checks above.
        from .research_assertions import source_bound_analysis
        return "source_bound_analysis" if source_bound_analysis(text, refs) else False

    from .research_assertions import ensure_assertion_session
    with ensure_assertion_session():
        safe, audit = project_supported_sections(output, fields,
                                          verify, prose, available, computed_fields=computed_fields)
    from .research_section_validation import content_digest
    from .research_assertions import independent_sentences
    for node in audit.get("nodes", ()):
        if node.get("validation_state") != "partial" or not isinstance(node.get("body"), str):
            continue
        parts = [node["body"], *independent_sentences(node["body"])]
        if any(content_digest(part) in numeric_gaps for part in parts):
            node["issue_code"] = "CONTENT_NUMERIC_PREMISE_UNVERIFIED"
    audit["local_numeric_gaps"] = list(numeric_gaps.values())
    return safe, audit


def _presentation_stage_value(value: Any, *, remove_claims: bool = True, _root: bool = True) -> Any:
    if isinstance(value, dict):
        if _root:
            from .research_response import response_parts
            value, _metadata, _diagnostics = response_parts(value)
        return {
            key: _presentation_stage_value(item, remove_claims=remove_claims, _root=False)
            for key, item in value.items()
            if not str(key).startswith("_")
            and key != "structured_output_valid"
            and (not remove_claims or key != "claims")
        }
    if isinstance(value, list):
        return [
            _presentation_stage_value(item, remove_claims=remove_claims, _root=False)
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
    eligible: list[dict[str, Any]] = []
    for claim in found:
        text = str(
            claim.get("text")
            or claim.get("conclusion")
            or claim.get("argument")
            or ""
        ).strip()
        if not text:
            continue
        eligible.append(_presentation_stage_value(claim, remove_claims=False))
    unique, audit = deduplicate_claims(eligible)
    return rewrite_claim_references(unique, audit["claim_id_aliases"])


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
        elif kind in {"assumption", "forecast", "scenario", "risk", "opinion", "question", "unresolved"}:
            channel = "assumptions_scenarios"
        else:
            channel = "unresolved"
        channels[channel].append(dict(claim))
    return channels


def _trusted_stage_result(
    result: dict[str, Any], verification: dict[str, Any],
    evidence_records: dict[str, dict[str, Any]] | None = None, language: str = "zh-CN",
) -> dict[str, Any]:
    """Separate verified claims from host-source-bound labelled analysis.

    Raw output remains durable. A partial analysis is not verified financial
    input; its own citation and contradiction checks govern presentation.
    """
    states = {
        int(item.get("index", -1)): item.get("state")
        for item in verification.get("claim_verifications", [])
        if isinstance(item, dict)
    }
    source_claims = verification.get("normalized_claims")
    if not isinstance(source_claims, list):
        source_claims = result.get("claims", [])
    trusted_claims = [
        claim
        for index, claim in enumerate(source_claims)
        if isinstance(claim, dict)
        and states.get(index) in {"numeric_verified", "text_supported"}
    ] if isinstance(result.get("claims"), list) else []
    channels = _typed_trusted_channels(source_claims if isinstance(source_claims, list) else [], states)
    if evidence_records is not None:
        # Stage claims belong to this stage's object; they cannot grant trust
        # to an unrelated sibling paragraph. Use the same leaf-level proof as
        # final synthesis, rather than copy the whole partial provider object.
        from .research_response import response_parts
        stage_body, stage_metadata, stage_diagnostics = response_parts(result)
        safe, audit = _supported_research_fields(
            {"stage": stage_body, "claims": result.get("claims", [])}, ["stage"],
            set(evidence_records), evidence_records, language,
        )
        audit["stage_system_metadata"] = stage_metadata
        audit["stage_response_diagnostics"] = stage_diagnostics
        retained = safe.get("stage", {})
        safe_claims = safe.get("claims", [])
        safe_verification = verify_agent_output(
            {"claims": safe_claims}, set(evidence_records), language, evidence_records)
        safe_states = {item["index"]: item["state"]
                       for item in safe_verification["claim_verifications"]}
        retained["claims"] = safe_claims
        # Side channels are derived from the already projected claims. A
        # forecast label cannot smuggle a rejected reference into downstream
        # input through assumptions_scenarios after its main claim was removed.
        retained["trusted_channels"] = {
            **_typed_trusted_channels(safe_claims, safe_states), "unresolved": []}
        retained["_section_validation"] = audit
        retained["_verified_claim_count"] = safe_verification["verified_claim_count"]
        retained["_verification_state"] = (
            "completed_verified" if verification.get("passed")
            and safe_verification["verified_claim_count"] and not audit["quarantined_paths"]
            else "completed_partial" if safe.get("stage") or safe_claims
            else "failed_verification")
        return retained
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
    if trusted_claims:
        retained = dict(result)
        retained["claims"] = trusted_claims
        retained["_verification_state"] = "completed_partial"
        retained["_verified_claim_count"] = len(trusted_claims)
        retained["trusted_channels"] = channels
        return retained
    return {
        "claims": [],
        "_verification_state": "failed_verification",
        "_verified_claim_count": 0,
        "trusted_channels": channels,
    }


def _gate_stage_output(
    result: dict[str, Any],
    available: set[str],
    evidence_records: dict[str, dict[str, Any]],
    language: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One local-retention contract for base, opportunity and scenario stages."""
    verification = verify_agent_output(result, available, language, evidence_records)
    trusted = _trusted_stage_result(result, verification, evidence_records, language)
    if trusted.get("_verification_state") != "completed_verified":
        # Protocol validity is not evidence verification. In particular an
        # assumptions-only scenario with no claims is displayable, not a fully
        # verified stage simply because all([]) passed the claims validator.
        verification["passed"] = False
    # The shared projector owns references and per-assertion state. Specialized
    # growth/forecast gates must not delete entire objects for a missing locator.
    if isinstance(result.get("opportunities"), list):
        retained = trusted.get("opportunities", [])
        retained = retained if isinstance(retained, list) else []
        audit = trusted.get("_section_validation", {})
        nodes = audit.get("nodes", [])
        verified = sum(bool(local) and all(node.get("validation_state") == "verified" for node in local)
                       for index in range(len(retained))
                       if (local := [node for node in nodes
                           if str(node.get("path", "")).startswith(f"stage.opportunities[{index}].")]))
        lineage = dict(result.get("_lineage") or {})
        raw_count = lineage.get("raw_candidate_count", len(result["opportunities"]))
        lineage.update({"raw_candidate_count": raw_count,
                        "normalized_count": len(result["opportunities"]),
                        "verified_count": verified, "retained_count": len(retained),
                        "rejected_count": max(0, len(result["opportunities"]) - len(retained)),
                        "rejected_reasons": [item["reason"] for item in audit.get("quarantined_paths", [])]})
        trusted["_lineage"] = lineage
        trusted["_verified_claim_count"] = verified
    return trusted, verification


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
    """Give the skeptical stage claims without serializing evidence twice.

    Canonical evidence remains complete in ``ResearchContext.compact_json``;
    the stage manifest proves its identity and coverage.
    """
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
        def wrapped_size(chunk: Any) -> int:
            return _synthesis_json_bytes({
                "name": str(name), "content": chunk,
                "source_evidence_ids": _collect_evidence_ids(chunk),
            })

        for chunk in _recursive_synthesis_chunks(value, limit, measure=wrapped_size):
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


def _recursive_synthesis_chunks(
    value: Any, budget: int, *, measure: Callable[[Any], int] = _synthesis_json_bytes,
) -> list[Any]:
    """Split complete structured material; source strings retain every character."""
    if measure(value) <= budget:
        return [value]
    if isinstance(value, dict):
        chunks: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        identity_keys = {"evidence_id", "document_id", "content_hash", "company_cik", "source_url",
            "accession_number", "fiscal_year", "fiscal_period", "start_date", "end_date", "period_end",
            "currency", "unit", "scale", "scope", "consolidated_scope", "source_page", "page", "locator",
            "title", "topic", "kind", "statement", "table_id", "column_headers"}
        identity = {key: child for key, child in value.items() if key in identity_keys}
        for key, child in value.items():
            inherited_span = value.get("_source_span")
            inherited_field = inherited_span.get("field") if isinstance(inherited_span, dict) else None
            if key == "_source_span" and identity and isinstance(value.get(inherited_field), str):
                continue  # Emitted with its text, never as a detached metadata packet.
            if (identity and isinstance(child, str) and key not in identity_keys
                    and (measure({str(key): child}) > budget or inherited_field == str(key))):
                if current:
                    chunks.append(current)
                    current = {}
                parent_span = value.get("_source_span")
                parent_span = parent_span if isinstance(parent_span, dict) and parent_span.get("field") == str(key) else {}
                base_offset = parent_span.get("start", 0)
                original_sha = parent_span.get("original_sha256") or hashlib.sha256(child.encode("utf-8")).hexdigest()
                def source_fragment(text, start=0, end=None):
                    return {**identity, str(key): text, "_source_span": {"field": str(key),
                        "start": base_offset + start, "end": base_offset + (len(child) if end is None else end),
                        "total_characters": parent_span.get("total_characters", len(child)), "original_sha256": original_sha}}
                fragments = _source_text_chunks(child, budget,
                    measure=lambda text: measure(source_fragment(text, len(child), len(child))))
                offset = 0
                for fragment in fragments:
                    chunks.append(source_fragment(fragment, offset, offset + len(fragment)))
                    offset += len(fragment)
                continue
            for child_chunk in _recursive_synthesis_chunks(
                child, budget, measure=lambda chunk, field=str(key): measure({field: chunk})
            ):
                # Multiple chunks for one key are alternatives (for example
                # slices of a claims list), not values that may overwrite one
                # another in the same dict.
                if str(key) in current:
                    chunks.append(current)
                    current = {}
                candidate = dict(current)
                candidate[str(key)] = child_chunk
                if current and measure(candidate) > budget:
                    chunks.append(current)
                    current = {str(key): child_chunk}
                elif not current and measure(candidate) > budget:
                    raise SynthesisContextLimitError(
                        [{"name": str(key), "bytes": measure(candidate)}],
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
            for child_chunk in _recursive_synthesis_chunks(
                child, budget, measure=lambda chunk: measure([chunk])
            ):
                candidate = [*current, child_chunk]
                if current and measure(candidate) > budget:
                    chunks.append(current)
                    current = [child_chunk]
                elif not current and measure(candidate) > budget:
                    raise SynthesisContextLimitError(
                        [{"name": "list-item", "bytes": measure(candidate)}],
                        budget,
                    )
                else:
                    current = candidate
        if current:
            chunks.append(current)
        return chunks or [[]]
    if isinstance(value, str):
        return _source_text_chunks(value, budget, measure=measure)
    # A scalar larger than the input budget is indivisible by contract.
    raise SynthesisContextLimitError(
        [{"name": "atomic-value", "bytes": measure(value)}],
        budget,
    )


def _source_text_chunks(value: str, budget: int, *, measure) -> list[str]:
    """Prefer page/paragraph/row/sentence seams, with exact ordered UTF-8 replay.

    No strip, character cap, summarization or dropped suffix. Long unbroken
    prose can split at a codepoint seam, but a numeric literal is never cut.
    """
    result, start = [], 0
    while start < len(value):
        low, high = start, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if measure(value[start:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        end = low
        if end < len(value):
            seams = [match.end() + start for match in re.finditer(r"\n\s*\n|\n|[。！？；.!?;](?:\s|$)|\s+", value[start:end])]
            preferred = [position for position in seams if position >= start + (end - start) // 2]
            if preferred:
                end = preferred[-1]
            while (end > start and end < len(value) and value[end - 1] in "0123456789.,+-"
                   and value[end] in "0123456789.,+-"):
                end -= 1
        if end <= start:
            raise SynthesisContextLimitError([{"name": "indivisible-source-context-or-quantity",
                "bytes": measure(value[start:])}], budget)
        result.append(value[start:end])
        start = end
    return result


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


def _isolate_unknown_evidence_references(
    value: Any, allowed: set[str],
) -> tuple[Any, list[str]]:
    """Isolate only claim-like nodes with references outside a partition."""

    isolated: list[str] = []

    def visit(item: Any) -> Any:
        if isinstance(item, list):
            kept: list[Any] = []
            for child in item:
                safe = visit(child)
                if safe is not None:
                    kept.append(safe)
            return kept
        if not isinstance(item, dict):
            return item
        cited = item.get("evidence_ids")
        if isinstance(cited, list):
            unknown = [str(ref) for ref in cited if str(ref) not in allowed]
            if unknown:
                isolated.extend(unknown)
                return None
        return {key: visit(child) for key, child in item.items()}

    return visit(value), list(dict.fromkeys(isolated))


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
        try:
            return [_lossless_synthesis_value(item, seen) for item in value]
        finally:
            seen.remove(id(value))
    if isinstance(value, tuple):
        return [_lossless_synthesis_value(item, seen) for item in value]
    if isinstance(value, dict):
        if id(value) in seen:
            return "[cyclic synthesis value]"
        seen.add(id(value))
        try:
            return {
                str(key): _lossless_synthesis_value(item, seen)
                for key, item in value.items()
                if not str(key).startswith("_") and key not in {"raw_response", "prompt", "trusted_channels"}
            }
        finally:
            seen.remove(id(value))
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
        self.stage_registry = standard_model_registry()
        self._model_context = threading.local()
        self.manual_authorization_id = ""
        self.local_revalidation = False
        self.budget_gap_resume = False
        # A model's request ceiling is shared across simultaneous company runs
        # in this process. The graph still opts into one or two workers per run;
        # the scheduler's adaptive 429 handling can lower this shared ceiling.
        self.model_scheduler = shared_model_scheduler(
            model_config.connection_id or model_config.configured_model_id,
            limit=2,
        )

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
        *,
        run: ResearchRun | None = None,
    ) -> dict[str, Any]:
        """Produce synthesis from complete material using adaptive requests.

        Input refusal and output exhaustion are different recovery conditions.
        A received length-cut body is retained as partial, never mislabeled as
        an input-context refusal or certified complete because keys exist.
        """
        projected = _synthesis_prior_artifacts(dossier, growth, skeptic, forecast)
        context_json = context.compact_json()
        result = self._run_agent_adaptive(
            "research-synthesizer",
            "prompts/research-synthesizer.md",
            context_json,
            projected,
            run=run,
        )
        from .research_execution.model_output import response_refused
        if response_refused(result) or result.get("_response_error") == "provider_refusal":
            return result
        meta = result.get("_response_meta") if isinstance(result, dict) else None
        finish_reason = str(meta.get("finish_reason") or "").casefold() if isinstance(meta, dict) else ""
        if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
            result = {**result, "_output_incomplete": {
                "code": "MODEL_OUTPUT_BUDGET_EXHAUSTED", "state": "pending"}}
        if result.get("_output_incomplete"):
            result = self._recover_synthesis_output_sections(context_json, projected, result, run=run)
        from .research_execution.analysis_references import resolve_references
        result = resolve_references(result, projected)
        return result

    def _recover_synthesis_output_sections(self, context_json: str, projected: dict[str, Any],
            original: dict[str, Any], *, run: ResearchRun | None = None) -> dict[str, Any]:
        """One targeted supplement; never rewrite closed sections or loop.

        Missing objects are named before dispatch. An open partial object is
        retained for inspection, not overwritten by a guessed continuation.
        The request is charged to the same per-stage/run repair allowance.
        """
        sections = list(dict.fromkeys([*(key for key in REQUIRED_REPORT_SECTIONS if key != "claims"),
            *sorted({path.split(".")[0] for item in ResearchCoverageContract.obligations
                     for path in item.paths if path.split(".")[0] != "claims"})]))
        result = deepcopy(original)
        from .research_execution.executor import dependency_findings
        from .report_readiness import section_has_substance
        prior_findings = dependency_findings(original)
        issues = original.get("_output_normalization_issues", [])
        open_paths = [item.get("path", "") for item in issues if isinstance(item, dict)
                      and item.get("code") == "MODEL_OUTPUT_CONTAINER_INCOMPLETE"]
        states = [{"section": section, "complete": section_has_substance(section, original.get(section))
                   and not any(path.startswith("$." + section) for path in open_paths)} for section in sections]
        missing = [section for section in sections if section not in original]
        parts = []
        if missing:
            self._check_cancelled()
            try:
                part = self._run_agent_adaptive("research-synthesizer-missing-sections",
                    "prompts/research-synthesizer.md", context_json,
                    {**projected, "received_synthesis_findings": prior_findings,
                     "sectional_delivery": {"schema": "research-section-delivery-v2-targeted",
                         "sections": missing, "all_sections": sections}},
                    run=run, role_id="research-synthesizer", output_schema="research-report-section-v1")
                from .research_execution.model_output import response_refused
                meta = part.get("_response_meta", {})
                finished = (not part.get("_response_error") and not part.get("_output_incomplete")
                    and not response_refused(part) and str(meta.get("finish_reason", "")).casefold()
                    not in {"length", "max_tokens", "max_output_tokens"})
                parts.append(part)
                for state in states:
                    section = state["section"]
                    if section in missing:
                        if part.get(section) not in (None, "", [], {}):
                            result[section] = deepcopy(part[section])
                        state.update(complete=finished and section_has_substance(section, part.get(section)),
                                     candidate_sha256=recovery_digest(part))
            except (ProviderError, SynthesisContextLimitError) as exc:
                for state in states:
                    if state["section"] in missing:
                        state["code"] = exc.code
        result["claims"] = _collect_stage_claims(original, *parts)
        complete = bool(missing) and bool(states) and all(item["complete"] for item in states)
        result["_sectional_delivery"] = {"schema": "research-section-delivery-v1", "complete": complete,
            "original_candidate_sha256": recovery_digest(original), "sections": states}
        if complete:
            # This is a host-assembled candidate, not the original truncated
            # provider response. Never fabricate a supplier finish/usage receipt.
            for key in ("_response_meta", "_response_error", "_output_incomplete"):
                result.pop(key, None)
            result["structured_output_valid"] = True
        else:
            result["_output_incomplete"] = {"code": "MODEL_OUTPUT_BUDGET_EXHAUSTED", "state": "pending"}
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
        valuation: dict[str, Any] | None = None,
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
        metric_rows = []
        for metric in financial_metrics or []:
            try:
                identity = read_metric_period(metric)
            except (ValueError, TypeError, AttributeError):
                continue
            metric_rows.append((identity, metric))
        latest_metric = max(
            metric_rows,
            key=lambda row: (row[0].year, row[0].period_end),
            default=None,
        )
        metric_values: dict[str, Any] = {}
        if latest_metric is not None:
            identity, latest_metric = latest_metric
            metric_values.update(identity.serialize())
            for key in ("assets", "liabilities", "equity", "total_equity"):
                value = latest_metric.get(key)
                if value is not None:
                    metric_values[key] = value
        if any(key in metric_values for key in ("assets", "liabilities", "equity", "total_equity")):
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
        report = {
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
            # A synthesis failure does not erase the deterministic engine's
            # already-computed reverse DCF. Empty analysis remains a notice,
            # never a claim that valuation research was completed.
            "implied_expectations": {},
            "valuation_and_sensitivity": {},
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
        return enforce_valuation_consistency(report, valuation, self.report_language)[0]

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise ResearchCancelled()

    def _production_graph_bindings(self) -> GraphBindings:
        return GraphBindings(
            system_prompt=CORE_SYSTEM_PROMPT,
            coverage_version=RESEARCH_COVERAGE_VERSION,
            verification_version=RESEARCH_VERIFICATION_VERSION,
            model_role_version=DECLARED_MODEL_ROLE_VERSION,
            required_synthesis_sections=_REQUIRED_SYNTHESIS_SECTIONS,
            verify_agent_output=verify_agent_output,
            trusted_stage_result=_trusted_stage_result,
            gate_stage_output=_gate_stage_output,
            skeptical_prior_artifacts=_skeptical_prior_artifacts,
            validate_research_synthesis=validate_research_synthesis,
            collect_stage_claims=_collect_stage_claims,
            presentation_stage_value=_presentation_stage_value,
            cancelled_error=ResearchCancelled,
        )

    def _run_declarative_ot_workflow(
        self,
        run: ResearchRun,
        context: ResearchContext,
        evidence: list[dict[str, Any]],
        notify: ProgressCallback,
        publication: ResearchPublicationSnapshot,
    ) -> ResearchRun:
        return run_declarative_ot_workflow(
            self, self._production_graph_bindings(), run, context, evidence, notify, publication
        )

    def _execute_official_model_graph(
        self,
        context: ResearchContext,
        run: ResearchRun,
        artifacts: list[dict[str, Any]],
        *,
        publication: ResearchPublicationSnapshot,
        resume_target: str = "model-stages",
        notify: ProgressCallback | None = None,
        stage_targets: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, StageResult], ResearchPublicationSnapshot]:
        return execute_official_model_graph(
            self, self._production_graph_bindings(), context, run, artifacts,
            publication=publication, resume_target=resume_target, notify=notify, stage_targets=stage_targets,
        )

    def prefetch_source_stages(self, run: ResearchRun, evidence: list[dict[str, Any]]) -> None:
        """Same-run source-only stages; no final report or new lifecycle.

        The orchestrator waits before publishing the later financial snapshot.
        The full graph then reuses the exact source-scoped checkpoint/request.
        """
        if self.provider is None or self.pack.pack_id != "official.long-term-fundamentals":
            return
        context = ResearchContext(run.company, [], [], [], deepcopy(evidence))
        source_inputs = _source_business_inputs({}, context.compact_json(), evidence)
        context.evidence = json.loads(source_inputs["context_json"])["evidence"]
        targets = tuple(spec.stage_id for spec in self.stage_registry.stages
                        if spec.stage_id in SOURCE_ANALYSIS_STAGES and not spec.depends_on)
        if not targets:
            return
        publication = self.storage.capture_research_publication(run)
        self._execute_official_model_graph(context, run, self.storage.get_artifacts(run.run_id),
            publication=publication, stage_targets=targets)

    def _finish_official_model_graph(
        self,
        run: ResearchRun,
        context: ResearchContext,
        graph: dict[str, StageResult],
        *,
        publication: ResearchPublicationSnapshot,
        notify: ProgressCallback,
    ) -> ResearchRun:
        """Persist the public dossier/report from one verified graph result."""
        base_ids = tuple(
            spec.stage_id for spec in self.stage_registry.stages if not spec.depends_on
        )
        stage_results = {
            stage_id: graph[stage_id].validation.trusted_output
            for stage_id in base_ids
            if stage_id in graph and graph[stage_id].usable
        }
        stage_verifications = {
            stage_id: graph[stage_id].validation.record.get("verification", {})
            for stage_id in stage_results
            if isinstance(graph[stage_id].validation.record, dict)
        }
        stage_failures = {
            stage_id: graph[stage_id]
            for stage_id in base_ids
            if stage_id in graph and not graph[stage_id].usable
        }
        dossier = {
            "company": context.company.to_dict(),
            "metrics": context.metrics[:5],
            "analyses": stage_results,
            "verified_analyses": stage_results,
            "unverified_supplement": [
                {
                    "agent_id": agent_id,
                    "state": value.get("_verification_state", "completed_verified"),
                    "claim_verifications": stage_verifications.get(agent_id, {}).get(
                        "claim_verifications", []
                    ),
                    "issues": stage_verifications.get(agent_id, {}).get("issues", []),
                }
                for agent_id, value in stage_results.items()
                if value.get("_verification_state") not in (None, "completed_verified")
            ],
        }
        self._save(
            run,
            "verified-research-dossier",
            self._report_text("经过验证的研究档案", "Verified Research Dossier"),
            dossier,
            agent_id="evidence-verifier",
        )

        if not stage_results:
            failed = list(stage_failures.values())
            retryable = bool(failed) and all(
                item.retryable or item.validation.state == "failed_verification"
                for item in failed
            )
            stage_outcome = self._artifact(
                run,
                "stage-outcome",
                self._report_text("基础研究阶段未完成", "Base research stage incomplete"),
                {
                    "stage": "base-agents",
                    "outcome": "waiting_retryable" if retryable else "needs_action",
                    "retryable": retryable,
                    "completed": 0,
                    "required": len(base_ids),
                    "failed_agents": tuple(sorted(stage_failures)),
                    "error_codes": tuple(sorted({
                        item.error_code or (item.validation.issues[0] if item.validation.issues else "MODEL_RESPONSE_INVALID")
                        for item in failed
                    })),
                    "next_action": "resume_failed_model_stages",
                },
                agent_id="research-continuity",
            )
            # Optional viewpoints can fail while independently sourced later
            # stages succeed. Preserve the outcome, then assemble all trusted
            # content locally rather than returning before inspecting it.
            self.storage.save_artifact(stage_outcome)

        growth_result = graph.get("growth-opportunities")
        counter_result = graph.get("counter-analysis")
        forecast_result = graph.get("forecast-scenarios")
        synthesis_result = graph.get("final-synthesis")
        growth = growth_result.validation.trusted_output if growth_result and growth_result.usable else {}
        skeptic = counter_result.validation.trusted_output if counter_result and counter_result.usable else {}
        forecast = forecast_result.validation.trusted_output if forecast_result and forecast_result.usable else {}
        if synthesis_result and synthesis_result.has_candidate and isinstance(synthesis_result.candidate, dict):
            synthesis = deepcopy(synthesis_result.candidate)
        else:
            error_code = (
                synthesis_result.error_code
                if synthesis_result is not None
                else "STAGE_DEPENDENCY_UNAVAILABLE"
            )
            retryable = bool(synthesis_result and synthesis_result.retryable)
            synthesis = {
                "claims": [],
                "_response_error": "provider_error",
                "_provider_error_code": error_code,
                "_provider_retryable": retryable,
                "_context_counting_mode": "stage_not_completed",
            }

        available = {
            str(item.get("evidence_id"))
            for item in context.evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
        evidence_records = {
            str(item.get("evidence_id")): item
            for item in context.evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
        context_budget_exceeded = synthesis.get("_response_error") == "context_budget_exceeded"
        valuation_audit = {"valuation_available": False, "quarantined_count": 0}
        if synthesis.get("_response_error") is None:
            synthesis, valuation_audit = enforce_valuation_consistency(
                deepcopy(synthesis), context.valuation, self.report_language
            )
        persisted_verification = (synthesis_result.validation.record.get("verification")
                                  if synthesis_result and not synthesis.get("_response_error") else None)
        verification = (deepcopy(persisted_verification) if isinstance(persisted_verification, dict)
                        else validate_research_synthesis(
                            synthesis, available, self.report_language, evidence_records, coverage_context=context))
        verification["valuation_consistency"] = valuation_audit
        if stage_failures:
            verification["issues"] = list(dict.fromkeys([
                *verification.get("issues", []),
                "Base research stages incomplete: " + ", ".join(sorted(stage_failures)),
            ]))
            verification["passed"] = False

        report_payload: dict[str, Any] = deepcopy(verification.get("trusted_report", {}))
        report_mode = "synthesized"
        response_error = synthesis.get("_response_error")
        if response_error or not verification.get("claim_integrity_passed"):
            # Stage and synthesis nodes were validated at their commit seam.
            # Read those exact proofs; do not revalidate assembled stage prose
            # or let another bad Claim taint an already committed sibling.
            from .report_content import assemble_trusted_report_payload
            local = assemble_trusted_report_payload(self.storage.get_artifacts(run.run_id),
                run_id=run.run_id, input_digest=publication.input_digest)
            report_mode = "staged-fallback" if response_error == "insufficient_material" else "synthesis-incomplete"
            # A failed claim cannot replace generated synthesis with a staged
            # template. Fill genuinely absent sections only; preserve the
            # synthesis' independently bound nodes and its original wording.
            section_audit = verification.setdefault("section_validation",
                {"schema": SECTION_PROOF_VERSION, "nodes": []})
            if local:
                for key, body in local["report"].items():
                    if key.startswith("_") or key in {"claims", "research_complete", "cross_section_synthesis_status"}:
                        continue
                    if report_payload.get(key) not in (None, "", [], {}):
                        continue
                    report_payload[key] = deepcopy(body)
                    section_audit.setdefault("nodes", []).extend(
                        deepcopy(node) for node in local["verification"]["section_validation"].get("nodes", ())
                        if node.get("section_id") == key)
            report_payload.setdefault("claims", [])
            # The engine owns these computed fields. Project only its output,
            # never any provider-written valuation prose, once at this seam.
            valuation_fields = enforce_valuation_consistency(
                {"implied_expectations": {}, "valuation_and_sensitivity": {}}, context.valuation, self.report_language)[0]
            computed, computed_audit = _supported_synthesis_sections(
                valuation_fields, available, evidence_records, self.report_language, valuation=context.valuation)
            for key in ("implied_expectations", "valuation_and_sensitivity"):
                if key in computed and key not in report_payload:
                    report_payload[key] = computed[key]
                    verification["section_validation"]["nodes"].extend(
                        node for node in computed_audit.get("nodes", ()) if node["section_id"] == key)
            report_payload["research_complete"] = False
            report_payload["cross_section_synthesis_status"] = {
                "context_budget_exceeded": "not_completed_context_capacity",
                "provider_error": "not_completed_provider_error",
                "insufficient_material": "not_completed_insufficient_material",
            }.get(response_error, "generated_pending_verification" if not response_error and report_payload
                  else "not_completed_invalid_output")
            if context_budget_exceeded:
                report_payload["context_budget"] = {
                    "required_bytes": synthesis.get("_context_required_bytes"),
                    "available_bytes": synthesis.get("_context_available_bytes"),
                    "counting_mode": synthesis.get("_context_counting_mode")}
            if response_error == "provider_error":
                report_payload["provider_error_code"] = synthesis.get("_provider_error_code")
                report_payload["provider_retryable"] = synthesis.get("_provider_retryable")
        readiness_artifacts = self.storage.get_artifacts(run.run_id)
        coverage = ResearchCoverageContract().assess(
            report_payload, verification, evidence_records=evidence_records, metrics=context.metrics,
            valuation=context.valuation, company=context.company.to_dict(),
            financial_period_coverage=context.financial_period_coverage)
        readiness = assess_report_readiness(
            report_payload,
            verification,
            missing_stages=missing_research_stages(readiness_artifacts),
            financial_evidence_count=financial_source_evidence_count(readiness_artifacts),
            allow_complete=report_mode == "synthesized",
            coverage=coverage,
        )
        verification["report_readiness"] = readiness.to_dict()
        if not readiness.complete:
            verification["passed"] = False
            if report_mode == "synthesized":
                report_mode = "synthesis-incomplete"
        report_payload["research_complete"] = bool(readiness.complete)
        if not readiness.complete:
            report_payload.setdefault("cross_section_synthesis_status", "generated_pending_verification")
        diagnostics = _response_diagnostics(synthesis)
        if synthesis.get("_response_error") == "context_budget_exceeded":
            diagnostics.update({
                "required_bytes": synthesis.get("_context_required_bytes"),
                "available_bytes": synthesis.get("_context_available_bytes"),
                "counting_mode": synthesis.get("_context_counting_mode"),
            })
        synthesis_lineage = _synthesis_lineage(
            synthesis, verification, report_payload, dossier, growth, skeptic, forecast
        )
        report_artifact = self._artifact(
            run,
            "research-report",
            self._report_text("完整长期研究报告", "Complete Long-term Research Report"),
            {
                "mode": report_mode,
                "research_coverage_contract": RESEARCH_COVERAGE_VERSION,
                "publication_input_digest": publication.input_digest,
                "report": report_payload,
                "verification": verification,
                "readiness": readiness.to_dict(),
                "retryable": not verification["passed"],
                "diagnostics": diagnostics,
                "lineage": synthesis_lineage,
            },
            agent_id="research-synthesizer",
        )
        if not verification["passed"]:
            self._publish_report_result(run, publication, report_artifact, complete=False)
            notify(self._progress_text("综合报告生成不完整；已保留阶段研究结果，可稍后重试综合"), 100)
            return run
        thesis_content = {
            "thesis": synthesis.get("thesis"),
            "claims": synthesis.get("claims", []),
            "invalidation_conditions": synthesis.get("invalidation_conditions", []),
            "leading_indicators": synthesis.get("leading_indicators", []),
            "unresolved_questions": synthesis.get("unresolved_questions", []),
            "growth_opportunities": growth.get("opportunities", growth),
            "scenarios": forecast.get("scenarios", forecast),
        }
        self._publish_report_result(run, publication, report_artifact,
            complete=True, thesis_content=thesis_content)
        notify(self._progress_text("研究完成"), 100)
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
        existing_run: ResearchRun | None = None,
        resume_artifacts: list[dict[str, Any]] | None = None,
        resume_target: str = "model-stages",
    ) -> ResearchRun:
        notify = progress or (lambda _message, _percent: None)
        reproducibility = reproducibility or {}
        profile = facts if isinstance(facts, FinancialProfile) else None
        if profile is not None:
            facts = list(profile.fact_dicts)
        run = existing_run or ResearchRun(
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
        previous_publication = (
            self.storage.capture_research_publication(run)
            if existing_run is not None and self.storage.get_run(run.run_id) is not None else None)
        # A service-created run envelope is already durable before external
        # discovery/download.  Reusing it here prevents a second lifecycle and
        # preserves one report identity through every recovery path.
        run.status = RunStatus.RUNNING
        run.company = company
        run.report_language = self.report_language
        run.provider_id, run.model_id = self.model_config.provider, self.model_config.model
        run.model_configuration = {
            "configured_model_id": self.model_config.configured_model_id,
            "configuration_version": self.model_config.configuration_version,
            "role": self.model_config.role,
        }
        run.research_configuration = {
            **deepcopy(run.research_configuration),
            **deepcopy(reproducibility.get("research_configuration", {})),
            "report_language": self.report_language, "parallel_agents": self.parallel_agents,
            "research_pack_id": self.pack.pack_id, "research_pack_version": self.pack.version,
            "research_pack_content_identity": self.pack.content_hash,
        }
        run.market_snapshot = deepcopy(market_snapshot)
        run.data_snapshot = deepcopy(reproducibility.get("data_snapshot", {}))
        publication = self.storage.begin_research_publication(run, expected=previous_publication)
        try:
            self._check_cancelled()
            metrics = calculate_metrics(facts)
            interim_metrics = calculate_interim_metrics(facts)
            source_records = tuple(ref.to_dict() for ref in profile.source_evidence) if profile is not None else ()
            evidence = build_fact_evidence(facts, source_records=source_records)
            if profile is not None:
                evidence.extend(build_fact_evidence([item.to_dict() for item in profile.disclosure_facts],
                    source_records=source_records))
                from .research_source_context import observation_source_record
                evidence.extend(record for item in profile.repair_observations
                    if (record := observation_source_record(item)) is not None)
            evidence.extend(filing_evidence or [])
            valuation = None
            snapshot_currency = str((market_snapshot or {}).get("valuation_currency") or (market_snapshot or {}).get("currency", ""))
            # Explicit manual valuation inputs are a separate user-approved
            # lane. A supplied but unclassified market snapshot is not verified.
            snapshot_status = str((market_snapshot or {}).get("status", "UNRESOLVED" if market_snapshot else "MANUAL")).upper()
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
            elif valuation_inputs and (not company.reporting_currency or snapshot_status not in {"VERIFIED", "MANUAL"}):
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
            from .financials import fcf_sensitivity_analysis
            if company.industry_support != "financial_beta":
                sensitivity = fcf_sensitivity_analysis(metrics, currency=company.reporting_currency,
                    discount_rate=(valuation_inputs or {}).get("discount_rate", .10),
                    terminal_growth=(valuation_inputs or {}).get("terminal_growth", .03),
                    horizon_years=(valuation_inputs or {}).get("horizon_years", 5))
                valuation = {**(valuation or {"status": "missing_market_price", "currency": company.reporting_currency}),
                             "intrinsic_analysis": sensitivity}
            context = ResearchContext(
                company,
                facts,
                metrics,
                interim_metrics,
                evidence,
                valuation,
                market_snapshot,
                dict(profile.period_coverage) if profile is not None else {},
                list(profile.repair_observations) if profile is not None else [],
                research_as_of=run.data_as_of,
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
                    "company": {
                        "name": company.name,
                        "ticker": company.ticker,
                        "cik": company.cik,
                        "market": company.market,
                        "exchange": company.exchange,
                        "industry_support": company.industry_support,
                    },
                    "markdown": summary,
                    "metrics": metrics,
                    "interim_metrics": interim_metrics,
                    "evidence": evidence,
                    "repair_observations": context.repair_observations,
                    "currency": company.reporting_currency,
                    "financial_generations": dict(
                        run.data_snapshot.get("financial_generations", {})
                    ),
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
            if self.provider is None and not self.local_revalidation:
                report_artifact = self._artifact(
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
                self._publish_report_result(run, publication, report_artifact, complete=False)
                notify(
                    self._progress_text(
                        "基础财务分析完成；配置模型后可运行完整研究"
                    ),
                    100,
                )
                return run

            if self.pack.pack_id != "official.long-term-fundamentals":
                return self._run_declarative_ot_workflow(run, context, evidence, notify, publication)

            base_total = sum(not spec.depends_on for spec in self.stage_registry.stages)
            notify(
                (
                    f"Running base agents in parallel (0/{base_total})"
                    if self.ui_language == EN and self.parallel_agents
                    else f"Running base agents sequentially (0/{base_total})"
                    if self.ui_language == EN
                    else f"正在並行執行基礎 Agent（0/{base_total}）"
                    if self.ui_language == ZH_HANT and self.parallel_agents
                    else f"正在依序執行基礎 Agent（0/{base_total}）"
                    if self.ui_language == ZH_HANT
                    else f"正在并行运行基础 Agent（0/{base_total}）"
                    if self.parallel_agents
                    else f"正在按顺序运行基础 Agent（0/{base_total}）"
                ),
                25,
            )
            graph_artifacts = (
                resume_artifacts
                if resume_artifacts is not None
                else self.storage.get_artifacts(run.run_id)
            )
            graph, publication = self._execute_official_model_graph(
                context,
                run,
                graph_artifacts,
                publication=publication,
                resume_target=resume_target,
                notify=notify,
            )
            return self._finish_official_model_graph(
                run, context, graph, publication=publication, notify=notify,
            )
        except ResearchPublicationConflict:
            # Another owner changed the inputs/head. Never restore this
            # worker's old payload, or mark that other generation failed.
            raise
        except ResearchCancelled as exc:
            exc.run_id = run.run_id
            payload = self.storage.save_run_stage_result(run.run_id,
                StageAttempt(run.run_id, "report-rendering", StageOutcome.CANCELLED,
                    error_code="RESEARCH_CANCELLED"), status=RunStatus.CANCELLED,
                error=str(exc), completed_at=utc_now_iso())
            self._apply_run_lifecycle(run, payload)
            raise
        except Exception as exc:
            payload = self.storage.save_run_stage_result(run.run_id,
                StageAttempt(run.run_id, "report-rendering", StageOutcome.FAILED_INTERNAL,
                    error_code="RESEARCH_EXECUTION_FAILED"), status=RunStatus.FAILED,
                error="RESEARCH_EXECUTION_FAILED", completed_at=utc_now_iso())
            self._apply_run_lifecycle(run, payload)
            raise

    def retry_synthesis(
        self,
        run: ResearchRun,
        artifacts: list[dict[str, Any]],
        facts: list[dict[str, Any]],
    ) -> ResearchRun:
        """Invalidate final synthesis and run it through the shared model graph."""
        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        deterministic = _latest_artifact(artifacts, "deterministic-financial-summary")
        if deterministic is None:
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
            (deterministic_content.get("financial_quality") or {}).get("period_coverage", {}),
            deterministic_content.get("repair_observations", []),
            research_as_of=run.data_as_of,
        )
        publication = self._capture_resume_publication(run)
        graph, publication = self._execute_official_model_graph(
            context, run, artifacts, publication=publication, resume_target="final-synthesis"
        )
        return self._finish_official_model_graph(
            run,
            context,
            graph,
            publication=publication,
            notify=lambda _message, _percent: None,
        )

    def retry_growth(
        self,
        run: ResearchRun,
        artifacts: list[dict[str, Any]],
        facts: list[dict[str, Any]],
    ) -> ResearchRun:
        """Invalidate growth plus DAG descendants; retain independent base stages."""
        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        deterministic = _latest_artifact(artifacts, "deterministic-financial-summary")
        if deterministic is None:
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
            (deterministic_content.get("financial_quality") or {}).get("period_coverage", {}),
            deterministic_content.get("repair_observations", []),
            research_as_of=run.data_as_of,
        )
        publication = self._capture_resume_publication(run)
        graph, publication = self._execute_official_model_graph(
            context, run, artifacts, publication=publication, resume_target="growth-opportunities"
        )
        return self._finish_official_model_graph(
            run,
            context,
            graph,
            publication=publication,
            notify=lambda _message, _percent: None,
        )

    def _set_agent_state(self, agent_id: str, state: str) -> None:
        journal = getattr(self._model_context, "journal", None)
        scope = journal.capture_scope() if journal is not None else None
        if scope:
            if scope[0] in SOURCE_ANALYSIS_STAGES:
                spec = self.stage_registry.get(scope[0])
                if agent_id == spec.role:
                    agent_id = spec.stage_id
            # OT stages have their own registry, not the official stage IDs.
            # Generation receipt is never the terminal stage publication.
            # The executor will announce completed_* after validation.
            if state == "completed":
                state = "validating"
        self.agent_progress(agent_id, state)

    def _model_stage_executor(self) -> ModelStageExecutor:
        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        return ModelStageExecutor(self.provider, self.model_scheduler, cancel_check=self.cancel_check,
                                  request_journal=getattr(self._model_context, "journal", None))

    @contextmanager
    def _bind_request_journal(self, journal: ModelRequestJournal):
        # Parallel stages and concurrent runs must not overwrite another
        # worker's journal. Bind before the model executor captures its scope.
        previous = getattr(self._model_context, "journal", None)
        self._model_context.journal = journal
        try:
            yield
        finally:
            self._model_context.journal = previous

    def _new_request_journal(self, run: ResearchRun) -> ModelRequestJournal:
        journal = ModelRequestJournal(self.storage, run.run_id, self.model_config.public_id,
                                   timeout_seconds=self.model_config.timeout_seconds,
                                   cancel_check=self.cancel_check,
                                   manual_authorization_id=self.manual_authorization_id)
        journal.budget_only = self.budget_gap_resume
        journal.hydrate_input_budget(self.provider)
        return journal

    def _generate_with_cancellation(
        self, system_prompt: str, user_prompt: str, *, json_mode: bool = True,
    ) -> dict[str, Any]:
        # Compatibility entry for coverage partitions and their consolidation.
        # There is one provider-call implementation for every model stage.
        try:
            return self._model_stage_executor().execute(
                ModelStageRequest("material-coverage", system_prompt, user_prompt, json_mode,
                                  purpose="material_coverage",
                                  max_output_tokens=self._stage_output_budget("material-coverage")),
            )
        except ModelStageCancelled:
            raise ResearchCancelled() from None

    def _run_agent(
        self, agent_id: str, prompt_path: str, context_json: str,
        prior_artifacts: dict[str, Any], *, enforce_local_budget: bool = True,
        material_adequacy_override: dict[str, Any] | None = None,
        role_id: str | None = None, output_schema: str | None = None,
    ) -> dict[str, Any]:
        self._check_cancelled()
        executor = self._model_stage_executor()
        role = _agent_role(agent_id, role_id)
        gate = material_adequacy_override or route_materials(
            json.loads(context_json), role
        )["material_adequacy"]
        system_prompt, user_prompt = _agent_prompt_bundle(
            agent_id, self.pack.prompt(prompt_path), self.report_language,
            context_json, prior_artifacts,
            role_id=role_id, output_schema=output_schema,
        )
        request = ModelStageRequest(
            agent_id, system_prompt, user_prompt,
            max_input_bytes=(provider_context_capability(self._stage_budget_provider(agent_id)).max_input_bytes
                             if enforce_local_budget else None),
            output_contract=_agent_output_contract(role, output_schema),
            claim_repair_contract=self._claim_repair_contract(context_json) if role == "research-synthesizer" else None,
            max_output_tokens=self._stage_output_budget(agent_id),
            purpose="section_delivery" if output_schema == "research-report-section-v1" else "generation",
        )
        try:
            return executor.execute(request, material_adequacy=gate, language=self.report_language)
        except ModelStageCancelled:
            raise ResearchCancelled() from None

    def _stage_output_budget(self, agent_id: str) -> int:
        # A per-call allowance, never a report length cap. Truncation is not
        # completion; the adaptive coverage path retains received candidates.
        synthesis = "synthes" in agent_id
        extended = any(key in agent_id for key in ("growth", "skeptic", "forecast"))
        desired = 131_072 if synthesis else 98_304 if extended else 65_536
        # Even without a user hint the planner has a provisional context. Do
        # not reserve more than that whole window and collapse recovery to a
        # one-byte input allowance. This is planning, not a vendor maximum;
        # the host applies only officially supported model-specific limits.
        capacity = provider_context_capability(self.provider).max_input_tokens
        return min(desired, max(1024, capacity // (2 if synthesis else 3 if extended else 4)))

    def _stage_budget_provider(self, agent_id: str):
        scope = getattr(self.provider, "for_output_budget", None)
        return scope(self._stage_output_budget(agent_id)) if callable(scope) else self.provider

    def _claim_repair_contract(self, context_json: str) -> ClaimRepairContract:
        context = json.loads(context_json)
        records = {item["evidence_id"]: item for item in context.get("evidence", [])
                   if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)}

        def repair_material(targets):
            from .research_source_context import route_repair_observations
            selected, catalog = route_repair_observations(context.get("repair_observations", ()),
                [{"evidence_id": ref} for target in targets for ref in target.evidence_ids])
            return {"sources": [deepcopy(records[ref]) for ref in sorted({ref for target in targets
                        for ref in target.evidence_ids}) if ref in records],
                    "company": context.get("company", {}),
                    "targeted_repair_observations": selected, "repair_catalog": catalog,
                    "allowed_uses": ["repair_mapping"], "financial_admission": False,
                    "instruction": "Observations may locate a damaged source cell. They are not accepted financial facts. A mapping must return through the financial compiler; a claim patch cannot admit or calculate from them."}
        return ClaimRepairContract(lambda output: verify_agent_output(
            output, set(records), self.report_language, records), RESEARCH_VERIFICATION_VERSION,
            repair_material=repair_material)

    def _coverage_capsule(
        self,
        *,
        agent_id: str,
        role_prompt: str,
        packet: dict[str, Any],
        index: int,
        total: int,
        capability: ProviderContextCapability,
        role_id: str | None = None, output_schema: str | None = None,
        refinement: int = 0,
    ) -> dict[str, Any]:
        """Read one complete material partition without pretending it is a final stage."""

        packet_json = json.dumps(
            packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        packet_digest = hashlib.sha256(packet_json.encode("utf-8")).hexdigest()
        system_prompt = (
            CORE_SYSTEM_PROMPT
            + "\n"
            + OUTPUT_LANGUAGE_INSTRUCTIONS[self.report_language]
            + "\nThis is a material-coverage pass, not the final analysis. Preserve every "
            "decision-relevant fact, risk, uncertainty, number and evidence ID from the "
            "supplied partition. Return JSON with claims, observations, risks, unknowns, "
            "and source_evidence_ids. Do not infer from missing partitions."
        )
        user_prompt = json.dumps(
            {
                "target_agent": agent_id,
                **({"target_role": role_id} if role_id is not None else {}),
                **({"output_schema": output_schema} if output_schema is not None else {}),
                "target_instructions": role_prompt,
                "partition": {"index": index, "total": total, "sha256": packet_digest},
                "material": packet,
            },
            ensure_ascii=False,
        )
        input_size = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
        if input_size > capability.max_input_bytes:
            raise SynthesisContextLimitError(
                [{"name": f"{agent_id}:partition:{index}", "bytes": input_size}],
                capability.max_input_bytes,
            )
        refine_material = False
        try:
            content = self._generate_with_cancellation(system_prompt, user_prompt, json_mode=True)
        except ProviderError as exc:
            if exc.code not in {"MODEL_CONTEXT_CAPACITY", "MODEL_REQUEST_BODY_TOO_LARGE"} or refinement >= 8:
                raise
            refine_material = True
            content = {}
        from .research_execution.model_output import response_refused
        if response_refused(content) or content.get("_response_error") == "provider_refusal":
            raise ProviderError("Provider refused this material partition", code="MODEL_PROVIDER_REFUSAL", retryable=False)
        meta = content.get("_response_meta", {}) if isinstance(content, dict) else {}
        output_incomplete = (isinstance(content.get("_output_incomplete"), dict)
            or isinstance(meta, dict) and str(meta.get("finish_reason") or "").casefold()
               in {"length", "max_tokens", "max_output_tokens"})
        if output_incomplete and refinement < 8:
            refine_material = True
        children_complete = True
        if refine_material:
            # A confirmed refusal has no unknown remote result. Split the
            # original packet, not a lossy summary. Output exhaustion can also
            # need smaller material obligations, not an identical paid retry.
            refined = ProviderContextCapability(capability.max_input_tokens,
                capability.reserved_output_tokens, capability.reserved_repair_tokens,
                max(1, min(capability.max_input_bytes - 1, input_size // 2)), capability.bytes_per_token)
            overhead = len(system_prompt.encode()) + len(json.dumps({
                "target_agent": agent_id, "target_instructions": role_prompt,
                "target_role": role_id, "output_schema": output_schema,
                "partition": {"index": index, "total": total, "sha256": packet_digest},
                "material": {}}).encode()) + 256
            try:
                pieces = _partition_synthesis_context({"material": packet}, refined,
                                                      input_overhead_bytes=overhead)
            except SynthesisContextLimitError:
                if not output_incomplete:
                    raise
                # An indivisible context is not permission to discard its
                # already received findings or claim complete coverage.
                children_complete = False
            else:
                children = [self._coverage_capsule(agent_id=agent_id, role_prompt=role_prompt,
                    packet=part, index=part_index, total=len(pieces), capability=refined,
                    role_id=role_id, output_schema=output_schema, refinement=refinement + 1)
                    for part_index, part in enumerate(pieces, 1)]
                children_complete = all(child.get("complete", True) for child in children)
                content = {"partition_findings": [child["content"] for child in children],
                           "source_evidence_ids": _collect_evidence_ids(packet)}
                output_incomplete = not children_complete
        complete = children_complete and not output_incomplete
        source_evidence_ids = _collect_evidence_ids(packet)
        content, unsupported_ids = _isolate_unknown_evidence_references(
            content, set(source_evidence_ids)
        )
        if content is None:
            content = {}
        return {
            "partition_id": f"{index}/{total}",
            "sha256": packet_digest,
            "source_evidence_ids": source_evidence_ids,
            "isolated_evidence_ids": sorted(unsupported_ids),
            "complete": complete,
            "content": content,
        }

    def _consolidate_coverage_capsules(
        self,
        *,
        agent_id: str,
        role_prompt: str,
        capsules: list[dict[str, Any]],
        lean_context_json: str,
        compact_ledger: dict[str, Any],
        capability: ProviderContextCapability,
        input_overhead_bytes: int,
        run: ResearchRun | None = None,
        input_sha256: str = "",
        coverage_request_sha256: str = "",
        cached_capsules: dict[str, dict[str, Any]] | None = None,
        role_id: str | None = None, output_schema: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Recursively consolidate complete capsule batches until merge fits.

        Every consolidation packet carries the union of its source evidence
        IDs.  The original partition ledger remains immutable and is stored on
        the final result; consolidation changes only the bounded model input.
        """

        current = list(capsules)
        rounds = 0
        minimum_merge_size = _agent_input_size(
            agent_id, role_prompt, self.report_language, lean_context_json,
            {"coverage_ledger": compact_ledger, "coverage_capsules": []},
            role_id=role_id, output_schema=output_schema,
        )
        if minimum_merge_size > capability.max_input_bytes:
            # Consolidation cannot make the required instructions, evidence
            # registry and immutable coverage ledger any smaller. Keep the
            # partition checkpoints and do not spend on impossible merge calls.
            raise SynthesisContextLimitError(
                [{"name": f"{agent_id}:minimum-coverage-merge", "bytes": minimum_merge_size}],
                capability.max_input_bytes, required_bytes=minimum_merge_size,
            )
        while True:
            merge_prior = {
                "coverage_ledger": compact_ledger,
                "coverage_capsules": current,
            }
            merge_size = _agent_input_size(
                agent_id,
                role_prompt,
                self.report_language,
                lean_context_json,
                merge_prior,
                role_id=role_id, output_schema=output_schema,
            )
            if merge_size <= capability.max_input_bytes:
                return current, rounds
            if rounds >= 6 or len(current) <= 1:
                raise SynthesisContextLimitError(
                    [{"name": f"{agent_id}:coverage-merge", "bytes": merge_size}],
                    capability.max_input_bytes,
                )
            packets = _partition_synthesis_context(
                {"coverage_capsules": current},
                capability,
                input_overhead_bytes=input_overhead_bytes,
            )
            next_capsules: list[dict[str, Any]] = []
            for index, packet in enumerate(packets, 1):
                packet_json = json.dumps(
                    packet,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                packet_digest = hashlib.sha256(
                    packet_json.encode("utf-8")
                ).hexdigest()
                capsule = (cached_capsules or {}).get(packet_digest)
                if capsule is None:
                    capsule = self._coverage_capsule(
                        agent_id=agent_id,
                        role_prompt=role_prompt,
                        packet=packet,
                        index=index,
                        total=len(packets),
                        capability=capability,
                        role_id=role_id, output_schema=output_schema,
                    )
                    if run is not None:
                        self._save(
                            run,
                            "stage-material-coverage",
                            self._report_text(
                                "研究材料覆盖检查点",
                                "Research material coverage checkpoint",
                            ),
                            {
                                "schema": "openthesis.stage-coverage-checkpoint.v1",
                                "agent_id": agent_id,
                                "input_sha256": input_sha256,
                                "coverage_request_sha256": coverage_request_sha256,
                                "partition_sha256": packet_digest,
                                "capsule": capsule,
                            },
                            agent_id="research-continuity",
                        )
                    if cached_capsules is not None:
                        cached_capsules[packet_digest] = capsule
                next_capsules.append(capsule)
            before_digests = [str(item.get("sha256", "")) for item in current]
            after_digests = [str(item.get("sha256", "")) for item in next_capsules]
            if len(next_capsules) >= len(current) and after_digests == before_digests:
                raise SynthesisContextLimitError(
                    [{"name": f"{agent_id}:coverage-merge", "bytes": merge_size}],
                    capability.max_input_bytes,
                )
            current = next_capsules
            rounds += 1

    def _run_agent_adaptive(
        self,
        agent_id: str,
        prompt_path: str,
        context_json: str,
        prior_artifacts: dict[str, Any],
        *,
        run: ResearchRun | None = None,
        role_id: str | None = None, output_schema: str | None = None,
    ) -> dict[str, Any]:
        """Use one complete call when possible, otherwise cover every bounded partition."""

        role = _agent_role(agent_id, role_id)
        context_json, direct_manifest = _deduplicated_stage_context(
            context_json, agent_id
        )
        stage_input_sha256 = _stage_input_digest(context_json, prior_artifacts,
                                                role_id=role_id, output_schema=output_schema)
        direct_manifest = StageMaterialManifest(
            stage=direct_manifest.stage,
            input_sha256=stage_input_sha256,
            unique_evidence_ids=direct_manifest.unique_evidence_ids,
            exact_duplicates_removed=direct_manifest.exact_duplicates_removed,
            mode=direct_manifest.mode,
            partition_count=direct_manifest.partition_count,
        )
        if self.provider is None:
            raise RuntimeError("model provider is not configured")
        role_prompt = self.pack.prompt(prompt_path)
        capability = provider_context_capability(self._stage_budget_provider(agent_id))
        capability_declared = _provider_declares_context_capability(self.provider)
        input_size = _agent_input_size(
            agent_id,
            role_prompt,
            self.report_language,
            context_json,
            prior_artifacts,
            role_id=role_id, output_schema=output_schema,
        )
        fits = input_size <= capability.max_input_bytes
        from .research_execution.input_budget import input_measurement
        measured = input_measurement(self.provider, *_agent_prompt_bundle(
            agent_id, role_prompt, self.report_language, context_json, prior_artifacts,
            role_id=role_id, output_schema=output_schema))
        if measured["exact"] and capability_declared:
            fits = measured["tokens"] <= capability.available_input_tokens
            if capability.explicit_max_input_bytes is not None:
                fits = fits and input_size <= capability.explicit_max_input_bytes
        if fits or not capability_declared:
            try:
                result = self._run_agent(
                    agent_id,
                    prompt_path,
                    context_json,
                    prior_artifacts,
                    enforce_local_budget=capability_declared,
                    role_id=role_id, output_schema=output_schema,
                )
            except ProviderError as exc:
                if exc.code not in {"MODEL_CONTEXT_CAPACITY", "MODEL_REQUEST_BODY_TOO_LARGE"}:
                    raise
                # A confirmed rejection has no remote candidate to await. Use
                # a smaller, complete material partition through this same
                # journal; never resubmit the identical rejected request.
                capability = ProviderContextCapability(capability.max_input_tokens,
                    capability.reserved_output_tokens, capability.reserved_repair_tokens,
                    max(1_024, min(capability.max_input_bytes, input_size // 2)))
            else:
                result["_stage_material_manifest"] = direct_manifest.to_dict()
                return result

        routed = route_materials(json.loads(context_json), role)
        gate = routed["material_adequacy"]
        if gate["status"] == "insufficient":
            return insufficient_material_result(gate, self.report_language)
        source = {
            "research_context": routed,
            "prior_artifacts": prior_artifacts,
        }
        # Reserve the complete fixed instructions and JSON envelope before
        # partitioning. Each emitted packet is checked again with the exact
        # serialized prompt in ``_coverage_capsule``.
        coverage_system_prompt = (
            CORE_SYSTEM_PROMPT
            + "\n"
            + OUTPUT_LANGUAGE_INSTRUCTIONS[self.report_language]
            + "\nThis is a material-coverage pass, not the final analysis. Preserve every "
            "decision-relevant fact, risk, uncertainty, number and evidence ID from the "
            "supplied partition. Return JSON with claims, observations, risks, unknowns, "
            "and source_evidence_ids. Do not infer from missing partitions."
        )
        coverage_envelope = json.dumps(
            {
                "target_agent": agent_id,
                **({"target_role": role_id} if role_id is not None else {}),
                **({"output_schema": output_schema} if output_schema is not None else {}),
                "target_instructions": role_prompt,
                "partition": {"index": 1, "total": 1, "sha256": "0" * 64},
                "material": {},
            },
            ensure_ascii=False,
        )
        # Material identity alone cannot authorize reuse of a model's analysis.
        # Bind both checkpoint shapes to the exact instructions/envelope,
        # language, configuration and capacity used to produce the capsules.
        # Store only this non-secret digest, never a second copy of prompts.
        coverage_request_sha256 = recovery_digest({
            "schema": "openthesis.coverage-request-identity.v1",
            "coverage_policy": "complete-source-spans-output-v2",
            "output_budget_tokens": self._stage_output_budget("material-coverage"),
            "input_sha256": direct_manifest.input_sha256,
            "model_id": self.model_config.public_id,
            "max_input_bytes": capability.max_input_bytes,
            "system_prompt": coverage_system_prompt,
            "prompt_envelope": coverage_envelope,
        })
        fixed_overhead = (
            len(coverage_system_prompt.encode("utf-8"))
            + len(coverage_envelope.encode("utf-8"))
            + 512
        )
        packets = _partition_synthesis_context(
            source,
            capability,
            input_overhead_bytes=fixed_overhead,
        )
        cached_capsules: dict[str, dict[str, Any]] = {}
        cached_coverage_set: dict[str, Any] | None = None
        if run is not None:
            for artifact in self.storage.get_artifacts(run.run_id):
                cached = artifact.get("content")
                if not isinstance(cached, dict):
                    continue
                if (
                    artifact.get("artifact_type") == "stage-material-coverage-set"
                    and cached.get("agent_id") == agent_id
                    and cached.get("input_sha256") == direct_manifest.input_sha256
                    and cached.get("coverage_request_sha256") == coverage_request_sha256
                    and cached.get("model_id") == self.model_config.public_id
                    and cached.get("max_input_bytes") == capability.max_input_bytes
                ):
                    cached_coverage_set = cached
                    continue
                if artifact.get("artifact_type") != "stage-material-coverage":
                    continue
                if (
                    cached.get("agent_id") == agent_id
                    and cached.get("input_sha256") == direct_manifest.input_sha256
                    and cached.get("coverage_request_sha256") == coverage_request_sha256
                    and isinstance(cached.get("capsule"), dict)
                ):
                    cached_capsules[str(cached.get("partition_sha256") or "")] = cached["capsule"]
        capsules: list[dict[str, Any]] = []
        if cached_coverage_set is not None and isinstance(
            cached_coverage_set.get("raw_capsules"), list
        ):
            capsules = [
                item
                for item in cached_coverage_set["raw_capsules"]
                if isinstance(item, dict)
            ]
        for index, packet in enumerate(packets, 1):
            if cached_coverage_set is not None:
                break
            packet_json = json.dumps(
                packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            packet_digest = hashlib.sha256(packet_json.encode("utf-8")).hexdigest()
            capsule = cached_capsules.get(packet_digest)
            if capsule is None:
                capsule = self._coverage_capsule(
                    agent_id=agent_id,
                    role_prompt=role_prompt,
                    packet=packet,
                    index=index,
                    total=len(packets),
                    capability=capability,
                    role_id=role_id, output_schema=output_schema,
                )
                if run is not None:
                    self._save(
                        run,
                        "stage-material-coverage",
                        self._report_text(
                            "研究材料覆盖检查点",
                            "Research material coverage checkpoint",
                        ),
                        {
                            "schema": "openthesis.stage-coverage-checkpoint.v1",
                            "agent_id": agent_id,
                            "input_sha256": direct_manifest.input_sha256,
                            "coverage_request_sha256": coverage_request_sha256,
                            "partition_sha256": packet_digest,
                            "capsule": capsule,
                        },
                        agent_id="research-continuity",
                    )
            capsules.append(capsule)
        if len(capsules) != len(packets):
            raise RuntimeError("stage material coverage is incomplete")

        context = json.loads(context_json)
        evidence = context.get("evidence", []) if isinstance(context, dict) else []
        registry: list[dict[str, Any]] = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            entry = {
                key: item.get(key)
                for key in ("evidence_id", "kind", "topic", "source_url", "document_id")
                if item.get(key) not in (None, "")
            }
            if item.get("kind") != "financial_fact":
                entry["raw_text"] = (
                    "Original material was supplied to a recorded partition. The linked capsule contains model findings, not a lossless copy of that original."
                )
            registry.append(entry)
        lean_context = {
            key: value
            for key, value in context.items()
            if key != "evidence"
        }
        lean_context["evidence"] = registry
        coverage_complete = all(item.get("complete", True) for item in capsules)
        coverage = {
            "schema": "openthesis.stage-coverage.v1",
            "complete": coverage_complete,
            "partition_count": len(packets),
            "partitions": [
                {
                    "partition_id": item["partition_id"],
                    "sha256": item["sha256"],
                    "source_evidence_ids": item["source_evidence_ids"],
                    "status": "complete" if item.get("complete", True) else "output_incomplete",
                }
                for item in capsules
            ],
        }
        lean_context_json = json.dumps(
            lean_context, ensure_ascii=False, separators=(",", ":")
        )
        compact_ledger = {
            "schema": coverage["schema"],
            "complete": coverage_complete,
            "partition_count": len(packets),
            "covered_evidence_ids": sorted(
                {
                    evidence_id
                    for item in capsules
                    for evidence_id in item["source_evidence_ids"]
                }
            ),
        }
        raw_capsules = list(capsules)
        if cached_coverage_set is not None and isinstance(
            cached_coverage_set.get("consolidated_capsules"), list
        ):
            capsules = [
                item
                for item in cached_coverage_set["consolidated_capsules"]
                if isinstance(item, dict)
            ]
            consolidation_rounds = int(
                cached_coverage_set.get("consolidation_rounds") or 0
            )
        else:
            capsules, consolidation_rounds = self._consolidate_coverage_capsules(
                agent_id=agent_id,
                role_prompt=role_prompt,
                capsules=capsules,
                lean_context_json=lean_context_json,
                compact_ledger=compact_ledger,
                capability=capability,
                input_overhead_bytes=fixed_overhead,
                run=run,
                input_sha256=direct_manifest.input_sha256,
                coverage_request_sha256=coverage_request_sha256,
                cached_capsules=cached_capsules,
                role_id=role_id, output_schema=output_schema,
            )
            if run is not None:
                self._save(
                    run,
                    "stage-material-coverage-set",
                    self._report_text(
                        "研究材料覆盖集合",
                        "Research material coverage set",
                    ),
                    {
                        "schema": "openthesis.stage-coverage-set.v1",
                        "agent_id": agent_id,
                        "input_sha256": direct_manifest.input_sha256,
                        "coverage_request_sha256": coverage_request_sha256,
                        "model_id": self.model_config.public_id,
                        "max_input_bytes": capability.max_input_bytes,
                        "raw_capsules": raw_capsules,
                        "consolidated_capsules": capsules,
                        "consolidation_rounds": consolidation_rounds,
                    },
                    agent_id="research-continuity",
                )
        compact_ledger["consolidation_rounds"] = consolidation_rounds
        merge_prior = {
            "coverage_ledger": compact_ledger,
            "coverage_capsules": capsules,
        }
        result = self._run_agent(
            agent_id,
            prompt_path,
            lean_context_json,
            merge_prior,
            material_adequacy_override=gate,
            role_id=role_id, output_schema=output_schema,
        )
        _, manifest = _deduplicated_stage_context(
            context_json,
            agent_id,
            mode="partitioned-material-coverage",
            partition_count=len(packets),
        )
        manifest = StageMaterialManifest(
            stage=manifest.stage,
            input_sha256=stage_input_sha256,
            unique_evidence_ids=manifest.unique_evidence_ids,
            exact_duplicates_removed=manifest.exact_duplicates_removed,
            mode=manifest.mode,
            partition_count=manifest.partition_count,
        )
        result["_stage_material_manifest"] = manifest.to_dict()
        result["_coverage_ledger"] = coverage
        result["_coverage_ledger"]["consolidation_rounds"] = consolidation_rounds
        if not coverage_complete:
            result["_output_incomplete"] = {
                "code": "MODEL_OUTPUT_BUDGET_EXHAUSTED", "state": "pending",
                "scope": "material-coverage"}
        return result

    @staticmethod
    def _apply_run_lifecycle(run: ResearchRun, payload: dict[str, Any]) -> None:
        # Only call after the repository transaction committed successfully.
        run.status = RunStatus(payload["status"])
        run.completed_at = payload.get("completed_at")
        run.errors = list(payload["errors"])

    def _capture_resume_publication(self, run: ResearchRun) -> ResearchPublicationSnapshot:
        self._check_cancelled()
        publication = self.storage.capture_research_publication(run)
        configuration = {
            "configured_model_id": self.model_config.configured_model_id,
            "configuration_version": self.model_config.configuration_version,
            "role": self.model_config.role,
        }
        publication = self.storage.update_research_model(publication,
            provider_id=self.model_config.provider, model_id=self.model_config.model,
            model_configuration=configuration)
        # Do not relabel the old revision; its immutable context stays frozen.
        run.provider_id, run.model_id = self.model_config.provider, self.model_config.model
        run.model_configuration = configuration
        return publication

    def _publish_report_result(
        self, run: ResearchRun, publication: ResearchPublicationSnapshot,
        artifact: ResearchArtifact, *, complete: bool,
        thesis_content: dict[str, Any] | None = None,
    ) -> None:
        self._check_cancelled()
        # A final body may be identical to a historical revision while this
        # attempt has published newer progressive revisions. Bind this commit
        # to its CAS generation so artifact deduplication cannot leave the
        # progressive head current or confuse a real competing writer.
        if artifact.artifact_type == "research-report":
            artifact = self._artifact(run, artifact.artifact_type, artifact.title,
                {**artifact.content, "publication_generation": publication.report_generation + 1},
                agent_id=artifact.agent_id)

        def thesis_artifact(thesis: dict[str, Any]) -> ResearchArtifact:
            # Pure construction; the repository allocates the version under
            # the same write transaction as the report and final run outcome.
            return self._artifact(run, "thesis-snapshot", self._report_text(
                f"投资逻辑 v{thesis['version']}", f"Investment Thesis v{thesis['version']}"),
                thesis, agent_id="thesis-versioning")

        attempt = StageAttempt(run.run_id, "report-rendering",
            StageOutcome.COMPLETE if complete else StageOutcome.NEEDS_ACTION,
            error_code="" if complete else "RESEARCH_RESULT_INCOMPLETE",
            diagnostics={"input_digest": publication.input_digest,
                         "expected_report_generation": publication.report_generation,
                         "artifact_id": artifact.artifact_id})
        try:
            payload = self.storage.publish_research_result(publication, artifact, attempt,
                status=RunStatus.COMPLETED if complete else RunStatus.PARTIAL,
                completed_at=utc_now_iso(), cancel_check=self.cancel_check,
                thesis_content=thesis_content, thesis_created_by=self.model_config.public_id,
                thesis_artifact_factory=thesis_artifact if thesis_content is not None else None)
        except CancelledError as exc:
            raise ResearchCancelled(run_id=run.run_id) from exc
        self._apply_run_lifecycle(run, payload)

    def _save(
        self,
        run: ResearchRun,
        artifact_type: str,
        title: str,
        content: dict[str, Any],
        *,
        agent_id: str,
    ) -> ResearchArtifact:
        artifact = self._artifact(run, artifact_type, title, content, agent_id=agent_id)
        self.storage.save_artifact(artifact)
        return artifact

    def _artifact(
        self,
        run: ResearchRun,
        artifact_type: str,
        title: str,
        content: dict[str, Any],
        *,
        agent_id: str,
    ) -> ResearchArtifact:
        # Artifact identity is derived from serialized content. Normalize
        # tuples and other JSON-compatible containers before both hashing and
        # persistence so an identical replay compares equal after SQLite's
        # JSON round-trip.
        content = json.loads(json.dumps(content, ensure_ascii=False, sort_keys=True))
        raw = json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        model_id = self.model_config.public_id
        if artifact_type in {"deterministic-financial-summary", "deterministic-valuation"}:
            model_id = ""  # Deterministic material is not produced by a model.
        elif artifact_type == "stage-checkpoint":
            model_id = str(content.get("result", {}).get("execution_fingerprint") or model_id)
        elif artifact_type == "stage-candidate":
            model_id = str(content.get("execution_fingerprint") or model_id)
        # Immutable metadata is part of identity too. Identical body text from
        # a different producer/configuration is a new auditable artifact, not
        # permission to overwrite the prior model's attribution.
        identity = json.dumps([agent_id, model_id, title], ensure_ascii=False).encode() + b"|" + raw
        suffix = hashlib.sha256(identity).hexdigest()[:10]
        artifact = ResearchArtifact(
            artifact_id=f"{run.run_id}:{artifact_type}:{suffix}",
            run_id=run.run_id,
            artifact_type=artifact_type,
            title=title,
            content=content,
            model_id=model_id,
            agent_id=agent_id,
        )
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


def _trusted_prior_base_results(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    eligible_agents: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Recover only the latest trusted base-agent artifacts.

    Artifacts are append-only, so an earlier successful result must not mask a
    later failed retry.  Both the role result and its verifier record are
    reconstructed from the same newest artifact to avoid pairing data from
    different attempts.
    """
    latest: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("artifact_type") != "agent-analysis":
            continue
        agent_id = str(artifact.get("agent_id") or "")
        if agent_id in eligible_agents:
            latest[agent_id] = artifact

    results: dict[str, dict[str, Any]] = {}
    verifications: dict[str, dict[str, Any]] = {}
    for agent_id, artifact in latest.items():
        content = artifact.get("content")
        if not isinstance(content, dict) or content.get("trusted_state") not in {
            "completed_verified", "completed_partial",
        }:
            continue
        raw = content.get("result")
        verification = content.get("verification")
        if not isinstance(raw, dict) or not isinstance(verification, dict):
            continue
        trusted = _trusted_stage_result(raw, verification)
        if trusted.get("_verification_state") == "failed_verification":
            continue
        results[agent_id] = trusted
        verifications[agent_id] = verification
    return results, verifications


def _trusted_prior_stage_result(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    artifact_type: str,
) -> dict[str, Any] | None:
    """Return the newest verified output for an upstream model stage.

    A newer failed attempt deliberately shadows an older success: recovery
    must not silently reuse stale content after the persisted stage identity
    has moved forward. Internal audit keys are not passed back to models.
    """
    artifact = _latest_artifact(list(artifacts), artifact_type)
    content = artifact.get("content") if isinstance(artifact, dict) else None
    if not isinstance(content, dict):
        return None
    audit = content.get("_audit")
    verification = audit.get("verification") if isinstance(audit, dict) else None
    validation = content.get("_validation")
    verified = (
        isinstance(verification, dict) and verification.get("passed") is True
    ) or (
        artifact_type == "growth-opportunities"
        and isinstance(validation, dict) and validation.get("passed") is True
        and bool(content.get("opportunities"))
    )
    if not verified:
        return None
    return {key: value for key, value in content.items() if not key.startswith("_")}
