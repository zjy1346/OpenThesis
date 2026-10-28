"""Opt-in, isolated PP-StructureV3 runtime; no local training or cloud calls."""
from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import re
import subprocess
import tempfile
import threading
import time

from .financial_contracts import CandidateBatch, FactCandidate, GapStageKind, STATEMENT_FOR, fact_row_evidence_ids
from .vision_financials import VisionPageRequest
from .resources.structure_worker import VERSIONS


STRUCTURE_MODEL_LOCK_SCHEMA = 'openthesis-structure-models-v1'
STRUCTURE_STRATEGY_VERSION = 'pp-structure-v3-page-route-v1'
_MODEL_HASH_CACHE = {}
_MODEL_HASH_CACHE_LOCK = threading.RLock()


def structure_lock_path():
    """Return the lock file shipped with this exact package version."""
    return Path(__file__).parent / 'resources' / 'structure-models.lock.json'


def _normalized_lock_files(lock):
    if (not isinstance(lock, dict) or lock.get('schema') != STRUCTURE_MODEL_LOCK_SCHEMA
            or lock.get('versions') != VERSIONS or not isinstance(lock.get('files'), list)
            or not lock['files']):
        raise ValueError('STRUCTURE_MODEL_LOCK_INVALID')
    files = []
    seen = set()
    for item in lock['files']:
        if not isinstance(item, dict):
            raise ValueError('STRUCTURE_MODEL_LOCK_INVALID')
        relative = item.get('path')
        size = item.get('size')
        digest = item.get('sha256')
        if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative or '\x00' in relative
                or not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('STRUCTURE_MODEL_LOCK_INVALID')
        relative_path = PurePosixPath(relative)
        windows_path = PureWindowsPath(relative)
        if (relative_path.is_absolute() or windows_path.is_absolute() or windows_path.drive
                or any(part in {'', '.', '..'} for part in relative_path.parts)
                or relative_path.as_posix() != relative or relative.casefold() in seen):
            raise ValueError('STRUCTURE_MODEL_LOCK_INVALID')
        seen.add(relative.casefold())
        files.append({'path': relative, 'size': size, 'sha256': digest})
    return files


def _read_structure_lock(lock_path=None):
    path = Path(lock_path) if lock_path is not None else structure_lock_path()
    lock = json.loads(path.read_text(encoding='utf-8'))
    return lock, _normalized_lock_files(lock)


def _contained_directory(path, root):
    try:
        resolved_root = Path(root).resolve(strict=True)
        resolved = Path(path).resolve(strict=True)
        return resolved if resolved.is_dir() and resolved.is_relative_to(resolved_root) else None
    except (OSError, RuntimeError):
        return None


def _contained_file(path, root):
    try:
        resolved_root = Path(root).resolve(strict=True)
        resolved = Path(path).resolve(strict=True)
        return resolved if resolved.is_file() and resolved.is_relative_to(resolved_root) else None
    except (OSError, RuntimeError):
        return None


def _component_model_paths(root, files):
    root = Path(root)
    component_root = root.resolve(strict=True)
    if not component_root.is_dir():
        return None
    model_root = _contained_directory(root / 'cache' / 'official_models', component_root)
    if model_root is None:
        return None
    paths = []
    for item in files:
        source = _contained_file(model_root.joinpath(*PurePosixPath(item['path']).parts), model_root)
        if source is None or source.stat().st_size != item['size']:
            return None
        paths.append((item, source))
    return paths


def _manifest_matches_lock(manifest, files):
    if (not isinstance(manifest, dict) or manifest.get('versions') != VERSIONS
            or manifest.get('cpu_inference_verified') is not True
            or not isinstance(manifest.get('files'), list)):
        return False
    try:
        manifest_files = _normalized_lock_files({
            'schema': STRUCTURE_MODEL_LOCK_SCHEMA,
            'versions': VERSIONS,
            'files': manifest['files'],
        })
    except (ValueError, TypeError):
        return False
    return manifest_files == files


def _stat_fingerprint(path):
    stat = path.stat()
    return (getattr(stat, 'st_dev', 0), getattr(stat, 'st_ino', 0), stat.st_size,
            getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1_000_000_000)),
            getattr(stat, 'st_ctime_ns', int(stat.st_ctime * 1_000_000_000)))


