"""Financial periods come from statement headers, never release/audit dates."""
from openthesis.domain import FilingDocument
from openthesis.financial_ingestion import _manifest_for, _manifest_from_observed_pages
from openthesis.financial_sources.statement_periods import statement_period_declarations, statement_period_gaps, period_gap_evidence


def filing(title="Fiscal Year 2026 Annual Report"):
    return FilingDocument(
        "hk:fixture", "HK:HKEX:09988.HK", "fixture", "ANNUAL_REPORT", "FY",
        "2026-12-31", "2026-06-18", title, "https://example.invalid/annual.pdf",
    )


def resolve(pages, title="Fiscal Year 2026 Annual Report"):
    source = filing(title)
    return _manifest_from_observed_pages(_manifest_for(source), source, pages)


def test_missing_period_has_exact_original_header_page_and_repair_evidence():
    text = "1. Consolidated Balance Sheet\nUnreadable date header\nRMB Million\nTotal assets 100"
    gaps = statement_period_gaps(["Cover 2026", text])
    gap, = gaps
    assert gap.code == "PERIOD_DECLARATION_NOT_FOUND"
    assert gap.page == 2 and gap.kind == "balance_sheet"
    assert text[gap.span[0]:gap.span[1]] == gap.excerpt
    source = filing()
    source.content_hash = "original-hash"
    ref, = period_gap_evidence(source, gaps)
    assert ref.locator == "page:2" and ref.content_hash == "original-hash"
    assert ref.excerpt == text
    assert resolve([text]).period_end == ""


def test_period_repair_targets_exclude_contents_and_already_dated_headers():
    assert statement_period_gaps(["Contents\n1. Consolidated Balance Sheet\npage 100"]) == ()
    assert statement_period_gaps(["1. Consolidated Balance Sheet\nAs at 31 December 2025"]) == ()


def test_audit_and_release_dates_cannot_replace_split_statement_header():
    manifest = resolve([
        "Independent Auditor's Report\nHong Kong, May 20, 2026",
        "Consolidated Income Statements\nYear ended March 31,\n2024 2025 2026\nRMB RMB RMB\nRevenue 10 11 12",
        "This annual report is dated May 13, 2026.",
    ])
    assert manifest.period_end == "2026-03-31"


def test_only_release_or_audit_date_keeps_identity_unresolved():
    manifest = resolve(["Independent Auditor's Report\nMay 20, 2026",
                        "This annual report is dated May 13, 2026."])
    assert manifest.period_end == ""


def test_conflicting_current_statement_periods_are_not_decided_by_latest_date():
    manifest = resolve([
        "Consolidated Income Statements\nYear ended March 31, 2026\nRevenue 12",
        "Consolidated Balance Sheets\nAs of May 31, 2026\nTotal assets 15",
    ])
    assert manifest.period_end == ""
    assert manifest.revision == "period_header_conflict"
    source = filing()
    # An isolated window cannot erase a conflict found by the whole-file prepass.
    again = _manifest_from_observed_pages(manifest, source, [
        "Consolidated Income Statements\nYear ended March 31, 2026",
    ])
    assert again.period_end == ""
    assert again.revision == "period_header_conflict"


def test_formal_chinese_header_accepts_dates_but_not_signature_date():
    manifest = resolve([
        "合并利润表\n单位：人民币千元\n2026年6月30日 2025年6月30日\n营业收入 12 10\n董事签字2026年8月15日",
    ], "2026年半年度报告")
    # The source's filed date must actually permit the half-year period.
    assert manifest.period_end == ""
    source = filing("2026年半年度报告")
    source.filed_at = "2026-08-20"
    manifest = _manifest_from_observed_pages(_manifest_for(source), source, [
        "合并利润表\n单位：人民币千元\n2026年6月30日 2025年6月30日\n营业收入 12 10\n董事签字2026年8月15日",
    ])
    assert manifest.period_end == "2026-06-30"


def test_statement_header_year_end_is_not_forced_to_calendar_month():
    manifest = resolve([
        "Consolidated Balance Sheets\nAs of April 30,\n2025 2026\nTotal assets 15 20",
    ])
    assert manifest.period_end == "2026-04-30"


def test_statement_declaration_overrides_unverified_noncalendar_metadata():
    source = filing()
    source.period_end = "2026-04-30"
    manifest = _manifest_from_observed_pages(_manifest_for(source), source, [
        "Consolidated Income Statements\nYear ended March 31, 2026",
    ])
    assert manifest.period_end == "2026-03-31"


def test_fully_dated_title_conflicting_with_source_remains_unresolved():
    source = filing("Annual Report for year ended April 30, 2026")
    manifest = _manifest_from_observed_pages(_manifest_for(source), source, [
        "Consolidated Income Statements\nYear ended March 31, 2026",
    ])
    assert manifest.period_end == ""
    assert manifest.revision == "period_header_conflict"


def test_numbered_heading_and_six_month_anchor_bind_the_header_period():
    declarations = statement_period_declarations([
        "1.2 Consolidated Income Statements\n"
        "For the six months ended June 30, 2025\n"
        "Revenue 12",
    ])

    assert [(item.page, item.period_end, item.duration_months) for item in declarations] == [
        (1, "2025-06-30", 6),
    ]


def test_table_of_contents_cannot_supply_a_statement_period():
    manifest = resolve([
        "Table of Contents\n"
        "1. Consolidated Income Statements\n"
        "Year ended March 31, 2026",
    ])

    assert manifest.period_end == ""


def test_narrative_as_at_date_below_a_title_is_not_a_period_header():
    manifest = resolve([
        "Consolidated Balance Sheets\n"
        "As at May 20, 2026, the note describes a later transaction.",
    ])

    assert manifest.period_end == ""


def test_wrapped_formal_title_keeps_the_following_date_inside_its_header_window():
    manifest = resolve([
        "1. Consolidated Statement of\n"
        "Financial Position\n"
        "As at April 30, 2026\n"
        "Total assets 20",
    ])

    assert manifest.period_end == "2026-04-30"


def test_numeric_bilingual_year_grid_is_bound_to_its_formal_header():
    declarations = statement_period_declarations([
        "3. Consolidated Income Statements\n"
        "Unit: RMB'000\nYear ended Year ended\n"
        "Item 31/12/2025 31/12/2024\nOperating income 100 90",
    ])
    assert [(item.period_end, item.duration_months) for item in declarations] == [
        ("2025-12-31", 12), ("2024-12-31", 12),
    ]
    assert all(item.excerpt.startswith("Year ended Year ended\nItem") for item in declarations)


def test_numeric_dates_in_signature_or_prose_do_not_bind_a_period():
    assert statement_period_declarations([
        "Consolidated Income Statements\n"
        "Director signature 31/12/2025\n"
        "The report was prepared on 31/12/2025.",
    ]) == ()


def test_invalid_numeric_header_date_is_not_silently_corrected():
    assert statement_period_declarations([
        "Consolidated Balance Sheets\nAs at 30/02/2025",
    ]) == ()
