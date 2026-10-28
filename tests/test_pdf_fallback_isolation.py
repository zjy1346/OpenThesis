"""Alternative PDF readers must keep the same hard process boundary."""
import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from openthesis.financial_ingestion import (
    _candidate_financial_pages_with_diagnostic, _pdf_page_count,
)


def test_sidecar_launcher_freezes_multiprocessing_before_import():
    root = Path(__file__).resolve().parents[1]
    launcher_path = root / "src" / "openthesis" / "__main__.py"
    launcher = ast.parse(launcher_path.read_text(encoding="utf-8"))
    entrypoint = next(node for node in launcher.body
                      if isinstance(node, ast.FunctionDef) and node.name == "entrypoint")
    freeze_indices = [index for index, statement in enumerate(entrypoint.body)
                      if any(isinstance(node, ast.Call)
                             and isinstance(node.func, ast.Attribute)
                             and node.func.attr == "freeze_support"
                             for node in ast.walk(statement))]
    sidecar_indices = [index for index, statement in enumerate(entrypoint.body)
                       if any(isinstance(node, ast.ImportFrom) and node.module == "sidecar"
                              for node in ast.walk(statement))]
    assert freeze_indices, "launcher must enable multiprocessing freeze support"
    assert sidecar_indices, "launcher must import the sidecar entrypoint"
    assert min(freeze_indices) < min(sidecar_indices)


def test_importing_sidecar_launcher_does_not_initialize_service_in_subprocess():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    process = subprocess.run(
        [sys.executable, "-c",
         "import sys; import openthesis.__main__; "
         "assert 'openthesis.sidecar' not in sys.modules; "
         "assert 'openthesis.service' not in sys.modules"],
        cwd=root, env=env, capture_output=True, text=True, timeout=15,
    )
    assert process.returncode == 0, process.stderr[-1000:]


def test_alternative_index_is_never_extracted_on_the_coordinator_thread():
    with (
        patch("openthesis.financial_ingestion._run_pdfium_isolated", side_effect=[
            (None, "PDFIUM_WORKER_EXIT"), ((frozenset({3, 8}), None), None),
        ]) as isolated,
        patch("openthesis.financial_ingestion._candidate_financial_pages_pypdf",
              side_effect=AssertionError("unbounded coordinator-thread extraction")),
    ):
        pages, diagnostic = _candidate_financial_pages_with_diagnostic("private.pdf")
    assert pages == frozenset({3, 8})
    assert diagnostic == "PDFIUM_WORKER_EXIT"
    assert isolated.call_count == 2
    assert isolated.call_args.kwargs["timeout_seconds"] > 0


def test_alternative_index_start_failure_preserves_a_safe_diagnostic():
    with (
        patch("openthesis.financial_ingestion._run_pdfium_isolated", side_effect=[
            (None, "PDFIUM_WORKER_EXIT"), (None, "PDFIUM_WORKER_START_FAILED"),
        ]) as isolated,
        patch("openthesis.financial_ingestion._candidate_financial_pages_pypdf",
              side_effect=AssertionError("must not evade isolation failure")),
    ):
        pages, diagnostic = _candidate_financial_pages_with_diagnostic("private.pdf")
    assert pages is None
    assert diagnostic == "PDFIUM_WORKER_START_FAILED"
    assert isolated.call_count == 2
    assert "private.pdf" not in diagnostic


def test_alternative_page_count_keeps_cancellation_and_a_deadline():
    cancelled = lambda: False
    with patch("openthesis.financial_ingestion._run_pdfium_isolated", side_effect=[
        (None, "PDFIUM_WORKER_EXIT"), (12, None),
    ]) as isolated:
        assert _pdf_page_count("private.pdf", cancel_check=cancelled) == 12
    assert isolated.call_count == 2
    assert isolated.call_args.kwargs["cancel_check"] is cancelled
    assert isolated.call_args.kwargs["timeout_seconds"] > 0


def test_cancelled_native_index_does_not_start_an_alternative_reader():
    with patch("openthesis.financial_ingestion._run_pdfium_isolated",
               return_value=(None, "PDFIUM_WORKER_CANCELLED")) as isolated:
        assert _candidate_financial_pages_with_diagnostic("private.pdf") == (
            None, "pdf_parse_cancelled")
    assert isolated.call_count == 1
