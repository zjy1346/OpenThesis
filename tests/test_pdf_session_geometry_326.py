from dataclasses import replace
from types import SimpleNamespace

from openthesis.domain import Company, FilingDocument
from openthesis.financial_compatibility import FinancialRulesSnapshot
from openthesis.financial_checkpoint import PdfWindow
from openthesis.financial_ingestion import (
    FinancialIngestionEngine, _manifest_for, _CheckpointPdfDocumentSession,
    _CheckpointPdfWindowWorker,
)


def test_rule_change_reuses_raw_pages_without_reopening_or_extracting_pdf(tmp_path, monkeypatch):
    words = [{'text':'Revenue', 'x0':1.0, 'x1':80.0, 'top':2.0, 'bottom':9.0}]
    calls = {'open':0, 'words':0, 'close':0}
    class Page:
        def extract_words(self, **options):
            assert options == {'keep_blank_chars':False,'use_text_flow':False}
            calls['words'] += 1
            return words
        def extract_text(self):
            return 'original positioned page text'
        def close(self):
            pass
    class Pdf:
        pages = [Page(), Page()]
        def close(self):
            calls['close'] += 1
    def open_pdf(_path):
        calls['open'] += 1
        return Pdf()
    monkeypatch.setattr('pdfplumber.open', open_pdf)
    observed = []
    def parse(self, *args, **options):
        for page in sorted(options['candidate_pages']):
            observed.append((options['compatibility_rules'].version, options['page_data'](page)))
        return [], []
    monkeypatch.setattr(FinancialIngestionEngine, '_parse_pdf_ast', parse)
    company = Company('fixture','FIX','Fixture')
    filing = FilingDocument('doc','fixture','accession','10-K','FY','2025-12-31',
        '2026-01-01','annual.pdf','https://example.invalid/annual.pdf',content_hash='a'*64)
    first_rules = FinancialRulesSnapshot(version='first')
    first_worker = _CheckpointPdfWindowWorker('not-read-by-cache.pdf',company,filing,
        _manifest_for(filing),first_rules,document_texts=('native index','native index'),
        checkpoint_directory=str(tmp_path))
    session = _CheckpointPdfDocumentSession(first_worker)
    assert calls['open'] == 0
    session(PdfWindow(1,(1,)),{})
    session(PdfWindow(2,(2,)),{})
    assert calls['open'] == 1 and calls['words'] == 2
    assert session.diagnostics['geometry_extracted_pages'] == 2
    session.close()
    second = _CheckpointPdfDocumentSession(replace(first_worker,
        compatibility_rules=replace(first_rules,version='second')))
    second(PdfWindow(3,(1,)),{})
    second(PdfWindow(4,(2,)),{})
    second.close()
    assert calls == {'open':1,'words':2,'close':1}
    assert second.diagnostics['geometry_cache_hits'] == 2
    assert second.diagnostics['geometry_extracted_pages'] == 0
    assert second.diagnostics['pdf_open_elapsed_seconds'] == 0
    assert observed[:2] == [('first',(words,'original positioned page text'))]*2
    assert observed[2:] == [('second',(words,'original positioned page text'))]*2
