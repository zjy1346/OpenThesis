from __future__ import annotations

from copy import deepcopy
import socket
import urllib.request
import unittest
from unittest.mock import patch

from openthesis.research import _supported_synthesis_sections
from openthesis.research_assertions import AssertionSession, assertion_session, validate_response
from openthesis.research_coverage import (
    OBLIGATIONS,
    ResearchCoverageContract,
    has_analysis,
    prompt_contract,
)


DOCUMENT_ID = "source:annual-report-2025"
MARKET_ID = "source:market-data-2026"
SENSITIVITY_ID = "source:sensitivity-data-2026"
COMPANY_CIK = "0000001234"

_SECTION_ANALYSIS = {
    "company_business_model": "Example Co. sells recurring software subscriptions to enterprise customers.",
    "earnings_quality": "Reported earnings are supported by recurring revenue and operating cash generation.",
    "balance_sheet_strength": "Cash and manageable liabilities support balance-sheet flexibility.",
    "competitive_advantages": "Customer switching costs and strong renewals strengthen competitive position.",
    "management_capital_allocation": "Management reinvests in product development while keeping capital allocation disciplined.",
    "existing_growth_engines": "Existing growth engines are enterprise subscriptions and expansion within installed accounts.",
    "potential_growth_opportunities": "Potential growth opportunities include international expansion and new product modules.",
    "industry_value_allocation": "Industry value is shifting toward providers with trusted cloud infrastructure.",
    "growth_counterarguments": "Counterarguments include slower renewals, rising costs, and competitive price pressure.",
    "investment_thesis": "Recurring enterprise demand and disciplined reinvestment support the investment thesis.",
    "thesis_invalidation": "A sustained decline in customer renewals would invalidate the growth thesis.",
    "leading_indicators": "Customer renewal rates and subscription expansion are leading indicators.",
    "unresolved_questions": "Additional disclosure is needed to resolve sensitivity to customer renewal rates.",
    "evidence_methodology": "The analysis compares reported consolidated results across annual filing periods.",
}
_SCENARIO_ANALYSIS = {
    "bear": "Lower renewal rates reduce growth; disciplined costs soften margin pressure.",
    "base": "Recurring demand supports measured enterprise subscription expansion.",
    "bull": "Higher adoption improves operating leverage and subscription revenue growth.",
}
_MARKET_ANALYSIS = {
    "current_price_expectations": "Market expectations imply continued enterprise subscription revenue growth.",
    "valuation_sensitivity": "Valuation sensitivity shows enterprise value changes as free cash flow growth assumptions change.",
}


def _set_path(target: dict, path: str, value: object) -> None:
    parts = path.split(".")
    current = target
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise AssertionError(f"Cannot assign through non-object path: {path}")
        current = child
    current[parts[-1]] = value


