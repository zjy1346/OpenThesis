"""Deep application seams used by :mod:`openthesis.service`.

These components own one concern each while preserving the existing JSON-RPC
surface.  They deliberately accept the already-configured collaborators so
desktop, tests, and headless callers keep the same dependency seams.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from .financial_recovery import FinancialRecoveryController
from .financial_recognition import FinancialRecognitionCoordinator
from .financial_ingestion import FinancialIngestionEngine
from .research import ResearchWorkflow


class DisclosureService:
    """Resolve companies and capture market disclosures through one seam."""

    def __init__(self, market_data: Any, market_snapshot: Any):
        self.market_data = market_data
        self.market_snapshot = market_snapshot

    def resolve(self, query: str, market: Any, *, limit: int = 15) -> Sequence[Any]:
        return self.market_data.resolve(query, market, limit=limit)

    def adapter_for(self, company: Any) -> Any:
        return self.market_data.adapter_for(company)

    def capture(self, company: Any, policy: Any, manual: Any = None) -> Any:
        if manual is None:
            return self.market_snapshot.capture(company, policy)
        return self.market_snapshot.capture(company, policy, manual)


class FinancialPipeline:
    """Own the financial engine, recognition, and recovery lifecycle."""

    def __init__(
        self,
        ingestion: FinancialIngestionEngine,
        recognition: FinancialRecognitionCoordinator,
        recovery: FinancialRecoveryController,
    ):
        self.ingestion = ingestion
        self.recognition = recognition
        self.recovery = recovery

    def ingestion_for(self, company: Any, filings: Sequence[Any]) -> Any:
        clone = getattr(self.ingestion, "with_compatibility_rules", None)
        if not callable(clone):
            return self.ingestion
        report_type = "annual" if any(
            str(item.fiscal_period or "FY").upper() == "FY" for item in filings
        ) else "quarterly"
        rules = self.recovery.compatibility_rules(company, report_type)
        return clone(rules)

    def recognition_for(self, company: Any, filings: Sequence[Any]) -> FinancialRecognitionCoordinator:
        engine = self.ingestion_for(company, filings)
        if engine is self.ingestion:
            return self.recognition
        return FinancialRecognitionCoordinator(engine)

    def discover(self, adapter: Any, company: Any, *, annual_limit: int, force: bool = False) -> Any:
        return self.recovery.discover(adapter, company, annual_limit=annual_limit, force=force)

    def compatibility_rules(self, company: Any, report_type: str) -> Any:
        return self.recovery.compatibility_rules(company, report_type)

    def compatibility_summary(self, market: Any, report_type: str) -> Any:
        return self.recovery.compatibility_summary(market, report_type)


class ResearchOrchestrator:
    """Construct research workflows without owning JSON-RPC policy."""

    def __init__(self, storage: Any, provider_factory: Callable[[Any], Any]):
        self.storage = storage
        self.provider_factory = provider_factory

    def create(
        self,
        selected_pack: Any,
        config: Any,
        *,
        cancel_check: Callable[[], bool] | None = None,
        report_language: str = "zh-CN",
        ui_language: str = "zh-CN",
        parallel_agents: bool = False,
        agent_progress: Callable[[str, str], None] | None = None,
    ) -> ResearchWorkflow:
        return ResearchWorkflow(
            self.storage,
            selected_pack,
            self.provider_factory(config),
            config,
            cancel_check=cancel_check,
            report_language=report_language,
            ui_language=ui_language,
            parallel_agents=parallel_agents,
            agent_progress=agent_progress,
        )


class ReportService:
    """Render both report projections from the same canonical artifacts."""

    def __init__(self, markdown_renderer: Callable[..., str], html_renderer: Callable[..., str]):
        self._markdown_renderer = markdown_renderer
        self._html_renderer = html_renderer

    def markdown(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> str:
        return self._markdown_renderer(run_id, artifacts, **kwargs)

    def html(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> str:
        return self._html_renderer(run_id, artifacts, **kwargs)

    def render(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> tuple[str, str]:
        return self.markdown(run_id, artifacts, **kwargs), self.html(run_id, artifacts, **kwargs)
