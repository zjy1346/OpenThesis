"""Profile one original page with its frozen predecessor table context."""
import argparse
import cProfile
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import pstats
import sqlite3
import time

from openthesis.domain import Company, FilingDocument
from openthesis.financial_checkpoint import CheckpointKey, WindowCheckpointStore
from openthesis.financial_ingestion import FinancialIngestionEngine, _manifest_for
from openthesis.financial_sources.pdf_document_units import read_document_context
from openthesis.financial_statutory_currency import bind_statutory_presentation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--company', required=True)
    parser.add_argument('--accession', required=True)
    parser.add_argument('--page', type=int, required=True)
    parser.add_argument('--checkpoints', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        value = next(json.loads(row[0])['company'] for row in db.execute(
            'SELECT payload_json FROM research_runs ORDER BY started_at DESC')
            if json.loads(row[0]).get('company',{}).get('ticker') == args.company)
        subject = Company(**{field.name:value[field.name] for field in fields(Company) if field.name in value})
        row = db.execute('SELECT * FROM filings WHERE accession_number=? AND company_cik=?',
            (args.accession,subject.security_id)).fetchone()
        filing = FilingDocument(**{field.name:row[field.name] for field in fields(FilingDocument) if field.name in row.keys()})
    with Path(filing.local_path).open('rb') as handle:
        assert hashlib.file_digest(handle,'sha256').hexdigest() == filing.content_hash
    context = read_document_context(filing.local_path,source_hash=filing.content_hash,
        checkpoint_dir=args.checkpoints/'document-text',timeout_seconds=120)
    initial = None
    for path in args.checkpoints.glob('*.json'):
        payload = json.loads(path.read_text(encoding='utf-8')).get('payload',{})
        if payload.get('key',{}).get('document_hash') != filing.content_hash:
            continue
        if payload.get('window',{}).get('pages') == [args.page-1]:
            key = CheckpointKey(**payload['key'])
            saved = WindowCheckpointStore(args.checkpoints).load(key,payload['window']['index'])
            initial = saved.context if saved else None
            break
    units = bind_statutory_presentation(context.units,context.page_texts,subject,filing)
    engine = FinancialIngestionEngine()
    profiler = cProfile.Profile()
    started = time.monotonic()
    facts, refs = profiler.runcall(engine._parse_pdf_ast,filing.local_path,subject,filing,_manifest_for(filing),
        candidate_pages=frozenset({args.page}),index_precomputed=True,initial_context=initial,
        document_units=units,document_units_bound=True,document_texts=context.page_texts)
    args.output.mkdir(parents=True,exist_ok=False)
    profiler.dump_stats(str(args.output/'page.prof'))
    with (args.output/'profile.txt').open('w',encoding='utf-8') as handle:
        pstats.Stats(profiler,stream=handle).sort_stats('cumulative').print_stats(30)
    result = {'elapsed_seconds':time.monotonic()-started,'source_hash':filing.content_hash,
        'page':args.page,'facts':[fact.to_dict() for fact in facts],'evidence_count':len(refs)}
    (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'elapsed_seconds':result['elapsed_seconds'],'facts':len(facts)}))


if __name__ == '__main__':
    main()
