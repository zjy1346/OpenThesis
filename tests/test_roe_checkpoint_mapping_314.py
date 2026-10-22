import hashlib
import json
from dataclasses import replace

from openthesis.financial_checkpoint import CheckpointKey, PdfWindow, WindowCheckpoint, WindowCheckpointStore
from openthesis.financial_contracts import PDF_PARSER_VERSION
from test_source_evidence_flow import source_fixture


def test_old_roe_window_is_local_miss_but_untouched_window_remains_reusable(tmp_path):
    _company, _filing, fact, row, _unit = source_fixture()
    fact = replace(fact, parser_version=PDF_PARSER_VERSION)
    store = WindowCheckpointStore(tmp_path)
    key = CheckpointKey("original-hash", PDF_PARSER_VERSION, "rules")
    for index, concept in enumerate(("revenue", "reported_roe")):
        checkpoint = WindowCheckpoint(key, PdfWindow(index, (index + 1,)),
            (replace(fact, concept=concept),), (row,), {})
        store.save(checkpoint)
        assert store.load(key, index) is not None
        path = store.path_for(key, index)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"].pop("roe_row_mapping_version")
        encoded = json.dumps(envelope["payload"], ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()
        envelope["sha256"] = hashlib.sha256(encoded).hexdigest()
        path.write_text(json.dumps(envelope), encoding="utf-8")
    assert store.load(key, 0) is not None
    assert store.load(key, 1) is None
    assert store.path_for(key, 1).is_file()  # No destructive cache cleanup.
    store.save_exhausted(key, reason="old deterministic failure", failed_window=PdfWindow(2, (3,)))
    marker = store.exhausted_path(key)
    old = json.loads(marker.read_text(encoding="utf-8"))
    old.pop("roe_row_mapping_version")
    marker.write_text(json.dumps(old), encoding="utf-8")
    assert store.load_exhausted(key) is None  # Only the affected old mapping is retried.
    assert marker.is_file()
    store.save_exhausted(key, reason="current deterministic failure", failed_window=PdfWindow(2, (3,)))
    assert store.load_exhausted(key)["reason"] == "current deterministic failure"