def _evidence_records() -> dict[str, dict]:
    filing_text = " ".join((
        "Recurring demand supports a durable operating model.",
        *_SECTION_ANALYSIS.values(),
        *_SCENARIO_ANALYSIS.values(),
        "No verified free cash flow projections are supplied in the annual filing.",
    ))
    records = {
        DOCUMENT_ID: {
            "kind": "filing_text",
            "evidence_id": DOCUMENT_ID,
            "document_id": "doc:annual-report-2025",
            "title": "Example Co. 2025 annual report",
            "path": "fixtures/research_coverage/example-2025-annual-report.pdf",
            "sha256": "a" * 64,
            "source_url": "https://example.invalid/reports/example-2025.pdf",
            "locator": "page:7-12",
            "company_cik": COMPANY_CIK,
            "entity": "Example Co.",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": "2025-12-31",
            "excerpt": filing_text,
        },
        MARKET_ID: {
            "kind": "market_data",
            "evidence_id": MARKET_ID,
            "document_id": "market:example:2026-02-10",
            "title": "Example Co. market data, 2026-02-10",
            "source_url": "https://example.invalid/markets/EXMP/2026-02-10",
            "locator": "as-of:2026-02-10",
            "company_ticker": "EXMP",
            "as_of": "2026-02-10",
            "concept": "market expectations",
            "raw_text": _MARKET_ANALYSIS["current_price_expectations"],
        },
        SENSITIVITY_ID: {
            "kind": "market_data",
            "evidence_id": SENSITIVITY_ID,
            "document_id": "valuation:example:2026-02-10",
            "title": "Example Co. valuation sensitivity inputs, 2026-02-10",
            "source_url": "https://example.invalid/valuation/EXMP/2026-02-10",
            "locator": "as-of:2026-02-10",
            "company_ticker": "EXMP",
            "as_of": "2026-02-10",
            "concept": "valuation sensitivity",
            "raw_text": _MARKET_ANALYSIS["valuation_sensitivity"],
        },
        "source:bank-annual-report-2025": {
            "kind": "filing_text",
            "evidence_id": "source:bank-annual-report-2025",
            "document_id": "doc:bank-annual-report-2025",
            "title": "Example Bank 2025 annual report",
            "source_url": "https://example.invalid/reports/bank-2025.pdf",
            "locator": "page:14",
            "company_cik": "0000099999",
            "entity": "Example Bank",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": "2025-12-31",
            "excerpt": "A bank's balance-sheet and regulatory capital economics require other valuation methods.",
        },
    }
    for fiscal_year, revenue, net_income in (
        (2024, 4_250_000.0, 310_000.0),
        (2025, 4_800_000.0, 365_000.0),
    ):
        for concept, amount in (("revenue", revenue), ("net_income", net_income)):
            fact_id = f"fact:{concept}-{fiscal_year}"
            records[f"evidence:{concept}-{fiscal_year}"] = {
                "kind": "financial_fact",
                "fact_id": fact_id,
                "document_id": "doc:annual-report-2025",
                "concept": concept,
                "fiscal_year": fiscal_year,
                "value": amount,
                "currency": "USD",
                "unit": "USD",
                "unit_provenance": "filing unitRef: USD",
                "company_cik": COMPANY_CIK,
                "entity": "Example Co.",
                "fiscal_period": "FY",
                "start_date": f"{fiscal_year}-01-01",
                "end_date": f"{fiscal_year}-12-31",
                "scope": "consolidated",
                "consolidated_scope": "consolidated",
                "page": fiscal_year - 2018,
            }
    return records


def _financial_metrics() -> list[dict]:
    return [
        {
            "year": fiscal_year,
            "fiscal_period": "FY",
            "period_start": f"{fiscal_year}-01-01",
            "period_end": f"{fiscal_year}-12-31",
            "scope": "consolidated",
            "revenue": revenue,
            "net_income": net_income,
            "money_metadata": {
                "revenue": {
                    "fact_id": f"fact:revenue-{fiscal_year}",
                    "currency": "USD",
                    "unit_provenance": "filing unitRef: USD",
                    "period": "FY",
                    "period_start": f"{fiscal_year}-01-01",
                    "period_end": f"{fiscal_year}-12-31",
                    "scope": "consolidated",
                },
                "net_income": {
                    "fact_id": f"fact:net_income-{fiscal_year}",
                    "currency": "USD",
                    "unit_provenance": "filing unitRef: USD",
                    "period": "FY",
                    "period_start": f"{fiscal_year}-01-01",
                    "period_end": f"{fiscal_year}-12-31",
                    "scope": "consolidated",
                },
            },
        }
        for fiscal_year, revenue, net_income in (
            (2024, 4_250_000.0, 310_000.0),
            (2025, 4_800_000.0, 365_000.0),
        )
    ]


def _analysis(text: str, evidence_id: str, claim_id: str, *, bind_filing: bool) -> dict:
    text = "Based on the disclosed evidence, " + text
    claim = {
        "claim_id": claim_id,
        "kind": "inference",
        "text": text,
        "evidence_ids": [evidence_id],
    }
    if bind_filing:
        claim.update(
            company_cik=COMPANY_CIK,
            entity="Example Co.",
            fiscal_year=2025,
            fiscal_period="FY",
            end_date="2025-12-31",
            scope="consolidated",
        )
    return {"summary": text, "evidence_ids": [evidence_id], "claims": [claim]}


