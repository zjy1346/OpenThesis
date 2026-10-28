from __future__ import annotations

from decimal import Decimal

import pytest

from openthesis.domain import EvidenceRef, FinancialFact, FinancialNormalizationLineage
from openthesis.financial_unit_declarations import (
    combined_money_unit,
    literal_money_unit,
    physical_unit_context,
)


@pytest.mark.parametrize(
    ("excerpt", "currencies", "scales"),
    [
        ("RMB in thousands", frozenset({"CNY"}), {Decimal("1000")}),
        ("人民币百万元", frozenset({"CNY"}), {Decimal("1000000")}),
        ("人民幣千元", frozenset({"CNY"}), {Decimal("1000")}),
        ("USD millions", frozenset({"USD"}), {Decimal("1000000")}),
        ("US$ '000", frozenset({"USD"}), {Decimal("1000")}),
        ("HK$ million", frozenset({"HKD"}), {Decimal("1000000")}),
        ("Unit: RMB", frozenset({"CNY"}), {Decimal("1")}),
        ("Total assets", frozenset(), set()),
        ("", frozenset(), set()),
        (
            "RMB millions or thousands",
            frozenset({"CNY"}),
            {Decimal("1000000"), Decimal("1000")},
        ),
        ("RMB USD thousands", frozenset({"CNY", "USD"}), {Decimal("1000")}),
    ],
)
def test_literal_money_unit_requires_unambiguous_literal_declaration(
    excerpt: str,
    currencies: frozenset[str],
    scales: set[Decimal],
) -> None:
    declaration = literal_money_unit(excerpt)

    assert isinstance(declaration.currencies, frozenset)
    assert isinstance(declaration.scales, frozenset)
    assert declaration.currencies == currencies
    assert {Decimal(scale) for scale in declaration.scales} == scales

    if excerpt == "RMB millions or thousands":
        assert not declaration.proves("CNY", "1000000")
    elif excerpt == "RMB USD thousands":
        assert not declaration.proves("CNY", "1000")


def test_thousand_does_not_declare_usd_from_an_internal_us_substring() -> None:
    declaration = literal_money_unit("thousand")

    assert declaration.currencies == frozenset()
    assert {Decimal(scale) for scale in declaration.scales} == {Decimal("1000")}


@pytest.mark.parametrize(
    "excerpts",
    [
        ("RMB", "in millions"),
        ("in millions", "RMB"),
        ("人民币", "百万元"),
        ("百万元", "人民币"),
    ],
)
def test_combined_currency_only_and_scale_is_order_independent(excerpts) -> None:
    declaration = combined_money_unit(excerpts)

    assert declaration.currencies == frozenset({"CNY"})
    assert {Decimal(scale) for scale in declaration.scales} == {Decimal("1000000")}
    assert declaration.proves("CNY", "1000000")


@pytest.mark.parametrize(
    ("excerpts", "expected_currency", "expected_scale"),
    [
        (("人民币元", "千元"), "CNY", "1"),
        (("RMB", "USD", "thousands"), "CNY", "1000"),
        (("RMB in millions", "RMB", "RMB'000"), "CNY", "1000000"),
    ],
)
def test_combined_unit_rejects_explicit_currency_or_scale_conflicts(
    excerpts, expected_currency: str, expected_scale: str,
) -> None:
    declaration = combined_money_unit(excerpts)

    # A currency-only observation must not hide either an explicit scale
    # conflict or a second currency. ``proves`` is the admission seam used by
    # the financial validator.
    assert not declaration.proves(expected_currency, expected_scale)


def _fact_with_unit_evidence(*evidence_ids: str) -> FinancialFact:
    return FinancialFact(
        fact_id="fact-1",
        company_cik="",
        concept="revenue",
        reported_concept="Revenue",
        value=1.0,
        unit="CNY",
        fiscal_year=2025,
        fiscal_period="FY",
        form_type="annual-report",
        start_date=None,
        end_date="2025-12-31",
        filed_at="2026-03-01",
        accession_number="",
        source_url="",
        normalization_lineage=FinancialNormalizationLineage(
            raw_value="1",
            scale="1",
            normalized_value="1",
            quantity_kind="money",
            currency="CNY",
            unit_evidence_ids=tuple(evidence_ids),
        ),
    )


def _header_ref(
    evidence_id: str,
    *,
    locator: str = "page:4",
    content_hash: str = "sha256:source-a",
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        document_id="filing-1",
        source_url="https://example.test/annual-report.pdf",
        title="Statement of financial position",
        locator=locator,
        excerpt="Unit: RMB millions",
        published_at="2026-03-01",
        content_hash=content_hash,
        bbox=(10.0, 20.0, 300.0, 40.0),
    )


def test_physical_unit_context_ignores_generated_evidence_id() -> None:
    first = physical_unit_context(
        _fact_with_unit_evidence("unit-ref-a"),
        {"unit-ref-a": _header_ref("unit-ref-a")},
    )
    second = physical_unit_context(
        _fact_with_unit_evidence("unit-ref-b"),
        {"unit-ref-b": _header_ref("unit-ref-b")},
    )

    assert first == second


@pytest.mark.parametrize(
    "changed_ref",
    [
        _header_ref("unit-ref", locator="page:5"),
        _header_ref("unit-ref", content_hash="sha256:source-b"),
    ],
    ids=("different-page", "different-source-hash"),
)
def test_physical_unit_context_changes_with_page_or_source_hash(
    changed_ref: EvidenceRef,
) -> None:
    baseline = physical_unit_context(
        _fact_with_unit_evidence("unit-ref"),
        {"unit-ref": _header_ref("unit-ref")},
    )
    changed = physical_unit_context(
        _fact_with_unit_evidence("unit-ref"),
        {"unit-ref": changed_ref},
    )

    assert baseline != changed


def test_physical_unit_context_is_empty_for_missing_evidence_reference() -> None:
    assert physical_unit_context(
        _fact_with_unit_evidence("not-in-index"),
        {},
    ) == ()
