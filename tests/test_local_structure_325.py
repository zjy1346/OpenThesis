import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from concurrent.futures import CancelledError
from types import SimpleNamespace
import uuid

import pytest

from openthesis import local_structure
from openthesis.resources.structure_worker import VERSIONS


@pytest.fixture
def tmp_path():
    path = Path(__file__).parent / ".local-structure-325" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def _write_lock(tmp_path, monkeypatch, contents=b"locked model"):
    monkeypatch.setattr(local_structure, "_structure_layout_route", lambda *args, **kwargs: None)
    lock = tmp_path / "package" / "resources" / "structure-models.lock.json"
    lock.parent.mkdir(parents=True)
    model = tmp_path / "component" / "cache" / "official_models" / "fixture" / "inference.pdiparams"
    model.parent.mkdir(parents=True)
    model.write_bytes(contents)
    lock_data = {
        "schema": "openthesis-structure-models-v1",
        "versions": VERSIONS,
        "files": [{
            "path": "fixture/inference.pdiparams",
            "size": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
        }],
    }
    lock.write_text(json.dumps(lock_data), encoding="utf-8")
    monkeypatch.setattr(local_structure, "structure_lock_path", lambda: lock)
    root = tmp_path / "component"
    runtime = root / "runtime" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"python")
    (root / "component-manifest.json").write_text(json.dumps({
        "versions": VERSIONS,
        "cpu_inference_verified": True,
        "files": lock_data["files"],
    }), encoding="utf-8")
    return root, lock, model, lock_data


def test_component_status_binds_to_packaged_lock_and_rejects_manifest_path_escape(tmp_path, monkeypatch):
    root, _lock, model, lock_data = _write_lock(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.write_bytes(b"locked model")
    malicious = dict(lock_data["files"][0], path="../../outside")
    (root / "component-manifest.json").write_text(json.dumps({
        "versions": VERSIONS, "cpu_inference_verified": True, "files": [malicious],
    }), encoding="utf-8")

    status = local_structure.component_status(root)

    assert status["available"] is False
    assert status["error_code"] == "STRUCTURE_COMPONENT_INCOMPLETE"
    assert model.is_file()


def test_component_status_uses_only_lightweight_stat_checks(tmp_path, monkeypatch):
    root, _lock, _model, _lock_data = _write_lock(tmp_path, monkeypatch)
    monkeypatch.setattr(local_structure, "file_hash", lambda *_: pytest.fail("status must not hash model files"))

    status = local_structure.component_status(root)

    assert status["available"] is True
    assert status["versions"] == VERSIONS


def test_model_hash_cache_is_invalidated_when_stat_fingerprint_changes(tmp_path, monkeypatch):
    root, lock, model, lock_data = _write_lock(tmp_path, monkeypatch)
    calls = []
    real_hash = local_structure.file_hash

    def counted_hash(path):
        calls.append(Path(path))
        return real_hash(path)

    monkeypatch.setattr(local_structure, "file_hash", counted_hash)
    assert local_structure.verify_model_integrity(root, lock_path=lock) is True
    assert local_structure.verify_model_integrity(root, lock_path=lock) is True
    assert calls == [model.resolve()]

    model.write_bytes(b"LOCKED MODEL")
    assert local_structure.verify_model_integrity(root, lock_path=lock) is False
    assert calls == [model.resolve(), model.resolve()]


def test_worker_environment_keeps_temp_inside_component_root(tmp_path):
    root = tmp_path / "component"

    env = local_structure.worker_environment(root)

    for name in ("TEMP", "TMP", "TMPDIR"):
        assert Path(env[name]).resolve().is_relative_to(root.resolve())
        assert Path(env[name]).is_dir()


def test_worker_environment_rejects_a_temp_symlink_escape(tmp_path):
    root = tmp_path / "component"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "temp").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(ValueError, match="STRUCTURE_TEMP_PATH_INVALID"):
        local_structure.worker_environment(root)


def test_worker_nonzero_exit_preserves_controlled_python_version_code(tmp_path, monkeypatch):
    class FailedProcess:
        returncode = 12

        def poll(self):
            return 12

    monkeypatch.setattr(local_structure.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())

    with pytest.raises(RuntimeError, match="STRUCTURE_RUNTIME_VERSION_MISMATCH"):
        local_structure.run_component_command(
            [sys.executable, "-c", "pass"], tmp_path,
            failure_code="STRUCTURE_RUNTIME_VERSION_MISMATCH",
        )


