"""Single orchestration boundary for canonical financial recognition.

Acquisition adapters may discover or download filings, but every local,
structured, retry, and cloud candidate enters research through this module.
The coordinator deliberately delegates all acceptance decisions to
``FinancialFactCompiler``; it owns orchestration and terminal state only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from typing import Any, Callable, Sequence

from .domain import Company, FilingDocument
from .financial_compiler import FinancialDataset, FinancialFactCompiler
from .financial_ingestion import FinancialIngestionEngine
from .vision_financials import (
    VisionFallbackConfig,
    VisionFinancialSourceAdapter,
)


class RecognitionState(str, Enum):
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RecognitionOutcome:
    dataset: FinancialDataset
    state: RecognitionState
    elapsed_seconds: float
    diagnostics: tuple[str, ...] = ()


class FinancialRecognitionCoordinator:
    """Deep, model-free recognition seam shared by every service workflow."""

    def __init__(
        self,
        engine: FinancialIngestionEngine,
        compiler: FinancialFactCompiler | None = None,
    ) -> None:
        self.engine = engine
        self.compiler = compiler or FinancialFactCompiler()

    def recognize(
        self,
        subject: Company,
        filings: Sequence[FilingDocument],
        *,
        structured_sources: Sequence[Any] = (),
        vision_fallback: VisionFinancialSourceAdapter | None = None,
        vision_config: VisionFallbackConfig | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[..., None] | None = None,
    ) -> RecognitionOutcome:
        started = time.monotonic()
        cancel = cancel_check or (lambda: False)
        if cancel():
            return RecognitionOutcome(
                FinancialDataset((), (), (), (), (), (), {}, ("CANCELLED",), False),
                RecognitionState.CANCELLED,
                0.0,
                ("CANCELLED",),
            )
        try:
            dataset = self.compiler.compile_from_ingestion(
                subject,
                tuple(filings),
                self.engine,
                structured_sources=tuple(structured_sources),
                vision_fallback=vision_fallback,
                vision_config=vision_config,
                cancel_check=cancel,
                progress=progress,
                reporting_currency=subject.reporting_currency,
            )
        except Exception:
            # Preserve typed adapter/compiler exceptions for the caller while
            # ensuring the coordinator never invents a partial success.
            raise
        elapsed = max(0.0, time.monotonic() - started)
        if cancel():
            state = RecognitionState.CANCELLED
        elif dataset.allow_ai:
            state = RecognitionState.COMPLETE
        elif dataset.resolved_facts:
            state = RecognitionState.BLOCKED
        else:
            state = RecognitionState.FAILED
        return RecognitionOutcome(
            dataset,
            state,
            elapsed,
            tuple(dict.fromkeys(dataset.diagnostics)),
        )
