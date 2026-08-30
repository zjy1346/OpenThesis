from __future__ import annotations

import unittest

from openthesis.domain import Company, FilingDocument, FinancialFact
from openthesis.financial_compiler import FinancialDataset
from openthesis.financial_recognition import (
    FinancialRecognitionCoordinator,
    RecognitionState,
)


def _company() -> Company:
    return Company(
        "CN_A:SZSE:002594.SZ", "002594.SZ", "BYD", "SZSE",
        market="CN_A", reporting_currency="CNY",
    )


def _filing() -> FilingDocument:
    return FilingDocument(
        "filing-2025", _company().security_id, "2025", "ANNUAL_REPORT",
        "FY", "2025-12-31", "2026-03-01", "2025 Annual Report",
        "https://example.invalid/2025.pdf",
    )


def _fact() -> FinancialFact:
    filing = _filing()
    return FinancialFact(
        "fact-1", _company().security_id, "revenue", "revenue", 100.0,
        "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01",
        "2025-12-31", filing.filed_at, filing.accession_number,
        filing.source_url,
    )


def _dataset(*, allow_ai: bool, resolved: bool = True) -> FinancialDataset:
    facts = (_fact(),) if resolved else ()
    return FinancialDataset(
        (_filing(),), facts, (), (), (), (), {}, (), allow_ai,
        facts if allow_ai else (), (), (),
    )


class RecordingCompiler:
    def __init__(self, dataset: FinancialDataset):
        self.dataset = dataset
        self.calls = []

    def compile_from_ingestion(self, subject, filings, engine, **kwargs):
        self.calls.append((subject, tuple(filings), engine, kwargs))
        return self.dataset


class FinancialRecognitionCoordinatorTests(unittest.TestCase):
    def test_complete_blocked_and_failed_states_come_only_from_canonical_dataset(self):
        for expected, dataset in (
            (RecognitionState.COMPLETE, _dataset(allow_ai=True)),
            (RecognitionState.BLOCKED, _dataset(allow_ai=False)),
            (RecognitionState.FAILED, _dataset(allow_ai=False, resolved=False)),
        ):
            with self.subTest(expected=expected):
                compiler = RecordingCompiler(dataset)
                coordinator = FinancialRecognitionCoordinator(object(), compiler)
                outcome = coordinator.recognize(_company(), [_filing()])
                self.assertEqual(outcome.state, expected)
                self.assertIs(outcome.dataset, dataset)
                self.assertEqual(len(compiler.calls), 1)

    def test_cancelled_before_start_never_calls_compiler(self):
        compiler = RecordingCompiler(_dataset(allow_ai=True))
        outcome = FinancialRecognitionCoordinator(object(), compiler).recognize(
            _company(), [_filing()], cancel_check=lambda: True
        )
        self.assertEqual(outcome.state, RecognitionState.CANCELLED)
        self.assertEqual(compiler.calls, [])
        self.assertFalse(outcome.dataset.allow_ai)


if __name__ == "__main__":
    unittest.main()
