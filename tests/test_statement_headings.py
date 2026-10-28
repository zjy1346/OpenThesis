from openthesis.financial_statement_identity import find_statement_headings


def test_numbered_formal_titles_preserve_source_text_page_and_span():
    text = "1. Consolidated Balance Sheet\n（一）合并利润表\nIV. Statement of Changes in Equity"

    headings = find_statement_headings(text, page=8)

    assert [(heading.kind, heading.scope) for heading in headings] == [
        ("balance_sheet", "consolidated"),
        ("income_statement", "consolidated"),
        ("equity_changes", "parent"),
    ]
    assert [heading.normalized_title for heading in headings] == [
        "Consolidated Balance Sheet",
        "合并利润表",
        "Statement of Changes in Equity",
    ]
    assert [heading.raw_text for heading in headings] == text.splitlines()
    assert all(heading.page == 8 for heading in headings)
    assert all(text[start:end] == heading.raw_text for heading in headings for start, end in [heading.span])


def test_wrapped_title_keeps_the_cross_line_source_span():
    text = "  1.2 Consolidated Statement of\r\n       Financial Position\r\nAs at June 30, 2025"

    headings = find_statement_headings(text, page=12)

    assert len(headings) == 1
    heading = headings[0]
    assert heading.kind == "balance_sheet"
    assert heading.scope == "consolidated"
    assert heading.normalized_title == "Consolidated Statement of Financial Position"
    assert heading.raw_text == "  1.2 Consolidated Statement of\r\n       Financial Position"
    assert text[heading.span[0]:heading.span[1]] == heading.raw_text
    assert heading.page == 12


def test_prose_mention_is_not_a_heading_and_unscoped_title_keeps_parent_default():
    assert find_statement_headings("This report discusses the Consolidated Balance Sheet.") == ()
    headings = find_statement_headings("Statement of Profit or Loss")

    assert [(heading.kind, heading.scope) for heading in headings] == [("income_statement", "parent")]


def test_bare_english_and_traditional_chinese_titles_keep_expected_scope():
    titles = {
        "Balance Sheet": ("balance_sheet", "parent"),
        "Income Statement": ("income_statement", "parent"),
        "Statement of Operations": ("income_statement", "parent"),
        "Statement of Cash Flow": ("cash_flow", "parent"),
        "合併資產負債表": ("balance_sheet", "consolidated"),
        "綜合損益表": ("income_statement", "parent"),
        "現金流量表": ("cash_flow", "parent"),
        "權益變動表": ("equity_changes", "parent"),
    }

    for title, expected in titles.items():
        headings = find_statement_headings(title)
        assert [(heading.kind, heading.scope) for heading in headings] == [expected]


def test_parent_narrative_is_not_promoted_to_a_formal_heading():
    assert find_statement_headings("The parent company prepared the Balance Sheet.") == ()
    assert find_statement_headings("母公司董事已审阅资产负债表。") == ()


def test_arabic_parenthesis_and_chinese_continuation_preserve_raw_title():
    raw = "(2) 合併資產負債表（續）"
    heading, = find_statement_headings(raw)
    assert (heading.kind, heading.scope, heading.raw_text) == ("balance_sheet", "consolidated", raw)


def test_wrapped_mda_sentence_and_cash_flow_notes_are_not_formal_titles():
    assert find_statement_headings(
        "Revenue is recognized net of VAT in our\nconsolidated income statement."
    ) == ()
    assert find_statement_headings("66、现金流量表项目") == ()
    assert find_statement_headings("Cash flow statement projects") == ()
    assert find_statement_headings("1. Consolidated Income Statement.")
