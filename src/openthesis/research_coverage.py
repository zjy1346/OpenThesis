"""Versioned research obligations, evaluated from frozen inputs only.

The contract owns completeness, not display or execution. A notice, eighteen
headings, or model-provided completion flags cannot satisfy an obligation.
Reading an assessment never downloads data, calls a model, or changes history.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Mapping, Sequence

from .research_section_validation import proven_content_nodes, is_structural_content_path
from .financials import read_metric_period


CONTRACT_VERSION = "openthesis.research-coverage.v5-local-retention"


@dataclass(frozen=True, slots=True)
class ResearchObligation:
    obligation_id: str
    paths: tuple[str, ...]
    source_kind: str = "analysis"


OBLIGATIONS = (
    ResearchObligation("company_business_model", ("business_model",)),
    ResearchObligation("historical_financial_performance", (), "financial_history"),
    ResearchObligation("earnings_quality", ("financial_quality",)),
    ResearchObligation("balance_sheet_strength", ("balance_sheet",)),
    ResearchObligation("competitive_advantages", ("competitive_position",)),
    ResearchObligation("management_capital_allocation", ("management_and_capital_allocation", "business_model.management_and_capital_allocation")),
    ResearchObligation("existing_growth_engines", ("existing_growth_engines", "business_model.existing_growth_engines")),
    ResearchObligation("potential_growth_opportunities", ("growth_opportunities",)),
    ResearchObligation("industry_value_allocation", ("industry_changes_and_value_allocation", "growth_opportunities.industry_changes_and_value_allocation")),
    ResearchObligation("growth_counterarguments", ("counterarguments",)),
    ResearchObligation("operating_scenarios", ("scenarios",), "scenarios"),
    ResearchObligation("current_price_expectations", ("implied_expectations",), "valuation"),
    ResearchObligation("valuation_sensitivity", ("valuation_and_sensitivity", "implied_expectations.valuation_and_sensitivity"), "sensitivity"),
    ResearchObligation("investment_thesis", ("thesis",)),
    ResearchObligation("thesis_invalidation", ("invalidation_conditions",)),
    ResearchObligation("leading_indicators", ("leading_indicators",)),
    ResearchObligation("unresolved_questions", ("unresolved_questions",)),
    ResearchObligation("evidence_methodology", ("evidence_and_methodology",), "methodology"),
)

_METADATA_KEYS = frozenset({
    "evidence_ids", "source_ids", "source_evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids",
    "unknown_evidence_ids", "claim_id", "fact_id", "document_id", "source_id", "role", "kind",
    "status", "confidence", "probability", "currency", "unit", "fiscal_year", "fiscal_period",
    "end_date", "period_end", "page", "pages", "url", "source_url", "name", "title",
    "complete", "verification", "verified", "evidence_count", "materiality", "applicability",
    "method", "schema_version", "contract_version", "mode", "scenario_eligibility",
    "claims", "claim_ids", "input_claim_ids", "type", "label", "severity", "as_of",
    "supporting_evidence_count", "contradicting_evidence_count",
})
_NON_ANALYSIS_ROLES = frozenset({"notice", "placeholder", "source", "unknown"})

# Producers consume this SAME obligation catalogue as final completeness.
# Required sources describe the question, never a blanket stage success gate.
_PRODUCERS = {
    "company_business_model": ("business-analyst", "sourced_disclosures"),
    "historical_financial_performance": ("financial-analyst", "comparable_financial_periods"),
    "earnings_quality": ("financial-analyst", "quantitative_facts"),
    "balance_sheet_strength": ("financial-analyst", "quantitative_facts"),
    "competitive_advantages": ("business-analyst", "sourced_disclosures"),
    "management_capital_allocation": ("business-analyst", "sourced_disclosures"),
    "existing_growth_engines": ("business-analyst", "sourced_disclosures"),
    "potential_growth_opportunities": ("growth-opportunity-analyst", "sourced_disclosures"),
    "industry_value_allocation": ("growth-opportunity-analyst", "sourced_disclosures"),
    "growth_counterarguments": ("skeptical-analyst", "sourced_disclosures"),
    "operating_scenarios": ("forecast-analyst", "verified_premises"),
    "current_price_expectations": ("valuation-engine", "verified_price_and_cash_flow"),
    "valuation_sensitivity": ("valuation-engine", "verified_price_and_cash_flow"),
    "investment_thesis": ("research-synthesizer", "verified_premises"),
    "thesis_invalidation": ("research-synthesizer", "verified_premises"),
    "leading_indicators": ("research-synthesizer", "verified_premises"),
    "unresolved_questions": ("research-synthesizer", "research_gaps"),
    "evidence_methodology": ("research-synthesizer", "actual_research_methods"),
}


def obligation_production_plan(role: str = "") -> list[dict[str, Any]]:
    """No I/O; an extensible question-to-producer plan from canonical IDs."""
    if set(_PRODUCERS) != {item.obligation_id for item in OBLIGATIONS}:
        raise ValueError("RESEARCH_OBLIGATION_PRODUCER_MAPPING_INVALID")
    return [{"obligation_id": item.obligation_id, "report_paths": list(item.paths),
             "producer": _PRODUCERS[item.obligation_id][0],
             "necessary_input": _PRODUCERS[item.obligation_id][1],
             "optional_inputs": ["verified_previous_analysis"],
             "gap_action": "revalidate_sources" if item.source_kind in {"valuation", "sensitivity", "financial_history"}
                           else "expand_official_disclosure_retrieval",
             "instructions": "Answer this question from supplied sources; preserve independently supported parts. Missing inputs are a local gap, not non-applicability."}
            for item in OBLIGATIONS if not role or _PRODUCERS[item.obligation_id][0] == role]
_PLACEHOLDERS = frozenset({
    "", "—", "-", "暂无", "暂无。", "暂无可显示内容。", "證據不足或尚未提供。",
    "证据不足或尚未提供。", "当前研究阶段未返回可验证的此章节内容。",
    "本研究階段未返回可驗證的此章節內容。", "insufficient evidence or not provided.",
    "this section was not returned with verifiable content in the current research stage.",
    "no displayable content.", "none.", "not available.",
})


def has_analysis(value: Any) -> bool:
    """Text semantics with typed notice exclusion; metadata alone is not analysis."""
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", re.sub(r"[`*_>#\[\](){}]", "", value)).strip().casefold()
        if text in _PLACEHOLDERS:
            return False
        if text.startswith(("insufficient evidence or not provided", "this section was not returned with verifiable content",
                            "当前研究阶段未返回可验证", "本研究階段未返回可驗證")):
            return False
        # Source identifiers cannot be a human-readable explanation.
        return bool(text) and not re.fullmatch(r"(?:fact|filing|evidence|artifact|run|source|claim|document):[\w.:/-]+", text)
    if isinstance(value, dict):
        if str(value.get("role") or "").casefold() in _NON_ANALYSIS_ROLES:
            return False
        return any(has_analysis(child) for key, child in value.items()
                   if not str(key).startswith("_") and str(key).casefold() not in _METADATA_KEYS)
    if isinstance(value, (tuple, list)):
        return any(has_analysis(item) for item in value)
    return False


@dataclass(frozen=True, slots=True)
class ObligationCoverage:
    obligation_id: str
    state: str
    source_path: str = ""
    evidence_ids: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    applicability_rule: str = ""


@dataclass(frozen=True, slots=True)
class ResearchCoverage:
    contract_version: str
    complete: bool
    obligations: tuple[ObligationCoverage, ...]

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(item.obligation_id for item in self.obligations
                     if item.state not in {"fulfilled", "not_applicable"})

    def to_dict(self) -> dict:
        return {**asdict(self), "gaps": self.gaps}


def _path_value(report: Mapping[str, Any], path: str) -> Any:
    value: Any = report
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _references(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"evidence_ids", "source_ids", "source_evidence_ids", "supporting_evidence_ids", "contradicting_evidence_ids"}:
                if isinstance(child, (list, tuple)):
                    found.update(item for item in child if isinstance(item, str) and item)
            elif not str(key).startswith("_"):
                found.update(_references(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_references(child))
    return found


def _unproven_analysis(value: Any, path: str, proven_paths: set[str]) -> bool:
    if path in proven_paths:
        return False
    if isinstance(value, dict):
        return any(_unproven_analysis(child, path + "." + key, proven_paths)
                   for key, child in value.items() if not key.startswith("_")
                   and key.casefold() not in _METADATA_KEYS)
    if isinstance(value, (list, tuple)):
        return any(_unproven_analysis(child, f"{path}[{index}]", proven_paths)
                   for index, child in enumerate(value))
    return has_analysis(value)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _metric_matches_fact(metric: dict, concept: str, money: dict, record: dict) -> bool:
    """A registered ID alone is not proof of the displayed financial value."""
    try:
        identity = read_metric_period(metric)
    except ValueError:
        return False
    if (record.get("kind") != "financial_fact" or record.get("concept") != concept
            or not _finite(record.get("value"))
            or record.get("fiscal_year") != identity.year):
        return False
    try:
        if Decimal(str(record["value"])) != Decimal(str(metric[concept])):
            return False
    except (ValueError, InvalidOperation, KeyError):
        return False
    currency = str(record.get("currency") or record.get("unit") or "").upper()
    if not currency or currency != str(money.get("currency") or "").upper():
        return False
    if money.get("unit_provenance") in (None, "", "unknown"):
        return False
    for metadata_key, source_keys in (
        ("period", ("fiscal_period",)), ("period_end", ("end_date",)),
        ("period_start", ("start_date",)), ("generation_id", ("generation_id",)),
        ("scope", ("consolidated_scope", "scope")),
    ):
        expected = next((record.get(key) for key in source_keys if record.get(key)), None)
        if expected is not None and str(money.get(metadata_key) or "") != str(expected):
            return False
    return True


class ResearchCoverageContract:
    version = CONTRACT_VERSION
    obligations = OBLIGATIONS

    def assess(self, report: Any, verification: Any, *, evidence_records: Mapping[str, dict],
               metrics: Sequence[dict] = (), valuation: Mapping[str, Any] | None = None,
               company: Mapping[str, Any] | None = None,
               financial_period_coverage: Mapping[str, Any] | None = None) -> ResearchCoverage:
        value = report if isinstance(report, dict) else {}
        proof = verification if isinstance(verification, dict) else {}
        # Global counters describe the attempt, not each section's trust.
        # Consume only independently produced, body-bound local proofs.
        nodes = tuple(node for node in proven_content_nodes(value, proof.get("section_validation"))
                      if not is_structural_content_path(node["source_path"]))
        known = set(evidence_records)
        financial = {key for key, record in evidence_records.items()
                     if isinstance(record, dict) and record.get("kind") == "financial_fact"}
        fact_lookup = {str(record.get("fact_id")): key for key, record in evidence_records.items()
                       if key in financial and record.get("fact_id")}
        rows = []
        for requirement in self.obligations:
            if requirement.source_kind == "financial_history":
                sources = set()
                annual_revenues = set()
                invalid_bindings = False
                for metric in metrics:
                    metadata = metric.get("money_metadata", {}) if isinstance(metric, dict) else {}
                    if not isinstance(metadata, dict):
                        continue
                    for concept, money in metadata.items():
                        if not _finite(metric.get(concept)) or not isinstance(money, dict):
                            continue
                        fact_id = str(money.get("fact_id") or "")
                        reference = fact_lookup.get(fact_id, fact_id if fact_id in financial else "fact:" + fact_id)
                        # Formula outputs use input IDs rather than a direct fact ID.
                        # Their independent formula contract owns that validation.
                        if not fact_id:
                            continue
                        if reference in financial and _metric_matches_fact(metric, concept, money, evidence_records[reference]):
                            sources.add(reference)
                            identity = read_metric_period(metric)
                            if concept == "revenue" and identity.period == "FY":
                                annual_revenues.add((identity.year, identity.period_end))
                        else:
                            invalid_bindings = True
                required_periods = set()
                plan_invalid = False
                plan = (financial_period_coverage or {}).get("required_annual_periods", ())
                if not isinstance(plan, (list, tuple)):
                    plan_invalid = True
                    plan = ()
                for period in plan:
                    try:
                        identity = read_metric_period(period)
                        if identity.period != "FY" or not identity.period_end:
                            raise ValueError("annual_plan_identity_invalid")
                        required_periods.add((identity.year, identity.period_end))
                    except (ValueError, TypeError, AttributeError):
                        plan_invalid = True
                missing_periods = required_periods - annual_revenues
                history_present = len(annual_revenues) >= 2 and not missing_periods and not plan_invalid
                history_issues = (("financial_metric_source_mismatch",) if invalid_bindings else
                    ("financial_period_plan_invalid",) if plan_invalid else
                    tuple(f"verified_annual_period_missing:{year}:{end}" for year, end in sorted(missing_periods))
                    if missing_periods else () if history_present else ("verified_financial_history_missing",))
                rows.append(ObligationCoverage(requirement.obligation_id,
                            "verification_failed" if invalid_bindings or plan_invalid else "fulfilled" if history_present else "missing_source_data",
                            "context.metrics", tuple(sorted(sources)),
                            history_issues))
                continue

            path, analysis = "", None
            for alternate in requirement.paths:
                candidate = _path_value(value, alternate)
                if has_analysis(candidate):
                    path, analysis = alternate, candidate
                    break
            references = _references(analysis)
            local_nodes = [node for node in nodes if node["path"] == path
                           or node["path"].startswith(path + ".")
                           or node["path"].startswith(path + "[")]
            proven_refs = {ref for node in local_nodes for ref in node.get("evidence_ids", ())}
            sources, unknown = (references | proven_refs) & known, (references | proven_refs) - known
            issues = []
            if local_nodes and all(node.get("role") == "source_quote" for node in local_nodes):
                issues.append("source_quotation_without_analysis")
            if analysis is None:
                issues.append("substantive_analysis_missing")
            nonfactual_roles = ({"question"} if requirement.obligation_id == "unresolved_questions" else
                {"methodology"} if requirement.obligation_id == "evidence_methodology" else set())
            nonfactual_obligation = (nonfactual_roles and local_nodes
                and all(node.get("role") in nonfactual_roles for node in local_nodes))
            if not sources and not nonfactual_obligation:
                issues.append("source_links_missing")
            if unknown:
                issues.append("source_links_unregistered")
            unproven = (analysis is not None and _unproven_analysis(
                analysis, path, {node["path"] for node in local_nodes}))
            pending_validation = any(node.get("validation_state") == "partial" for node in local_nodes)
            if pending_validation:
                issues.append("generated_content_pending_verification")
            if analysis is not None and (not local_nodes or unproven):
                issues.append("local_content_proof_missing")

            if requirement.source_kind == "scenarios":
                cases = self._scenario_cases(analysis)
                if cases != {"bear", "base", "bull"}:
                    issues.append("bear_base_bull_analysis_missing")
            if requirement.source_kind in {"valuation", "sensitivity"}:
                is_beta = isinstance(company, dict) and company.get("industry_support") == "financial_beta"
                explicit_limit = (is_beta and isinstance(analysis, dict)
                                  and analysis.get("applicability") == "not_applicable"
                                  and analysis.get("method") == "ordinary_fcf_valuation"
                                  and has_analysis(analysis.get("reason")))
                if explicit_limit:
                    # The host's business applicability rule plus a proven
                    # reason can establish a limitation. It need not pretend
                    # to be an investment analysis in order to be inapplicable.
                    issues = [issue for issue in issues if issue != "source_quotation_without_analysis"]
                if explicit_limit and not issues:
                    rows.append(ObligationCoverage(requirement.obligation_id, "not_applicable", path,
                                tuple(sorted(sources)), (), "financial_beta_ordinary_fcf_valuation"))
                    continue
                from .valuation_claims import intrinsic_sensitivity_is_usable
                independent_sensitivity = (requirement.source_kind == "sensitivity" and isinstance(valuation, dict)
                    and intrinsic_sensitivity_is_usable(valuation.get("intrinsic_analysis")))
                if not independent_sensitivity and (not isinstance(valuation, dict) or valuation.get("status") not in {"ok", "extreme_assumption"}):
                    intrinsic = valuation.get("intrinsic_analysis") if isinstance(valuation, dict) else None
                    issues.append("verified_intrinsic_sensitivity_unavailable:" + str(intrinsic.get("reason", "inputs_missing"))
                        if requirement.source_kind == "sensitivity" and isinstance(intrinsic, dict) else "verified_valuation_unavailable")
                elif requirement.source_kind == "sensitivity":
                    sensitivity = valuation["intrinsic_analysis"]["sensitivity"] if independent_sensitivity else valuation.get("sensitivity")
                    if (not isinstance(sensitivity, (list, tuple)) or not sensitivity
                            or any(not isinstance(item, dict) or not _finite(item.get("equity_value"))
                                   or not _finite(item.get("fcf_growth")) for item in sensitivity)):
                        issues.append("verified_sensitivity_unavailable")
            state = ("fulfilled" if not issues else "missing_analysis" if analysis is None
                     else "missing_source_data" if any(issue.startswith("verified_") for issue in issues)
                     else "generated_unverified" if pending_validation
                     else "verification_failed" if unknown or not local_nodes or unproven
                     else "missing_evidence" if not sources else "missing_source_data")
            rows.append(ObligationCoverage(requirement.obligation_id, state, path,
                                          tuple(sorted(sources)), tuple(issues)))
        return ResearchCoverage(self.version,
                                all(row.state in {"fulfilled", "not_applicable"} for row in rows), tuple(rows))

    @staticmethod
    def _scenario_cases(value: Any) -> set[str]:
        aliases = {"bear": "bear", "base": "base", "bull": "bull", "悲观": "bear", "悲觀": "bear",
                   "基准": "base", "基準": "base", "乐观": "bull", "樂觀": "bull"}
        cases = set()
        if isinstance(value, dict):
            for key, child in value.items():
                canonical = aliases.get(str(key).casefold())
                if canonical and has_analysis(child):
                    cases.add(canonical)
            value = value.get("scenarios", ())
        if isinstance(value, (list, tuple)):
            for item in value:
                if not isinstance(item, dict) or not has_analysis(item):
                    continue
                canonical = aliases.get(str(item.get("name") or item.get("scenario") or "").casefold())
                if canonical:
                    cases.add(canonical)
        return cases


def prompt_contract(*, compact: bool = False) -> dict:
    """Explicit obligations, not permission to generate filler or fake citations."""
    contract = {
        "schema": CONTRACT_VERSION,
        "production_plan": obligation_production_plan(),
        "obligations": [{"id": item.obligation_id, "report_paths": item.paths} for item in OBLIGATIONS],
        "instructions": (
            "Address all applicable obligations with substantive analysis. Preserve every supplied risk, "
            "counterargument and source link. Give each analysis section evidence_ids from supplied material; "
            "never invent IDs. Include management_and_capital_allocation, existing_growth_engines, "
            "industry_changes_and_value_allocation, valuation_and_sensitivity, and evidence_and_methodology. "
            "Give separate bear/base/bull assumptions and their evidence. Copy valuation and sensitivity only "
            "from verified deterministic inputs. Missing source data is a gap, not non-applicability. "
            "For financial_beta only, an ordinary FCF valuation limitation may use applicability=not_applicable, "
            "method=ordinary_fcf_valuation and an evidence-backed reason. Never fill missing obligations with placeholders."
        ),
    }
    if compact:
        # Same obligations with no repeated internal IDs/field names. The
        # complete machine-readable mapping remains the validator's contract.
        contract.pop("obligations")
        # Routing belongs to the host. The same assigned producer plan already
        # travels in assigned_research_obligations; repeating all 18 plans here
        # can make even the minimum merge envelope exceed a small context.
        # Required questions/paths and source/quality instructions remain whole.
        contract.pop("production_plan")
        contract["required_report_sections"] = [item.paths[0] for item in OBLIGATIONS if item.paths]
        contract["historical_financial_performance"] = "verified context.metrics"
        contract["alternate_paths"] = {item.paths[0]: item.paths[1:]
                                       for item in OBLIGATIONS if len(item.paths) > 1}
    return contract
