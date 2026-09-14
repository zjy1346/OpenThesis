"""Single quality-preserving entry point for financial evidence recognition.

Callers provide an immutable request describing the already selected official
documents and the adapters authorized for this attempt.  The coordinator owns
the recognition invocation so initial research, automatic recovery and manual
rebuilds cannot silently omit structured or visual capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .domain import Company, FilingDocument
from .financial_recognition import FinancialRecognitionCoordinator, RecognitionOutcome
from .vision_financials import VisionFallbackConfig, VisionFinancialSourceAdapter


@dataclass(frozen=True, slots=True)
class FinancialEvidenceRequest:
    subject: Company
    filings: tuple[FilingDocument, ...]
    structured_sources: tuple[Any, ...] = ()
    vision_fallback: VisionFinancialSourceAdapter | None = None
    vision_config: VisionFallbackConfig | None = None
    cancel_check: Callable[[], bool] | None = None
    progress: Callable[..., None] | None = None


class FinancialEvidenceCoordinator:
    """Deep seam that keeps every recognition capability in one request."""

    def __init__(
        self,
        recognition_for: Callable[
            [Company, Sequence[FilingDocument]], FinancialRecognitionCoordinator
        ],
    ) -> None:
        self._recognition_for = recognition_for

    def execute(self, request: FinancialEvidenceRequest) -> RecognitionOutcome:
        recognition = self._recognition_for(request.subject, request.filings)
        return recognition.recognize(
            request.subject,
            request.filings,
            structured_sources=request.structured_sources,
            vision_fallback=request.vision_fallback,
            vision_config=request.vision_config,
            cancel_check=request.cancel_check,
            progress=request.progress,
        )
