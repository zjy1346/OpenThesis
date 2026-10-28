"""A verified alternative backend resolves only its own native failure fence."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openthesis.financial_contracts import CandidateBatch, CompilationPolicy, FactCandidate, FinancialSourceFailure, GapStageKind, fact_row_evidence_ids
from openthesis.financial_ingestion import FinancialCandidateCollection
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from openthesis.financial_sources.structure import parse_structure
from openthesis.financial_source_errors import PDF_RUNTIME_FAILURE_CODES
from openthesis.vision_financials import VisionPageRequest
from test_vision_financials import _company, _filing


@pytest.mark.parametrize('backend',['local','cloud'])
@pytest.mark.parametrize('code,recovered',
    [(code,True) for code in sorted(PDF_RUNTIME_FAILURE_CODES | {
        'DOCUMENT_CONTEXT_OCR_REQUIRED','PDF_TEXT_EXTRACTION_FAILED','DOCUMENT_CONTEXT_TIMEOUT',
        'PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION','PDFIUM_WORKER_TIMEOUT'})]
    + [(code,False) for code in ('PDF_SOURCE_HASH_MISMATCH','FILING_DOWNLOAD_FAILED',
        'FILING_PERIOD_METADATA_CONFLICT','PDF_SOURCE_INVALID','DOCUMENT_CONTEXT_CANCELLED')])
def test_verified_structure_core_releases_only_recoverable_native_fence(monkeypatch,code,recovered,backend):
    filing = _filing()
    frame = {'schema':'financial-structure-v1','original_page':1,'backend':'paddle-structure-v3','blocks':[]}
    for title,rows in [('合并资产负债表',[['资产总计','100'],['负债合计','60'],['归属于母公司所有者权益合计','40']]),
                       ('合并利润表',[['营业收入','100'],['净利润','10']]),
                       ('合并现金流量表',[['经营活动产生的现金流量净额','15']])]:
        frame['blocks'].extend([{'type':'title','text':title},{'type':'text','text':'单位：人民币元'},
            {'type':'table','rows':[[{'text':'项目'},{'text':'2025年'}],
                *[[{'text':value} for value in row] for row in rows]]}])
    parsed = parse_structure(frame,_company(),filing,(VisionPageRequest(1,b'page'),))
    refs = {ref.evidence_id:ref for ref in parsed.evidence}
    candidates = tuple(FactCandidate(fact,tuple(refs[key] for key in fact_row_evidence_ids(fact) if key in refs),
        'paddle-structure-v3') for fact in parsed.facts)
    class Engine:
        _local_structure_root = 'component'
        _document_execution = {filing.document_id:{'source_hash':filing.content_hash,'local_structure':{
            'selected_pages':1,'completed_pages':1,'state':'ready'}}}
        def collect_candidate_batches(self,*args,**kwargs):
            return FinancialCandidateCollection((),{filing.document_id:(CandidateBatch(filing),CandidateBatch(filing))},
                source_failures=(FinancialSourceFailure(filing.document_id,filing.accession_number,'parse',code),))
    adapter = SimpleNamespace(name='paddle-structure-v3',stage_kind=GapStageKind.LOCAL_STRUCTURE,
        extract_missing=lambda *args:CandidateBatch(filing,candidates,parsed.evidence))
    monkeypatch.setattr('openthesis.local_structure.LocalStructureExtractor',lambda *args:adapter)
    engine = Engine()
    options = {}
    cloud_calls = []
    if backend == 'cloud':
        engine._local_structure_root = None
        def prefetch(*args,**kwargs):
            cloud_calls.append(True)
            return {filing.document_id:CandidateBatch(filing,candidates,parsed.evidence)}
        monkeypatch.setattr('openthesis.financial_pipeline._prefetch_vision_batches',
            prefetch)
        options = {'vision_config':SimpleNamespace(enabled=True,consent=True,timeout_seconds=60,validate=lambda:None),
            'vision_fallback':object(),'authorized_source_stages':frozenset({GapStageKind.MINERU})}
    view = FinancialPipeline(engine).prepare(FinancialResearchSnapshot(_company(),(filing,),**options),
        CompilationPolicy(reporting_currency='CNY'))
    if backend == 'cloud' and code in PDF_RUNTIME_FAILURE_CODES:
        # A runtime failure without a trustworthy page plan is not consent to
        # upload an entire original. The independently installed local runtime
        # may recover it; this cloud fixture must preserve the existing fence.
        assert cloud_calls == []
        assert view.canonical_dataset.source_failures[0].required is True
        assert filing.accession_number not in view.publishable_accessions
        return
    if recovered:
        assert not [issue for group in view.canonical_dataset.validations for issue in group.issues]
    assert view.canonical_dataset.source_failures[0].code == code
    assert view.canonical_dataset.source_failures[0].required is not recovered, (
        view.canonical_dataset.diagnostics,
        [(group.covered,group.issues) for group in view.canonical_dataset.validations],
        [(fact.concept,fact.currency,fact.end_date) for fact in view.facts])
    assert (filing.accession_number in view.publishable_accessions) is recovered