def verify_model_integrity(root, *, lock_path=None):
    """Full SHA-256 validation immediately before use, cached by safe stat identity."""
    try:
        root = Path(root)
        lock, files = _read_structure_lock(lock_path)
        root_resolved = root.resolve(strict=True)
        manifest_path = _contained_file(root / 'component-manifest.json', root_resolved)
        if manifest_path is None:
            return False
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not _manifest_matches_lock(manifest, files):
            return False
        model_paths = _component_model_paths(root, files)
        if model_paths is None:
            return False
        for item, source in model_paths:
            fingerprint = _stat_fingerprint(source)
            cache_key = (str(source), item['sha256'])
            with _MODEL_HASH_CACHE_LOCK:
                if _MODEL_HASH_CACHE.get(cache_key) == fingerprint:
                    continue
            if file_hash(source) != item['sha256'] or _stat_fingerprint(source) != fingerprint:
                return False
            with _MODEL_HASH_CACHE_LOCK:
                if len(_MODEL_HASH_CACHE) > 1024:
                    _MODEL_HASH_CACHE.clear()
                _MODEL_HASH_CACHE[cache_key] = fingerprint
        return True
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def component_status(root, *, lock_path=None):
    root = Path(root)
    manifest_path = root / 'component-manifest.json'
    try:
        root_resolved = root.resolve(strict=True)
        manifest_path = _contained_file(manifest_path, root_resolved)
    except (OSError, RuntimeError):
        manifest_path = None
    if manifest_path is None:
        if (root / 'component-manifest.json').exists():
            return {'available':False,'versions':{},'error_code':'STRUCTURE_COMPONENT_INCOMPLETE'}
        return {'available':False,'versions':{},'error_code':'STRUCTURE_COMPONENT_NOT_INSTALLED'}
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        _lock, files = _read_structure_lock(lock_path)
        runtime = _contained_file(root / 'runtime' / 'Scripts' / 'python.exe', root_resolved)
        ready = (_manifest_matches_lock(manifest, files) and runtime is not None
            and _component_model_paths(root, files) is not None)
        versions = manifest.get('versions', {}) if isinstance(manifest, dict) else {}
        return {'available': bool(ready), 'versions': versions if isinstance(versions, dict) else {},
                'error_code': '' if ready else 'STRUCTURE_COMPONENT_INCOMPLETE'}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return {'available':False,'versions':{},'error_code':'STRUCTURE_COMPONENT_INCOMPLETE'}


def install_component(python_executable, root, *, confirmed=False, cancel_check=None, progress=None):
    """Explicit user action, fixed isolated environment, no silent activation."""
    if confirmed is not True:
        raise ValueError('STRUCTURE_INSTALL_CONFIRMATION_REQUIRED')
    python = Path(python_executable)
    if not python.is_absolute() or not python.is_file():
        raise ValueError('STRUCTURE_PYTHON_REQUIRED')
    root = Path(root)
    resources = Path(__file__).parent / 'resources'
    notify = progress or (lambda phase: None)
    notify('checking-python')
    run_component_command([str(python), '-I', '-c',
        'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 12)'], root,
        cancel_check=cancel_check, timeout=30., log_name='python-version.log',
        failure_code='STRUCTURE_RUNTIME_VERSION_MISMATCH')
    notify('installing-runtime')
    run_component_command([str(python), '-I', '-m', 'venv', str(root / 'runtime')], root,
        cancel_check=cancel_check, timeout=120., log_name='venv.log')
    interpreter = str(root / 'runtime/Scripts/python.exe')
    notify('downloading-dependencies')
    run_component_command([interpreter, '-m', 'pip', 'install', '--disable-pip-version-check',
        '-r', str(resources / 'structure-runtime-requirements.txt')], root,
        cancel_check=cancel_check, timeout=None, log_name='dependencies.log')
    notify('downloading-models-and-verifying-cpu')
    proof = root / 'cpu-proof.json'
    run_component_command([interpreter, str(resources / 'structure_worker.py'), '--prepare',
        '--models',str(root / 'cache/official_models'), '--output',str(proof)], root,
        cancel_check=cancel_check, timeout=None, log_name='prepare.log')
    result = json.loads(proof.read_text(encoding='utf-8'))
    if result.get('versions') != VERSIONS or result.get('cpu_inference_verified') is not True:
        raise ValueError('STRUCTURE_RUNTIME_VERSION_MISMATCH')
    notify('verifying-model-integrity')
    _lock, files = _read_structure_lock(resources / 'structure-models.lock.json')
    model_paths = _component_model_paths(root, files)
    if model_paths is None:
        raise ValueError('STRUCTURE_MODEL_INTEGRITY_FAILED')
    for item, source in model_paths:
        if file_hash(source) != item['sha256']:
            raise ValueError('STRUCTURE_MODEL_INTEGRITY_FAILED')
    manifest = {'versions':VERSIONS,'cpu_inference_verified':True,'files':files}
    destination = root / 'component-manifest.json'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',suffix='.tmp',dir=root,delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(manifest,handle,sort_keys=True)
    try:
        os.replace(temporary,destination)
    finally:
        temporary.unlink(missing_ok=True)
    return component_status(root)


