from unittest.mock import patch

from openthesis.financial_sources.document_units import discover_document_units
from openthesis.financial_sources.pdf_document_units import read_document_units
from openthesis.financial_taxonomy import profit_row_concept


def test_statement_basis_is_money_only_and_retains_its_source_page():
    result = discover_document_units([
        "本公司现金分红总额为人民币千元。", "",
        "本公司记账本位币和编制本财务报表所采用的货币均为人民币，除有特别说明外，均以人民币千元为单位表示。",
    ])
    unit = result.for_scope("consolidated")
    assert unit is not None and (unit.page, unit.currency, unit.scale) == (3, "CNY", "1000")
    assert unit.excerpt.startswith("本公司记账本位币")
    assert result.for_scope("parent") == unit


def test_english_and_traditional_statement_basis():
    for text, currency, scale in (
        ("These financial statements are presented in US dollars. All amounts are rounded to the nearest million unless otherwise stated.", "USD", "1000000"),
        ("本財務報表除另有說明外，所有金額均以人民幣千元表示。", "CNY", "1000"),
        ("These financial statements are expressed in thousands of Hong Kong dollars, except where indicated.", "HKD", "1000"),
    ):
        declaration = discover_document_units([text]).for_scope("consolidated")
        assert declaration is not None and (declaration.currency, declaration.scale) == (currency, scale)


def test_narrative_currency_presentation_without_amount_units_stays_unknown():
    for text in (
        "Financial statements are presented in Renminbi.",
        "Our financial statements recorded sales of RMB 100 million.",
        "本财务报表单位不明，现金分红金额为人民币千元。",
        "All amounts are expressed in millions, unless otherwise stated.",
    ):
        assert discover_document_units([text]).for_scope("consolidated") is None


def test_conflicting_declarations_do_not_select_by_page_order():
    texts = ["本财务报表除另有说明外，所有金额均以人民币千元表示。",
             "本财务报表除另有说明外，所有金额均以人民币百万元表示。"]
    assert discover_document_units(texts).for_scope("consolidated") is None
    assert discover_document_units(list(reversed(texts))).for_scope("consolidated") is None


def test_one_basis_sentence_with_different_money_multipliers_is_unresolved():
    for text in (
        "These financial statements are presented in RMB millions or thousands, unless otherwise stated.",
        "本财务报表所有金额均以人民币千元或百万元表示。",
    ):
        result = discover_document_units([text])
        assert result.for_scope("consolidated") is None
    # The 万元 substring inside 百万元 is not a second unit declaration.
    declared = discover_document_units([
        "本财务报表所有金额均以人民币百万元（millions）表示。",
    ]).for_scope("consolidated")
    assert declared is not None and declared.scale == "1000000"


def test_parent_only_basis_cannot_be_used_for_group_statements():
    result = discover_document_units(["母公司财务报表除另有说明外，所有金额均以人民币千元表示。"])
    assert result.for_scope("consolidated") is None
    assert result.for_scope("parent").scale == "1000"


def test_cached_prepass_does_not_start_another_native_worker():
    with patch("openthesis.financial_sources.pdf_document_units.cached_page_texts", return_value=(
        "本财务报表除另有说明外，所有金额均以人民币千元表示。",
    )), patch("openthesis.financial_sources.pdf_document_units._run_pdfium_isolated") as worker:
        assert read_document_units("fixture.pdf").for_scope("consolidated").scale == "1000"
        worker.assert_not_called()


def test_subsidiary_statement_translation_is_bound_group_presentation_not_functional_guess():
    clause = ('本公司下属子公司，根据其经营所处的主要经济环境自行决定其记账本位币，'
              '编制财务报表时折算为人\n民币。')
    result = discover_document_units(['本公司以人民币为记账本位币。', clause])
    declaration = result.currency_for_scope('consolidated')
    assert declaration is not None and declaration.currency == 'CNY' and declaration.page == 2
    assert declaration.excerpt + '。' == clause
    assert result.currency_for_scope('parent') is None
    assert result.for_scope('consolidated') is None  # currency is not a display multiplier


def test_functional_and_conditional_translation_never_prove_reporting_currency():
    for text in ('本公司以人民币为记账本位币。', '记账本位币\n人民币元。',
                 '本公司下属子公司若编制财务报表时折算为人民币。',
                 '本公司下属子公司编制财务报表时可能折算为人民币。'):
        assert discover_document_units([text]).currency_for_scope('consolidated') is None


def test_conflicting_group_statement_translation_currencies_remain_unknown():
    result = discover_document_units(['本公司下属子公司编制财务报表时折算为人民币。',
                                     '本公司下属子公司编制财务报表时折算为美元。'])
    assert result.currency_for_scope('consolidated') is None


def test_profit_allocation_is_semantic_and_excludes_eps():
    for label, attribution, expected in (
        ("淨利潤", "", "net_income"),
        ("歸屬於母公司股東的淨利潤", "", "parent_net_income"),
        ("Equity holders of the Company", "Attributable to:", "parent_net_income"),
        ("Non-controlling interests", "Attributable to:", "minority_interest_profit"),
        ("Non-controlling interests", "", None),
        ("Net income attributable to shareholders", "", "parent_net_income"),
        ("Basic earnings per share for profit attributable to equity holders", "", None),
        ("Other income, net Income before tax", "", None),
        ("(Loss)/profit for the year", "", "net_income"),
        ("Total comprehensive loss attributable to noncontrolling interests", "", None),
        ("Noncontrolling interests", "Total comprehensive loss attributable to:", None),
        ("Net income attributable to ordinary shareholders", "", "ordinary_shareholder_net_income"),
    ):
        assert profit_row_concept(label, attribution) == expected
