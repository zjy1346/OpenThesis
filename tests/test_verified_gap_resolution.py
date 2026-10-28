from dataclasses import replace

from openthesis.domain import EvidenceRef
from openthesis.financial_contracts import CandidateBatch, FactCandidate, GapStageKind
from openthesis.financial_pipeline import CollectionPolicy, FinancialCandidatePipeline
from openthesis.financial_sources import GapResolver
from test_financial_compiler import _facts, _filing, _subject


class _Stage:
    def __init__(self, name, kind, batch):
        self.name = name
        self.stage_kind = kind
        self.batch = batch
        self.calls = 0

    def extract(self, subject, filing):
        self.calls += 1
        return self.batch


def _pipeline(filing, *stages):
    return FinancialCandidatePipeline().compile(
        _subject(),
        filing.period_end,
        CollectionPolicy(
            filings=(filing,),
            extractors=tuple(stages),
            reporting_currency="CNY",
        ),
    )


def test_candidate_only_resolver_never_uses_raw_names_as_admission_without_injected_gate():
    filing = _filing("gap-candidate-only", "2022-12-31", "Fixture")
    revenue = _facts(filing, net_income=188_243_000_000.0)[0]
    primary = _Stage("primary", GapStageKind.OFFICIAL_STRUCTURED, CandidateBatch(filing, (revenue,)))
    fallback = _Stage("fallback", GapStageKind.PDF_AST, CandidateBatch(filing))
    result = GapResolver().resolve(
        _subject(), filing, ("revenue",), (primary, fallback), existing_concepts={"revenue"},
    )
    assert primary.calls == fallback.calls == 1
    assert result.candidates == (revenue,)
    assert "verified_concepts_required" in result.diagnostics
    assert "gap_unresolved" in result.diagnostics


def test_bad_currency_primary_is_recompiled_and_good_fallback_fills_gap():
    filing = _filing("gap-bad-currency", "2022-12-31", "Fixture")
    good = _facts(filing, net_income=188_243_000_000.0)
    revenue = good[0]
    bad_revenue = FactCandidate(
        replace(revenue.fact, fact_id="bad-revenue-currency", currency="USD", unit="USD"),
        revenue.evidence,
        "bad-structured",
    )
    primary = _Stage(
        "primary",
        GapStageKind.OFFICIAL_STRUCTURED,
        CandidateBatch(filing, (bad_revenue, *good[1:])),
    )
    fallback = _Stage(
        "fallback",
        GapStageKind.PDF_AST,
        CandidateBatch(filing, (revenue,)),
    )

    dataset = _pipeline(filing, primary, fallback)

    assert fallback.calls == 1
    assert dataset.allow_ai
    assert any(item.concept == "revenue" and item.currency == "CNY" for item in dataset.resolved_facts)
    # The malformed USD observation remains available for audit, but must not
    # be admitted into the canonical research view or close the gap.
    assert not any(item.fact_id == "bad-revenue-currency" for item in dataset.research_facts)


def test_wrong_scope_or_period_does_not_cover_current_gap_and_later_stage_runs():
    filing = _filing("gap-wrong-context", "2022-12-31", "Fixture")
    good = _facts(filing, net_income=188_243_000_000.0)
    revenue = good[0]
    wrong = FactCandidate(
        replace(
            revenue.fact,
            fact_id="wrong-revenue-period",
            end_date="2021-12-31",
            fiscal_year=2021,
            scope="parent",
            consolidated_scope="parent",
        ),
        revenue.evidence,
        "wrong-context",
    )
    primary = _Stage(
        "primary",
        GapStageKind.OFFICIAL_STRUCTURED,
        CandidateBatch(filing, (*good[1:],)),
    )
    wrong_stage = _Stage(
        "wrong",
        GapStageKind.PDF_AST,
        CandidateBatch(filing, (wrong,)),
    )
    correct_stage = _Stage(
        "correct",
        GapStageKind.SAME_YEAR_OFFICIAL,
        CandidateBatch(filing, (revenue,)),
    )

    dataset = _pipeline(filing, primary, wrong_stage, correct_stage)

    assert wrong_stage.calls == 1
    assert correct_stage.calls == 1
    assert any(item.concept == "revenue" and item.end_date == "2022-12-31" for item in dataset.resolved_facts)
    # Wrong-period/parent observations remain in the audit projection; they
    # cannot enter the canonical research view or satisfy current FY coverage.
    assert not any(item.fact_id == "wrong-revenue-period" for item in dataset.research_facts)


def test_bad_fallback_candidate_does_not_stop_a_later_verified_source():
    filing = _filing("gap-bad-fallback", "2022-12-31", "Fixture")
    good = _facts(filing, net_income=188_243_000_000.0)
    revenue = good[0]
    bad = FactCandidate(
        replace(
            revenue.fact,
            fact_id="bad-revenue",
            scope="parent",
            consolidated_scope="parent",
        ),
        revenue.evidence,
        "bad-pdf",
    )
    primary = _Stage(
        "primary",
        GapStageKind.OFFICIAL_STRUCTURED,
        CandidateBatch(filing, good[1:]),
    )
    bad_stage = _Stage("bad", GapStageKind.PDF_AST, CandidateBatch(filing, (bad,)))
    good_stage = _Stage(
        "good", GapStageKind.SAME_YEAR_OFFICIAL, CandidateBatch(filing, (revenue,))
    )

    dataset = _pipeline(filing, primary, bad_stage, good_stage)

    assert bad_stage.calls == 1
    assert good_stage.calls == 1
    assert dataset.allow_ai
    assert any(item.fact_id == revenue.fact.fact_id for item in dataset.resolved_facts)


def test_two_good_conflicting_candidates_remain_conflict_after_compilation():
    filing = _filing("gap-two-good-conflict", "2022-12-31", "Fixture")
    good = _facts(filing, net_income=188_243_000_000.0)
    revenue = good[0]
    conflicting = FactCandidate(
        replace(revenue.fact, fact_id="revenue-conflict", value=revenue.fact.value * 1.2),
        revenue.evidence,
        "second-good-source",
    )
    primary = _Stage(
        "primary",
        GapStageKind.OFFICIAL_STRUCTURED,
        CandidateBatch(filing, (*good[1:], revenue, conflicting)),
    )

    dataset = _pipeline(filing, primary)

    assert not dataset.allow_ai
    assert any(item.concept == "revenue" for item in dataset.quarantined_facts)
    assert dataset.conflicts


def test_verified_primary_does_not_repeat_already_covered_gap_stage():
    filing = _filing("gap-already-covered", "2022-12-31", "Fixture")
    good = _facts(filing, net_income=188_243_000_000.0)
    primary = _Stage(
        "primary", GapStageKind.OFFICIAL_STRUCTURED, CandidateBatch(filing, good)
    )
    fallback = _Stage(
        "fallback", GapStageKind.PDF_AST, CandidateBatch(filing, good[:1])
    )

    dataset = _pipeline(filing, primary, fallback)

    assert dataset.allow_ai
    assert fallback.calls == 0


def test_unauthorized_mineru_is_not_called_even_when_compiler_reports_gap():
    filing = _filing("gap-no-mineru", "2022-12-31", "Fixture")
    stage = _Stage("mineru", GapStageKind.MINERU, CandidateBatch(filing))
    result = GapResolver().resolve(
        _subject(),
        filing,
        ("revenue",),
        (stage,),
        verified_concepts=lambda candidates, evidence: frozenset(),
        user_authorized=False,
    )

    assert stage.calls == 0
    assert "mineru_not_authorized" in result.diagnostics