def _valid_inputs() -> dict:
    report: dict = {}
    for obligation in OBLIGATIONS:
        if not obligation.paths:
            continue
        if obligation.source_kind == "scenarios":
            value = {
                case: _analysis(
                    text, DOCUMENT_ID,
                    f"claim:{obligation.obligation_id}:{case}", bind_filing=True,
                )
                for case, text in _SCENARIO_ANALYSIS.items()
            }
        else:
            text = _SECTION_ANALYSIS.get(
                obligation.obligation_id,
                _MARKET_ANALYSIS.get(obligation.obligation_id),
            )
            evidence_id = {
                "current_price_expectations": MARKET_ID,
                "valuation_sensitivity": SENSITIVITY_ID,
            }.get(obligation.obligation_id, DOCUMENT_ID)
            value = _analysis(
                text,
                evidence_id,
                f"claim:{obligation.obligation_id}",
                bind_filing=evidence_id == DOCUMENT_ID,
            )
        _set_path(report, obligation.paths[0], value)

    return {
        "report": report,
        "verification": {"verified_claim_count": 8, "unsupported_fact_count": 0},
        "evidence_records": _evidence_records(),
        "metrics": _financial_metrics(),
        "valuation": {
            "status": "ok",
            "sensitivity": [
                {"equity_value": 72.5, "fcf_growth": 0.04},
                {"equity_value": 91.0, "fcf_growth": 0.06},
            ],
        },
        "company": {"ticker": "EXMP", "industry_support": "ordinary"},
    }


def _assessment(**overrides):
    inputs = _valid_inputs()
    inputs.update(overrides)
    def validate():
        return _supported_synthesis_sections(inputs["report"], set(inputs["evidence_records"]),
            inputs["evidence_records"], "en", valuation=inputs["valuation"])
    collector = AssertionSession()
    with assertion_session(collector):
        validate()
    known = {"Based on the disclosed evidence, " + text for text in (
        *_SECTION_ANALYSIS.values(), *_SCENARIO_ANALYSIS.values(), *_MARKET_ANALYSIS.values())}
    queries = list(collector.queries.values())
    replies = {"structured_output_valid": True, "results": [{"assertion_id": query["assertion_id"], "state": "supported",
        "reason": "Offline annotated known fixture analysis",
        "source_spans": [{"evidence_id": source["evidence_id"], "start": 0,
            "end": len(source["raw_text"]), "quote": source["raw_text"],
            "source_sha256": source["source_sha256"]} for source in query["sources"]]}
        for query in queries if query["text"] in known]}
    with assertion_session(AssertionSession(validate_response(replies, queries, "offline-coverage-fixture"))):
        safe_report, section_validation = validate()
    verification = deepcopy(inputs["verification"])
    verification["section_validation"] = section_validation
    # This module exercises the coverage contract against the original
    # candidate plus the independently generated body-bound audit. Delivery
    # tests exercise the sanitized projection that is rendered to users.
    coverage = ResearchCoverageContract().assess(
        inputs["report"],
        verification,
        evidence_records=inputs["evidence_records"],
        metrics=inputs["metrics"],
        valuation=inputs["valuation"],
        company=inputs["company"],
    )
    return coverage, safe_report, section_validation


def _coverage(**overrides):
    return _assessment(**overrides)[0]


