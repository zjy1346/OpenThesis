"""Replay hash-verified local originals through the production pipeline, offline."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, fields
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone

from openthesis.domain import Company, FilingDocument
from openthesis.financial_contracts import CompilationPolicy
from openthesis.financial_ingestion import FinancialIngestionEngine
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot


def encode(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


def fact_identity(fact):
    return tuple(fact.get(key) for key in ('accession_number', 'concept', 'start_date', 'end_date',
        'fiscal_period', 'currency', 'consolidated_scope', 'value', 'unit_scale'))


def windows_tree_rss():
    import ctypes
    from ctypes import wintypes
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('size',wintypes.DWORD), ('usage',wintypes.DWORD), ('pid',wintypes.DWORD),
            ('heap',ctypes.c_size_t), ('module',wintypes.DWORD), ('threads',wintypes.DWORD),
            ('parent',wintypes.DWORD), ('priority',wintypes.LONG), ('flags',wintypes.DWORD),
            ('exe',wintypes.WCHAR*260)]
    class Counters(ctypes.Structure):
        _fields_ = [('size',wintypes.DWORD), ('faults',wintypes.DWORD)] + [
            (name,ctypes.c_size_t) for name in ('peak_ws','ws','peak_pool','pool','peak_nonpool',
                'nonpool','pagefile','peak_pagefile')]
    kernel, api = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    for name in ('Process32FirstW','Process32NextW'):
        function = getattr(kernel,name)
        function.argtypes = [wintypes.HANDLE,ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    snapshot = kernel.CreateToolhelp32Snapshot(2,0)
    parents = {}
    try:
        entry = ProcessEntry(size=ctypes.sizeof(ProcessEntry))
        if kernel.Process32FirstW(snapshot,ctypes.byref(entry)):
            while True:
                parents[entry.pid] = entry.parent
                if not kernel.Process32NextW(snapshot,ctypes.byref(entry)):
                    break
    finally:
        kernel.CloseHandle(snapshot)
    tree = {os.getpid()}
    while True:
        children = {pid for pid,parent in parents.items() if parent in tree}
        if children.issubset(tree):
            break
        tree.update(children)
    total = 0
    for pid in tree:
        handle = kernel.OpenProcess(0x0410,False,pid)
        if handle:
            try:
                counters = Counters(size=ctypes.sizeof(Counters))
                if api.GetProcessMemoryInfo(handle,ctypes.byref(counters),ctypes.sizeof(counters)):
                    total += counters.ws
            finally:
                kernel.CloseHandle(handle)
    return total


def source_digest():
    digest = hashlib.sha256()
    for path in sorted(Path('src/openthesis').rglob('*')):
        if path.is_file() and path.suffix in {'.py','.json','.txt','.md'}:
            digest.update(path.as_posix().encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def run_packaged(executable, output, company, filings, progress, timeout, cache_from=None):
    """Seed identities only; the frozen binary owns extraction and admission."""
    from openthesis.domain import ResearchRun, RunStatus
    from openthesis.storage import Storage
    data = (cache_from or output).resolve() / 'data'
    storage = Storage(data)
    run_id = 'recognition-benchmark'
    if cache_from is None:
        storage.save_company(company)
        storage.save_filings(filings)
        storage.save_run(ResearchRun(run_id, company, 'complete-fundamental-research',
            'default', '1', 'none', 'none', '2026-10-26', status=RunStatus.PARTIAL,
            report_language='zh-CN', model_configuration={}, research_configuration={
                'annual_history_years':5, 'run_envelope':{'policy':'none'}}))
    else:
        expected = {(filing.accession_number,filing.content_hash) for filing in filings}
        actual = {(filing.accession_number,filing.content_hash) for filing in storage.get_filings(
            company.cik if company.market == 'US' else company.security_id)}
        if actual != expected or storage.get_run(run_id) is None:
            raise ValueError('package warm source identity mismatch')
    environment = os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','OPENTHESIS_PYTHON'):
        environment.pop(key, None)
    environment['PATH'] = str(Path(environment.get('SystemRoot', 'C:/Windows')) / 'System32')
    environment['TEMP'] = environment['TMP'] = str(output.resolve())
    messages = queue.Queue()
    with (output/'sidecar-stderr.log').open('w', encoding='utf-8') as stderr:
        child = subprocess.Popen([str(executable.resolve()), '--data-dir', str(data)],
            cwd=executable.resolve().parent, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=stderr, text=True, encoding='utf-8',
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def read():
            for line in child.stdout:
                messages.put(json.loads(line))
            messages.put(None)
        threading.Thread(target=read, daemon=True).start()
        def rpc(method, params):
            child.stdin.write(json.dumps({'jsonrpc':'2.0','id':method,'method':method,'params':params})+'\n')
            child.stdin.flush()
            response = messages.get(timeout=180)
            if response is None or response.get('id') != method or 'error' in response:
                raise RuntimeError(response)
            return response['result']
        try:
            hello = rpc('system.hello', {})
            job = rpc('research.start_financial_retry', {'run_id':run_id})
            deadline = time.monotonic() + timeout
            while True:
                status = rpc('research.status', {'job_id':job['job_id']})
                progress(status.get('stage'), status.get('percent'), 100, status)
                if status.get('state') in {'completed','partial','failed','cancelled'}:
                    break
                if time.monotonic() >= deadline:
                    rpc('research.cancel', {'job_id':job['job_id']})
                    raise TimeoutError('benchmark observer timeout; partial evidence retained')
                time.sleep(1)
            diagnostics = rpc('research.financial_diagnostics', {'run_id':run_id})
            report = rpc('research.get_report', {'run_id':run_id, 'language':'zh-CN'})
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=encode),encoding='utf-8')
            (output/'package-status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2,default=encode),encoding='utf-8')
            if storage.model_call_progress(run_id)['calls']:
                raise AssertionError('offline benchmark unexpectedly contains model calls')
            if status.get('state') not in {'completed','partial'}:
                raise AssertionError('packaged recognition failed: '+str(status.get('error_code')))
            return storage.get_facts(company.cik if company.market == 'US' else company.security_id), {
                'package_version':hello.get('app_version'), 'build_info':hello.get('build_info'),
                'sidecar_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
                'diagnostics':diagnostics, 'financial_status':report.get('financial_status'),
                'research_status':status.get('state'),
                'open_recovery_cases':[case for case in diagnostics.get('recovery_cases',[]) if case.get('status') != 'resolved'],
                'parsing_observations':diagnostics.get('parse_observations',[])}
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--company', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache-from', type=Path)
    parser.add_argument('--baseline-facts', type=Path)
    parser.add_argument('--sidecar', type=Path)
    parser.add_argument('--observer-timeout', type=float, default=3600)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        company = None
        for row in db.execute('SELECT payload_json FROM research_runs ORDER BY started_at DESC'):
            value = json.loads(row[0]).get('company', {})
            if args.company in (value.get('security_id'), value.get('ticker'), value.get('cik')):
                company = Company(**{item.name:value[item.name] for item in fields(Company) if item.name in value})
                break
        if company is None:
            raise ValueError('benchmark company absent')
        filing_fields = {item.name for item in fields(FilingDocument)}
        filings = tuple(FilingDocument(**{key:row[key] for key in row.keys() if key in filing_fields})
            for row in db.execute('SELECT * FROM filings WHERE company_cik=? ORDER BY period_end DESC,filed_at DESC,document_id',
                (company.cik if company.market == 'US' else company.security_id,)))
    if not filings:
        raise ValueError('benchmark originals absent')
    args.output.mkdir(parents=True, exist_ok=False)
    originals = []
    for filing in filings:
        with Path(filing.local_path).open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != filing.content_hash:
            raise ValueError('benchmark original hash mismatch')
        originals.append({'document_id':filing.document_id, 'accession':filing.accession_number,
            'period_end':filing.period_end, 'fiscal_period':filing.fiscal_period, 'sha256':digest})
    cache = args.cache_from or args.output
    source_before = source_digest()
    engine = FinancialIngestionEngine(cache_dir=cache/'cache', checkpoint_dir=cache/'checkpoints')
    started = time.monotonic()
    def progress(stage, current, total, detail=None):
        with (args.output/'progress.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'elapsed':round(time.monotonic()-started,4), 'stage':stage,
                'current':current, 'total':total, 'detail':detail}, ensure_ascii=False)+'\n')
    stop = threading.Event()
    memory = {'peak_process_tree_rss_bytes':None, 'sampler':'unavailable'}
    def sample():
        try:
            import psutil
            process = psutil.Process()
            memory['sampler'] = 'psutil-process-tree'
            while not stop.is_set():
                total = 0
                for item in (process, *process.children(recursive=True)):
                    try:
                        total += item.memory_info().rss
                    except psutil.Error:
                        pass
                memory['peak_process_tree_rss_bytes'] = max(memory['peak_process_tree_rss_bytes'] or 0, total)
                stop.wait(.25)
        except ImportError:
            if os.name == 'nt':
                memory['sampler'] = 'windows-process-tree-working-set'
                while not stop.is_set():
                    try:
                        total = windows_tree_rss()
                        memory['peak_process_tree_rss_bytes'] = max(memory['peak_process_tree_rss_bytes'] or 0,total)
                    except OSError:
                        pass
                    stop.wait(.25)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        if args.sidecar:
            facts, package = run_packaged(args.sidecar,args.output,company,filings,progress,args.observer_timeout,args.cache_from)
        else:
            view = FinancialPipeline(engine).prepare(FinancialResearchSnapshot(company, filings,
                requested_annual_count=6, research_as_of='2026-10-26', progress=progress),
                CompilationPolicy(reporting_currency=company.reporting_currency))
            facts = [asdict(fact) for fact in view.facts]
    finally:
        stop.set()
        sampler.join(timeout=2)
    missing = []
    if args.baseline_facts:
        previous = json.loads(args.baseline_facts.read_text(encoding='utf-8'))
        now = {fact_identity(fact) for fact in facts}
        missing = [fact for fact in previous if fact_identity(fact) not in now]
    result = {'company':company.to_dict(), 'elapsed_seconds':round(time.monotonic()-started,4),
        'cold':args.cache_from is None, 'originals':originals, 'model_calls':0,
        'completed_at':datetime.now(timezone.utc).isoformat(),
        'source_digest_before':source_before, 'source_digest_after':source_digest(),
        'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'environment':{'platform':platform.platform(), 'processor':platform.processor(), 'python':platform.python_version()},
        'memory':memory, 'facts_by_accession':dict(Counter(fact['accession_number'] for fact in facts)),
        'baseline_missing_facts':missing, **(package if args.sidecar else {
            'readiness':asdict(view.readiness),
            'source_failures':[asdict(item) for item in view.canonical_dataset.source_failures],
            'diagnostics':view.diagnostics, 'document_execution':engine._document_execution,
            'parsing_observations':view.profile.parsing_observations})}
    outputs = [('result',result), ('facts',facts)]
    if not args.sidecar:
        outputs.append(('canonical',asdict(view.canonical_dataset)))
    for name, value in outputs:
        (args.output/f'{name}.json').write_text(json.dumps(value,ensure_ascii=False,indent=2,default=encode),encoding='utf-8')
    print(json.dumps({key:result[key] for key in ('elapsed_seconds','facts_by_accession','source_failures','open_recovery_cases','memory') if key in result},ensure_ascii=False))


if __name__ == '__main__':
    main()
