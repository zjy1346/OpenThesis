"""Positioned-PDF regression tests for same-filing ratio comparators."""

from __future__ import annotations

from unittest.mock import patch

from openthesis.domain import Company, FilingDocument
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_ingestion import FinancialIngestionEngine, _manifest_for


class _Page:
    def __init__(self, rows: list[tuple[tuple[str, float, float], ...]]) -> None:
        self.rows = rows

    def extract_text(self) -> str:
        return "\n".join(" ".join(cell[0] for cell in row) for row in self.rows)

    def extract_words(self, **_kwargs):
        words = []
        for row_index, row in enumerate(self.rows):
            top = row_index * 16.0 + 10.0
            for text, x0, x1 in row:
                words.append({
                    "text": text,
                    "x0": x0,
                    "x1": x1,
                    "top": top,
                    "bottom": top + 9.0,
                })
        return words


class _Pdf:
    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _company() -> Company:
    return Company(
        "CN:TEST:303", "303.SZ", "Ratio Fixture", "SZSE",
        "CN:TEST:303", "CN_A", "CN:TEST:303", "CNY", "CNY", "CAS",
    )


def _filing() -> FilingDocument:
    company = _company()
    return FilingDocument(
        "ratio-filing-2025", company.security_id, "ratio-filing-2025",
        "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-31",
        "ratio-fixture.pdf", "https://example.invalid/ratio-fixture.pdf",
        content_hash="a" * 64,
    )


def _cell(text: str, x0: float, width: float = 70.0) -> tuple[str, float, float]:
    return text, x0, x0 + width


def _formal_pages(*, ratio_header: str = "加权平均净资产收益率（%）",
                  ratio_current: str = "24.91%",
                  ratio_prior: str = "24.13%") -> list[_Page]:
    return [
        _Page([
            (_cell("合并利润表", 50, 150),),
            (_cell("2025年12月31日", 50, 150),),
            (_cell("单位：人民币百万元", 50, 150),),
            (_cell("项目", 50), _cell("2025", 320), _cell("2024", 460)),
            (_cell("营业收入", 50, 150), _cell("100", 320), _cell("80", 460)),
            (_cell("净利润", 50, 150), _cell("10", 320), _cell("8", 460)),
        ]),
        _Page([
            (_cell("合并资产负债表", 50, 180),),
            (_cell("2025年12月31日", 50, 150),),
            (_cell("单位：人民币百万元", 50, 150),),
            (_cell("项目", 50), _cell("2025", 320), _cell("2024", 460)),
            (_cell("资产总计", 50, 150), _cell("1000", 320), _cell("900", 460)),
            (_cell("负债合计", 50, 150), _cell("500", 320), _cell("450", 460)),
            (_cell("所有者权益合计", 50, 180), _cell("500", 320), _cell("450", 460)),
        ]),
        _Page([
            (_cell("合并现金流量表", 50, 180),),
            (_cell("2025年12月31日", 50, 150),),
            (_cell("单位：人民币百万元", 50, 150),),
            (_cell("项目", 50), _cell("2025", 320), _cell("2024", 460)),
            (_cell("经营活动产生的现金流量净额", 50, 240), _cell("20", 320), _cell("15", 460)),
        ]),
        _Page([
            (_cell("净资产收益率及每股收益", 50, 220),),
            (_cell("2025年12月31日", 50, 150),),
            (_cell("项目", 50), _cell("本期发生额", 320), _cell("上期发生额", 460)),
            (_cell(ratio_header, 50, 250), _cell(ratio_current, 320), _cell(ratio_prior, 460)),
        ]),
    ]


def _parse(pages: list[_Page]):
    company = _company()
    filing = _filing()
    with patch("pdfplumber.open", return_value=_Pdf(pages)):
        return FinancialIngestionEngine()._parse_pdf_ast(
            "virtual-ratio-fixture.pdf", company, filing, _manifest_for(filing),
            candidate_pages=frozenset(range(1, len(pages) + 1)), index_precomputed=True,
        )


def test_same_filing_roe_current_and_prior_columns_keep_ratio_provenance():
    facts, refs = _parse(_formal_pages())
    roe = [fact for fact in facts if fact.concept == "reported_roe"]

    assert {fact.fiscal_year for fact in roe} == {2025, 2024}
    current = next(fact for fact in roe if fact.fiscal_year == 2025)
    prior = next(fact for fact in roe if fact.fiscal_year == 2024)
    assert current.value == 0.2491
    assert prior.value == 0.2413
    assert current.source_column == "本期发生额"
    assert prior.source_column == "上期发生额"
    assert current.usage_status != "comparator"
    assert prior.usage_status == "comparator"
    assert current.unit_provenance in {"explicit_percent", "explicit_ratio"}
    assert prior.unit_provenance in {"explicit_percent", "explicit_ratio"}
    assert current.normalization_lineage is not None
    assert prior.normalization_lineage is not None
    assert any(ref.evidence_id == f"fact:{prior.fact_id.removeprefix('ingest:')}" for ref in refs)


def test_ratio_without_percent_or_ratio_declaration_stays_unverified():
    facts, _refs = _parse(_formal_pages(
        ratio_header="加权平均净资产收益率",
        ratio_current="0.2491",
        ratio_prior="0.2413",
    ))
    roe = [fact for fact in facts if fact.concept == "reported_roe"]
    assert roe
    assert all(fact.unit_provenance == "unknown" for fact in roe)
    assert all(fact.normalization_lineage is None for fact in roe)


def test_same_filing_ratio_header_without_percent_keeps_explicit_ratio_provenance():
    facts, _refs = _parse(_formal_pages(
        ratio_header="加权平均净资产收益率（比率）",
        ratio_current="0.2491",
        ratio_prior="0.2413",
    ))
    roe = [fact for fact in facts if fact.concept == "reported_roe"]

    assert {fact.fiscal_year for fact in roe} == {2025, 2024}
    assert all(fact.unit_provenance == "explicit_ratio" for fact in roe)
    assert all(fact.normalization_lineage is not None for fact in roe)


def test_compiler_keeps_same_filing_roe_comparator_available_for_comparison():
    facts, refs = _parse(_formal_pages())
    filing = _filing()

    dataset = FinancialFactCompiler().compile_facts(
        _company(),
        (filing,),
        facts,
        reporting_currency="CNY",
        required_concepts=frozenset({"reported_roe"}),
        evidence=refs,
    )

    current = [fact for fact in dataset.research_facts
               if fact.concept == "reported_roe" and fact.fiscal_year == 2025]
    comparative = [fact for fact in dataset.comparator_facts
                   if fact.concept == "reported_roe" and fact.fiscal_year == 2024]
    assert current
    assert comparative
    assert comparative[0].usage_status == "comparator"

