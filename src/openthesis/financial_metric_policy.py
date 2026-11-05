"""Industry applicability for derived metrics, never admission of source facts.

Generic cash-flow arithmetic is not a bank valuation or earnings-quality
contract. Preserve the original observations and their provenance while making
inapplicable derived values unavailable to reports and model context alike.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from typing import Any

from .domain import Company
from .i18n import EN, ZH_HANT, normalize_language

METRIC_POLICY_VERSION = "industry-metric-applicability-v1"
BANK_INAPPLICABLE = ("free_cash_flow", "cash_conversion", "financial_tensions")


def metric_policy_identity(profile_id: str) -> dict[str, str] | None:
    return {"profile_id": "bank", "version": METRIC_POLICY_VERSION} if profile_id == "bank" else None


def metric_profile(subject: Company | dict[str, Any] | None) -> str:
    # One shared classifier, not a second ticker/name-based industry registry.
    from .financial_compiler import CoveragePlanner
    if isinstance(subject, dict):
        values = {item.name: subject[item.name] for item in fields(Company) if item.name in subject}
        subject = Company(**{"cik": "", "ticker": "", "name": "", **values})
    return CoveragePlanner().plan(subject).profile_id if isinstance(subject, Company) else "non_financial"


def apply_metric_policy(rows: list[dict[str, Any]], *, profile_id: str) -> list[dict[str, Any]]:
    result = deepcopy(rows)
    if profile_id != "bank":
        return result
    for row in result:
        if not isinstance(row, dict):
            continue
        row.update(
            free_cash_flow=None, free_cash_flow_money=None,
            free_cash_flow_gap="industry_not_applicable",
            cash_conversion=None, cash_conversion_formula=None, cash_conversion_scope=None,
            cash_conversion_status="not_applicable", financial_tensions=[],
            metric_applicability={"profile_id": "bank", "version": METRIC_POLICY_VERSION,
                                  "not_applicable": list(BANK_INAPPLICABLE)},
        )
        # A previously serialized derived-money entry is not a raw source
        # observation. Keep OCF/CapEx metadata unchanged, never clear all money.
        if isinstance(row.get("money_metadata"), dict):
            row["money_metadata"].pop("free_cash_flow", None)
    return result


def metric_applicable(row: dict[str, Any], metric: str) -> bool:
    policy = row.get("metric_applicability", {})
    return not (isinstance(policy, dict) and policy.get("profile_id") == "bank"
                and metric in BANK_INAPPLICABLE)


def evidence_metric_policy(records: list[dict[str, Any]], *, profile_id: str) -> list[dict[str, Any]]:
    """Host metadata for calculation applicability, not another fact value."""
    result = deepcopy(records)
    if profile_id == "bank":
        for record in result:
            if isinstance(record, dict) and record.get("kind") == "financial_fact":
                record["metric_profile_id"] = "bank"
    return result


def project_metric_artifacts(artifacts: list[dict[str, Any]], subject: Company | dict[str, Any], *, run_id: str) -> list[dict[str, Any]]:
    """Apply saved issuer identity to legacy read views, never rewrite storage."""
    if metric_profile(subject) != "bank":
        return list(artifacts)
    result = []
    industry = subject.industry if isinstance(subject, Company) else subject.get("industry", "")
    for row in artifacts:
        if row.get("run_id") != run_id or row.get("artifact_type") != "deterministic-financial-summary":
            result.append(row)
            continue
        projected = deepcopy(row)
        result.append(projected)
        content = projected.get("content")
        if not isinstance(content, dict):
            continue
        identity = content.get("company", {})
        content["company"] = {**(identity if isinstance(identity, dict) else {}), "industry": industry}
        for name in ("metrics", "interim_metrics"):
            if isinstance(content.get(name), list):
                content[name] = apply_metric_policy(content[name], profile_id="bank")
    return result


def bank_metric_notice(language: str) -> str:
    locale = normalize_language(language)
    if locale == EN:
        return ("Bank metric applicability: reported operating cash flow and capital expenditure are retained as source observations. "
                "Ordinary-company free cash flow, cash conversion and earnings–cash-flow divergence tests are not applicable "
                "as bank valuation or earnings-quality measures. Debt to assets is not a regulatory capital ratio. "
                "Bank-specific indicators require their own verified disclosures; none are inferred from these amounts.")
    if locale == ZH_HANT:
        return ("銀行指標適用性：保留披露的經營現金流與資本開支作為原始觀察；不將普通企業自由現金流、現金利潤轉化率或盈利與現金流背離用於銀行估值及盈利品質判斷。"
                "資產負債率不等於監管資本比率。銀行專用指標須有各自已核驗披露，不從上述金額推算。")
    return ("银行指标适用性：保留披露的经营现金流与资本开支作为原始观察；不将普通企业自由现金流、现金利润转化率或盈利与现金流背离用于银行估值及盈利质量判断。"
            "资产负债率不等于监管资本比率。银行专用指标须有各自已核验披露，不从上述金额推算。")