class ResearchCoverageContractTests(unittest.TestCase):
    def test_contract_has_exactly_eighteen_unique_obligations(self) -> None:
        identifiers = [item.obligation_id for item in OBLIGATIONS]

        self.assertEqual(len(identifiers), 18)
        self.assertEqual(len(set(identifiers)), 18)
        self.assertEqual(
            [item["id"] for item in prompt_contract()["obligations"]],
            identifiers,
        )

    def test_source_backed_analysis_and_verified_inputs_cover_all_obligations(self) -> None:
        result, _, section_validation = _assessment()

        self.assertTrue(result.complete)
        self.assertEqual(result.gaps, ())
        self.assertEqual(len(result.obligations), 18)
        self.assertTrue(all(item.state == "fulfilled" for item in result.obligations))
        self.assertIn(DOCUMENT_ID, result.obligations[0].evidence_ids)
        history = next(item for item in result.obligations
                       if item.obligation_id == "historical_financial_performance")
        self.assertEqual(history.state, "fulfilled")
        self.assertTrue(any(item.startswith("evidence:revenue-") for item in history.evidence_ids))
        self.assertTrue(any(
            node["path"] == "business_model.summary"
            and node["validator_version"]
            for node in section_validation["nodes"]
        ))

    def test_section_titles_alone_do_not_cover_the_obligations(self) -> None:
        inputs = _valid_inputs()
        report: dict = {}
        for obligation in OBLIGATIONS:
            if obligation.paths:
                title = obligation.obligation_id.replace("_", " ").title()
                _set_path(report, obligation.paths[0], title)

        result = _coverage(report=report)

        self.assertFalse(result.complete)
        self.assertIn("source_links_missing", result.obligations[0].issues)
        self.assertTrue(any(item.state != "fulfilled" for item in result.obligations))

    def test_long_trilingual_notice_and_placeholder_text_is_not_analysis(self) -> None:
        variants = (
            ("notice", "Research is still pending; this notice does not contain evidence-backed analysis."),
            ("placeholder", "当前研究仍在等待可核验材料，本段仅说明稍后补充，不构成事实分析。"),
            ("notice", "目前研究仍待可核實來源，本段只是後續補充提示，並非實質分析。"),
        )
        report: dict = {}
        index = 0
        for obligation in OBLIGATIONS:
            if not obligation.paths:
                continue
            role, text = variants[index % len(variants)]
            index += 1
            _set_path(report, obligation.paths[0], {
                "role": role,
                "text": text,
                "evidence_ids": [DOCUMENT_ID],
            })

        result = _coverage(report=report)

        self.assertFalse(result.complete)
        self.assertTrue(all(item.state != "fulfilled" for item in result.obligations
                            if item.obligation_id != "historical_financial_performance"))

    def test_numbers_and_identifier_only_values_are_not_analysis(self) -> None:
        identifier_record = {
            "claim_id": "claim:net-income-2025",
            "fact_id": "fact:net_income-2025",
            "document_id": "doc:annual-report-2025",
            "evidence_ids": ["evidence:net-income-2025"],
            "page": 7,
            "fiscal_year": 2025,
            "verified": True,
        }
        for value in (0, 2025, 4_800_000.0, identifier_record):
            with self.subTest(value=value):
                self.assertFalse(has_analysis(value))
        identifier_texts = (
            "fact:net_income-2025",
            "evidence:annual-report-2025",
            "source:annual-report-2025",
            "claim:net-income-2025",
            "document:annual-report-2025",
            "run:research-2025",
        )
        self.assertEqual([text for text in identifier_texts if has_analysis(text)], [])

    def test_missing_source_unknown_reference_and_local_claim_rejection_are_isolated(self) -> None:
        no_source = _valid_inputs()
        no_source["report"] = deepcopy(no_source["report"])
        no_source["report"]["business_model"]["evidence_ids"] = []
        no_source["report"]["business_model"]["claims"][0]["evidence_ids"] = []
        no_source_result = _coverage(report=no_source["report"])
        business_model = next(item for item in no_source_result.obligations
                              if item.obligation_id == "company_business_model")
        self.assertFalse(no_source_result.complete)
        self.assertIn("source_links_missing", business_model.issues)

        unknown_id = _valid_inputs()
        unknown_id["report"] = deepcopy(unknown_id["report"])
        unknown_id["report"]["business_model"]["evidence_ids"] = ["source:not-registered"]
        unknown_id["report"]["business_model"]["claims"][0]["evidence_ids"] = [
            "source:not-registered"
        ]
        unknown_result, unknown_safe, unknown_audit = _assessment(
            report=unknown_id["report"]
        )
        unknown_business = next(item for item in unknown_result.obligations
                                if item.obligation_id == "company_business_model")
        self.assertFalse(unknown_result.complete)
        self.assertNotEqual(unknown_business.state, "fulfilled")
        self.assertEqual(unknown_safe["business_model"]["summary"],
                         unknown_id["report"]["business_model"]["summary"])
        self.assertEqual(unknown_business.state, "generated_unverified")
        self.assertTrue(any(
            item["path"] == "business_model.evidence_ids"
            and item["reason"] == "unknown_or_invalid_evidence"
            for item in unknown_audit["pending_paths"]
        ))

        unverified, _, unverified_audit = _assessment(
            verification={"verified_claim_count": 2, "unsupported_fact_count": 1}
        )
        self.assertTrue(unverified.complete)
        self.assertTrue(unverified_audit["nodes"])
        self.assertTrue(all(item.state == "fulfilled" for item in unverified.obligations))

        bad_local = _valid_inputs()
        bad_local["report"] = deepcopy(bad_local["report"])
        bad_local["report"]["business_model"]["claims"] = [{
            "claim_id": "claim:wrong-revenue-amount",
            "kind": "fact",
            "text": "Reported revenue was USD 99,999,999.",
            "concept": "revenue",
            "value": 99_999_999.0,
            "currency": "USD",
            "unit": "USD",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": "2025-12-31",
            "company_cik": COMPANY_CIK,
            "entity": "Example Co.",
            "scope": "consolidated",
            "evidence_ids": ["evidence:revenue-2025"],
        }]
        bad_local_result, bad_local_safe, bad_local_audit = _assessment(
            report=bad_local["report"]
        )
        bad_local_business = next(
            item for item in bad_local_result.obligations
            if item.obligation_id == "company_business_model"
        )
        self.assertEqual(bad_local_business.state, "fulfilled")
        self.assertIn("business_model", bad_local_safe)
        self.assertNotIn("claims", bad_local_safe["business_model"])
        self.assertTrue(any(
            item["path"] == "business_model.claims[0]"
            and item["reason"] == "claim_not_verified"
            for item in bad_local_audit["quarantined_paths"]
        ))

    def test_each_bear_base_bull_case_requires_its_own_analysis(self) -> None:
        for missing_case in ("bear", "base", "bull"):
            with self.subTest(missing_case=missing_case):
                inputs = _valid_inputs()
                inputs["report"] = deepcopy(inputs["report"])
                del inputs["report"]["scenarios"][missing_case]

                result = _coverage(report=inputs["report"])
                scenario = next(item for item in result.obligations
                                if item.obligation_id == "operating_scenarios")

                self.assertFalse(result.complete)
                self.assertIn("bear_base_bull_analysis_missing", scenario.issues)

    def test_missing_quote_or_fcf_is_not_non_applicability_for_ordinary_company(self) -> None:
        for reason in ("The current market quote is unavailable.", "Verified free cash flow is missing."):
            with self.subTest(reason=reason):
                inputs = _valid_inputs()
                inputs["report"] = deepcopy(inputs["report"])
                not_applicable = {
                    "applicability": "not_applicable",
                    "method": "ordinary_fcf_valuation",
                    "reason": reason,
                    "evidence_ids": [DOCUMENT_ID],
                }
                inputs["report"]["implied_expectations"] = deepcopy(not_applicable)
                inputs["report"]["valuation_and_sensitivity"] = deepcopy(not_applicable)
                inputs["valuation"] = {"status": "unavailable"}

                result = _coverage(
                    report=inputs["report"],
                    valuation=inputs["valuation"],
                    company=inputs["company"],
                )
                valuation_rows = [item for item in result.obligations
                                  if item.obligation_id in {"current_price_expectations", "valuation_sensitivity"}]

                self.assertFalse(result.complete)
                self.assertTrue(all(item.state != "not_applicable" for item in valuation_rows))

    def test_only_explicit_financial_beta_rule_can_make_valuation_not_applicable(self) -> None:
        inputs = _valid_inputs()
        inputs["report"] = deepcopy(inputs["report"])
        limitation = {
            "applicability": "not_applicable",
            "method": "ordinary_fcf_valuation",
            "reason": "A bank's balance-sheet and regulatory capital economics require other valuation methods.",
            "evidence_ids": ["source:bank-annual-report-2025"],
        }
        inputs["report"]["implied_expectations"] = deepcopy(limitation)
        inputs["report"]["valuation_and_sensitivity"] = deepcopy(limitation)

        beta_result = _coverage(
            report=inputs["report"],
            company={"ticker": "BANK", "industry_support": "financial_beta"},
            valuation=None,
        )
        beta_rows = [item for item in beta_result.obligations
                     if item.obligation_id in {"current_price_expectations", "valuation_sensitivity"}]
        self.assertTrue(beta_result.complete)
        self.assertTrue(all(item.state == "not_applicable" for item in beta_rows))
        self.assertTrue(all(item.applicability_rule == "financial_beta_ordinary_fcf_valuation"
                            for item in beta_rows))

        ordinary_result = _coverage(
            report={
                **deepcopy(inputs["report"]),
                "implied_expectations": {
                    **deepcopy(limitation),
                    "reason": "Verified free cash flow is missing.",
                    "evidence_ids": [DOCUMENT_ID],
                },
                "valuation_and_sensitivity": {
                    **deepcopy(limitation),
                    "reason": "Verified free cash flow is missing.",
                    "evidence_ids": [DOCUMENT_ID],
                },
            },
            company={"ticker": "EXMP", "industry_support": "ordinary"},
            valuation=None,
        )
        ordinary_rows = [item for item in ordinary_result.obligations
                         if item.obligation_id in {"current_price_expectations", "valuation_sensitivity"}]
        self.assertFalse(ordinary_result.complete)
        self.assertTrue(all(item.state != "not_applicable" for item in ordinary_rows))

    def test_historical_years_or_amounts_without_registered_fact_metadata_are_gaps(self) -> None:
        cases = (
            [{"fiscal_year": 2024}, {"fiscal_year": 2025}],
            [{"fiscal_year": 2024, "revenue": 4_250_000.0}],
            [{
                "fiscal_year": 2025,
                "revenue": 4_800_000.0,
                "money_metadata": {
                    "revenue": {
                        "fact_id": "fact:not-registered",
                        "unit_provenance": "filing unitRef: USD",
                    },
                },
            }],
        )
        for metrics in cases:
            with self.subTest(metrics=metrics):
                result = _coverage(metrics=metrics)
                history = next(item for item in result.obligations
                               if item.obligation_id == "historical_financial_performance")

                self.assertFalse(result.complete)
                self.assertIn(history.state, {"missing_source_data", "verification_failed"})
                self.assertTrue(history.issues)

    def test_registered_fact_id_does_not_bless_wrong_amount_currency_year_or_concept(self) -> None:
        for field, value in (("value", 7.0), ("currency", "CNY"),
                             ("fiscal_year", 2023), ("concept", "assets")):
            with self.subTest(field=field):
                inputs = _valid_inputs()
                inputs["evidence_records"]["evidence:revenue-2025"][field] = value
                result = _coverage(**inputs)
                history = next(row for row in result.obligations
                               if row.obligation_id == "historical_financial_performance")
                self.assertFalse(result.complete)
                self.assertEqual(history.state, "verification_failed")
                self.assertIn("financial_metric_source_mismatch", history.issues)

    def test_assessment_does_not_mutate_inputs_or_use_network(self) -> None:
        inputs = _valid_inputs()
        before = deepcopy(inputs)
        with patch.object(socket, "create_connection", side_effect=AssertionError("network access attempted")):
            with patch.object(urllib.request, "urlopen", side_effect=AssertionError("network access attempted")):
                result = _coverage(**inputs)

        self.assertTrue(result.complete)
        self.assertEqual(inputs, before)


if __name__ == "__main__":
    unittest.main()
