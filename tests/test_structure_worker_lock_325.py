"""The optional model preparer must use only pinned, hash-verified files."""
import hashlib
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from openthesis.resources import structure_worker


def _revision(name: str) -> str:
    return hashlib.sha1(f"test-pinned-revision:{name}".encode()).hexdigest()


def _fixture(tmp_path, monkeypatch):
    worker_dir = tmp_path / "worker-package"
    worker_dir.mkdir()
    worker_file = worker_dir / "structure_worker.py"
    worker_file.write_text("# mocked resource location\n", encoding="utf-8")
    monkeypatch.setattr(structure_worker, "__file__", str(worker_file))

    directory = tmp_path / "models"
    payloads = {}
    models = [
        {"name": name, "revision": _revision(name)}
        for name in structure_worker.MODELS.values()
    ]
    files = []
    for model in models:
        payload = f"small fixture model file:{model['name']}".encode()
        payloads[model["name"]] = payload
        files.append({
            "path": f"{model['name']}/inference.yml",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    lock = {
        "versions": structure_worker.VERSIONS,
        "models": models,
        "files": files,
    }
    (worker_dir / "structure-models.lock.json").write_text(
        json.dumps(lock), encoding="utf-8",
    )
    return directory, lock, payloads


def _install_download_mock(monkeypatch, callback):
    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = callback
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)


def _write_locked_files(directory, lock, payloads):
    for item in lock["files"]:
        path = directory / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        model_name = item["path"].split("/", 1)[0]
        path.write_bytes(payloads[model_name])


def test_prepare_models_skips_download_when_all_locked_hashes_match(tmp_path, monkeypatch):
    directory, lock, payloads = _fixture(tmp_path, monkeypatch)
    _write_locked_files(directory, lock, payloads)
    snapshot_download = Mock()
    _install_download_mock(monkeypatch, snapshot_download)

    structure_worker.prepare_models(directory)

    snapshot_download.assert_not_called()


def test_prepare_models_downloads_only_mismatch_from_pinned_official_revision(tmp_path, monkeypatch):
    directory, lock, payloads = _fixture(tmp_path, monkeypatch)
    _write_locked_files(directory, lock, payloads)
    damaged_model = lock["models"][0]
    (directory / damaged_model["name"] / "inference.yml").write_bytes(b"tampered")

    def download(*, repo_id, revision, allow_patterns, local_dir):
        assert repo_id == f"PaddlePaddle/{damaged_model['name']}"
        assert re.fullmatch(r"[0-9a-f]{40}", revision)
        assert revision == damaged_model["revision"]
        assert allow_patterns == ["inference.yml"]
        assert Path(local_dir) == directory / damaged_model["name"]
        target = Path(local_dir) / allow_patterns[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payloads[damaged_model["name"]])

    snapshot_download = Mock(side_effect=download)
    _install_download_mock(monkeypatch, snapshot_download)

    structure_worker.prepare_models(directory)

    snapshot_download.assert_called_once_with(
        repo_id=f"PaddlePaddle/{damaged_model['name']}",
        revision=damaged_model["revision"],
        allow_patterns=["inference.yml"],
        local_dir=directory / damaged_model["name"],
    )


def test_prepare_models_rejects_download_that_fails_locked_hash(tmp_path, monkeypatch):
    directory, lock, _payloads = _fixture(tmp_path, monkeypatch)
    damaged_model = lock["models"][0]

    def download(*, repo_id, revision, allow_patterns, local_dir):
        assert repo_id == f"PaddlePaddle/{damaged_model['name']}"
        assert revision == damaged_model["revision"]
        assert allow_patterns == ["inference.yml"]
        target = Path(local_dir) / allow_patterns[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not the locked bytes")

    snapshot_download = Mock(side_effect=download)
    _install_download_mock(monkeypatch, snapshot_download)

    with pytest.raises(ValueError, match="STRUCTURE_MODEL_INTEGRITY_FAILED"):
        structure_worker.prepare_models(directory)

    snapshot_download.assert_called_once_with(
        repo_id=f"PaddlePaddle/{damaged_model['name']}",
        revision=damaged_model["revision"],
        allow_patterns=["inference.yml"],
        local_dir=directory / damaged_model["name"],
    )
