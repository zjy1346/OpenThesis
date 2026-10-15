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
from .financial_pipeline import (
    FinancialPipeline,
    FinancialResearchSnapshot,
)
from .financial_contracts import CompilationPolicy, GapStageKind
from .financial_admission import FinancialResearchView
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
    research_view: FinancialResearchView | None = None


class FinancialRecognitionCoordinator:
    """Deep, model-free recognition seam shared by every service workflow."""

    def __init__(
        self,
        engine: FinancialIngestionEngine,
        compiler: FinancialFactCompiler | None = None,
    ) -> None:
        self.engine = engine
        self.compiler = compiler or FinancialFactCompiler()
        self.pipeline = FinancialPipeline(self.engine, self.compiler)

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
        selected_filings = tuple(filings)
        authorized_stages = (
            frozenset({GapStageKind.MINERU})
            if bool(getattr(vision_config, "enabled", False))
            and bool(getattr(vision_config, "consent", False))
            else frozenset()
        )
        view = self.pipeline.prepare(
            FinancialResearchSnapshot(
                subject=subject,
                filings=selected_filings,
                candidate_sources=tuple(structured_sources),
                authorized_source_stages=authorized_stages,
                vision_fallback=vision_fallback,
                vision_config=vision_config,
                cancel_check=cancel,
                progress=progress,
            ),
            CompilationPolicy(
                filings=selected_filings,
                reporting_currency=subject.reporting_currency,
            ),
        )
        dataset = view.canonical_dataset
        if not isinstance(dataset, FinancialDataset):
            raise TypeError("FINANCIAL_PIPELINE_CANONICAL_DATASET_REQUIRED")
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
            view,
        )
