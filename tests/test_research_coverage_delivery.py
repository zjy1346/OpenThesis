from __future__ import annotations

from copy import deepcopy
import socket
import urllib.request
import unittest
from unittest.mock import patch

from openthesis.report_document import (
    ReportDocument,
    assemble_report_document,
    render_html,
    render_markdown,
)
from openthesis.report_readiness import (
    REQUIRED_RESEARCH_ARTIFACTS,
    assess_report_readiness,
    report_readiness_notice,
)
from openthesis.research_coverage import (
    CONTRACT_VERSION,
    OBLIGATIONS,
    ResearchCoverageContract,
)
from openthesis.research import _supported_synthesis_sections
from openthesis.research_assertions import AssertionSession, assertion_session, validate_response
from openthesis.report_content import trusted_snapshot_for_artifact


REPORT_SOURCE_ID = "ev:annual-report"
FINANCIAL_FACT_ID = "ev:reported-revenue"
MARKET_SOURCE_ID = "ev:market-data"
SENSITIVITY_SOURCE_ID = "ev:valuation-sensitivity"
COMPANY_CIK = "0000001234"
EXPECTED_GAPS = (
    "historical_financial_performance",
    "current_price_expectations",
    "valuation_sensitivity",
)

_REPORT_ANALYSIS = {
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
    "bear": "Lower demand slows expansion, and rising costs compress margins.",
    "base": "Recurring demand supports measured enterprise subscription growth.",
    "bull": "Higher adoption improves operating leverage and revenue growth.",
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


def _analysis(
    text: str,
    evidence_id: str = REPORT_SOURCE_ID,
    claim_id: str = "claim:report-section",
    *,
    bind_filing: bool = True,
) -> dict:
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


def _synthesized_report() -> dict:
    report: dict = {
        "executive_summary": (
            "Recurring demand and disciplined reinvestment support durable growth, "
            "while unverified historical metrics and valuation inputs remain open."
        ),
        "claims": [{
            "claim_id": "claim:durable-demand",
            "text": "Recurring demand supports a durable operating model.",
            "kind": "analysis",
            "evidence_ids": [REPORT_SOURCE_ID],
            "company_cik": COMPANY_CIK,
            "entity": "Example Co.",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": "2025-12-31",
            "scope": "consolidated",
        }, {
            "claim_id": "claim:executive-summary",
            "text": (
                "Example Co. has retained recurring enterprise demand and disciplined "
                "reinvestment; the synthesis is preserved while historical proof and "
                "valuation work remain incomplete."
            ),
            "kind": "analysis",
            "evidence_ids": [REPORT_SOURCE_ID],
            "company_cik": COMPANY_CIK,
            "entity": "Example Co.",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": "2025-12-31",
            "scope": "consolidated",
        }],
    }
    for obligation in OBLIGATIONS:
        if not obligation.paths:
            continue
        if obligation.source_kind == "scenarios":
            value = {
                case: _analysis(
                    text, REPORT_SOURCE_ID,
                    f"claim:{obligation.obligation_id}:{case}",
                )
                for case, text in _SCENARIO_ANALYSIS.items()
            }
        else:
            text = _REPORT_ANALYSIS.get(
                obligation.obligation_id,
                _MARKET_ANALYSIS.get(obligation.obligation_id),
            )
            evidence_id = {
                "current_price_expectations": MARKET_SOURCE_ID,
                "valuation_sensitivity": SENSITIVITY_SOURCE_ID,
            }.get(obligation.obligation_id, REPORT_SOURCE_ID)
            value = _analysis(
                text,
                evidence_id,
                f"claim:{obligation.obligation_id}",
                bind_filing=evidence_id == REPORT_SOURCE_ID,
            )
        _set_path(report, obligation.paths[0], value)

    report["business_model"] = _analysis(
        _REPORT_ANALYSIS["company_business_model"],
        REPORT_SOURCE_ID,
        "claim:company_business_model",
    )
    report["executive_summary"] = (
        "Example Co. has retained recurring enterprise demand and disciplined reinvestment; "
        "the synthesis is preserved while historical proof and valuation work remain incomplete."
    )
    return report


def _financial_snapshot(*, valid_history: bool = False) -> dict:
    metrics = []
    report_text = " ".join((
        "Recurring demand supports a durable operating model.",
        "Example Co. has retained recurring enterprise demand and disciplined reinvestment; "
        "the synthesis is preserved while historical proof and valuation work remain incomplete.",
        *_REPORT_ANALYSIS.values(),
        *_SCENARIO_ANALYSIS.values(),
    ))
    evidence = [{
        "evidence_id": REPORT_SOURCE_ID,
        "kind": "filing_text",
        "document_id": "doc:annual-report-2025",
        "title": "Example Co. 2025 Annual Report",
        "locator": "page:8/table:2",
        "source_url": "https://example.test/annual-report.pdf",
        "company_cik": COMPANY_CIK,
        "entity": "Example Co.",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "end_date": "2025-12-31",
        "excerpt": report_text,
        "scope": "consolidated",
    }, {
        "evidence_id": MARKET_SOURCE_ID,
        "kind": "market_data",
        "document_id": "market:EXMP:2026-02-10",
        "title": "Example Co. market data, 2026-02-10",
        "locator": "as-of:2026-02-10",
        "source_url": "https://example.test/markets/EXMP/2026-02-10",
        "company_ticker": "EXMP",
        "as_of": "2026-02-10",
        "concept": "market expectations",
        "raw_text": _MARKET_ANALYSIS["current_price_expectations"],
    }, {
        "evidence_id": SENSITIVITY_SOURCE_ID,
        "kind": "market_data",
        "document_id": "valuation:EXMP:2026-02-10",
        "title": "Example Co. valuation sensitivity inputs, 2026-02-10",
        "locator": "as-of:2026-02-10",
        "source_url": "https://example.test/valuation/EXMP/2026-02-10",
        "company_ticker": "EXMP",
        "as_of": "2026-02-10",
        "concept": "valuation sensitivity",
        "raw_text": _MARKET_ANALYSIS["valuation_sensitivity"],
    }]
    if valid_history:
        # The revised contract requires revenue and net income source bindings
        # across multiple annual periods, not one registered amount per year.
        for year in range(2021, 2026):
            period_end = f"{year}-12-31"
            period_start = f"{year}-01-01"
            values = {
                "revenue": 3_000_000.0 + (year - 2021) * 450_000.0,
                "net_income": 220_000.0 + (year - 2021) * 38_000.0,
            }
            metric = {
                "year": year,
                "fiscal_period": "FY",
                "period_start": period_start,
                "period_end": period_end,
                "scope": "consolidated",
                **values,
                "money_metadata": {},
            }
            for concept, value in values.items():
                fact_id = f"fact:{concept}-{year}"
                evidence_id = f"ev:reported-{concept}-{year}"
                metric["money_metadata"][concept] = {
                        "fact_id": fact_id,
                        "currency": "USD",
                        "unit_provenance": "filing unitRef: USD",
                        "period": "FY",
                        "period_start": period_start,
                        "period_end": period_end,
                        "scope": "consolidated",
                }
                evidence.append({
                    "evidence_id": evidence_id,
                    "fact_id": fact_id,
                    "kind": "financial_fact",
                    "document_id": "doc:annual-report-2025",
                    "concept": concept,
                    "value": value,
                    "currency": "USD",
                    "unit": "USD",
                    "unit_provenance": "filing unitRef: USD",
                    "company_cik": COMPANY_CIK,
                    "entity": "Example Co.",
                    "fiscal_year": year,
                    "fiscal_period": "FY",
                    "start_date": period_start,
                    "end_date": period_end,
                    "scope": "consolidated",
                    "consolidated_scope": "consolidated",
                })
            metrics.append(metric)
    else:
        # This registered historical amount deliberately has no accepted metric
        # binding; it should satisfy the financial-evidence prerequisite only.
        evidence.append({
            "evidence_id": FINANCIAL_FACT_ID,
            "kind": "financial_fact",
            "concept": "revenue",
            "value": 4_800_000.0,
            "currency": "USD",
            "unit": "USD",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "company_cik": COMPANY_CIK,
            "entity": "Example Co.",
            "scope": "consolidated",
            "consolidated_scope": "consolidated",
        })
    return {
        "artifact_type": "deterministic-financial-summary",
        "artifact_id": "financial-summary-1",
        "content": {
            "currency": "USD",
            "company": {
                "ticker": "EXMP",
                "name": "Example Co.",
                "cik": COMPANY_CIK,
                "industry_support": "ordinary",
            },
            "metrics": metrics,
            "evidence": evidence,
        },
    }


def _artifact_bundle(*, valid_history: bool = False, quotations_only: bool = False) -> list[dict]:
    report = _synthesized_report()
    verification = {
        "passed": True,
        "verified_claim_count": 1,
        "unsupported_fact_count": 0,
        "claim_integrity_passed": True,
    }
    financial = _financial_snapshot(valid_history=valid_history)
    financial_content = financial["content"]
    evidence_records = {
        item["evidence_id"]: item
        for item in financial_content["evidence"]
    }
    # Presentation/coverage fixtures must contain assessed research, not just
    # copy an issuer quotation into every section and call it complete. The
    # source-bound judgments below are offline fixture data, never production
    # trust flags or a substitute for a real semantic-model evaluation.
    if not quotations_only:
        def adapt(value):
            if isinstance(value, dict):
                return {key: ("Based on the disclosed evidence, " + child if key in {"summary", "text", "executive_summary"} and isinstance(child, str)
                    else adapt(child)) for key, child in value.items()}
            if isinstance(value, list):
                return [adapt(child) for child in value]
            return value
        report = adapt(report)
        session = AssertionSession()
        with assertion_session(session):
            _supported_synthesis_sections(report, set(evidence_records), evidence_records, "en")
        queries = list(session.queries.values())
        replies = {"structured_output_valid": True, "results": [
            {"assertion_id": query["assertion_id"], "state": "supported", "reason": "Offline annotated fixture assessment",
                "source_spans": [{"evidence_id": source["evidence_id"], "start": 0, "end": len(source["raw_text"]),
                    "quote": source["raw_text"], "source_sha256": source["source_sha256"]} for source in query["sources"]]}
            for query in queries]}
        with assertion_session(AssertionSession(validate_response(replies, queries, "offline-coverage-fixture"))):
            report, section_validation = _supported_synthesis_sections(report, set(evidence_records), evidence_records, "en")
    else:
        report, section_validation = _supported_synthesis_sections(report, set(evidence_records), evidence_records, "en")
    if not any(node.get("path") == "business_model.summary"
               for node in section_validation.get("nodes", ())):
        raise AssertionError("Fixture must yield validator-produced business-model proof.")
    verification["section_validation"] = section_validation
    coverage = ResearchCoverageContract().assess(
        report,
        verification,
        evidence_records=evidence_records,
        metrics=financial_content["metrics"],
        valuation=None,
        company=financial_content["company"],
    )
    readiness = assess_report_readiness(
        report,
        verification,
        financial_evidence_count=1,
        allow_complete=True,
        coverage=coverage,
    )
    if not valid_history and not quotations_only:
        if coverage.complete or coverage.gaps != EXPECTED_GAPS:
            raise AssertionError(f"Fixture must stay honestly incomplete: {coverage.gaps}")
        if readiness.complete or readiness.state != "substantive_partial":
            raise AssertionError(f"Fixture must be substantive partial: {readiness.to_dict()}")

    report_artifact = {
        "artifact_type": "research-report",
        "artifact_id": "report-final-coverage-1",
        "title": "Integrated report",
        "content": {
            "mode": "synthesized",
            "report": report,
            "verification": verification,
            "readiness": readiness.to_dict(),
            "research_coverage_contract": CONTRACT_VERSION,
        },
    }
    prerequisite_artifacts = [
        {
            "artifact_type": artifact_type,
            "artifact_id": f"{artifact_type}-1",
            "content": {"available": True},
        }
        for artifact_type in REQUIRED_RESEARCH_ARTIFACTS
        if artifact_type != "deterministic-financial-summary"
    ]
    for item in [*prerequisite_artifacts, financial, report_artifact]:
        item["run_id"] = "fixture-run"
    snapshot = trusted_snapshot_for_artifact(report_artifact, evidence_records=list(evidence_records.values()))
    return [*prerequisite_artifacts, financial, report_artifact,
        *([{"artifact_type": "trusted-content-snapshot", "artifact_id": "fixture-trusted-snapshot", "run_id": "fixture-run", "content": snapshot}] if snapshot else [])]


def _assemble(run_label: str, artifacts: list[dict], **options):
    # All artifacts, source-bound proofs and their immutable snapshot share
    # the same run. A human-readable test label is not a new run identity.
    return assemble_report_document("fixture-run", artifacts, **options)


def _report_artifact(artifacts: list[dict]) -> dict:
    return next(item for item in artifacts if item["artifact_type"] == "research-report")


def _section(document: ReportDocument, section_id: str):
    return next(section for section in document.sections if section.section_id == section_id)


class ResearchCoverageDeliveryTests(unittest.TestCase):
    def test_quotations_in_every_section_are_not_a_completed_research_report(self) -> None:
        document = _assemble("quotes-only", _artifact_bundle(valid_history=True, quotations_only=True), language="en")
        self.assertFalse(document.readiness.complete)
        self.assertIn("company_business_model", document.readiness.coverage["gaps"])

    def test_incomplete_coverage_is_collected_in_one_gap_section(self) -> None:
        document = _assemble("run-coverage-gaps", _artifact_bundle(), language="en")
        gap_sections = [section for section in document.sections
                        if section.section_id == "research-coverage-gaps"]

        self.assertEqual(len(gap_sections), 1)
        section = gap_sections[0]
        labels = [node.text for node in section.nodes if node.kind == "list_item"]
        self.assertEqual(labels, [
            "Historical Financial Performance · Key research inputs missing",
            "Current-price Implied Expectations · Key research inputs missing",
            "Valuation and Sensitivity · Key research inputs missing",
        ])
        self.assertFalse(document.readiness.complete)
        self.assertEqual(document.readiness.coverage["gaps"], EXPECTED_GAPS)
        self.assertFalse(document.readiness.coverage["complete"])

    def test_same_partial_readiness_is_localized_across_three_languages(self) -> None:
        expected = {
            "en": (
                "Substantive partial research",
                "Historical Financial Performance",
                "Integrated analysis has been preserved",
                "Staged research",
                "Substantive results have been preserved",
            ),
            "zh-CN": (
                "阶段性研究，尚未达到完整报告条件",
                "历史财务表现",
                "已保留综合分析",
                "阶段性研究",
                "已保留有实质内容的研究结果",
            ),
            "zh-TW": (
                "階段性研究，尚未達到完整報告條件",
                "歷史財務表現",
                "已保留綜合分析",
                "階段性研究",
                "已保留具實質內容的研究結果",
            ),
        }
        for language, (status_title, gap_label, preserved_text, stage_title, readiness_message) in expected.items():
            with self.subTest(language=language):
                document = _assemble(
                    "run-coverage-localized", _artifact_bundle(), language=language
                )
                markdown = render_markdown(document)
                status = _section(document, "research-status")
                gaps = _section(document, "research-coverage-gaps")
                readiness_notice = report_readiness_notice(document.readiness.to_dict(), language)

                self.assertEqual(document.readiness.state, "substantive_partial")
                self.assertFalse(document.readiness.complete)
                self.assertEqual(document.readiness.coverage["gaps"], EXPECTED_GAPS)
                self.assertEqual(status.title, stage_title)
                self.assertIsNotNone(readiness_notice)
                self.assertEqual(readiness_notice[0], status_title)
                self.assertIn(readiness_message, readiness_notice[1])
                self.assertIn(preserved_text, status.body)
                self.assertIn(gap_label, gaps.body)
                self.assertIn(gap_label, markdown)

    def test_coverage_gap_notice_is_typed_non_substantive_content(self) -> None:
        document = _assemble("run-gap-notice", _artifact_bundle(), language="en")
        section = _section(document, "research-coverage-gaps")
        gap_nodes = [node for node in section.nodes if node.kind == "list_item"]

        self.assertFalse(section.is_substantive)
        self.assertTrue(section.is_visible)
        self.assertEqual(section.completeness, "incomplete")
        self.assertTrue(all(node.role == "notice" for node in gap_nodes))
        self.assertTrue(all(node.materiality == "non_material" for node in gap_nodes))
        self.assertFalse(document.readiness.complete)

    def test_integrated_analysis_remains_available_when_coverage_is_partial(self) -> None:
        document = _assemble("run-preserved-partial", _artifact_bundle(), language="en")
        business = _section(document, "business_model")
        executive = _section(document, "executive_summary")
        markdown = render_markdown(document)

        self.assertFalse(document.readiness.complete)
        self.assertTrue(business.is_substantive)
        self.assertIn("recurring software subscriptions", business.body)
        self.assertIn("Example Co. has retained recurring enterprise demand", executive.body)
        self.assertIn("Example Co. has retained recurring enterprise demand", markdown)
        self.assertIn("Integrated analysis has been preserved", _section(document, "research-status").body)

    def test_five_section_semantic_fields_and_typed_node_metadata_are_preserved(self) -> None:
        document = _assemble("run-section-semantics", _artifact_bundle(), language="en")
        gap_section = _section(document, "research-coverage-gaps")
        business_section = _section(document, "business_model")
        business_nodes = [node for node in business_section.nodes if node.role == "verified_analysis"]

        # The delivery block keeps all five semantic fields carried by SectionBlock.
        self.assertEqual(gap_section.verification_state, "unknown")
        self.assertFalse(gap_section.is_substantive)
        self.assertEqual(gap_section.diagnostic_code, "")
        self.assertTrue(gap_section.is_visible)
        self.assertEqual(gap_section.completeness, "incomplete")
        self.assertTrue(business_nodes)
        self.assertEqual(business_nodes[0].role, "verified_analysis")
        self.assertEqual(business_nodes[0].source_ids, (REPORT_SOURCE_ID,))
        self.assertEqual(business_nodes[0].validation_record.state, "verified")
        self.assertEqual(business_nodes[0].materiality, "material")

    def test_cached_complete_flag_and_unknown_contract_version_are_rejected(self) -> None:
        for corruption in ("complete_flag", "unknown_version"):
            with self.subTest(corruption=corruption):
                artifacts = deepcopy(_artifact_bundle())
                final = _report_artifact(artifacts)
                if corruption == "complete_flag":
                    final["content"]["readiness"]["complete"] = True
                    final["content"]["readiness"]["state"] = "complete"
                    final["content"]["readiness"]["coverage"]["complete"] = True
                    final["content"]["readiness"]["coverage"]["gaps"] = []
                else:
                    final["content"]["research_coverage_contract"] = "openthesis.research-coverage.unknown"
                    final["content"]["readiness"]["coverage"]["contract_version"] = "openthesis.research-coverage.unknown"

                document = _assemble("run-tampered-coverage", artifacts, language="en")

                self.assertFalse(document.readiness.complete)
                self.assertEqual(document.readiness.state, "substantive_partial")
                self.assertEqual(document.readiness.coverage["gaps"], EXPECTED_GAPS)
                if corruption == "unknown_version":
                    self.assertIn("research_coverage_contract_invalid", document.readiness.issues)

    def test_legacy_body_stays_readable_but_old_global_pass_flag_is_not_completion_proof(self) -> None:
        artifacts = _artifact_bundle(valid_history=True)
        final = _report_artifact(artifacts)
        final["content"].pop("readiness")
        final["content"].pop("research_coverage_contract")

        document = _assemble("run-legacy-coverage", artifacts, language="en")

        self.assertFalse(document.readiness.complete)
        self.assertIn("report_verification_unknown", document.readiness.issues)
        self.assertIsNone(document.readiness.coverage)
        self.assertTrue(_section(document, "business_model").is_substantive)

    def test_current_report_cannot_bypass_coverage_by_losing_cached_readiness(self) -> None:
        for cached_readiness in (None, "damaged metadata", []):
            with self.subTest(cached_readiness=cached_readiness):
                artifacts = _artifact_bundle()
                final = _report_artifact(artifacts)
                final["content"]["readiness"] = cached_readiness
                document = _assemble("run-missing-readiness", artifacts, language="en")
                self.assertFalse(document.readiness.complete)
                self.assertIsNotNone(document.readiness.coverage)
                self.assertEqual(document.readiness.coverage["gaps"], EXPECTED_GAPS)

    def test_read_only_delivery_does_not_mutate_artifacts_or_use_network(self) -> None:
        artifacts = _artifact_bundle()
        before = deepcopy(artifacts)
        with patch.object(socket, "create_connection", side_effect=AssertionError("network access attempted")):
            with patch.object(urllib.request, "urlopen", side_effect=AssertionError("network access attempted")):
                document = _assemble("run-read-only-delivery", artifacts, language="en")
                markdown = render_markdown(document)
                html = render_html(document)

        self.assertEqual(artifacts, before)
        self.assertIn("Remaining Research Obligations", markdown)
        self.assertIn("Remaining Research Obligations", html)
        self.assertFalse(document.readiness.complete)


if __name__ == "__main__":
    unittest.main()
