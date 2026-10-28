from types import SimpleNamespace
from pathlib import Path

from openthesis.financial_sources.document_catalog import (
    candidate_pages_from_catalog,
    document_catalog,
    load_document_catalog,
    save_document_catalog,
)


def test_catalogue_keeps_scan_pages_and_unknown_pages_not_only_financial_matches():
    context = SimpleNamespace(page_texts=('合并利润表\n单位：人民币千元','', 'Unregistered business disclosure'),
        units=SimpleNamespace(declarations=()),periods=())
    catalog = document_catalog(context,'original','rules',
        lambda text:('income_statement','consolidated') if '利润表' in text else None)
    assert len(catalog['pages']) == 3
    assert catalog['pages'][0]['statement'] == 'income_statement'
    assert catalog['pages'][1]['representation'] == 'image_or_empty'
    assert catalog['pages'][2]['representation'] == 'native_text'
    assert all(page['text_sha256'] for page in catalog['pages'])


def test_catalogue_reuses_identical_source_and_rules_without_rewriting(tmp_path):
    context = SimpleNamespace(page_texts=('page',),units=SimpleNamespace(declarations=()),periods=())
    catalog = document_catalog(context,'original','rules',lambda text:None)
    path = save_document_catalog(tmp_path,catalog)
    stamp = path.stat().st_mtime_ns
    assert save_document_catalog(tmp_path,catalog) == path
    assert path.stat().st_mtime_ns == stamp
    assert save_document_catalog(tmp_path,{**catalog,'rules_fingerprint':'changed-rules'}) != path


def test_catalogue_persists_and_reuses_the_single_page_plan(tmp_path):
    context = SimpleNamespace(page_texts=('income', 'continuation', 'balance'),
        units=SimpleNamespace(declarations=()), periods=())
    catalog = document_catalog(context, 'original', 'rules',
        lambda text: ('income_statement', 'consolidated') if text == 'income' else
            ('balance_sheet', 'consolidated') if text == 'balance' else None,
        select_candidates=lambda _texts, _pages: frozenset({1, 2, 3}))
    save_document_catalog(tmp_path, catalog)

    restored = load_document_catalog(tmp_path, 'original', 'rules')

    assert candidate_pages_from_catalog(restored) == frozenset({1, 2, 3})
    assert [page['candidate'] for page in restored['pages']] == [True, True, True]


def test_incomplete_catalogue_plan_is_not_treated_as_no_financial_pages():
    context = SimpleNamespace(page_texts=('unknown',), units=SimpleNamespace(declarations=()), periods=())
    catalog = document_catalog(context, 'original', 'rules', lambda _text: None,
        select_candidates=lambda _texts, _pages: None)
    assert candidate_pages_from_catalog(catalog) is None
