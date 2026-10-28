"""Offline frozen-sidecar PDF check, not a host-Python parser smoke test.

The driver seeds only source identities. All page reading is performed by the
packaged RPC executable with a Windows-only PATH and cold caches. No provider,
network, user database or pre-extracted text is involved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import zipfile


def main():
    parser = argparse.ArgumentParser()
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument('--archive', type=Path)
    source_group.add_argument('--sidecar', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    unpacked = args.output / '中文路径 with spaces'
    if args.archive:
        with zipfile.ZipFile(args.archive) as archive:
            for name in archive.namelist():
                if not (unpacked / name).resolve().is_relative_to(unpacked.resolve()):
                    raise ValueError('archive path traversal')
            archive.extractall(unpacked)
        executable = unpacked / f'OpenThesis-{args.version}/bin/openthesis-sidecar/openthesis-sidecar.exe'
    else:
        executable = args.sidecar.resolve()
    data = args.output.resolve() / 'data'
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    # Synthetic local fixture; standard PDF text operators are independent of
    # the native backend being checked. It is never used as financial truth.
    data.mkdir()
    source = data / '公开测试 sample.pdf'
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 20 250 Td (OpenThesis packaged PDF runtime check 2025 revenue) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    with source.open('wb') as handle:
        writer.write(handle)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    from openthesis.storage import Storage
    from openthesis.domain import Company, FilingDocument, ResearchRun, RunStatus
    storage = Storage(data)
    company = Company('runtime-fixture', 'RUNTIME', 'Local runtime fixture')
    storage.save_company(company)
    storage.save_filings([FilingDocument('runtime-pdf', company.cik, 'runtime-pdf', '10-K', 'FY', '2025-12-31', '2026-01-01', 'Local fixture',
        'https://example.invalid/fixture.pdf', local_path=str(source), content_hash=source_hash)])
    storage.save_run(ResearchRun('runtime-probe', company, 'runtime-check', '', '', 'none', '', '2026-01-01', status=RunStatus.PARTIAL))
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'OPENTHESIS_PYTHON'):
        env.pop(key, None)
    env['PATH'] = str(Path(env.get('SystemRoot', 'C:/Windows')) / 'System32')
    env['TEMP'] = env['TMP'] = str(args.output.resolve())
    events = queue.Queue()
    with (args.output / 'stderr.log').open('wb') as stderr:
        child = subprocess.Popen([str(executable.resolve()), '--data-dir', str(data.resolve())], cwd=executable.parent,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def reader():
            for line in child.stdout:
                events.put(line)
            events.put(None)
        threading.Thread(target=reader, daemon=True).start()
        def rpc(method, params):
            child.stdin.write(json.dumps(dict(jsonrpc='2.0', id=method, method=method, params=params)) + '\n')
            child.stdin.flush()
            response = json.loads(events.get(timeout=120))
            if response.get('id') != method or 'error' in response:
                raise RuntimeError(response)
            return response['result']
        try:
            hello = rpc('system.hello', {})
            result = rpc('research.pdf_runtime', {'run_id':'runtime-probe', 'document_id':'runtime-pdf'})
            proof = {'archive_sha256': hashlib.sha256(args.archive.read_bytes()).hexdigest() if args.archive else None, 'version':hello['app_version'], **result}
            (args.output / 'pdf-proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
            assert hello['app_version'] == args.version
            assert result['source_hash'] == source_hash and result['raw_error_code'] == ''
            assert result['page_count'] == result['nonempty_pages'] == 1 and result['text_characters'] >= 40
            assert len(result['worker_diagnostics']) >= 2
            assert all(item['ready_received'] and item['artifact_received'] and item['exit_code'] == 0 for item in result['worker_diagnostics'])
            assert storage.model_call_progress('runtime-probe')['calls'] == []
            print(json.dumps({'packaged_pdf':'PASS', 'version':hello['app_version'], 'proof':str(args.output / 'pdf-proof.json')}))
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)


if __name__ == '__main__':
    main()
