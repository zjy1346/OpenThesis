from dataclasses import replace
from copy import deepcopy

from openthesis.domain import ResearchRun
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from openthesis.financial_contracts import CompilationPolicy, SourceAmount
from openthesis.markets import build_company
from openthesis.service import AppService, _canonical_snapshot_digest, _reporting_currency_identity
from openthesis.storage import Storage
from test_source_evidence_flow import source_fixture


def verified_currency_run(store):
    _company, original, base, row, unit = source_fixture()
    # A legacy run with absent reporting currency, not a market-profile hint.
    company = replace(build_company("09988.HK", "Currency fixture"), reporting_currency="")
    filing = replace(original, company_cik=company.cik)
    unit = replace(unit, excerpt="单位：美元 币种：美元 USD")
    facts, evidence = [], [unit]
    for concept, label, value, statement in (
        ("revenue", "营业收入", 100, "income_statement"),
        ("net_income", "净利润", 10, "income_statement"),
        ("operating_cash_flow", "经营活动产生的现金流量净额", 15, "cash_flow"),
        ("assets", "资产总计", 1000, "balance_sheet"),
        ("liabilities", "负债合计", 400, "balance_sheet"),
        ("total_equity", "所有者权益合计", 600, "balance_sheet"),
    ):
        identity = "currency:" + concept
        text = f"{label} {value} USD"
        fact = replace(base, fact_id=identity, company_cik=company.cik, concept=concept,
            reported_concept=label, value=float(value), currency="USD", unit="USD", statement=statement,
            start_date=None if statement == "balance_sheet" else base.start_date,
            period_start=None if statement == "balance_sheet" else base.period_start,
            raw_text=text, normalization_lineage=SourceAmount(str(value), "1", "money", "USD", (unit.evidence_id,)).normalize())
        facts.append(fact)
        evidence.append(replace(row, evidence_id="fact:" + identity, excerpt=text))
    canonical = FinancialPipeline(None).prepare(FinancialResearchSnapshot.from_facts(
        company, [filing], facts, evidence=evidence), CompilationPolicy(reporting_currency="")).canonical_dataset
    assert canonical.allow_ai and len(canonical.research_facts) == 6, (canonical.coverage, [(group.identity, group.validation.status, group.validation.issues) for group in canonical.group_validations])
    store.save_company(company)
    store.save_filings([filing])
    store.replace_financial_ingestion(company.cik, [filing.accession_number],
        list(canonical.research_facts), validation_groups=canonical.group_validations, evidence=evidence)
    run = ResearchRun("currency-run", company, "long-term-fundamentals", "", "", "offline", "", "2025-12-31")
    run.data_snapshot = {"financial_fact_ids_sha256": _canonical_snapshot_digest(sorted(f.fact_id for f in canonical.research_facts))}
    store.save_run(run)
    return run


def test_official_snapshot_recovers_usd_not_hkd_and_does_not_change_saved_identity(tmp_path):
    service = AppService(tmp_path)
    run = verified_currency_run(service.storage)
    payload = run.to_dict()
    before = deepcopy(payload)
    company, confirmation = _reporting_currency_identity(service.storage, payload["company"], payload)
    assert company["reporting_currency"] == "USD" and company["listing_currency"] == "HKD"
    assert confirmation["state"] == "official_verified" and len(confirmation["fact_ids"]) == 6
    assert payload == before
    # Lightweight history summaries retain the saved identity; opening the
    # report below performs snapshot-bound recovery, not startup-wide I/O.
    assert service.list_research_runs()[0]["reporting_currency"] == ""
    report = AppService(tmp_path).get_report(run.run_id)
    assert report["reporting_currency"] == "USD"
    assert report["reporting_currency_confirmation"]["state"] == "official_verified"


def test_stale_snapshot_cannot_recover_currency_and_listing_is_not_evidence(tmp_path):
    store = Storage(tmp_path)
    run = verified_currency_run(store)
    payload = run.to_dict()
    payload["data_snapshot"]["financial_fact_ids_sha256"] = "0" * 64
    company, confirmation = _reporting_currency_identity(store, payload["company"], payload)
    assert company["reporting_currency"] == "" and confirmation["state"] == "unresolved"
