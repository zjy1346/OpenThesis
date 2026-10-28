from openthesis.financial_sources.structure import parse_structure
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.vision_financials import VisionPageRequest, parse_vision_json
from test_vision_financials import _company, _filing


def frame():
    return {'schema':'financial-structure-v1','original_page':1,'blocks':[
        {'type':'title','text':'合并利润表'}, {'type':'text','text':'单位：人民币元'},
        {'type':'table','bbox':[0,0,300,300],'rows':[
            [{'text':'项目'},{'text':'2025年'},{'text':'2024年'}],
            [{'text':'营业收入'},{'text':'100'},{'text':'80'}],
            [{'text':'未登记的复杂会计科目'},{'text':'11'},{'text':'9'}]]}]}


def test_structured_cells_keep_coordinates_unknown_rows_and_comparative_columns():
    filing = _filing()
    page = VisionPageRequest(1,b'page')
    payload = frame()
    result = parse_vision_json(payload, _company(), filing, (page,))
    assert result.error_code is None
    assert {(fact.fiscal_year, fact.value) for fact in result.facts} == {(2025,100),(2024,80)}
    assert any('未登记的复杂会计科目' in ref.excerpt for ref in result.evidence)
    compiled = FinancialFactCompiler().compile_facts(_company(), (filing,), result.facts,
        evidence=result.evidence, required_concepts=frozenset({'revenue'}))
    assert len(compiled.accepted_facts) == 2 and not compiled.quarantined_facts


def test_geometry_and_page_identity_cannot_be_guessed():
    payload = frame()
    payload['blocks'][-1]['bbox'] = [5,5,1,1]
    assert not parse_structure(payload,_company(),_filing(),(VisionPageRequest(1,b'p'),)).facts
    assert not parse_structure(frame(),_company(),_filing(),(VisionPageRequest(2,b'p'),)).facts
    for invalid_page in (True,False,1.0,'1',0,-1,None):
        payload = frame()
        payload['original_page'] = invalid_page
        result = parse_structure(payload,_company(),_filing(),(VisionPageRequest(1,b'p'),))
        assert not result.facts and result.error_code == 'VISION_PAGE_MAPPING_REQUIRED'


def test_parent_and_consolidated_structures_remain_separate():
    payload = frame()
    payload['blocks'][0]['text'] = '母公司利润表'
    result = parse_structure(payload,_company(),_filing(),(VisionPageRequest(1,b'p'),))
    assert result.facts
    assert all(fact.scope == fact.consolidated_scope == 'parent' for fact in result.facts)
    assert not any(fact.consolidated_scope == 'consolidated' for fact in result.facts)


def test_parallel_currencies_bind_to_their_own_header_columns_not_listing_currency():
    payload = frame()
    payload['blocks'][1]['text'] = 'Amounts in millions'
    payload['blocks'][-1]['rows'] = [
        [{'text':'Item'}, {'text':'2025 RMB'}, {'text':'2025 US$'}, {'text':'2024 RMB'}],
        [{'text':'Revenue'}, {'text':'100'}, {'text':'14'}, {'text':'80'}]]
    result = parse_structure(payload,_company(),_filing(),(VisionPageRequest(1,b'p'),))
    assert {(fact.fiscal_year, fact.value, fact.currency) for fact in result.facts} == {
        (2025,100_000_000,'CNY'),(2024,80_000_000,'CNY')}
    compiled = FinancialFactCompiler().compile_facts(_company(),(_filing(),),result.facts,
        evidence=result.evidence,required_concepts=frozenset({'revenue'}))
    assert len(compiled.accepted_facts) == 2 and not compiled.quarantined_facts


def test_conflicting_currency_column_keeps_other_proven_columns_and_unknown_structure():
    payload = frame()
    payload['blocks'][1]['text'] = 'Amounts in millions'
    payload['blocks'][-1]['rows'][0][1]['text'] = '2025 RMB / USD'
    payload['blocks'][-1]['rows'][0][2]['text'] = '2024 RMB'
    result = parse_structure(payload,_company(),_filing(),(VisionPageRequest(1,b'p'),))
    assert {(fact.fiscal_year, fact.value) for fact in result.facts} == {(2024,80_000_000)}
    assert any('未登记的复杂会计科目' in ref.excerpt for ref in result.evidence)


def test_balance_equity_attribution_is_not_misclassified_as_profit():
    from openthesis.financial_table_text import concept_for_label
    assert concept_for_label('归属于母公司所有者权益合计',statement='balance_sheet') == 'equity'
    assert concept_for_label('归属于母公司所有者的净利润',statement='income_statement') == 'parent_net_income'


def test_malformed_native_structure_is_a_controlled_candidate_failure():
    import pytest
    from openthesis.financial_sources.structure import paddle_structure
    for raw in (None, [], {'res':None}, {'parsing_res_list':None},
                {'parsing_res_list':['not a block']},
                {'parsing_res_list':[{'block_label':[]} ]},
                {'parsing_res_list':[{'block_label':'table','block_content':{}}]}):
        with pytest.raises(ValueError, match='VISION_STRUCTURE_INVALID'):
            paddle_structure(raw,1)