def test_install_python_probe_uses_controlled_version_failure_code(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_bytes(b"python placeholder")
    root = tmp_path / "component"
    seen = []

    def reject_probe(_command, _root, **kwargs):
        seen.append(kwargs["failure_code"])
        raise RuntimeError(kwargs["failure_code"])

    monkeypatch.setattr(local_structure, "run_component_command", reject_probe)

    with pytest.raises(RuntimeError, match="STRUCTURE_RUNTIME_VERSION_MISMATCH"):
        local_structure.install_component(python, root, confirmed=True)

    assert seen == ["STRUCTURE_RUNTIME_VERSION_MISMATCH"]


def test_command_completion_wins_if_cancellation_arrives_at_process_exit(tmp_path, monkeypatch):
    class CompletingProcess:
        returncode = 0

        def __init__(self):
            self.polls = iter((None, 0))
            self.finished = False

        def poll(self):
            if self.finished:
                return 0
            value = next(self.polls)
            if value is not None:
                self.finished = True
            return value

    monkeypatch.setattr(local_structure.subprocess, "Popen", lambda *args, **kwargs: CompletingProcess())

    local_structure.run_component_command([sys.executable], tmp_path, cancel_check=lambda: True)


def test_missing_source_returns_diagnostic_without_hashing_or_pdf_worker(tmp_path, monkeypatch):
    root, lock, _model, _lock_data = _write_lock(tmp_path, monkeypatch)
    source = tmp_path / "absent.pdf"
    filing = SimpleNamespace(content_hash="a" * 64, local_path=str(source))
    extractor = local_structure.LocalStructureExtractor(root, SimpleNamespace(_checkpoint_dir=None))
    monkeypatch.setattr(local_structure, "verify_model_integrity", lambda *_args, **_kwargs: True)

    batch = extractor.extract_missing(SimpleNamespace(), filing, ("revenue",))

    assert batch.candidates == ()
    assert batch.diagnostics == ("STRUCTURE_SOURCE_UNAVAILABLE",)
    assert lock.is_file()


def test_document_units_are_forwarded_and_page_context_allows_missing_checkpoint(tmp_path, monkeypatch):
    root, lock, _model, _lock_data = _write_lock(tmp_path, monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source pdf bytes")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    filing = SimpleNamespace(content_hash=source_hash, local_path=str(source))
    engine = SimpleNamespace(_checkpoint_dir=None, _compatibility_rules=None)
    extractor = local_structure.LocalStructureExtractor(root, engine)
    text = "These financial statements are expressed in thousands of Hong Kong dollars, except where indicated."
    monkeypatch.setattr(local_structure, "verify_model_integrity", lambda *_args, **_kwargs: True)
    seen_context = {}

    def read_context(path, **kwargs):
        seen_context.update(kwargs)
        return SimpleNamespace(page_texts=(text,), diagnostic_code="")

    monkeypatch.setattr("openthesis.financial_sources.pdf_document_units.read_document_context", read_context)
    monkeypatch.setattr("openthesis.financial_ingestion._statement_context", lambda *_args: None)
    monkeypatch.setattr(local_structure, "run_component_command", lambda command, work, **kwargs: (
        Path(command[command.index("--output") + 1]).write_text(
            json.dumps({"results": [{"unknown_row": "retained"}]}), encoding="utf-8"
        ),
        Path(command[command.index("--output") + 1]).with_suffix(".png").write_bytes(b"image"),
    ))
    structure_inputs = []
    requests = []
    monkeypatch.setattr("openthesis.financial_sources.structure.paddle_structure",
        lambda result, page: {"schema":"financial-structure-v1","blocks":[],
            "native_result": result, "original_page": page})
    monkeypatch.setattr("openthesis.financial_sources.structure.parse_structure",
        lambda payload, _subject, _filing, pages: (
            structure_inputs.append(payload), requests.extend(pages),
            SimpleNamespace(facts=(), evidence=(), diagnostics=()),
        )[-1])

    batch = extractor.extract_missing(SimpleNamespace(), filing, ("revenue",))

    assert seen_context["checkpoint_dir"] is None
    assert batch.diagnostics == ()
    assert structure_inputs[0]["native_result"] == {"unknown_row": "retained"}
    assert requests[0].original_page == 1
    assert requests[0].unit_contexts[0].original_page == 1
    assert requests[0].unit_contexts[0].document_hash == source_hash


def test_failed_page_is_marked_exhausted_and_not_replayed_for_same_identity(tmp_path, monkeypatch):
    root, lock, _model, _lock_data = _write_lock(tmp_path, monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source pdf bytes")
    filing = SimpleNamespace(content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), local_path=str(source))
    extractor = local_structure.LocalStructureExtractor(
        root, SimpleNamespace(_checkpoint_dir=None, _compatibility_rules=None)
    )
    monkeypatch.setattr(local_structure, "verify_model_integrity", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("openthesis.financial_sources.pdf_document_units.read_document_context",
        lambda *_args, **_kwargs: SimpleNamespace(page_texts=("",), diagnostic_code=""))
    monkeypatch.setattr("openthesis.financial_ingestion._statement_context", lambda *_args: None)
    attempts = []

    def fail_worker(*_args, **_kwargs):
        attempts.append(1)
        raise RuntimeError("STRUCTURE_WORKER_FAILED")

    monkeypatch.setattr(local_structure, "run_component_command", fail_worker)
    first = extractor.extract_missing(SimpleNamespace(), filing, ("revenue",))
    second = extractor.extract_missing(SimpleNamespace(), filing, ("revenue",))

    assert attempts == [1]
    assert first.diagnostics == ("STRUCTURE_PAGE_FAILED",)
    assert second.diagnostics == ("STRUCTURE_PAGE_EXHAUSTED",)
    exhausted_records = list((root / "page-results").rglob("exhausted.json"))
    assert len(exhausted_records) == 1
    identity = json.loads(exhausted_records[0].read_text(encoding="utf-8"))["identity"]
    assert set(identity) == {"source", "page", "strategy", "model_version"}

    manifest_path = root / "component-manifest.json"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    third = extractor.extract_missing(SimpleNamespace(), filing, ("revenue",))
    assert attempts == [1, 1]
    assert third.diagnostics == ("STRUCTURE_PAGE_FAILED",)
    assert lock.is_file()


def test_cancelled_page_is_not_persisted_as_exhausted(tmp_path, monkeypatch):
    root, _lock, _model, _lock_data = _write_lock(tmp_path, monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source pdf bytes")
    filing = SimpleNamespace(content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), local_path=str(source))
    extractor = local_structure.LocalStructureExtractor(
        root, SimpleNamespace(_checkpoint_dir=None, _compatibility_rules=None)
    )
    monkeypatch.setattr(local_structure, "verify_model_integrity", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("openthesis.financial_sources.pdf_document_units.read_document_context",
        lambda *_args, **_kwargs: SimpleNamespace(page_texts=("",), diagnostic_code=""))
    monkeypatch.setattr("openthesis.financial_ingestion._statement_context", lambda *_args: None)

    def cancel_worker(*_args, **_kwargs):
        raise CancelledError()

    monkeypatch.setattr(local_structure, "run_component_command", cancel_worker)

    with pytest.raises(CancelledError):
        extractor.extract_missing(SimpleNamespace(), filing, ("revenue",))

    assert not list((root / "page-results").rglob("exhausted.json"))


def test_scanned_later_unit_page_reinterprets_cached_structure_without_rerunning_ocr(tmp_path, monkeypatch):
    from dataclasses import replace
    from test_vision_financials import _filing, _company
    root, _lock, _model, _lock_data = _write_lock(tmp_path, monkeypatch)
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'original image PDF fixture')
    filing = replace(_filing(),local_path=str(source),
        content_hash=hashlib.sha256(source.read_bytes()).hexdigest())
    extractor = local_structure.LocalStructureExtractor(root,SimpleNamespace(_checkpoint_dir=None))
    monkeypatch.setattr(local_structure,'verify_model_integrity',lambda *args:True)
    monkeypatch.setattr('openthesis.financial_sources.pdf_document_units.read_document_context',
        lambda *args,**kwargs:SimpleNamespace(page_texts=('',''),diagnostic_code='DOCUMENT_CONTEXT_OCR_REQUIRED'))
    calls = []
    def run(command,*args,**kwargs):
        if '--batch' in command:
            jobs = json.loads(Path(command[command.index('--batch')+1]).read_text(encoding='utf-8'))
            for job in jobs:
                run([*command[:command.index('--batch')], '--page', str(job['page']), '--output', job['output']])
            return
        page = int(command[command.index('--page')+1])
        output = Path(command[command.index('--output')+1])
        calls.append(page)
        blocks = ([{'block_label':'doc_title','block_content':'合并利润表'},
            {'block_label':'table','block_content':'<table><tr><td>项目</td><td>2025年</td><td>2024年</td></tr><tr><td>营业收入</td><td>100</td><td>80</td></tr></table>'}]
            if page == 1 else [{'block_label':'text','block_content':'本财务报表除另有说明外，所有金额均以人民币千元表示。'}])
        output.write_text(json.dumps({'results':[{'parsing_res_list':blocks}]}),encoding='utf-8')
        output.with_suffix('.png').write_bytes(b'original page image')
    monkeypatch.setattr(local_structure,'run_component_command',run)
    first = extractor.extract_missing(_company(),filing,('revenue',))
    assert {(candidate.fact.fiscal_year,candidate.fact.value) for candidate in first.candidates} == {(2025,100_000),(2024,80_000)}
    assert any(ref.locator == 'page:2' for ref in first.evidence if ref.evidence_id.startswith('vision-unit:'))
    second = extractor.extract_missing(_company(),filing,('revenue',))
    assert len(second.candidates) == 2 and calls == [1,2]
    assert not list((root/'page-results').rglob('exhausted.json'))


def _batch_jobs(root):
    jobs = []
    for page in (1, 2):
        output = root / 'page-results' / str(page) / 'result.json'
        output.parent.mkdir(parents=True)
        jobs.append({'page':page, 'output':str(output), 'identity':{'source':'fixed', 'page':page}})
    return jobs


def _write_batch_page(job):
    path = Path(job['output'])
    path.write_text(json.dumps({'results':[]}), encoding='utf-8')
    path.with_suffix('.png').write_bytes(b'page-image')


def test_batch_reuses_one_command_and_persists_pages_before_process_finishes(tmp_path, monkeypatch):
    jobs = _batch_jobs(tmp_path)
    calls, ready = [], []
    def run(command, _root, **options):
        calls.append(command)
        requests = json.loads(Path(command[command.index('--batch')+1]).read_text(encoding='utf-8'))
        for item in requests:
            _write_batch_page(item)
            options['progress']('running')
            assert (Path(jobs[item['page']-1]['output']).parent / 'receipt.json').is_file()
    monkeypatch.setattr(local_structure, 'run_component_command', run)
    local_structure._run_structure_batch(tmp_path, 'source.pdf', 'worker.py', jobs, on_ready=ready.append)
    assert len(calls) == 1 and ready == [1, 2]


def test_batch_cancel_preserves_completed_page_without_exhausting_unstarted_page(tmp_path, monkeypatch):
    jobs = _batch_jobs(tmp_path)
    def run(command, _root, **options):
        requests = json.loads(Path(command[command.index('--batch')+1]).read_text(encoding='utf-8'))
        _write_batch_page(requests[0])
        options['progress']('running')
        raise CancelledError()
    monkeypatch.setattr(local_structure, 'run_component_command', run)
    with pytest.raises(CancelledError):
        local_structure._run_structure_batch(tmp_path, 'source.pdf', 'worker.py', jobs)
    assert (Path(jobs[0]['output']).parent/'receipt.json').is_file()
    assert not list(tmp_path.rglob('exhausted.json'))


@pytest.mark.parametrize('invalid_payload', ['{invalid', '[]', '{"results":"invalid"}'])
def test_malformed_batch_page_does_not_discard_valid_sibling(tmp_path, monkeypatch, invalid_payload):
    jobs = _batch_jobs(tmp_path)
    ready = []
    def run(command, _root, **options):
        requests = json.loads(Path(command[command.index('--batch')+1]).read_text(encoding='utf-8'))
        _write_batch_page(requests[0])
        Path(requests[0]['output']).write_text(invalid_payload, encoding='utf-8')
        _write_batch_page(requests[1])
        options['progress']('running')
    monkeypatch.setattr(local_structure, 'run_component_command', run)
    local_structure._run_structure_batch(tmp_path, 'source.pdf', 'worker.py', jobs, on_ready=ready.append)
    assert ready == [2]
    assert (Path(jobs[0]['output']).parent/'exhausted.json').is_file()
    assert (Path(jobs[1]['output']).parent/'receipt.json').is_file()


def test_validated_batch_target_stops_scan_expansion_and_keeps_frontier_pending(tmp_path, monkeypatch):
    jobs = _batch_jobs(tmp_path)
    seen = []
    def run(command, _root, **options):
        requests = json.loads(Path(command[command.index('--batch')+1]).read_text(encoding='utf-8'))
        for request in requests:
            seen.append(request['page'])
            _write_batch_page(request)
            options['progress']('running')
    monkeypatch.setattr(local_structure, 'run_component_command', run)
    local_structure._run_structure_batch(tmp_path, 'source.pdf', 'worker.py', jobs, on_ready=lambda page:page == 1)
    assert seen == [1]
    assert not list(tmp_path.rglob('exhausted.json'))


def test_layout_route_preserves_every_page_and_reuses_its_source_bound_plan(tmp_path, monkeypatch):
    calls = []
    def run(command, _root, **_options):
        calls.append(command)
        output = Path(command[command.index('--output')+1])
        output.write_text(json.dumps({'pages':[{'page':2}, {'page':1}, {'page':3}]}), encoding='utf-8')
    monkeypatch.setattr(local_structure, 'run_component_command', run)
    assert local_structure._structure_layout_route(tmp_path, 'source.pdf', 'hash', 3) == [2, 1, 3]
    assert local_structure._structure_layout_route(tmp_path, 'source.pdf', 'hash', 3) == [2, 1, 3]
    assert len(calls) == 1 and '--locate' in calls[0]
