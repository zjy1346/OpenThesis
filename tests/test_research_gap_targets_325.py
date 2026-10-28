"""Approved 3.2.5 research-start scoping regressions at the public use case."""
from dataclasses import replace
from hashlib import sha256

from openthesis.application.financials import OfficialFinancialRequest
from openthesis.application.research_preparation import NewResearchPreparer, ResearchPreparationRequest
from openthesis.domain import FilingDocument
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.sec_client import SecFinancialSourceAdapter
from test_inline_xbrl_source import source
from test_official_financial_preparation import setup_preparer


def _annual_original(company, year, accession_suffix, concepts, path):
    """Create a hash-bound original whose inline facts are parsed by production code."""
    start = f"{year - 1}-01-27"
    end = f"{year}-01-25"
    instant = f'''<x:context id="instant"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">{company.cik}</x:identifier></x:entity>
      <x:period><x:instant>{end}</x:instant></x:period></x:context>'''
    concept_tags = {
        "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "c"),
        "net_income": ("NetIncomeLoss", "c"),
        "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities", "c"),
        "assets": ("Assets", "instant"),
        "liabilities": ("Liabilities", "instant"),
        "equity": ("StockholdersEquity", "instant"),
    }
    numbers = "".join(
        f'<ix:nonFraction name="g:{concept_tags[concept][0]}" '
        f'contextRef="{concept_tags[concept][1]}" unitRef="u" '
        f'format="ixt:num-dot-decimal">{100 + index}</ix:nonFraction>'
        for index, concept in enumerate(concepts)
    )
    _, template, raw = source(numbers=numbers, extra_context=instant)
    raw = raw.replace(b"2025-01-27", start.encode())
    raw = raw.replace(b"2026-01-25", end.encode())
    raw = raw.replace(b">2026<", f">{year}<".encode())
    path.write_bytes(raw)
    accession = f"0001045810-{str(year)[-2:]}-{accession_suffix}"
    return replace(
        template,
        document_id=f"annual-{year}",
        accession_number=accession,
        period_end=end,
        filed_at=f"{year}-02-25",
        primary_document=path.name,
        source_url=f"https://www.sec.gov/Archives/{accession}/{path.name}",
        local_path=str(path),
        content_hash=sha256(raw).hexdigest(),
    )


def test_preparation_targets_only_latest_period_with_two_missing_statements(tmp_path):
    company, _filing, adapter, official, storage = setup_preparer(tmp_path)
    complete = ("revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity")
    filings = (
        _annual_original(company, 2024, "000021", complete, tmp_path / "annual-2024.htm"),
        _annual_original(company, 2025, "000021", complete, tmp_path / "annual-2025.htm"),
        _annual_original(company, 2026, "000021", ("assets",), tmp_path / "annual-2026.htm"),
    )
    adapter.list_annual_filings = lambda _company, limit: [replace(item) for item in filings]
    request = ResearchPreparationRequest(
        company=company,
        selected_pack=builtin_pack(),
        models=(ModelConfig("offline.primary", role="primary"),),
        official=OfficialFinancialRequest(
            company, adapter, candidate_sources=(SecFinancialSourceAdapter(adapter),),
            annual_history_years=3,
        ),
        annual_history_years=3,
    )

    prepared = NewResearchPreparer(storage, official, object()).prepare(request)
    source_state = prepared.request.reproducibility["data_snapshot"]["official_source_state"]

    assert source_state["scope_gaps"] == [{
        "period_end": "2026-01-25",
        "fiscal_period": "FY",
        "missing_statements": ["income_statement", "cash_flow"],
    }]
    assert source_state["recovery_targets"] == [filings[-1].accession_number]
    assert source_state["source_acquisition_gaps"] == []


def test_unobtained_original_is_a_source_gap_and_minimal_download_target(tmp_path):
    company, _filing, adapter, official, storage = setup_preparer(tmp_path)
    request = ResearchPreparationRequest(
        company=company,
        selected_pack=builtin_pack(),
        models=(ModelConfig("offline.primary", role="primary"),),
        official=OfficialFinancialRequest(
            company, adapter, candidate_sources=(SecFinancialSourceAdapter(adapter),),
            annual_history_years=1,
        ),
        annual_history_years=1,
    )
    original_prepare = official.prepare

    def with_original_unavailable(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        unavailable = tuple(replace(item, local_path="", content_hash="") for item in prepared.filings)
        return replace(prepared, filings=unavailable)

    official.prepare = with_original_unavailable
    prepared = NewResearchPreparer(storage, official, object()).prepare(request)
    source_state = prepared.request.reproducibility["data_snapshot"]["official_source_state"]

    assert source_state["source_acquisition_gaps"] == [{
        "accession_number": _filing.accession_number,
        "period_end": _filing.period_end,
        "fiscal_period": _filing.fiscal_period,
        "diagnostic_code": "FILING_LOCAL_FILE_MISSING",
    }]
    assert source_state["recovery_targets"] == [_filing.accession_number]