def worker_environment(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve(strict=True)
    temporary = root / 'temp'
    temporary.mkdir(parents=True, exist_ok=True)
    temporary = temporary.resolve(strict=True)
    if not temporary.is_dir() or not temporary.is_relative_to(root_resolved):
        raise ValueError('STRUCTURE_TEMP_PATH_INVALID')
    temporary = str(temporary)
    env = dict(os.environ)
    env.update(PADDLE_PDX_CACHE_HOME=str(root / 'cache'), HF_HOME=str(root / 'hf-cache'),
        MODELSCOPE_CACHE=str(root / 'modelscope-cache'), PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True',
        PIP_CACHE_DIR=str(root / 'pip-cache'), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    env.update(TEMP=temporary, TMP=temporary, TMPDIR=temporary)
    env.pop('PYTHONPATH', None)
    return env


def run_component_command(command, root, *, cancel_check=None, timeout=180., progress=None,
                          log_name='worker.log', failure_code='STRUCTURE_WORKER_FAILED'):
    """Cancelable native child with file output (no full-pipe deadlock)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with (root / log_name).open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            shell=False, env=worker_environment(root), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                return_code = process.poll()
                if return_code is not None:
                    break
                if cancel_check and cancel_check():
                    if process.poll() is None:
                        raise CancelledError()
                    break
                if timeout is not None and time.monotonic() - started >= timeout:
                    if process.poll() is None:
                        raise TimeoutError('STRUCTURE_WORKER_TIMEOUT')
                    break
                if progress:
                    progress('running')
                time.sleep(.1)
            if process.returncode:
                raise RuntimeError(failure_code)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


@dataclass(frozen=True)
class LocalStructureExtractor:
    root: Path
    engine: object
    cancel_check: object = None
    progress: object = None
    name: str = 'paddle-structure-v3'
    stage_kind: GapStageKind = GapStageKind.LOCAL_STRUCTURE

    def extract_missing(self, subject, filing, concepts):
        from .financial_sources.pdf_document_units import read_document_context
        from .financial_ingestion import _statement_context
        from .financial_cloud_proof import CloudUnitContext
        from .financial_sources.document_units import discover_document_units
        from .financial_sources.structure import paddle_structure, parse_structure
        status = component_status(self.root)
        if not status['available']:
            return CandidateBatch(filing, diagnostics=(status['error_code'] or 'STRUCTURE_COMPONENT_INCOMPLETE',))
        try:
            source_path = Path(filing.local_path)
            if not source_path.is_file():
                return CandidateBatch(filing, diagnostics=('STRUCTURE_SOURCE_UNAVAILABLE',))
        except (OSError, TypeError, ValueError):
            return CandidateBatch(filing, diagnostics=('STRUCTURE_SOURCE_UNAVAILABLE',))
        try:
            source_hash = file_hash(source_path)
        except OSError:
            return CandidateBatch(filing, diagnostics=('STRUCTURE_SOURCE_UNAVAILABLE',))
        if not filing.content_hash or source_hash != filing.content_hash:
            return CandidateBatch(filing, diagnostics=('PDF_SOURCE_HASH_MISMATCH',))
        checkpoint = getattr(self.engine, '_checkpoint_dir', None)
        checkpoint_dir = Path(checkpoint) / 'document-text' if checkpoint is not None else None
        try:
            context = read_document_context(str(source_path), source_hash=filing.content_hash,
                checkpoint_dir=checkpoint_dir, cancel_check=self.cancel_check)
        except CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError):
            return CandidateBatch(filing, diagnostics=('STRUCTURE_DOCUMENT_CONTEXT_FAILED',))
        if context.diagnostic_code == 'DOCUMENT_CONTEXT_CANCELLED':
            raise CancelledError()
        context_diagnostics = (context.diagnostic_code,) if context.diagnostic_code else ()
        if not context.page_texts:
            return CandidateBatch(filing, diagnostics=context_diagnostics or ('STRUCTURE_DOCUMENT_CONTEXT_EMPTY',))
        recovered_texts = list(context.page_texts)
        kinds = {STATEMENT_FOR[concept] for concept in concepts if concept in STATEMENT_FOR}
        selected = set()
        compatibility_rules = getattr(self.engine, '_compatibility_rules', None)
        from .financial_sources.document_catalog import load_document_catalog
        fingerprint = getattr(compatibility_rules, 'semantic_fingerprint', None)
        catalog = load_document_catalog(checkpoint, filing.content_hash, fingerprint()) if checkpoint is not None and callable(fingerprint) else None
        records = catalog.get('pages', ()) if catalog else ()
        for number, text in enumerate(context.page_texts, 1):
            record = records[number-1] if number <= len(records) else {}
            statement = (record['statement'], record.get('scope', '')) if record.get('statement') else (
                None if records else _statement_context(text, compatibility_rules))
            if statement and statement[0] in kinds:
                selected.update(range(max(1,number-1), min(len(context.page_texts),number+3)+1))
        if not selected and records:
            selected.update(item['page'] for item in records if item.get('candidate') is True)
        # Missing titles first use the same lexical rows as the native index.
        if not selected:
            from .financial_ingestion import _labels_for_rules, _label_compact
            aliases = _labels_for_rules(compatibility_rules)
            for number, text in enumerate(context.page_texts, 1):
                compact = _label_compact(text)
                if sum(any(_label_compact(alias) in compact for alias in aliases.get(concept, ()))
                        for concept in concepts) >= min(2, len(concepts)) and compact:
                    selected.update(range(max(1,number-1), min(len(context.page_texts),number+2)+1))
        route_order = None
        adaptive_search = not selected
        if not selected:
            # Scans receive a cheap layout pass first. Keep the entire ranked
            # frontier so a low-scoring continuation or unit page is reachable.
            selected.update(range(1, len(context.page_texts)+1))
            route_order = _structure_layout_route(self.root, source_path, filing.content_hash,
                len(context.page_texts), cancel_check=self.cancel_check)
        ordered_pages = route_order or sorted(selected)
        facts, refs, diagnostics = [], [], list(context_diagnostics)
        recovered_pages = []
        lock_path = structure_lock_path()
        version = hashlib.sha256((file_hash(self.root / 'component-manifest.json')
            + file_hash(lock_path)).encode()).hexdigest()
        worker = Path(__file__).parent / 'resources/structure_worker.py'
        metrics = {'backend':'paddle-structure-v3','versions':dict(VERSIONS),
            'ocr_calls':0,'cache_hits':0,'completed_pages':0,'selected_pages':len(selected)}
        started = time.monotonic()
        durable_pages = set()
        def notify(state, number):
            execution = getattr(self.engine, '_document_execution', None)
            if isinstance(execution, dict):
                observation = execution.setdefault(filing.document_id, {})
                observation.update(source_hash=filing.content_hash,
                    local_structure={**metrics,'state':state,'page':number,
                        'elapsed_seconds':round(time.monotonic()-started,3)})
            if self.progress:
                from .financial_pipeline import _emit_filing_progress
                _emit_filing_progress(self.progress,'filing-parse',metrics['completed_pages'],len(selected),
                    filing,status='local-structure-' + state,
                    outcome={'local_structure':dict(metrics)})
        pending = []
        page_outputs = {}
        for number in ordered_pages:
            identity = {'source':filing.content_hash, 'page':number,
                'strategy':STRUCTURE_STRATEGY_VERSION, 'model_version':version}
            key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            cache = self.root / 'page-results' / key
            cache.mkdir(parents=True, exist_ok=True)
            output = cache / 'result.json'
            page_outputs[number] = output
            try:
                saved = json.loads((cache / 'receipt.json').read_text(encoding='utf-8'))
                if (saved.get('identity') == identity and saved.get('result') == file_hash(output)
                        and saved.get('image') == file_hash(output.with_suffix('.png'))):
                    durable_pages.add(number)
                    continue
            except (OSError, ValueError, KeyError):
                pass
            try:
                if json.loads((cache / 'exhausted.json').read_text(encoding='utf-8')).get('identity') == identity:
                    continue
            except (OSError, ValueError, KeyError):
                pass
            pending.append({'page':number, 'output':str(output), 'identity':identity})
        metrics['completed_pages'] = len(durable_pages)
        if pending:
            if not verify_model_integrity(self.root):
                return CandidateBatch(filing, diagnostics=('STRUCTURE_MODEL_INTEGRITY_FAILED',))
            def ready(number):
                durable_pages.add(number)
                metrics['completed_pages'] = len(durable_pages)
                metrics['ocr_calls'] += 1
                raw_metrics = json.loads(page_outputs[number].read_text(encoding='utf-8')).get('metrics', {})
                if isinstance(raw_metrics, dict):
                    for key in ('model_load_seconds', 'inference_seconds', 'peak_memory_bytes'):
                        value = raw_metrics.get(key)
                        limit = 274_877_906_944 if key == 'peak_memory_bytes' else 86_400
                        if (isinstance(value, bool) or not isinstance(value, (int,float))
                                or not math.isfinite(value) or not 0 <= value <= limit):
                            continue
                        metrics[key] = (metrics.get(key,0) + value if key == 'inference_seconds'
                            else max(metrics.get(key,0), value))
                notify('ready', number)
                if adaptive_search and _structure_targets_ready(subject, filing, concepts,
                        recovered_texts, page_outputs, durable_pages):
                    metrics['targets_complete'] = True
                    return True
            _run_structure_batch(self.root, source_path, worker, pending,
                cancel_check=self.cancel_check, on_ready=ready,
                on_state=lambda number: notify('running', number), metrics=metrics)
        for number in ordered_pages:
            if self.cancel_check and self.cancel_check():
                raise CancelledError()
            if metrics.get('targets_complete') and number not in durable_pages:
                continue
            identity = {'source':filing.content_hash, 'page':number,
                'strategy':STRUCTURE_STRATEGY_VERSION, 'model_version':version}
            key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            cache = self.root / 'page-results' / key
            cache.mkdir(parents=True, exist_ok=True)
            output = cache / 'result.json'
            receipt = cache / 'receipt.json'
            exhausted = cache / 'exhausted.json'
            work = {'identity':identity,'backend':self.name,'backend_version':dict(VERSIONS),
                'statement_types':sorted(kinds),'period_end':getattr(filing,'period_end',''),
                'fiscal_period':getattr(filing,'fiscal_period',''),'scope':'original-statement-scopes',
                'target_concepts':sorted(concepts),'pages':[number],
                'input_artifact_hash':filing.content_hash,'state':'pending'}
            _write_json_atomic(cache/'work.json',work)
            try:
                saved = json.loads(receipt.read_text(encoding='utf-8'))
                reusable = (saved.get('identity') == identity
                    and saved.get('result') == file_hash(output)
                    and saved.get('image') == file_hash(output.with_suffix('.png')))
            except (OSError, ValueError, KeyError):
                reusable = False
            try:
                saved_exhausted = json.loads(exhausted.read_text(encoding='utf-8'))
                is_exhausted = saved_exhausted.get('identity') == identity
            except (OSError, ValueError, KeyError):
                is_exhausted = False
            if is_exhausted:
                diagnostics.append('STRUCTURE_PAGE_FAILED' if number in {item['page'] for item in pending}
                    else 'STRUCTURE_PAGE_EXHAUSTED')
                _write_json_atomic(cache/'work.json',{**work,'state':'exhausted'})
                notify('exhausted',number)
                continue
            try:
                if not reusable:
                    raise RuntimeError('STRUCTURE_PAGE_FAILED')
                if number not in {item['page'] for item in pending}:
                    metrics['cache_hits'] += 1
                raw = json.loads(output.read_text(encoding='utf-8'))
                structures = tuple(paddle_structure(result,number) for result in raw['results'])
                # OCR units may be on a later accounting-basis page. Retain
                # hash-bound structures first, then interpret all candidate
                # cells once the available original context is assembled.
                from .financial_sources.structure import structure_to_markdown
                from .financial_table_text import plain_text
                texts = [plain_text(structure_to_markdown(item)) for item in structures]
                recovered_texts[number-1] = '\n'.join((recovered_texts[number-1], *texts))
                recovered_pages.append((number, output.with_suffix('.png'), structures))
                durable_pages.add(number)
                metrics['completed_pages'] = len(durable_pages)
                _write_json_atomic(cache/'work.json',{**work,'state':'ready',
                    'output_artifact_hash':file_hash(output)})
                notify('ready',number)
            except CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError) as error:
                error_code = str(error)
                if not error_code.startswith('STRUCTURE_'):
                    error_code = 'STRUCTURE_PAGE_FAILED'
                _write_json_atomic(exhausted, {'identity':identity, 'error_code':error_code})
                _write_json_atomic(cache/'work.json',{**work,'state':'exhausted','error_code':error_code})
                diagnostics.append('STRUCTURE_PAGE_FAILED')
                notify('failed',number)
        unit_contexts = tuple(CloudUnitContext(item.page, item.excerpt, filing.content_hash)
            for item in discover_document_units(recovered_texts).declarations if item.scope == 'all')
        for number, image, structures in recovered_pages:
            if self.cancel_check and self.cancel_check():
                raise CancelledError()
            page = VisionPageRequest(number, image.read_bytes(), unit_contexts=unit_contexts)
            for structure in structures:
                candidate = parse_structure(structure,subject,filing,(page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
                diagnostics.extend(candidate.diagnostics)
                # A complete structure with unresolved financial meaning is
                # reusable raw material, not a failed native OCR operation.
        by_id = {ref.evidence_id:ref for ref in refs}
        return CandidateBatch(filing, tuple(FactCandidate(fact,
            tuple(by_id[key] for key in fact_row_evidence_ids(fact) if key in by_id),self.name)
            for fact in facts), tuple(by_id.values()), tuple(dict.fromkeys(diagnostics)))


def _structure_targets_ready(subject, filing, concepts, texts, outputs, pages):
    """Stop scan expansion only after the existing financial compiler agrees."""
    from .financial_sources.structure import paddle_structure, parse_structure, structure_to_markdown
    from .financial_table_text import plain_text
    from .financial_sources.document_units import discover_document_units
    from .financial_cloud_proof import CloudUnitContext
    from .financial_compiler import FinancialFactCompiler, CoveragePlanner, concepts_cover_profile
    from .financial_contracts import CompilationPolicy
    try:
        page_texts = list(texts)
        structures = {}
        for number in sorted(pages):
            payload = json.loads(outputs[number].read_text(encoding='utf-8'))
            structures[number] = tuple(paddle_structure(result, number) for result in payload['results'])
            page_texts[number-1] += '\n' + '\n'.join(plain_text(structure_to_markdown(item))
                for item in structures[number])
        units = tuple(CloudUnitContext(item.page, item.excerpt, filing.content_hash)
            for item in discover_document_units(page_texts).declarations if item.scope == 'all')
        facts, evidence = [], []
        for number, items in structures.items():
            page = VisionPageRequest(number, outputs[number].with_suffix('.png').read_bytes(), unit_contexts=units)
            for item in items:
                batch = parse_structure(item, subject, filing, (page,))
                facts.extend(batch.facts)
                evidence.extend(batch.evidence)
        required = frozenset(concepts) | CoveragePlanner().plan(subject).required_concepts
        result = FinancialFactCompiler().compile(subject, (CandidateBatch(filing,
            tuple(FactCandidate(fact, tuple(evidence), 'paddle-structure-v3') for fact in facts), tuple(evidence)),),
            CompilationPolicy(filings=(filing,), required_concepts=required,
                reporting_currency=subject.reporting_currency, fiscal_period=filing.fiscal_period,
                period_range=(filing.period_end, filing.period_end), scope='consolidated'))
        covered = {fact.concept for fact in result.research_facts
            if fact.accession_number == filing.accession_number and fact.end_date == filing.period_end
            and fact.fiscal_period == filing.fiscal_period and fact.consolidated_scope == 'consolidated'}
        return concepts_cover_profile(covered, required)
    except (AttributeError, OSError, ValueError, TypeError, KeyError):
        return False


def _structure_layout_route(root, source, source_hash, page_count, *, cancel_check=None):
    root = Path(root)
    route_root = root / 'page-routes' / source_hash
    route_root.mkdir(parents=True, exist_ok=True)
    route_path = route_root / 'layout.json'
    try:
        if not route_path.is_file():
            run_component_command([str(root/'runtime/Scripts/python.exe'),
                str(Path(__file__).parent/'resources/structure_worker.py'), '--locate',
                '--models',str(root/'cache/official_models'), '--input',str(source), '--output',str(route_path)],
                route_root, cancel_check=cancel_check, timeout=180.)
        route = json.loads(route_path.read_text(encoding='utf-8'))
        order = [int(item['page']) for item in route['pages']]
        if len(order) == page_count and set(order) == set(range(1, page_count+1)):
            return order
    except CancelledError:
        raise
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError):
        pass
    return None


def _run_structure_batch(root, source, worker, jobs, *, cancel_check=None, on_ready=None, on_state=None, metrics=None):
    """Reuse a model process, publish pages while running, isolate one hung page."""
    root = Path(root)
    remaining = list(jobs)
    metrics = metrics if metrics is not None else {}
    while remaining:
        work_root = root / 'batch-work'
        work_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='batch-', dir=work_root))
        requests = [{**item, 'output':str(work / f"page-{item['page']}.json")} for item in remaining]
        request_path, progress_path = work / 'request.json', work / 'progress.json'
        _write_json_atomic(request_path, requests)
        published = set()
        last_product = time.monotonic()
        active = None

        def harvest(_state=None, *, check_timeout=True, allow_stop=True):
            nonlocal last_product, active
            for item, request in zip(remaining, requests):
                page = item['page']
                temporary, destination = Path(request['output']), Path(item['output'])
                if page in published or not temporary.is_file() or not temporary.with_suffix('.png').is_file():
                    continue
                try:
                    raw = json.loads(temporary.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                if not isinstance(raw, dict) or not isinstance(raw.get('results'), list):
                    continue
                os.replace(temporary.with_suffix('.png'), destination.with_suffix('.png'))
                os.replace(temporary, destination)
                _write_json_atomic(destination.parent / 'receipt.json', {'identity':item['identity'],
                    'result':file_hash(destination), 'image':file_hash(destination.with_suffix('.png'))})
                published.add(page)
                last_product = time.monotonic()
                if on_ready:
                    if on_ready(page) and allow_stop:
                        raise _StructureBatchComplete()
            try:
                state = json.loads(progress_path.read_text(encoding='utf-8'))
                if state.get('page') != active:
                    active = state.get('page')
                    last_product = time.monotonic()
                    if on_state and active is not None:
                        on_state(active)
            except (OSError, ValueError):
                pass
            if check_timeout and time.monotonic() - last_product >= 180:
                raise TimeoutError('STRUCTURE_PAGE_TIMEOUT')

        metrics['worker_start_count'] = metrics.get('worker_start_count', 0) + 1
        command = [str(root / 'runtime/Scripts/python.exe'), str(worker),
            '--models',str(root / 'cache/official_models'), '--input',str(source)]
        # Preserve the standalone one-page command for installed integrations.
        command += (['--page',str(remaining[0]['page']), '--output',requests[0]['output']] if len(remaining) == 1
            else ['--batch',str(request_path), '--output',str(progress_path)])
        failure = None
        try:
            run_component_command(command, work, cancel_check=cancel_check,
                timeout=180. * (len(remaining) + 1), progress=harvest)
        except _StructureBatchComplete:
            return
        except CancelledError:
            harvest(check_timeout=False, allow_stop=False)
            raise
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            failure = exc
        try:
            harvest(check_timeout=False)
        except _StructureBatchComplete:
            return
        next_jobs = []
        for item, request in zip(remaining, requests):
            if item['page'] in published:
                continue
            error_path = Path(request['output']).with_suffix('.error.json')
            if error_path.is_file() or active == item['page'] or active is None or failure is None:
                _write_json_atomic(Path(item['output']).parent / 'exhausted.json',
                    {'identity':item['identity'], 'error_code':'STRUCTURE_PAGE_FAILED'})
            else:
                next_jobs.append(item)
        remaining = next_jobs


class _StructureBatchComplete(Exception):
    """A validated gap search is complete; queued pages stay unconsumed."""


def _write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.tmp',
            dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
